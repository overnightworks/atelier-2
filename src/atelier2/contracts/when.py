"""An attributed recording instant, RFC 3339 UTC at second precision.

Time is recording metadata, not identity: it does not enter an event hash, a
run id, or a terminal hash. The catalog's activation instant is a different
owner — a catalog event is not a run, an attempt, or a node event.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

RECORDED_AT_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
_RECORDED_AT = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})Z",
    re.ASCII,
)


@dataclass(frozen=True)
class RecordedAt:
    """When a run, attempt, or event was written, as RFC 3339 UTC."""

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str):
            raise TypeError("a recorded instant must be text")
        matched = _RECORDED_AT.fullmatch(self.value)
        if matched is None:
            raise ValueError(
                "a recorded instant must be RFC 3339 UTC at second precision"
            )
        try:
            datetime(
                int(matched[1]),
                int(matched[2]),
                int(matched[3]),
                int(matched[4]),
                int(matched[5]),
                int(matched[6]),
                tzinfo=UTC,
            )
        except ValueError:
            raise ValueError(
                "a recorded instant must be RFC 3339 UTC at second precision"
            ) from None


def recorded_instant(now: datetime | None = None) -> RecordedAt:
    """The recording instant, taken from the caller or from the clock."""

    instant = datetime.now(UTC) if now is None else now.astimezone(UTC)
    return RecordedAt(instant.strftime("%Y-%m-%dT%H:%M:%SZ"))
