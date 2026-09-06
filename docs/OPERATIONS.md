# Operations

Audience: the human operator deciding how this installation is started,
stopped, redeployed, how an executor toolchain is pinned, or how an older
store is raised to the current schema.

This file owns that runbook. It does not own product intent, requirement
sentences, or trust-boundary decisions. [PRODUCT.md](PRODUCT.md) says what
exists; [ADR 0009](decisions/0009-runner-trust.md) owns network hardening and
reachability; [ADR 0001](decisions/0001-durable-runtime.md) owns the schema
versions and fingerprints. This file only says how the packaged process is
started, how a predecessor store is raised offline, and how a deployment pins
an executor toolchain.

No operations owner existed. [docs/README.md](README.md) now names this file
for that question. [Journeys](journeys/) illustrate requirements and bind
nothing.

## Disposable Serve candidate

The repository `Dockerfile` bakes the locked project and production cockpit
into one provider-free Serve image. It has no provider executable,
credential/configuration mount, host home, scratch mount, Docker socket,
system service access, added capability, privileged mode, or Runner service.
It runs non-root with a read-only root filesystem, dropped capabilities and
`no-new-privileges`. Its only writable durable state is one Compose-named
`store` volume.

Start it from a clean checkout at a committed tree:

```bash
bash scripts/container_up.sh
```

The script refuses dirty, untracked, or unreadable source before Docker, then
archives the resolved commit into a temporary build context. It binds that
commit and tree to image and Compose resource labels, creates a fresh Compose
project, waits for `/atelier/api/v1/health` to report that same identity, and
only then prints the Docker-assigned loopback URL. A private candidate-lifecycle
descriptor freezes its teardown shape, so the shell-quoted
`down --volumes --rmi local --remove-orphans` command carries the exact identity
without ambient variables or a mutable checkout. It removes only this
candidate's container, network, volume and local project image; errors and
interrupts attempt that same cleanup and preserve the descriptor for retry when
it fails. Successful teardown removes it. Other containers and services are not
selected or changed. A rerun creates a new disposable candidate. This package
supplies no external provider or Runner.

## Runner (deleted)

A container-hosted Runner carrier -- the disposable #301-A candidate harness,
`atelier2-runner-launcher`, and "Serve as the lease writer" -- was deleted on
05.09.2026 (issue #1252) for having no live caller (0 of 485 live attempts ever
ran over it). It lives on in Git history for whoever names a caller next.

## Stable local Serve installation

From a clean committed checkout, install the one stable provider-free console:

```bash
bash scripts/container_live.sh install
```

The command refuses an active or enabled host Atelier service, a listener on
port 8422, another Docker resource labelled as the stable deployment, ambient
Compose mode values, or an existing accepted installation. It archives the
committed source through the same snapshot owner as the disposable candidate,
records durable `INSTALLING` intent before Docker can mutate anything, and
publishes the completed exact container, image, volume, network, configuration,
engine, source and frozen-descriptor identity only after health succeeds. The
private record lives under
`${XDG_STATE_HOME:-$HOME/.local/state}/atelier2/container-live`.

The console then owns `http://127.0.0.1:8422/atelier/`, uses
`restart: unless-stopped`, and preserves its Compose volume. Operate only its
recorded identity:

```bash
bash scripts/container_live.sh status
bash scripts/container_live.sh stop
bash scripts/container_live.sh start
bash scripts/container_live.sh uninstall
bash scripts/container_live.sh update
bash scripts/container_live.sh reconcile
```

`status` is read-only and prints exactly `RUNNING`, `STOPPED`, `INCOMPLETE`, or
`DRIFTED`. `stop` and `start` first validate the complete record and then address
only its exact container ID; they never rebuild, recreate, search for a
replacement, or adopt a listener or Docker resource. A failed start stops that
same container and leaves the volume intact. A failed install removes only its
intent-owned project when exact identity can be proved; otherwise it leaves the
incomplete record and fails loudly.

`uninstall` tears the installation down completely — container, network,
volume, image, and the record directory itself — reading only its own
record and compose truth, never an operator-supplied variable. It is
idempotent: nothing installed is a clean success. When the record is exact it
tears down through `docker compose down --volumes --rmi local
--remove-orphans`; when the record is missing, corrupt, or its exactness
cannot be proved (the "record gone, Docker residue remains" case), it instead
sweeps every Docker resource carrying the stable deployment's label — the
same identity `install`'s own collision guard checks for the container,
volume, and network — plus any image under the stable project's name prefix,
which never blocks a new install (each install tags a fresh image under a
new random project name) but would otherwise linger as disk residue. A
foreign Docker object under a different label or name prefix is never
touched by either path. Either path leaves zero matching Docker resources
behind, so a following `install` always succeeds — `another local-live
Docker owner exists` cannot recur.

