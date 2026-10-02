"""Capability-coupled tool-grant redemption, and the effect-shaped port beside it.

`#431` names the bug shape Phase 1 closes before a second capability can ever
trigger it for real: `execute_agent_attempt._redeemed` used to call
`project.verifications.run(...)` unconditionally, deaf to which capability the
pinned grant actually named. A second capability landing beside
`RUN_PROJECT_VERIFICATION` would have been silently redeemed as a project
verification rather than performed or refused. The tests below prove the
dispatch now reads the capability before it decides what to run, and that a
capability no redeemer here performs is refused by name -- proved now, while
the vocabulary still holds exactly one member, by bypassing the constructor
validation nothing legitimate can get past today.

The effect-shaped redemption port (`ports.agent_tool_effects`) is proved here
too, directly against a scripted fake adapter: nothing in production calls it
yet -- no `ToolGrantCapability` names an effect -- so this module is its only
caller in Phase 1.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path

import pytest

from atelier2.adapters.project_source import LocalGitProjectSource
from atelier2.application.execute_agent_attempt import execute_agent_attempt
from atelier2.contracts.agent_attempts import (
    AgentAttempt,
    AgentAttemptId,
    AgentAttemptState,
)
from atelier2.contracts.agent_permissions import GRANTS_NOTHING
from atelier2.contracts.agents import AgentExecutionResult
from atelier2.contracts.artifacts import Artifact
from atelier2.contracts.effects import (
    AdapterOperationalIdentity,
    AdapterRevision,
    CanonicalRequest,
    ConfirmationSource,
    EffectAbsence,
    EffectBinding,
    EffectDestination,
    EffectId,
    EffectIntent,
    EffectIntentMismatch,
    EffectReadback,
    EffectReceipt,
    EffectResult,
    EffectUnknownOutcome,
    LogicalEffectKey,
    PerformedEffect,
    ReadbackPhase,
)
from atelier2.contracts.executions import AgentAttemptExecution
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.project_sources import ProjectSourcePin
from atelier2.contracts.revisions_v3 import PublishedRevisionHash
from atelier2.contracts.runs import RunId, WorkflowRevision
from atelier2.contracts.tool_grants_v3 import (
    DeclaredToolGrant,
    ToolGrantCapability,
    ToolGrantCapabilityNotRedeemed,
)
from atelier2.contracts.workflows import RunCompletes
from atelier2.ports.agent_attempts import (
    AgentAttemptClaimedByThisCall,
    AgentAttemptSucceeded,
)
from atelier2.ports.agent_executions import (
    AgentAttemptWorkspaceLease,
    AgentProcessCommand,
    AgentProcessCompletion,
    AgentProcessInvocation,
    PermissionDecider,
    PrintModeExecutor,
)
from atelier2.ports.agent_tool_effects import (
    AgentToolEffectDelivered,
    AgentToolEffectPending,
    redeem_prepared_tool_effect,
)
from atelier2.ports.artifacts import PublishArtifactResult
from atelier2.ports.project_verification import (
    PinnedProjectSource,
    ProjectVerificationOutcome,
)
from tests.scenarios.agents import (
    agent_attempt_execution,
    agent_execution_request_v2,
    leased_directory_identity,
    prepared_agent_attempt,
    workspace_files_nobody_opens,
)
from tests.scenarios.projects import (
    CandidatesKeptInMemory,
    declaring_verification,
    git_project,
)

THE_GRANT = DeclaredToolGrant(
    PublishedRevisionHash("c3" * 32), ToolGrantCapability.RUN_PROJECT_VERIFICATION
)


class _AFutureCapability(StrEnum):
    """Shaped exactly like `ToolGrantCapability` -- a `StrEnum` with one member
    -- standing in for the second member `#431` Phase 2 will add, so the
    dispatch is proved against the shape it will actually receive rather than
    an arbitrary string."""

    OPEN_PR = "open-pr"


def _grant_naming_a_capability_no_redeemer_performs() -> DeclaredToolGrant:
    """A grant `read_tool_grant_document` would already refuse to accept.

    `DeclaredToolGrant.__post_init__` requires `ToolGrantCapability`, and that
    vocabulary holds exactly one member -- so nothing legitimate can construct
    this today. It is built by bypassing that validation on purpose: the
    redemption dispatch must refuse this shape defensively, before a second
    capability ever exists to reach it for real, rather than assume the
    validation upstream is the only guard the invariant will ever have.
    """
    grant = object.__new__(DeclaredToolGrant)
    object.__setattr__(grant, "revision_hash", PublishedRevisionHash("c3" * 32))
    object.__setattr__(grant, "capability", _AFutureCapability.OPEN_PR)
    return grant


@dataclass
class _ClaimingStore:
    """A store that wins the claim and records what redemption it kept."""

    attempt: AgentAttempt | None = None
    redemption: object = None
    completed: int = 0

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
        execution: object,
        result: object,
        redemption: object,
        candidate_diff: str | None = None,
    ) -> AgentAttemptSucceeded:
        del execution, result, candidate_diff
        self.completed += 1
        self.redemption = redemption
        assert self.attempt is not None
        return AgentAttemptSucceeded(self.attempt, RunCompletes())


@dataclass
class _SucceedingExecutor(PrintModeExecutor):
    """A provider that answers; what redeems its work is the subject."""

    def prepare_process(self, request: object) -> AgentProcessCommand:
        del request
        return AgentProcessCommand(("/bin/true",), standard_output_frame_bytes=1024)

    def decode_process_completion(
        self, invocation: AgentProcessInvocation, completion: AgentProcessCompletion
    ) -> AgentExecutionResult:
        del invocation, completion
        return AgentExecutionResult(b'"ok"')

    def release_credential_channel(self, command: AgentProcessCommand) -> None:
        del command

    def close(self) -> None:
        return None


@dataclass
class _RecordingSupervisor:
    """A supervisor that records whether an attempt ever reached finalize."""

    finalized: int = 0

    def prepare(self, execution: AgentAttemptExecution) -> AgentAttempt:
        return prepared_agent_attempt(execution)

    def launch_and_wait(
        self,
        execution: AgentAttemptExecution,
        invocation: AgentProcessInvocation,
        permissions: PermissionDecider,
    ) -> AgentProcessCompletion:
        del permissions
        del execution, invocation
        return AgentProcessCompletion(0, b'"ok"', b"")

    def finalize(self, execution: AgentAttemptExecution) -> None:
        del execution
        self.finalized += 1


@dataclass
class _LeasingWorkspaces:
    """A workspace owner that leases a real directory and counts release calls."""

    directory: Path
    acquired: int = 0
    released: int = 0

    def preflight(self) -> None:
        return None

    def acquire(self, attempt_id: AgentAttemptId) -> AgentAttemptWorkspaceLease:
        self.acquired += 1
        return leased_directory_identity(attempt_id, self.directory)

    def release(self, attempt_id: AgentAttemptId) -> None:
        del attempt_id
        self.released += 1


@dataclass
class _RunOnceVerifications:
    """A verification runner standing in for `RUN_PROJECT_VERIFICATION`'s own."""

    ran: int = 0

    def preflight(self, pin: ProjectSourcePin) -> None:
        del pin

    def run(
        self, pin: ProjectSourcePin, lease: AgentAttemptWorkspaceLease
    ) -> ProjectVerificationOutcome:
        del pin, lease
        self.ran += 1
        return ProjectVerificationOutcome(
            ("/bin/true",), 0, Sha256Hash.of(b""), 0.0, b"", None
        )


