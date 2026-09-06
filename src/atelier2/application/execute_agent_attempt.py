from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace

from atelier2.application.publish_artifact import (
    ArtifactPublicationCreated,
    ArtifactPublicationExisting,
    ArtifactPublicationInvalid,
    publish_artifact,
)
from atelier2.application.refusals import DurableStateCorrupt, WriteUnavailable
from atelier2.contracts.agent_attempts import (
    AgentAttemptFailureCode,
    AgentAttemptId,
)
from atelier2.contracts.agent_permissions import (
    PermissionDecision,
    PermissionPolicyRevision,
    PermissionReceipt,
    PermissionRequest,
    PolicyPermissionDecider,
)
from atelier2.contracts.agents import AgentExecutionResult
from atelier2.contracts.candidate_reports import patch_safe_to_show
from atelier2.contracts.executions import AgentAttemptExecution
from atelier2.contracts.process_endings import (
    ProcessExitSignature,
    receipted_agent_answer,
)
from atelier2.contracts.secret_redaction import (
    RedactedText,
    redact_an_unopened_credential,
    redact_credentials,
)
from atelier2.contracts.tool_grants_v3 import (
    ToolGrantCapability,
    ToolGrantCapabilityNotRedeemed,
    ToolRedemptionReceipt,
)
from atelier2.contracts.when import RecordedAt, recorded_instant
from atelier2.ports.agent_attempts import (
    NOTHING_TO_KEEP,
    AgentAttemptClaimedByThisCall,
    AgentAttemptExecutionOutcome,
    AgentAttemptFailed,
    AgentAttemptStore,
    AgentAttemptSucceeded,
    KeptEvidence,
    ProjectVerificationFailureEvidence,
)
from atelier2.ports.agent_executions import (
    AgentAttemptWorkspaceLease,
    AgentAttemptWorkspaceOwner,
    AgentExecutionFailure,
    AgentExecutionPreflightRefusal,
    AgentExecutorV2,
    AgentProcessCompletion,
    AgentProcessInvocation,
    AgentSession,
)
from atelier2.ports.artifacts import ArtifactPublisher
from atelier2.ports.candidate_store import (
    MAXIMUM_CANDIDATE_DIFF_BYTES,
    CandidateNotKept,
    LeasedWorkingTree,
)
from atelier2.ports.project_verification import (
    PinnedProjectSource,
    ProjectVerificationOutcome,
    ProjectVerificationUnavailable,
)
from atelier2.ports.provider_conversations import (
    ProviderFilesystemAccess,
    ProviderFilesystemAuthority,
)

_LOG = logging.getLogger("atelier2")

type WorkspaceFileAccessOpener = Callable[
    [AgentAttemptWorkspaceLease, int, ProviderFilesystemAuthority],
    ProviderFilesystemAccess,
]
"""The deployment's fenced file adapter, anchored to one lease, one read ceiling
and one authority. Handed in because this layer must not reach the disk, and
bound over whatever access the executor opened its conversation with: an
executor reaching files on its own would be granting itself the workspace. The
ceiling is the driver's reply bound, since nothing wider could be answered."""


@dataclass(frozen=True, slots=True)
class _DecisionsKeptBeforeTheyAnswer:
    """The bound policy, answering only questions it has already written down.

    ADR 0020 §3 makes the receipt the authorisation itself, so the order here is
    the whole contract: the policy decides, the ledger keeps that decision, and
    only then does the answer travel back to the provider. A write that fails
    therefore produces no answer at all -- the error rises through the session,
    this attempt's driver stops and reaps its process, and the `LAUNCH_ARMED`
    attempt is left to the convergence that owns every attempt whose driver is
    gone. What must never happen is the opposite order: a provider acting on a
    permission whose only record died with the process that granted it.

    One object serves both seams that ask: the session relaying a permission
    question, and the file access asking from inside its fence. `refuse` keeps
    a question the policy is never put -- a path the fence found not to be the
    workspace -- as a refusal under the same revision, never as a grant.
    """

    attempt_id: AgentAttemptId
    policy: PolicyPermissionDecider
    store: AgentAttemptStore
    clock: Callable[[], RecordedAt]

    def decide(self, request: PermissionRequest) -> PermissionDecision:
        return self._kept(request, self.policy.decide(request))

    def refuse(self, request: PermissionRequest) -> PermissionDecision:
        return self._kept(request, self.policy.refuse(request))

    def _kept(
        self, request: PermissionRequest, decision: PermissionDecision
    ) -> PermissionDecision:
        self.store.record_permission_decision(
            PermissionReceipt.of(self.attempt_id, request, decision, self.clock())
        )
        return decision


