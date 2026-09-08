from __future__ import annotations

import re
from collections.abc import Callable
from http import HTTPStatus
from typing import assert_never

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from atelier2.api.limits import ApiLimitExceeded, ApiLimits
from atelier2.api.problems import (
    ApiProblem,
    durable_projection_unrepresentable_detail,
)
from atelier2.api.projection.runs import run_resource
from atelier2.api.references import (
    MAX_SIGNED_INT64,
    InvalidPublicProjectReference,
    InvalidPublicRunReference,
    InvalidPublicSourceReference,
    decode_canonical_base64,
    decode_public_project_reference,
    decode_public_run_reference,
    decode_public_source_reference,
)
from atelier2.api.stream import BoundedQueryRunner, QueryAdmissionTimeout
from atelier2.api.wire.requests import RevisionListingView
from atelier2.api.wire.resources import RunResourceV3
from atelier2.application.read_runs import (
    GetRunResult,
    RunNotFound,
    RunRead,
)
from atelier2.application.refusals import (
    DurableStateCorrupt,
    ProjectionTooLarge,
    ReadUnavailable,
)
from atelier2.contracts.host_configuration import ProjectId, ProjectSourceId
from atelier2.contracts.run_projections import (
    RunProjection,
)
from atelier2.contracts.runs import RunId


def resource_response(resource: BaseModel, status: HTTPStatus) -> JSONResponse:
    return JSONResponse(
        resource.model_dump(mode="json", by_alias=True), status_code=status
    )


async def load_run_resource(
    run_id: RunId,
    read_run: Callable[[RunId], GetRunResult],
    runner: BoundedQueryRunner,
    limits: ApiLimits,
) -> RunResourceV3:
    """Render the run a command just changed, through the one read use-case."""
    return run_resource(await load_run_projection(run_id, read_run, runner, limits))


async def load_run_projection(
    run_id: RunId,
    read_run: Callable[[RunId], GetRunResult],
    runner: BoundedQueryRunner,
    limits: ApiLimits,
) -> RunProjection:
    """The run behind a command's answer and behind a stream's first frame.

    Both callers need the same decision — several command routes answer with the
    run's current resource, and a stream opens with where the run stands — so it is
    read once here and rendered by whoever asked. `get_run` owns the decision; this
    is the admission to the API's query budget and the refusal that a full budget
    or an oversized projection produces, neither of which the application decides.
    """
    result = await run_control_query(runner, lambda: read_run(run_id))
    match result:
        case RunRead(projection):
            require_run_projections((projection,), limits)
            return projection
        case RunNotFound():
            raise ApiProblem("run-not-found")
        case ReadUnavailable(detail):
            raise ApiProblem("temporarily-unavailable", detail)
        case ProjectionTooLarge():
            raise ApiProblem("durable-projection-unrepresentable")
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)


def require_run_projections(
    projections: tuple[RunProjection, ...], limits: ApiLimits
) -> None:
    try:
        for projection in projections:
            limits.require_run_projection(projection)
    except ApiLimitExceeded as error:
        raise ApiProblem(
            "durable-projection-unrepresentable",
            durable_projection_unrepresentable_detail(
                error.field_name, error.bound, error.unit
            ),
        ) from error


async def run_control_query[Result](
    runner: BoundedQueryRunner, query: Callable[[], Result]
) -> Result:
    try:
        return await runner.run(query)
    except QueryAdmissionTimeout as error:
        raise ApiProblem("temporarily-unavailable") from error


def decode_public_reference(value: str, limits: ApiLimits) -> RunId:
    try:
        limits.require_field(value)
        run_id = decode_public_run_reference(value)
        limits.require_field(run_id.value)
        return run_id
    except ApiLimitExceeded as error:
        raise ApiProblem("invalid-public-run-reference") from error
    except InvalidPublicRunReference as error:
        raise ApiProblem("invalid-public-run-reference") from error


def decode_public_project_reference_value(value: str, limits: ApiLimits) -> ProjectId:
    try:
        # Encoded length is the codec bound, not maximum_field_characters: the
        # configuration contract admits a maximum-length, UTF-8-encodable
        # ProjectId whose wire encoding is longer than its decoded value.
        project_id = decode_public_project_reference(value)
        limits.require_field(project_id.value)
        return project_id
    except ApiLimitExceeded as error:
        raise ApiProblem("invalid-public-project-reference") from error
    except InvalidPublicProjectReference as error:
        raise ApiProblem("invalid-public-project-reference") from error


def decode_public_source_reference_value(
    value: str, limits: ApiLimits
) -> ProjectSourceId:
    try:
        limits.require_field(value)
        return decode_public_source_reference(value)
    except ApiLimitExceeded as error:
        raise ApiProblem("invalid-public-source-reference") from error
    except InvalidPublicSourceReference as error:
        raise ApiProblem("invalid-public-source-reference") from error


def decode_base64(value: str, limits: ApiLimits) -> bytes:
    try:
        limits.require_base64(value)
        decoded = decode_canonical_base64(value)
        limits.require_payload(decoded)
        return decoded
    except ApiLimitExceeded as error:
        raise ApiProblem("invalid-request", str(error)) from error
    except ValueError as error:
        raise ApiProblem("invalid-base64") from error


def require_field(value: str, limits: ApiLimits) -> None:
    try:
        limits.require_field(value)
    except ApiLimitExceeded as error:
        raise ApiProblem("invalid-request", str(error)) from error


def require_fields(limits: ApiLimits, *values: str) -> None:
    for value in values:
        require_field(value, limits)


def require_new_run_identity(run_id: RunId, limits: ApiLimits) -> None:
    try:
        limits.require_field(run_id.value)
        limits.require_public_run_reference(run_id)
        limits.require_event_cursor(run_id, MAX_SIGNED_INT64)
    except ValueError as error:
        raise ApiProblem("invalid-request") from error


def parse_revision_view(value: str) -> RevisionListingView:
    """Read which representation a listing was asked for, or refuse by name.

    An unknown view is refused rather than quietly served as the default: a
    caller that misspells the described representation would otherwise be handed
    the summary and find the fields it wanted silently absent.
    """

    try:
        return RevisionListingView(value)
    except ValueError as error:
        raise ApiProblem("invalid-request") from error


def require_media_type(request: Request, expected: str) -> None:
    header = request.headers.get("content-type")
    if header is None:
        raise ApiProblem("unsupported-media-type")
    parts = [part.strip().lower() for part in header.split(";")]
    if (
        parts[0] != expected
        or (len(parts) == 2 and parts[1] != "charset=utf-8")
        or len(parts) > 2
    ):
        raise ApiProblem("unsupported-media-type")


async def require_json_media_dependency(request: Request) -> None:
    require_media_type(request, "application/json")


def require_sse_accept(request: Request) -> None:
    header = request.headers.get("accept")
    if header is None:
        return
    for item in header.lower().split(","):
        pieces = [piece.strip() for piece in item.split(";")]
        if pieces[0] not in {"*/*", "text/event-stream"}:
            continue
        quality_parameters = [piece for piece in pieces[1:] if piece.startswith("q=")]
        if not quality_parameters:
            return
        if len(quality_parameters) > 1:
            continue
        quality = quality_parameters[0]
        if (
            re.fullmatch(r"q=(?:0(?:\.\d{0,3})?|1(?:\.0{0,3})?)", quality, re.ASCII)
            is None
        ):
            continue
        if re.fullmatch(r"q=0(?:\.0{0,3})?", quality) is None:
            return
    raise ApiProblem("not-acceptable")
