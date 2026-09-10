"""A reading process that stops in the middle of a record: one whole record,
then the beginning of another it never finishes, then gone.

What arrived whole must stand in the report, and the half record must never
become one.
"""

from __future__ import annotations

import os

from atelier2.api.problems import problem_resource
from atelier2.api.wire.resources import StreamFailureResource
from atelier2.host.instance_reader import (
    FRAME_HEADER,
    FeedFrame,
    framed,
    reader_invocation,
)

_FAILED_STREAM_FRAME = StreamFailureResource(
    problem=problem_resource("durable-state-corrupt")
).model_dump_json()

WHOLE_RECORD = FeedFrame(
    data=_FAILED_STREAM_FRAME, bytes_read=len(_FAILED_STREAM_FRAME)
)
_FIRST_BYTE_OF_THE_NEXT_ONE = framed(WHOLE_RECORD)[: FRAME_HEADER.size + 1]

if __name__ == "__main__":
    descriptor = reader_invocation().descriptor
    os.write(descriptor, framed(WHOLE_RECORD))
    os.write(descriptor, _FIRST_BYTE_OF_THE_NEXT_ONE)
    os._exit(0)