@dataclass
class _NeverRedeemedVerifications:
    """A verification runner an unrecognized capability must never reach."""

    asked: int = 0

    def preflight(self, pin: ProjectSourcePin) -> None:
        del pin
        self.asked += 1

    def run(
        self, pin: ProjectSourcePin, lease: AgentAttemptWorkspaceLease
    ) -> ProjectVerificationOutcome:
        del pin, lease
        raise AssertionError(
            "a capability this runtime does not recognize must not be "
            "silently redeemed as a project verification"
        )


@dataclass
class _UnreachedArtifactPublisher:
    """A publisher a passing verification must never touch.

    A redeemable `run-project-verification` grant is wired with one before any
    provider work runs; a check that exits zero never publishes anything, so
    reaching this fake's own method would itself be the defect under test.
    """

    def publish_artifact(self, artifact: Artifact) -> PublishArtifactResult:
        raise AssertionError(
            "a passing verification must not publish anything", artifact
        )


def _drive(
    project: PinnedProjectSource, root: Path
) -> tuple[_ClaimingStore, _RecordingSupervisor, _LeasingWorkspaces]:
    store = _ClaimingStore()
    supervisor = _RecordingSupervisor()
    workspaces = _LeasingWorkspaces(root / "lease")
    execute_agent_attempt(
        agent_attempt_execution(agent_execution_request_v2()),
        _SucceedingExecutor(),  # type: ignore[arg-type]
        store,  # type: ignore[arg-type]
        supervisor,  # type: ignore[arg-type]
        workspaces,  # type: ignore[arg-type]
        project,
        _UnreachedArtifactPublisher(),  # type: ignore[arg-type]
        permissions=GRANTS_NOTHING,
        workspace_files=workspace_files_nobody_opens,
    )
    return store, supervisor, workspaces


