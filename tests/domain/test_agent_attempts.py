from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

import pytest

from atelier2.contracts.agent_attempts import (
    AGENT_ATTEMPT_ORDINAL,
    REPLACEMENT_AGENT_ATTEMPT_ORDINAL,
    AgentAttempt,
    AgentAttemptCancellation,
    AgentAttemptCancellationDisposition,
    AgentAttemptFailureCode,
    AgentAttemptId,
    AgentAttemptProcessPhase,
    AgentAttemptRedriveState,
    AgentAttemptReplacement,
    AgentAttemptState,
    AgentProcessOwnerId,
    CancelAgentAttemptRequest,
    WatchdogGenerationId,
)
from atelier2.contracts.agent_transcripts import (
    AttemptTranscript,
    ProviderTerminalRefusal,
    Usage,
)
from atelier2.contracts.agents import (
    MAXIMUM_AGENT_FIELD_CHARACTERS,
    AgentExecutionRequestHash,
    AgentExecutorOperationalIdentity,
    AgentReceiptHash,
)
from atelier2.contracts.executions import NodeExecutionId
from atelier2.contracts.process_endings import (
    MAXIMUM_RECEIPTED_STANDARD_ERROR_BYTES,
    ProcessExitSignature,
    process_exit_verdict,
)
from atelier2.contracts.runs import RunId, WorkflowRevisionHash
from atelier2.contracts.secret_redaction import REDACTION_MARKER


def _attempt(state: AgentAttemptState = AgentAttemptState.PREPARED) -> AgentAttempt:
    execution_id = NodeExecutionId("0" * 64)
    request_hash = AgentExecutionRequestHash("1" * 64)
    return AgentAttempt(
        AgentAttemptId.for_execution(execution_id, request_hash),
        execution_id,
        request_hash,
        AgentExecutorOperationalIdentity("controlled-executor"),
        RunId("run-17"),
        WorkflowRevisionHash("2" * 64),
        "builder",
        AGENT_ATTEMPT_ORDINAL,
        state,
        0 if state is AgentAttemptState.PREPARED else 1,
    )


def test_attempt_id_has_fixed_canonical_vector() -> None:
    attempt_id = AgentAttemptId.for_execution(
        NodeExecutionId("0" * 64),
        AgentExecutionRequestHash("1" * 64),
    )
    assert (
        attempt_id.value
        == "6ea67ad4ac9b01be7a7eddef32e44a8b3bd7391fe69f89eb8407bd05a7dc1129"
    )


def test_attempt_identity_accepts_exactly_two_ordinals() -> None:
    execution_id = NodeExecutionId("0" * 64)
    request_hash = AgentExecutionRequestHash("1" * 64)

    first = AgentAttemptId.for_execution(execution_id, request_hash, 1)
    replacement = AgentAttemptId.for_execution(execution_id, request_hash, 2)

    assert first != replacement
    assert AGENT_ATTEMPT_ORDINAL == 1
    assert REPLACEMENT_AGENT_ATTEMPT_ORDINAL == 2
    for invalid in (0, 3, True):
        with pytest.raises(ValueError, match="ordinal"):
            AgentAttemptId.for_execution(execution_id, request_hash, invalid)


@pytest.mark.parametrize(
    "build",
    (
        AgentProcessOwnerId,
        WatchdogGenerationId,
        lambda value: AgentAttemptCancellation(value, 1, AgentAttemptReplacement.NONE),
    ),
    ids=("process owner id", "watchdog generation id", "cancellation command id"),
)
def test_attempt_text_fields_end_at_the_agent_field_bound(
    build: Callable[[str], object],
) -> None:
    build("x" * MAXIMUM_AGENT_FIELD_CHARACTERS)

    with pytest.raises(ValueError, match=str(MAXIMUM_AGENT_FIELD_CHARACTERS)):
        build("x" * (MAXIMUM_AGENT_FIELD_CHARACTERS + 1))


def test_cancellation_contract_has_one_closed_canonical_terminal_shape() -> None:
    cancellation = AgentAttemptCancellation(
        command_id="cancel-17",
        expected_attempt_state_version=1,
        replacement=AgentAttemptReplacement.ONE,
        redrive_state=AgentAttemptRedriveState.CLEANUP_ATTESTED,
        disposition=AgentAttemptCancellationDisposition.REAPED_AFTER_TERM,
    )
    armed = replace(
        _attempt(AgentAttemptState.LAUNCH_ARMED),
        process_phase=AgentAttemptProcessPhase.LAUNCH_AUTHORIZED,
        process_owner_id=AgentProcessOwnerId("owner-17"),
        watchdog_generation_id=WatchdogGenerationId("generation-17"),
    )
    cancelled = replace(
        armed,
        state=AgentAttemptState.CANCELLED,
        state_version=3,
        process_phase=AgentAttemptProcessPhase.CLEANUP_ATTESTED,
        cancellation=cancellation,
    )

    assert cancelled.cancellation == cancellation
    assert cancelled.state is AgentAttemptState.CANCELLED

    with pytest.raises(ValueError, match="cancellation"):
        replace(cancelled, cancellation=None)


