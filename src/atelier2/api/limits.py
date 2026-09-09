from __future__ import annotations

import re
from dataclasses import dataclass
from typing import cast

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from atelier2.api.problem_vocabulary import artifact_problem_code
from atelier2.api.references import (
    MAXIMUM_RUN_ORDERS,
    base64_characters_for,
    encode_event_cursor,
    encode_public_run_reference,
)
from atelier2.application.publish_workflow_revision import WorkflowPublicationLimits
from atelier2.contracts.artifacts import MAXIMUM_ARTIFACT_BYTES, ArtifactRefusal
from atelier2.contracts.effects import OperatorFoundEffect
from atelier2.contracts.executions import AgentNodeRefusalRecord, RunEventKind
from atelier2.contracts.pages import PageLimit
from atelier2.contracts.run_events import (
    PersistedRunEvent,
)
from atelier2.contracts.run_projections import (
    RunProjection,
)
from atelier2.contracts.runs import RunId


def durable_projection_limit(limits: ApiLimits) -> WorkflowPublicationLimits:
    """The bound a durable reader must hold, derived from the API's own limits.

    It has one owner because it has one value: the whole application reads through
    the same bound, and a reader that could be handed a different one per call is a
    reader whose answers cannot be compared. Deriving it here rather than inside
    the application builder is what lets the reader be constructed with it.
    """
    return WorkflowPublicationLimits(
        maximum_document_bytes=min(
            limits.maximum_request_body_bytes,
            limits.maximum_base64_decoded_bytes,
        ),
        maximum_nodes=limits.maximum_workflow_nodes,
        maximum_string_characters=limits.maximum_field_characters,
        maximum_payload_bytes=min(
            limits.maximum_decoded_payload_bytes,
            limits.maximum_base64_decoded_bytes,
        ),
    )


class ApiLimitExceeded(ValueError):
    """One value cannot fit a named API representation bound."""

    def __init__(self, field_name: str, bound: int, unit: str) -> None:
        self.field_name = field_name
        self.bound = bound
        self.unit = unit
        messages = {
            "public_run_reference": "public run reference exceeds its character limit",
            "event_cursor": "event cursor exceeds its character limit",
        }
        message = messages.get(field_name)
        if message is None:
            if unit == "bytes":
                message = "decoded payload exceeds its byte limit"
            elif unit == "base64 characters":
                message = "encoded payload exceeds its character limit"
            else:
                message = f"{field_name} exceeds its {unit} bound of {bound}"
        super().__init__(message)


