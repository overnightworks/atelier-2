from __future__ import annotations

from typing import assert_never

from fastapi import APIRouter, Response

from atelier2.api._support import run_control_query
from atelier2.api.context import ApiContext, api_context_dependency
from atelier2.api.openapi import (
    CATALOG_LINEAGE_MEMBERS_PATH,
    CATALOG_LINEAGE_RETIREMENTS_PATH,
)
from atelier2.api.problems import ApiProblem
from atelier2.api.references import CatalogLineageIdPath
from atelier2.api.wire.requests import (
    AdmitCatalogMemberRequestResource,
    RetireCatalogLineageRequestResource,
)
from atelier2.api.wire.resources import CatalogAdmissionResource
from atelier2.application.admit_catalog_member import (
    CatalogAuthoredNameRestated,
    CatalogDisplayNameInvalid,
    CatalogExplicitNameRequired,
    CatalogRevisionUnpublished,
)
from atelier2.application.refusals import (
    DurableStateCorrupt,
    ProjectionTooLarge,
    WriteUnavailable,
)
from atelier2.contracts.catalog_v3 import (
    CatalogActivatedAt,
    CatalogActor,
    CatalogAdmissionExisting,
    CatalogAdmissionKindMismatch,
    CatalogAdmissionLineageMissing,
    CatalogAdmissionNameHeld,
    CatalogAdmissionRetired,
    CatalogAdmissionRevisionOwned,
    CatalogAdmissionUnpublished,
    CatalogLineageFounded,
    CatalogLineageId,
    CatalogLineageIdMismatch,
    CatalogLineageRetired,
    CatalogMemberAdmitted,
    CatalogRetirementExisting,
)
from atelier2.contracts.revisions_v3 import PublishedRevisionHash

router = APIRouter()


@router.post(
    CATALOG_LINEAGE_MEMBERS_PATH,
    response_model=CatalogAdmissionResource,
    status_code=201,
)
async def admit_catalog_member_route(
    lineage_id: CatalogLineageIdPath,
    request: AdmitCatalogMemberRequestResource,
    context: ApiContext = api_context_dependency,
) -> CatalogAdmissionResource:
    """Admit one published revision into the lineage this path names."""

    try:
        identity = CatalogLineageId(lineage_id)
    except ValueError as error:
        raise ApiProblem("catalog-lineage-missing") from error
    try:
        actor = CatalogActor(request.actor)
        activated_at = CatalogActivatedAt(request.activated_at)
    except (TypeError, ValueError) as error:
        raise ApiProblem("invalid-request") from error
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.admit_catalog_member(
            request.kind,
            identity,
            PublishedRevisionHash(request.catalog_revision_hash),
            actor,
            activated_at,
        ),
    )
    return catalog_admission_resource(result)


@router.post(
    CATALOG_LINEAGE_RETIREMENTS_PATH,
    status_code=204,
)
async def retire_catalog_lineage_route(
    lineage_id: CatalogLineageIdPath,
    request: RetireCatalogLineageRequestResource,
    context: ApiContext = api_context_dependency,
) -> Response:
    """Retire one live lineage while preserving its immutable revision history."""

    try:
        identity = CatalogLineageId(lineage_id)
    except ValueError as error:
        raise ApiProblem("catalog-lineage-missing") from error
    try:
        actor = CatalogActor(request.actor)
        activated_at = CatalogActivatedAt(request.activated_at)
    except (TypeError, ValueError) as error:
        raise ApiProblem("invalid-request") from error

    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.retire_catalog_lineage(identity, actor, activated_at),
    )
    match result:
        case CatalogLineageRetired() | CatalogRetirementExisting():
            return Response(status_code=204)
        case CatalogAdmissionLineageMissing():
            raise ApiProblem("catalog-lineage-missing")
        case CatalogLineageIdMismatch() | DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case WriteUnavailable(detail):
            raise ApiProblem("temporarily-unavailable", detail)
        case _ as unreachable:
            assert_never(unreachable)


def catalog_admission_resource(result: object) -> CatalogAdmissionResource:
    """One resource for both acts, because both answer the same question."""

    match result:
        case CatalogLineageFounded(lineage, revision, display_name):
            return CatalogAdmissionResource(
                display_name=display_name.value,
                lineage_id=lineage.lineage_id.value,
                catalog_revision_hash=revision.revision_hash.value,
                revision_number=1,
            )
        case CatalogMemberAdmitted(lineage, revision, revision_number, display_name):
            return CatalogAdmissionResource(
                display_name=display_name.value,
                lineage_id=lineage.lineage_id.value,
                catalog_revision_hash=revision.revision_hash.value,
                revision_number=revision_number,
            )
        case CatalogAdmissionExisting(lineage, revision, revision_number, display_name):
            # Admitting what is already admitted is the same answer, not a
            # conflict: the caller asked for a state the catalog is already in.
            return CatalogAdmissionResource(
                display_name=display_name.value,
                lineage_id=lineage.lineage_id.value,
                catalog_revision_hash=revision.revision_hash.value,
                revision_number=revision_number,
            )
        case CatalogRevisionUnpublished():
            raise ApiProblem("catalog-revision-unpublished")
        case CatalogAdmissionUnpublished():
            raise ApiProblem("catalog-revision-unpublished")
        case CatalogAdmissionNameHeld():
            raise ApiProblem("catalog-name-held")
        case CatalogAdmissionRevisionOwned():
            raise ApiProblem("catalog-revision-owned")
        case CatalogAdmissionLineageMissing():
            raise ApiProblem("catalog-lineage-missing")
        case CatalogAdmissionRetired():
            raise ApiProblem("catalog-lineage-retired")
        case (
            CatalogAuthoredNameRestated()
            | CatalogExplicitNameRequired()
            | CatalogDisplayNameInvalid()
        ):
            raise ApiProblem("invalid-request")
        case CatalogAdmissionKindMismatch() | CatalogLineageIdMismatch():
            raise ApiProblem("durable-state-corrupt")
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case ProjectionTooLarge():
            raise ApiProblem("durable-projection-unrepresentable")
        case WriteUnavailable():
            raise ApiProblem("temporarily-unavailable")
        case _:
            raise ApiProblem("internal-error")
