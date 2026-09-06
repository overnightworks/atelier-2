"""No new invented fixed value enters the operator-relevant families unnamed.

The audit on #251 classified every literal in ``src/`` into real knobs, owned
constants, and buried defaults, and the operator ruling drew the line: a value
with real variance reaches the configuration channel, everything else lives at
exactly one named place with a stated owner. This gate keeps that classification
true after the audit: a *newly* spelled bare numeric literal whose name falls
into the operator-relevant families -- patience, retry, delay, backoff, page
size, read budget -- turns red until it either reaches the channel or is
registered below with the owner reason it may stay.

The gate reads source text on purpose, like the bound-ownership suite next
door: an invented value is a property of the source, not of the running object.
It covers the three spellings such a value enters through -- a constant
assignment, a parameter default, and a keyword argument. Named boundaries, kept
deliberately outside: positional call literals, one-off count bounds outside
the families (for example ``_MAXIMUM_HELD_WORKSPACE_LEVELS``), and attribute
assignments, which initialize running state rather than decide a value.
"""

from __future__ import annotations

import ast
import shutil
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from re import IGNORECASE
from re import compile as compile_pattern

import pytest

SOURCE_ROOT = Path(__file__).parents[2] / "src" / "atelier2"

OPERATOR_VALUE_FAMILIES = compile_pattern(
    r"(seconds|milliseconds|timeout|retries|retry|attempts"
    r"|delay|multiplier|backoff|page_size|queries)$",
    IGNORECASE,
)


@dataclass(frozen=True)
class SpelledValue:
    """One name the source may still spell as a bare literal, and why."""

    occurrences: int
    reason: str


