from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol

from atelier2.contracts.agent_attempts import (
    AgentAttempt,
    AgentAttemptCancellationDisposition,
    AgentAttemptId,
    AgentProcessOwnerId,
    CancelAgentAttemptRequest,
    WatchdogGenerationId,
)
from atelier2.contracts.agent_permissions import PermissionReceipt
from atelier2.contracts.agent_transcripts import AttemptTranscript
from atelier2.contracts.agents import AgentExecutionResult
from atelier2.contracts.artifacts import ArtifactHash
from atelier2.contracts.executions import AgentAttemptExecution, AgentExecutionRefusal
from atelier2.contracts.pages import PageLimit
from atelier2.contracts.process_endings import ProcessExitSignature
from atelier2.contracts.run_bindings import AnyRun
from atelier2.contracts.run_cancellations import CancelRunRequest
from atelier2.contracts.run_projections import RunCancellationRefusal
from atelier2.contracts.tool_grants_v3 import ToolRedemptionReceipt
from atelier2.contracts.workflows import NodeCompletion
from atelier2.ports.durable_runs import DurableStateCorrupt, DurableWriteUnavailable


@dataclass(frozen=True)
class AgentAttemptClaimedByThisCall:
    attempt: AgentAttempt


@dataclass(frozen=True)
class AgentAttemptSucceeded:
    attempt: AgentAttempt
    completion: NodeCompletion


@dataclass(frozen=True)
class AgentAttemptFailed:
    attempt: AgentAttempt


@dataclass(frozen=True)
class AgentAttemptPossiblyRan:
    attempt: AgentAttempt


type AgentAttemptClaimResult = (
    AgentAttemptClaimedByThisCall
    | AgentAttemptSucceeded
    | AgentAttemptFailed
    | AgentAttemptPossiblyRan
)
type AgentAttemptExecutionOutcome = (
    AgentAttemptSucceeded | AgentAttemptFailed | AgentAttemptPossiblyRan
)


@dataclass(frozen=True)
class AgentExecutorBindingRefusalWritten:
    """The run closed with its attempt-less unavailable-executor event."""


@dataclass(frozen=True)
class AgentExecutorBindingRefusalNeedsPreparedCleanup:
    """Ordinal one is safe to clean through the existing cancellation path."""

    attempt: AgentAttempt
    cleanup_request: CancelAgentAttemptRequest


@dataclass(frozen=True)
class AgentExecutorBindingRefusalFenced:
    """This attempt may have crossed a launch boundary; #426 leaves it alone."""

    attempt: AgentAttempt


type AgentExecutorBindingRefusalResult = (
    AgentExecutorBindingRefusalWritten
    | AgentExecutorBindingRefusalNeedsPreparedCleanup
    | AgentExecutorBindingRefusalFenced
)


@dataclass(frozen=True)
class AgentAttemptCancellationAccepted:
    attempt: AgentAttempt
    terminal: bool
    replacement_attempt_id: AgentAttemptId | None = None


@dataclass(frozen=True)
class AgentAttemptCancellationRunMissing:
    pass


@dataclass(frozen=True)
class AgentAttemptCancellationTargetMissing:
    pass


@dataclass(frozen=True)
class AgentAttemptCancellationNotCurrent:
    pass


@dataclass(frozen=True)
class AgentAttemptCancellationStale:
    pass


@dataclass(frozen=True)
class AgentAttemptCancellationTerminalConflict:
    pass


@dataclass(frozen=True)
class AgentAttemptCancellationCommandConflict:
    pass


@dataclass(frozen=True)
class AgentAttemptReplacementNotAllowed:
    pass


type AgentAttemptCancellationResult = (
    AgentAttemptCancellationAccepted
    | AgentAttemptCancellationRunMissing
    | AgentAttemptCancellationTargetMissing
    | AgentAttemptCancellationNotCurrent
    | AgentAttemptCancellationStale
    | AgentAttemptCancellationTerminalConflict
    | AgentAttemptCancellationCommandConflict
    | AgentAttemptReplacementNotAllowed
    | DurableWriteUnavailable
    | DurableStateCorrupt
)


