from datetime import UTC, datetime

import pytest

from atelier2.contracts.when import RecordedAt, recorded_instant


def test_a_recorded_instant_is_rfc3339_utc_at_second_precision() -> None:
    assert RecordedAt("2026-08-18T15:05:52Z").value == "2026-08-18T15:05:52Z"


@pytest.mark.parametrize(
    "value",
    (
        "2026-08-18T15:05:52+00:00",
        "2026-08-18 15:05:52Z",
        "2026-13-01T00:00:00Z",
        "٢٠٢٦-٠٨-١٨T١٥:٠٥:٥٢Z",
        "not-a-time",
        1,
    ),
)
def test_a_recorded_instant_refuses_anything_but_utc_seconds(value: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        RecordedAt(value)  # type: ignore[arg-type]


def test_recorded_instant_takes_the_clock_the_caller_hands_it() -> None:
    assert (
        recorded_instant(datetime(2026, 8, 18, 15, 5, 52, tzinfo=UTC)).value
        == "2026-08-18T15:05:52Z"
    )
