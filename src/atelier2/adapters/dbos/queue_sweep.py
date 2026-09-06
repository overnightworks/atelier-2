"""The queue sweep's own clock, so admitted work starts between two deploys."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Final

QUEUE_SWEEP_INTERVAL_SECONDS: Final = 300.0
"""How long a label set at the tracker may wait for the sweep that reads it.

Every tick reads the tracker for the admission label, so the interval is what
an idle project costs there: five minutes is a few hundred reads a day rather
than well over a thousand, and nobody waits it out -- an admission through the
door asks for a sweep the moment it commits, and a label is set by a person
who is not standing at the queue.
"""

QUEUE_SWEEP_THREAD_NAME: Final = "queue-sweep"
"""What the sweep's own thread is called, so a process can be asked whether one
is still running."""


class QueueSweepTicker:
    """Runs one queue sweep on its own thread, on every tick and when asked.

    The runtime owns it: started once its launch has armed recovery, stopped
    before the binding the sweep reads and writes through is destroyed --
    `stop()` returns only once no sweep is under way, so a close never
    destroys a binding a sweep still writes through. It owns the clock and
    nothing else -- what a failed sweep means, and what it
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
        """End the clock and wait for a sweep under way to finish.

        The wait has no bound of its own: the sweep's reads and writes carry
        their own timeouts, and a close that outlived them would otherwise
        destroy the binding the sweep is still writing through.
        """

        self._stopping = True
        self._wake.set()
        self._thread.join()

    def _tick(self) -> None:
        while not self._stopping:
            self._wake.wait(self._interval_seconds)
            self._wake.clear()
            if self._stopping:
                return
            self._sweep()
