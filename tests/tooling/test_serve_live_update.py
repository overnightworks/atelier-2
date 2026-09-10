from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.tooling.test_auto_redeploy import write_stub

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SERVE_LIVE_UPDATE = PROJECT_ROOT / "scripts" / "serve_live_update.sh"
STORE_FILENAMES = (
    "atelier.sqlite",
    "atelier.sqlite-wal",
    "atelier.sqlite-shm",
    "external.sqlite",
)
DEFINITION_SOURCE_ID = "a" * 64
TOOL_STUB = r"""\
import json
import os
import sys
from pathlib import Path

DEFINITION_SOURCE_ID = "a" * 64

command = Path(sys.argv[0]).name
arguments = sys.argv[1:]
state_path = Path(os.environ["ATELIER2_TEST_TOOL_STATE"])
log_path = Path(os.environ["ATELIER2_TEST_TOOL_LOG"])
state = json.loads(state_path.read_text(encoding="utf-8"))

with log_path.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps([command, *arguments]) + "\n")


def save() -> None:
    state_path.write_text(json.dumps(state), encoding="utf-8")


if command == "git":
    git_arguments = arguments[2:] if arguments[:1] == ["-C"] else arguments
    if git_arguments == ["rev-parse", "--abbrev-ref", "HEAD"]:
        print(state["branch"])
    elif git_arguments == ["status", "--porcelain", "-uall"]:
        print(state["dirty"], end="")
    elif git_arguments == ["rev-parse", "--absolute-git-dir"]:
        print(state["git_admin_directory"])
    elif git_arguments == ["rev-parse", "HEAD"]:
        print(state["head"])
    elif git_arguments == ["pull", "--ff-only", "--quiet", "origin", "main"]:
        state["head"] = state["target_commit"]
        save()
    elif git_arguments == ["fetch", "--quiet", "origin", "main"]:
        pass
    elif git_arguments[:2] == ["merge-base", "--is-ancestor"] and git_arguments[3:] == ["FETCH_HEAD"]:
        if git_arguments[2] != state["target_commit"]:
            raise SystemExit(1)
    elif git_arguments[:3] == ["merge", "--ff-only", "--quiet"]:
        requested = git_arguments[3]
        if state.get("checkout_already_ahead"):
            # Real `merge --ff-only` silently no-ops when the checkout is
            # already at or ahead of the requested commit: HEAD stays where
            # it is instead of raising.
            pass
        else:
            if requested != state["target_commit"]:
                raise SystemExit(f"unexpected fast-forward target: {requested}")
            state["head"] = requested
            save()
    elif git_arguments == ["ls-files", "--error-unmatch", "--", "package.json"]:
        if not state["package_tracked"]:
            raise SystemExit(1)
        print("package.json")
    elif git_arguments == ["check-ignore", "--quiet", "--", "package.json"]:
        if not state["package_excluded"]:
            raise SystemExit(1)
    elif git_arguments[:2] == ["reset", "--hard"]:
        state["head"] = git_arguments[2]
        save()
    else:
        raise SystemExit(f"unsupported git invocation: {git_arguments!r}")
    raise SystemExit(0)

if command == "uv":
    if arguments == ["sync", "--locked"]:
        state["sync_count"] += 1
        save()
        raise SystemExit(0)
    if arguments[:4] == ["run", "--locked", "atelier2", "migrate"]:
        if state["serve_started"]:
            raise SystemExit("migration reached while serve was still running")
        store = Path(os.environ["ATELIER2_TEST_LIVE_STORE"])
        backups = list((store / "backups").glob("pre-redeploy-*"))
        if len(backups) != 1:
            raise SystemExit("migration reached without exactly one backup")
        for source in (path for name in state["store_filenames"] if (path := store / name).exists()):
            backup = backups[0] / source.name
            if not backup.exists() or backup.stat().st_size != source.stat().st_size:
                raise SystemExit(f"migration reached without an equal-size backup of {source.name}")
        if state.get("fail_migrate"):
            raise SystemExit(42)
        state["migrated"] = True
        save()
        raise SystemExit(0)
    if arguments[:5] == ["run", "--locked", "atelier2", "definition-source", "connect"]:
        if not state.get("migrated"):
            raise SystemExit("definition source connect reached before migration")
        if state.get("connect_refusal"):
            raise SystemExit(state["connect_refusal"])
        location = arguments[arguments.index("--location") + 1]
        ref = arguments[arguments.index("--ref") + 1]
        if state.get("connect_already_registered"):
            print(
                f"{DEFINITION_SOURCE_ID} is already connected to {location!r} at "
                f"{ref!r}; revision 1 is unchanged"
            )
        else:
            print(
                f"connected git source {location!r} at {ref!r} as "
                f"{DEFINITION_SOURCE_ID} revision 1"
            )
            print("  workflows/*.yaml -> workflow")
        raise SystemExit(0)
    if arguments[:5] == ["run", "--locked", "atelier2", "definition-source", "intake"]:
        if not state.get("migrated"):
            raise SystemExit("definition source intake reached before migration")
        if state.get("intake_refused"):
            print(
                "refused workflows/example.yaml: intake refused for testing",
                file=sys.stderr,
            )
            raise SystemExit(1)
        print(f"{DEFINITION_SOURCE_ID} at 'refs/heads/main' resolves to {'d' * 40}")
        print("  published workflow workflows/example.yaml")
        raise SystemExit(0)
    raise SystemExit(f"unsupported uv invocation: {arguments!r}")

if command == "npm":
    if arguments == ["ci"]:
        raise SystemExit(0)
    if arguments == ["run", "build"]:
        state["npm_build_count"] += 1
        save()
        if state.get("write_dist", True):
            output = Path.cwd() / "dist" / "index.html"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text("built\n", encoding="utf-8")
        raise SystemExit(0)
    raise SystemExit(f"unsupported npm invocation: {arguments!r}")

if command == "systemctl":
    if arguments == ["--user", "stop", "atelier2-serve.service"]:
        state["serve_started"] = False
        save()
        raise SystemExit(0)
    if arguments == ["--user", "start", "atelier2-serve.service"]:
        state["start_count"] += 1
        if state.get("fail_start_on") == state["start_count"]:
            save()
            raise SystemExit(62)
        state["serve_started"] = True
        save()
        raise SystemExit(0)
    raise SystemExit(f"unsupported systemctl invocation: {arguments!r}")

if command == "curl":
    if not state["serve_started"]:
        raise SystemExit(7)
    if state.get("health_refusals", 0) > 0:
        state["health_refusals"] -= 1
        save()
        raise SystemExit(7)
    health_commit = state.get("health_commit") or state["head"]
    print(json.dumps({"status": state["health_status"], "source_commit": health_commit}))
    raise SystemExit(0)

raise SystemExit(f"unsupported stub command: {command}")
"""


