from __future__ import annotations

from inspect import signature
from typing import cast

import pytest

from atelier2.contracts.agents import AgentExecutionCapability
from atelier2.contracts.executions import NodeExecutionId
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.node_records_v3 import (
    AvailableContextGrant,
    BoundNodeRevisions,
    DeclaredContextPackage,
    DeclaredOutput,
    InputEnvelope,
    InputReceiptBinding,
    NodeArtifact,
    NodeExecutionRequest,
    NodeKindV3,
    NodeReceipt,
    NodeReceiptHash,
    NodeReceiptReason,
    PersistedReceiptDisposition,
    ProjectedDeliveryStatus,
    ReceiptOutput,
    node_receipt_reason,
)
from atelier2.contracts.revisions_v3 import PublishedRevisionHash
from atelier2.contracts.run_configuration_v3 import RunConfigurationRevisionHash
from atelier2.contracts.runs import RunId, WorkflowRevisionHash
from atelier2.contracts.stored_node_receipt_reasons import (
    read_stored_node_receipt_reason,
    store_node_receipt_reason,
)

WORKFLOW = WorkflowRevisionHash("aa" * 32)
RUN_CONFIGURATION = RunConfigurationRevisionHash("bb" * 32)
SCHEMA = PublishedRevisionHash("cc" * 32)
SOURCE = PublishedRevisionHash("dd" * 32)
READ_OPERATION = PublishedRevisionHash("ee" * 32)
VALUE = Sha256Hash("ff" * 32)
RUN = RunId("run-v3")
NODE = "build"
MANIFEST = b"context-manifest\x00"
PACKAGE_HASH = "32508a7d4553d5af0dedf0710b0bce660fc4294e6c07709426081be8a6e219cb"
REQUEST_HASH = "1a15122cbe7914197904af684a951b8d3a42180cb417ed1072756d81a98344e4"
ARTIFACT_HASH = "6dfc8742c2ea8d216481dafb31294010c0586c4cbcb87a6641ee4f73e816cd10"
RECEIPT_HASH = "9f582bf8b4a9583de77ff8bf7f36b5e4211847f31543e9f62392609bf29144ab"
RESULT_BYTES = b"result-bytes"
RESULT_VALUE_HASH = "4796ef914c847f1124994597d49d513b96882c4176bf1ccb0bc4f4c5b18ee95a"


def _package() -> DeclaredContextPackage:
    return DeclaredContextPackage(MANIFEST)


def _request(
    *,
    context_package_hash: str | None = None,
    kind: NodeKindV3 = NodeKindV3.AGENT,
    mode: AgentExecutionCapability | None = AgentExecutionCapability.HEADLESS,
    node_id: str = NODE,
) -> NodeExecutionRequest:
    package_hash = _package().package_hash
    if context_package_hash is not None:
        from atelier2.contracts.node_records_v3 import DeclaredContextPackageHash

        package_hash = DeclaredContextPackageHash(context_package_hash)
    return NodeExecutionRequest(
        WORKFLOW,
        RUN_CONFIGURATION,
        RUN,
        node_id,
        package_hash,
        (AvailableContextGrant("notes", SOURCE, (READ_OPERATION,)),),
        kind,
        mode,
        (InputEnvelope(ProjectedDeliveryStatus.SUCCEEDED, "draft", SCHEMA, VALUE),),
        BoundNodeRevisions(
            agent_configuration=PublishedRevisionHash("11" * 32),
            profile=PublishedRevisionHash("22" * 32),
            budget=PublishedRevisionHash("33" * 32),
        ),
        (DeclaredOutput("result", SCHEMA),),
    )


def _execution() -> NodeExecutionId:
    return NodeExecutionId.for_node(RUN, WORKFLOW, NODE)


def test_context_package_hash_is_the_literal_v3_vector() -> None:
    assert _package().package_hash.value == PACKAGE_HASH


def test_changing_context_package_bytes_changes_the_hash() -> None:
    assert (
        DeclaredContextPackage(b"context-manifest\x01").package_hash.value
        != PACKAGE_HASH
    )


def test_node_execution_request_hash_is_the_literal_v3_vector() -> None:
    assert _request().request_hash.value == REQUEST_HASH