@dataclass(frozen=True)
class RunCancellationAccepted:
    """A genuinely new command moved the run's live attempt to `CANCEL_REQUESTED`."""

    attempt: AgentAttempt


@dataclass(frozen=True)
class RunCancellationTerminalRetry:
    """The exact command was already accepted, and cleanup already ended it."""

    run: AnyRun


@dataclass(frozen=True)
class RunCancellationOvertakenBySuccess:
    """The exact command was accepted, but the attempt succeeded first.

    The run is not terminal because of this command -- it kept going on the
    success. `terminal` is not a field here the way it is on
    `AgentAttemptCancellationAccepted`: there is no terminal attempt state a
    caller could read as "this cancel ended the run", because it did not.
    """

    run: AnyRun


@dataclass(frozen=True)
class RunCancellationEndedRun:
    """This command ended the run itself, with nothing left to converge (#668).

    A run resting at a pause has no attempt to stop, so the command's own
    attestation is the last thing written and the run is already `CANCELLED`
    when this answer is given. Distinct from `RunCancellationTerminalRetry`,
    which reports an *earlier* command whose cleanup has since ended the run:
    both hand back a terminal run, but only one of them is news.
    """

    run: AnyRun


@dataclass(frozen=True)
class RunCancellationNotCancellable:
    reason: RunCancellationRefusal


@dataclass(frozen=True)
class RunCancellationCommandConflict:
    """This run's live attempt already carries a different command's cancel."""


@dataclass(frozen=True)
class RunCancellationRunMissing:
    pass


type RunCancellationResult = (
    RunCancellationAccepted
    | RunCancellationEndedRun
    | RunCancellationTerminalRetry
    | RunCancellationOvertakenBySuccess
    | RunCancellationNotCancellable
    | RunCancellationCommandConflict
    | RunCancellationRunMissing
    | DurableWriteUnavailable
    | DurableStateCorrupt
)


class AgentAttemptReader(Protocol):
    """Read one durable attempt back: all that workspace reconciliation needs."""

    def load(self, attempt_id: AgentAttemptId) -> AgentAttempt: ...


@dataclass(frozen=True, slots=True)
class KeptEvidence:
    """One bounded piece of a rejected attempt's evidence, and where it was kept.

    `artifact_hash` is absent for two different reasons a reader must be able to
    tell apart, and the second field is what tells them apart: nothing to keep
    at all leaves `retention_failure` empty too, while a publication that failed
    names why in it. `redacted` is true where `redact_credentials` found and
    replaced a credential shape before the material was published, so a reader
    knows the artifact is not the exact bytes.

    One shape for both pieces this evidence carries -- what a red check printed
    and what the attempt itself changed -- because they are kept the same way,
    fail the same way, and are read the same way; two near-identical field
    triples on one record would drift the day one of them grew a third reason.
    """

    artifact_hash: ArtifactHash | None
    redacted: bool = False
    retention_failure: str | None = None


NOTHING_TO_KEEP = KeptEvidence(None)
"""What a piece of evidence that never existed looks like: no address, no reason."""


@dataclass(frozen=True, slots=True)
class ProjectVerificationFailureEvidence:
    """What a failed verification's refusal names beyond the command and its exit.

    Composed once, by the caller that already ran the check and read what it
    printed -- the store turns this into the receipt's words, but it never
    reopens a process or a released workspace to learn what one already said.
    `summary_line` is absent where the retained tail carried no pytest summary
    to read.

    `output` is what the check itself printed (#1137). `candidate_diff` is the
    other half of the same question (#1156): a check that said no is only half
    an answer while nobody can see what it said no *to*, and the patch is kept
    as evidence exactly because the work behind it is not kept as a candidate.
    """

    summary_line: str | None
    duration_seconds: float
    output: KeptEvidence
    candidate_diff: KeptEvidence