def execute_agent_attempt(
    execution: AgentAttemptExecution,
    executor: AgentExecutorV2,
    store: AgentAttemptStore,
    session: AgentSession,
    workspaces: AgentAttemptWorkspaceOwner,
    project: PinnedProjectSource | None = None,
    artifacts: ArtifactPublisher | None = None,
    clock: Callable[[], RecordedAt] = recorded_instant,
    *,
    permissions: PermissionPolicyRevision,
    workspace_files: WorkspaceFileAccessOpener,
) -> AgentAttemptExecutionOutcome:
    """Invoke only after this live call durably wins the launch boundary.

    Preparing the provider command and attesting the scratch root happen before
    the claim, so a call that loses it leaves no workspace behind: the directory
    is created only once this call's own compare-and-set has won. A prepared
    command's private channel is released on every path, including a failed
    preflight. The workspace is removed only once both facts that make removal
    safe are in hand -- the completion proving no process or descendant of this
    attempt is left, and the durable terminal attempt.

    `project` is absent where this runtime was pointed at no project. Where one
    is pinned, the tree that commit names is unpacked into the leased directory
    between the claim and the provider, so the work happens on exactly the
    material the durable binding pinned rather than on whatever a checkout holds
    now -- and a pin the source can no longer answer for refuses beside the
    scratch root, before any claim.

    A node that pinned a tool grant has it redeemed here, in that same lease
    after the provider produced the work the verification is about. What the
    project declares is read at the pin and attested before the claim too, so a
    project that declares no verification refuses before anything runs. A
    verification that does not answer within its declared deadline after the
    claim ends the attempt `FAILED` under `PROJECT_VERIFICATION_FAILED` rather
    than leaving it `LAUNCH_ARMED`.

    A grant is redeemed only once the tree the provider left has been read
    against the pin. An attempt that changed nothing has nothing a check could
    verify, so it ends `FAILED` under `CANDIDATE_UNCHANGED` in seconds instead
    of paying a whole test suite to learn that the pinned tree still passes
    (#1156).

    `permissions` is the authorisation this execution runs under, bound by
    whoever dispatched it; the decider handed to the session answers every
    question against exactly that revision, and keeps each answer in the store
    before the provider is told it. It has no default: an authority nobody
    named is an authority nobody bound, and this call would then run under one
    its dispatcher never chose.

    `artifacts` is where a verification that exits nonzero publishes the tail of
    what it printed and the patch it rejected, so the words the attempt ends
    with can name where that proof lives rather than just the exit code (#1137,
    #1156). It is required exactly where a project is pinned with a redeemable
    grant; every other attempt never reads it. A runtime pinned with a grant but
    wired with no publisher refuses beside the verification's own preflight,
    before any provider process, so a check that later exits nonzero never
    discovers the missing door while trying to keep what it already ran.

    What the attempt made is kept last of all, after any granted check has run
    and before the attempt is completed. Last, because the candidate must be the
    state the verification passed on rather than the state before it; before,
    because the lease is released once this attempt is terminal and nothing can
    recover the work afterwards. The two writes cannot share a transaction --
    the candidate is a git object and the attempt a durable row -- so the order
    is what carries the invariant: no attempt is ever `SUCCEEDED` without its
    work kept, and a capture that fails ends it `FAILED` under
    `CANDIDATE_CAPTURE_FAILED` instead.
    """

    store.prepare(execution)
    try:
        command = executor.prepare_process(execution.request)
    except AgentExecutionPreflightRefusal as refusal:
        claim = store.claim(execution)
        if isinstance(claim, AgentAttemptClaimedByThisCall):
            return store.complete_agent_refusal(execution, refusal.reason)
        return claim
    try:
        workspaces.preflight()
        _preflight_pinned_project(project, artifacts)
        session.prepare(execution)
        claim = store.claim(execution)
        if not isinstance(claim, AgentAttemptClaimedByThisCall):
            if isinstance(claim, (AgentAttemptSucceeded, AgentAttemptFailed)):
                session.finalize(execution)
            return claim
        lease = workspaces.acquire(execution.attempt_id)
        if project is not None:
            project.source.materialize(project.pin, lease)
        authority = _DecisionsKeptBeforeTheyAnswer(
            execution.attempt_id, PolicyPermissionDecider(permissions), store, clock
        )
        conversation = executor.open_conversation(execution.request, command, lease)
        if conversation is not None:
            conversation = replace(
                conversation,
                files=workspace_files(
                    lease, conversation.driver.bounds.maximum_reply_bytes, authority
                ),
            )
        invocation = AgentProcessInvocation(command, lease, conversation)
        completion = session.launch_and_wait(execution, invocation, authority)
        result = _with_recorded_transcript(
            executor.decode_process_completion(invocation, completion), clock
        )
        if isinstance(result, AgentExecutionFailure):
            outcome = _ended_by_the_process(execution, result, completion, store)
        else:
            outcome = _ended_after_the_provider(
                execution, result, lease, project, store, artifacts
            )
            if isinstance(outcome, AgentAttemptFailed):
                _log_named_failure(execution, outcome.attempt.failure_code)
        session.finalize(execution)
        workspaces.release(execution.attempt_id)
    finally:
        executor.release_credential_channel(command)
    return outcome


