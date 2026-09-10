#!/usr/bin/env bash
set -euo pipefail

readonly deploy_remote="origin"
readonly deploy_branch="main"
readonly serve_unit="atelier2-serve.service"
readonly health_url="http://127.0.0.1:8422/atelier/api/v1/health"
readonly definition_source_ref="refs/heads/${deploy_branch}"
readonly -a definition_source_selections=(
  "workflows/*.yaml=workflow"
  "workflows/schemas/*.json=schema"
  "workflows/budgets/*.json=budget_policy"
)
readonly definition_source_actor="atelier2-deploy"
# A refused intake still lets the new commit serve (the catalog keeps its
# previous workflow state), so this is not the generic fail() exit 1: it is
# its own code, distinct in the journal, and still nonzero so
# auto_redeploy.sh's plain success/failure exit-code check counts the tick as
# a failure without the watcher needing a second signal.
readonly intake_refused_exit_code=3

log() {
  echo "serve live update: $1"
}

fail() {
  echo "serve live update: $1" >&2
  exit 1
}

requested_target_commit="${1:-}"

repository="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
frontend="${repository}/frontend"
root_package="${repository}/package.json"

if [[ -n "${XDG_DATA_HOME:-}" ]]; then
  data_home="${XDG_DATA_HOME}"
elif [[ -n "${HOME:-}" ]]; then
  data_home="${HOME}/.local/share"
else
  fail "HOME and XDG_DATA_HOME are both unset; the live store cannot be located"
fi
live_store="${data_home}/atelier2/live-store"
database="${live_store}/atelier.sqlite"

build_checkout() {
  log "syncing the locked Python environment"
  (cd "${repository}" && uv sync --locked) || return 1

  log "building the frontend"
  (cd "${frontend}" && npm ci && npm run build) || return 1
  [[ -s "${frontend}/dist/index.html" ]] || {
    echo "serve live update: frontend/dist/index.html is missing or empty after the build" >&2
    return 1
  }
}

prepare_root_package() {
  log "checking the root package.json stub"
  [[ -e "${root_package}" || -L "${root_package}" ]] || return 0

  if [[ -L "${root_package}" ]]; then
    echo "serve live update: root package.json is a symlink; refusing to remove it" >&2
    return 1
  fi

  if git -C "${repository}" ls-files --error-unmatch -- package.json >/dev/null 2>&1; then
    echo "serve live update: root package.json is tracked; refusing to remove it" >&2
    return 1
  fi
  if [[ -s "${root_package}" ]]; then
    echo "serve live update: untracked root package.json is not empty; refusing to remove it" >&2
    return 1
  fi
  if ! git -C "${repository}" check-ignore --quiet -- package.json; then
    echo "serve live update: zero-byte root package.json is not excluded; refusing to remove it" >&2
    return 1
  fi

  log "removing excluded zero-byte root package.json stub"
  rm -- "${root_package}"
}

backup_file() {
  local source="$1" destination="$2" source_size destination_size
  cp -- "${source}" "${destination}" || return 1
  [[ -f "${destination}" ]] || return 1
  source_size="$(stat -c %s -- "${source}")" || return 1
  destination_size="$(stat -c %s -- "${destination}")" || return 1
  [[ "${source_size}" == "${destination_size}" ]] || {
    echo "serve live update: backup size mismatch for ${source}" >&2
    return 1
  }
}

backup_live_store() {
  local backup_directory filename source
  backup_directory="${live_store}/backups/pre-redeploy-$(date -u +%Y%m%dT%H%M%S.%NZ)"

  [[ -f "${database}" ]] || {
    echo "serve live update: live database is missing: ${database}" >&2
    return 1
  }
  [[ -f "${live_store}/external.sqlite" ]] || {
    echo "serve live update: live effect store is missing: ${live_store}/external.sqlite" >&2
    return 1
  }
  mkdir -p -- "${live_store}/backups" || return 1
  mkdir -- "${backup_directory}" || return 1

  for filename in atelier.sqlite atelier.sqlite-wal atelier.sqlite-shm external.sqlite; do
    source="${live_store}/${filename}"
    [[ -e "${source}" ]] || continue
    backup_file "${source}" "${backup_directory}/${filename}" || return 1
  done
}

