"""The reason column of a judged `node-receipt/v3`, written and read back.

A receipt's reason is one column, and a judged receipt has to name the schema
identity it judged beside its words. That is a payload question rather than a
record question: `node_records_v3` owns the receipt and its vocabulary, this
owns the one string that column holds and the three truths reading it can find.
"""

from __future__ import annotations

import json
from enum import StrEnum

from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.node_records_v3 import NodeReceiptReason
from atelier2.contracts.revisions_v3 import PublishedRevisionHash


class NodeReceiptIdentityField(StrEnum):
    """The JSON keys a judged `node-receipt/v3` adds to the stored reason column.

    The family is a JSON payload, not a new frame domain: old rows keep the
    plain reason string, new judged rows name these keys beside it. The words
    stay the reason; these two are the schema identity the judgment used.
    """

    SCHEMA_REVISION = "schema_revision"
    VALUE_HASH = "value_hash"


_STORED_REASON_FIELD = "reason"
_UNREADABLE_NODE_RECEIPT_V3_PAYLOAD = "a node-receipt/v3 payload is unreadable"


def node_receipt_reason_names_a_schema_judgment(reason: str) -> bool:
    """Whether these words are a schema owner's verdict, not a process ending."""

    token, _separator, _rest = reason.partition(": ")
    return token in {
        NodeReceiptReason.OUTPUT_ACCEPTED.value,
        NodeReceiptReason.OUTPUT_SCHEMA_REFUSED.value,
    }


def store_node_receipt_reason(
    reason: str,
    schema_revision: PublishedRevisionHash | None,
    value_hash: Sha256Hash | None,
) -> str:
    """The reason column: the words, or the words plus the judged identity.

    A receipt nobody judged stays the plain string it has always been. A
    receipt that judged bytes becomes the JSON object that carries those
    bytes' schema identity -- same column, same family, no store hop.
    """

    if schema_revision is None and value_hash is None:
        return reason
    if schema_revision is None or value_hash is None:
        raise ValueError("schema identity is both fields or neither")
    return json.dumps(
        {
            _STORED_REASON_FIELD: reason,
            NodeReceiptIdentityField.SCHEMA_REVISION.value: schema_revision.value,
            NodeReceiptIdentityField.VALUE_HASH.value: value_hash.value,
        },
        separators=(",", ":"),
        sort_keys=True,
        ensure_ascii=True,
    )


def read_stored_node_receipt_reason(
    stored: str,
) -> tuple[str, PublishedRevisionHash | None, Sha256Hash | None]:
    """Three truths, never mixed: plain words, named identity, or loud corruption.

    A string that is not an object is an older row, or a receipt nobody
    judged -- honestly empty identity, words intact. A well-formed object
    names the words and both identity fields. Anything that looks like an
    object and is not that shape is unreadable, not empty.
    """

    if not stored.startswith("{"):
        return stored, None, None
    try:
        payload = json.loads(stored)
    except json.JSONDecodeError as error:
        raise ValueError(_UNREADABLE_NODE_RECEIPT_V3_PAYLOAD) from error
    expected = {
        _STORED_REASON_FIELD,
        NodeReceiptIdentityField.SCHEMA_REVISION.value,
        NodeReceiptIdentityField.VALUE_HASH.value,
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        raise ValueError(_UNREADABLE_NODE_RECEIPT_V3_PAYLOAD)
    reason = payload[_STORED_REASON_FIELD]
    if not isinstance(reason, str) or reason == "":
        raise ValueError(_UNREADABLE_NODE_RECEIPT_V3_PAYLOAD)
    try:
        return (
            reason,
            PublishedRevisionHash(
                payload[NodeReceiptIdentityField.SCHEMA_REVISION.value]
            ),
            Sha256Hash(payload[NodeReceiptIdentityField.VALUE_HASH.value]),
        )
    except (TypeError, ValueError) as error:
        raise ValueError("a node-receipt/v3 schema identity is unreadable") from error