def _preflight_pinned_project(
    project: PinnedProjectSource | None, artifacts: ArtifactPublisher | None
) -> None:
    """Attest the pin and its declared verification before anything is claimed."""
    if project is None:
        return
    project.source.attest(project.pin)
    if project.grant is None:
        return
    project.verifications.preflight(project.pin)
    if (
        project.grant.capability is ToolGrantCapability.RUN_PROJECT_VERIFICATION
        and artifacts is None
    ):
        raise RuntimeError(
            "a project pinned a redeemable run-project-verification "
            "grant, but this attempt was given no artifact "
            "publisher to keep a failed verification's output with"
        )


def _ended_by_the_process(
    execution: AgentAttemptExecution,
    result: AgentExecutionFailure,
    completion: AgentProcessCompletion,
    store: AgentAttemptStore,
) -> AgentAttemptFailed:
    """The named ending of an attempt whose process gave the executor no answer.

    The completion, not the executor's verdict, carries how the child ended: the
    executor answers whether it could use the process, and only supervision saw
    the exit code and the standard error that says why. Naming the ending here
    keeps that one reading of one process rather than asking every provider to
    repeat it; what the process wrote travels on the failure the executor returned.
    """
    if result.code is not AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY:
        raise ValueError("executor returned an unsupported known failure")
    _warn_attempt(execution, "agent_attempt_failed", "failed.")
    return store.complete_known_failure(
        execution,
        ProcessExitSignature(completion.return_code, completion.standard_error),
        result.transcript,
    )


def _ended_after_the_provider(
    execution: AgentAttemptExecution,
    result: AgentExecutionResult,
    lease: AgentAttemptWorkspaceLease,
    project: PinnedProjectSource | None,
    store: AgentAttemptStore,
    artifacts: ArtifactPublisher | None,
) -> AgentAttemptExecutionOutcome:
    """How an attempt whose provider answered ends, in the order that costs least.

    The tree is read first, before anything expensive is spent on it. An attempt
    that left the pinned tree exactly as it found it has nothing for a check to
    verify: running one would spend a project's whole test suite to learn that
    the pin still passes, and would then write `PROJECT_VERIFICATION_FAILED`
    over an attempt where no verification was ever the truth (#1156). Naming
    that ending is what turns ten minutes of machine and provider into seconds.

    Every ending here is reached through the store rather than raised, because
    the claim has already won: an exception escaping this leaves the attempt
    `LAUNCH_ARMED`, which no operator and no replay can resolve.
    """

    try:
        written = _the_tree_the_attempt_left(lease, project)
    except CandidateNotKept:
        # Reading the tree only ever saves work, so a store that cannot answer
        # costs the saving and not the attempt: everything below runs exactly as
        # it did before this reading existed. The refusal is not swallowed --
        # every ending below reads the tree again, after the check, and says
        # what the same store answered then.
        written = None
    if written is not None and not written.changed_the_pinned_tree:
        return store.complete_candidate_unchanged(
            execution, _unchanged_verdict(written, result), result.transcript
        )
    try:
        redeemed = _redeemed(execution, lease, project)
    except ProjectVerificationUnavailable as error:
        # The provider had already answered when the check went silent, so its
        # steps travel into this ending too rather than this being the one path
        # that drops them.
        return store.complete_project_verification_failure(
            execution, _verification_unavailable_verdict(error), result.transcript
        )
    if redeemed is not None and not redeemed.receipt.satisfied_the_project:
        return _ended_under_a_red_check(
            execution, result, lease, project, store, artifacts, redeemed
        )
    return _ended_under_a_passed_check(
        execution,
        result,
        lease,
        project,
        store,
        redeemed.receipt if redeemed is not None else None,
    )