rollback_previous_serve() {
  log "restoring ${previous_commit}"
  git -C "${repository}" reset --hard "${previous_commit}" || return 1
  build_checkout || return 1
  log "starting the previous atelier2-serve.service"
  systemctl --user start "${serve_unit}" || return 1
  wait_for_served_health || return 1
  [[ "${served_status}" == "serving" && "${served_commit}" == "${previous_commit}" ]]
}

served_status=""
served_commit=""
intake_refused=0
# Duplicates auto_redeploy.sh's read_served_health; a later slice extracts the
# shared health-parsing helper.
read_served_health() {
  local body
  body="$(curl -fsS --max-time 5 "${health_url}")" || return 1
  served_status="$(grep -oE '"status"[[:space:]]*:[[:space:]]*"[a-z_]+"' <<<"${body}" \
    | head -n 1 | sed -E 's/^.*"([a-z_]+)"$/\1/')" || true
  served_commit="$(grep -oE '"source_commit"[[:space:]]*:[[:space:]]*"[0-9a-f]{40}"' <<<"${body}" \
    | head -n 1 | sed -E 's/^.*"([0-9a-f]{40})"$/\1/')" || true
  [[ -n "${served_status}" && -n "${served_commit}" ]]
}

read_deployed_commit_marker() {
  [[ -f "${deployed_commit_marker}" ]] || return 1
  IFS= read -r previous_commit <"${deployed_commit_marker}" || return 1
  [[ "${previous_commit}" =~ ^[0-9a-f]{40}$ ]]
}

# Polls the same way container_live.sh's start_container waits for
# container_is_healthy (scripts/container_live.sh:576-584): the unit is
# Type=exec, so uvicorn is not yet listening the instant `systemctl start`
# returns.
wait_for_served_health() {
  local attempt
  log "waiting up to 30s for live serve health"
  for attempt in $(seq 1 30); do
    read_served_health && return 0
    sleep 1
  done
  return 1
}

connect_and_intake_definitions() {
  local connect_output source_id intake_output selection
  local -a selection_arguments=()
  for selection in "${definition_source_selections[@]}"; do
    selection_arguments+=(--select "${selection}")
  done

  log "connecting the workflow definition source"
  if ! connect_output="$(cd "${repository}" && uv run --locked atelier2 definition-source connect \
      --database "${database}" --location "${repository}" --ref "${definition_source_ref}" \
      "${selection_arguments[@]}" --actor "${definition_source_actor}" 2>&1)"; then
    printf '%s\n' "${connect_output}" >&2
    return 1
  fi
  while IFS= read -r line; do log "${line}"; done <<<"${connect_output}"
  source_id="$(grep -oE '[0-9a-f]{64}' <<<"${connect_output}" | head -n 1)"
  if [[ -z "${source_id}" ]]; then
    echo "serve live update: no definition source id was readable from connect's output" >&2
    return 1
  fi

  log "taking the connected source's workflows into the live catalog"
  if intake_output="$(cd "${repository}" && uv run --locked atelier2 definition-source intake \
      --database "${database}" --source-id "${source_id}" --actor "${definition_source_actor}" 2>&1)"; then
    intake_refused=0
  else
    intake_refused=1
  fi
  while IFS= read -r line; do log "${line}"; done <<<"${intake_output}"
  if ((intake_refused)); then
    echo "serve live update: WORKFLOW INTAKE REFUSED; the live serve starts with the previous catalog state" >&2
  fi
}

log "checking the deploy checkout"
current_branch="$(git -C "${repository}" rev-parse --abbrev-ref HEAD)" \
  || fail "the deploy checkout branch is unreadable"
[[ "${current_branch}" == "${deploy_branch}" ]] \
  || fail "the deploy checkout is on ${current_branch@Q}, not ${deploy_branch}"
worktree_status="$(git -C "${repository}" status --porcelain -uall)" \
  || fail "the deploy checkout status is unreadable"
[[ -z "${worktree_status}" ]] \
  || fail "the deploy checkout is not clean; refusing to touch operator work"
git_admin_directory="$(git -C "${repository}" rev-parse --absolute-git-dir)" \
  || fail "the deploy checkout Git admin directory is unreadable"
