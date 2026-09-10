"""The operator's V3 run-cancel command identity and confirmation request.

#439 P1 gives this vocabulary a durable home before it has a consumer: P2
wires a store path that resolves `CancelRunRequest`, and P4 gives it an HTTP
route. Until then this module is inert -- exactly like `RunState.CANCELLED`.

`CancelRunRequest` is the boundary shape one confirmed operator cancel
arrives in: which run, the idempotency key the client repeats on retry, and
the node execution the operator's confirmation named. The last field is D2's
fence (#439 Bauplan): it binds run, revision, node *and* declared-loop round
in one value the store recomputes with `NodeExecutionId.for_node` rather than
trusts, so a confirmation read in one loop round can never stop the wrong
round's attempt after a loop jump.

`RunCancelCommandId` is the durable identity that idempotency key mints into.
The server mints it -- a client only ever supplies the key it repeats on
retry, never a `command_id` -- so every accepted operator run-cancel command
is, by construction, inside the reserved namespace `is_operator_run_cancel`
recognizes. That namespace is disjoint from the two existing
`AgentAttemptCancellation.command_id` families in `agent_attempt_store.py`:
`STOP_AFTER_DRIVER_LOSS` (`atelier2-driver-lost`, a driver-lost restart) and
`_unstartable_node_cleanup_command_id` (`<refusal word>:<attempt id>`, the
never-launched cleanup of a node that cannot start) -- neither starts with
this module's prefix, so a command minted for one purpose can never be
mistaken for another. `is_operator_run_cancel` is the one recognizer both P2's
store and P4's route namespace refusal share, mirroring the mint/recognize pair
`_unstartable_node_cleanup_command_id`/`_is_unstartable_node_cleanup` already
use for their own family.
"""

from __future__ import annotations

from dataclasses import dataclass

from atelier2.contracts.agents import MAXIMUM_AGENT_FIELD_CHARACTERS
from atelier2.contracts.executions import NodeExecutionId
from atelier2.contracts.hashing import Sha256Hash, frame
from atelier2.contracts.runs import RunId

_OPERATOR_RUN_CANCEL_NAMESPACE = "atelier2-operator-run-cancel"


def is_operator_run_cancel(command_id: str) -> bool:
    """Whether a stored command id was minted by `RunCancelCommandId.for_key`.

    A structural check, not a recomputation: the caller here never has the
    idempotency key a stored command id was minted from, only the id itself.
    """

    prefix = f"{_OPERATOR_RUN_CANCEL_NAMESPACE}:"
    if not command_id.startswith(prefix):
        return False
    try:
        Sha256Hash(command_id[len(prefix) :])
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class RunCancelCommandId:
    """The durable command id one operator run-cancel idempotency key mints to.

    Framed once, in one place: the same idempotency key always mints the same
    id, so a client's retry after a lost response resubmits the exact command
    the store already knows rather than a second one it must reconcile.
    """

    value: str

    def __post_init__(self) -> None:
        if not is_operator_run_cancel(self.value):
            raise ValueError(
                "a run-cancel command id must be framed in the reserved "
                f"'{_OPERATOR_RUN_CANCEL_NAMESPACE}' namespace"
            )

    @classmethod
    def for_key(cls, idempotency_key: str) -> RunCancelCommandId:
        if (
            not isinstance(idempotency_key, str)
            or not 1 <= len(idempotency_key) <= MAXIMUM_AGENT_FIELD_CHARACTERS
        ):
            raise ValueError(
                "a run-cancel idempotency key must contain "
                f"1..{MAXIMUM_AGENT_FIELD_CHARACTERS} characters"
            )
        digest = Sha256Hash.of(
            frame("run-cancel-command-id/v1", idempotency_key.encode("utf-8"))
        )
        return cls(f"{_OPERATOR_RUN_CANCEL_NAMESPACE}:{digest.value}")


@dataclass(frozen=True)
class CancelRunRequest:
    """One operator's confirmed run-cancel, before the store resolves it."""

    run_id: RunId
    idempotency_key: str
    expected_node_execution_id: NodeExecutionId

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, RunId):
            raise TypeError("a run-cancel request names a typed run id")
        if not isinstance(self.expected_node_execution_id, NodeExecutionId):
            raise TypeError(
                "a run-cancel request names a typed expected node execution"
            )
        if (
            not isinstance(self.idempotency_key, str)
            or not 1 <= len(self.idempotency_key) <= MAXIMUM_AGENT_FIELD_CHARACTERS
        ):
            raise ValueError(
                "a run-cancel idempotency key must contain "
                f"1..{MAXIMUM_AGENT_FIELD_CHARACTERS} characters"
            )
