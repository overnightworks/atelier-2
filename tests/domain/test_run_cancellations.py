from __future__ import annotations

import pytest

from atelier2.adapters.dbos import agent_attempt_store
from atelier2.contracts.agent_attempts import STOP_AFTER_DRIVER_LOSS, AgentAttemptId
from atelier2.contracts.agents import AgentExecutionRequestHash
from atelier2.contracts.executions import AgentExecutionRefusal, NodeExecutionId
from atelier2.contracts.run_cancellations import (
    CancelRunRequest,
    RunCancelCommandId,
    is_operator_run_cancel,
)
from atelier2.contracts.runs import RunId, WorkflowRevisionHash


def test_run_cancel_command_id_has_a_fixed_canonical_vector() -> None:
    command_id = RunCancelCommandId.for_key("operator-cancel-1")
    assert (
        command_id.value == "atelier2-operator-run-cancel:"
        "ff30d981d83c36885485eb82942c5def4822669cf12b9476d8938b65958fa9e2"
    )


def test_the_same_idempotency_key_mints_the_same_command_id() -> None:
    first = RunCancelCommandId.for_key("operator-cancel-1")
    second = RunCancelCommandId.for_key("operator-cancel-1")
    third = RunCancelCommandId.for_key("operator-cancel-2")

    assert first == second
    assert first != third


def test_every_minted_command_id_is_recognized_as_an_operator_run_cancel() -> None:
    for key in ("a", "operator-cancel-1", "retry-after-lost-response", "x" * 200):
        assert is_operator_run_cancel(RunCancelCommandId.for_key(key).value)


def test_a_minted_command_id_is_disjoint_from_the_driver_lost_family() -> None:
    assert not is_operator_run_cancel(STOP_AFTER_DRIVER_LOSS)
    assert RunCancelCommandId.for_key(STOP_AFTER_DRIVER_LOSS).value != (
        STOP_AFTER_DRIVER_LOSS
    )


@pytest.mark.parametrize("refusal", AgentExecutionRefusal)
def test_a_minted_command_id_is_disjoint_from_the_unstartable_node_cleanup_family(
    refusal: AgentExecutionRefusal,
) -> None:
    """Compares against the real adapter mint, not a hand-copied format.

    Reconstructing `<refusal word>:<attempt id>` by hand here would duplicate
    the family's own construction logic and could drift silently from it;
    calling the adapter's own minting function is what keeps this a true
    disjointness proof against the family that actually exists.
    """

    attempt_id = AgentAttemptId.for_execution(
        NodeExecutionId("0" * 64), AgentExecutionRequestHash("1" * 64)
    )
    cleanup_command_id = agent_attempt_store._unstartable_node_cleanup_command_id(
        attempt_id, refusal
    )

    assert not is_operator_run_cancel(cleanup_command_id)


@pytest.mark.parametrize(
    "foreign",
    [
        "",
        "garbage",
        "atelier2-operator-run-cancel",
        "atelier2-operator-run-cancel:",
        "atelier2-operator-run-cancel:not-a-hex-digest",
        "atelier2-operator-run-cancel:" + "g" * 64,
        STOP_AFTER_DRIVER_LOSS,
    ],
)
def test_a_foreign_command_id_is_never_recognized_or_constructible(
    foreign: str,
) -> None:
    assert not is_operator_run_cancel(foreign)
    with pytest.raises(ValueError, match="reserved"):
        RunCancelCommandId(foreign)


def test_cancel_run_request_names_a_typed_run_node_execution_and_key() -> None:
    run_id = RunId("run-17")
    revision_hash = WorkflowRevisionHash.of(b"workflow-document")
    node_execution_id = NodeExecutionId.for_node(run_id, revision_hash, "review")

    request = CancelRunRequest(run_id, "operator-cancel-1", node_execution_id)

    assert request.run_id == run_id
    assert request.idempotency_key == "operator-cancel-1"
    assert request.expected_node_execution_id == node_execution_id


def test_cancel_run_request_refuses_an_empty_idempotency_key() -> None:
    run_id = RunId("run-17")
    revision_hash = WorkflowRevisionHash.of(b"workflow-document")
    node_execution_id = NodeExecutionId.for_node(run_id, revision_hash, "review")

    with pytest.raises(ValueError, match="idempotency key"):
        CancelRunRequest(run_id, "", node_execution_id)