@pytest.mark.parametrize(
    "builder",
    (
        lambda: _request(context_package_hash=("00" * 32)),
        lambda: _request(node_id="other"),
        lambda: _request(
            kind=NodeKindV3.ACTION,
            mode=None,
        ),
    ),
    ids=("context", "node", "kind"),
)
def test_changing_one_request_preimage_field_changes_the_hash(builder) -> None:
    assert builder().request_hash.value != REQUEST_HASH


def test_identical_request_retry_keeps_the_literal_hash() -> None:
    assert _request().request_hash.value == REQUEST_HASH


def test_agent_request_requires_a_mode_and_other_kinds_refuse_one() -> None:
    with pytest.raises(ValueError, match="mode"):
        _request(kind=NodeKindV3.AGENT, mode=None)
    with pytest.raises(ValueError, match="mode"):
        _request(kind=NodeKindV3.WAIT, mode=AgentExecutionCapability.HEADLESS)


def _upstream_receipt(
    disposition: PersistedReceiptDisposition = PersistedReceiptDisposition.SUCCEEDED,
    reason: str = "completed",
) -> InputReceiptBinding:
    return InputReceiptBinding(
        "implement", disposition, reason, NodeReceiptHash("00" * 32)
    )


def test_stale_is_a_delivery_status_and_not_a_persisted_disposition() -> None:
    envelope = InputEnvelope(
        ProjectedDeliveryStatus.STALE, "draft", receipt=_upstream_receipt()
    )
    assert envelope.status is ProjectedDeliveryStatus.STALE
    assert envelope.receipt is not None
    assert envelope.receipt.disposition is PersistedReceiptDisposition.SUCCEEDED
    assert "stale" not in {item.value for item in PersistedReceiptDisposition}
    with pytest.raises(TypeError):
        NodeReceipt(
            _execution(),
            cast(PersistedReceiptDisposition, "stale"),
            "projected",
            _request().request_hash,
            _package().package_hash,
            (),
        )


def test_non_succeeded_envelope_names_the_upstream_receipt_and_refuses_a_value() -> (
    None
):
    failed = InputEnvelope(
        ProjectedDeliveryStatus.FAILED,
        "draft",
        receipt=_upstream_receipt(
            PersistedReceiptDisposition.FAILED, "schema_violation"
        ),
    )
    assert failed.receipt is not None
    assert failed.schema_revision is None
    with pytest.raises(ValueError, match="receipt"):
        InputEnvelope(ProjectedDeliveryStatus.STALE, "draft", SCHEMA, None)
    with pytest.raises(ValueError, match="schema or value"):
        InputEnvelope(
            ProjectedDeliveryStatus.FAILED,
            "draft",
            SCHEMA,
            None,
            _upstream_receipt(PersistedReceiptDisposition.FAILED, "schema_violation"),
        )
    with pytest.raises(ValueError, match="matches the persisted disposition"):
        InputEnvelope(
            ProjectedDeliveryStatus.FAILED,
            "draft",
            receipt=_upstream_receipt(
                PersistedReceiptDisposition.CANCELLED, "cancelled"
            ),
        )
    with pytest.raises(ValueError, match="no receipt"):
        InputEnvelope(
            ProjectedDeliveryStatus.SUCCEEDED,
            "draft",
            SCHEMA,
            VALUE,
            _upstream_receipt(),
        )


def test_node_artifact_hash_is_the_literal_v3_vector() -> None:
    artifact = NodeArtifact(RUN, NODE, _execution(), "result", SCHEMA, RESULT_BYTES)
    assert artifact.value_hash.value == RESULT_VALUE_HASH
    assert artifact.artifact_hash.value == ARTIFACT_HASH


def test_changing_artifact_value_bytes_changes_the_hash() -> None:
    changed = NodeArtifact(RUN, NODE, _execution(), "result", SCHEMA, b"other-bytes")
    assert changed.artifact_hash.value != ARTIFACT_HASH
    assert changed.value_hash.value != RESULT_VALUE_HASH


@pytest.mark.proves("receipt-has-no-access-input-and-keeps-v3-identity")
def test_succeeded_receipt_hash_is_the_literal_v3_vector() -> None:
    assert "access_receipt_hashes" not in signature(NodeReceipt).parameters
    artifact = NodeArtifact(RUN, NODE, _execution(), "result", SCHEMA, RESULT_BYTES)
    receipt = NodeReceipt(
        _execution(),
        PersistedReceiptDisposition.SUCCEEDED,
        "completed",
        _request().request_hash,
        _package().package_hash,
        (ReceiptOutput("result", SCHEMA, artifact.value_hash),),
    )
    assert receipt.receipt_hash.value == RECEIPT_HASH