VALUES_THE_SOURCE_MAY_STILL_SPELL: dict[str, SpelledValue] = {
    "host/serving.py::SERVE_SHUTDOWN_CONNECTION_GRACE_SECONDS": SpelledValue(
        1,
        "stable slice invariant: the grace a redeploy stop gives the "
        "never-ending SSE events stream before uvicorn drops its sockets, "
        "kept well under the live unit's TimeoutStopSec (documented in "
        "OPERATIONS.md) so runtime.close() still runs before SIGKILL -- an "
        "operator's stop cadence, never a per-request patience (#1117)",
    ),
    "host/serving.py::EVENT_PAGE_SIZE": SpelledValue(
        1, "the channel's named default, feeding --event-page-size into ApiLimits"
    ),
    "host/serving.py::MAXIMUM_CONTROL_QUERIES": SpelledValue(
        1, "the channel's named default, feeding --maximum-control-queries"
    ),
    "host/serving.py::MAXIMUM_EVENT_POLL_QUERIES": SpelledValue(
        1, "the channel's named default, feeding --maximum-event-poll-queries"
    ),
    "host/serving.py::MAXIMUM_QUERY_ADMISSION_WAIT_MILLISECONDS": SpelledValue(
        1, "the channel's named default, feeding --query-admission-wait-milliseconds"
    ),
    "host/serving.py::INITIAL_EVENT_POLL_DELAY_SECONDS": SpelledValue(
        1, "the channel's named default, feeding --initial-event-poll-delay-seconds"
    ),
    "host/serving.py::MAXIMUM_EVENT_POLL_DELAY_SECONDS": SpelledValue(
        1, "the channel's named default, feeding --maximum-event-poll-delay-seconds"
    ),
    "host/serving.py::EVENT_POLL_DELAY_MULTIPLIER": SpelledValue(
        1, "the channel's named default, feeding --event-poll-delay-multiplier"
    ),
    "adapters/dbos/runtime.py::SQLITE_LOCK_TIMEOUT_SECONDS": SpelledValue(
        1, "the channel's named default, feeding --sqlite-lock-timeout-seconds"
    ),
    "adapters/dbos/runtime.py::AGENT_TERMINATION_GRACE_SECONDS": SpelledValue(
        1, "the channel's named default, feeding --agent-termination-grace-seconds"
    ),
    "host/served_seat.py::SEAT_COMMAND_TIMEOUT_SECONDS": SpelledValue(
        1,
        "stable slice invariant: how long a seat's own management command -- a "
        "tmux or systemd call about a socket on this host -- may take before "
        "it counts as failed. A hung one would hold the serve's startup, and "
        "no operator dials the patience of a local socket call (#1099)",
    ),
    "host/served_seat.py::TERMINAL_TERMINATION_GRACE_SECONDS": SpelledValue(
        1,
        "stable slice invariant: what the seat's terminal server gets to close "
        "its own sockets before it is killed at serve shutdown; the session it "
        "served outlives both, so this bounds a child's exit, never an "
        "operator's stop cadence (#1099)",
    ),
    "host/run_command.py::REQUEST_TIMEOUT_SECONDS": SpelledValue(
        1,
        "one named owner for every CLI request; the run/resolve flag is still "
        "pending and stands as an open finding on #251",
    ),
    "host/provider_canary.py::PROVIDER_CANARY_CONFIGURATION_PAGE_SIZE": SpelledValue(
        1,
        "stable slice invariant: OPERATIONS.md documents discovery as capped "
        "at four pages of this size; the operator tunes which vectors run "
        "through the deploy's agent configuration, never this client's own "
        "listing stride (#950)",
    ),
    "host/provider_canary.py::PROVIDER_CANARY_DISCOVERY_TIMEOUT_SECONDS": SpelledValue(
        1,
        "stable slice invariant: OPERATIONS.md's named discovery deadline, "
        "the first term the documented whole-run ceiling sums (#950)",
    ),
    "host/provider_canary.py::PROVIDER_CANARY_HEALTH_WAIT_POLL_INTERVAL_SECONDS": SpelledValue(
        1,
        "stable slice invariant: how often this client re-reads /health while "
        "it waits for a fresh start to answer serving -- a fact about the wait "
        "loop, never an operator's to choose (#1076)",
    ),
    "host/provider_canary.py::PROVIDER_CANARY_HEALTH_WAIT_TIMEOUT_SECONDS": SpelledValue(
        1,
        "stable slice invariant: OPERATIONS.md's named 60-second health-wait "
        "deadline, bounding the pre-discovery poll before this run refuses "
        "with the last health answer rather than trying a vector (#1076)",
    ),
    "host/provider_canary.py::PROVIDER_CANARY_HTTP_TIMEOUT_SECONDS": SpelledValue(
        1,
        "stable slice invariant: OPERATIONS.md's named per-call cap, itself "
        "reduced to the remaining discovery/vector/process deadline at every "
        "call site -- not a patience an operator dials (#950)",
    ),
    "host/provider_canary.py::PROVIDER_CANARY_POLL_INTERVAL_SECONDS": SpelledValue(
        1,
        "stable slice invariant: how often this client re-reads a run's own "
        "terminal state while the documented terminal deadline runs out -- a "
        "fact about the poll loop, never an operator's to choose (#950)",
    ),
    "host/provider_canary.py::PROVIDER_CANARY_TERMINAL_TIMEOUT_SECONDS": SpelledValue(
        1,
        "stable slice invariant: OPERATIONS.md's named per-vector terminal "
        "deadline, the factor the documented whole-run ceiling multiplies by "
        "vector count (#950)",
    ),
    "adapters/dbos/runtime.py::_SQLITE_WAL_RETRY_SECONDS": SpelledValue(
        1, "owner with a seam: named once beside the lock timeout it retries under"
    ),
    "adapters/dbos/runtime.py::_SHUTDOWN_WORKFLOW_COMPLETION_SECONDS": SpelledValue(
        1, "owner with a seam: how long shutdown waits for workflows to finish"
    ),
    "adapters/dbos/workflow.py::CANCELLATION_REDRIVE_SECONDS": SpelledValue(
        1, "owner with a seam: the whole redrive ladder is decided in this one tuple"
    ),
    "adapters/agent_processes.py::MAXIMUM_AGENT_CONTROL_REQUEST_ATTEMPTS": SpelledValue(
        1, "owner with a seam: control-frame retry budget, named once"
    ),
    "adapters/candidate_store.py::LOCK_HANDOVER_PAUSE_SECONDS": SpelledValue(
        1,
        "stable slice invariant: how long one local rename takes, which is what "
        "a writer refused a git ref lock is waiting for -- a fact about the "
        "filesystem under the store, never an operator's to choose; exported so "
        "the ref-lock-handover test can derive its own timing from it (#747)",
    ),
    "adapters/agent_processes.py::ready_timeout_seconds": SpelledValue(
        1,
        "constructor seam already open -- a composer may pass another patience; "
        "the default itself waits for an owner, a sweep follow-up on #251",
    ),
    "adapters/agent_processes.py::timeout_seconds": SpelledValue(
        1,
        "control-frame request patience behind an open parameter seam; the "
        "default waits for an owner, a sweep follow-up on #251",
    ),
    "adapters/agent_process_watchdog.py::CONTROL_FRAME_TIMEOUT_SECONDS": SpelledValue(
        1,
        "this file is condemned to deletion by #15 slice 2; giving it a seam "
        "would preserve it",
    ),
    "adapters/claude_subscription.py::_PROBE_TIMEOUT_SECONDS": SpelledValue(
        1,
        "one thought written three times across the providers -- the sweep's "
        "named duplication, waiting for its one owner on #251",
    ),
    "adapters/codex_subscription.py::_VERSION_PROBE_TIMEOUT_SECONDS": SpelledValue(
        1, "second spelling of the shared probe patience; see the claude entry"
    ),
    "adapters/grok_subscription.py::_VERSION_PROBE_TIMEOUT_SECONDS": SpelledValue(
        1, "third spelling of the shared probe patience; see the claude entry"
    ),
    "adapters/dbos/queries.py::busy_timeout_seconds": SpelledValue(
        1,
        "constructor seam with its own named range refusal directly below it; "
        "the default waits for an owner, a sweep follow-up on #251",
    ),
    "adapters/dbos/queries.py::query_deadline_seconds": SpelledValue(
        1,
        "constructor seam with its own named range refusal directly below it; "
        "the default waits for an owner, a sweep follow-up on #251",
    ),
    "adapters/dbos/schema.py::timeout": SpelledValue(
        1,
        "deliberate zero: the offline migrate command fails loud on a locked "
        "store instead of waiting, per the busy_timeout=0 beside it",
    ),
    "adapters/loopback.py::timeout": SpelledValue(
        2,
        "reader and writer connection of the loopback effect store; mirrors "
        "SQLITE_LOCK_TIMEOUT_SECONDS' value without its owner -- a sweep "
        "follow-up on #251",
    ),
    "adapters/github/effects.py::_SQLITE_LOCK_TIMEOUT_SECONDS": SpelledValue(
        1,
        "reader, writer, and list connections of the recorded GitHub effect "
        "store; mirrors SQLITE_LOCK_TIMEOUT_SECONDS' value without its owner "
        "-- a sweep follow-up on #251",
    ),
    "adapters/github/observation.py::_ISSUES_PAGE_SIZE": SpelledValue(
        1,
        "GitHub's own maximum per_page for its issue listing: a protocol fact "
        "of the platform the adapter speaks, not an operator choice",
    ),
    "adapters/agent_claim_cli.py::AGENT_CLAIM_TIMEOUT_SECONDS": SpelledValue(
        1,
        "stable slice invariant: a bound against a hung agent-claim process, "
        "not an operator's patience -- the CLI answers within seconds (#1299)",
    ),
    "adapters/attempt_workspace_files.py::_STAGED_WRITE_NAME_ATTEMPTS": SpelledValue(
        1,
        "stable slice invariant: a name collision on a random 8-byte suffix is "
        "vanishingly unlikely, so this bounds the retry loop against a genuine "
        "bug rather than shaping a patience an operator would ever dial (#1335)",
    ),
}
"""Every bare literal the operator families still contain, each with its owner.

Naming them is what keeps the promise checkable: a value that quietly joins
this register is a review question in the diff, and a value that leaves the
source takes its entry with it or the gate turns red.
"""