def test_attempt_contract_accepts_only_the_four_exact_state_shapes() -> None:
    prepared = _attempt()
    armed = replace(prepared, state=AgentAttemptState.LAUNCH_ARMED, state_version=1)
    succeeded = replace(
        armed,
        state=AgentAttemptState.SUCCEEDED,
        state_version=2,
        receipt_hash=AgentReceiptHash("3" * 64),
    )
    failed = replace(
        armed,
        state=AgentAttemptState.FAILED,
        state_version=2,
        failure_code=AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY,
    )

    assert (prepared.state, armed.state, succeeded.state, failed.state) == (
        AgentAttemptState.PREPARED,
        AgentAttemptState.LAUNCH_ARMED,
        AgentAttemptState.SUCCEEDED,
        AgentAttemptState.FAILED,
    )


@pytest.mark.parametrize(
    "mutation",
    (
        lambda value: replace(value, attempt_ordinal=2),
        lambda value: replace(value, state_version=1),
        lambda value: replace(
            value,
            failure_code=AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY,
        ),
        lambda value: replace(value, receipt_hash=AgentReceiptHash("3" * 64)),
        lambda value: replace(
            value,
            state=AgentAttemptState.LAUNCH_ARMED,
            state_version=2,
        ),
        lambda value: replace(
            value,
            state=AgentAttemptState.SUCCEEDED,
            state_version=2,
        ),
        lambda value: replace(
            value,
            state=AgentAttemptState.FAILED,
            state_version=2,
        ),
    ),
)
def test_attempt_contract_rejects_every_noncanonical_state_shape(
    mutation: Callable[[AgentAttempt], AgentAttempt],
) -> None:
    with pytest.raises((TypeError, ValueError)):
        mutation(_attempt())


def _cancellation() -> AgentAttemptCancellation:
    return AgentAttemptCancellation(
        command_id="cancel-17",
        expected_attempt_state_version=1,
        replacement=AgentAttemptReplacement.ONE,
    )


def _cancel_request() -> CancelAgentAttemptRequest:
    return CancelAgentAttemptRequest(
        RunId("run-1"),
        AgentAttemptId("2" * 64),
        "cancel-17",
        1,
        AgentAttemptReplacement.ONE,
    )


def test_a_cancellation_matches_the_request_that_named_its_exact_command() -> None:
    assert _cancellation().matches(_cancel_request())


@pytest.mark.parametrize(
    "divergence",
    (
        {"command_id": "cancel-18"},
        {"expected_attempt_state_version": 2},
        {"replacement": AgentAttemptReplacement.NONE},
    ),
    ids=("command id", "expected state version", "replacement policy"),
)
def test_a_cancellation_matches_no_request_that_differs_in_any_bound_field(
    divergence: dict[str, object],
) -> None:
    assert not _cancellation().matches(replace(_cancel_request(), **divergence))


def test_a_cancellation_ignores_progress_the_request_never_carries() -> None:
    attested = replace(
        _cancellation(),
        redrive_state=AgentAttemptRedriveState.CLEANUP_ATTESTED,
        disposition=AgentAttemptCancellationDisposition.REAPED_AFTER_TERM,
    )

    assert attested.matches(_cancel_request())


@pytest.mark.parametrize(
    ("return_code", "named"),
    (
        (1, "exited with code 1"),
        (127, "exited with code 127"),
        (-9, "killed by signal 9"),
        (0, "exited with code 0 leaving an answer its executor could not read"),
    ),
    ids=("a nonzero exit", "a shell not-found exit", "a signal", "a clean exit"),
)
def test_an_exit_signature_says_which_of_the_three_endings_happened(
    return_code: int, named: str
) -> None:
    """Three endings share one failure code, and the receipt tells them apart.

    `PROCESS_EXITED_UNSUCCESSFULLY` covers a child that failed, one that was
    killed, and one that exited cleanly leaving an answer its executor could
    not read. A receipt that only repeated the code would leave an operator
    with the same question the code was supposed to answer.
    """
    assert ProcessExitSignature(return_code, b"").named().startswith(f"{named};")


