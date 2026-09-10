"""What the fence really does to a real child on this host.

Every proof here starts a process. They are skipped where this machine cannot
open a user namespace at all, because a deployment there is refused at serve
start rather than served unfenced, and there is nothing left to observe.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from atelier2.adapters import agent_processes as process_module
from atelier2.adapters.bwrap_sandbox import (
    entered_fence,
    toolchain_sandbox,
)
from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.contracts.sandbox_grants import SandboxUnavailable
from atelier2.ports.agent_executions import AgentProcessCompletion
from tests.integration.test_agent_attempts import attempt_request, attempt_runtime
from tests.scenarios.agents import (
    NOTHING_IS_PERMITTED,
    agent_attempt_execution,
    process_invocation,
)

INTERPRETER = Path("/usr/bin/python3")
"""The fake toolchain every proof here runs: the system interpreter.

The suite's own interpreter lives outside every granted root, and a fenced
child reaches nothing that is not granted -- which is the whole point, so the
fake toolchain is one that stands where a real one does.
"""

SEARCH_PATH = os.environ.get("PATH", "/usr/bin")
"""Where a deployment finds its enforcer. It is not where a child may read:
this grant names its roots itself."""

_UNFENCEABLE = None
try:
    toolchain_sandbox(INTERPRETER, SEARCH_PATH, Path.cwd())
except SandboxUnavailable as refusal:
    _UNFENCEABLE = str(refusal)
pytestmark = pytest.mark.skipif(
    _UNFENCEABLE is not None, reason=f"this machine fences nothing: {_UNFENCEABLE}"
)

_WRITES_TWO_VALUES_AND_FAILS = """
import os
os.write(1, b'{"first":1}{"second":[2,3]}')
raise SystemExit(7)
"""

_REPORTS_WHAT_IT_REACHES = """
import json, os, sys
report = {}
try:
    with open("candidate.txt", "w") as handle:
        handle.write("landed")
    report["workspace"] = "written"
except OSError as error:
    report["workspace"] = type(error).__name__
for name, path in json.loads(sys.argv[1]).items():
    try:
        with open(path, "rb") as handle:
            report[name] = handle.read().decode()
    except OSError as error:
        report[name] = type(error).__name__
try:
    with open(sys.argv[2], "w") as handle:
        handle.write("landed")
    report["read_only_grant"] = "written"
except OSError as error:
    report["read_only_grant"] = type(error).__name__
report["environment"] = sorted(os.environ)
os.write(1, json.dumps(report).encode())
"""

_SPAWNS_A_CHILD_AND_WAITS = """
import subprocess, sys, time
subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
time.sleep(300)
"""

_REPORTS_ITS_DESCRIPTORS = """
import json, os
descriptors = sorted(os.listdir("/proc/self/fd"))
climbed = []
for name in descriptors:
    try:
        held = os.open(name, os.O_RDONLY | os.O_DIRECTORY, dir_fd=int(name))
    except OSError:
        continue
    os.close(held)
    climbed.append(name)
kept = [name for name in descriptors if int(name) < 3]
os.write(1, json.dumps({"descriptors": kept, "climbed": climbed}).encode())
"""

_ENDS_ON_TERM = """
import signal, time
def note(number, frame):
    open("ended-on-term", "w").write("asked")
    raise SystemExit(0)
signal.signal(signal.SIGTERM, note)
open("started", "w").write("up")
time.sleep(120)
"""

_ECHOES_ITS_LAST_ARGUMENT = """
import os, sys
os.write(1, sys.argv[-1].encode())
"""


def _wait_for(evidence: Path, seconds: float = 20.0) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if evidence.exists():
            return
        time.sleep(0.05)
    raise AssertionError(f"the fenced child never wrote {evidence}")


def _fenced_completion(
    tmp_path: Path,
    name: str,
    provider: tuple[str, ...],
    environment: tuple[tuple[str, str], ...] = (),
) -> tuple[AgentProcessCompletion, Path]:
    """Run one command through the whole supervised launch, behind its grant."""

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_directory = tmp_path / "private-home"
    state_directory.mkdir()
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        execution = agent_attempt_execution(attempt_request(runtime, name))
        store = DbosAgentAttemptStore(
            runtime.engine, runtime.settings.application_version
        )
        supervisor = runtime.agent_process_supervisor
        store.prepare(execution)
        supervisor.prepare(execution)
        store.claim(execution)
        invocation = process_invocation(
            execution.attempt_id,
            provider,
            workspace,
            environment,
            sandbox=toolchain_sandbox(INTERPRETER, SEARCH_PATH, state_directory),
        )
        return (
            supervisor.launch_and_wait(execution, invocation, NOTHING_IS_PERMITTED),
            workspace,
        )
    finally:
        runtime.close()


def test_a_fenced_child_hands_back_its_exit_code_and_every_byte_it_wrote(
    tmp_path: Path,
) -> None:
    """The fence is not a filter: supervision reads the child, not the enforcer."""

    completion, _ = _fenced_completion(
        tmp_path,
        "fence/passthrough",
        (str(INTERPRETER), "-c", _WRITES_TWO_VALUES_AND_FAILS),
    )

    assert completion.return_code == 7
    assert completion.standard_output == b'{"first":1}{"second":[2,3]}'


def test_a_fenced_child_writes_its_leased_workspace_and_reaches_nothing_else(
    tmp_path: Path,
) -> None:
    """The negative proof: the same run that lands a candidate cannot read the
    account's keys or its live store, and cannot write the system files it
    reads."""

    keys = tmp_path / "home" / ".ssh"
    keys.mkdir(parents=True)
    (keys / "id_ed25519").write_text("the operator's own key", encoding="utf-8")
    store = tmp_path / "live-store"
    store.mkdir()
    (store / "atelier2.sqlite").write_text("every run ever made", encoding="utf-8")
    reachable = json.dumps(
        {"keys": str(keys / "id_ed25519"), "store": str(store / "atelier2.sqlite")}
    )

    completion, workspace = _fenced_completion(
        tmp_path,
        "fence/negative",
        (str(INTERPRETER), "-c", _REPORTS_WHAT_IT_REACHES, reachable, str(INTERPRETER)),
        (("HOME", str(tmp_path / "private-home")), ("PATH", SEARCH_PATH)),
    )

    report = json.loads(completion.standard_output)
    assert report["workspace"] == "written"
    assert (workspace / "candidate.txt").read_text(encoding="utf-8") == "landed"
    assert report["keys"] == "FileNotFoundError"
    assert report["store"] == "FileNotFoundError"
    assert report["read_only_grant"] == "OSError"


def test_a_fenced_child_carries_no_variable_of_the_server_that_started_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The command's environment is the child's whole environment."""

    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "the-server-own-venv"))

    completion, _ = _fenced_completion(
        tmp_path,
        "fence/environment",
        (str(INTERPRETER), "-c", _REPORTS_WHAT_IT_REACHES, "{}", str(INTERPRETER)),
        (("HOME", str(tmp_path / "private-home")), ("PATH", SEARCH_PATH)),
    )

    carried = set(json.loads(completion.standard_output)["environment"])
    assert "VIRTUAL_ENV" not in carried
    # Beside the two declared names a start adds only what it says about
    # itself: the enforcer publishes the directory it entered, and CPython
    # names the locale it coerced.
    assert carried <= {"HOME", "PATH", "PWD", "LC_CTYPE"}


