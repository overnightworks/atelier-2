from __future__ import annotations

from fastapi.testclient import TestClient

from atelier2.api.app import create_app
from atelier2.api.openapi import SEAT_PATH
from atelier2.api.seat import SeatReader, SeatReading, SeatState, no_seat_declared
from tests.scenarios.api import api_limits, api_ports, event_poll_backoff

SEAT_URL = "http://127.0.0.1:7681/seat-uHmVvQ/"
PROJECT_ID = "atelier-2"


def client(seat_reader: SeatReader = no_seat_declared) -> TestClient:
    """A cockpit served over the seat reading its composition bound.

    The default is the one a serve started without the seat flags binds, so
    the room without a seat is read here exactly as it is served.
    """

    return TestClient(
        create_app(
            source_commit="commit",
            source_tree="tree",
            ports=api_ports(),
            limits=api_limits(),
            event_poll_backoff=event_poll_backoff(),
            seat_reader=seat_reader,
        )
    )


def test_a_serve_without_a_seat_says_it_has_none_rather_than_nothing() -> None:
    answered = client().get(SEAT_PATH)

    assert answered.status_code == 200
    assert answered.json() == {
        "state": SeatState.MISSING.value,
        "url": None,
        "project_id": None,
    }


def test_a_living_seat_names_its_address_and_whose_seat_it_is() -> None:
    reading = SeatReading(SeatState.ALIVE, url=SEAT_URL, project_id=PROJECT_ID)

    answered = client(lambda: reading).get(SEAT_PATH).json()

    assert answered == {
        "state": SeatState.ALIVE.value,
        "url": SEAT_URL,
        "project_id": PROJECT_ID,
    }


def test_a_seat_reading_is_an_address_only_while_it_lives() -> None:
    for unaddressed in (SeatState.MISSING, SeatState.FAILED):
        try:
            SeatReading(unaddressed, url=SEAT_URL, project_id=PROJECT_ID)
        except ValueError:
            continue
        raise AssertionError(f"{unaddressed} was read as an address")
