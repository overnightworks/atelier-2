"""An attempt records decoded transcript events at the application boundary."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import cast

import pytest

from atelier2.application.execute_agent_attempt import execute_agent_attempt
from atelier2.contracts.agent_attempts import (
    AgentAttempt,
    AgentAttemptId,
    AgentAttemptState,
)
from atelier2.contracts.agent_permissions import (
    ATTEMPT_WORKSPACE,
    GRANTS_NOTHING,
    PermissionAuthority,
    PermissionCorrelationId,
    PermissionDecision,
    PermissionEffect,
    PermissionPolicyRevision,
    PermissionReceipt,
    PermissionRequest,
    PermissionScope,
    PermissionScopeKind,
)
from atelier2.contracts.agent_transcripts import (
    AssistantTurn,
    AttemptTranscript,
    TranscriptBeforeMoments,
    TranscriptMomentOrigin,
    TranscriptRecordedMoment,
)
from atelier2.contracts.agents import AgentExecutionResult
from atelier2.contracts.executions import AgentAttemptExecution
from atelier2.contracts.when import RecordedAt
from atelier2.contracts.workflows import RunCompletes
from atelier2.ports.agent_attempts import (
    AgentAttemptClaimedByThisCall,
    AgentAttemptStore,
    AgentAttemptSucceeded,
)
from atelier2.ports.agent_executions import (
    AgentAttemptWorkspaceLease,
    AgentProcessCommand,
    AgentProcessCompletion,
    AgentProcessInvocation,
    ProviderConversationBinding,
)
from atelier2.ports.provider_conversations import (
    ProviderFilesystemAccess,
    ProviderFilesystemAnswer,
    ProviderFilesystemAuthority,
    ProviderFilesystemEffect,
    ProviderFilesystemReply,
    ProviderFilesystemRequest,
    ProviderFilesystemRequestId,
)
from tests.scenarios.agents import (
    FakeAgentSession,
    agent_attempt_execution,
    agent_execution_request_v2,
    conversation_nobody_drives,
    leased_directory_identity,
    prepared_agent_attempt,
    workspace_files_nobody_opens,
)


@dataclass
class _ClaimingStore:
    attempt: AgentAttempt | None = None
    completed_result: AgentExecutionResult | None = None
    receipts: list[PermissionReceipt] = field(default_factory=list)

    def record_permission_decision(self, receipt: PermissionReceipt) -> None:
        self.receipts.append(receipt)

    def prepare(self, execution: AgentAttemptExecution) -> AgentAttempt:
        self.attempt = prepared_agent_attempt(execution)
        return self.attempt

    def claim(self, execution: AgentAttemptExecution) -> AgentAttemptClaimedByThisCall:
        del execution
        assert self.attempt is not None
        self.attempt = replace(
            self.attempt,
            state=AgentAttemptState.LAUNCH_ARMED,
            state_version=self.attempt.state_version + 1,
        )
        return AgentAttemptClaimedByThisCall(self.attempt)

    def complete_success(
        self,
        execution: AgentAttemptExecution,
        result: AgentExecutionResult,
        redemption: object,
        candidate_diff: str | None = None,
    ) -> AgentAttemptSucceeded:
        del execution, redemption, candidate_diff
        self.completed_result = result
        assert self.attempt is not None
        return AgentAttemptSucceeded(self.attempt, RunCompletes())


class _TheReceiptCouldNotBeKept(RuntimeError):
    """What a store raises when durable state will not take the decision."""


@dataclass
class _StoreThatCannotKeepAReceipt(_ClaimingStore):
    def record_permission_decision(self, receipt: PermissionReceipt) -> None:
        del receipt
        raise _TheReceiptCouldNotBeKept("durable state is unavailable")


class _TranscriptExecutor:
    def prepare_process(self, request: object) -> AgentProcessCommand:
        del request
        return AgentProcessCommand(("/bin/true",), standard_output_frame_bytes=1)

    def open_conversation(
        self,
        request: object,
        command: AgentProcessCommand,
        lease: AgentAttemptWorkspaceLease,
    ) -> ProviderConversationBinding | None:
        del request, command, lease
        return None

    def decode_process_completion(
        self,
        invocation: AgentProcessInvocation,
        completion: AgentProcessCompletion,
    ) -> AgentExecutionResult:
        del invocation, completion
        return AgentExecutionResult(
            b'"done"', AttemptTranscript.of((AssistantTurn("The work is done."),))
        )

    def release_credential_channel(self, command: AgentProcessCommand) -> None:
        del command

    def close(self) -> None:
        return None


class _ConversingExecutor(_TranscriptExecutor):
    def open_conversation(
        self,
        request: object,
        command: AgentProcessCommand,
        lease: AgentAttemptWorkspaceLease,
    ) -> ProviderConversationBinding:
        del request, command, lease
        return conversation_nobody_drives()


@dataclass
class _Bound:
    """What the dispatch handed the opener, and what the access it got answered."""

    lease: AgentAttemptWorkspaceLease
    maximum_read_bytes: int
    authority: ProviderFilesystemAuthority
    answered: list[ProviderFilesystemRequest] = field(default_factory=list)

    def answer(self, request: ProviderFilesystemRequest) -> ProviderFilesystemReply:
        self.answered.append(request)
        return ProviderFilesystemReply(
            request.request_id, ProviderFilesystemAnswer.ANSWERED, b"bound"
        )


@dataclass
class _RecordingOpener:
    bound: list[_Bound] = field(default_factory=list)

    def __call__(
        self,
        lease: AgentAttemptWorkspaceLease,
        maximum_read_bytes: int,
        authority: ProviderFilesystemAuthority,
    ) -> ProviderFilesystemAccess:
        self.bound.append(_Bound(lease, maximum_read_bytes, authority))
        return self.bound[-1]


@dataclass
class _Workspaces:
    directory: Path

    def preflight(self) -> None:
        return None

    def acquire(self, attempt_id: AgentAttemptId) -> AgentAttemptWorkspaceLease:
        return leased_directory_identity(attempt_id, self.directory)

    def release(self, attempt_id: AgentAttemptId) -> None:
        del attempt_id


@pytest.mark.proves("every-transcript-event-carries-its-moment")
def test_proves_every_transcript_event_carries_its_moment_when_recorded(
    tmp_path: Path,
) -> None:
    """proves(every-transcript-event-carries-its-moment)"""

    store = _ClaimingStore()
    recording_moment = RecordedAt("2026-08-29T12:00:00Z")
    execute_agent_attempt(
        agent_attempt_execution(agent_execution_request_v2()),
        _TranscriptExecutor(),
        cast(AgentAttemptStore, store),
        FakeAgentSession(AgentProcessCompletion(0, b'"done"', b"")),
        _Workspaces(tmp_path / "workspace"),
        clock=lambda: recording_moment,
        permissions=GRANTS_NOTHING,
        workspace_files=workspace_files_nobody_opens,
    )

    assert store.completed_result is not None
    recorded = store.completed_result.transcript
    assert recorded is not None
    assert recorded.document.startswith(b'{"kind":"attempt-transcript/v2",')
    assert all(
        event.moment
        == TranscriptRecordedMoment(recording_moment, TranscriptMomentOrigin.RECORDED)
        for event in recorded.events
    )


@pytest.mark.proves("every-transcript-event-carries-its-moment")
def test_proves_every_v1_transcript_event_explicitly_predates_moments() -> None:
    """proves(every-transcript-event-carries-its-moment)"""

    v1_document = (
        b'{"kind":"attempt-transcript/v1","events":['
        b'{"event":"assistant-turn","text":"Earlier work.","redacted":false}]}'
    )

    historical = AttemptTranscript.from_document(v1_document)

    assert historical.document == v1_document
    assert isinstance(historical.events[0].moment, TranscriptBeforeMoments)
    with pytest.raises(ValueError, match="cannot be given a recording moment"):
        historical.with_recorded_moment(RecordedAt("2026-08-29T12:00:00Z"))


def test_a_question_the_dispatched_policy_never_granted_is_refused(
    tmp_path: Path,
) -> None:
    """The dispatched policy answers, and the answer names it.

    The policy handed in grants one host and the provider asks about another, so
    the refusal can only have been decided against this revision -- a call that
    ignored it and answered under any other authority would carry another hash.
    """

    execution = agent_attempt_execution(agent_execution_request_v2())
    may_reach_one_host = PermissionPolicyRevision(
        frozenset(
            {
                (
                    PermissionEffect.NETWORK,
                    PermissionScope(PermissionScopeKind.HOST, "granted.invalid"),
                )
            }
        )
    )
    session = FakeAgentSession(
        AgentProcessCompletion(0, b'"done"', b""),
        asks=(
            (
                PermissionEffect.NETWORK,
                PermissionScope(PermissionScopeKind.HOST, "unnamed.invalid"),
            ),
        ),
    )

    execute_agent_attempt(
        execution,
        _TranscriptExecutor(),
        cast(AgentAttemptStore, _ClaimingStore()),
        session,
        _Workspaces(tmp_path / "workspace"),
        permissions=may_reach_one_host,
        workspace_files=workspace_files_nobody_opens,
    )

    assert may_reach_one_host.revision_hash != GRANTS_NOTHING.revision_hash
    assert session.answers == [
        PermissionDecision(
            PermissionCorrelationId.for_call(execution.attempt_id, 1),
            False,
            may_reach_one_host.revision_hash,
            PermissionAuthority.POLICY,
        )
    ]


def test_a_decision_that_cannot_be_kept_is_never_given_to_the_provider(
    tmp_path: Path,
) -> None:
    """No receipt, no decision, no effect -- and the attempt is left armed.

    ADR 0020 §3 makes the receipt the authorisation, so a store that cannot keep
    one leaves nothing to answer with. The error rises loudly through the
    session; this attempt is not ended here under some invented failure, because
    nothing about the provider's work is known. It stays `LAUNCH_ARMED` for the
    convergence that owns every attempt whose driver is gone -- which is what
    stops and reaps the process.
    """

    execution = agent_attempt_execution(agent_execution_request_v2())
    store = _StoreThatCannotKeepAReceipt()
    session = FakeAgentSession(
        AgentProcessCompletion(0, b'"done"', b""),
        asks=(
            (
                PermissionEffect.NETWORK,
                PermissionScope(PermissionScopeKind.HOST, "unnamed.invalid"),
            ),
        ),
    )

    with pytest.raises(_TheReceiptCouldNotBeKept):
        execute_agent_attempt(
            execution,
            _TranscriptExecutor(),
            cast(AgentAttemptStore, store),
            session,
            _Workspaces(tmp_path / "workspace"),
            permissions=GRANTS_NOTHING,
            workspace_files=workspace_files_nobody_opens,
        )

    assert session.answers == []
    assert store.completed_result is None
    assert store.attempt is not None
    assert store.attempt.state is AgentAttemptState.LAUNCH_ARMED


def test_a_conversing_provider_reaches_files_only_through_the_lease_bound_access(
    tmp_path: Path,
) -> None:
    """The executor's own access is replaced, never consulted: the dispatch binds
    the deployment's opener to the lease it acquired and to the driver's reply
    bound, and every file request the session relays lands there."""

    execution = agent_attempt_execution(agent_execution_request_v2())
    opener = _RecordingOpener()
    read = ProviderFilesystemRequest(
        ProviderFilesystemEffect.READ, Path("notes.md"), ProviderFilesystemRequestId(1)
    )
    session = FakeAgentSession(
        AgentProcessCompletion(0, b'"done"', b""), file_requests=(read,)
    )

    execute_agent_attempt(
        execution,
        _ConversingExecutor(),
        cast(AgentAttemptStore, _ClaimingStore()),
        session,
        _Workspaces(tmp_path / "workspace"),
        permissions=GRANTS_NOTHING,
        workspace_files=opener,
    )

    (bound,) = opener.bound
    assert bound.lease == leased_directory_identity(
        execution.attempt_id, tmp_path / "workspace"
    )
    assert (
        bound.maximum_read_bytes
        == conversation_nobody_drives().driver.bounds.maximum_reply_bytes
    )
    assert bound.answered == [read]
    assert session.file_replies == [
        ProviderFilesystemReply(
            ProviderFilesystemRequestId(1), ProviderFilesystemAnswer.ANSWERED, b"bound"
        )
    ]


def test_the_authority_bound_to_the_files_keeps_every_answer_before_giving_it(
    tmp_path: Path,
) -> None:
    """Both answers the access can ask for are receipts under the dispatched
    revision: a decision the policy made, and a refusal the fence made without
    the policy being asked -- refused, though this revision grants the workspace."""

    execution = agent_attempt_execution(agent_execution_request_v2())
    store = _ClaimingStore()
    opener = _RecordingOpener()
    recording_moment = RecordedAt("2026-09-06T12:00:00Z")
    grants_the_workspace = PermissionPolicyRevision(
        frozenset({(PermissionEffect.WORKSPACE_READ, ATTEMPT_WORKSPACE)})
    )
    execute_agent_attempt(
        execution,
        _ConversingExecutor(),
        cast(AgentAttemptStore, store),
        FakeAgentSession(AgentProcessCompletion(0, b'"done"', b"")),
        _Workspaces(tmp_path / "workspace"),
        clock=lambda: recording_moment,
        permissions=grants_the_workspace,
        workspace_files=opener,
    )
    (bound,) = opener.bound
    decided_question = PermissionRequest(
        PermissionEffect.WORKSPACE_READ,
        ATTEMPT_WORKSPACE,
        PermissionCorrelationId.for_file_call(execution.attempt_id, 1),
    )
    refused_question = PermissionRequest(
        PermissionEffect.WORKSPACE_READ,
        ATTEMPT_WORKSPACE,
        PermissionCorrelationId.for_file_call(execution.attempt_id, 2),
    )

    decided = bound.authority.decide(decided_question)
    refused = bound.authority.refuse(refused_question)

    assert (decided.granted, refused.granted) == (True, False)
    assert store.receipts == [
        PermissionReceipt.of(
            execution.attempt_id, decided_question, decided, recording_moment
        ),
        PermissionReceipt.of(
            execution.attempt_id, refused_question, refused, recording_moment
        ),
    ]
    assert all(
        receipt.policy_revision_hash == grants_the_workspace.revision_hash
        for receipt in store.receipts
    )


def test_a_file_receipt_that_cannot_be_kept_is_never_answered(tmp_path: Path) -> None:
    """The same order as a permission: no receipt, no answer, no effect."""

    execution = agent_attempt_execution(agent_execution_request_v2())
    store = _StoreThatCannotKeepAReceipt()
    opener = _RecordingOpener()
    execute_agent_attempt(
        execution,
        _ConversingExecutor(),
        cast(AgentAttemptStore, store),
        FakeAgentSession(AgentProcessCompletion(0, b'"done"', b"")),
        _Workspaces(tmp_path / "workspace"),
        permissions=GRANTS_NOTHING,
        workspace_files=opener,
    )
    (bound,) = opener.bound

    with pytest.raises(_TheReceiptCouldNotBeKept):
        bound.authority.refuse(
            PermissionRequest(
                PermissionEffect.WORKSPACE_READ,
                ATTEMPT_WORKSPACE,
                PermissionCorrelationId.for_file_call(execution.attempt_id, 1),
            )
        )
