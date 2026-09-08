from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Final, Literal, assert_never, get_args
from urllib.parse import quote

from fastapi.sse import ServerSentEvent

from atelier2.api.limits import ApiLimitExceeded, ApiLimits
from atelier2.api.problems import (
    PROBLEM_DEFINITIONS,
    PROBLEM_TYPE_PREFIX,
    durable_projection_unrepresentable_detail,
    problem_resource,
)
from atelier2.api.projection.events import bounded_event_summary, run_event_resource
from atelier2.api.projection.runs import node_rail_resources
from atelier2.api.references import encode_event_cursor, encode_public_run_reference
from atelier2.api.wire.resources import (
    DurableStateCorruptProblemResource,
    RunProjectionCorruptResource,
    StreamFailureResource,
)
from atelier2.application.project_node_rail import (
    NodeRailUnprojectable,
    project_node_rail,
)
from atelier2.application.read_attention_events import (
    AttentionCursorUnknown,
    AttentionEvent,
    AttentionEventCorrupt,
    AttentionEventPageOversized,
    AttentionEventsRead,
    ReadAttentionEventsResult,
)
from atelier2.application.read_run_events import (
    ReadRunEventsResult,
    RunEventPageOversized,
    RunEventsRead,
)
from atelier2.application.read_runs import GetRunResult, RunNotFound, RunRead
from atelier2.application.refusals import (
    DurableStateCorrupt,
    ProjectionTooLarge,
    ReadUnavailable,
)
from atelier2.contracts.pages import PageLimit
from atelier2.contracts.run_events import (
    PersistedRunEvent,
)
from atelier2.contracts.run_projections import (
    RunProjection,
)
from atelier2.contracts.runs import RunId
from atelier2.contracts.when import RecordedAt

StreamFailureCode = Literal[
    "durable-projection-unrepresentable",
    "durable-state-corrupt",
    "internal-error",
]
STREAM_FAILURE_CODES: Final[tuple[StreamFailureCode, ...]] = get_args(StreamFailureCode)
"""The problem vocabulary a failed stream may speak, owned by the only emitter.

The published document narrows the failure frame to exactly these problems, so
a consumer that accepts them accepts every frame this stream can send.
"""


class QueryAdmissionTimeout(TimeoutError):
    """The bounded API query runner could not admit work before its deadline."""


@dataclass(frozen=True)
class PreparedEventStream:
    run_id: RunId
    after_sequence: int
    head_sequence: int
    terminal: bool
    projection: RunProjection
    """The run this stream reads, read once, so every frame can say where it stands.

    A reader given only events has to rebuild the state machine to learn what one
    event means for the rest of the run. Reading the run here is the edge that
    spares every reader that rebuild: one durable read per stream, onto which the
    events streamed afterwards are folded.
    """


@dataclass(frozen=True)
class PreparedAttentionStream:
    after_run_id: RunId | None
    after_sequence: int | None
    """Resume by same-instant identity exclusion from this event1, or the start of the feed."""


@dataclass(frozen=True)
class EventPollBackoff:
    initial_delay_seconds: float
    maximum_delay_seconds: float
    multiplier: float

    def __post_init__(self) -> None:
        if (
            not math.isfinite(self.initial_delay_seconds)
            or self.initial_delay_seconds <= 0
        ):
            raise ValueError("initial poll delay must be positive")
        if (
            not math.isfinite(self.maximum_delay_seconds)
            or self.maximum_delay_seconds < self.initial_delay_seconds
        ):
            raise ValueError("maximum poll delay must not be below the initial delay")
        if not math.isfinite(self.multiplier) or self.multiplier <= 1:
            raise ValueError("poll delay multiplier must be greater than one")


class BoundedQueryRunner:
    """Run blocking durable calls under one global bound despite task cancellation."""

    def __init__(
        self,
        maximum_concurrent_queries: int,
        *,
        admission_timeout_seconds: float,
    ) -> None:
        if (
            type(maximum_concurrent_queries) is not int
            or maximum_concurrent_queries <= 0
        ):
            raise ValueError("maximum concurrent queries must be a positive integer")
        if (
            not math.isfinite(admission_timeout_seconds)
            or admission_timeout_seconds <= 0
        ):
            raise ValueError("query admission timeout must be positive")
        self._semaphore = asyncio.Semaphore(maximum_concurrent_queries)
        self._admission_timeout_seconds = admission_timeout_seconds
        self._active_queries = 0
        self._peak_active_queries = 0
        self._abandoned_tasks: set[asyncio.Task[object]] = set()

    @property
    def peak_active_queries(self) -> int:
        return self._peak_active_queries

    @property
    def abandoned_queries(self) -> int:
        return len(self._abandoned_tasks)

    async def run[Result](self, query: Callable[[], Result]) -> Result:
        try:
            await asyncio.wait_for(
                self._semaphore.acquire(), self._admission_timeout_seconds
            )
        except TimeoutError as error:
            raise QueryAdmissionTimeout(
                "query admission exceeded its configured wait bound"
            ) from error
        self._active_queries += 1
        self._peak_active_queries = max(self._peak_active_queries, self._active_queries)
        task = asyncio.create_task(asyncio.to_thread(query))

        def finished(completed: asyncio.Task[Result]) -> None:
            self._active_queries -= 1
            self._abandoned_tasks.discard(completed)
            self._semaphore.release()

        task.add_done_callback(finished)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if not task.done():
                self._abandoned_tasks.add(task)
            raise


