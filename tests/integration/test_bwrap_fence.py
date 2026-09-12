"""What the fence really does to a real child on this host.

Every proof here starts a process. A developer machine that cannot open a user
namespace at all skips them, because a deployment there is refused at serve
start rather than served unfenced, and there is nothing left to observe. The
pipeline gives its runner the enforcer these proofs need, so the same silence
there is a failure: it would report a containment seam nobody observed.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

from atelier2.adapters import agent_processes as process_module
from atelier2.adapters import bwrap_sandbox
from atelier2.adapters import process_containment as containment
from atelier2.adapters.bwrap_sandbox import (
    SYSTEM_READ_ONLY_ROOTS,
    entered_fence,
    toolchain_sandbox,
    verified_sandbox_host,
)
from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.adapters.grok_capability import (
    CONFORMANT_GROK_VERSIONS,
    verify_grok_capability,
)
from atelier2.adapters.grok_subscription import (
    GrokExecutableUnsupported,
    attest_grok_workspace_tool_invocation,
)
from atelier2.contracts.agent_attempts import AgentAttemptCancellationDisposition
from atelier2.contracts.executions import AgentAttemptExecution
from atelier2.ports.agent_executions import (
    AgentProcessCompletion,
    AgentProcessInvocation,
    AgentSession,
)
from tests.integration.test_agent_attempts import attempt_request, attempt_runtime
from tests.integration.test_agent_process_supervisor import cancel_and_release
from tests.integration.test_grok_subscription import (
    INTROSPECTING_GROK,
    grok_subscription_deployment,
)
from tests.scenarios.agents import (
    HOST_ENFORCER,
    NOTHING_IS_PERMITTED,
    UNFENCEABLE,
    agent_attempt_execution,
    process_invocation,
)

INTERPRETER = Path("/usr/bin/python3")
"""The fake toolchain every proof here runs: the system interpreter.

The suite's own interpreter lives outside every granted root, and a fenced
child reaches nothing that is not granted -- which is the whole point, so the
fake toolchain is one that stands where a real one does.
"""

pytestmark = pytest.mark.skipif(
    UNFENCEABLE is not None, reason=f"this machine fences nothing: {UNFENCEABLE}"
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
import fcntl, json, os
held = []
for name in sorted(int(entry) for entry in os.listdir("/proc/self/fd")):
    try:
        fcntl.fcntl(name, fcntl.F_GETFD)
    except OSError:
        continue
    held.append(name)
climbed = {}
for descriptor in held:
    try:
        parent = os.open("..", os.O_RDONLY | os.O_DIRECTORY, dir_fd=descriptor)
    except OSError:
        continue
    climbed[descriptor] = sorted(os.listdir(parent))
    os.close(parent)
os.write(1, json.dumps({"held": held, "climbed": climbed}).encode())
"""
"""What a child still holds, and what it can walk to with it.

Nothing is closed before the count: a probe that dropped what it does not
expect would report its own expectation. The listing's own descriptor is the
one exception, and it is not filtered by number but by being already closed by
the time it is asked about.
"""

_ANSWERED_THE_ASKING = "asked, and given the time to answer"
_WORK_ONE_ANSWER_TAKES_SECONDS = 1.0
"""How long the payload below works after it is asked to end.

Ending is not instant for a real provider: it has a candidate to write, a
process to reap, a last frame to send. This is the smallest span that tells a
payload which finished its answer apart from one the kernel took mid-sentence.
"""

_ENDS_ON_TERM = f"""
import signal, time
def note(number, frame):
    time.sleep({_WORK_ONE_ANSWER_TAKES_SECONDS})
    open("ended-on-term", "w").write({_ANSWERED_THE_ASKING!r})
    raise SystemExit(0)
signal.signal(signal.SIGTERM, note)
open("started", "w").write("up")
time.sleep(120)
"""

CERTIFICATE_DIRECTORY = Path("/etc/ssl")
"""Where this host keeps both halves: the public store any client verifies a
certificate with, and the keys its own servers were issued."""

_LISTS_THE_CERTIFICATE_DIRECTORY = f"""
import json, os
os.write(1, json.dumps(sorted(os.listdir({str(CERTIFICATE_DIRECTORY)!r}))).encode())
"""

_ECHOES_ITS_LAST_ARGUMENT = """
import os, sys
os.write(1, sys.argv[-1].encode())
"""

_WAITS_UNTIL_ASKED = (str(INTERPRETER), "-c", "import time; time.sleep(300)")


def _wait_for(evidence: Path, seconds: float = 20.0) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if evidence.exists():
            return
        time.sleep(0.05)
    raise AssertionError(f"the fenced child never wrote {evidence}")


@dataclass(frozen=True)
class _FencedAttempt:
    """One claimed attempt and the fenced launch it is ready to run."""

    supervisor: AgentSession
    store: DbosAgentAttemptStore
    execution: AgentAttemptExecution
    invocation: AgentProcessInvocation
    workspace: Path