def _ended_under_a_red_check(
    execution: AgentAttemptExecution,
    result: AgentExecutionResult,
    lease: AgentAttemptWorkspaceLease,
    project: PinnedProjectSource | None,
    store: AgentAttemptStore,
    artifacts: ArtifactPublisher | None,
    redeemed: _RedeemedGrant,
) -> AgentAttemptExecutionOutcome:
    """How an attempt whose granted check said no ends, with what it did beside it.

    A check that said no has already decided this attempt, so nothing is
    captured and nothing else may rename the ending. Capturing first would keep
    work the project rejected and, worse, let the loss of that work overwrite
    the verdict: the attempt would read `CANDIDATE_CAPTURE_FAILED` and carry a
    redemption saying the check failed. The store owns what a nonzero redemption
    means; this only refuses to reach past it. What the attempt did is still
    kept -- as a bounded patch an operator can read, never as a candidate a
    later run could take.
    """

    return store.complete_success(
        execution,
        result,
        redeemed.receipt,
        _published_verification_failure_evidence(
            redeemed.outcome,
            _kept_candidate_diff(lease, project, artifacts),
            artifacts,
        ),
    )


def _ended_under_a_passed_check(
    execution: AgentAttemptExecution,
    result: AgentExecutionResult,
    lease: AgentAttemptWorkspaceLease,
    project: PinnedProjectSource | None,
    store: AgentAttemptStore,
    redemption: ToolRedemptionReceipt | None,
) -> AgentAttemptExecutionOutcome:
    """How an attempt the project accepted ends, once its work is really kept.

    The tree is read again here, after the check rather than before it: a
    verification command runs in the same workspace and may write into it, so
    the tree that is kept and the patch a reviewer judges are both this reading
    and not the one taken before the check. A check that reverted every change
    leaves an attempt that changed nothing after all, and it ends under that
    word instead of succeeding with no patch to show.

    Keeping is anchored last, so a store that cannot print the patch has kept
    nothing either -- which is what makes `CANDIDATE_CAPTURE_FAILED` the true
    word for that ending.
    """

    try:
        verified = _the_tree_the_attempt_left(lease, project)
        if verified is not None and not verified.changed_the_pinned_tree:
            return store.complete_candidate_unchanged(
                execution, _unchanged_verdict(verified, result), result.transcript
            )
        candidate_diff = _candidate_diff_the_next_node_reads(project, verified)
        _keep_what_the_attempt_made(lease, project)
    except CandidateNotKept as refusal:
        # The work is gone either way, but a named failure is a fact an operator
        # can act on. What the granted check proved before the loss is kept with
        # it -- the check passed, and that stays true however the keeping ended.
        return store.complete_candidate_capture_failure(
            execution, str(refusal), result.transcript, redemption
        )
    return store.complete_success(
        execution, result, redemption, candidate_diff=candidate_diff
    )