def test_a_declared_capability_dispatches_to_its_own_redeemer(tmp_path: Path) -> None:
    """`RUN_PROJECT_VERIFICATION` still reaches `verifications.run`, through
    the capability dispatch rather than an unconditional call."""
    root = tmp_path / "project"
    pin = git_project(root, declaring_verification(["/bin/true"]))
    verifications = _RunOnceVerifications()
    project = PinnedProjectSource(
        LocalGitProjectSource(root),
        verifications,
        CandidatesKeptInMemory(),
        pin,
        THE_GRANT,
        None,
    )

    store, supervisor, workspaces = _drive(project, tmp_path)

    assert verifications.ran == 1
    assert store.completed == 1
    assert store.redemption is not None
    assert supervisor.finalized == 1
    assert workspaces.released == 1


def test_a_capability_no_redeemer_performs_is_refused_by_name(tmp_path: Path) -> None:
    """A capability the dispatch does not recognize is refused, not silently
    performed as a project verification -- the exact bug shape `#431` names."""
    root = tmp_path / "project"
    pin = git_project(root, declaring_verification(["/bin/true"]))
    verifications = _NeverRedeemedVerifications()
    grant = _grant_naming_a_capability_no_redeemer_performs()
    project = PinnedProjectSource(
        LocalGitProjectSource(root),
        verifications,
        CandidatesKeptInMemory(),
        pin,
        grant,
        None,
    )
    store = _ClaimingStore()
    supervisor = _RecordingSupervisor()
    workspaces = _LeasingWorkspaces(tmp_path / "lease")

    with pytest.raises(ToolGrantCapabilityNotRedeemed) as raised:
        execute_agent_attempt(
            agent_attempt_execution(agent_execution_request_v2()),
            _SucceedingExecutor(),  # type: ignore[arg-type]
            store,  # type: ignore[arg-type]
            supervisor,  # type: ignore[arg-type]
            workspaces,  # type: ignore[arg-type]
            project,
            permissions=GRANTS_NOTHING,
            workspace_files=workspace_files_nobody_opens,
        )

    assert raised.value.capability == "open-pr"
    assert store.completed == 0
    assert supervisor.finalized == 0
    assert workspaces.released == 0


def test_a_non_verification_grant_needs_no_artifact_publisher_to_reach_its_own_refusal(
    tmp_path: Path,
) -> None:
    """Preflight's artifact-publisher requirement is scoped to `run-project-verification`.

    `#1137` wires a publisher only where a redeemable `RUN_PROJECT_VERIFICATION`
    grant is pinned, because only that redeemer ever has a failed check's tail
    to keep. A grant naming a real but different capability -- `open-pr`,
    redeemed as a platform effect elsewhere -- must reach its own dispatch
    refusal (`ToolGrantCapabilityNotRedeemed`) with no publisher wired at all,
    rather than tripping a publisher requirement that was never its own.
    """
    root = tmp_path / "project"
    pin = git_project(root, declaring_verification(["/bin/true"]))
    verifications = _NeverRedeemedVerifications()
    grant = DeclaredToolGrant(
        PublishedRevisionHash("c3" * 32), ToolGrantCapability.OPEN_PR
    )
    project = PinnedProjectSource(
        LocalGitProjectSource(root),
        verifications,
        CandidatesKeptInMemory(),
        pin,
        grant,
        None,
    )
    store = _ClaimingStore()
    supervisor = _RecordingSupervisor()
    workspaces = _LeasingWorkspaces(tmp_path / "lease")

    with pytest.raises(ToolGrantCapabilityNotRedeemed) as raised:
        execute_agent_attempt(
            agent_attempt_execution(agent_execution_request_v2()),
            _SucceedingExecutor(),  # type: ignore[arg-type]
            store,  # type: ignore[arg-type]
            supervisor,  # type: ignore[arg-type]
            workspaces,  # type: ignore[arg-type]
            project,
            None,
            permissions=GRANTS_NOTHING,
            workspace_files=workspace_files_nobody_opens,
        )

    assert raised.value.capability == "open-pr"
    assert store.completed == 0
    assert supervisor.finalized == 0
    assert workspaces.released == 0


