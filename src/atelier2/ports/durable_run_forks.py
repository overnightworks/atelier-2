from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from atelier2.contracts.agent_modes import AgentModeMismatch
from atelier2.contracts.run_bindings import RunV3
from atelier2.contracts.run_forks import RunFork
from atelier2.contracts.runs import RunId


@dataclass(frozen=True)
class ForkRunRequest:
    origin_run_id: RunId
    idempotency_key: str
    restart_from_node_id: str


@dataclass(frozen=True)
class DurableRunForkCreated:
    fork: RunFork
    run: RunV3


@dataclass(frozen=True)
class DurableRunForkExisting:
    fork: RunFork
    run: RunV3


@dataclass(frozen=True)
class DurableRunForkOriginMissing:
    pass


@dataclass(frozen=True)
class DurableRunForkOriginNotTerminal:
    pass


@dataclass(frozen=True)
class DurableRunForkNodeMissing:
    pass


@dataclass(frozen=True)
class DurableRunForkLoopUnsupported:
    pass


@dataclass(frozen=True)
class DurableRunForkPrefixNotReusable:
    pass


@dataclass(frozen=True)
class DurableRunForkCommandConflict:
    pass


@dataclass(frozen=True)
class DurableRunForkExecutorUnavailable:
    pass


@dataclass(frozen=True)
class DurableRunForkCapabilityUnavailable:
    pass


@dataclass(frozen=True)
class DurableRunForkWriteUnavailable:
    detail: str | None = None


@dataclass(frozen=True)
class DurableRunForkStateCorrupt:
    pass


type DurableRunForkResult = (
    DurableRunForkCreated
    | DurableRunForkExisting
    | DurableRunForkOriginMissing
    | DurableRunForkOriginNotTerminal
    | DurableRunForkNodeMissing
    | DurableRunForkLoopUnsupported
    | DurableRunForkPrefixNotReusable
    | DurableRunForkCommandConflict
    | DurableRunForkExecutorUnavailable
    | DurableRunForkCapabilityUnavailable
    | AgentModeMismatch
    | DurableRunForkWriteUnavailable
    | DurableRunForkStateCorrupt
)


class DurableRunForker(Protocol):
    def fork_run(self, request: ForkRunRequest) -> DurableRunForkResult: ...
