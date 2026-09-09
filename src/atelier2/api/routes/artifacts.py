from __future__ import annotations

from http import HTTPStatus
from typing import assert_never

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from atelier2.api._support import (
    require_media_type,
    resource_response,
    run_control_query,
)
from atelier2.api.context import ApiContext, api_context_dependency
from atelier2.api.openapi import ARTIFACT_PATH, ARTIFACTS_PATH
from atelier2.api.problem_vocabulary import artifact_problem_code
from atelier2.api.problems import ApiProblem
from atelier2.api.references import ArtifactHashPath
from atelier2.api.wire.resources import ArtifactResource
from atelier2.application.publish_artifact import (
    ArtifactPublicationCreated,
    ArtifactPublicationExisting,
    ArtifactPublicationInvalid,
)
from atelier2.application.read_artifact import ArtifactNotFound, ArtifactRead
from atelier2.application.refusals import DurableStateCorrupt, WriteUnavailable
from atelier2.contracts.artifacts import ArtifactHash

router = APIRouter()


ARTIFACT_MEDIA_TYPE = "application/octet-stream"
"""The one media type this door speaks, in both directions.

An artifact is whatever a caller needs an agent to read, so the bytes travel
opaque going in and come back out exactly as they arrived.
"""


class ArtifactBytesResponse(Response):
    """The read answer, so the published document declares the bytes it sends."""

    media_type = ARTIFACT_MEDIA_TYPE


@router.post(
    ARTIFACTS_PATH,
    response_model=ArtifactResource,
    status_code=HTTPStatus.CREATED,
    responses={HTTPStatus.OK: {"model": ArtifactResource}},
)
async def publish_artifact_route(
    request: Request, context: ApiContext = api_context_dependency
) -> JSONResponse:
    """Publish material by its bytes, and answer with the address they hash to.

    The media type is opaque bytes rather than JSON because an artifact is
    whatever a caller needs an agent to read; what an order made of it must be
    is decided later, against the schema its document pinned.
    """
    require_media_type(request, ARTIFACT_MEDIA_TYPE)
    content = await request.body()
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.publish_artifact(content),
    )
    match result:
        case ArtifactPublicationCreated(artifact):
            status = HTTPStatus.CREATED
        case ArtifactPublicationExisting(artifact):
            status = HTTPStatus.OK
        case ArtifactPublicationInvalid(verdict):
            raise ApiProblem(artifact_problem_code(verdict.refusal), str(verdict))
        case WriteUnavailable(detail):
            raise ApiProblem("temporarily-unavailable", detail)
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)
    return resource_response(
        ArtifactResource(artifact_hash=artifact.artifact_hash.value), status
    )


@router.get(
    ARTIFACT_PATH,
    response_class=ArtifactBytesResponse,
    responses={
        HTTPStatus.OK: {
            "content": {
                ARTIFACT_MEDIA_TYPE: {"schema": {"type": "string", "format": "binary"}}
            }
        }
    },
)
async def read_artifact_route(
    artifact_hash: ArtifactHashPath, context: ApiContext = api_context_dependency
) -> Response:
    """The exact bytes one address names, for a caller holding only the address.

    This is the read side of the door the publication answered with: same media
    type, same bytes, so what an agent was handed can be read back by whoever
    holds the hash a run or a publication named.
    """
    try:
        parsed = ArtifactHash(artifact_hash)
    except (TypeError, ValueError) as error:
        raise ApiProblem("invalid-artifact-hash") from error
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.read_artifact(parsed),
    )
    match result:
        case ArtifactRead(artifact):
            return ArtifactBytesResponse(artifact.content)
        case ArtifactNotFound():
            raise ApiProblem("artifact-not-found")
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)