def _log_named_failure(
    execution: AgentAttemptExecution, failure: AgentAttemptFailureCode | None
) -> None:
    """Say once, in the operator's log, which named ending this attempt reached.

    A failed attempt carrying no code at all is the same defect as one carrying
    a code nothing here names, and is refused the same way: this log line is the
    only place the two are read, so neither may pass as the other.
    """

    match failure:
        case AgentAttemptFailureCode.OUTPUT_SCHEMA_REFUSED:
            event = "agent_attempt_output_refused"
            detail = (
                "produced an output its own schema refuses; "
                "the refusal is durably named."
            )
        case AgentAttemptFailureCode.AGENT_REFUSED:
            event = "agent_attempt_refused"
            detail = "declared a refusal; the refusal is durably named."
        case AgentAttemptFailureCode.PROJECT_VERIFICATION_FAILED:
            event = "agent_attempt_project_verification_failed"
            detail = (
                "project verification ended unsuccessfully; "
                "the failure is durably named."
            )
        case AgentAttemptFailureCode.CANDIDATE_CAPTURE_FAILED:
            event = "agent_attempt_candidate_capture_failed"
            detail = (
                "did the work and none of it could be kept; the loss is durably named."
            )
        case AgentAttemptFailureCode.PRODUCED_VALUE_REFUSED:
            event = "agent_attempt_produced_value_refused"
            detail = (
                "produced a value its own schema refuses; the refusal is durably named."
            )
        case AgentAttemptFailureCode.CANDIDATE_UNCHANGED:
            event = "agent_attempt_candidate_unchanged"
            detail = (
                "left the pinned tree untouched; no verification was started "
                "and the ending is durably named."
            )
        case _:
            raise ValueError("attempt ended under an unnamed failure")
    _warn_attempt(execution, event, detail)


def _warn_attempt(execution: AgentAttemptExecution, event: str, ending: str) -> None:
    _LOG.warning(
        "Agent attempt %s on node %s of run %s %s",
        execution.attempt_id.value,
        execution.request.node_id,
        execution.request.run_id.value,
        ending,
        extra={
            "event": event,
            "run_id": execution.request.run_id.value,
            "node_id": execution.request.node_id,
            "attempt_id": execution.attempt_id.value,
        },
    )


def _the_tree_the_attempt_left(
    lease: AgentAttemptWorkspaceLease, project: PinnedProjectSource | None
) -> LeasedWorkingTree | None:
    """What stands in the lease now, named against the pin it started from.

    Nothing is anchored under the attempt: this reads the lease and must not by
    itself keep work no ending has decided to keep.

    Asked twice on one attempt, at the two moments its answer can differ.
    Before the granted check, because "changed nothing" is what saves paying
    for one at all. After it, because the check runs in the same workspace and
    a command that writes there leaves a tree the earlier reading never saw --
    and the patch handed to whoever judges the candidate has to be the tree
    that is kept, not the one that stood before the check.

    Asked only of an attempt that redeems a grant, because that is the only
    attempt for which "changed nothing" is a failure. A node that pinned no
    grant may honestly answer without touching a file -- a reviewer reading a
    candidate and judging it is exactly that -- and there is no verification
    cost to save there either. A runtime pointed at no project has no pin the
    work would be a change to and no store to name a tree in.
    """

    if project is None or project.grant is None:
        return None
    return project.candidates.written(project.pin, lease)


def _unchanged_verdict(written: LeasedWorkingTree, result: AgentExecutionResult) -> str:
    """Why this attempt ended, with what the provider claimed beside it.

    The two together are the whole evidence: a tree identical to the pin, and an
    answer describing work done to it. An operator reading only the first would
    suspect the runtime; reading both, they can see which of the two lied.
    """

    return (
        f"the workspace still holds the pinned tree {written.pin.tree}, so this "
        f"attempt changed nothing; the agent answered: "
        f"{receipted_agent_answer(result.output_bytes)}"
    )


def _with_recorded_transcript(
    result: AgentExecutionResult | AgentExecutionFailure,
    clock: Callable[[], RecordedAt],
) -> AgentExecutionResult | AgentExecutionFailure:
    """Stamp decoded events at the one boundary that records their transcript."""

    transcript = result.transcript
    if transcript is None:
        return result
    return replace(result, transcript=transcript.with_recorded_moment(clock()))


def _keep_what_the_attempt_made(
    lease: AgentAttemptWorkspaceLease, project: PinnedProjectSource | None
) -> None:
    """Anchor what this attempt made, where a reader can still find it later.

    Nothing comes back. The candidate is anchored under the attempt's own
    identity, so whoever holds the attempt can ask the store for it; carrying
    the tree's address into the durable row as well would be a second record of
    one fact, and two records of one fact can disagree.

    A runtime pointed at no project keeps nothing, because there is no store
    that could own the result and no pin the work would be a change to.
    """

    if project is None:
        return
    project.candidates.capture(project.pin, lease)


def _verification_unavailable_verdict(error: ProjectVerificationUnavailable) -> str:
    """How the granted check failed to complete, without inventing an exit code."""

    if error.timeout_seconds is None:
        return str(error)
    return f"timeout {error.timeout_seconds} seconds"


