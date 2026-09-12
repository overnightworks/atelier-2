"""A reading that says its last word and then stays until it is ended.

An interpreter has more to do after the reading than the reading itself takes
notice of, and on a loaded machine that outlasts the grace the stopping gives
it. What follows -- a stop, and the code it produces -- is this read's own
doing, and it must not turn a reading that finished into a reader that died.

This process waits for a signal rather than for a clock, because a sleep short
enough for a test to sit through would let a delayed stopping meet a process
that had already left on its own. What the stopping meets here is always a
process only it can end.
"""

from __future__ import annotations

import signal

from atelier2.host.instance_reader import ReadingEnd, record_sink
from atelier2.host.instance_reader_main import reader_invocation

if __name__ == "__main__":
    record_sink(reader_invocation().descriptor)(ReadingEnd())
    while True:
        signal.pause()
