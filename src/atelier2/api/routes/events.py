from __future__ import annotations

from collections.abc import AsyncIterator
from typing import assert_never

from fastapi import APIRouter, Depends, Request
from fastapi.sse import EventSourceResponse, ServerSentEvent

from atelier2.api._support import (
    decode_public_reference,
    load_run_projection,
    require_sse_accept,
    run_control_query,
)
from atelier2.api.context import ApiContext, api_context_dependency
from atelier2.api.limits import ApiLimitExceeded
from atelier2.api.openapi import API_PREFIX
from atelier2.api.problems import ApiProblem
from atelier2.api.references import (
    InvalidEventCursor,
    PublicRunReferencePath,
    parse_event_cursor,
)
from atelier2.api.stream import (
    PreparedAttentionStream,
    PreparedEventStream,
    stream_attention_events,
    stream_server_events,
)
from atelier2.application.prepare_run_events import (
    EventCursorAhead,
    RunEventStreamPrepared,
    RunNotFound,
)
from atelier2.application.read_attention_events import (
    AttentionCursorUnknown,
    AttentionEventPageOversized,
    AttentionEventsRead,
)
from atelier2.application.refusals import (
    DurableStateCorrupt,
    ProjectionTooLarge,
    ReadUnavailable,
)
from atelier2.contracts.runs import RunId

router = APIRouter()


async def prepare_events(
    request: Request,
    public_ref: PublicRunReferencePath,
    context: ApiContext = api_context_dependency,
) -> PreparedEventStream:
    require_sse_accept(request)
    run_id = decode_public_reference(public_ref, context.limits)
    cursor_headers = request.headers.getlist("last-event-id")
    if len(cursor_headers) > 1:
        raise ApiProblem("invalid-event-cursor")
    after_sequence = 0
    if cursor_headers:
        try:
            context.limits.require_field(cursor_headers[0])
            cursor = parse_event_cursor(cursor_headers[0])
        except (ApiLimitExceeded, InvalidEventCursor) as error:
            raise ApiProblem("invalid-event-cursor") from error
        if cursor.run_id != run_id:
            raise ApiProblem("event-cursor-run-mismatch")
        after_sequence = cursor.sequence
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.runs.prepare_run_events(run_id, after_sequence),
    )
    match result:
        case RunEventStreamPrepared(prepared_run_id, first_after, head, terminal):
            return PreparedEventStream(
                prepared_run_id,
                first_after,
                head,
                terminal,
                await load_run_projection(
                    prepared_run_id,
                    context.use_cases.runs.get_run,
                    context.control_runner,
                    context.limits,
                ),
            )
        case RunNotFound():
            raise ApiProblem("run-not-found")
        case EventCursorAhead():
            raise ApiProblem("event-cursor-ahead")
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case ReadUnavailable(detail):
            raise ApiProblem("temporarily-unavailable", detail)
        case _ as unreachable:
            assert_never(unreachable)


prepared_events_dependency = Depends(prepare_events)


@router.get(
    API_PREFIX + "/runs/{public_ref}/events",
    response_class=EventSourceResponse,
)
async def event_stream_route(
    prepared: PreparedEventStream = prepared_events_dependency,
    context: ApiContext = api_context_dependency,
) -> AsyncIterator[ServerSentEvent]:
    async for event in stream_server_events(
        prepared,
        context.use_cases.runs.read_run_events,
        context.event_runner,
        page_size=context.limits.event_page_size,
        limits=context.limits,
        poll_backoff=context.event_poll_backoff,
    ):
        yield event


async def prepare_attention_events(
    request: Request,
    context: ApiContext = api_context_dependency,
) -> PreparedAttentionStream:
    require_sse_accept(request)
    cursor_headers = request.headers.getlist("last-event-id")
    if len(cursor_headers) > 1:
        raise ApiProblem("invalid-event-cursor")
    after_run_id: RunId | None = None
    after_sequence: int | None = None
    if cursor_headers:
        try:
            context.limits.require_field(cursor_headers[0])
            cursor = parse_event_cursor(cursor_headers[0])
        except (ApiLimitExceeded, InvalidEventCursor) as error:
            raise ApiProblem("invalid-event-cursor") from error
        after_run_id = cursor.run_id
        after_sequence = cursor.sequence
        result = await run_control_query(
            context.control_runner,
            lambda: context.use_cases.runs.read_attention_events(
                after_run_id, after_sequence, 1, ()
            ),
        )
        match result:
            case AttentionEventsRead():
                pass
            case AttentionCursorUnknown():
                raise ApiProblem("invalid-event-cursor")
            case DurableStateCorrupt():
                raise ApiProblem("durable-state-corrupt")
            case ReadUnavailable(detail):
                raise ApiProblem("temporarily-unavailable", detail)
            case ProjectionTooLarge():
                raise ApiProblem("durable-projection-unrepresentable")
            case AttentionEventPageOversized():
                raise ApiProblem("internal-error")
            case _ as unreachable:
                assert_never(unreachable)
    return PreparedAttentionStream(after_run_id, after_sequence)


prepared_attention_events_dependency = Depends(prepare_attention_events)


@router.get(
    API_PREFIX + "/events",
    response_class=EventSourceResponse,
)
async def attention_event_stream_route(
    prepared: PreparedAttentionStream = prepared_attention_events_dependency,
    context: ApiContext = api_context_dependency,
) -> AsyncIterator[ServerSentEvent]:
    async for event in stream_attention_events(
        prepared,
        context.use_cases.runs.read_attention_events,
        context.use_cases.runs.get_run,
        context.event_runner,
        page_size=context.limits.event_page_size,
        limits=context.limits,
        poll_backoff=context.event_poll_backoff,
    ):
        yield event
