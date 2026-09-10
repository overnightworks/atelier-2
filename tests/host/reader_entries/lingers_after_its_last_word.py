"""A reading that says its last word and is then slow to leave.

An interpreter has more to do after the reading than the reading itself takes
notice of, and on a loaded machine that outlasts the grace the stopping gives
it. What follows -- a stop, and the code it produces -- is this read's own
doing, and it must not turn a reading that finished into a reader that died.
"""

from __future__ import annotations

import time

from atelier2.host.instance_reader import (
    READER_STOP_GRACE_SECONDS,
    ReadingEnd,
    record_sink,
)
from atelier2.host.instance_reader_main import reader_invocation

LINGER_SECONDS = 20 * READER_STOP_GRACE_SECONDS
"""Far past the whole stopping -- one wait for a clean exit, one after a stop,
one after a kill -- so no loaded machine can let this process leave on its own
before the stopping reaches it. No test waits it out: the stopping ends this
process long before the sleep does."""

if __name__ == "__main__":
    record_sink(reader_invocation().descriptor)(ReadingEnd())
    time.sleep(LINGER_SECONDS)