@dataclass(frozen=True)
class UpdateHarness:
    repository: Path
    store: Path
    state_path: Path
    log_path: Path
    environment: dict[str, str]

    @classmethod
    def create(cls, tmp_path: Path) -> UpdateHarness:
        repository = tmp_path / "deploy checkout"
        scripts = repository / "scripts"
        frontend = repository / "frontend"
        scripts.mkdir(parents=True)
        frontend.mkdir()
        shutil.copy2(SERVE_LIVE_UPDATE, scripts / SERVE_LIVE_UPDATE.name)
        git_admin_directory = repository / ".git"
        git_admin_directory.mkdir()
        (git_admin_directory / "serve-live.deployed").write_text(
            f"{'1' * 40}\n", encoding="utf-8"
        )

        data_home = tmp_path / "data home"
        store = data_home / "atelier2" / "live-store"
        store.mkdir(parents=True)
        for index, filename in enumerate(STORE_FILENAMES, start=1):
            (store / filename).write_bytes(bytes([index]) * (index + 2))

        state_path = tmp_path / "tool-state.json"
        state_path.write_text(
            json.dumps(
                {
                    "branch": "main",
                    "dirty": "",
                    "head": "1" * 40,
                    "target_commit": "2" * 40,
                    "git_admin_directory": str(git_admin_directory),
                    "package_tracked": False,
                    "package_excluded": True,
                    "serve_started": True,
                    "sync_count": 0,
                    "npm_build_count": 0,
                    "start_count": 0,
                    "health_status": "serving",
                    "store_filenames": list(STORE_FILENAMES),
                }
            ),
            encoding="utf-8",
        )
        log_path = tmp_path / "tool-log.jsonl"
        bin_directory = tmp_path / "bin"
        bin_directory.mkdir()
        tool_stub = bin_directory / "tool-stub"
        write_stub(tool_stub, TOOL_STUB)
        for command in ("git", "uv", "npm", "systemctl", "curl"):
            (bin_directory / command).symlink_to(tool_stub.name)
        write_stub(bin_directory / "sleep", "")

        environment = {
            **os.environ,
            "PATH": f"{bin_directory}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(tmp_path / "home"),
            "XDG_DATA_HOME": str(data_home),
            "ATELIER2_TEST_TOOL_STATE": str(state_path),
            "ATELIER2_TEST_TOOL_LOG": str(log_path),
            "ATELIER2_TEST_LIVE_STORE": str(store),
        }
        return cls(repository, store, state_path, log_path, environment)

    def configure(self, **settings: object) -> None:
        state = self.state()
        state.update(settings)
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

    def run(self, target_commit: str | None = None) -> subprocess.CompletedProcess[str]:
        command = ["bash", str(self.repository / "scripts" / SERVE_LIVE_UPDATE.name)]
        if target_commit is not None:
            command.append(target_commit)
        return subprocess.run(
            command,
            cwd=self.repository,
            env=self.environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def state(self) -> dict[str, object]:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def invocations(self) -> list[list[str]]:
        if not self.log_path.exists():
            return []
        return [
            json.loads(line)
            for line in self.log_path.read_text(encoding="utf-8").splitlines()
        ]

    def one_backup(self) -> Path:
        backups = list((self.store / "backups").glob("pre-redeploy-*"))
        assert len(backups) == 1
        return backups[0]

    @property
    def deployed_commit_marker(self) -> Path:
        return Path(str(self.state()["git_admin_directory"])) / "serve-live.deployed"


def assert_in_order(output: str, messages: tuple[str, ...]) -> None:
    positions = [output.index(message) for message in messages]
    assert positions == sorted(positions)


def test_clean_main_updates_in_the_declared_order_and_backs_up_the_store(
    tmp_path: Path,
) -> None:
    harness = UpdateHarness.create(tmp_path)

    completed = harness.run()

    assert completed.returncode == 0, completed.stderr
    assert_in_order(
        completed.stdout,
        (
            "checking the deploy checkout",
            "fast-forwarding main",
            "checking the root package.json stub",
            "syncing the locked Python environment",
            "building the frontend",
            "stopping atelier2-serve.service",
            "backing up the live store",
            "migrating the live store",
            "connecting the workflow definition source",
            "taking the connected source's workflows into the live catalog",
            "starting atelier2-serve.service",
            "checking live serve health",
            "now serves",
        ),
    )
    assert harness.invocations() == [
        ["git", "-C", str(harness.repository), "rev-parse", "--abbrev-ref", "HEAD"],
        ["git", "-C", str(harness.repository), "status", "--porcelain", "-uall"],
        [
            "git",
            "-C",
            str(harness.repository),
            "rev-parse",
            "--absolute-git-dir",
        ],
        [
            "curl",
            "-fsS",
            "--max-time",
            "5",
            "http://127.0.0.1:8422/atelier/api/v1/health",
        ],
        [
            "git",
            "-C",
            str(harness.repository),
            "pull",
            "--ff-only",
            "--quiet",
            "origin",
            "main",
        ],
        ["git", "-C", str(harness.repository), "rev-parse", "HEAD"],
        ["uv", "sync", "--locked"],
        ["npm", "ci"],
        ["npm", "run", "build"],
        ["systemctl", "--user", "stop", "atelier2-serve.service"],
        [
            "uv",
            "run",
            "--locked",
            "atelier2",
            "migrate",
            "--database",
            str(harness.store / "atelier.sqlite"),
        ],
        [
            "uv",
            "run",
            "--locked",
            "atelier2",
            "definition-source",
            "connect",
            "--database",
            str(harness.store / "atelier.sqlite"),
            "--location",
            str(harness.repository),
            "--ref",
            "refs/heads/main",
            "--select",
            "workflows/*.yaml=workflow",
            "--select",
            "workflows/schemas/*.json=schema",
            "--select",
            "workflows/budgets/*.json=budget_policy",
            "--actor",
            "atelier2-deploy",
        ],
        [
            "uv",
            "run",
            "--locked",
            "atelier2",
            "definition-source",
            "intake",
            "--database",
            str(harness.store / "atelier.sqlite"),
            "--source-id",
            DEFINITION_SOURCE_ID,
            "--actor",
            "atelier2-deploy",
        ],
        ["systemctl", "--user", "start", "atelier2-serve.service"],
        [
            "curl",
            "-fsS",
            "--max-time",
            "5",
            "http://127.0.0.1:8422/atelier/api/v1/health",
        ],
    ]
    backup = harness.one_backup()
    for filename in STORE_FILENAMES:
        assert (backup / filename).stat().st_size == (
            harness.store / filename
        ).stat().st_size


def test_intake_refusal_does_not_prevent_the_start_and_exits_its_own_code(
    tmp_path: Path,
) -> None:
    harness = UpdateHarness.create(tmp_path)
    harness.configure(intake_refused=True)

    completed = harness.run()

    assert completed.returncode == 3, completed.stderr
    assert_in_order(
        completed.stdout,
        (
            "migrating the live store",
            "connecting the workflow definition source",
            "taking the connected source's workflows into the live catalog",
            "starting atelier2-serve.service",
            "now serves",
        ),
    )
    assert "refused workflows/example.yaml" in completed.stdout
    assert "WORKFLOW INTAKE REFUSED" in completed.stderr
    assert harness.state()["serve_started"] is True
    assert harness.state()["start_count"] == 1
    intake_calls = [
        call
        for call in harness.invocations()
        if call[:5] == ["uv", "run", "--locked", "atelier2", "definition-source"]
        and call[5] == "intake"
    ]
    assert len(intake_calls) == 1
    assert str(harness.store / "atelier.sqlite") in intake_calls[0]


def test_a_refused_connect_says_why_restarts_the_previous_serve_and_never_intakes(
    tmp_path: Path,
) -> None:
    """The revert of the schema and budget selections, taken without reconnecting first.

    The older build cannot read the stored selections, so its connect refuses:
    the deploy must say so loudly and bring the serve it replaced back up, with
    a catalog no intake touched.
    """

    harness = UpdateHarness.create(tmp_path)
    previous_commit = str(harness.state()["head"])
    refusal = "the store holds a definition source it cannot read back"
    harness.configure(connect_refusal=refusal)

    completed = harness.run()

    assert completed.returncode == 1
    assert refusal in completed.stderr
    assert (
        "connecting the workflow definition source failed; restored the previous "
        "commit and restarted the live serve" in completed.stderr
    )
    assert harness.state()["head"] == previous_commit
    assert harness.state()["serve_started"] is True
    assert not any(
        call[:5] == ["uv", "run", "--locked", "atelier2", "definition-source"]
        and call[5] == "intake"
        for call in harness.invocations()
    )


@pytest.mark.parametrize("connect_already_registered", [False, True])
def test_the_id_connect_reports_either_way_is_the_id_intake_receives(
    tmp_path: Path, connect_already_registered: bool
) -> None:
    """A first connect and an idempotent repeat use different wordings

    (`connected ... as <id> ...` versus `<id> is already connected to ...`),
    and the script must read the source id out of either one.
    """
    harness = UpdateHarness.create(tmp_path)
    harness.configure(connect_already_registered=connect_already_registered)

    completed = harness.run()

    assert completed.returncode == 0, completed.stderr
    intake_calls = [
        call
        for call in harness.invocations()
        if call[:5] == ["uv", "run", "--locked", "atelier2", "definition-source"]
        and call[5] == "intake"
    ]
    assert len(intake_calls) == 1
    assert intake_calls[0][intake_calls[0].index("--source-id") + 1] == (
        DEFINITION_SOURCE_ID
    )


def test_with_a_target_argument_it_fast_forwards_exactly_to_that_commit(
    tmp_path: Path,
) -> None:
    harness = UpdateHarness.create(tmp_path)
    target_commit = str(harness.state()["target_commit"])

    completed = harness.run(target_commit=target_commit)

    assert completed.returncode == 0, completed.stderr
    assert harness.state()["head"] == target_commit
    assert (
        harness.deployed_commit_marker.read_text(encoding="utf-8")
        == f"{target_commit}\n"
    )
    git_arguments = [call[3:] for call in harness.invocations() if call[0] == "git"]
    assert ["fetch", "--quiet", "origin", "main"] in git_arguments
    assert ["merge-base", "--is-ancestor", target_commit, "FETCH_HEAD"] in git_arguments
    assert ["merge", "--ff-only", "--quiet", target_commit] in git_arguments
    assert not any(arguments[:1] == ["pull"] for arguments in git_arguments)


def test_refuses_a_target_not_reachable_from_origin_main(tmp_path: Path) -> None:
    harness = UpdateHarness.create(tmp_path)
    unreachable_commit = "9" * 40

    completed = harness.run(target_commit=unreachable_commit)

    assert completed.returncode != 0
    assert "is not reachable from origin/main" in completed.stderr
    assert harness.state()["head"] == "1" * 40
    assert not any(
        call[0] in {"uv", "npm", "systemctl"} for call in harness.invocations()
    )


def test_a_checkout_already_ahead_of_the_requested_target_refuses_the_deploy(
    tmp_path: Path,
) -> None:
    harness = UpdateHarness.create(tmp_path)
    ahead_commit = "9" * 40
    requested_target_commit = str(harness.state()["target_commit"])
    harness.configure(head=ahead_commit, checkout_already_ahead=True)

    completed = harness.run(target_commit=requested_target_commit)

    assert completed.returncode != 0
    assert (
        f"the checkout is ahead of {requested_target_commit}; "
        f"refusing to deploy {ahead_commit}" in completed.stderr
    )
    assert harness.state()["head"] == ahead_commit
    assert not any(
        call[0] in {"uv", "npm", "systemctl"} for call in harness.invocations()
    )


def test_failed_migration_rolls_back_to_served_commit_not_checkout_head(
    tmp_path: Path,
) -> None:
    harness = UpdateHarness.create(tmp_path)
    checkout_commit = "b" * 40
    served_commit = "a" * 40
    harness.configure(
        fail_migrate=True,
        head=checkout_commit,
        health_commit=served_commit,
    )

    completed = harness.run()

    assert completed.returncode != 0
    assert harness.state()["head"] == served_commit
    assert harness.state()["serve_started"] is True
    assert harness.state()["sync_count"] == 2
    assert harness.state()["npm_build_count"] == 2
    assert [
        invocation
        for invocation in harness.invocations()
        if invocation[0] in {"git", "uv", "npm", "systemctl"}
    ][-5:] == [
        ["git", "-C", str(harness.repository), "reset", "--hard", served_commit],
        ["uv", "sync", "--locked"],
        ["npm", "ci"],
        ["npm", "run", "build"],
        ["systemctl", "--user", "start", "atelier2-serve.service"],
    ]
    assert (
        "restored the previous commit and restarted the live serve" in completed.stderr
    )
    assert "live serve is DOWN" not in completed.stderr


def test_unreachable_serve_uses_deployed_marker_as_rollback_point(
    tmp_path: Path,
) -> None:
    harness = UpdateHarness.create(tmp_path)
    marker_commit = "c" * 40
    harness.deployed_commit_marker.write_text(f"{marker_commit}\n", encoding="utf-8")
    harness.configure(fail_migrate=True, health_refusals=1, health_commit=marker_commit)

    completed = harness.run()

    assert completed.returncode != 0
    assert harness.state()["head"] == marker_commit
    assert ["git", "-C", str(harness.repository), "reset", "--hard", marker_commit] in (
        harness.invocations()
    )


def test_unreachable_serve_without_deployed_marker_refuses_before_writes(
    tmp_path: Path,
) -> None:
    harness = UpdateHarness.create(tmp_path)
    harness.deployed_commit_marker.unlink()
    harness.configure(health_refusals=1)

    completed = harness.run()

    assert completed.returncode != 0
    assert "provide no rollback target; refusing the update" in completed.stderr
    assert harness.state()["head"] == "1" * 40
    assert not (harness.store / "backups").exists()
    assert harness.invocations() == [
        ["git", "-C", str(harness.repository), "rev-parse", "--abbrev-ref", "HEAD"],
        ["git", "-C", str(harness.repository), "status", "--porcelain", "-uall"],
        [
            "git",
            "-C",
            str(harness.repository),
            "rev-parse",
            "--absolute-git-dir",
        ],
        [
            "curl",
            "-fsS",
            "--max-time",
            "5",
            "http://127.0.0.1:8422/atelier/api/v1/health",
        ],
    ]


def test_successful_update_records_deployed_commit_marker(tmp_path: Path) -> None:
    harness = UpdateHarness.create(tmp_path)

    completed = harness.run()

    assert completed.returncode == 0, completed.stderr
    assert harness.deployed_commit_marker.read_text(encoding="utf-8") == f"{'2' * 40}\n"


def test_failed_migration_and_failed_rollback_report_that_the_serve_is_down(
    tmp_path: Path,
) -> None:
    harness = UpdateHarness.create(tmp_path)
    harness.configure(fail_migrate=True, fail_start_on=1)

    completed = harness.run()

    assert completed.returncode != 0
    assert harness.state()["serve_started"] is False
    assert "live serve is DOWN, operator action needed" in completed.stderr


@pytest.mark.parametrize(
    ("settings", "reason"),
    [
        ({"health_status": "starting"}, "not serving"),
        ({"health_commit": "f" * 40}, "does not match the deployed commit"),
    ],
)
def test_health_mismatch_fails_after_start(
    tmp_path: Path, settings: dict[str, object], reason: str
) -> None:
    harness = UpdateHarness.create(tmp_path)
    harness.configure(**settings)

    completed = harness.run()

    assert completed.returncode != 0
    assert harness.state()["serve_started"] is True
    assert reason in completed.stderr


def test_zero_byte_untracked_excluded_root_package_stub_is_removed(
    tmp_path: Path,
) -> None:
    harness = UpdateHarness.create(tmp_path)
    package = harness.repository / "package.json"
    package.touch()

    completed = harness.run()

    assert completed.returncode == 0, completed.stderr
    assert not package.exists()
    assert "removing excluded zero-byte root package.json stub" in completed.stdout


@pytest.mark.parametrize(
    ("contents", "tracked", "excluded", "reason"),
    [
        (b"", True, True, "is tracked"),
        (b"{}\n", False, True, "is not empty"),
        (b"", False, False, "is not excluded"),
    ],
)
def test_unsafe_root_package_files_are_refused_before_the_build(
    tmp_path: Path,
    contents: bytes,
    tracked: bool,
    excluded: bool,
    reason: str,
) -> None:
    harness = UpdateHarness.create(tmp_path)
    (harness.repository / "package.json").write_bytes(contents)
    harness.configure(package_tracked=tracked, package_excluded=excluded)

    completed = harness.run()

    assert completed.returncode != 0
    assert reason in completed.stderr
    assert not any(
        call[0] in {"uv", "npm", "systemctl"} for call in harness.invocations()
    )


def test_dirty_checkout_refuses_before_pull_and_can_never_reach_hard_reset(
    tmp_path: Path,
) -> None:
    harness = UpdateHarness.create(tmp_path)
    harness.configure(dirty=" M operator-work.txt\n")

    completed = harness.run()

    assert completed.returncode != 0
    assert "not clean" in completed.stderr
    git_arguments = [call[3:] for call in harness.invocations() if call[0] == "git"]
    assert not any(arguments[:1] == ["pull"] for arguments in git_arguments)
    assert not any(arguments[:2] == ["reset", "--hard"] for arguments in git_arguments)


def test_non_main_checkout_refuses_before_reading_or_changing_worktree_state(
    tmp_path: Path,
) -> None:
    harness = UpdateHarness.create(tmp_path)
    harness.configure(branch="operator-work")

    completed = harness.run()

    assert completed.returncode != 0
    assert "not main" in completed.stderr
    assert harness.invocations() == [
        ["git", "-C", str(harness.repository), "rev-parse", "--abbrev-ref", "HEAD"]
    ]


def test_missing_frontend_artifact_refuses_before_stopping_the_serve(
    tmp_path: Path,
) -> None:
    harness = UpdateHarness.create(tmp_path)
    previous_commit = str(harness.state()["head"])
    harness.configure(write_dist=False)

    completed = harness.run()

    assert completed.returncode != 0
    assert "frontend/dist/index.html is missing or empty" in completed.stderr
    assert harness.state()["serve_started"] is True
    assert not any(call[0] == "systemctl" for call in harness.invocations())
    # build_checkout failed after main had already fast-forwarded past the
    # broken target; the checkout must not be left sitting on it.
    assert harness.state()["head"] == previous_commit
    assert f"reset the checkout back to {previous_commit}" in completed.stderr


def test_backup_failure_rolls_back_to_the_previous_build_after_health_confirms_it(
    tmp_path: Path,
) -> None:
    harness = UpdateHarness.create(tmp_path)
    previous_commit = str(harness.state()["head"])
    (harness.store / "external.sqlite").unlink()

    completed = harness.run()

    assert completed.returncode != 0
    assert harness.state()["head"] == previous_commit
    assert harness.state()["serve_started"] is True
    assert harness.state()["sync_count"] == 2
    assert harness.state()["npm_build_count"] == 2
    assert [
        invocation
        for invocation in harness.invocations()
        if invocation[0] in {"git", "uv", "npm", "systemctl"}
    ][-5:] == [
        ["git", "-C", str(harness.repository), "reset", "--hard", previous_commit],
        ["uv", "sync", "--locked"],
        ["npm", "ci"],
        ["npm", "run", "build"],
        ["systemctl", "--user", "start", "atelier2-serve.service"],
    ]
    assert (
        "live store backup failed; restored the previous commit and restarted the live serve"
        in completed.stderr
    )
    assert "live serve is DOWN" not in completed.stderr
    assert not any(
        call[0] == "uv" and call[1:5] == ["run", "--locked", "atelier2", "migrate"]
        for call in harness.invocations()
    )


def test_absent_sqlite_sidecars_do_not_block_a_complete_backup(tmp_path: Path) -> None:
    harness = UpdateHarness.create(tmp_path)
    for filename in ("atelier.sqlite-wal", "atelier.sqlite-shm"):
        (harness.store / filename).unlink()

    completed = harness.run()

    assert completed.returncode == 0, completed.stderr
    backup = harness.one_backup()
    assert {path.name for path in backup.iterdir()} == {
        "atelier.sqlite",
        "external.sqlite",
    }


def test_health_polls_past_an_initial_refusal_after_start(tmp_path: Path) -> None:
    harness = UpdateHarness.create(tmp_path)
    harness.configure(health_refusals=3)

    completed = harness.run()

    assert completed.returncode == 0, completed.stderr
    assert "waiting up to 30s for live serve health" in completed.stdout
    assert "now serves" in completed.stdout


def test_health_that_never_becomes_available_fails_after_the_deadline(
    tmp_path: Path,
) -> None:
    harness = UpdateHarness.create(tmp_path)
    harness.configure(health_refusals=999)

    completed = harness.run()

    assert completed.returncode != 0
    assert "health is unavailable after start" in completed.stderr
    assert "now serves" not in completed.stdout
    curl_calls = [call for call in harness.invocations() if call[0] == "curl"]
    assert len(curl_calls) == 31


def test_symlinked_root_package_is_refused(tmp_path: Path) -> None:
    harness = UpdateHarness.create(tmp_path)
    (harness.repository / "package.json").symlink_to("does-not-exist")

    completed = harness.run()

    assert completed.returncode != 0
    assert "is a symlink" in completed.stderr
    assert not any(
        call[0] in {"uv", "npm", "systemctl"} for call in harness.invocations()
    )