@contextmanager
def _fenced_attempt(
    tmp_path: Path,
    name: str,
    provider: tuple[str, ...],
    environment: tuple[tuple[str, str], ...] = (),
) -> Iterator[_FencedAttempt]:
    """One command, ready to run through the whole supervised launch behind its
    grant: the durable runtime lives for the body and is closed after it."""

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
        yield _FencedAttempt(
            supervisor,
            store,
            execution,
            process_invocation(
                execution.attempt_id,
                provider,
                workspace,
                environment,
                sandbox=toolchain_sandbox(INTERPRETER, HOST_ENFORCER, state_directory),
            ),
            workspace,
        )
    finally:
        runtime.close()


def _fenced_completion(
    tmp_path: Path,
    name: str,
    provider: tuple[str, ...],
    environment: tuple[tuple[str, str], ...] = (),
) -> tuple[AgentProcessCompletion, Path]:
    """Run one command to its end through the whole supervised launch."""

    with _fenced_attempt(tmp_path, name, provider, environment) as attempt:
        return (
            attempt.supervisor.launch_and_wait(
                attempt.execution, attempt.invocation, NOTHING_IS_PERMITTED
            ),
            attempt.workspace,
        )


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
        (
            ("HOME", str(tmp_path / "private-home")),
            ("PATH", os.environ.get("PATH", "/usr/bin")),
        ),
    )

    report = json.loads(completion.standard_output)
    assert report["workspace"] == "written"
    assert (workspace / "candidate.txt").read_text(encoding="utf-8") == "landed"
    assert report["keys"] == "FileNotFoundError"
    assert report["store"] == "FileNotFoundError"
    assert report["read_only_grant"] == "OSError"


def test_a_fenced_child_sees_public_certificate_material_and_no_private_key(
    tmp_path: Path,
) -> None:
    """The same directory a client reads to trust a certificate holds the keys
    this account's own servers were issued, so the grant names the public half
    of it file by file and the private half has no name in the namespace."""

    completion, _ = _fenced_completion(
        tmp_path,
        "fence/certificates",
        (str(INTERPRETER), "-c", _LISTS_THE_CERTIFICATE_DIRECTORY),
    )

    granted = toolchain_sandbox(
        INTERPRETER, HOST_ENFORCER, tmp_path
    ).grants.readable_and_executable
    seen = json.loads(completion.standard_output)
    assert seen == sorted(
        path.name for path in granted if path.parent == CERTIFICATE_DIRECTORY
    )
    assert "private" not in seen


def test_a_fenced_child_carries_no_variable_of_the_server_that_started_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The command's environment is the child's whole environment."""

    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "the-server-own-venv"))

    completion, _ = _fenced_completion(
        tmp_path,
        "fence/environment",
        (str(INTERPRETER), "-c", _REPORTS_WHAT_IT_REACHES, "{}", str(INTERPRETER)),
        (
            ("HOME", str(tmp_path / "private-home")),
            ("PATH", os.environ.get("PATH", "/usr/bin")),
        ),
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


def test_an_executable_that_answers_its_version_and_cannot_spawn_is_refused(
    tmp_path: Path,
) -> None:
    """The gap the Grok attestation exists for: a version answer is not startability.

    The refusal quotes whatever the failed start said, and inside the fence
    that sentence is the enforcer's: it is the process that got as far as
    trying to run this executable.
    """

    settings = grok_subscription_deployment(tmp_path, INTROSPECTING_GROK)
    assert verify_grok_capability(settings.executable) in CONFORMANT_GROK_VERSIONS
    settings.executable.write_text(
        "#!/atelier2/no/such/interpreter\n", encoding="utf-8"
    )
    settings.executable.chmod(0o755)

    with pytest.raises(GrokExecutableUnsupported, match="No such file or directory"):
        attest_grok_workspace_tool_invocation(settings)


def test_a_host_keeping_its_temporary_files_inside_a_grant_still_attests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Where this host puts a temporary directory is not the probe's to assume.

    Staged as a host does it: the temporary root this process resolved stands
    inside a granted system root -- `/usr/local/atelier-tmp` is the real shape
    of it -- so a probe laying its markers where that root points would have
    both read behind a true fence, and would refuse the working enforcer this
    machine carries. The markers stand under the directory the fence covers
    with a filesystem of its own instead, so the answer is the enforcer's and
    not the host's temporary layout.
    """

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(
        bwrap_sandbox, "SYSTEM_READ_ONLY_ROOTS", (*SYSTEM_READ_ONLY_ROOTS, tmp_path)
    )

    verified_sandbox_host(HOST_ENFORCER)


def test_a_fenced_child_holds_no_descriptor_of_this_host(tmp_path: Path) -> None:
    """The door a grant cannot close: a directory descriptor answers `..`.

    The enforcer closes the one it binds the workspace from, and a start passes
    it nothing else, so the child keeps its three standard streams and holds
    nothing it could walk the host filesystem with.
    """

    completion, _ = _fenced_completion(
        tmp_path,
        "fence/descriptors",
        (str(INTERPRETER), "-c", _REPORTS_ITS_DESCRIPTORS),
    )

    report = json.loads(completion.standard_output)
    assert report["held"] == [0, 1, 2]
    assert report["climbed"] == {}


