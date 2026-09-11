"""The one value a node's success writes, judged against the schema it declared.

Two authors meet here. The provider wrote the bytes the node answered with, and
the atelier writes the patch of the tree that answer kept -- under the property
the node's own published output schema declares for it. What the store persists
is the value both together are, so the value is read against that same schema
once the patch stands in it: judging only the provider's half would let an
artifact and a completion event carry a value the very schema they name refuses.

Whose text failed is part of the answer, never a detail: a refusal named after
the provider would put an agent's name on bytes the atelier composed, and would
order a repair round asking that provider to answer differently about something
it never wrote.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal

from atelier2.adapters.dbos.run_store import load_published_schema_document
from atelier2.adapters.dbos.run_transitions import RunTransitionConflict
from atelier2.adapters.dbos.sql_executor import SqlExecutor
from atelier2.contracts.agents import (
    MAXIMUM_AGENT_FIELD_CHARACTERS,
    MAXIMUM_AGENT_OUTPUT_BYTES_V2,
)
from atelier2.contracts.candidate_reports import (
    CandidateReportDoesNotFit,
    report_carrying_candidate_diff,
    schema_declares_candidate_diff,
)
from atelier2.contracts.node_records_v3 import NodeReceiptReason, node_receipt_reason
from atelier2.contracts.schemas_v3 import (
    InstanceRefused,
    SchemaRefused,
    read_instance_document,
    read_schema_document,
)
from atelier2.contracts.workflows_v3 import NodeOutput


@dataclass(frozen=True, slots=True)
class NoProducibleValue:
    """Why this node cannot produce the one value its own contract declares.

    Only a node whose declared output schema names the atelier's patch property
    can reach this: the provider's own bytes were admitted a moment earlier, so
    what is refused here is always the value once the patch stands in it -- a
    property the author declared as something a patch can never be, or an answer
    already filling the whole produced value. `judged` is what that judgment
    read, kept with the ending like every other refused value.
    """

    verdict: str
    judged: bytes


def declared_output_schema_document(
    session: SqlExecutor, node_id: str, declared: NodeOutput
) -> bytes:
    """The exact document this node's author pinned as its output's schema.

    Read once per success and passed on, because the same document answers two
    questions: whether the provider's bytes are a value it admits, and whether
    its author made room for the atelier's own candidate diff beside them.
    """
    document = load_published_schema_document(
        session, declared.schema_reference.revision
    )
    if document is None:
        raise RunTransitionConflict(
            f"the schema node {node_id!r} pinned for output "
            f"{declared.name!r} is absent from the store"
        )
    return document


def declared_output_schema_refusal(
    document: bytes, node_id: str, declared: NodeOutput, payload: bytes
) -> InstanceRefused | None:
    """Read one declared output against its pinned schema without losing its shape."""
    schema = read_schema_document(document)
    if isinstance(schema, SchemaRefused):
        raise RunTransitionConflict(
            f"the schema node {node_id!r} pinned for output "
            f"{declared.name!r} is not one: {schema}"
        )
    # The byte bound belongs to the route the value arrived by
    # (schemas_v3.py's read_instance_document docstring), and an agent output
    # arrives through the provider frame, not an inline order: its route bound
    # is MAXIMUM_AGENT_OUTPUT_BYTES_V2, not read_instance_document's inline
    # default. #901 slice 5's V3 schema validation newly applied the inline
    # door's bound to outputs the provider frame legally admits, refusing a
    # legal answer before the schema itself was ever consulted.
    verdict = read_instance_document(
        payload, schema, maximum_bytes=MAXIMUM_AGENT_OUTPUT_BYTES_V2
    )
    return verdict if isinstance(verdict, InstanceRefused) else None


def the_value_this_execution_produced(
    schema_document: bytes,
    node_id: str,
    declared: NodeOutput,
    output_bytes: bytes,
    candidate_diff: str | None,
) -> bytes | NoProducibleValue:
    """The bytes this node's success writes, or why it has none to write.

    The patch is the atelier's word, so the value carrying it is judged again
    before anything durable keeps it: the schema admitted the provider's bytes,
    and nothing has yet asked whether it admits the property its own author
    declared beside them.

    No repair round follows a refusal here. The one this store orders asks the
    provider to answer again, and the provider did not write what was refused.
    """

    if not schema_declares_candidate_diff(schema_document):
        return output_bytes
    try:
        value = report_carrying_candidate_diff(
            output_bytes, candidate_diff, MAXIMUM_AGENT_OUTPUT_BYTES_V2
        )
    except CandidateReportDoesNotFit as refusal:
        return NoProducibleValue(
            _bounded_verdict(str(refusal), NodeReceiptReason.PRODUCED_VALUE_REFUSED),
            output_bytes,
        )
    refusal = declared_output_schema_refusal(schema_document, node_id, declared, value)
    if refusal is None:
        return value
    return NoProducibleValue(
        _compact_schema_refusal(
            refusal, value, NodeReceiptReason.PRODUCED_VALUE_REFUSED
        ),
        value,
    )


def schema_refusal_receipt_reason(
    refusal: InstanceRefused, payload: bytes, named: NodeReceiptReason
) -> str:
    """The receipt reason a schema verdict becomes, under the token naming its author.

    The token is the caller's word, because the same verdict says two different
    things about two different authors.
    """

    return node_receipt_reason(named, _compact_schema_refusal(refusal, payload, named))


def _compact_schema_refusal(
    refusal: InstanceRefused, payload: bytes, named: NodeReceiptReason
) -> str:
    """Name the schema's place and rule without embedding rejected output."""
    violation = refusal.violation
    if violation is None:
        words = str(refusal)
    else:
        place = "the value itself" if violation.pointer is None else violation.pointer
        words = (
            f"{refusal.refusal.value}: {place}: "
            f"{_schema_rule_without_rejected_value(violation.reason, _value_at_schema_violation(payload, violation.pointer))}"
        )
    return _bounded_verdict(words, named)


def _bounded_verdict(words: str, named: NodeReceiptReason) -> str:
    """One ending's words, cut to what a receipt reason has room for beside its token."""

    return words[: MAXIMUM_AGENT_FIELD_CHARACTERS - len(named.value) - len(": ")]


def _value_at_schema_violation(payload: bytes, pointer: str | None) -> object:
    """Read the one JSON value whose repr JSON Schema put in its diagnostic."""
    value: object = json.loads(payload.decode("utf-8"), parse_float=Decimal)
    if pointer is None:
        return value
    for escaped_part in pointer.removeprefix("/").split("/"):
        part = escaped_part.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list):
            value = value[int(part)]
        elif isinstance(value, dict):
            value = value[part]
        else:
            raise TypeError(
                f"schema violation pointer {pointer!r} does not address its value"
            )
    return value


def _schema_rule_without_rejected_value(reason: str, rejected_value: object) -> str:
    """Remove exactly JSON Schema's repr of the rejected value from its rule."""
    rendered_value = repr(rejected_value)
    return reason.replace(rendered_value, "", 1).strip()
