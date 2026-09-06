"""The queue sweep's own clock, so admitted work starts between two deploys."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Final

QUEUE_SWEEP_INTERVAL_SECONDS: Final = 60.0
"""How long an admitted item may wait for the sweep that starts it.

A minute is short enough that an item admitted while nobody watches is running
before anyone asks why it is not, and long enough that an idle queue costs one
small projection read a minute. Nobody waits it out after admitting through the
door: that admission asks for a sweep the moment it commits.
"""

QUEUE_SWEEP_THREAD_NAME: Final = "queue-sweep"
"""What the sweep's own thread is called, so a process can be asked whether one
is still running."""

_SWEEP_HANDOVER_SECONDS: Final = 30.0
"""How long a closing runtime waits for a sweep already under way.

Past this the sweep is left to the daemon end of its own thread rather than
holding the process open: everything it writes is a durable transaction, so an
interrupted sweep leaves decided rows, never half of one.
"""


class QueueSweepTicker:
    """Runs one queue sweep on its own thread, on every tick and when asked.

    The runtime owns it: started once its launch has armed recovery, stopped
    before the binding the sweep reads and writes through is destroyed. It
    owns the clock and nothing else -- what a failed sweep means, and what it
    says about itself, belongs to the sweep the runtime hands in, which is why
    one that raises ends the tick with it.
    """

    def __init__(
        self,
        sweep: Callable[[], None],
        *,
        interval_seconds: float = QUEUE_SWEEP_INTERVAL_SECONDS,
    ) -> None:
        self._sweep = sweep
        self._interval_seconds = interval_seconds
        self._wake = threading.Event()
        self._stopping = False
        self._thread = threading.Thread(
            target=self._tick, name=QUEUE_SWEEP_THREAD_NAME, daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def sweep_now(self) -> None:
        """Ask for a sweep at once rather than at the next tick."""

        self._wake.set()

    def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        self._thread.join(_SWEEP_HANDOVER_SECONDS)

    def _tick(self) -> None:
        while not self._stopping:
            self._wake.wait(self._interval_seconds)
            self._wake.clear()
            if self._stopping:
                return
            self._sweep()