def test_a_process_that_said_nothing_reads_as_silence_rather_than_as_emptiness() -> (
    None
):
    """Honestly empty is its own answer, never an absent one."""
    assert (
        ProcessExitSignature(1, b"")
        .named()
        .endswith("it wrote nothing to standard error")
    )


def test_a_short_standard_error_is_kept_whole() -> None:
    assert (
        ProcessExitSignature(2, b"grok: rate limited")
        .named()
        .endswith("standard error: grok: rate limited")
    )


def test_only_the_tail_of_a_long_standard_error_reaches_the_receipt() -> None:
    """A receipt is a sentence an operator reads, not the provider's log.

    The tail rather than the head, because the words that explain an ending are
    the last ones a dying process wrote, and the receipt says how much of how
    much it is keeping so nobody mistakes the fragment for the whole.
    """
    said = b"a" * 400_000 + b"the last words"
    named = ProcessExitSignature(3, said).named()

    assert named.endswith("the last words")
    assert (
        f"last {MAXIMUM_RECEIPTED_STANDARD_ERROR_BYTES} of {len(said)} "
        "standard error bytes: "
    ) in named
    assert len(named) < 2 * MAXIMUM_RECEIPTED_STANDARD_ERROR_BYTES


def test_a_terminal_control_sequence_a_provider_wrote_cannot_reach_a_terminal() -> None:
    """This reason is printed by the command line, so it may not drive a cursor."""
    named = ProcessExitSignature(1, b"\x1b[2Jcleared\x07").named()

    assert "\x1b" not in named
    assert "\x07" not in named
    assert "cleared" in named


def test_bytes_that_are_not_text_are_replaced_rather_than_raised_over() -> None:
    """A provider's standard error is external input; a receipt still has to land."""
    assert "\ufffd" in ProcessExitSignature(1, b"broken \xff\xfe end").named()


def test_an_exit_signature_refuses_an_untyped_return_code_or_standard_error() -> None:
    with pytest.raises(TypeError):
        ProcessExitSignature("1", b"")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        ProcessExitSignature(1, "said")  # type: ignore[arg-type]


def test_a_provider_refusal_the_transcript_named_replaces_the_exit_signature() -> None:
    """The receipt keeps what the provider said, not the shell's silence.

    The exit code and standard error explain nothing about a call the provider
    itself read and refused before it did anything (#1029) -- the process
    behaved exactly as designed.
    """

    exit_signature = ProcessExitSignature(1, b"")
    transcript = AttemptTranscript.of(
        [
            Usage(0, 0),
            ProviderTerminalRefusal(
                "rate_limit_error", "429", "Not logged in · Please run /login"
            ),
        ]
    )

    assert process_exit_verdict(exit_signature, transcript) == (
        "provider-reported: rate_limit_error: Not logged in · Please run /login"
    )


def test_an_exit_without_a_named_provider_refusal_keeps_the_exit_signature() -> None:
    """Every other ending -- a crash, a timeout -- still speaks for itself."""

    exit_signature = ProcessExitSignature(1, b"")

    assert process_exit_verdict(exit_signature, None) == exit_signature.named()
    assert (
        process_exit_verdict(exit_signature, AttemptTranscript.of([Usage(0, 0)]))
        == exit_signature.named()
    )


def test_a_refusal_missing_its_own_text_still_composes_a_provider_reason() -> None:
    """A refusal need not explain itself for the receipt to name the provider.

    A `result` line can declare `is_error: true` with no `result` text at all
    (#1029 review); the composed reason still leads with "provider-reported"
    rather than falling back to the exit signature's silence.
    """

    exit_signature = ProcessExitSignature(1, b"")
    transcript = AttemptTranscript.of([ProviderTerminalRefusal("api_error", "", "")])

    assert (
        process_exit_verdict(exit_signature, transcript)
        == "provider-reported: api_error: "
    )


def test_an_operator_home_path_in_the_composed_reason_is_scrubbed() -> None:
    """The receipt's own reason string is as safe as the transcript step it reads.

    `process_exit_verdict` only ever reads an already-kept `AttemptTranscript`,
    so a path the transcript's own redactor already replaced never has a
    second chance to appear in the composed sentence (#1029 review).
    """

    exit_signature = ProcessExitSignature(1, b"")
    transcript = AttemptTranscript.of(
        [
            ProviderTerminalRefusal(
                "api_error",
                "",
                "reading /home/felix-hummert/git/atelier-2/AGENTS.md failed",
            )
        ]
    )

    reason = process_exit_verdict(exit_signature, transcript)

    assert "felix-hummert" not in reason
    assert reason == f"provider-reported: api_error: reading {REDACTION_MARKER} failed"