deployed_commit_marker="${git_admin_directory}/serve-live.deployed"

if read_served_health; then
  previous_commit="${served_commit}"
elif read_deployed_commit_marker; then
  log "live serve health is unavailable; using the last deployed commit marker"
else
  fail "live serve health and the last deployed commit marker provide no rollback target; refusing the update"
fi

if [[ -n "${requested_target_commit}" ]]; then
  log "fetching ${deploy_remote}/${deploy_branch}"
  git -C "${repository}" fetch --quiet "${deploy_remote}" "${deploy_branch}" \
    || fail "could not fetch ${deploy_remote}/${deploy_branch}"
  git -C "${repository}" merge-base --is-ancestor "${requested_target_commit}" FETCH_HEAD \
    || fail "${requested_target_commit} is not reachable from ${deploy_remote}/${deploy_branch}"
  log "fast-forwarding main to ${requested_target_commit}"
  git -C "${repository}" merge --ff-only --quiet "${requested_target_commit}" \
    || fail "main could not fast-forward to ${requested_target_commit}"
else
  log "fast-forwarding main"
  git -C "${repository}" pull --ff-only --quiet "${deploy_remote}" "${deploy_branch}" \
    || fail "main could not fast-forward from checkout HEAD"
fi
target_commit="$(git -C "${repository}" rev-parse HEAD)" \
  || fail "the fast-forwarded deploy commit is unreadable"
# A `merge --ff-only` to a commit the checkout already contains is a silent
# no-op (HEAD does not move), so a checkout already at or ahead of a
# requested older commit would otherwise fall through and serve whatever
# newer commit HEAD already sits at -- diverging from the commit the caller
# (auto_redeploy.sh's green-ancestor walk) verified and journaled.
[[ -z "${requested_target_commit}" || "${target_commit}" == "${requested_target_commit}" ]] \
  || fail "the checkout is ahead of ${requested_target_commit}; refusing to deploy ${target_commit}"

prepare_root_package || fail "the root package.json safety check refused the update"
build_checkout || {
  # The live serve is still untouched at this point (systemctl stop runs
  # only below), so resetting the checkout back is a plain, safe git
  # operation -- it leaves no failed target fast-forwarded into main for the
  # next tick to trip over.
  git -C "${repository}" reset --hard "${previous_commit}" \
    || fail "the new checkout could not be built, and resetting it back to ${previous_commit} also failed; the live serve was not stopped, but the checkout is left inconsistent"
  fail "the new checkout could not be built; reset the checkout back to ${previous_commit}; the live serve was not stopped"
}

log "stopping atelier2-serve.service"
systemctl --user stop "${serve_unit}" \
  || fail "atelier2-serve.service could not be stopped"

log "backing up the live store"
if ! backup_live_store; then
  if rollback_previous_serve; then
    fail "live store backup failed; restored the previous commit and restarted the live serve"
  fi
  fail "live serve is DOWN, operator action needed"
fi

log "migrating the live store"
if ! (cd "${repository}" && uv run --locked atelier2 migrate --database "${database}"); then
  if rollback_previous_serve; then
    fail "migration failed; restored the previous commit and restarted the live serve"
  fi
  fail "live serve is DOWN, operator action needed"
fi

if ! connect_and_intake_definitions; then
  if rollback_previous_serve; then
    fail "connecting the workflow definition source failed; restored the previous commit and restarted the live serve"
  fi
  fail "live serve is DOWN, operator action needed"
fi

log "starting atelier2-serve.service"
systemctl --user start "${serve_unit}" \
  || fail "live serve is DOWN, operator action needed"

log "checking live serve health"
wait_for_served_health \
  || fail "live serve health is unavailable after start; the update is unverified"
[[ "${served_status}" == "serving" ]] \
  || fail "live serve health reports ${served_status@Q}, not serving"
[[ "${served_commit}" == "${target_commit}" ]] \
  || fail "live serve commit ${served_commit@Q} does not match the deployed commit ${target_commit}"
printf '%s\n' "${target_commit}" >"${deployed_commit_marker}" \
  || fail "the deployed commit marker could not be written"

log "now serves ${target_commit}"
if ((intake_refused)); then
  exit "${intake_refused_exit_code}"
fi
