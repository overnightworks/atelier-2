"""A reading process that will not go when it is asked, and never ends its
pipe: only a kill ends this one, and the stopping must not wait for it to
change its mind."""

from __future__ import annotations

import signal
import time

UNENDING_SLEEP_SECONDS = 60.0

if __name__ == "__main__":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(UNENDING_SLEEP_SECONDS)
