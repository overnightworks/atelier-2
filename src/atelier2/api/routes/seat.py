"""The one door that says whether this serve has a terminal seat, and where."""

from __future__ import annotations

from fastapi import APIRouter

from atelier2.api.context import ApiContext, api_context_dependency
from atelier2.api.openapi import SEAT_PATH
from atelier2.api.wire.resources import SeatResource

router = APIRouter()


@router.get(SEAT_PATH)
async def seat(context: ApiContext = api_context_dependency) -> SeatResource:
    reading = context.seat()
    return SeatResource(
        state=reading.state, url=reading.url, project_id=reading.project_id
    )
