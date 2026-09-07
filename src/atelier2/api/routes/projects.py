from __future__ import annotations

from typing import assert_never

from fastapi import APIRouter

from atelier2.api._support import (
    decode_public_project_reference_value,
    run_control_query,
)
from atelier2.api.context import ApiContext, api_context_dependency
from atelier2.api.openapi import PROJECT_PATH, PROJECTS_PATH
from atelier2.api.problems import ApiProblem
from atelier2.api.projection.projects import project_list_resource, project_resource
from atelier2.api.references import PublicProjectReferencePath
from atelier2.api.wire.resources import ProjectListResource, ProjectResource
from atelier2.application.read_projects import (
    ProjectListRead,
    ProjectRead,
    ServedProjectUnknown,
)
from atelier2.application.refusals import DurableStateCorrupt, ReadUnavailable

router = APIRouter()


@router.get(PROJECTS_PATH)
async def list_projects_route(
    context: ApiContext = api_context_dependency,
) -> ProjectListResource:
    result = await run_control_query(
        context.control_runner, context.use_cases.list_projects
    )
    match result:
        case ProjectListRead() as projects:
            return project_list_resource(projects)
        case ServedProjectUnknown():
            raise ApiProblem("project-unknown")
        case ReadUnavailable(detail):
            raise ApiProblem("temporarily-unavailable", detail)
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)


@router.get(PROJECT_PATH)
async def get_project_route(
    public_project_reference: PublicProjectReferencePath,
    context: ApiContext = api_context_dependency,
) -> ProjectResource:
    project_id = decode_public_project_reference_value(
        public_project_reference, context.limits
    )
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.get_project(project_id),
    )
    match result:
        case ProjectRead() as project:
            return project_resource(project)
        case ServedProjectUnknown():
            raise ApiProblem("project-unknown")
        case ReadUnavailable(detail):
            raise ApiProblem("temporarily-unavailable", detail)
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)