# --- ports.agent_tool_effects: the effect-shaped redemption port -----------

_BINDING = EffectBinding(
    logical_key=LogicalEffectKey("run-1/open-pr"),
    run_id=RunId("run-1"),
    workflow_revision_hash=WorkflowRevision(b"workflow-v1").revision_hash,
    adapter_revision=AdapterRevision("github-adapter-1"),
    destination=EffectDestination("github.com/FlexOr2/atelier-2"),
    adapter_operational_identity=AdapterOperationalIdentity("github-installation-1"),
)
_PREPARED_INTENT = EffectIntent(
    _BINDING, CanonicalRequest(b'{"title":"ship the slice"}')
)


def _receipt_for(
    intent: EffectIntent, confirmation_source: ConfirmationSource
) -> EffectReceipt:
    return EffectReceipt(
        intent=intent,
        effect_id=EffectId("pull-request-7"),
        result=EffectResult(b'{"number":7}'),
        confirmation_source=confirmation_source,
    )


@dataclass
class _ScriptedEffectAdapter:
    """A fake `EffectAdapter`: a readback this test scripts, an execute this
    test may forbid or answer once."""

    readback_result: EffectReadback
    execute_result: PerformedEffect | None = None
    readback_calls: int = field(default=0, init=False)
    execute_calls: int = field(default=0, init=False)

    def readback(self, intent: EffectIntent, phase: ReadbackPhase) -> EffectReadback:
        del intent
        self.readback_calls += 1
        return self.readback_result

    def execute(self, intent: EffectIntent) -> PerformedEffect:
        del intent
        self.execute_calls += 1
        if self.execute_result is None:
            raise AssertionError(
                "readback already found the effect; execute must not run"
            )
        return self.execute_result

    def close(self) -> None:
        return None


def test_a_readback_that_already_finds_the_effect_never_creates_it() -> None:
    """Delivered, and by readback alone -- `execute` is never asked."""
    receipt = _receipt_for(_PREPARED_INTENT, ConfirmationSource.ADAPTER_READBACK)
    adapter = _ScriptedEffectAdapter(receipt)

    redemption = redeem_prepared_tool_effect(_PREPARED_INTENT, adapter)

    assert redemption == AgentToolEffectDelivered(receipt)
    assert adapter.execute_calls == 0


def test_an_authoritative_absence_is_the_only_readback_that_licenses_create() -> None:
    """Absent by readback, then created once -- `create` follows `readback`."""
    performed = PerformedEffect(
        EffectId("pull-request-7"), EffectResult(b'{"number":7}')
    )
    adapter = _ScriptedEffectAdapter(
        EffectAbsence(_PREPARED_INTENT.reference), performed
    )

    redemption = redeem_prepared_tool_effect(_PREPARED_INTENT, adapter)

    assert isinstance(redemption, AgentToolEffectDelivered)
    assert redemption.receipt.effect_id == performed.effect_id
    assert redemption.receipt.result == performed.result
    assert (
        redemption.receipt.confirmation_source is ConfirmationSource.ADAPTER_EXECUTION
    )
    assert adapter.readback_calls == 1
    assert adapter.execute_calls == 1


def test_an_unknown_readback_is_handed_back_rather_than_guessed_at() -> None:
    """UNKNOWN licenses nothing: no create, and no fabricated receipt."""
    unknown = EffectUnknownOutcome(_PREPARED_INTENT.reference)
    adapter = _ScriptedEffectAdapter(unknown)

    redemption = redeem_prepared_tool_effect(_PREPARED_INTENT, adapter)

    assert redemption == AgentToolEffectPending(unknown)
    assert adapter.execute_calls == 0


def test_a_readback_for_a_different_intent_is_refused_rather_than_trusted() -> None:
    """An adapter answering for the wrong prepared intent is caught here too,
    exactly as `EffectIntent.authorize_adapter_readback` already refuses it."""
    other_intent = EffectIntent(
        _BINDING, CanonicalRequest(b'{"title":"a different pr"}')
    )
    adapter = _ScriptedEffectAdapter(
        _receipt_for(other_intent, ConfirmationSource.ADAPTER_READBACK)
    )

    with pytest.raises(EffectIntentMismatch):
        redeem_prepared_tool_effect(_PREPARED_INTENT, adapter)
