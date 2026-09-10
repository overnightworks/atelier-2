"""A reading process that ends its pipe and then refuses to end itself.

The gathering sees the reading through -- there is nothing more to come -- and
the stopping then meets a process that ignores being told to go, which is the
one it may wait for and must still get rid of.
"""

from __future__ import annotations

import os
import signal
import time

from atelier2.host.instance_reader import reader_invocation

UNENDING_SLEEP_SECONDS = 60.0

if __name__ == "__main__":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    os.close(reader_invocation().descriptor)
    time.sleep(UNENDING_SLEEP_SECONDS)
