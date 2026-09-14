from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never

from atelier2.application.refusals import DurableStateCorrupt, WriteUnavailable
from atelier2.contracts.agent_modes import AgentModeMismatch
from atelier2.contracts.run_bindings import RunV3
from atelier2.contracts.run_forks import RunFork
from atelier2.contracts.runs import RunId
from atelier2.ports.durable_run_forks import (
    DurableRunForkCapabilityUnavailable,
    DurableRunForkCommandConflict,
    DurableRunForkCreated,
    DurableRunForkDocumentNotExecutable,
    DurableRunForker,
    DurableRunForkExecutorUnavailable,
    DurableRunForkExisting,
    DurableRunForkLoopUnsupported,
    DurableRunForkNodeMissing,
    DurableRunForkOriginMissing,
    DurableRunForkOriginNotTerminal,
    DurableRunForkPrefixNotReusable,
    DurableRunForkStateCorrupt,
    DurableRunForkWriteUnavailable,
    ForkRunRequest,
)


@dataclass(frozen=True)
class RunForkCreated:
    fork: RunFork
    run: RunV3


@dataclass(frozen=True)
class RunForkExisting:
    fork: RunFork
    run: RunV3


@dataclass(frozen=True)
class RunForkOriginMissing:
    pass


@dataclass(frozen=True)
class RunForkOriginNotTerminal:
    pass


@dataclass(frozen=True)
class RunForkNodeMissing:
    pass


@dataclass(frozen=True)
class RunForkLoopUnsupported:
    pass


@dataclass(frozen=True)
class RunForkPrefixNotReusable:
    pass


@dataclass(frozen=True)
class RunForkCommandConflict:
    pass


@dataclass(frozen=True)
class RunForkExecutorUnavailable:
    pass


@dataclass(frozen=True)
class RunForkCapabilityUnavailable:
    pass


@dataclass(frozen=True)
class RunForkDocumentNotExecutable:
    """This build refuses to start the document the origin run was started under."""

    reason: str


type ForkRunResult = (
    RunForkCreated
    | RunForkExisting
    | RunForkOriginMissing
    | RunForkOriginNotTerminal
    | RunForkNodeMissing
    | RunForkLoopUnsupported
    | RunForkPrefixNotReusable
    | RunForkCommandConflict
    | RunForkExecutorUnavailable
    | RunForkCapabilityUnavailable
    | RunForkDocumentNotExecutable
    | AgentModeMismatch
    | WriteUnavailable
    | DurableStateCorrupt
)


def fork_run(
    origin_run_id: RunId,
    idempotency_key: str,
    restart_from_node_id: str,
    forker: DurableRunForker,
) -> ForkRunResult:
    result = forker.fork_run(
        ForkRunRequest(origin_run_id, idempotency_key, restart_from_node_id)
    )
    match result:
        case DurableRunForkCreated(fork, run):
            return RunForkCreated(fork, run)
        case DurableRunForkExisting(fork, run):
            return RunForkExisting(fork, run)
        case DurableRunForkOriginMissing():
            return RunForkOriginMissing()
        case DurableRunForkOriginNotTerminal():
            return RunForkOriginNotTerminal()
        case DurableRunForkNodeMissing():
            return RunForkNodeMissing()
        case DurableRunForkLoopUnsupported():
            return RunForkLoopUnsupported()
        case DurableRunForkPrefixNotReusable():
            return RunForkPrefixNotReusable()
        case DurableRunForkCommandConflict():
            return RunForkCommandConflict()
        case DurableRunForkExecutorUnavailable():
            return RunForkExecutorUnavailable()
        case DurableRunForkCapabilityUnavailable():
            return RunForkCapabilityUnavailable()
        case DurableRunForkDocumentNotExecutable(reason):
            return RunForkDocumentNotExecutable(reason)
        case AgentModeMismatch() as refused:
            return refused
        case DurableRunForkWriteUnavailable(detail):
            return WriteUnavailable(detail)
        case DurableRunForkStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)