`update` refuses ambient Compose mode first, before touching anything, then
keeps the installed Compose volume and network and raises the store through
the offline migration ladder (`atelier2 migrate`, #244) in place, before the
new container starts. It refuses an installation whose identity has drifted
rather than guess at it. The previous container is stopped first to give the
ladder exclusive access to the store files; the ladder's own contract is the
backup — each step is one transaction that either commits completely or
leaves the file exactly as it was, so there is nothing to separately copy.

If the ladder refuses the store (an unknown or newer schema, or a locked
file), or the previous container fails to stop, nothing has happened yet: the
previous container is restarted untouched and `update` fails with the
refusal. `compose up` itself is not part of that safe window — the new
commit always changes the running container's labels, so Compose always
recreates it, deleting the previous container as an intrinsic part of that
one call, before startup can even be confirmed healthy. A failure at or
after that point therefore finds the previous container already gone:
`update` reports the true state instead — the store is migrated, the new
container's health is unconfirmed — and names `status`, then `reconcile`, as
the store-preserving recovery path. The durable record is untouched either
way until the very end. On full success the new container starts on the
migrated store and `update` reports the ladder's fingerprint proof alongside
the cockpit URL.

`reconcile` is that recovery: an interruption in `update`'s unprotected
window leaves a healthy new container running beside a record that still
names the deleted previous one, so `status` reports `DRIFTED` and every
exact operation refuses — and before `reconcile` existed the only exits,
`uninstall` and `update --fresh`, both discarded the store. `reconcile`
rebuilds the durable record from the one container of the recorded Compose
project, and publishes it only after the full exact verification proves that
container serves the recorded store volume at its frozen origin commit, on
the recorded engine and network, with the complete hardening, running and
healthy. It runs no Docker mutation and never touches the volume. Anything
it cannot prove — no or several project containers, another engine, foreign
labels, an unhealthy container — is a named refusal that changes nothing;
`uninstall` and `update --fresh` remain the store-discarding last resort.
After a successful `reconcile`, `update` proceeds store-preserving again.

`update --fresh` is the previous behavior: `uninstall` followed by `install`
in one step, discarding the Compose volume and starting empty. It states
that plainly in its own output, and only when a volume actually existed to
lose — a sweep that only ever found a stray container or network never
claims a store was lost.

This container installation has no automatic deploy path; `update` above is
always a hand command. The automatic auto-redeploy watcher below applies only
to the loopback host Serve installation.

### Loopback host Serve hand update

This section describes the loopback host installation, not the container
installation above. The host-process installation runs as the systemd user unit
`atelier2-serve.service`. From its clean `main` deploy checkout, one hand
command fast-forwards, installs the locked Python and frontend dependencies,
builds the frontend, stops the unit, backs up the live store, migrates it,
takes the checkout's own workflows into the live catalog, starts the unit, and
verifies that health serves the new commit:

```bash
bash scripts/serve_live_update.sh
```

The store lives under
`${XDG_DATA_HOME:-$HOME/.local/share}/atelier2/live-store`. Every update copies
`atelier.sqlite` and `external.sqlite`, plus the SQLite `-wal` and `-shm`
sidecars when present, into a timestamped `backups/pre-redeploy-*` directory
and verifies every copied size before migration. A migration refusal restores
and rebuilds the commit reported by the running Serve, restarts the Serve, and
still exits nonzero. With the Serve unreachable and no recorded deploy, the
command refuses and changes nothing. A `live serve is DOWN, operator action
needed` line means that recovery itself failed; inspect `journalctl --user -u
atelier2-serve.service -e` before acting.

After migration and before the unit restarts -- the Serve is still stopped, so
nothing else writes the store at the same time -- the command connects the
deploy checkout itself as a definition source (`workflows/*.yaml`, ref
`refs/heads/main`; connecting the same checkout and ref again is the same
source, so this step is idempotent across runs) and takes its workflows in.
Each path gets its own word in the log: `published` for bytes the catalog
gained, `present` for bytes it already held, or `refused` for the one path
that stopped that intake. A name already held by a manually imported lineage
is adopted rather than refused -- the source revision becomes that lineage's
new head and the manual revisions stay its history -- while a name any source
has already delivered still refuses. Bytes an unsourced lineage already
serves as its current revision under the same name are recognised as present
and gain the same provenance; the lineage id and its history are untouched. A
refusal does not hold the Serve
back -- it starts with whatever workflow catalog state it already had -- but
the command exits
`3` rather than `0`, distinct from the generic failure exit `1`, so
auto-redeploy's watcher can tell the two apart. It logs a warning naming the
served commit and the refusal, does not count a failure tick, and keeps the
unit green: the deploy itself succeeded, so nothing here should raise the
operator's failure streak toward its alert threshold. The refused path keeps
serving its previous catalog state until the next successful deploy or a hand
`atelier2 definition-source intake` fixes it. The source itself failing to
connect (an unreadable checkout or an unresolved ref, not a per-file refusal)
is treated like a migration failure instead: it rolls back to the previously
served commit and restarts that, and does count as an ordinary failure tick.

Install the clean-stop classification once beside the unit, as the same user:

```bash
mkdir -p ~/.config/systemd/user/atelier2-serve.service.d
cp scripts/atelier2-serve.service.d/clean-stop.conf \
  ~/.config/systemd/user/atelier2-serve.service.d/
systemctl --user daemon-reload
```

The drop-in makes the launcher's SIGTERM exit code 143 a successful stop, so a
deliberate update stop does not leave the unit failed. Installation does not
start, restart, or enable the live service; those remain explicit host actions.

The unit sets no `TimeoutStopSec`, so a `stop` waits systemd's default 90
seconds before SIGKILLing the process; the serve process itself bounds an open
Workbench tab's event stream to `SERVE_SHUTDOWN_CONNECTION_GRACE_SECONDS`
(`src/atelier2/host/serving.py`, 10 seconds), comfortably under that default so
a stop always finishes clean and runs `runtime.close()` regardless of how many
tabs are open. An operator who ever sets `TimeoutStopSec` on the unit must keep
it above that grace. 10 seconds is also long enough to let the longest
legitimate in-flight request -- the project-source connect POST reaching out
to a remote such as GitHub -- finish; cutting it mid-flight is acceptable
because the redeploy that triggers this grace already checked for running
runs before it started, and a cut connect is simply retried by the operator.

### The Workbench's terminal seat

The Workbench shows a terminal, not a chat: one tmux session with the agent CLI
in it, reached through a ttyd child on loopback. Three flags declare it on
`serve`, beside the `--claude-executable` the seat runs:

```bash
--seat-tmux-executable /usr/bin/tmux \
--seat-ttyd-executable "$HOME/.local/bin/ttyd" \
--seat-port 7681
```

Both paths are named together or not at all, and a declared seat needs
`--project-id` and `--project-root`: a seat belongs to one project, opened
where that project lies. A named path that is not an executable file refuses
the start rather than sending the serve looking for one somewhere else.

The session is deliberately not the serve's child. `serve` creates it inside a
transient systemd user scope (`systemd-run --user --scope --collect`), so
stopping `atelier2-serve.service` — every redeploy does — takes the ttyd child
with the unit's control group but leaves the session and the agent running.
The next start finds that session again and draws a **fresh** address for it,
so a tab open across a redeploy reconnects on its next read; MCP calls made
from the seat fail visibly during the restart minute and work again after it.
Socket, session and scope carry one digest over the store path and the project
id, so two deployments on this machine (the live one and a test one) never
meet.

The seat is reached only over loopback, under a base path drawn fresh at every
serve start and told to nobody but the cockpit. There is no login: a pty is a
shell, so the seat has exactly the trust boundary the loopback serve has —
this machine, one user (#82 is open). The page says so beneath the terminal.
Nothing of the terminal is collected: no `pipe-pane`, no capture, no seat bytes
in the journal, in events or in receipts.

The MCP configuration the seat's agent is started with is written beside the
store, under `terminal-seat/mcp.json` in the store's own directory, never into
the operator's project tree. It names this deployment's own loopback door and
carries no credential.

Only this ends a seat — the serve never does, and neither does closing the tab:

```bash
uv run --locked python -m atelier2 seat stop \
  --database "$STORE/atelier.sqlite" \
  --project-id atelier-2 \
  --project-root "$CHECKOUT" \
  --claude-executable "$(command -v claude)" \
  --seat-tmux-executable /usr/bin/tmux \
  --seat-ttyd-executable "$HOME/.local/bin/ttyd"
```

It names the seat exactly as the serve named it, because the identity is drawn
from those values. It prints `terminal seat stopped`, or `no terminal seat was
running` when there was none. A forgotten stop leaves a tmux server with the
agent in it running under the operator's own session, which is what
`systemctl --user list-units 'atelier2-seat-*'` shows.

### Auto-redeploy watcher

**Auto-redeploy is the deploy path for the loopback host Serve above: a green
landing on `main` reaches it without an operator hand.** A systemd user timer
(`scripts/atelier2-auto-redeploy.timer`, a two-minute poll) runs
`scripts/auto_redeploy.sh`. The watcher serializes timer and hand runs in the
checkout's Git admin directory, fetches `origin/main`, and compares it with the
commit reported by live health. A matching commit is a no-op. Otherwise the
watcher requires a clean `main` checkout on its **tracked** paths -- an
untracked file or directory (the operator's own scratch files, a build
artefact) never blocks a deploy, because the deploy only ever fetches and
fast-forwards, which cannot touch anything git does not already track (#1186)
-- checks for running runs, then walks
`main`'s first-parent history back from the fetched commit (bounded to
`green_ancestor_search_depth` commits) for the newest commit with green
GitHub checks, so continuous merges landing faster than CI never starve live
delivery behind a HEAD that is always still checking; a commit older than
what is already served is never deployed. It checks for running runs again
immediately before the update, then hands the verified commit to
`scripts/serve_live_update.sh`. That script owns the fast-forward to the
verified commit and remains the one owner of build, backup, migration,
restart, and post-update health verification; the watcher never moves the
checkout itself. The watcher runs the **target commit's own**
`serve_live_update.sh` (materialised via `git show` into the checkout's Git
admin directory, then removed), never the copy already on disk, because Git
replaces a tracked file by unlink-and-create and a shell that already opened
the old file would otherwise keep reading it for the rest of the run. Staging
it under the Git admin directory, rather than the tracked checkout, means the
materialised file never shows up as an untracked file in `git status` and can
never itself cause the watcher's own clean-checkout preflight to refuse a
deploy.

Queued or running GitHub checks wait for another tick, unless an older commit
in the window is already green. No reported checks wait for up to 30 minutes
after the commit; after that they count as red. A run in
`STARTED` also waits without failing the unit; a run parked on a person --
`WAITING_INPUT`, `WAITING_RECONCILIATION` -- does not, because its answer is
taken under whichever `--application-version` the serve carries when the person
answers, so a redeploy cannot strand it
(`tests/integration/test_wait_survives_version_change.py`). An unreadable
health, run list, or check result fails closed. The watcher never deploys a
commit with a failed, cancelled, or timed-out check; completed neutral and
skipped checks are accepted as non-red.

Enable it once per host, from the deploy checkout, no root:

```bash
mkdir -p ~/.config/systemd/user
sed "s|/absolute/path/to/atelier-2|${PWD}|" \
  scripts/atelier2-auto-redeploy.service \
  > ~/.config/systemd/user/atelier2-auto-redeploy.service
cp scripts/atelier2-auto-redeploy.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now atelier2-auto-redeploy.timer
```

The Git admin directory holds `auto-redeploy.failures`, `auto-redeploy.busy`,
`auto-redeploy.last-alert`, and `auto-redeploy.last-busy-alert`. A successful
deploy or genuine no-op clears both streaks. A dirty tracked checkout or a
non-`main` checkout warns and increments the failure streak but leaves the
tree untouched. The third consecutive failure, and the first repeat at least
one hour later while the streak persists, logs at error priority and fails
that oneshot tick; other failure ticks exit successfully. Every deferred tick
names the runs it waits for -- public reference, state, and start time -- and
the tenth in a row, then at most hourly while the streak persists, repeats
that at warning priority; a busy tick never fails the unit. Inspect the
tagged journal and unit state with `journalctl --user -t atelier2-autodeploy
-e` and `systemctl --user status atelier2-auto-redeploy.service`.

A standing failure streak is also visible without the journal: on every tick
that resolves to a success or a failure, the watcher writes its own outcome
(`failure_count`, the last failure's reason and instant, the last success's
commit and instant) to `redeploy-status.json` beside the live database
(`${XDG_DATA_HOME:-$HOME/.local/share}/atelier2/live-store`); once
`failure_count` reaches three, `GET /health` names it as
`redeploy: {blocked_since, reason}`, and a status file that exists but does
not parse is named as unreadable rather than read as "no problem" (#1186).

### Live provider canaries

The billed loopback host process is installed as the systemd user unit
`atelier2-serve.service`. Its launcher owns the `atelier2 serve` executor
flags; the canary does not copy them. The installed canary unit does share the
launcher's effective runtime truth: installation substitutes the same deploy
checkout as its working directory, and `%h/.local/bin/uv run --locked` is the
same pinned interpreter and lock path that `serve-live.sh` uses. It asks that
running instance for its currently startable agent configurations, runs the
matching headless, workspace-tools, or atelier-doors workflow once with each
exact configuration hash, and refuses an admitted workflow whose hash differs
from the deploy checkout. Because the post-Serve-start drop-in fires this run
at process start rather than once it can answer, the run first polls `/health`
for up to 60 seconds until it answers `serving` -- failing loud with the last
health answer, and trying no vector, if it never does -- before any other
discovery step begins. A configuration is a vector when the listing's own
`startable` field says so, or when its only problem is a missing live
receipt (`not_startable_reason: provider-probe-receipt-missing`) -- both the
same judgment a start reads, computed once by the deployment's atomic
snapshot; discovery derives nothing of its own from either field. A
superseded revision (the model registry no longer points to it) carries its
own distinct reason and is excluded; a redeploy that invalidates every
registered configuration's receipt still leaves it reprobable. Discovery is
capped at four configuration pages, 50 known startable vectors, and 300
seconds. All distinct admitted workflow names resolve before any vector
starts. Every discovered vector then probes concurrently, up to 8 vectors'
own live billed runs in flight at once
(`PROVIDER_CANARY_MAXIMUM_CONCURRENT_VECTORS`), so one vector's own terminal
deadline never delays or blocks a sibling's receipt beyond that shared
worker cap -- each vector's outcome replaces its own receipt the instant it
is known, not in the order discovery listed it. Each vector still
has its own 300-second terminal deadline, while the complete process has a
15,300-second deadline enforced by both the runner and its systemd unit. Every
HTTP call has a 30-second cap reduced to the remaining discovery, vector, and
process deadline. The durable run owns provider output. The canary atomically
replaces only the secret-free
`provider-probe-receipt/v1` at
`${XDG_STATE_HOME:-$HOME/.local/state}/atelier2/provider-probes/live/<vector-id>.json`.

A receipt's validity key is a content digest of the provider layer
(`provider_layer_digest`: every provider adapter module, `host/provider_canary.py`,
and `contracts/provider_probe_receipts.py`), not the whole `source_commit`
(`source_commit` still travels on the receipt, but only as journal provenance,
#1124). A redeploy that leaves those files' bytes unchanged leaves every
receipt proven and every configuration immediately startable; only a
redeploy that actually touches the provider layer turns receipts over, and it
turns over all of them at once, since they share one digest. Narrower than the
full provider surface on purpose: the pinned CLI executable path
(`serve-live.sh`'s `--claude-executable`), the executor start-binding wiring
(`application/resolve_start_bindings.py` and the composition it feeds), and the
probe workflow bytes themselves (`workflows/provider-canary-*.yaml`) stay out
of the digest -- none of the three has a settings-independent path both the
canary and the Serve process can read and hash identically today. The
26-hour receipt validity is the backstop for that residual: a redeploy the
digest cannot see still turns every receipt over within one day. Each run's
own journal line names which of three outcomes happened:
`receipts kept (provider layer unchanged)`,
`receipts invalidated (provider layer changed: <digest8> → <digest8>)`, or
`no readable prior receipt (this run's provider layer: <digest8>)` when no
earlier receipt this runtime can read exists yet -- printed the moment
discovery finishes and the digest is known, before any vector starts, and
visible through `journalctl --user -u atelier2-provider-canary.service -e`.
Receipts remain valid for 26 hours, so the nightly schedule has two hours of
overlap. A receipt always says what the youngest probe attempt found: after a
vector enters its own execution, a failed attempt replaces that vector's
still-valid success before the next readiness read. Health, configuration
pagination, an empty list, or global workflow-name resolution belong to
discovery instead: their failure leaves every vector receipt byte-identical and
makes the oneshot fail loudly through its exit status and journal. A locally
unreadable workflow, hash mismatch, start refusal, timeout, or terminal failure
after vector entry replaces only that vector's receipt.

`POST /runs` uses the public `StartRunRequestResourceV2` form from the shared
run-command owner: workflow revision, one exact agent binding, and no orders.
That public start has no separate `idempotency_key`; `run_id` is its durable
idempotency identity. The timestamped id makes every timer or deploy trigger a
new run, including another trigger on the same day. The runner does not persist
the planned id before POST. A process crash after an accepted POST but before
the receipt therefore leaves a named duplicate-billing gap: the next trigger
uses a new id. Closing it requires persisting the planned `run_id` as the retry
key before POST and replaying that id until its outcome is receipted.

Install the oneshot, its nightly persistent timer, and the post-Serve-start
drop-in from the deploy checkout, as the same user that owns
`atelier2-serve.service`:

```bash
mkdir -p ~/.config/systemd/user/atelier2-serve.service.d
sed "s|/absolute/path/to/atelier-2|${PWD}|" \
  scripts/atelier2-provider-canary.service \
  > ~/.config/systemd/user/atelier2-provider-canary.service
cp scripts/atelier2-provider-canary.timer ~/.config/systemd/user/
cp scripts/atelier2-serve.service.d/provider-canary.conf \
  ~/.config/systemd/user/atelier2-serve.service.d/
systemctl --user daemon-reload
systemctl --user enable --now atelier2-provider-canary.timer
```

The timer's `Persistent=true` catches a missed 03:00 local run after the user
manager returns. The drop-in takes effect on the next Serve start. Its
`--no-block` keeps Serve from waiting for a billed run, and its `-` prefix keeps
Serve healthy when the optional canary unit is missing or refuses activation.
Start one probe without restarting Serve with:

```bash
systemctl --user start atelier2-provider-canary.service
journalctl --user -u atelier2-provider-canary.service -e
```

The canary workflows are admitted only after their budget revision exists on
the live instance. This is a landing operation, in this order:

1. Publish `workflows/budgets/provider-canary.json` with
   `POST /atelier/api/v1/budget-revisions` and retain the returned
   `budget_revision_hash`.
2. Replace the TODO head in each `workflows/provider-canary-*.yaml` with a node
   budget reference named `provider-canary` and that exact returned hash. The
   budget file's local SHA-256 is not a publication receipt.
3. Publish each resulting YAML document through
   `POST /atelier/api/v1/workflow-revisions`. Admit each returned workflow hash
   through `POST /atelier/api/v1/catalog-lineages` with
   `{"kind": "workflow", "catalog_revision_hash": "<hash>", …}`; when its
   authored name already owns a lineage, resolve that name through
   `GET /atelier/api/v1/catalog-revisions/by-name/workflow/<name>` and append
   through `POST /atelier/api/v1/catalog-lineages/<lineage-id>/members`
   instead.
4. Only after all three admissions answer with their exact workflow hashes,
   activate the deployed revision and start the canary oneshot. A partial
   publication is not activation authority.

Every publication and admission above targets the same loopback base URL the
installed `atelier2-serve.service` serves (normally
`http://127.0.0.1:8422`). The landing records the four returned revision hashes
in its own evidence; this runbook does not copy live hashes that change with
the published documents.

This slice deliberately has no copy, preview, activation, rollback, or
acceptance command. The stable console exposes current Core/V1 provider-free
behavior only; it adds no provider or Runner. Use the disposable candidate
above for zero-residue release proof.

Upgrading an installation made before this migration-preserving `update`
existed needs one `uninstall` first: the installation record gained the
volume and network's origin commit/tree as durable fields, and an
older-format record on disk cannot satisfy the new record's shape. `status`
on such a record reports `DRIFTED`; `uninstall`'s label-based sweep still
finds and removes it without needing to read it, so a following `install`
starts clean.

**Connecting a served project to live GitHub is `atelier2 connect`, once,
offline — it replaces the removed `--github-*` serve flags.** The connect act
records the source kind (`github`), the opaque source address
(`owner/name`), its nonidentity operating ref (`--source-ref base-branch`), a
credential-directory reference holding a `token` file, the auth method
(`personal-access-token`) and the connecting actor;
serve then composes the live `open-pr` adapter from that record whenever it
serves the connected project, with no GitHub flag on the serve line. A serve
started with the old flags is refused by argparse as unrecognized arguments.
For GitHub, the CLI requires the separate ref and refuses the legacy
`owner/name@branch` address; V44 migration relocates that embedded branch into
the row's private source-ref detail before V45 readers accept it.
The live composition still requires a loopback bind. An agent-authored
`open-pr` grant now uses the same durable reconciliation path as an Action:
an unknown GitHub readback pauses at the agent node for an operator decision,
rather than refusing admission or reporting completion before a receipt exists.

`connect` refuses when a project already has an active connection at a
different address of the same source kind, to catch a typo. When the source
genuinely moved — the connected repository was renamed or transferred —
`atelier2 connect --move ...` publishes two revisions in the same command:
the old address continues as `DISCONNECTED`, and the given address is
published `CONNECTED`; the command prints both revision numbers, and nothing
is deleted. The running serve read the connection once at startup, so it
needs one restart to pick up the move; the auto-redeploy performs that
restart on its next deploy, and there is no reason to force one sooner. That
restart blocks on an effect intent under the old address only while the DBOS
workflow that owns it is still open: an open workflow must finish or be
reconciled first, but once it has ended, history under the old address never
blocks the restart, whatever that intent's own recorded state.

### Publish the issue-to-pr catalog

`serve_live_update.sh`'s Git-source intake admits only `workflows/*.yaml`; a
schema, budget, grant, or adapter operation a shipped workflow pins is never
picked up by that intake and must be published by hand before the workflow
that pins it can start. For `workflows/issue-to-pr.yaml`, this is a landing
operation, in this order:

1. Publish its three schemas --
   `workflows/schemas/issue_to_pr_candidate_report.json`,
   `workflows/schemas/code_review_result.json`, and
   `workflows/schemas/issue_to_pr_release_decision.json` -- through
   `POST /atelier/api/v1/schema-revisions`, one call per document.
2. Publish `workflows/budgets/push-implement.json` through
   `POST /atelier/api/v1/budget-revisions` if the live catalog does not
   already carry it (`push-before-open-pr` publishes the same budget).
3. Publish the two adapter operations through
   `POST /atelier/api/v1/adapter-operation-revisions`: `open-pr` is exactly
   the bytes `{"operation":"open-pr"}`; `push-atelier-commit` carries this
   deployment's own author and committer identity, so it has no canonical
   bytes here.
4. Publish the two tool grants through
   `POST /atelier/api/v1/tool-grant-revisions`, after step 3: the
   `run-project-verification` grant is exactly the bytes
   `{"capability":"run-project-verification"}`; the `push-atelier-commit`
   grant names step 3's operation by its own returned hash, so it cannot be
   published first.

The Git-source intake admits `workflows/issue-to-pr.yaml` regardless of this
order; it does not refuse the document for an unresolved pin. The admitted
revision reads `executable: false`, with `not_executable_reason` naming the
first pin still missing, until every schema, budget, grant, and operation
above is published -- publishing the missing ones then turns the same
revision `executable: true` in place, with no new intake. The order above
still matters: the `push-atelier-commit` grant names step 3's operation by
its own returned hash, so it cannot be published first. The live hashes are
the landing's own evidence; this runbook does not copy them, for the same
reason the canary's four hashes above are not copied either.

### Publish a queue policy with its cap and its automation label

One CAS-guarded call names both of a project's queue rules at once:

```bash
curl -fsS -X PUT \
  http://127.0.0.1:8422/atelier/api/v1/projects/<public-project-reference>/queue-policy \
  -H 'Content-Type: application/json' \
  -d '{"revision_number": <previous + 1>, "expected_revision": <previous>,
       "maximum_active_runs": 2, "automation_label": "bereit"}'
```

`expected_revision` is the revision number currently in force (`0` for a
project that has never published one) and `revision_number` is that plus one;
a mismatch is refused as `queue-policy-revision-conflict` rather than
overwriting a revision someone else published. Revisions are append-only, so
changing either rule means publishing the next revision, never editing this
one.

`maximum_active_runs` caps how many runs of this project may be active at
once; the rest wait in priority order. `automation_label` names the one label
that admits an item automatically: at the next sweep, every inspected proposal
whose tracker item carries that label is admitted under the `AUTOMATION_RULE`
authority and starts within the cap. The label is read from the tracker at
that moment, so removing it in the tracker before the sweep withholds the
admission; a human sets it there, and the atelier never writes it. Spell it
exactly as the tracker spells it, capitalisation included: `Bereit` and
`bereit` are two different labels here, and a policy naming one admits nothing
carrying the other. Omitting the field (or sending `null`) turns automatic
admission off, and `"*"` is refused: the policy names one label, and "admit
everything" is not a ruled value.

Two things this does not do. It admits nothing that has no inspected proposal
yet -- the label says "go", never which workflow or priority to go with, so
plan the item through `PUT /queue-proposals` first -- and it never overrides a
proposal marked `HUMAN_REQUIRED`.

The sweep runs at Serve start, then every five minutes on the runtime's own
tick, and again the moment `POST /queue-admissions` records an admission -- so
a policy published against a running Serve, or a label set at the tracker,
takes effect within five minutes, and an item admitted through the door starts
without waiting for that tick. Nothing here needs a restart, and nothing here
is an operator's handgrip: the cap and the priority still decide what actually
starts.

One thing the tick does not do: an item whose run ended stays bound to that
run. A failed or cancelled run is not tried again, and nothing in this build
frees the item -- it stays bound to the ended run.

### A red project verification's own output (#1137)

When the redeemed `run-project-verification` grant exits nonzero, the
attempt's node receipt no longer names only an exit code. It names the exact
command, how long it ran, pytest's own short summary line where the retained
tail carries one, and -- when the check printed anything at all -- the
address of an artifact holding the last 64 KiB of its combined stdout and
stderr, e.g. `project-verification-failed: exit 1; uv run --locked pytest …;
after 806 s; 3 failed, 5961 passed in 45.23s; output artifact
sha256:<hash>`. That artifact is the same content-addressed material `POST
/artifacts` publishes and `GET /artifacts/{hash}` reads back (#1089); no
second store and no new wire concept carry it, and any credential shape
`redact_credentials` recognises is replaced before the tail is kept. A
verification that exits zero keeps no artifact -- the outcome's own hash and
summary are proof enough for a check that passed.

### An attempt that changed nothing, and the patch a red check rejected (#1156)

Before a grant is redeemed, the tree standing in the attempt's leased directory
is written into the project's candidate store and compared with the pinned tree.
Only an attempt about to redeem a grant is asked: a node that pinned none may
honestly answer without touching a file, and a reviewer judging a candidate is
exactly that. Equal means the attempt changed nothing: it ends in seconds, `FAILED` under
`CANDIDATE_UNCHANGED`, with no verification started and no grant redeemed, and
the node receipt reads `candidate-unchanged: the workspace still holds the
pinned tree <tree>, so this attempt changed nothing; the agent answered: ...`.
That answer is bounded and credential-redacted, and it is the point of the line:
three live `issue-to-pr` runs each paid ten minutes of project tests and ended
`PROJECT_VERIFICATION_FAILED` on what was almost certainly the pinned tree, so
an answer claiming work that is not there is now stated instead of absorbed. No
candidate ref is written for this ending -- naming a tree is not keeping one.

Where the tree did change and the check then exited nonzero, the receipt names a
second artifact beside the output tail: the attempt's own patch against the
pinned tree, bounded to 64 KiB from its start and redacted the same way, as
`candidate diff artifact sha256:<hash>`. That receipt also carries the schema
revision and the value hash of the answer the provider gave, so what the builder
said is readable through `GET /artifacts/{hash}` as well. The rejected work is
still not kept as a candidate: what survives is evidence, not something a later
run could take.

## Pin an executor toolchain

The atelier owns the executor copies it serves. The operator's daily CLI
(`~/.local/bin/claude`, `~/.local/bin/grok`, `~/.local/bin/codex`) may update
freely and is not the pin. Point `--claude-executable`, `--grok-executable`,
or `--codex-executable` at an atelier toolchain, not at those host binaries.

Install one already-conformant release into
`${XDG_DATA_HOME:-$HOME/.local/share}/atelier2-toolchains`:

```bash
uv run --locked python scripts/install_executor_toolchain.py --provider claude --version 2.1.233
uv run --locked python scripts/install_executor_toolchain.py --provider codex
uv run --locked python scripts/install_executor_toolchain.py --provider grok --from /path/to/the-conformant-grok
```

The script prints the absolute executable path. It imports
`CONFORMANT_CLAUDE_VERSIONS`, `CONFORMANT_GROK_VERSIONS`, and
`CONFORMANT_CODEX_VERSIONS` from the subscription adapters and does not keep a
second list. Claude's set has more than one member, so the fenced command
includes `--version` with one of them. After the tree lands, the script asks
the binary `--version` and refuses an answer that is not that selected member.

Claude and Codex are fetched with `npm install` into an isolated prefix
(`node_modules/.bin/claude` or `codex`). Grok is a standalone binary, not an
npm package: pass `--from` to a conformant executable and the script copies it
to `grok-<version>/grok`. `--from` copies an already-held executable into that
layout instead of fetching.

This script does not alter a running Serve, download during `serve`, or resolve
the executable path from admission. Those remain later slices of the toolchain
item.

The pinned grok applies a declared output schema to *every* assistant message
and ends the session at the first message that carries no tool call. A Grok node
bound to `headless_with_tools` has to narrate and act before it answers, so that
vector hands the CLI no schema at all: the declared shape closes its job in
words, and the answer is judged against that schema at the output seam, where an
answer that is no such document fails the attempt as `OUTPUT_SCHEMA_REFUSED`.

Such a node can still end without a candidate for a reason that is neither a
crash nor a failed check: a session that answered without opening a single door
is refused as a provider failure rather than published as a report about work
nobody did. The node ends FAILED, and only an explicit operator replacement runs
it again. In the receipt it appears as a failed attempt whose transcript ends on
one assistant turn and no tool call. Seeing it repeatedly for one node is a
signal about that node's instruction, not about the deployment.

### Arm the Claude executor a builder needs

Pinning the executable serves one executor: `claude-subscription/v1`, a
tool-free call that can read no file. Two more are armed by name beside it,
and only one of them is a builder.

* `--claude-workspace-tools` arms `claude-subscription-tools/v1`. This is the
  only Claude executor whose invocation reaches the attempt's own workspace,
  so it is the only one a node that pins `run-project-verification` or
  `push-atelier-commit` can be cast onto.
* `--claude-atelier-doors` arms `claude-atelier-doors/v1`. Its tools are the
  atelier's own API doors and it removes every built-in with `--tools=`, so a
  node cast onto it can choose, start and watch catalog runs while touching no
  file of the project.

None of the three hands Anthropic the output schema its node declared. The API
refuses a schema whose root is an `allOf`, `anyOf` or `oneOf` as a tool's input
schema, and `code_review_result` -- what every reviewer of `issue-to-pr`
declares -- is exactly such a document, so a Claude review node died after
seconds with `api_error: API Error: 400` and no model was ever reached. The
declared schema now closes the job in words, and the answer is judged against
it at the output seam. A node whose answer carries no value that schema admits
fails there as `OUTPUT_SCHEMA_REFUSED` after its one repair round, which reads
in the receipt as a refused output rather than as a provider error.

Without `--claude-workspace-tools`, this deployment has no Claude builder for
`workflows/issue-to-pr.yaml`: its build node pins both grants above, and a
start that casts a Claude role onto either of the other two executors is
refused before the run exists
(`DurableAgentExecutorWithoutWorkspaceFileTools`, answered over the API as
`agent-executor-binding-unavailable`). Arming is an operations step of its own
-- add the flag to `serve-live.sh` and restart
`atelier2-serve.service` -- and, like every executor arming, it widens what a
billed run may do on this host. The flag is only half of it: the builder
model's registry row must also resolve to an agent configuration published on
`claude-subscription-tools/v1`, because a row still naming a tool-free Claude
is cast onto an executor that reaches no file, and the start then answers
`agent-executor-binding-unavailable` with the flag already in place.

## Connect a git definition source, see where it stands, and take it in

Three offline commands against a store that already exists. Only `intake`
publishes, and `serve` performs none of them at startup: a newer version of a
workflow enters the catalog because the operator asked for it.

```bash
atelier2 definition-source connect --database /path/to/atelier.sqlite \
    --location /srv/definitions.git --ref refs/heads/main \
    --select 'workflows/*.yaml=workflow' --actor felix
```

`connect` refuses without writing when the location is no repository or the
ref resolves nowhere -- there is no way to disconnect a source yet, so a wire
to nowhere is never registered. It answers for those two and no more: a
selection matching nothing today is an ordinary thing to configure, and every
selection problem surfaces at scan. It then records the repository, the ref,
and the selections,
and prints the source id every later command names. A selection is
`PATTERN=KIND`; the kind is configured, never guessed from the repository's
layout
([ADR 0018](decisions/0018-plugin-intake-and-neutral-roles.md)). The one
wildcard is `*`, matching inside a single path segment. Connecting the same
repository at the same ref again is the same source, not a second one.

```bash
atelier2 definition-source scan --database /path/to/atelier.sqlite \
    --source-id <id>
```

`scan` resolves the ref to one commit, reads every selected file of it, and
prints that commit followed by one line per path: `source_ahead` when the
catalog does not hold these bytes, `in_sync` when it does, and `source_absent`
for a path the catalog holds that the source stopped carrying. It writes
nothing at all, so a scan never changes what a run would use.

The location must *be* the repository -- a bare repository, a checkout, or a
linked worktree. A directory that merely lies inside one is refused, because
reading it would read a repository the operator never named. All three
commands refuse before writing anything, in one closed vocabulary:
`definition_source_unreachable`, `_ref_unresolved`, `_layout_unrecognized`,
`_selection_ambiguous`, `_path_escapes_repository`, `_no_selected_files`,
`_symlink_selected`, `_gitlink_selected`. A selected file the publication door
would refuse is reported in that door's own words, and stops the scan.

```bash
atelier2 definition-source intake --database /path/to/atelier.sqlite \
    --source-id <id> --actor felix
```

`intake` takes one commit of the source into the catalog. It reads the same
files `scan` reads, then publishes every one of them, admits it under the name
its document authored, and records where it came from -- source, commit and
path -- in one transaction. A refusal anywhere in the batch writes nothing at
all, so a failed intake leaves the catalog exactly as it was.

It prints the commit followed by one word per path: `published` when the bytes
entered the catalog, and `present` when the catalog already held them under the
name they author, which is why taking the same commit in twice writes no second
row. `refused` names the one path that stopped the whole batch: an authored
`name` outside the catalog's `[a-z][a-z0-9._-]*`, a name another lineage
already holds, bytes that already belong to another lineage -- including the
same bytes catalogued under a different name -- or a retired lineage. Pass
`--source-position <commit>` to take in exactly the commit a scan showed; a ref
that moved in between is refused rather than published unseen.

A path that is taken in a second time joins the lineage its earlier revision
belongs to, so an edited file becomes the next revision of the same catalog
entry and the revision before it stays exactly what it was. Continuity is the
repository path, never the authored name: renaming a file in the source starts
a new lineage.

Retire a lineage of any kind through
`POST /atelier/api/v1/catalog-lineages/<lineage-id>/retirements`. Repeating it
answers 204 again, and afterwards
`GET /atelier/api/v1/catalog-revisions/by-name/<kind>/<name>` answers
`catalog-lineage-retired` rather than a revision. Retirement takes the name out
of the live catalog and nothing else: every published revision stays readable by
its hash, and a run already under way keeps running.

Retiring an agent lineage does not yet stop a workflow start. A workflow
document references no agent definition today -- an agent reaches a run through
the role binding the start supplies, which names an agent configuration
revision and never a catalog name -- so there is nothing for a retired agent
name to refuse. Making such a start refuse by name needs that reference to
exist first; it is a named gap on #66, not a promise this door keeps.

Private repositories, project-scoped registration, disconnecting a source, and
the catalog's own Connect and Pull buttons are named absences, not oversights.

## Raise an older store

Runtime startup still refuses every predecessor (`MigrationRequired`) and
does not alter the file. The offline command is the tool that refusal names:

```bash
atelier2 migrate --database /path/to/atelier.sqlite
```

Stop the process that owns the file first. The command refuses a write lock
it can see; an idle reader is not always visible, so stopping the serve is
the operator's gate. It does not create a store, does not start a server, and
does not open a runtime.

The file is inspected, then raised one published step at a time. Each step
ends with the fingerprint [ADR 0001](decisions/0001-durable-runtime.md) names.
Any doubt rolls the transaction back, so a failed hop leaves the predecessor
unaltered. Today the built steps run from schema version 13 to the current one, each
either an additive table home or a rebuild that copies every predecessor row. Older published predecessors, and unknown or future
versions, are refused by name. A store already on the current schema is left
unaltered and said to be already current.

## Remove defective rows from the live store

A row the runtime can no longer read is removable without asking first; the
[prototype stage](PRODUCT.md) carries that ruling and its boundary. This is
the procedure, and it is a hand operation on the loopback host Serve's store
under `${XDG_DATA_HOME:-$HOME/.local/share}/atelier2/live-store`.

1. Stop the auto-redeploy timer, then the Serve, so nothing restarts the
   process into the middle of the write:

   ```bash
   systemctl --user stop atelier2-auto-redeploy.timer
   systemctl --user stop atelier2-serve.service
   ```

2. Copy `atelier.sqlite` and `external.sqlite`, plus their `-wal` and `-shm`
   sidecars where present, into a fresh timestamped directory beside the
   redeploy copies in the store's own `backups/`. This copy is the only way
   back; a deletion has no rollback of its own.

3. Delete in one transaction, children before parents. The immutability
   triggers refuse the delete by design, so the transaction drops the
   `*_no_delete` triggers that stand in the way, deletes, and recreates them
   with exactly the text `_PRODUCT_TRIGGERS` in
   `src/atelier2/adapters/dbos/schema.py` defines — that module is their
   owner, and a trigger recreated from memory or from an older copy leaves the
   store lying about what it protects. `PRAGMA foreign_key_list(<table>)`
   names each table's parents, so the delete order follows the walk from the
   leaves inward; a broken run's own row is the last to go.

4. Prove the store before starting anything: `PRAGMA integrity_check` and
   `PRAGMA foreign_key_check` must both come back clean, and the trigger set
   must be complete again.

5. Start the Serve and the timer again, then prove the repair from outside:
   `GET /atelier/api/v1/runs` answers the full list, and
   `journalctl --user -u atelier2-serve.service` stays quiet where the broken
   rows used to write a projection failure on every poll.

6. Say it in the report — what was removed, why it was unreadable, and where
   the backup stands. A removal nobody reads about is the silent kind the
   stage does not permit.

## What this slice does not do

- **Live cutover.** The candidate selects no existing process, port, container,
  network or volume; it is not a replacement action.
- **Runner or provider execution.** The image supplies neither; A.0 proves no
  external call.
- **CI image build.** CI checks the cheap recipe contract. The release/local
  gate, after a reviewed clean commit, builds, inspects, browses, restarts and
  tears down the candidate. Network hardening beyond its loopback publication
  stays with ADR 0009.

## Measure concurrent fake-executor load

This is a measurement of the current SQLite instance, not a capacity promise
and not a billed-provider run. CI keeps two concurrent runs so the suite stays
cheap. A larger local sweep reuses the same harness on one process:

```bash
ATELIER2_LOAD_CONCURRENCY=96 uv run --locked pytest --dist loadgroup -n 0 tests/integration/test_sqlite_load_measurement.py -s
```

`-n 0` keeps the instance on one worker. The report names the start door, the
event-write door, and one SSE reader per run, then the first observed pressure.
Raise `ATELIER2_LOAD_CONCURRENCY` until a named refusal appears; the 503 knee
was not reached at 96 on 2026-08-19 (`ed6376b`) and stays leftover.
Writer-lock, process spawn, watchdog cgroup, and memory are named only when
the harness observes them.

## Land a pull request

Every pull request against `main` carries exactly one typed classification
line in its body: `Work-Item: #n` together with a closing reference for that
same item (for example `Work-Item: #1267` plus `Closes #1267`), or `No-Item:
docs` / `No-Item: fix` for a lane that owns no issue. The required `Landing
classification` check runs `agent-claim pr-check --pr <n>` and refuses the
merge when the line is missing, malformed, or does not match the pull
request's active claim; the agent-claim README's "Landing classification"
section owns the full semantics (what counts as a valid line, parentage
through GitHub's sub-issue relation, and every refusal case).

**`gh pr merge --auto --merge` queues a pull request; it does not merge it on
the spot.** GitHub's merge queue builds a merge candidate from one or more
armed pull requests, runs `ci.yml` once against that candidate on the
`merge_group` event, and merges the pull request only once the ruleset's
required checks report green on that run; a red candidate leaves its pull
request out of the queue instead of blocking the ones behind it. This
replaces re-arming a pull request by hand after every trunk landing: the queue
absorbs a `main` move by rebuilding the candidate itself, instead of leaving a
`BEHIND` pull request to `cancel-in-progress` its own in-flight run.

The one-time ruleset step this depends on -- turning on "Require merge queue"
on the `main-protection` ruleset (merge method `merge`, group size cap 5,
admitting only non-failing pull requests) -- is an operator/head step done
once, through `gh api`, after this change lands; the queue has no effect on
pull requests opened before that step runs.

The Python pipeline separates fast feedback from the coverage-instrumented
suite: `Python: architecture, lint, types, tests` keeps the existing required
check name and proves the architecture, dead-code, documentation,
product-status, ANN401, Ruff, formatting, Pyright, and screenshot-review gates,
while the independent `Python: tests` job proves non-crash behavior and
publishes its JUnit and coverage reports. This lets static failures report
without waiting roughly 17 minutes for pytest; `Static and behavior` still
waits for both jobs, so every required-check name and its protection meaning
stays unchanged. The operator may later add `Python: tests` as its own required
context.

## Dead-code gates

Two gates keep code that nothing reaches out of the tree, and they do not yet
ask the same question of a test.

`uv run --locked python scripts/check_dead_code.py` runs vulture over
`src/atelier2` alone: a symbol only its own test reaches is not a symbol the
product uses, so it is dead. `npm run check:dead` (in `frontend`) runs knip
over the cockpit's `src`, where an unused file, export, or dependency is red --
but knip's vitest and playwright plugins register the test files as entry
points, so an export only a cockpit test imports counts as reached. Making the
cockpit gate ask what vulture asks turns roughly a dozen test-only exports red
and is its own slice, owned by #1168 (finding 12).

A vulture finding survives only by standing in one of three files, and which
file it stands in is the whole justification:

- `.vulture_allowlist.py` -- a production site *does* reach the name and vulture
  cannot see that site: a program built as text, a vocabulary the wire selects
  by value, a field read by a generated `__eq__` or by `asdict()`, a framework
  attribute. The entry names that site. If you cannot name one, the name does
  not belong here.
- `vulture_pending.py` -- the name waits for a decision an open item already
  owns. The entry carries an expiry and the gate turns red once it passes, so a
  parked decision stays slow rather than becoming permanent.
- `vulture_frozen.py` -- the name is built ahead of its caller and is kept
  (operator ruling 04.09.2026: freeze, do not throw away). No expiry; the entry
  names the open item that owns the caller. The gate lists these on every run
  without failing, and frozen means no hardening and no new tests -- the tests
  it already has keep running.

An entry naming something the gate no longer reports is red too: when a caller
arrives, or the code goes, its entry goes with it.

A third static gate in the same `quality` job, `ruff check --select ANN401`
scoped to `src/atelier2/contracts`, `ports`, `application`, and `api`, proves
only that none of those four packages accepts a parameter typed directly as
`Any` -- it says nothing about a nested `dict[str, Any]` or about return
types.

## The duplicate ratchet

`uv run --locked python scripts/check_architecture.py` also refuses copied
code. It reads every function of `src/atelier2` long enough to be recognised
again as five-token shingles, with its literals and its own names normalised,
so a copy someone renamed and reflowed still matches; a pair whose shingles
overlap by 95 per cent or more is the same code. `duplicate_baseline.toml`
names the pairs this tree already carries. A pair that is not listed turns the
gate red, and so does an entry whose pair is gone -- a list that only grows
stops describing anything. Resolving a listed pair therefore means giving the
two one owner *and* deleting its entry.

## The size and complexity ratchet

`uv run --locked python scripts/check_size_ratchet.py` holds three more debt
shapes in `src/atelier2` from growing: files at 800 lines or more, functions
and methods at 60 lines or more (measured with `ast`), and functions ruff's
`C901` McCabe check reports over a complexity of 15. `size_ratchet_baseline.toml`
names every path or qualified symbol this tree already carries at its current
value. An offender missing from the baseline, or one that grew past its listed
value, turns the gate red; a listed entry that no longer offends is an orphan
and is red too. Shrinking a listed offender, or leaving it exactly at its
baseline value, is quiet and rewrites nothing. This runs as a step of the
`quality` job.

## The core test import ratchet

`uv run --locked python scripts/check_core_test_imports.py` holds
`tests/domain`, `tests/application`, and `tests/api` to the count of test
modules each already imports `atelier2.adapters` from (AGENTS.md "Tests": a
core unit test imports no adapter). `core_test_import_baseline.toml` names
each directory's count; a directory that grew past its listed count turns the
gate red. A directory that shrank stays quiet -- lowering the recorded count
happens only by running the script with `--write-baseline`, never by hand-
editing the file. This runs as a step of the `quality` job.

## SonarCloud and CodeQL

`sonar-project.properties` at the repository root configures SonarCloud's
analysis of the public project `overnightworks_atelier-2` (organisation
`overnightworks`, Free plan): source, test, and exclusion layout, plus the
rule classes marked won't-fix by #1203's triage. CodeQL's default setup,
enabled directly on GitHub, scans Python, JavaScript/TypeScript, and Actions
on the same pushes. Neither is a required check yet -- that follows the
measurement week described in #1203, which compares Sonar's findings against
the duplicate ratchet above and the `C901` complexity count.

Automatic Analysis cannot read coverage, so analysis runs from CI instead
(ruling 05.09.2026, #1203): the `tests` job's pytest run writes
`reports/coverage.xml` (`pytest-cov`) and the `frontend` job's vitest run
writes `reports/frontend-coverage/lcov.info` (`@vitest/coverage-v8`); the
`sonar` job downloads both and runs `sonarqube-scan-action` with the
repository's `SONAR_TOKEN` secret. Its scan step is `continue-on-error` until
the operator turns Automatic Analysis off in the SonarCloud project settings
-- SonarCloud refuses CI-based analysis while Automatic Analysis stays
enabled. `sonar` is not a required check.

SonarCloud's own quality gate passes on ratings (A/B/C), not on a count of
findings: three green landings still carried 16 new open findings past it
(agent-claim #143, #1203's measurement). A single open finding on new code is
therefore its own bar (ruling 06.09.2026, #1294): after the scan step
(`sonar.qualitygate.wait=true` makes it block until analysis is final), the
"SonarCloud open findings" step pages
`api/issues/search?componentKeys=overnightworks_atelier-2&statuses=OPEN`,
scoped to the pull request or the pull request number parsed from the merge
queue's synthetic `head_ref` on a `merge_group` run, prints every hit as
`severity rule file:line message`, and fails the job at one or more. On a
push to main it instead prints the total as one informational line and always
exits 0: main already carries #1203's pre-existing backlog (318 open findings,
measured 06.09.2026), so PRs are gated on zero open findings while main's
backlog is #1203's to work down. No token: the project is public. Probed
06.09.2026 (#1294):
`python:S1192` (duplicated string literal) does not fire inside test files, so
it cannot serve as a red-path probe there; `python:S5778` (two exception-
raising calls inside one `pytest.raises` block) is silently excluded on
`tests/**` by this project's own `sonar.issue.ignore.multicriteria` (the rule
class above), so it never becomes an open finding either -- the probe run
with it came back green. `python:S9073` (a composite `assert a and b`) is not
excluded and does fire on test files; it is the reliable red-path probe.

## Code rules: gates, metrics, audit

The code rules in [`AGENTS.md`](../AGENTS.md) fall into three classes, and this
section only says which class a rule is in; `.github/workflows/ci.yml` stays the
live list of what actually runs.

Machine-checkable rules are gates there. Running today: the architecture check
(`scripts/check_architecture.py`, package boundaries), the duplicate ratchet
above, the size and complexity ratchet above, the dead-code gates above, the
frozen OpenAPI document check below, `ruff check --select ANN401` over
`contracts`, `ports`, `application` and `api` (#1196, landed #1197), and the
changed-narrative check over added Python comments and docstrings. The
core-test-import ratchet starts only once the first adapter-bound test module
has moved.

Rules about the shape of a change — slice size, context-file length, the
adapter-import share in core tests — stay reported metrics and never become
gates, because a check cannot judge a cut. `scripts/report_corridor.py` proves
the slice-size sentence this way: it runs as a step of the `quality` job,
prints the change's production file and line counts on every run, and only
over the corridor does it write the job summary and update its one pull
request comment (marker `<!-- corridor-report -->`) -- it always exits 0.
Everything else a machine cannot judge is ruled to run as a scheduled agent
audit on the self-hosted runner, producing one distributor issue per run
(operator ruling 04.09.2026); that workflow does not exist yet.

After a route change, regenerate the frozen OpenAPI document with `uv run
python scripts/write_openapi_frozen.py` before committing; its `--check` twin
runs as a step of the `quality` job.

## Verification

Container recipes:

`uv run --locked pytest --dist loadgroup -n auto tests/tooling/test_container_packaging.py`

Stable local lifecycle:

`uv run --locked pytest --dist loadgroup -n auto tests/tooling/test_container_live.py`

Those jobs exercise the recipes and lifecycle scripts with a fake `docker`.
They do not build a real image.

Auto-redeploy watcher (against a real local git repository pair and doubles
for `container_live.sh` and the health endpoint):

`uv run --locked pytest --dist loadgroup -n auto tests/tooling/test_auto_redeploy.py`

Store migration:

`uv run --locked pytest --dist loadgroup -n auto tests/integration/test_store_migration.py`

Pinned toolchain:

`uv run --locked pytest --dist loadgroup -n auto tests/tooling/test_install_executor_toolchain.py`

Fake-executor load (CI n=2):

`uv run --locked pytest --dist loadgroup -n auto tests/integration/test_sqlite_load_measurement.py`

Cockpit e2e harness (`npm --prefix frontend run e2e`, driving
`tests/e2e/serve_cockpit.py`): `frontend/playwright.config.ts` allocates a
free loopback port per run and passes it to the harness env as
`ATELIER2_E2E_PORT` and to the browser as `baseURL`, so two worktrees running
the suite at once never collide. Set `ATELIER2_E2E_PORT` explicitly to pin
the harness to one port instead.