def _stream_failure(
    code: StreamFailureCode, detail: str | None = None
) -> ServerSentEvent:
    """The last frame of a failed stream.

    It carries no id: a resume cursor on a refusal would invite the browser to
    reconnect into the same refusal forever.
    """

    return ServerSentEvent(
        data=StreamFailureResource(problem=problem_resource(code, detail))
    )


def _run_projection_corrupt(run_id: RunId, event_sequence: int) -> ServerSentEvent:
    """Name one unprojectable run on the attention feed without ending it.

    The cursor is the underlying attention event's identity so Last-Event-ID
    resumes past this run instead of reconnecting into the same corruption.
    """

    return ServerSentEvent(
        id=encode_event_cursor(run_id, event_sequence),
        data=RunProjectionCorruptResource(
            public_run_reference=encode_public_run_reference(run_id),
            problem=DurableStateCorruptProblemResource.model_validate(
                {
                    "type": PROBLEM_TYPE_PREFIX + "durable-state-corrupt",
                    "title": PROBLEM_DEFINITIONS["durable-state-corrupt"].title,
                    "status": PROBLEM_DEFINITIONS["durable-state-corrupt"].status,
                    "detail": PROBLEM_DEFINITIONS["durable-state-corrupt"].detail,
                }
            ),
        ),
    )


def _remember_attention_identity(
    item_recorded_at: RecordedAt,
    run_id: RunId,
    event_sequence: int,
    current_instant: RecordedAt | None,
    emitted_at_instant: set[tuple[RunId, int]],
) -> tuple[RecordedAt, RunId, int, set[tuple[RunId, int]]]:
    if current_instant is not None and item_recorded_at != current_instant:
        emitted_at_instant = set()
    emitted_at_instant.add((run_id, event_sequence))
    return item_recorded_at, run_id, event_sequence, emitted_at_instant


def _node_detail_path(event: PersistedRunEvent) -> str:
    encoded_node_id = quote(event.event.node_id, safe="-_.!~*'()")
    return (
        "/atelier/api/v1/runs/"
        f"{encode_public_run_reference(event.event.run_id)}/nodes/"
        f"{encoded_node_id}"
    )


def _projection_bounds_failure(
    error: ApiLimitExceeded, event: PersistedRunEvent
) -> ServerSentEvent:
    return _stream_failure(
        "durable-projection-unrepresentable",
        durable_projection_unrepresentable_detail(
            error.field_name,
            error.bound,
            error.unit,
            _node_detail_path(event),
        ),
    )


class _StreamEnded(Exception):
    """The stream ends here; `frame` is the last thing it says, if anything.

    A refusal is decided wherever a page, a run or an event is read, but only
    the one generator may yield the frame it becomes, so the decision travels
    up to it this way rather than as a second return channel on every helper.
    """

    def __init__(self, frame: ServerSentEvent | None) -> None:
        super().__init__()
        self.frame = frame


async def _admitted[Answer](
    runner: BoundedQueryRunner, query: Callable[[], Answer]
) -> Answer:
    """One durable read under the query bound; backpressure ends the stream regularly.

    Not being admitted in time is not a failure: the client's own reconnect is
    the answer, so the stream ends with no frame.
    """
    try:
        return await runner.run(query)
    except QueryAdmissionTimeout as timeout:
        raise _StreamEnded(None) from timeout


def _run_events_page(result: ReadRunEventsResult) -> RunEventsRead:
    """The page this read answered, or the ending the stream takes instead."""
    match result:
        case RunEventsRead() as page:
            return page
        case ReadUnavailable():
            # Transient unavailability is answered by the client's own reconnect.
            raise _StreamEnded(None)
        case ProjectionTooLarge():
            raise _StreamEnded(_stream_failure("durable-projection-unrepresentable"))
        case RunEventPageOversized():
            raise _StreamEnded(_stream_failure("internal-error"))
        case DurableStateCorrupt():
            raise _StreamEnded(_stream_failure("durable-state-corrupt"))
        case _ as unreachable:
            assert_never(unreachable)