class AgentAttemptStore(AgentAttemptReader, Protocol):
    def iter_driverless_attempts(self, page_limit: PageLimit) -> Iterator[AgentAttempt]:
        """Every nonterminal attempt no live workflow is driving any more.

        Answered by the durable runtime, because only it knows which of its
        workflows are still going to run. An attempt whose driver is merely
        waiting to be recovered is *not* driverless: recovery will move it, and
        stopping it would take work away from the machine that owns it.

        The iteration reads one bounded page at a time. Its cursor is local to
        this call, so a restart begins again from durable truth rather than from
        scan progress that never became product state.
        """
        ...

    def prepare(self, execution: AgentAttemptExecution) -> AgentAttempt: ...

    def refuse_unstartable_node(
        self, execution: AgentAttemptExecution, refusal: AgentExecutionRefusal
    ) -> AgentExecutorBindingRefusalResult: ...

    def claim(self, execution: AgentAttemptExecution) -> AgentAttemptClaimResult: ...

    def record_permission_decision(self, receipt: PermissionReceipt) -> None:
        """Keep one answered permission question, before its answer is handed out.

        The authorisation ledger of ADR 0020 §2: a permission is authorisation,
        and an authorisation written after the effect it allowed is none. A
        refusal is a row for the same reason a grant is -- what a run refused is
        a fact about that run, not an absence a reader has to infer.

        The same question of the same attempt is one row however often it is
        asked again: a recovered attempt re-runs its provider, which asks the
        same call under the same correlation id and is answered by the same
        bound policy. A *different* answer under that id is not a second
        receipt but a contradiction, and it is raised.

        Raises rather than returning a refusal, because a caller holding an
        answer it could not keep has no decision to give: the write is what
        makes the decision one.
        """
        ...

    def complete_success(
        self,
        execution: AgentAttemptExecution,
        result: AgentExecutionResult,
        redemption: ToolRedemptionReceipt | None = None,
        verification_failure_evidence: ProjectVerificationFailureEvidence | None = None,
        candidate_diff: str | None = None,
    ) -> AgentAttemptSucceeded | AgentAttemptFailed:
        """Keep this attempt's terminal truth, and what its grant redeemed with it.

        `redemption` is absent for an attempt whose node pinned no tool grant. One
        that redeemed a grant hands its evidence in here rather than writing it
        beside this call, so a succeeded attempt and the proof of what its tool
        ran become durable together or not at all.

        A decoded result whose bytes the node's own pinned schema refuses is not
        an error of this call: the attempt ends `FAILED` under
        `OUTPUT_SCHEMA_REFUSED` with the refusal durably named, and the failed
        outcome is returned rather than raised. A granted project verification
        that exits nonzero is the same kind of named failure, under
        `PROJECT_VERIFICATION_FAILED`, with how the command ended in the receipt
        reason and without a `tool_redemptions` row. `verification_failure_evidence`
        is what that reason names beyond the exit code the redemption already
        carries -- pytest's own summary, where the check's output was kept, and
        where the patch it rejected was kept -- and it is read only on that one
        ending; every other ending ignores it.

        `candidate_diff` is the patch the kept candidate is, for the node that
        judges it next. It is the atelier's own reading of the tree the attempt
        left, never the provider's word, and it reaches the node's value only
        where that node's declared output schema names a property for it
        (`contracts/candidate_reports.py`); the agent receipt keeps the exact
        bytes the provider answered either way. Absent means there was no patch
        to read -- no project, or a tree the check left as the pin had it. A
        patch that could not be read is not one of the ways to reach here at
        all: nothing is anchored before it is read, so that attempt ends as a
        candidate that was not kept.
        """
        ...

    def complete_known_failure(
        self,
        execution: AgentAttemptExecution,
        exit_signature: ProcessExitSignature,
        transcript: AttemptTranscript | None = None,
    ) -> AgentAttemptFailed:
        """End this attempt on the process that produced no usable answer.

        `exit_signature` is what the supervision saw -- how the child ended and
        the standard error it left -- and it is durably named in the node
        receipt this write keeps, because otherwise the only record of why a
        provider died is a log line nobody kept.

        `transcript` is what the executor read of what the process itself wrote,
        kept under its own address. It is absent where the executor decoded
        nothing, and a real failed run showed why that absence must not be the
        rule: an exit code beside an empty standard error explains nothing at
        all (#733).
        """
        ...

    def complete_agent_refusal(
        self, execution: AgentAttemptExecution, reason: str
    ) -> AgentAttemptFailed:
        """End an armed attempt whose executor refused it before launch."""
        ...

    def complete_project_verification_failure(
        self,
        execution: AgentAttemptExecution,
        verdict: str,
        transcript: AttemptTranscript | None = None,
    ) -> AgentAttemptFailed:
        """End an armed attempt whose granted verification never produced an exit.

        `verdict` names why -- the declared timeout, not an invented exit code.
        The attempt ends on the same `PROJECT_VERIFICATION_FAILED` seam a
        nonzero exit uses, without a `tool_redemptions` row.

        `transcript` is what the provider did before a check that never
        answered, and it is kept for the reason every other ending keeps its
        own: the agent's work is not undone by the verification's silence, and
        whoever reads this failure has to see what was produced before deciding
        whether the check or the work is at fault.
        """
        ...

    def complete_candidate_unchanged(
        self,
        execution: AgentAttemptExecution,
        verdict: str,
        transcript: AttemptTranscript | None = None,
    ) -> AgentAttemptFailed:
        """End an armed attempt that left the tree its pin named untouched.

        No check ran and no grant was redeemed, so there is no exit code and no
        `tool_redemptions` row: what ended this attempt is the work itself, and
        `verdict` says so in the words of the caller that compared the two trees
        -- the pinned tree it still holds, and what the provider claimed to have
        done to it. Those two beside each other are the whole point: an answer
        describing work that is not there is a lie the run must state, not
        absorb.

        `transcript` is what the provider did on the way to answering nothing,
        and it is the only place a reader can look for why.
        """
        ...

    def complete_candidate_capture_failure(
        self,
        execution: AgentAttemptExecution,
        verdict: str,
        transcript: AttemptTranscript | None = None,
        redemption: ToolRedemptionReceipt | None = None,
    ) -> AgentAttemptFailed:
        """End an armed attempt whose finished work could not be kept.

        Everything else about this attempt went right, so `verdict` carries the
        store's own words about why the work was not kept rather than an exit
        code nothing produced. The work is gone with the workspace, and that is
        exactly why this ending exists: an attempt that cannot show what it made
        must not be readable as one that succeeded.

        `transcript` is what the provider did before the capture, and once the
        directory is released it is the only remaining evidence that the work
        was ever done -- the strongest reason of any ending here to keep it.

        `redemption` is the evidence of a granted check that *passed* before the
        capture failed, and it becomes durable together with this ending. It is
        absent where the node pinned no grant. The check's own result is a fact
        about the project, not about the keeping: an ending that threw it away
        would leave a run that verified clean indistinguishable from one that
        never ran a check at all.
        """
        ...

    def request_cancellation(
        self, request: CancelAgentAttemptRequest
    ) -> AgentAttemptCancellationResult: ...

    def attest_cancellation_cleanup(
        self,
        request: CancelAgentAttemptRequest,
        disposition: AgentAttemptCancellationDisposition,
        process_owner_id: AgentProcessOwnerId | None,
        watchdog_generation_id: WatchdogGenerationId | None,
    ) -> AgentAttemptCancellationAccepted: ...

    def mark_cancellation_owner_not_local(
        self, request: CancelAgentAttemptRequest
    ) -> AgentAttempt: ...

    def bind_watchdog(
        self,
        execution: AgentAttemptExecution,
        process_owner_id: AgentProcessOwnerId,
        watchdog_generation_id: WatchdogGenerationId,
    ) -> AgentAttempt: ...

    def observe_process(
        self,
        execution: AgentAttemptExecution,
        process_owner_id: AgentProcessOwnerId,
        watchdog_generation_id: WatchdogGenerationId,
    ) -> AgentAttempt: ...


class TransactionalAgentAttemptCanceller(Protocol):
    def request_cancellation(
        self, request: CancelAgentAttemptRequest
    ) -> AgentAttemptCancellationResult: ...

    def request_run_cancellation(
        self, request: CancelRunRequest
    ) -> RunCancellationResult: ...