def test_a_descriptor_passed_beside_the_grant_is_a_door_this_probe_sees(
    tmp_path: Path,
) -> None:
    """The control under the proof above: this is what a leak looks like.

    One directory descriptor handed over beside the one the enforcer consumes
    survives into the child and lists the host directory above it -- so a probe
    that reported nothing would be reporting its own blindness.
    """

    outside = tmp_path / "outside-every-grant"
    outside.mkdir()
    (outside / "the-operators-own-file").touch()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    standing = workspace.stat()
    leaked = os.open(outside, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.set_inheritable(leaked, True)
        with entered_fence(
            (str(INTERPRETER), "-c", _REPORTS_ITS_DESCRIPTORS),
            workspace,
            standing.st_dev,
            standing.st_ino,
            toolchain_sandbox(INTERPRETER, HOST_ENFORCER, tmp_path),
        ) as (arguments, entered, inherited):
            answered = subprocess.run(
                arguments,
                capture_output=True,
                cwd=entered,
                pass_fds=(*inherited, leaked),
                check=True,
            )
    finally:
        os.close(leaked)

    report = json.loads(answered.stdout)
    assert leaked in report["held"]
    assert report["climbed"][str(leaked)] == sorted(
        entry.name for entry in tmp_path.iterdir()
    )


def test_a_fenced_payload_is_asked_to_end_and_keeps_the_time_to_answer(
    tmp_path: Path,
) -> None:
    """The grace supervision promises has to reach the payload, not the fence.

    Measured on bubblewrap 0.9.0: the enforcer stands in its child's process
    group, and it is the first process of the namespace it opened -- so a
    group signal ends the enforcer, the namespace goes with it, and the kernel
    kills the payload mid-answer, evidence unwritten. Asked through the cgroup
    with the enforcer left standing, the same payload finishes what it was
    writing, and the fence ends by itself once nothing is left inside it.
    """

    with _fenced_attempt(
        tmp_path, "fence/grace", (str(INTERPRETER), "-c", _ENDS_ON_TERM)
    ) as attempt:
        launched = threading.Thread(
            target=lambda: attempt.supervisor.launch_and_wait(
                attempt.execution, attempt.invocation, NOTHING_IS_PERMITTED
            )
        )
        launched.start()
        try:
            _wait_for(attempt.workspace / "started")
            disposition = cancel_and_release(
                attempt.store, attempt.supervisor, attempt.execution.attempt_id
            )
        finally:
            launched.join(timeout=30)

        assert disposition is AgentAttemptCancellationDisposition.REAPED_AFTER_TERM
        assert (attempt.workspace / "ended-on-term").read_text(
            encoding="utf-8"
        ) == _ANSWERED_THE_ASKING


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
        toolchain_sandbox(INTERPRETER, HOST_ENFORCER, tmp_path),
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


def test_a_listed_number_that_names_an_outsider_is_not_asked_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ending a launch reaches what it holds, never a stranger's process.

    A member that exits and is reaped frees its number for any process of this
    user on this host, so a listing is a snapshot of numbers, not of processes.
    Staged with a listing that names one real member and one process which
    never entered the cgroup: the member ends, the stranger keeps running.
    """

    cgroup = process_module.delegated_cgroup_root() / f"atelier2-reuse-{os.getpid()}"
    cgroup.mkdir()
    joining = ("/bin/sh", "-c", f'echo $$ > "{cgroup}/cgroup.procs"; exec "$@"', "sh")
    enforcer = subprocess.Popen((*joining, *_WAITS_UNTIL_ASKED))
    member = subprocess.Popen((*joining, *_WAITS_UNTIL_ASKED))
    outsider = subprocess.Popen(_WAITS_UNTIL_ASKED)
    try:
        _wait_for_members(cgroup, (enforcer.pid, member.pid))
        monkeypatch.setattr(
            containment, "cgroup_members", lambda _cgroup: (member.pid, outsider.pid)
        )

        containment.ask_provider_to_end(member, cgroup, enforcer.pid, signal.SIGTERM)

        assert member.wait(timeout=10) == -signal.SIGTERM
        assert outsider.poll() is None
    finally:
        for started in (enforcer, member, outsider):
            if started.poll() is None:
                started.kill()
            started.wait(timeout=10)
        process_module._kill_cgroup_and_wait_empty(cgroup, 10.0)
        cgroup.rmdir()


def _wait_for_members(cgroup: Path, expected: tuple[int, ...]) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        held = (cgroup / "cgroup.procs").read_text(encoding="ascii").split()
        if sorted(held) == sorted(str(pid) for pid in expected):
            return
        time.sleep(0.05)
    raise AssertionError(f"{expected} never stood in {cgroup}")