def _attention_page(result: ReadAttentionEventsResult) -> AttentionEventsRead:
    match result:
        case AttentionEventsRead() as page:
            return page
        case AttentionCursorUnknown() | DurableStateCorrupt():
            raise _StreamEnded(_stream_failure("durable-state-corrupt"))
        case ReadUnavailable():
            raise _StreamEnded(None)
        case ProjectionTooLarge():
            raise _StreamEnded(_stream_failure("durable-projection-unrepresentable"))
        case AttentionEventPageOversized():
            raise _StreamEnded(_stream_failure("internal-error"))
        case _ as unreachable:
            assert_never(unreachable)


def _attention_events_of(page: AttentionEventsRead) -> tuple[PersistedRunEvent, ...]:
    return tuple(
        bounded_event_summary(item.event)
        for item in page.events
        if isinstance(item, AttentionEvent)
    )


def _attention_identity(
    item: AttentionEvent | AttentionEventCorrupt,
) -> tuple[RunId, int]:
    match item:
        case AttentionEventCorrupt(run_id=run_id, event_sequence=event_sequence):
            return run_id, event_sequence
        case AttentionEvent(event=persisted):
            return persisted.event.run_id, persisted.event.event_sequence
        case _ as unreachable:
            assert_never(unreachable)


def _require_page_projectable(
    events: Iterable[PersistedRunEvent], limits: ApiLimits
) -> None:
    """Every event of a page fits the wire before any of it is sent."""
    for persisted in events:
        try:
            limits.require_event_projection(persisted)
        except ApiLimitExceeded as error:
            raise _StreamEnded(_projection_bounds_failure(error, persisted)) from error
        except ValueError as error:
            raise _StreamEnded(_stream_failure("durable-state-corrupt")) from error


def _run_event_frame(
    persisted: PersistedRunEvent,
    projection: RunProjection,
    streamed: Sequence[PersistedRunEvent],
) -> ServerSentEvent:
    try:
        resource = run_event_resource(
            persisted, node_rail_resources(project_node_rail(projection, streamed))
        )
    except (ValueError, NodeRailUnprojectable) as error:
        raise _StreamEnded(_stream_failure("durable-state-corrupt")) from error
    except AssertionError as error:
        raise _StreamEnded(_stream_failure("internal-error")) from error
    return ServerSentEvent(id=resource.cursor, data=resource)


async def _attention_item_frame(
    item: AttentionEvent | AttentionEventCorrupt,
    get_run: Callable[[RunId], GetRunResult],
    runner: BoundedQueryRunner,
    limits: ApiLimits,
) -> ServerSentEvent:
    """The one frame this row becomes: its event on its run's rail, or its run named corrupt."""
    match item:
        case AttentionEventCorrupt(run_id=run_id, event_sequence=event_sequence):
            return _run_projection_corrupt(run_id, event_sequence)
        case AttentionEvent(event=event):
            persisted = bounded_event_summary(event)
        case _ as unreachable:
            assert_never(unreachable)
    run_result = await _admitted(
        runner, lambda current_run_id=persisted.event.run_id: get_run(current_run_id)
    )
    match run_result:
        case RunRead(projection):
            pass
        case RunNotFound() | DurableStateCorrupt():
            return _run_projection_corrupt(
                persisted.event.run_id, persisted.event.event_sequence
            )
        case ReadUnavailable():
            raise _StreamEnded(None)
        case ProjectionTooLarge():
            raise _StreamEnded(_stream_failure("durable-projection-unrepresentable"))
        case _ as unreachable:
            assert_never(unreachable)
    try:
        limits.require_run_projection(projection)
        resource = run_event_resource(
            persisted,
            node_rail_resources(project_node_rail(projection, (persisted,))),
        )
    except ApiLimitExceeded as error:
        raise _StreamEnded(_projection_bounds_failure(error, persisted)) from error
    except (ValueError, NodeRailUnprojectable, AssertionError) as error:
        raise _StreamEnded(_stream_failure("internal-error")) from error
    return ServerSentEvent(id=resource.cursor, data=resource)


async def _delay_before_next_poll(
    page_had_events: bool,
    delay: float,
    backoff: EventPollBackoff,
    sleep: Callable[[float], Awaitable[None]],
) -> float:
    """Ask again at once after a page with events; otherwise wait, longer each time."""
    if page_had_events:
        return backoff.initial_delay_seconds
    await sleep(delay)
    return min(backoff.maximum_delay_seconds, delay * backoff.multiplier)