@dataclass(frozen=True)
class ApiLimits:
    maximum_request_body_bytes: int
    maximum_field_characters: int
    maximum_base64_characters: int
    maximum_decoded_payload_bytes: int
    maximum_workflow_nodes: int
    maximum_enriched_page_nodes: int
    maximum_enriched_page_document_bytes: int
    event_page_size: PageLimit
    maximum_control_queries: int
    maximum_event_poll_queries: int
    maximum_query_admission_wait_milliseconds: int

    def __post_init__(self) -> None:
        if not isinstance(self.event_page_size, PageLimit):
            raise TypeError("event_page_size must be a PageLimit")
        for name, value in self.__dict__.items():
            if name == "event_page_size":
                continue
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.maximum_base64_characters < 4:
            raise ValueError(
                "maximum_base64_characters must accommodate a nonempty payload"
            )

    @property
    def maximum_base64_decoded_bytes(self) -> int:
        return 3 * (self.maximum_base64_characters // 4)

    def require_field(self, value: str, field_name: str = "field") -> None:
        if len(value) > self.maximum_field_characters:
            raise ApiLimitExceeded(
                field_name, self.maximum_field_characters, "characters"
            )

    def require_base64(self, value: str, field_name: str = "base64 field") -> None:
        if len(value) > self.maximum_base64_characters:
            raise ApiLimitExceeded(
                field_name, self.maximum_base64_characters, "characters"
            )

    def require_payload(self, value: bytes, field_name: str = "payload") -> None:
        if len(value) > self.maximum_decoded_payload_bytes:
            raise ApiLimitExceeded(
                field_name, self.maximum_decoded_payload_bytes, "bytes"
            )

    def require_encoded_payload(
        self, value: bytes, field_name: str = "payload"
    ) -> None:
        self.require_payload(value, field_name)
        if base64_characters_for(len(value)) > self.maximum_base64_characters:
            raise ApiLimitExceeded(
                field_name, self.maximum_base64_characters, "base64 characters"
            )

    def require_public_run_reference(self, run_id: RunId) -> None:
        if len(encode_public_run_reference(run_id)) > self.maximum_field_characters:
            raise ApiLimitExceeded(
                "public_run_reference", self.maximum_field_characters, "characters"
            )

    def require_event_cursor(self, run_id: RunId, sequence: int) -> None:
        if len(encode_event_cursor(run_id, sequence)) > self.maximum_field_characters:
            raise ApiLimitExceeded(
                "event_cursor", self.maximum_field_characters, "characters"
            )

    def require_run_projection(self, projection: RunProjection) -> None:
        # No durable owner bounds how many orders a run was started with, so
        # the read admits or refuses one page at this edge -- not inside the
        # Pydantic model, where an oversized projection would surface as an
        # unhandled validation error instead of a named, typed refusal.
        if len(projection.orders) > MAXIMUM_RUN_ORDERS:
            raise ApiLimitExceeded("orders", MAXIMUM_RUN_ORDERS, "items")
        self.require_field(projection.run.run_id.value, "run_id")
        self.require_public_run_reference(projection.run.run_id)
        if projection.run.last_event_sequence > 0:
            self.require_event_cursor(
                projection.run.run_id, projection.run.last_event_sequence
            )
        self.require_field(projection.run.current_node_id, "current_node_id")
        attempt = projection.current_agent_attempt
        if attempt is not None:
            self.require_field(attempt.attempt_id.value, "attempt_id")
            self.require_field(attempt.node_execution_id.value, "node_execution_id")
            self.require_field(attempt.request_hash.value, "request_hash")
            self.require_field(attempt.state, "attempt_state")
            if attempt.failure_code is not None:
                self.require_field(attempt.failure_code.value, "failure_code")
        reconciliation = projection.reconciliation
        if reconciliation is None:
            return
        intent = reconciliation.intent.intent
        self.require_field(intent.binding.logical_key.value, "logical_key")
        self.require_encoded_payload(intent.request.payload, "request_payload")
        pending = reconciliation.pending_command
        if pending is None:
            return
        command = pending.command
        self.require_field(command.command_id.value, "command_id")
        self.require_field(command.actor.value, "actor")
        self.require_field(command.evidence, "evidence")
        determination = command.determination
        if isinstance(determination, OperatorFoundEffect):
            self.require_field(determination.effect_id.value, "effect_id")
            self.require_encoded_payload(determination.result.payload, "result_payload")

    def require_event_projection(self, projection: PersistedRunEvent) -> None:
        event = projection.event
        self.require_field(event.run_id.value, "run_id")
        self.require_public_run_reference(event.run_id)
        self.require_event_cursor(event.run_id, event.event_sequence)
        self.require_field(event.node_id, "node_id")
        if event.event_kind is RunEventKind.AGENT_FAILED:
            refusal = AgentNodeRefusalRecord.decode(event.payload)
            if refusal is None:
                self.require_field(event.payload.decode("ascii"), "failure_code")
            else:
                self.require_field(refusal.detail, "detail")
            if projection.node_receipt_reason is not None:
                self.require_field(
                    projection.node_receipt_reason, "node_receipt_reason"
                )
        if event.event_kind is not RunEventKind.WAITING_INPUT:
            # The public WAITING_INPUT resource omits the private durable
            # question payload. The adapter has already read its full bytes and
            # verified both payload_hash and event_hash; this boundary only
            # limits fields the response actually projects.
            self.require_encoded_payload(event.payload, "event_payload")
        if event.event_kind is RunEventKind.WAIT_ANSWERED:
            self.require_field(event.payload.decode("utf-8"), "event_payload")
        receipt = projection.receipt
        if receipt is None:
            return
        self.require_field(receipt.intent.binding.logical_key.value, "logical_key")
        self.require_field(receipt.effect_id.value, "effect_id")
        self.require_encoded_payload(receipt.result.payload, "receipt_payload")
        if receipt.reconcile_command_id is not None:
            self.require_field(
                receipt.reconcile_command_id.value, "reconcile_command_id"
            )


@dataclass(frozen=True)
class _BodyLimit:
    """What one bounded path refuses an oversized body under, and above which size."""

    code: str
    maximum_bytes: int


class RequestBodyLimitMiddleware:
    """The byte bound a POST body meets before any route reads it.

    Which paths are bounded, and by how much, is one table rather than one
    number, because the envelope a document travels in and the material an
    artifact *is* are two decisions with two owners. A path absent from the
    table is unbounded here and is bounded by whatever reads it.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        maximum_body_bytes: int,
        api_prefix: str,
    ) -> None:
        self._app = app
        self._maximum_body_bytes = maximum_body_bytes
        self._workflow_publication_path = api_prefix + "/workflow-revisions"
        self._start_run_path = api_prefix + "/runs"
        self._artifact_publication_path = api_prefix + "/artifacts"
        self._library_recognition_path = api_prefix + "/library/recognitions"
        self._library_addition_path = api_prefix + "/library/additions"
        self._run_command_path = re.compile(
            re.escape(api_prefix) + r"/runs/[^/]+/(?:answers|reconciliations)"
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        limit = self._limit_for(scope)
        if limit is None:
            await self._app(scope, receive, send)
            return
        limit_code = limit.code
        content_lengths = [
            value
            for name, value in cast(list[tuple[bytes, bytes]], scope["headers"])
            if name.lower() == b"content-length"
        ]
        if content_lengths:
            if (
                len(content_lengths) != 1
                or re.fullmatch(rb"\d+", content_lengths[0]) is None
            ):
                await self._problem(scope, receive, send, limit_code)
                return
            try:
                declared_length = int(content_lengths[0])
            except ValueError:
                await self._problem(scope, receive, send, limit_code)
                return
            if declared_length > limit.maximum_bytes:
                await self._problem(
                    scope,
                    receive,
                    send,
                    limit_code,
                    "Request body exceeds its byte limit.",
                )
                return

        received_bytes = 0

        async def limited_receive() -> Message:
            nonlocal received_bytes
            message = await receive()
            if message["type"] == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > limit.maximum_bytes:
                    from atelier2.api.problems import ApiProblem

                    raise ApiProblem(limit_code, "Request body exceeds its byte limit.")
            return message

        await self._app(scope, limited_receive, send)

    def _limit_for(self, scope: Scope) -> _BodyLimit | None:
        if scope["method"] != "POST":
            return None
        path = scope["path"]
        if path == self._workflow_publication_path:
            return _BodyLimit("invalid-workflow-document", self._maximum_body_bytes)
        if path in (
            self._start_run_path,
            self._library_recognition_path,
            self._library_addition_path,
        ) or self._run_command_path.fullmatch(path):
            return _BodyLimit("invalid-request", self._maximum_body_bytes)
        if path == self._artifact_publication_path:
            # Material is bounded by what an artifact may be, not by the
            # envelope a document travels in: the two are different decisions
            # and the transport must not quietly overrule the store's.
            return _BodyLimit(
                artifact_problem_code(ArtifactRefusal.ARTIFACT_TOO_LARGE),
                MAXIMUM_ARTIFACT_BYTES,
            )
        return None

    @staticmethod
    async def _problem(
        scope: Scope,
        receive: Receive,
        send: Send,
        code: str,
        detail: str | None = None,
    ) -> None:
        from atelier2.api.problems import problem_response

        await problem_response(code, detail)(scope, receive, send)
