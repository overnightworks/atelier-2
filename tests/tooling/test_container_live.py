from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from tests.tooling.container_test_support import (
    started_process,
    wait_for_exit,
    wait_until_exists,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONTAINER_LIVE = PROJECT_ROOT / "scripts" / "container_live.sh"
CONTAINER_SNAPSHOT = PROJECT_ROOT / "scripts" / "container_snapshot.sh"
CONTAINER_UP = PROJECT_ROOT / "scripts" / "container_up.sh"
CONTAINER_SERVE = PROJECT_ROOT / "scripts" / "container_serve.sh"
COMPOSE = PROJECT_ROOT / "compose.yaml"
DOCKERFILE = PROJECT_ROOT / "Dockerfile"

CONTAINER_ID = "a" * 64
UPDATED_CONTAINER_ID = "3" * 64
IMAGE_ID = f"sha256:{'b' * 64}"
UPDATED_IMAGE_ID = f"sha256:{'7' * 64}"
NETWORK_ID = "c" * 64
ENGINE_ID = "engine:local:test"
PROJECT_NAME = re.compile(r"^atelier2-live-[0-9a-f]{16}$")
RECORDED_START_STOP = [
    ["start", CONTAINER_ID],
    ["stop", "--time", "30", CONTAINER_ID],
]

# An unrelated Docker owner (the disposable Runner witness harness) that
# shares this host but carries a different label and name prefix. It must
# survive every uninstall/install cycle untouched.
FOREIGN_DEPLOYMENT_LABEL = "atelier2-301a-runner-witness"
FOREIGN_PROJECT = "atelier2-301a-f00dfeed"
FOREIGN_CONTAINER_ID = "9" * 64
FOREIGN_IMAGE_ID = f"sha256:{'8' * 64}"
FOREIGN_VOLUME_NAME = f"{FOREIGN_PROJECT}_store"
FOREIGN_NETWORK_NAME = f"{FOREIGN_PROJECT}_serve"
FOREIGN_IMAGE_REFERENCE = "atelier2-301a-core"


def write_stub(path: Path, body: str) -> None:
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(0o755)


def install_docker_stub(directory: Path) -> None:
    write_stub(
        directory / "docker",
        f"""\
import json
import os
import signal
import shutil
import sys
import time
from pathlib import Path
CONTAINER_ID = {CONTAINER_ID!r}; UPDATED_CONTAINER_ID = {UPDATED_CONTAINER_ID!r}
IMAGE_ID = {IMAGE_ID!r}; UPDATED_IMAGE_ID = {UPDATED_IMAGE_ID!r}
NETWORK_ID = {NETWORK_ID!r}; ENGINE_ID = {ENGINE_ID!r}
FOREIGN_DEPLOYMENT_LABEL = {FOREIGN_DEPLOYMENT_LABEL!r}
FOREIGN_CONTAINER_ID = {FOREIGN_CONTAINER_ID!r}; FOREIGN_IMAGE_ID = {FOREIGN_IMAGE_ID!r}
FOREIGN_VOLUME_NAME = {FOREIGN_VOLUME_NAME!r}; FOREIGN_NETWORK_NAME = {FOREIGN_NETWORK_NAME!r}
FOREIGN_IMAGE_REFERENCE = {FOREIGN_IMAGE_REFERENCE!r}
foreign_present = os.environ.get("ATELIER2_TEST_FOREIGN_LOCAL_RESOURCE") == "1"
arguments = sys.argv[1:]
state_path = Path(os.environ["ATELIER2_TEST_DOCKER_STATE"]); record_path = Path(os.environ["ATELIER2_TEST_DOCKER_RECORD"])
build_count_path = Path(os.environ["ATELIER2_TEST_DOCKER_BUILD_COUNT"])
with record_path.open("a", encoding="utf-8") as output:
    output.write(json.dumps(arguments) + "\\n")
state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {{}}
drift = os.environ.get("ATELIER2_TEST_DRIFT", ""); list_failure = os.environ.get("ATELIER2_TEST_PROJECT_LIST_FAILURE", "")
def save() -> None:
    state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
def current_image_id() -> str:
    # The second build a preserving update runs (the first belongs to the
    # install this test built on) produces a distinguishable image, exactly
    # as a real second `docker build` would produce a different digest.
    built = int(build_count_path.read_text()) if build_count_path.exists() else 0
    return UPDATED_IMAGE_ID if built >= 2 else IMAGE_ID
def current_container_id() -> str:
    # The service's labels bake in the running commit, so a second `up`
    # always carries a different config hash than the first and Compose
    # always recreates the container under a new id -- deleting the previous
    # one as part of that same call, exactly like a real redeploy.
    built = int(build_count_path.read_text()) if build_count_path.exists() else 0
    return UPDATED_CONTAINER_ID if built >= 2 else CONTAINER_ID
def wait_at(phase: str) -> None:
    if phase not in os.environ.get("ATELIER2_TEST_WAIT_PHASE", "").split(","):
        return
    (Path(os.environ["ATELIER2_TEST_READY_DIRECTORY"]) / f"{{phase}}-ready").touch()
    if phase != "start-cleanup":
        signal.pause()
        return
    for interruption in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        signal.signal(interruption, signal.SIG_IGN)
    release = Path(os.environ["ATELIER2_TEST_READY_DIRECTORY"]) / "start-cleanup-release"
    while not release.exists():
        time.sleep(0.01)
def label(name: str, origin: bool = False) -> str:
    # The volume keeps its creation-time (origin) commit/tree labels for as
    # long as a preserving update reuses it -- Compose never touches it. The
    # network has no such continuity: Compose recreates it on every `update`,
    # labelling it with the currently running commit/tree just like the
    # container.
    if drift == "label" and name == "atelier2.deployment":
        return "foreign"
    if name == "atelier2.deployment":
        return state["deployment"]
    if name == "atelier2.source.commit":
        commit = state["store_source_commit"] if origin else state["source_commit"]
        return "0" * 40 if drift == "source" else commit
    if name == "atelier2.source.tree":
        return state["store_source_tree"] if origin else state["source_tree"]
    if name == "com.docker.compose.project":
        return state["project"]
    return ""
if arguments[:2] == ["info", "--format"]:
    print(os.environ.get("ATELIER2_TEST_INITIAL_ENGINE_ID", "different-engine" if drift == "engine" else ENGINE_ID))
    raise SystemExit(0)
if "com.docker.compose.project=" in arguments[-1]:
    if list_failure == "container" and arguments[:2] == ["ps", "--all"]: raise SystemExit(52)
    if list_failure in ("volume", "network") and arguments[:2] == [list_failure, "ls"]: raise SystemExit(52)
if arguments and arguments[0] == "compose":
    project = arguments[arguments.index("--project-name") + 1]
    if "build" in arguments:
        if os.environ.get("ATELIER2_TEST_REQUIRE_INTENT") == "1":
            record = Path(os.environ["XDG_STATE_HOME"]) / "atelier2/container-live/installation.state"
            if not record.is_file() or "state=INSTALLING\\n" not in record.read_text(encoding="utf-8"): raise SystemExit(61)
        wait_at("build")
        if os.environ.get("ATELIER2_TEST_FAIL_BUILD") == "1": raise SystemExit(41)
        built = (int(build_count_path.read_text()) if build_count_path.exists() else 0) + 1
        build_count_path.write_text(str(built))
        context = Path(arguments[arguments.index("--project-directory") + 1])
        copy_to = os.environ.get("ATELIER2_TEST_CONTEXT")
        if copy_to: shutil.copytree(context, copy_to)
        raise SystemExit(0)
    if "up" in arguments:
        wait_at("up")
        previous_store_commit = state.get("store_source_commit")
        previous_store_tree = state.get("store_source_tree")
        state = {{
            "project": project,
            "deployment": os.environ["ATELIER2_DEPLOYMENT"],
            "source_commit": os.environ["ATELIER2_SOURCE_COMMIT"],
            "source_tree": os.environ["ATELIER2_SOURCE_TREE"],
            "store_source_commit": previous_store_commit or os.environ["ATELIER2_SOURCE_COMMIT"],
            "store_source_tree": previous_store_tree or os.environ["ATELIER2_SOURCE_TREE"],
            "status": "running",
            "health": "healthy",
            "image_id": current_image_id(),
            "container_id": current_container_id(),
        }}
        save()
        if os.environ.get("ATELIER2_TEST_FAIL_UP") == "1": raise SystemExit(42)
        raise SystemExit(0)
    if "ps" in arguments:
        if state: print(state.get("container_id", CONTAINER_ID))
        raise SystemExit(0)
    if "down" in arguments:
        if os.environ.get("ATELIER2_TEST_FAIL_DOWN") == "1": raise SystemExit(43)
        if state.get("project") == project: state = {{}}; save()
        raise SystemExit(0)
if arguments[:1] == ["run"] and "migrate" in arguments:
    if os.environ.get("ATELIER2_TEST_FAIL_MIGRATE") == "1":
        print("schema version 2 is unknown; this command will not alter it", file=sys.stderr)
        raise SystemExit(1)
    print("schema version 1 is already current")
    print("  fingerprint " + "f" * 64)
    print("  nothing to migrate")
    raise SystemExit(0)
if arguments[:3] == ["inspect", "--type", "image"]:
    if arguments[arguments.index("--format") + 1] == "{{{{.Id}}}}":
        print(arguments[-1])
        raise SystemExit(0)
    raise SystemExit(45)
if arguments[:2] == ["ps", "--all"]:
    if os.environ.get("ATELIER2_TEST_FOREIGN_RESOURCE") == "1":
        print("d" * 64)
    else:
        wanted = arguments[-1].split("=", 2)[-1]
        if foreign_present and wanted == FOREIGN_DEPLOYMENT_LABEL:
            print(FOREIGN_CONTAINER_ID)
        if state and not state.get("container_removed") and wanted in ("local-live", state["project"]):
            print(state.get("container_id", CONTAINER_ID))
    raise SystemExit(0)
if arguments[:2] in (["volume", "ls"], ["network", "ls"]):
    if os.environ.get("ATELIER2_TEST_FOREIGN_RESOURCE") == "1":
        print("foreign")
    else:
        wanted = arguments[-1].split("=", 2)[-1]
        foreign_name = FOREIGN_VOLUME_NAME if arguments[0] == "volume" else FOREIGN_NETWORK_NAME
        if foreign_present and wanted == FOREIGN_DEPLOYMENT_LABEL:
            print(foreign_name)
        if state and not state.get(f"{{arguments[0]}}_removed") and wanted in ("local-live", state["project"]):
            suffix = "store" if arguments[0] == "volume" else "serve"
            print(f"{{state['project']}}_{{suffix}}")
    raise SystemExit(0)
if arguments[:2] == ["images", "--quiet"]:
    prefix = arguments[arguments.index("--filter") + 1].split("=", 1)[1].rstrip("*")
    if foreign_present and FOREIGN_IMAGE_REFERENCE.startswith(prefix):
        print(FOREIGN_IMAGE_ID)
    if state and not state.get("image_removed") and f"{{state['project']}}-serve".startswith(prefix):
        print(current_image_id())
    raise SystemExit(0)
if arguments[:1] == ["rm"] and "--force" in arguments:
    identifier = arguments[-1]
    if identifier == FOREIGN_CONTAINER_ID: raise SystemExit(58)
    if not state or identifier != state.get("container_id", CONTAINER_ID): raise SystemExit(54)
    state["container_removed"] = True
    if all(state.get(f"{{kind}}_removed") for kind in ("container", "network", "volume", "image")):
        state = {{}}
    save()
    raise SystemExit(0)
if arguments[:2] == ["network", "rm"]:
    identifier = arguments[-1]
    if identifier == FOREIGN_NETWORK_NAME: raise SystemExit(59)
    if not state or identifier != f"{{state['project']}}_serve": raise SystemExit(55)
    state["network_removed"] = True
    if all(state.get(f"{{kind}}_removed") for kind in ("container", "network", "volume", "image")):
        state = {{}}
    save()
    raise SystemExit(0)
if arguments[:2] == ["volume", "rm"] and "--force" in arguments:
    identifier = arguments[-1]
    if identifier == FOREIGN_VOLUME_NAME: raise SystemExit(60)
    if not state or identifier != f"{{state['project']}}_store": raise SystemExit(56)
    state["volume_removed"] = True
    if all(state.get(f"{{kind}}_removed") for kind in ("container", "network", "volume", "image")):
        state = {{}}
    save()
    raise SystemExit(0)
if arguments[:1] == ["rmi"] and "--force" in arguments:
    identifier = arguments[-1]
    if identifier == FOREIGN_IMAGE_ID: raise SystemExit(62)
    if identifier not in (IMAGE_ID, UPDATED_IMAGE_ID): raise SystemExit(57)
    # Removing a stale, no-longer-current image (a preserving update's own
    # cleanup of the previous digest) does not touch installed-resource
    # bookkeeping -- exactly like real Docker, which leaves the running
    # container, network, and volume untouched by removing a dangling image.
    if state and identifier == state.get("image_id"):
        state["image_removed"] = True
        if all(state.get(f"{{kind}}_removed") for kind in ("container", "network", "volume", "image")):
            state = {{}}
        save()
    raise SystemExit(0)
if arguments and arguments[0] == "inspect":
    if not state or arguments[-1] != state.get("container_id", CONTAINER_ID): raise SystemExit(44)
    template = arguments[arguments.index("--format") + 1]
    template = template.replace("{{{{", "{{").replace("}}}}", "}}")
    if template == "{{.Id}}":
        print("d" * 64 if drift == "container" else state.get("container_id", CONTAINER_ID))
    elif template == "{{.Image}}":
        print(os.environ.get("ATELIER2_TEST_INITIAL_IMAGE_ID", f"sha256:{{'e' * 64}}" if drift == "image" else state.get("image_id", IMAGE_ID)))
    elif "index .Config.Labels" in template:
        name = template.split('"')[1]
        print(label(name))
    elif template == "{{.HostConfig.RestartPolicy.Name}}":
        print("always" if drift == "restart" else "unless-stopped")
    elif template == "{{.HostConfig.ReadonlyRootfs}}":
        print("true")
    elif template == "{{.HostConfig.Privileged}}":
        print("false")
    elif template == "{{json .HostConfig.CapDrop}}":
        print('["ALL"]')
    elif template == "{{json .HostConfig.SecurityOpt}}":
        print('["no-new-privileges:true"]')
    elif template == "{{len .HostConfig.PortBindings}}":
        print("1")
    elif "HostConfig.PortBindings" in template:
        print("127.0.0.1:9999" if drift == "port" else "127.0.0.1:8422")
    elif "range .Mounts" in template:
        print("bind||/var/lib/atelier2/store|true" if drift == "mount" else f"volume|{{state['project']}}_store|/var/lib/atelier2/store|true")
    elif template == "{{len .NetworkSettings.Networks}}":
        print("0" if drift == "network-detached" else "2" if drift == "network-extra" else "1")
    elif "index .NetworkSettings.Networks" in template:
        print("" if drift == "network-wrong" else "d" * 64 if drift == "network-attachment-id" else NETWORK_ID)
    elif template == "{{json .Config}}":
        configuration = {{"image": state.get("image_id", IMAGE_ID), "project": state["project"], "source": state["source_commit"]}}
        if drift == "config": configuration["changed"] = True
        print(json.dumps(configuration, sort_keys=True, separators=(",", ":")))
    elif template == "{{.State.Status}}":
        print(state["status"])
    elif template == "{{.State.Health.Status}}":
        wait_at("health")
        print("unhealthy" if drift == "health" else state["health"])
    else: raise SystemExit(45)
    raise SystemExit(0)
if arguments[:2] in (["volume", "inspect"], ["network", "inspect"]):
    resource_type = arguments[0]
    template = arguments[arguments.index("--format") + 1]
    template = template.replace("{{{{", "{{").replace("}}}}", "}}")
    name = arguments[-1]
    expected = f"{{state['project']}}_{{'store' if resource_type == 'volume' else 'serve'}}"
    if not state or name != expected: raise SystemExit(46)
    if template == "{{.Name}}":
        print(name)
    elif template == "{{.Id}}" and resource_type == "network":
        print(os.environ.get("ATELIER2_TEST_INITIAL_NETWORK_ID", "f" * 64 if drift == "network" else NETWORK_ID))
    elif "index .Labels" in template:
        print(label(template.split('"')[1], origin=(resource_type == "volume")))
    else: raise SystemExit(47)
    raise SystemExit(0)
if arguments and arguments[0] == "stop":
    if not state or arguments[-1] != state.get("container_id", CONTAINER_ID): raise SystemExit(48)
    wait_at("start-cleanup")
    if os.environ.get("ATELIER2_TEST_FAIL_STOP") == "1": raise SystemExit(53)
    state["status"] = "exited"
    save()
    print(state.get("container_id", CONTAINER_ID))
    raise SystemExit(0)
if arguments and arguments[0] == "start":
    if not state or arguments[-1] != state.get("container_id", CONTAINER_ID): raise SystemExit(49)
    wait_at("start")
    state["status"] = "running"
    state["health"] = "healthy"
    save()
    if os.environ.get("ATELIER2_TEST_FAIL_START") == "1": raise SystemExit(51)
    print(state.get("container_id", CONTAINER_ID))
    raise SystemExit(0)
raise SystemExit(50)
""",
    )


def install_host_stubs(directory: Path) -> None:
    write_stub(
        directory / "systemctl",
        """\
import os
import sys

unit = next(argument for argument in sys.argv if argument.endswith(".service"))
state = os.environ.get("ATELIER2_TEST_HOST_UNIT", "off")
if state == "active":
    print("LoadState=loaded\\nActiveState=active\\nUnitFileState=enabled")
elif state == "enabled":
    print("LoadState=loaded\\nActiveState=inactive\\nUnitFileState=enabled")
elif unit == "atelier2-live.service":
    print("LoadState=loaded\\nActiveState=inactive\\nUnitFileState=disabled")
else:
    print("LoadState=not-found\\nActiveState=inactive\\nUnitFileState=")
""",
    )
    write_stub(
        directory / "ss",
        """\
import os

if os.environ.get("ATELIER2_TEST_PORT_BUSY") == "1":
    print("LISTEN 0 4096 127.0.0.1:8422 0.0.0.0:*")
""",
    )
    write_stub(directory / "sleep", "")
    real_stat = shutil.which("stat")
    assert real_stat is not None
    write_stub(
        directory / "stat",
        f"""\
import os
import sys

if os.environ.get("ATELIER2_TEST_WRONG_OWNER") == "1" and sys.argv[-1].endswith("installation.state"):
    print("999999:600")
    raise SystemExit(0)
os.execv({real_stat!r}, [{real_stat!r}, *sys.argv[1:]])
""",
    )


def _with_default_interruption_signals(command: list[str]) -> list[str]:
    # Bash cannot trap a signal that was ignored at entry. Non-interactive
    # parents often inherit SIGHUP=IGN; restoring SIG_DFL lets the script's
    # own traps run.
    return [sys.executable, "-c", _RESTORE_DEFAULT_INTERRUPTION_SIGNALS, *command]


_RESTORE_DEFAULT_INTERRUPTION_SIGNALS = """\
import os
import signal
import sys

for interruption in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
    signal.signal(interruption, signal.SIG_DFL)
os.execvp(sys.argv[1], sys.argv[1:])
"""


def run_git(repository: Path, *arguments: str) -> str:
    environment = {
        "PATH": os.environ["PATH"],
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "container-live-test",
        "GIT_AUTHOR_EMAIL": "container-live-test@invalid",
        "GIT_COMMITTER_NAME": "container-live-test",
        "GIT_COMMITTER_EMAIL": "container-live-test@invalid",
    }
    return subprocess.run(
        _with_default_interruption_signals(["git", "-C", str(repository), *arguments]),
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def lifecycle_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository with ; metacharacters"
    (repository / "scripts").mkdir(parents=True)
    for source, destination in (
        (CONTAINER_LIVE, repository / "scripts/container_live.sh"),
        (CONTAINER_SNAPSHOT, repository / "scripts/container_snapshot.sh"),
        (CONTAINER_UP, repository / "scripts/container_up.sh"),
        (CONTAINER_SERVE, repository / "scripts/container_serve.sh"),
        (COMPOSE, repository / "compose.yaml"),
        (DOCKERFILE, repository / "Dockerfile"),
    ):
        shutil.copy2(source, destination)
    (repository / "payload.txt").write_text("committed\n", encoding="utf-8")
    run_git(repository, "init", "--quiet", "--initial-branch=main")
    run_git(repository, "add", "--all")
    run_git(repository, "commit", "--quiet", "--message", "fixture")
    return repository


def lifecycle_environment(tmp_path: Path, **settings: str) -> dict[str, str]:
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir(exist_ok=True)
    install_docker_stub(bin_directory)
    install_host_stubs(bin_directory)
    environment = {
        "PATH": f"{bin_directory}{os.pathsep}{os.environ['PATH']}",
        "XDG_STATE_HOME": str(tmp_path / "state home ; metacharacters"),
        "TMPDIR": str(tmp_path),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "ATELIER2_TEST_DOCKER_STATE": str(tmp_path / "docker-state.json"),
        "ATELIER2_TEST_DOCKER_RECORD": str(tmp_path / "docker-record.jsonl"),
        "ATELIER2_TEST_DOCKER_BUILD_COUNT": str(tmp_path / "docker-build-count"),
        "ATELIER2_TEST_CONTEXT": str(tmp_path / "docker-context"),
        "ATELIER2_TEST_READY_DIRECTORY": str(tmp_path),
    }
    environment.update(settings)
    return environment


def run_live(
    repository: Path,
    tmp_path: Path,
    command: str,
    *extra_arguments: str,
    **settings: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _with_default_interruption_signals(
            [
                "bash",
                str(repository / "scripts/container_live.sh"),
                command,
                *extra_arguments,
            ]
        ),
        cwd=repository,
        env=lifecycle_environment(tmp_path, **settings),
        capture_output=True,
        text=True,
        check=False,
    )


def docker_invocations(tmp_path: Path) -> list[list[str]]:
    path = tmp_path / "docker-record.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def installation_directory(tmp_path: Path) -> Path:
    return tmp_path / "state home ; metacharacters/atelier2/container-live"


def read_record(tmp_path: Path) -> dict[str, str]:
    path = installation_directory(tmp_path) / "installation.state"
    return dict(
        line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines()
    )


def installed_repository(tmp_path: Path) -> Path:
    repository = lifecycle_repository(tmp_path)
    completed = run_live(
        repository,
        tmp_path,
        "install",
        ATELIER2_TEST_REQUIRE_INTENT="1",
    )
    assert completed.returncode == 0, completed.stderr
    return repository


def stopped_repository(tmp_path: Path) -> tuple[Path, bytes]:
    repository = installed_repository(tmp_path)
    assert run_live(repository, tmp_path, "stop").returncode == 0
    record = (installation_directory(tmp_path) / "installation.state").read_bytes()
    (tmp_path / "docker-record.jsonl").write_text("", encoding="utf-8")
    return repository, record


def docker_mutations(invocations: list[list[str]]) -> list[list[str]]:
    return [
        arguments
        for arguments in invocations
        if "build" in arguments
        or "up" in arguments
        or "down" in arguments
        or arguments[:1] in (["start"], ["stop"], ["rm"], ["rmi"], ["run"])
        or arguments[:2] in (["network", "rm"], ["volume", "rm"])
    ]


def test_status_without_an_installation_is_truthfully_incomplete(
    tmp_path: Path,
) -> None:
    repository = lifecycle_repository(tmp_path)

    completed = run_live(repository, tmp_path, "status")

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "INCOMPLETE\n"
    assert not installation_directory(tmp_path).exists()
    assert docker_invocations(tmp_path) == []


def test_install_publishes_private_exact_identity_before_handoff(
    tmp_path: Path,
) -> None:
    repository = installed_repository(tmp_path)

    record = read_record(tmp_path)
    directory = installation_directory(tmp_path)
    assert record["state"] == "INSTALLED"
    assert record["deployment"] == "local-live"
    assert record["published_port"] == "8422"
    assert record["restart_policy"] == "unless-stopped"
    assert PROJECT_NAME.fullmatch(record["project"])
    assert record["source_commit"] == run_git(repository, "rev-parse", "HEAD")
    assert record["source_tree"] == run_git(repository, "rev-parse", "HEAD^{tree}")
    assert record["engine_id"] == ENGINE_ID
    assert record["container_id"] == CONTAINER_ID
    assert record["image_id"] == IMAGE_ID
    assert record["network_id"] == NETWORK_ID
    assert record["volume_name"] == f"{record['project']}_store"
    assert record["network_name"] == f"{record['project']}_serve"
    assert (directory.stat().st_mode & 0o777) == 0o700
    for filename in ("lifecycle.lock", "installation.state", "compose.yaml"):
        path = directory / filename
        assert path.is_file()
        assert not path.is_symlink()
        assert (path.stat().st_mode & 0o777) == 0o600
    assert not list(directory.glob(".*.??????"))
    assert not list(tmp_path.glob("atelier2-live.*"))
    invocations = docker_invocations(tmp_path)
    assert all("prune" not in arguments for arguments in invocations)
    build = next(arguments for arguments in invocations if "build" in arguments)
    up = next(arguments for arguments in invocations if "up" in arguments)
    assert build[build.index("--project-name") + 1] == record["project"]
    assert up[-6:] == ["up", "--detach", "--wait", "--wait-timeout", "30", "--no-build"]


def test_status_is_read_only_and_distinguishes_running_and_stopped(
    tmp_path: Path,
) -> None:
    repository = installed_repository(tmp_path)
    record_path = tmp_path / "docker-record.jsonl"
    before_record = (
        installation_directory(tmp_path) / "installation.state"
    ).read_bytes()
    before_descriptor = (installation_directory(tmp_path) / "compose.yaml").read_bytes()
    record_path.write_text("", encoding="utf-8")

    running = run_live(repository, tmp_path, "status")

    assert running.returncode == 0, running.stderr
    assert running.stdout == "RUNNING\n"
    assert docker_mutations(docker_invocations(tmp_path)) == []
    assert (
        installation_directory(tmp_path) / "installation.state"
    ).read_bytes() == before_record
    assert (
        installation_directory(tmp_path) / "compose.yaml"
    ).read_bytes() == before_descriptor

    stopped = run_live(repository, tmp_path, "stop")
    assert stopped.returncode == 0, stopped.stderr
    record_path.write_text("", encoding="utf-8")
    status = run_live(repository, tmp_path, "status")
    assert status.stdout == "STOPPED\n"
    assert docker_mutations(docker_invocations(tmp_path)) == []


def test_stop_and_start_use_only_the_recorded_container_id(tmp_path: Path) -> None:
    repository = installed_repository(tmp_path)
    record_path = tmp_path / "docker-record.jsonl"
    record_path.write_text("", encoding="utf-8")

    stopped = run_live(repository, tmp_path, "stop")
    started = run_live(repository, tmp_path, "start")

    assert stopped.returncode == 0, stopped.stderr
    assert stopped.stdout == "STOPPED\n"
    assert started.returncode == 0, started.stderr
    assert started.stdout == "RUNNING\n"
    mutations = docker_mutations(docker_invocations(tmp_path))
    assert mutations == [
        ["stop", "--time", "30", CONTAINER_ID],
        ["start", CONTAINER_ID],
    ]
    assert all("compose" not in arguments for arguments in mutations)


@pytest.mark.parametrize(
    "settings",
    (
        {"ATELIER2_TEST_PORT_BUSY": "1"},
        {"ATELIER2_TEST_HOST_UNIT": "active"},
        {"ATELIER2_TEST_HOST_UNIT": "enabled"},
        {"ATELIER2_TEST_FOREIGN_RESOURCE": "1"},
        {"ATELIER2_DEPLOYMENT": "disposable"},
        {"ATELIER2_PUBLISHED_PORT": "9999"},
        {"ATELIER2_RESTART_POLICY": "always"},
        {"ATELIER2_TEST_INITIAL_ENGINE_ID": ""},
        {"ATELIER2_TEST_INITIAL_ENGINE_ID": "engine\nmalformed"},
    ),
)
def test_install_refuses_collisions_and_ambient_modes_before_docker_mutation(
    tmp_path: Path, settings: dict[str, str]
) -> None:
    repository = lifecycle_repository(tmp_path)

    completed = run_live(repository, tmp_path, "install", **settings)

    assert completed.returncode != 0
    assert docker_mutations(docker_invocations(tmp_path)) == []
    assert not (installation_directory(tmp_path) / "installation.state").exists()


def test_dirty_source_refuses_before_docker_and_durable_intent(tmp_path: Path) -> None:
    repository = lifecycle_repository(tmp_path)
    (repository / "ambient.txt").write_text("dirty\n", encoding="utf-8")

    completed = run_live(repository, tmp_path, "install")

    assert completed.returncode != 0
    assert "source tree must be clean" in completed.stderr
    assert docker_mutations(docker_invocations(tmp_path)) == []
    assert not (installation_directory(tmp_path) / "installation.state").exists()
    assert not list(tmp_path.glob("atelier2-live.*"))


@pytest.mark.parametrize(
    "drift",
    (
        "engine",
        "image",
        "container",
        "label",
        "source",
        "restart",
        "port",
        "mount",
        "network",
        "network-detached",
        "network-wrong",
        "network-extra",
        "network-attachment-id",
        "config",
    ),
)
def test_identity_drift_refuses_status_and_exact_operations(
    tmp_path: Path, drift: str
) -> None:
    repository = installed_repository(tmp_path)
    record_path = tmp_path / "docker-record.jsonl"
    record_path.write_text("", encoding="utf-8")

    status = run_live(repository, tmp_path, "status", ATELIER2_TEST_DRIFT=drift)
    stopped = run_live(repository, tmp_path, "stop", ATELIER2_TEST_DRIFT=drift)
    started = run_live(repository, tmp_path, "start", ATELIER2_TEST_DRIFT=drift)

    assert status.returncode == 0
    assert status.stdout == "DRIFTED\n"
    assert stopped.returncode != 0
    assert "drifted" in stopped.stderr
    assert started.returncode != 0
    assert "drifted" in started.stderr
    assert docker_mutations(docker_invocations(tmp_path)) == []


@pytest.mark.parametrize(
    "damage",
    (
        "owner",
        "mode",
        "oversized",
        "symlink",
        "state-downgrade",
        "injection",
        "descriptor",
    ),
    ids=(
        "record-wrong-owner",
        "record-wrong-mode",
        "record-oversized",
        "record-symlink",
        "record-state-downgrade",
        "record-injection",
        "descriptor-content",
    ),
)
def test_record_boundary_refuses_unsafe_or_drifted_state(
    tmp_path: Path, damage: str
) -> None:
    repository = installed_repository(tmp_path)
    record_path = installation_directory(tmp_path) / "installation.state"
    original = record_path.read_bytes()
    if damage == "owner":
        pass
    elif damage == "mode":
        record_path.chmod(0o644)
    elif damage == "oversized":
        record_path.write_bytes(b"x" * 16385)
    elif damage == "symlink":
        target = installation_directory(tmp_path) / "record-target"
        target.write_bytes(original)
        target.chmod(0o600)
        record_path.unlink()
        record_path.symlink_to(target)
    elif damage == "state-downgrade":
        record_path.write_bytes(
            original.replace(b"state=INSTALLED", b"state=INSTALLING")
        )
    elif damage == "injection":
        marker = tmp_path / "record-was-executed"
        record_path.write_bytes(original + f"project=$(touch {marker})\n".encode())
    else:
        (installation_directory(tmp_path) / "compose.yaml").write_text(
            "changed\n", encoding="utf-8"
        )
    if damage in ("oversized", "injection"):
        record_path.chmod(0o600)
    before = docker_invocations(tmp_path)

    completed = run_live(
        repository,
        tmp_path,
        "status",
        ATELIER2_TEST_WRONG_OWNER="1" if damage == "owner" else "0",
    )

    assert completed.returncode == 0
    assert completed.stdout == "DRIFTED\n"
    assert docker_mutations(docker_invocations(tmp_path)[len(before) :]) == []
    assert not (tmp_path / "record-was-executed").exists()


def test_nonblocking_lock_refuses_concurrent_lifecycle_command(tmp_path: Path) -> None:
    repository = installed_repository(tmp_path)
    lock_path = installation_directory(tmp_path) / "lifecycle.lock"
    with lock_path.open("r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        completed = run_live(repository, tmp_path, "status")

    assert completed.returncode != 0
    assert "lifecycle is busy" in completed.stderr


@pytest.mark.parametrize(
    "settings",
    (
        {"ATELIER2_TEST_FAIL_UP": "1"},
        {"ATELIER2_TEST_INITIAL_IMAGE_ID": "sha256:invalid"},
        {"ATELIER2_TEST_INITIAL_NETWORK_ID": "invalid"},
    ),
)
def test_failed_install_cleans_only_its_intent_owned_project(
    tmp_path: Path, settings: dict[str, str]
) -> None:
    repository = lifecycle_repository(tmp_path)

    completed = run_live(repository, tmp_path, "install", **settings)

    assert completed.returncode != 0
    assert "cockpit ->" not in completed.stdout
    invocations = docker_invocations(tmp_path)
    project = next(
        arguments[arguments.index("--project-name") + 1]
        for arguments in invocations
        if "build" in arguments
    )
    down = next(arguments for arguments in invocations if "down" in arguments)
    assert down[down.index("--project-name") + 1] == project
    assert down[-5:] == ["down", "--volumes", "--rmi", "local", "--remove-orphans"]
    assert not (installation_directory(tmp_path) / "installation.state").exists()
    assert json.loads((tmp_path / "docker-state.json").read_text()) == {}


@pytest.mark.parametrize(
    "settings",
    (
        {"ATELIER2_TEST_FAIL_DOWN": "1"},
        {"ATELIER2_TEST_PROJECT_LIST_FAILURE": "container"},
        {"ATELIER2_TEST_PROJECT_LIST_FAILURE": "volume"},
        {"ATELIER2_TEST_PROJECT_LIST_FAILURE": "network"},
    ),
    ids=(
        "teardown-command-failure",
        "container-inventory-failure",
        "volume-inventory-failure",
        "network-inventory-failure",
    ),
)
def test_failed_cleanup_keeps_durable_intent_for_recovery(
    tmp_path: Path, settings: dict[str, str]
) -> None:
    repository = lifecycle_repository(tmp_path)

    completed = run_live(
        repository,
        tmp_path,
        "install",
        ATELIER2_TEST_FAIL_UP="1",
        **settings,
    )

    assert completed.returncode == 42
    assert "cleanup is incomplete" in completed.stderr
    assert read_record(tmp_path)["state"] == "INSTALLING"
    status = run_live(repository, tmp_path, "status")
    assert status.stdout == "INCOMPLETE\n"
    if "ATELIER2_TEST_PROJECT_LIST_FAILURE" in settings:
        assert not any("down" in item for item in docker_invocations(tmp_path))


LIFECYCLE_SIGNAL_CASES = (
    ("install", "build", signal.SIGHUP, 129, False),
    ("install", "up", signal.SIGINT, 130, False),
    ("start", "start", signal.SIGHUP, 129, False),
    ("start", "health", signal.SIGINT, 130, False),
    ("start", "start", signal.SIGINT, 130, True),
)
LIFECYCLE_SIGNAL_CASE_IDS = (
    "install-build-sighup",
    "install-up-sigint",
    "start-start-sighup",
    "start-health-sigint",
    "start-start-cleanup-second-signal",
)


def _lifecycle_signal_cleans_only_the_exact_owned_runtime(
    tmp_path: Path,
    command: str,
    phase: str,
    interruption: signal.Signals,
    status: int,
    repeated: bool,
) -> None:
    if command == "install":
        repository, before_record = lifecycle_repository(tmp_path), b""
    else:
        repository, before_record = stopped_repository(tmp_path)
    wait_phases = f"{phase},start-cleanup" if repeated else phase
    environment = lifecycle_environment(tmp_path, ATELIER2_TEST_WAIT_PHASE=wait_phases)
    with started_process(
        _with_default_interruption_signals(
            ["bash", str(repository / "scripts/container_live.sh"), command]
        ),
        cwd=repository,
        env=environment,
    ) as process:
        ready = tmp_path / f"{phase}-ready"
        wait_until_exists(ready, process, f"stub did not reach {ready.name}")
        os.killpg(process.pid, interruption)
        if repeated:
            wait_until_exists(
                tmp_path / "start-cleanup-ready",
                process,
                "stub did not reach start-cleanup-ready",
            )
            os.killpg(process.pid, signal.SIGTERM)
            (tmp_path / "start-cleanup-release").touch()

        assert (
            wait_for_exit(
                process, tmp_path, "lifecycle process did not exit after the signal"
            )
            == status
        )
    mutations = docker_mutations(docker_invocations(tmp_path))
    if command == "start":
        assert mutations == RECORDED_START_STOP
        assert (
            installation_directory(tmp_path) / "installation.state"
        ).read_bytes() == before_record
        return
    project = mutations[0][mutations[0].index("--project-name") + 1]
    assert mutations[-1][-5:] == [
        "down",
        "--volumes",
        "--rmi",
        "local",
        "--remove-orphans",
    ]
    assert all(
        arguments[arguments.index("--project-name") + 1] == project
        for arguments in mutations
    )
    assert not (installation_directory(tmp_path) / "installation.state").exists()


@pytest.mark.parametrize(
    ("command", "phase", "interruption", "status", "repeated"),
    LIFECYCLE_SIGNAL_CASES,
    ids=LIFECYCLE_SIGNAL_CASE_IDS,
)
def test_lifecycle_signal_cleans_only_the_exact_owned_runtime(
    tmp_path: Path,
    command: str,
    phase: str,
    interruption: signal.Signals,
    status: int,
    repeated: bool,
) -> None:
    _lifecycle_signal_cleans_only_the_exact_owned_runtime(
        tmp_path, command, phase, interruption, status, repeated
    )


def test_started_process_terminates_its_group_when_its_body_aborts(
    tmp_path: Path,
) -> None:
    ready = tmp_path / "child-ready"
    process_group_id: int | None = None

    with (
        pytest.raises(RuntimeError, match="fixture aborted"),
        started_process(
            [
                "bash",
                "-c",
                (
                    '"$1" -c "from pathlib import Path; import sys, time; '
                    'Path(sys.argv[1]).touch(); time.sleep(60)" "$2"'
                ),
                "bash",
                sys.executable,
                str(ready),
            ],
            cwd=tmp_path,
            env=os.environ.copy(),
        ) as process,
    ):
        process_group_id = process.pid
        wait_until_exists(ready, process, "child process did not start")
        raise RuntimeError("fixture aborted")

    assert process_group_id is not None
    with pytest.raises(ProcessLookupError):
        os.killpg(process_group_id, 0)


@pytest.mark.parametrize(
    ("command", "phase", "interruption", "status", "repeated"),
    LIFECYCLE_SIGNAL_CASES,
    ids=LIFECYCLE_SIGNAL_CASE_IDS,
)
def test_lifecycle_signals_are_unmoved_by_parent_atelier2_variables(
    tmp_path: Path,
    command: str,
    phase: str,
    interruption: signal.Signals,
    status: int,
    repeated: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ATELIER2_DEPLOYMENT", "local")
    monkeypatch.setenv("ATELIER2_PUBLISHED_PORT", "8422")
    monkeypatch.setenv("ATELIER2_RESTART_POLICY", "unless-stopped")
    monkeypatch.setenv("ATELIER2_SOURCE_COMMIT", "a" * 40)
    monkeypatch.setenv("ATELIER2_SOURCE_TREE", "b" * 40)
    previous_hangup = signal.getsignal(signal.SIGHUP)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        _lifecycle_signal_cleans_only_the_exact_owned_runtime(
            tmp_path, command, phase, interruption, status, repeated
        )
    finally:
        signal.signal(signal.SIGHUP, previous_hangup)


@pytest.mark.parametrize(
    ("settings", "expected_state", "cleanup_incomplete"),
    (
        ({"ATELIER2_TEST_DRIFT": "health"}, "exited", False),
        ({"ATELIER2_TEST_FAIL_START": "1"}, "exited", False),
        (
            {"ATELIER2_TEST_FAIL_START": "1", "ATELIER2_TEST_FAIL_STOP": "1"},
            "running",
            True,
        ),
    ),
    ids=(
        "unhealthy-health-check",
        "ambiguous-start-failure",
        "ambiguous-start-and-cleanup-failure",
    ),
)
def test_failed_start_stops_the_exact_recorded_container_and_keeps_state(
    tmp_path: Path,
    settings: dict[str, str],
    expected_state: str,
    cleanup_incomplete: bool,
) -> None:
    repository, before_record = stopped_repository(tmp_path)

    completed = run_live(repository, tmp_path, "start", **settings)

    assert completed.returncode == 1
    assert docker_mutations(docker_invocations(tmp_path)) == RECORDED_START_STOP
    state = json.loads((tmp_path / "docker-state.json").read_text())["status"]
    assert state == expected_state
    assert (
        installation_directory(tmp_path) / "installation.state"
    ).read_bytes() == before_record
    assert ("cleanup is incomplete" in completed.stderr) is cleanup_incomplete


def test_uninstall_without_an_installation_is_idempotent(tmp_path: Path) -> None:
    repository = lifecycle_repository(tmp_path)

    completed = run_live(repository, tmp_path, "uninstall")

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "container live: nothing installed\n"
    assert not installation_directory(tmp_path).exists()
    assert docker_invocations(tmp_path) == []


def test_uninstall_removes_the_complete_installation(tmp_path: Path) -> None:
    repository = installed_repository(tmp_path)
    directory = installation_directory(tmp_path)
    (tmp_path / "docker-record.jsonl").write_text("", encoding="utf-8")

    completed = run_live(repository, tmp_path, "uninstall")

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "container live: uninstalled\n"
    assert not directory.exists()
    mutations = docker_mutations(docker_invocations(tmp_path))
    down = next(arguments for arguments in mutations if "down" in arguments)
    assert down[-5:] == ["down", "--volumes", "--rmi", "local", "--remove-orphans"]

    status = run_live(repository, tmp_path, "status")
    assert status.stdout == "INCOMPLETE\n"

    shutil.rmtree(tmp_path / "docker-context")
    reinstalled = run_live(
        repository, tmp_path, "install", ATELIER2_TEST_REQUIRE_INTENT="1"
    )
    assert reinstalled.returncode == 0, reinstalled.stderr


def test_uninstall_after_a_preserving_update_uses_the_compose_teardown_path(
    tmp_path: Path,
) -> None:
    # teardown_recorded_installation's ownership check must accept the
    # network's post-update identity (record[source_commit/tree], relabelled
    # by Compose on every update) exactly like verify_installed_configuration
    # does; otherwise it wrongly disowns a healthy install's network and
    # uninstall silently falls back to the coarse label-filtered
    # force-removal path instead of the clean `compose down`.
    repository = installed_repository(tmp_path)
    commit_a_second_change(repository)
    (tmp_path / "docker-record.jsonl").write_text("", encoding="utf-8")
    shutil.rmtree(tmp_path / "docker-context")
    update = run_live(repository, tmp_path, "update")
    assert update.returncode == 0, update.stderr
    (tmp_path / "docker-record.jsonl").write_text("", encoding="utf-8")

    completed = run_live(repository, tmp_path, "uninstall")

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "container live: uninstalled\n"
    mutations = docker_mutations(docker_invocations(tmp_path))
    down = next(arguments for arguments in mutations if "down" in arguments)
    assert down[-5:] == ["down", "--volumes", "--rmi", "local", "--remove-orphans"]
    assert not any(arguments[:2] == ["network", "rm"] for arguments in mutations)


def test_uninstall_removes_orphaned_docker_resources_without_a_record(
    tmp_path: Path,
) -> None:
    repository = installed_repository(tmp_path)
    directory = installation_directory(tmp_path)
    (directory / "installation.state").unlink()
    (directory / "compose.yaml").unlink()
    (tmp_path / "docker-record.jsonl").write_text("", encoding="utf-8")

    completed = run_live(repository, tmp_path, "uninstall")

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "container live: uninstalled\n"
    assert not directory.exists()
    invocations = docker_invocations(tmp_path)
    assert any(arguments[:1] == ["rm"] for arguments in invocations)
    assert any(arguments[:2] == ["network", "rm"] for arguments in invocations)
    assert any(arguments[:2] == ["volume", "rm"] for arguments in invocations)
    assert any(arguments[:1] == ["rmi"] for arguments in invocations)
    assert all("compose" not in arguments for arguments in invocations)

    shutil.rmtree(tmp_path / "docker-context")
    reinstalled = run_live(
        repository, tmp_path, "install", ATELIER2_TEST_REQUIRE_INTENT="1"
    )
    assert reinstalled.returncode == 0, reinstalled.stderr


def test_uninstall_leaves_a_foreign_docker_object_untouched(tmp_path: Path) -> None:
    repository = installed_repository(tmp_path)
    directory = installation_directory(tmp_path)
    (directory / "installation.state").unlink()
    (directory / "compose.yaml").unlink()
    (tmp_path / "docker-record.jsonl").write_text("", encoding="utf-8")

    completed = run_live(
        repository, tmp_path, "uninstall", ATELIER2_TEST_FOREIGN_LOCAL_RESOURCE="1"
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "container live: uninstalled\n"
    assert not directory.exists()
    invocations = docker_invocations(tmp_path)
    foreign_identities = {
        FOREIGN_CONTAINER_ID,
        FOREIGN_VOLUME_NAME,
        FOREIGN_NETWORK_NAME,
        FOREIGN_IMAGE_ID,
    }
    assert not any(set(arguments) & foreign_identities for arguments in invocations)
    assert any(arguments[:1] == ["rm"] for arguments in invocations)
    assert any(arguments[:2] == ["network", "rm"] for arguments in invocations)
    assert any(arguments[:2] == ["volume", "rm"] for arguments in invocations)
    assert any(arguments[:1] == ["rmi"] for arguments in invocations)

    shutil.rmtree(tmp_path / "docker-context")
    reinstalled = run_live(
        repository,
        tmp_path,
        "install",
        ATELIER2_TEST_REQUIRE_INTENT="1",
        ATELIER2_TEST_FOREIGN_LOCAL_RESOURCE="1",
    )
    assert reinstalled.returncode == 0, reinstalled.stderr


def test_double_uninstall_is_harmless(tmp_path: Path) -> None:
    repository = installed_repository(tmp_path)
    assert run_live(repository, tmp_path, "uninstall").returncode == 0
    (tmp_path / "docker-record.jsonl").write_text("", encoding="utf-8")

    second = run_live(repository, tmp_path, "uninstall")

    assert second.returncode == 0, second.stderr
    assert second.stdout == "container live: nothing installed\n"
    assert docker_invocations(tmp_path) == []
    assert not installation_directory(tmp_path).exists()


def commit_a_change(repository: Path, payload: str) -> None:
    (repository / "payload.txt").write_text(payload, encoding="utf-8")
    run_git(repository, "add", "--all")
    run_git(repository, "commit", "--quiet", "--message", f"{payload.strip()} change")


def commit_a_second_change(repository: Path) -> None:
    commit_a_change(repository, "second\n")


@pytest.mark.proves("an-update-migrates-the-preserved-store-and-starts-on-it")
def test_update_migrates_the_preserved_store_and_starts_on_it(
    tmp_path: Path,
) -> None:
    repository = installed_repository(tmp_path)
    before = read_record(tmp_path)
    commit_a_second_change(repository)
    (tmp_path / "docker-record.jsonl").write_text("", encoding="utf-8")
    shutil.rmtree(tmp_path / "docker-context")

    completed = run_live(repository, tmp_path, "update")

    assert completed.returncode == 0, completed.stderr
    assert "fingerprint" in completed.stdout
    assert "cockpit ->" in completed.stdout
    assert "container live: uninstalled" not in completed.stdout
    record = read_record(tmp_path)
    assert record["state"] == "INSTALLED"
    assert record["project"] == before["project"]
    assert record["volume_name"] == before["volume_name"]
    assert record["network_name"] == before["network_name"]
    assert record["store_source_commit"] == before["store_source_commit"]
    assert record["store_source_tree"] == before["store_source_tree"]
    assert record["source_commit"] == run_git(repository, "rev-parse", "HEAD")
    assert record["source_commit"] != before["source_commit"]

    mutations = docker_mutations(docker_invocations(tmp_path))
    stopped = mutations.index(["stop", "--time", "30", CONTAINER_ID])
    migrated = next(
        index for index, args in enumerate(mutations) if args[:1] == ["run"]
    )
    started_new = next(index for index, args in enumerate(mutations) if "up" in args)
    assert stopped < migrated < started_new
    removed_image = next(args for args in mutations if args[:1] == ["rmi"])
    assert removed_image[-1] == before["image_id"]


@pytest.mark.proves("an-update-migrates-the-preserved-store-and-starts-on-it")
def test_two_consecutive_preserving_updates_leave_status_running(
    tmp_path: Path,
) -> None:
    # Compose recreates the network on every update (labelling it with the
    # then-current commit, just like the container) while the volume is kept
    # untouched. A network-identity check pinned to the frozen store commit
    # (the volume's own identity) would therefore drift on the very next
    # update; verify_installed_configuration must judge the network by the
    # commit that is actually running, exactly like it judges the container.
    repository = installed_repository(tmp_path)

    commit_a_second_change(repository)
    (tmp_path / "docker-record.jsonl").write_text("", encoding="utf-8")
    shutil.rmtree(tmp_path / "docker-context")
    first_update = run_live(repository, tmp_path, "update")
    assert first_update.returncode == 0, first_update.stderr

    commit_a_change(repository, "third\n")
    (tmp_path / "docker-record.jsonl").write_text("", encoding="utf-8")
    shutil.rmtree(tmp_path / "docker-context")
    second_update = run_live(repository, tmp_path, "update")
    assert second_update.returncode == 0, second_update.stderr

    record = read_record(tmp_path)
    assert record["source_commit"] == run_git(repository, "rev-parse", "HEAD")
    assert record["source_commit"] != record["store_source_commit"]

    status = run_live(repository, tmp_path, "status")
    assert status.returncode == 0, status.stderr
    assert status.stdout.strip() == "RUNNING"


@pytest.mark.proves("a-refused-migration-restarts-the-previous-container-untouched")
def test_update_restarts_the_previous_container_on_a_refused_migration(
    tmp_path: Path,
) -> None:
    repository = installed_repository(tmp_path)
    before = read_record(tmp_path)
    before_record_bytes = (
        installation_directory(tmp_path) / "installation.state"
    ).read_bytes()
    commit_a_second_change(repository)
    (tmp_path / "docker-record.jsonl").write_text("", encoding="utf-8")
    shutil.rmtree(tmp_path / "docker-context")

    completed = run_live(repository, tmp_path, "update", ATELIER2_TEST_FAIL_MIGRATE="1")

    assert completed.returncode != 0
    assert "migration refused" in completed.stderr
    assert (
        installation_directory(tmp_path) / "installation.state"
    ).read_bytes() == before_record_bytes
    mutations = docker_mutations(docker_invocations(tmp_path))
    stopped = mutations.index(["stop", "--time", "30", CONTAINER_ID])
    assert mutations[stopped + 1] == [
        "run",
        "--rm",
        "--entrypoint",
        "atelier2",
        "--volume",
        f"{before['volume_name']}:/var/lib/atelier2/store",
        UPDATED_IMAGE_ID,
        "migrate",
        "--database",
        "/var/lib/atelier2/store/atelier.sqlite",
    ]
    assert mutations[-1] == ["start", CONTAINER_ID]
    assert (
        json.loads((tmp_path / "docker-state.json").read_text())["status"] == "running"
    )
    assert not any("up" in args for args in mutations)


@pytest.mark.proves(
    "an-unconfirmed-new-container-after-migration-gets-a-true-diagnosis"
)
def test_update_reports_the_true_state_when_the_new_container_is_unconfirmed(
    tmp_path: Path,
) -> None:
    repository = installed_repository(tmp_path)
    before_record_bytes = (
        installation_directory(tmp_path) / "installation.state"
    ).read_bytes()
    commit_a_second_change(repository)
    (tmp_path / "docker-record.jsonl").write_text("", encoding="utf-8")
    shutil.rmtree(tmp_path / "docker-context")

    completed = run_live(repository, tmp_path, "update", ATELIER2_TEST_FAIL_UP="1")

    assert completed.returncode != 0
    assert f"docker start {CONTAINER_ID}" not in completed.stderr
    assert "no longer exists to restart" in completed.stderr
    assert "container_live.sh status" in completed.stderr
    assert (
        installation_directory(tmp_path) / "installation.state"
    ).read_bytes() == before_record_bytes
    mutations = docker_mutations(docker_invocations(tmp_path))
    migrated = next(
        index for index, args in enumerate(mutations) if args[:1] == ["run"]
    )
    assert any("up" in args for args in mutations[migrated:])
    assert not any(args[:1] == ["start"] for args in mutations)


def interrupted_update_repository(tmp_path: Path) -> Path:
    """A healthy new container beside a record still naming the dead old one.

    Exactly the state an update crash between `compose up` and the record
    publish leaves behind: the drifted state reconcile exists to recover.
    """
    repository = installed_repository(tmp_path)
    commit_a_second_change(repository)
    shutil.rmtree(tmp_path / "docker-context")
    interrupted = run_live(repository, tmp_path, "update", ATELIER2_TEST_FAIL_UP="1")
    assert interrupted.returncode != 0
    (tmp_path / "docker-record.jsonl").write_text("", encoding="utf-8")
    return repository


@pytest.mark.proves("reconcile-adopts-the-proven-running-container-and-restores-update")
def test_reconcile_adopts_the_running_container_after_an_interrupted_update(
    tmp_path: Path,
) -> None:
    repository = interrupted_update_repository(tmp_path)
    before = read_record(tmp_path)
    assert run_live(repository, tmp_path, "status").stdout == "DRIFTED\n"
    refused_update = run_live(repository, tmp_path, "update")
    assert refused_update.returncode != 0
    assert "reconcile" in refused_update.stderr

    completed = run_live(repository, tmp_path, "reconcile")

    assert completed.returncode == 0, completed.stderr
    assert "reconciled to running container" in completed.stdout
    record = read_record(tmp_path)
    assert record["state"] == "INSTALLED"
    assert record["container_id"] == UPDATED_CONTAINER_ID
    assert record["image_id"] == UPDATED_IMAGE_ID
    assert record["source_commit"] == run_git(repository, "rev-parse", "HEAD")
    assert record["source_tree"] == run_git(repository, "rev-parse", "HEAD^{tree}")
    assert record["project"] == before["project"]
    assert record["volume_name"] == before["volume_name"]
    assert record["network_name"] == before["network_name"]
    assert record["store_source_commit"] == before["store_source_commit"]
    assert record["store_source_tree"] == before["store_source_tree"]
    assert docker_mutations(docker_invocations(tmp_path)) == []
    assert run_live(repository, tmp_path, "status").stdout == "RUNNING\n"


@pytest.mark.proves("reconcile-adopts-the-proven-running-container-and-restores-update")
def test_update_runs_store_preserving_after_reconcile(tmp_path: Path) -> None:
    repository = interrupted_update_repository(tmp_path)
    reconciled = run_live(repository, tmp_path, "reconcile")
    assert reconciled.returncode == 0, reconciled.stderr
    before = read_record(tmp_path)
    commit_a_change(repository, "third\n")
    (tmp_path / "docker-record.jsonl").write_text("", encoding="utf-8")
    shutil.rmtree(tmp_path / "docker-context")

    completed = run_live(repository, tmp_path, "update")

    assert completed.returncode == 0, completed.stderr
    assert "fingerprint" in completed.stdout
    assert "cockpit ->" in completed.stdout
    record = read_record(tmp_path)
    assert record["project"] == before["project"]
    assert record["volume_name"] == before["volume_name"]
    assert record["store_source_commit"] == before["store_source_commit"]
    assert record["source_commit"] == run_git(repository, "rev-parse", "HEAD")


def test_reconcile_on_an_exact_installation_changes_nothing(tmp_path: Path) -> None:
    repository = installed_repository(tmp_path)
    before_record = (
        installation_directory(tmp_path) / "installation.state"
    ).read_bytes()
    (tmp_path / "docker-record.jsonl").write_text("", encoding="utf-8")

    completed = run_live(repository, tmp_path, "reconcile")

    assert completed.returncode == 0, completed.stderr
    assert "nothing to reconcile" in completed.stdout
    assert (
        installation_directory(tmp_path) / "installation.state"
    ).read_bytes() == before_record
    assert docker_mutations(docker_invocations(tmp_path)) == []


@pytest.mark.proves("reconcile-refuses-by-name-instead-of-guessing")
@pytest.mark.parametrize(
    ("settings", "refusal"),
    (
        ({"ATELIER2_TEST_DRIFT": "label"}, "refusing to adopt"),
        ({"ATELIER2_TEST_DRIFT": "mount"}, "refusing to adopt"),
        ({"ATELIER2_TEST_DRIFT": "restart"}, "refusing to adopt"),
        ({"ATELIER2_TEST_DRIFT": "health"}, "not healthy"),
        ({"ATELIER2_TEST_INITIAL_ENGINE_ID": "different-engine"}, "engine identity"),
    ),
    ids=(
        "foreign-deployment-label",
        "wrong-store-mount",
        "drifted-restart-policy",
        "unhealthy-container",
        "different-docker-engine",
    ),
)
def test_reconcile_refuses_an_unprovable_container_and_changes_nothing(
    tmp_path: Path, settings: dict[str, str], refusal: str
) -> None:
    repository = interrupted_update_repository(tmp_path)
    before_record = (
        installation_directory(tmp_path) / "installation.state"
    ).read_bytes()

    completed = run_live(repository, tmp_path, "reconcile", **settings)

    assert completed.returncode != 0
    assert refusal in completed.stderr
    assert (
        installation_directory(tmp_path) / "installation.state"
    ).read_bytes() == before_record
    assert docker_mutations(docker_invocations(tmp_path)) == []


@pytest.mark.proves("reconcile-refuses-by-name-instead-of-guessing")
def test_reconcile_refuses_when_no_project_container_exists_to_adopt(
    tmp_path: Path,
) -> None:
    repository = interrupted_update_repository(tmp_path)
    before_record = (
        installation_directory(tmp_path) / "installation.state"
    ).read_bytes()
    state_path = tmp_path / "docker-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["container_removed"] = True
    state_path.write_text(json.dumps(state), encoding="utf-8")

    completed = run_live(repository, tmp_path, "reconcile")

    assert completed.returncode != 0
    assert "exactly one container" in completed.stderr
    assert (
        installation_directory(tmp_path) / "installation.state"
    ).read_bytes() == before_record
    assert docker_mutations(docker_invocations(tmp_path)) == []


@pytest.mark.proves("reconcile-refuses-by-name-instead-of-guessing")
def test_reconcile_refuses_an_incomplete_installation(tmp_path: Path) -> None:
    repository = lifecycle_repository(tmp_path)
    failed_install = run_live(
        repository,
        tmp_path,
        "install",
        ATELIER2_TEST_FAIL_UP="1",
        ATELIER2_TEST_FAIL_DOWN="1",
    )
    assert failed_install.returncode != 0
    assert read_record(tmp_path)["state"] == "INSTALLING"
    (tmp_path / "docker-record.jsonl").write_text("", encoding="utf-8")

    completed = run_live(repository, tmp_path, "reconcile")

    assert completed.returncode != 0
    assert "never completed" in completed.stderr
    assert read_record(tmp_path)["state"] == "INSTALLING"
    assert docker_invocations(tmp_path) == []


@pytest.mark.proves("reconcile-refuses-by-name-instead-of-guessing")
def test_reconcile_without_an_installation_refuses(tmp_path: Path) -> None:
    repository = lifecycle_repository(tmp_path)

    completed = run_live(repository, tmp_path, "reconcile")

    assert completed.returncode != 0
    assert "nothing installed" in completed.stderr
    assert docker_invocations(tmp_path) == []


@pytest.mark.proves("fresh-explicitly-names-the-store-it-discards")
def test_update_fresh_wipes_the_store_and_names_it(tmp_path: Path) -> None:
    repository = installed_repository(tmp_path)
    original_project = read_record(tmp_path)["project"]
    (tmp_path / "docker-record.jsonl").write_text("", encoding="utf-8")
    shutil.rmtree(tmp_path / "docker-context")

    completed = run_live(
        repository, tmp_path, "update", "--fresh", ATELIER2_TEST_REQUIRE_INTENT="1"
    )

    assert completed.returncode == 0, completed.stderr
    assert "container live: uninstalled" in completed.stdout
    assert "--fresh discards the previous store" in completed.stdout
    assert "cockpit ->" in completed.stdout
    record = read_record(tmp_path)
    assert record["state"] == "INSTALLED"
    assert record["project"] != original_project


def test_update_fresh_names_store_loss_only_when_a_volume_was_actually_removed(
    tmp_path: Path,
) -> None:
    repository = installed_repository(tmp_path)
    directory = installation_directory(tmp_path)
    (directory / "installation.state").unlink()
    (directory / "compose.yaml").unlink()
    state_path = tmp_path / "docker-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["volume_removed"] = True
    state_path.write_text(json.dumps(state), encoding="utf-8")
    (tmp_path / "docker-record.jsonl").write_text("", encoding="utf-8")
    shutil.rmtree(tmp_path / "docker-context")

    completed = run_live(
        repository, tmp_path, "update", "--fresh", ATELIER2_TEST_REQUIRE_INTENT="1"
    )

    assert completed.returncode == 0, completed.stderr
    assert "container live: uninstalled" in completed.stdout
    assert "discards the previous store" not in completed.stdout
    assert "cockpit ->" in completed.stdout


def test_update_from_no_installation_installs_fresh_with_nothing_to_preserve(
    tmp_path: Path,
) -> None:
    repository = lifecycle_repository(tmp_path)

    completed = run_live(
        repository, tmp_path, "update", ATELIER2_TEST_REQUIRE_INTENT="1"
    )

    assert completed.returncode == 0, completed.stderr
    assert "discards the previous store" not in completed.stdout
    assert "cockpit ->" in completed.stdout
    assert read_record(tmp_path)["state"] == "INSTALLED"


def test_update_fresh_from_no_installation_installs_fresh_without_a_store_loss_note(
    tmp_path: Path,
) -> None:
    repository = lifecycle_repository(tmp_path)

    completed = run_live(
        repository, tmp_path, "update", "--fresh", ATELIER2_TEST_REQUIRE_INTENT="1"
    )

    assert completed.returncode == 0, completed.stderr
    assert "container live: nothing installed" in completed.stdout
    assert "discards the previous store" not in completed.stdout
    assert "cockpit ->" in completed.stdout
    assert read_record(tmp_path)["state"] == "INSTALLED"


def test_update_refuses_ambient_mode_before_touching_a_good_installation(
    tmp_path: Path,
) -> None:
    repository = installed_repository(tmp_path)
    before_record = (
        installation_directory(tmp_path) / "installation.state"
    ).read_bytes()
    (tmp_path / "docker-record.jsonl").write_text("", encoding="utf-8")

    completed = run_live(
        repository, tmp_path, "update", ATELIER2_DEPLOYMENT="disposable"
    )

    assert completed.returncode != 0
    assert docker_invocations(tmp_path) == []
    assert (
        installation_directory(tmp_path) / "installation.state"
    ).read_bytes() == before_record


def test_update_rejects_unrecognized_extra_arguments(tmp_path: Path) -> None:
    repository = installed_repository(tmp_path)
    (tmp_path / "docker-record.jsonl").write_text("", encoding="utf-8")

    completed = run_live(repository, tmp_path, "update", "--unknown")

    assert completed.returncode != 0
    assert "optional --fresh flag" in completed.stderr
    assert docker_invocations(tmp_path) == []


@pytest.mark.parametrize(
    "command", ("install", "status", "stop", "start", "uninstall", "reconcile")
)
def test_single_argument_commands_reject_extra_arguments(
    tmp_path: Path, command: str
) -> None:
    repository = lifecycle_repository(tmp_path)

    completed = run_live(repository, tmp_path, command, "extra")

    assert completed.returncode != 0
    assert docker_invocations(tmp_path) == []


LIFECYCLE_GUARDS = (
    "flock --nonblock 9",
    '[state]="INSTALLING"',
    "sync -f",
    'mv -f -- "${temporary_record}" "${record_file}"',
    '[[ -f "${path}" && ! -L "${path}" ]]',
    "((${#record[@]} == ${#required[@]}))",
    'resources="$(docker ps --all --quiet',
    '[[ -z "${temporary_descriptor}" ]] || rm -f -- "${temporary_descriptor}"',
    'docker stop --time 30 "${record[container_id]}"',
    'docker start "${record[container_id]}"',
    'rm -rf -- "${installation_directory}"',
    'docker rm --force -- "${resource}"',
    'uninstalled_existing_store="${volume_removed}"',
    'docker stop --time 30 "${update_old_container_id}"',
    'docker start "${update_old_container_id}"',
    'fail "store migration refused',
    '[[ "${candidate}" =~ ^[0-9a-f]{64}$ ]]',
    'fail "the running container does not match',
)


def assert_lifecycle_guards(script: str) -> None:
    for required in LIFECYCLE_GUARDS:
        assert required in script


def test_lifecycle_guard_mutations_bite_the_contract() -> None:
    script = CONTAINER_LIVE.read_text(encoding="utf-8")
    assert_lifecycle_guards(script)
    for required in LIFECYCLE_GUARDS:
        with pytest.raises(AssertionError):
            assert_lifecycle_guards(script.replace(required, "removed"))


def test_live_script_has_no_broad_or_deferred_lifecycle_authority() -> None:
    # #564 consumed the offline migration ladder from `update` itself, so
    # "migrate" is no longer forbidden here -- it names an owned command.
    script = CONTAINER_LIVE.read_text(encoding="utf-8").lower()
    for forbidden in (
        "prune",
        "systemctl --user start",
        "systemctl --user stop",
        "docker restart",
        "update_container",
        "rollback",
        "retire",
        "provider",
        "runner",
        "songmaker",
    ):
        assert forbidden not in script