def _published_verification_failure_evidence(
    outcome: ProjectVerificationOutcome,
    candidate_diff: KeptEvidence,
    artifacts: ArtifactPublisher | None,
) -> ProjectVerificationFailureEvidence:
    """The two readable halves of a red ending, beside the exit code it already has.

    What the check printed, and what it was printed about. Neither is a
    candidate: the work the project rejected must not survive as something a
    later run could take, and the patch is kept precisely so that refusing to
    keep the work costs an operator nothing.
    """

    return ProjectVerificationFailureEvidence(
        outcome.summary_line,
        outcome.duration_seconds,
        _published_evidence(_tail_safe_to_show(outcome.output_tail), artifacts),
        candidate_diff,
    )


def _tail_safe_to_show(output_tail: bytes) -> RedactedText:
    """What a check printed last, made safe although its own beginning is cut off.

    An outcome retains the last bytes of both streams, so an armoured block a
    check printed can stand in it with its opening marker on the far side of
    that cut: key material and the closing marker that would have named it, and
    no shape recognising either. What stands ahead of such a close is key
    material, and goes with it.
    """

    printed = redact_credentials(output_tail.decode("utf-8", "replace"))
    kept = redact_an_unopened_credential(printed.text)
    return RedactedText(kept.text, printed.redacted or kept.redacted)


def _kept_candidate_diff(
    lease: AgentAttemptWorkspaceLease,
    project: PinnedProjectSource | None,
    artifacts: ArtifactPublisher | None,
) -> KeptEvidence:
    """The patch this attempt is, kept beside the check that rejected it.

    Read from the tree the check itself left, like the patch a passed check
    hands on: a command that writes into the workspace before it exits nonzero
    leaves a tree the reading before it never saw, and an operator asking what
    the check said no to is asking about that tree.

    A store that cannot answer degrades these words rather than ending the
    attempt: the check has already spoken, and losing the patch does not make
    its verdict less true. A runtime pointed at no project has no pin the work
    would be a patch against, so it keeps nothing.
    """

    try:
        written = _the_tree_the_attempt_left(lease, project)
        if project is None or written is None:
            return NOTHING_TO_KEEP
        patch = project.candidates.changes(written)
    except CandidateNotKept as refusal:
        return KeptEvidence(None, retention_failure=str(refusal))
    return _published_evidence(
        patch_safe_to_show(patch, MAXIMUM_CANDIDATE_DIFF_BYTES), artifacts
    )


def _candidate_diff_the_next_node_reads(
    project: PinnedProjectSource | None, written: LeasedWorkingTree | None
) -> str | None:
    """The same patch, read as text by whoever judges this candidate next (#1235).

    A reviewer in a headless call reaches no file and runs nothing, so the
    builder's own prose was its whole evidence and it answered `cannot-judge`.
    The patch is what turns that into a judgement, and it is the atelier's
    reading of the tree rather than the provider's account of it.

    A store that cannot print the patch is not answered around here: the
    refusal travels to the caller, which has not anchored anything yet and ends
    the attempt on it, because an attempt that hands a reviewer a report with
    the patch silently missing tells that reviewer the change was empty. A
    runtime pointed at no project, or an attempt whose tree was never named, has
    no patch to read.
    """

    if project is None or written is None:
        return None
    patch = patch_safe_to_show(
        project.candidates.changes(written), MAXIMUM_CANDIDATE_DIFF_BYTES
    )
    return patch.text or None


