from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from atelier2.contracts.hashing import Sha256Hash


class RevisionHashCollision(RuntimeError):
    """Durable bytes disagree with the document identified by their hash."""


class WorkflowRevisionHash(Sha256Hash):
    """The immutable product identity of one workflow document."""


@dataclass(frozen=True)
class WorkflowRevision:
    document: bytes
    revision_hash: WorkflowRevisionHash = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "revision_hash", WorkflowRevisionHash.of(self.document)
        )


@dataclass(frozen=True)
class RunId:
    value: str

    def __post_init__(self) -> None:
        if self.value == "":
            raise ValueError("RunId must be a nonempty caller string")


class RunState(StrEnum):
    STARTED = "STARTED"
    WAITING_RECONCILIATION = "WAITING_RECONCILIATION"
    WAITING_INPUT = "WAITING_INPUT"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL_RUN_STATES = frozenset(
    {RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED}
)
"""The three ways a run ends. Success keeps `COMPLETED`; an uncontinuable
failure lifts the node's own ending rather than inventing a third word; an
operator's run-cancel command lifts the run under its own word rather than
borrowing `FAILED` for something the operator chose. Issue #439 P1 gives
`CANCELLED` its durable home; no writer constructs it until #439 P3 gives the
word its own end-of-run seam."""


UNSUCCESSFUL_TERMINAL_RUN_STATES = frozenset(TERMINAL_RUN_STATES - {RunState.COMPLETED})
"""The two ways a run ends without an answer. A reader asking whether a run
ended badly asks this rather than spelling the pair again, so a further ending
reaches every such reader at once."""


FIRST_ROUND_ORDINAL = 1
"""The round a run stands in until a declared loop turns it on.

A run whose document declares no loop never leaves this round, and a node no
loop repeats runs exactly once wherever it sits. Naming the one rather than
spelling it at every call site is what keeps "the round this belongs to" one
sentence across the identity, the durable row and the continuation rule.
"""


def require_exact_round_ordinal(round_ordinal: int) -> None:
    """A round ordinal is a whole count from one, and nothing else is admitted."""
    if type(round_ordinal) is not int or round_ordinal < FIRST_ROUND_ORDINAL:
        raise ValueError(f"a round ordinal is a whole count from {FIRST_ROUND_ORDINAL}")


@dataclass(frozen=True)
class Run:
    run_id: RunId
    revision_hash: WorkflowRevisionHash
    state: RunState
    current_node_id: str
    state_version: int
    last_event_sequence: int
    terminal_hash: Sha256Hash | None = None
    current_round_ordinal: int = FIRST_ROUND_ORDINAL

    @staticmethod
    def validate_head(
        current_node_id: str,
        state: RunState,
        state_version: int,
        last_event_sequence: int,
        terminal_hash: Sha256Hash | None,
        current_round_ordinal: int = FIRST_ROUND_ORDINAL,
    ) -> None:
        if current_node_id == "":
            raise ValueError("current_node_id must be nonempty")
        if state_version < 0 or last_event_sequence < 0:
            raise ValueError("run versions and cursors must be nonnegative")
        if (state in TERMINAL_RUN_STATES) != (terminal_hash is not None):
            raise ValueError("only an ended run has a terminal hash")
        require_exact_round_ordinal(current_round_ordinal)

    def __post_init__(self) -> None:
        self.validate_head(
            self.current_node_id,
            self.state,
            self.state_version,
            self.last_event_sequence,
            self.terminal_hash,
            self.current_round_ordinal,
        )