@dataclass(frozen=True)
class Finding:
    key: str
    spelled: str


def bare_value_findings(source_root: Path) -> tuple[Finding, ...]:
    findings: list[Finding] = []
    for module in sorted(source_root.rglob("*.py")):
        relative = module.relative_to(source_root).as_posix()
        for name, value in _family_named_values(
            ast.parse(module.read_text(encoding="utf-8"))
        ):
            if _is_bare_number(value):
                findings.append(Finding(f"{relative}::{name}", ast.unparse(value)))
    return tuple(findings)


def _family_named_values(tree: ast.Module) -> list[tuple[str, ast.expr]]:
    named: list[tuple[str, ast.expr]] = []

    def keep(name: str, value: ast.expr | None) -> None:
        if value is not None and OPERATOR_VALUE_FAMILIES.search(name):
            named.append((name, value))

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    keep(target.id, node.value)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            keep(node.target.id, node.value)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            positional = node.args.posonlyargs + node.args.args
            defaulted = positional[len(positional) - len(node.args.defaults) :]
            for argument, default in zip(defaulted, node.args.defaults, strict=True):
                keep(argument.arg, default)
            for argument, keyword_default in zip(
                node.args.kwonlyargs, node.args.kw_defaults, strict=True
            ):
                keep(argument.arg, keyword_default)
        elif isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg is not None:
                    keep(keyword.arg, keyword.value)
    return named