def _published_evidence(
    material: RedactedText, artifacts: ArtifactPublisher | None
) -> KeptEvidence:
    """Keep one bounded piece of a red ending where its refusal can point to it.

    Nothing is published where there was nothing to keep -- the artifact store
    refuses empty content by its own rule, and evidence naming no artifact is
    exactly as honest as one naming an empty one. Publication is idempotent by
    content, so a replay of this same attempt lands on the same address rather
    than growing a second copy. What arrives is already past its caller's
    redactor, and it says so, because this artifact is HTTP-readable durable
    material rather than a transcript already behind its own boundary -- and
    only the caller knows where it cut, which is the one order a credential
    straddling that cut survives.

    A publisher this runtime was never wired with is refused by preflight,
    before any provider work; `artifacts` is `None` here only if that invariant
    broke, which this treats as this function's own defect rather than
    repeating preflight's wiring refusal. A publisher that answers but refuses,
    cannot be reached, or answers with a state no write sequence could have
    produced degrades the attempt's words instead of abandoning it: the exit
    code, the command and the summary line still reach the receipt, with a note
    naming why this piece is not kept beside them.
    """

    if not material.text:
        return NOTHING_TO_KEEP
    if artifacts is None:
        raise AssertionError(
            "preflight refuses a redeemable grant with no artifact publisher "
            "before any provider work; reaching publication without one is a "
            "defect in this runtime, not a condition this attempt can name"
        )
    published = publish_artifact(material.text.encode("utf-8"), artifacts)
    match published:
        case ArtifactPublicationCreated(artifact) | ArtifactPublicationExisting(
            artifact
        ):
            return KeptEvidence(artifact.artifact_hash, material.redacted)
        case ArtifactPublicationInvalid() | WriteUnavailable() | DurableStateCorrupt():
            return KeptEvidence(
                None, material.redacted, _publication_failure_reason(published)
            )


def _publication_failure_reason(
    published: ArtifactPublicationInvalid | WriteUnavailable | DurableStateCorrupt,
) -> str:
    """Why a piece of this evidence could not be kept, in the reader's own words."""

    match published:
        case ArtifactPublicationInvalid(verdict):
            return str(verdict)
        case WriteUnavailable(detail):
            return detail if detail is not None else "artifact write unavailable"
        case DurableStateCorrupt():
            return "durable artifact state corrupt"


@dataclass(frozen=True)
class _RedeemedGrant:
    """A grant's receipt, beside the raw outcome it was built from.

    The receipt alone answers the store's question -- did the check pass --
    but composing a failed check's words needs what the receipt deliberately
    does not keep: the output itself, not just its hash. Both travel together
    so a caller with one always has the other, rather than reopening a process
    or a released workspace to get back what it already read.
    """

    receipt: ToolRedemptionReceipt
    outcome: ProjectVerificationOutcome


def _redeemed(
    execution: AgentAttemptExecution,
    lease: AgentAttemptWorkspaceLease,
    project: PinnedProjectSource | None,
) -> _RedeemedGrant | None:
    """What redeeming this node's grant ran, or nothing where no grant was pinned.

    Which redeemer answers is read from the capability the pinned grant
    actually names, not assumed: a grant is redeemed by the one redeemer its
    own capability reaches, never by whichever redeemer this runtime happens
    to carry. The verification runs before the attempt is durably terminal, so
    its evidence reaches the store in the same write that keeps the provider's
    own receipt: a redemption durably missing beside a succeeded attempt would
    say a grant was never redeemed, which is exactly the thing this evidence
    exists to answer.
    """
    if project is None or project.grant is None:
        return None
    grant = project.grant
    outcome = _redeemed_via_capability(grant.capability, project, lease)
    request = execution.request
    receipt = ToolRedemptionReceipt.of(
        request.node_execution_id,
        request.run_id,
        request.workflow_revision_hash,
        request.node_id,
        execution.attempt_id,
        grant,
        outcome.command,
        outcome.exit_code,
        outcome.standard_output_hash,
    )
    return _RedeemedGrant(receipt, outcome)


def _redeemed_via_capability(
    capability: ToolGrantCapability,
    project: PinnedProjectSource,
    lease: AgentAttemptWorkspaceLease,
) -> ProjectVerificationOutcome:
    """The one redeemer this exact capability reaches, run against this lease.

    `ToolGrantCapability` holds one EXEC-shaped member, so this dispatch has one
    case -- but the case is still asked for, rather than assumed, because the
    bug this dispatch exists to keep impossible is exactly a capability that
    used to reach this point unread and got redeemed as a project verification
    regardless of what it actually named. A capability this match does not
    recognize is refused by name instead: only an exec-shaped grant is bound
    into the lease this redeemer runs against -- the effect-shaped `open-pr` is
    redeemed as a platform effect elsewhere -- so a capability reaching this
    branch that this redeemer does not perform is a defect in the runtime that
    bound it, not a condition an attempt can recover from.
    """
    match capability:
        case ToolGrantCapability.RUN_PROJECT_VERIFICATION:
            return project.verifications.run(project.pin, lease)
        case _:
            raise ToolGrantCapabilityNotRedeemed(capability)