async def stream_server_events(
    prepared: PreparedEventStream,
    read_page: Callable[[RunId, int, int], ReadRunEventsResult],
    runner: BoundedQueryRunner,
    *,
    page_size: PageLimit,
    limits: ApiLimits,
    poll_backoff: EventPollBackoff,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> AsyncIterator[ServerSentEvent]:
    """Forward one run's events until it ends, is refused, or the client leaves.

    What one page *means* is decided by `read_page`; this loop owns only what is
    the API's to own — the query budget it is admitted through, how long it waits
    before asking again, the limits the served projection must fit, and the frame
    each outcome becomes. Backpressure and transient unavailability end the stream
    regularly rather than as a failure, because the client's own reconnect is the
    answer to both.
    """
    after_sequence = prepared.after_sequence
    next_poll_delay = poll_backoff.initial_delay_seconds
    streamed: list[PersistedRunEvent] = []
    if prepared.terminal and after_sequence == prepared.head_sequence:
        return
    while True:
        try:
            page = _run_events_page(
                await _admitted(
                    runner,
                    lambda current_after_sequence=after_sequence: read_page(
                        prepared.run_id, current_after_sequence, page_size.value
                    ),
                )
            )
            persisted_events = tuple(
                bounded_event_summary(event) for event in page.events
            )
            _require_page_projectable(persisted_events, limits)
            for persisted in persisted_events:
                streamed.append(persisted)
                yield _run_event_frame(persisted, prepared.projection, streamed)
                after_sequence = persisted.event.event_sequence
        except _StreamEnded as ended:
            if ended.frame is not None:
                yield ended.frame
            return
        if page.terminal_seen:
            return
        next_poll_delay = await _delay_before_next_poll(
            bool(page.events), next_poll_delay, poll_backoff, sleep
        )


async def stream_attention_events(
    prepared: PreparedAttentionStream,
    read_page: Callable[
        [RunId | None, int | None, int, tuple[tuple[RunId, int], ...]],
        ReadAttentionEventsResult,
    ],
    get_run: Callable[[RunId], GetRunResult],
    runner: BoundedQueryRunner,
    *,
    page_size: PageLimit,
    limits: ApiLimits,
    poll_backoff: EventPollBackoff,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> AsyncIterator[ServerSentEvent]:
    """Forward the ATTENTION_EVENT_KINDS across runs until the client leaves.

    The feed does not end: a terminal run is one event, not the end of the
    subscription. A run whose projection cannot be served is named as
    RUN_PROJECTION_CORRUPT and does not end the subscription. Backpressure and
    transient unavailability end regularly so the client's own reconnect is the
    answer to both.

    Resume is same-instant identity exclusion: from the cursor event's instant
    T, later instants, or other identities still at T. Last-Event-ID seeds the
    set with that event1; this loop adds each identity it emits and resets the
    set when the second advances.
    """
    after_run_id = prepared.after_run_id
    after_sequence = prepared.after_sequence
    emitted_at_instant: set[tuple[RunId, int]] = set()
    if after_run_id is not None and after_sequence is not None:
        emitted_at_instant.add((after_run_id, after_sequence))
    current_instant: RecordedAt | None = None
    next_poll_delay = poll_backoff.initial_delay_seconds
    while True:
        excluded = tuple(
            identity
            for identity in emitted_at_instant
            if identity != (after_run_id, after_sequence)
        )
        try:
            page = _attention_page(
                await _admitted(
                    runner,
                    lambda current_run_id=after_run_id, current_sequence=after_sequence, current_excluded=excluded: (
                        read_page(
                            current_run_id,
                            current_sequence,
                            page_size.value,
                            current_excluded,
                        )
                    ),
                )
            )
            _require_page_projectable(_attention_events_of(page), limits)
            for item in page.events:
                yield await _attention_item_frame(item, get_run, runner, limits)
                run_id, event_sequence = _attention_identity(item)
                (
                    current_instant,
                    after_run_id,
                    after_sequence,
                    emitted_at_instant,
                ) = _remember_attention_identity(
                    item.recorded_at,
                    run_id,
                    event_sequence,
                    current_instant,
                    emitted_at_instant,
                )
        except _StreamEnded as ended:
            if ended.frame is not None:
                yield ended.frame
            return
        next_poll_delay = await _delay_before_next_poll(
            bool(page.events), next_poll_delay, poll_backoff, sleep
        )