def _is_bare_number(value: ast.expr) -> bool:
    if isinstance(value, ast.Constant):
        return isinstance(value.value, int | float) and not isinstance(
            value.value, bool
        )
    if isinstance(value, ast.UnaryOp):
        return _is_bare_number(value.operand)
    if isinstance(value, ast.BinOp):
        return _is_bare_number(value.left) and _is_bare_number(value.right)
    if isinstance(value, ast.Tuple):
        return bool(value.elts) and all(_is_bare_number(e) for e in value.elts)
    return False


def ownership_problems(
    source_root: Path, register: dict[str, SpelledValue]
) -> tuple[str, ...]:
    problems = [
        f"{key}: a register entry without a positive count and a real owner "
        "reason is a blanket ignore"
        for key, entry in register.items()
        if entry.occurrences < 1 or not entry.reason.strip()
    ]
    findings = bare_value_findings(source_root)
    spelled = Counter(finding.key for finding in findings)
    example = {finding.key: finding.spelled for finding in findings}
    for key, count in sorted(spelled.items()):
        registered = register.get(key)
        if registered is None:
            problems.append(
                f"{key} spells {example[key]} as a bare literal in an "
                "operator-relevant family; lift it into the configuration "
                "channel (serve flags / settings) or register it in "
                "VALUES_THE_SOURCE_MAY_STILL_SPELL with the owner reason it "
                "may stay"
            )
        elif count > registered.occurrences:
            problems.append(
                f"{key} is spelled {count} times but registered for "
                f"{registered.occurrences}; a registered name may not quietly "
                "multiply"
            )
    for key, entry in sorted(register.items()):
        if spelled[key] < entry.occurrences:
            problems.append(
                f"{key} is registered but the source spells it {spelled[key]} "
                f"of {entry.occurrences} registered times; delete or shrink "
                "its entry together with the code it named"
            )
    return tuple(problems)


@pytest.mark.proves("a-new-bare-operator-value-cannot-enter-unnamed")
def test_todays_source_spells_no_unregistered_operator_value() -> None:
    problems = ownership_problems(SOURCE_ROOT, VALUES_THE_SOURCE_MAY_STILL_SPELL)

    assert problems == (), "\n".join(problems)


def copied_source(tmp_path: Path) -> Path:
    copy = tmp_path / "atelier2"
    shutil.copytree(SOURCE_ROOT, copy)
    return copy


