"""What a reading process is started as: read this process's arguments, then
read the instance they name.

Its own module rather than `instance_reader`'s `__main__`, because a module
run as `__main__` is not the module the process that reports knows: a record
pickled by such a copy carries `__main__` as the place its class was defined,
and the reporting process has no class of that name. Here nothing is defined
that travels; the records come from `instance_reader`, imported under its own
name, and this entry only carries what the arguments said into it.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from atelier2.host.instance_reader import (
    READER_MODULE,
    ReaderInvocation,
    ReadingBudget,
    read_as_a_child,
)


def reader_invocation(arguments: Sequence[str] | None = None) -> ReaderInvocation:
    """What this reading process was told, read back from its own arguments.

    The other half of this protocol is `instance_reader`'s own reader command,
    which writes these arguments for the process it starts.
    """

    parser = argparse.ArgumentParser(prog=READER_MODULE, description=__doc__)
    parser.add_argument("--service", required=True)
    parser.add_argument("--descriptor", type=int, required=True)
    parser.add_argument("--parent", type=int, required=True)
    parser.add_argument("--deadline-seconds", type=float, required=True)
    parser.add_argument("--door-read-timeout-seconds", type=float, required=True)
    parser.add_argument(
        "--event-sample-read-timeout-seconds", type=float, required=True
    )
    parsed = parser.parse_args(arguments)
    return ReaderInvocation(
        service_url=parsed.service,
        budget=ReadingBudget(
            deadline_seconds=parsed.deadline_seconds,
            door_read_timeout_seconds=parsed.door_read_timeout_seconds,
            event_sample_read_timeout_seconds=(
                parsed.event_sample_read_timeout_seconds
            ),
        ),
        descriptor=parsed.descriptor,
        parent_process_id=parsed.parent,
    )


if __name__ == "__main__":
    raise SystemExit(read_as_a_child(reader_invocation()))
