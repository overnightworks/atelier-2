from dataclasses import dataclass
from typing import assert_never

from atelier2.application.refusals import DurableStateCorrupt, WriteUnavailable
from atelier2.contracts.executions import (
    NodeExecutionId,
    SubmitWaitAnswerRequest,
    WaitAnswerActor,
    WaitAnswerSnapshot,
    WaitAnswerState,
)
from atelier2.contracts.runs import RunId, WorkflowRevisionHash
from atelier2.ports.durable_runs import (
    DurableAnswerActorMismatch,
    DurableAnswerCreated,
    DurableAnswerExisting,
    DurableAnswerNodeMissing,
    DurableAnswerNotAdmitted,
    DurableAnswerRevisionConflict,
    DurableAnswerRoundAnswered,
    DurableAnswerRunMissing,
    DurableAnswerStale,
    DurableAnswerStateConflict,
    DurableWriteUnavailable,
    TransactionalWaitAnswerer,
)
from atelier2.ports.durable_runs import (
    DurableStateCorrupt as PortDurableStateCorrupt,
)


@dataclass(frozen=True)
class AnswerAcceptedPending:
    snapshot: WaitAnswerSnapshot


@dataclass(frozen=True)
class AnswerExistingPending:
    snapshot: WaitAnswerSnapshot


@dataclass(frozen=True)
class AnswerExistingApplied:
    snapshot: WaitAnswerSnapshot


@dataclass(frozen=True)
class AnswerActorMismatch:
    expected_actor: WaitAnswerActor


@dataclass(frozen=True)
class RunMissing:
    pass


@dataclass(frozen=True)
class NodeMissing:
    pass


@dataclass(frozen=True)
class AnswerRevisionConflict:
    pass


@dataclass(frozen=True)
class AnswerStateConflict:
    """The run is not waiting for this answer.

    Two ways reach it: the run is not waiting at all, or the round named
    already holds a different answer. Whoever asked does the same either way --
    reload the run and answer only what it is waiting for now -- so the two
    share one word.
    """


@dataclass(frozen=True)
class AnswerStale:
    pass


type AnswerWaitResult = (
    UnanswerableWait
    | AnswerAcceptedPending
    | AnswerExistingPending
    | AnswerExistingApplied
    | AnswerActorMismatch
    | RunMissing
    | NodeMissing
    | AnswerRevisionConflict
    | AnswerStateConflict
    | AnswerStale
    | WriteUnavailable
    | DurableStateCorrupt
)


@dataclass(frozen=True)
class UnanswerableWait:
    """The authored answer is no answer to this wait.

    Two ways reach it and both mean the same thing to whoever asked: the values
    make no submission at all, or the waiting node's own declaration does not
    admit the bytes. Which of the two it was is not a distinction an operator
    acts on differently -- either way the answer has to be rewritten -- so they
    share one word rather than two the caller would have to tell apart.
    """


def answer_wait_result(
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    node_id: str,
    expected_node_execution_id: NodeExecutionId,
    actor: WaitAnswerActor,
    answer_bytes: bytes,
    answerer: TransactionalWaitAnswerer,
) -> AnswerWaitResult:
    """Answer one waiting node, from the values an author supplied.

    Building the submission is part of the decision rather than a step before it:
    a submission that cannot be built at all -- a node nobody named -- refuses the
    answer in the same vocabulary as everything else that can go wrong here, and
    the store is not asked.

    Whether the bytes are an answer *this* node accepts is asked of the store
    rather than decided here. A V1 or V2 Wait node admits the canonical text of an
    integer and a V3 one admits whatever the schema its author pinned admits;
    which of the two applies is a fact about the node, and this layer cannot read
    a node. Deciding it here would mean owning one format's vocabulary and
    refusing the other's valid answers under it. The answer comes back as the
    same `UnanswerableWait` either way, so what a caller is told did not move
    when the decision did.
    """
    try:
        request = SubmitWaitAnswerRequest(
            run_id,
            revision_hash,
            node_id,
            expected_node_execution_id,
            actor,
            answer_bytes,
        )
    except (TypeError, ValueError):
        return UnanswerableWait()
    result = answerer.submit_result(request)
    match result:
        case DurableAnswerCreated(snapshot):
            return AnswerAcceptedPending(snapshot)
        case DurableAnswerExisting(snapshot):
            if snapshot.state is WaitAnswerState.PENDING:
                return AnswerExistingPending(snapshot)
            return AnswerExistingApplied(snapshot)
        case DurableAnswerActorMismatch(expected_actor):
            return AnswerActorMismatch(expected_actor)
        case DurableAnswerRunMissing():
            return RunMissing()
        case DurableAnswerNodeMissing():
            return NodeMissing()
        case DurableAnswerRevisionConflict():
            return AnswerRevisionConflict()
        case DurableAnswerStateConflict() | DurableAnswerRoundAnswered():
            return AnswerStateConflict()
        case DurableAnswerStale():
            return AnswerStale()
        case DurableAnswerNotAdmitted():
            return UnanswerableWait()
        case DurableWriteUnavailable():
            return WriteUnavailable()
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)