@pytest.mark.parametrize(
    ("invented", "key"),
    [
        ("POLL_TIMEOUT_SECONDS = 3.0\n", "adapters/invented.py::POLL_TIMEOUT_SECONDS"),
        (
            "PROBE_TIMEOUT_SECONDS = 3 * 60\n",
            "adapters/invented.py::PROBE_TIMEOUT_SECONDS",
        ),
        (
            "def wait_for_ready(delay_seconds: float = 0.5) -> None: ...\n",
            "adapters/invented.py::delay_seconds",
        ),
        (
            "def poll(*, backoff_multiplier: float = 1.5) -> None: ...\n",
            "adapters/invented.py::backoff_multiplier",
        ),
        (
            'import sqlite3\n\nprobe = sqlite3.connect("probe", timeout=5)\n',
            "adapters/invented.py::timeout",
        ),
    ],
    ids=[
        "constant-assignment",
        "computed-constant",
        "parameter-default",
        "keyword-only-default",
        "keyword-argument",
    ],
)
@pytest.mark.proves("a-new-bare-operator-value-cannot-enter-unnamed")
def test_a_new_bare_value_in_an_operator_family_turns_the_gate_red(
    tmp_path: Path, invented: str, key: str
) -> None:
    source = copied_source(tmp_path)
    (source / "adapters/invented.py").write_text(invented, encoding="utf-8")

    problems = ownership_problems(source, VALUES_THE_SOURCE_MAY_STILL_SPELL)

    assert len(problems) == 1
    assert key in problems[0]


@pytest.mark.proves("a-new-bare-operator-value-cannot-enter-unnamed")
def test_a_registered_name_that_multiplies_turns_the_gate_red(
    tmp_path: Path,
) -> None:
    source = copied_source(tmp_path)
    with (source / "adapters/loopback.py").open("a", encoding="utf-8") as module:
        module.write('\nprobe = sqlite3.connect("probe", timeout=30.0)\n')

    problems = ownership_problems(source, VALUES_THE_SOURCE_MAY_STILL_SPELL)

    assert len(problems) == 1
    assert "adapters/loopback.py::timeout is spelled 3 times" in problems[0]


@pytest.mark.proves("a-new-bare-operator-value-cannot-enter-unnamed")
def test_a_register_entry_whose_value_left_the_source_turns_the_gate_red(
    tmp_path: Path,
) -> None:
    source = copied_source(tmp_path)
    command = source / "host/run_command.py"
    text = command.read_text(encoding="utf-8")
    removed = text.replace("REQUEST_TIMEOUT_SECONDS = 30.0\n", "", 1)
    assert removed != text
    command.write_text(removed, encoding="utf-8")

    problems = ownership_problems(source, VALUES_THE_SOURCE_MAY_STILL_SPELL)

    assert len(problems) == 1
    assert "host/run_command.py::REQUEST_TIMEOUT_SECONDS is registered" in problems[0]


def test_a_value_that_references_its_owner_is_no_finding(tmp_path: Path) -> None:
    source = copied_source(tmp_path)
    with (source / "host/run_command.py").open("a", encoding="utf-8") as module:
        module.write("\nRESOLVE_TIMEOUT_SECONDS = REQUEST_TIMEOUT_SECONDS\n")

    assert ownership_problems(source, VALUES_THE_SOURCE_MAY_STILL_SPELL) == ()


def test_a_literal_outside_the_operator_families_is_no_finding(
    tmp_path: Path,
) -> None:
    source = copied_source(tmp_path)
    (source / "adapters/invented.py").write_text(
        "MAXIMUM_PROBE_BYTES = 4096\n", encoding="utf-8"
    )

    assert ownership_problems(source, VALUES_THE_SOURCE_MAY_STILL_SPELL) == ()


def test_a_register_entry_without_a_reason_is_refused() -> None:
    doctored = {
        **VALUES_THE_SOURCE_MAY_STILL_SPELL,
        "host/serving.py::EVENT_PAGE_SIZE": SpelledValue(1, "   "),
    }

    problems = ownership_problems(SOURCE_ROOT, doctored)

    assert len(problems) == 1
    assert "blanket ignore" in problems[0]
