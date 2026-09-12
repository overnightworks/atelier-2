"""A reading process that says when it has taken the feed's first frame off
the wire.

A server that means to tear its connection down after a frame has to know
when that frame has been read, because a reset discards whatever the peer has
not taken yet -- and a wait long enough to hope for it is a wait every run
pays. Only the sink is replaced here; everything the reading does is the
production reader's.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from atelier2.host import instance_reader
from atelier2.host.instance_reader import FeedFrame, ReadingRecord
from atelier2.host.instance_reader_main import reader_invocation

FIRST_FRAME_CHANNEL = "ATELIER2_TEST_FIRST_FRAME_SIGNAL"
"""Where the test tells this process to leave word that a frame has arrived."""

_record_sink = instance_reader.record_sink


def _sink_that_says_when_a_frame_arrived(
    descriptor: int,
) -> Callable[[ReadingRecord], None]:
    send = _record_sink(descriptor)
    arrived = Path(os.environ[FIRST_FRAME_CHANNEL])

    def send_and_say(record: ReadingRecord) -> None:
        send(record)
        if isinstance(record, FeedFrame):
            arrived.touch()

    return send_and_say


if __name__ == "__main__":
    instance_reader.record_sink = _sink_that_says_when_a_frame_arrived
    instance_reader.read_as_a_child(reader_invocation())