def test_a_prompt_of_shell_metacharacters_is_carried_and_never_run(
    tmp_path: Path,
) -> None:
    """A fenced start is still an argument vector: nothing parses the prompt."""

    marker = tmp_path / "pwned"
    prompt = f"'; touch {marker}; #"

    completion, _ = _fenced_completion(
        tmp_path,
        "fence/metacharacters",
        (str(INTERPRETER), "-c", _ECHOES_ITS_LAST_ARGUMENT, prompt),
    )

    assert completion.standard_output.decode("utf-8") == prompt
    assert not marker.exists()


def test_a_fenced_child_holds_no_descriptor_of_this_host(tmp_path: Path) -> None:
    """The door a grant cannot close: a directory descriptor answers `..`.

    The enforcer closes the one it binds the workspace from, and a start passes
    it nothing else, so the child keeps its three standard streams and no way
    to walk the host filesystem past every grant.
    """

    completion, _ = _fenced_completion(
        tmp_path,
        "fence/descriptors",
        (str(INTERPRETER), "-c", _REPORTS_ITS_DESCRIPTORS),
    )

    report = json.loads(completion.standard_output)
    assert report["descriptors"] == ["0", "1", "2"]
    assert report["climbed"] == []


def test_a_fenced_child_is_still_asked_to_end_before_it_is_killed(
    tmp_path: Path,
) -> None:
    """Measured under the fence: the enforcer is the child's process group, not
    a wall around it, so the group signal supervision sends still reaches the
    payload and it ends its own way."""

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    standing = workspace.stat()
    with entered_fence(
        (str(INTERPRETER), "-c", _ENDS_ON_TERM),
        workspace,
        standing.st_dev,
        standing.st_ino,
        toolchain_sandbox(INTERPRETER, SEARCH_PATH, tmp_path),
    ) as (arguments, entered, inherited):
        process = subprocess.Popen(
            arguments, cwd=entered, pass_fds=inherited, start_new_session=True
        )
    try:
        _wait_for(workspace / "started")
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=20)

        # The enforcer stands in the child's own process group, so the same
        # signal ends it too and its exit is what a launcher reads back; what
        # this proves is that the payload was asked first and answered.
        assert (workspace / "ended-on-term").read_text(encoding="utf-8") == "asked"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)


def test_the_kill_cgroup_ends_the_enforcer_and_the_child_inside_it(
    tmp_path: Path,
) -> None:
    """Containment the fence must not escape: one write ends the whole tree."""

    cgroup = process_module.delegated_cgroup_root() / f"atelier2-fence-{os.getpid()}"
    cgroup.mkdir()
    standing = tmp_path.stat()
    joining = ("/bin/sh", "-c", f'echo $$ > "{cgroup}/cgroup.procs"; exec "$@"', "sh")
    with entered_fence(
        (str(INTERPRETER), "-c", _SPAWNS_A_CHILD_AND_WAITS),
        tmp_path,
        standing.st_dev,
        standing.st_ino,
        toolchain_sandbox(INTERPRETER, SEARCH_PATH, tmp_path),
    ) as (fenced, entered, inherited):
        process = subprocess.Popen((*joining, *fenced), cwd=entered, pass_fds=inherited)
    try:
        deadline = time.monotonic() + 20
        held: list[str] = []
        while time.monotonic() < deadline and len(held) < 3:
            held = (cgroup / "cgroup.procs").read_text(encoding="ascii").split()
            time.sleep(0.05)

        assert len(held) >= 3, held
        assert str(process.pid) in held

        process_module._kill_cgroup_and_wait_empty(cgroup, 10.0)

        assert (cgroup / "cgroup.procs").read_text(encoding="ascii").split() == []
        assert process.wait(timeout=10) != 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        cgroup.rmdir()
