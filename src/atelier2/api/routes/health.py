from __future__ import annotations

from fastapi import APIRouter

from atelier2.api.context import ApiContext, api_context_dependency
from atelier2.api.openapi import API_PREFIX
from atelier2.api.wire.resources import HealthResource, RedeployBlockedResource
from atelier2.application.read_redeploy_status import (
    RedeployBlocked,
    RedeployStatusUnreadable,
)

router = APIRouter()

REDEPLOY_STATUS_UNREADABLE_REASON = "auto-redeploy's own status file is unreadable"


@router.get(API_PREFIX + "/health")
async def health(context: ApiContext = api_context_dependency) -> HealthResource:
    return HealthResource(
        status="serving",
        source_commit=context.source_commit,
        source_tree=context.source_tree,
        serve_started_at=context.serve_started_at.value,
        redeploy=_redeploy_blocked_resource(context),
    )


def _redeploy_blocked_resource(
    context: ApiContext,
) -> RedeployBlockedResource | None:
    match context.use_cases.queue.read_redeploy_status():
        case RedeployBlocked(blocked_since, reason):
            return RedeployBlockedResource(blocked_since=blocked_since, reason=reason)
        case RedeployStatusUnreadable():
            return RedeployBlockedResource(
                blocked_since=None, reason=REDEPLOY_STATUS_UNREADABLE_REASON
            )
        case _:
            return None