def test_failed_receipt_carries_no_output_values_and_differs() -> None:
    failed = NodeReceipt(
        _execution(),
        PersistedReceiptDisposition.FAILED,
        "schema_violation",
        _request().request_hash,
        _package().package_hash,
        (),
    )
    assert failed.receipt_hash.value != RECEIPT_HASH
    with pytest.raises(ValueError, match="output"):
        NodeReceipt(
            _execution(),
            PersistedReceiptDisposition.FAILED,
            "schema_violation",
            _request().request_hash,
            _package().package_hash,
            (ReceiptOutput("result", SCHEMA, Sha256Hash(RESULT_VALUE_HASH)),),
        )


def test_receipt_refuses_a_wrong_request_hash_type() -> None:
    with pytest.raises(TypeError):
        NodeReceipt(
            _execution(),
            PersistedReceiptDisposition.BLOCKED,
            "dependency_failed",
            cast(object, PACKAGE_HASH),  # type: ignore[arg-type]
            _package().package_hash,
            (),
        )


def test_a_judged_receipt_includes_schema_identity_in_its_hash() -> None:
    artifact = NodeArtifact(RUN, NODE, _execution(), "result", SCHEMA, RESULT_BYTES)
    judged = NodeReceipt(
        _execution(),
        PersistedReceiptDisposition.SUCCEEDED,
        NodeReceiptReason.OUTPUT_ACCEPTED.value,
        _request().request_hash,
        _package().package_hash,
        (ReceiptOutput("result", SCHEMA, artifact.value_hash),),
        schema_revision=SCHEMA,
        value_hash=artifact.value_hash,
    )
    assert judged.receipt_hash.value != RECEIPT_HASH
    other_bytes = NodeReceipt(
        _execution(),
        PersistedReceiptDisposition.SUCCEEDED,
        NodeReceiptReason.OUTPUT_ACCEPTED.value,
        _request().request_hash,
        _package().package_hash,
        (ReceiptOutput("result", SCHEMA, artifact.value_hash),),
        schema_revision=SCHEMA,
        value_hash=Sha256Hash.of(b"other-bytes"),
    )
    assert other_bytes.receipt_hash != judged.receipt_hash


def test_schema_identity_is_both_fields_or_neither() -> None:
    with pytest.raises(ValueError, match="both fields or neither"):
        NodeReceipt(
            _execution(),
            PersistedReceiptDisposition.FAILED,
            node_receipt_reason(
                NodeReceiptReason.OUTPUT_SCHEMA_REFUSED, "instance-not-json"
            ),
            _request().request_hash,
            _package().package_hash,
            (),
            schema_revision=SCHEMA,
        )


def test_a_plain_stored_reason_is_honestly_empty_identity() -> None:
    words = node_receipt_reason(
        NodeReceiptReason.OUTPUT_SCHEMA_REFUSED, "instance-not-json"
    )
    reason, schema_revision, value_hash = read_stored_node_receipt_reason(words)
    assert reason == words
    assert schema_revision is None
    assert value_hash is None


def test_a_stored_judgment_round_trips_its_schema_identity() -> None:
    words = node_receipt_reason(
        NodeReceiptReason.OUTPUT_SCHEMA_REFUSED, "instance-not-json"
    )
    stored = store_node_receipt_reason(words, SCHEMA, Sha256Hash(RESULT_VALUE_HASH))
    reason, schema_revision, value_hash = read_stored_node_receipt_reason(stored)
    assert reason == words
    assert schema_revision == SCHEMA
    assert value_hash == Sha256Hash(RESULT_VALUE_HASH)


@pytest.mark.parametrize(
    "stored",
    (
        "{not-json",
        '{"reason":"output-accepted"}',
        (
            '{"reason":"output-accepted","schema_revision":"not-a-hash",'
            f'"value_hash":"{RESULT_VALUE_HASH}"}}'
        ),
    ),
    ids=("broken object", "missing identity", "unreadable hash"),
)
def test_an_unreadable_receipt_payload_is_loud(stored: str) -> None:
    with pytest.raises(ValueError, match="unreadable"):
        read_stored_node_receipt_reason(stored)
