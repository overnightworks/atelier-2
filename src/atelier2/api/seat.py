"""What the cockpit is told about this serve's terminal seat.

The seat itself is a host concern -- a tmux session, a terminal server, a
process tree. What crosses into the API is only this: whether a terminal
answers right now, and where the browser reaches it. The composition binds the
reader; a serve that declares no seat binds `no_seat_declared`, so the door
always answers with a state rather than with nothing.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum


class SeatState(Enum):
    """Whether this serve's seat has a terminal to show right now."""

    ALIVE = "ALIVE"
    MISSING = "MISSING"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class SeatReading:
    """One reading of the seat: a living address, or a state without one."""

    state: SeatState
    url: str | None = None
    project_id: str | None = None

    def __post_init__(self) -> None:
        addressed = self.url is not None and self.project_id is not None
        if addressed is not (self.state is SeatState.ALIVE):
            raise ValueError(
                "a living seat is read as its address and its project, and only "
                "a living one is"
            )


SeatReader = Callable[[], SeatReading]


def no_seat_declared() -> SeatReading:
    """What a serve started without the seat flags answers."""

    return SeatReading(SeatState.MISSING)
