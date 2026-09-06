"""The clock that keeps the queue moving between two deploys.

What the sweep itself decides belongs to `advance_queue`; this file pins only
what the ticker promises about running it: again and again on its own, at once
when a door asks, and never after the runtime stopped it.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from atelier2.adapters.dbos.queue_sweep import QueueSweepTicker

# Short enough that a tick lands inside a test's patience, long enough that the
# "asked for" and "stopped" cases are not carried by the tick behind them.
_FAST_TICK_SECONDS = 0.02
_SLOW_TICK_SECONDS = 30.0
_PATIENCE_SECONDS = 5.0
# Long enough for a stop that does not wait to have returned.
_A_MOMENT_SECONDS = 0.1


@dataclass
class _RecordedSweeps:
    """Counts sweeps and lets a test wait for the next one instead of sleeping."""

    swept: threading.Event = field(default_factory=threading.Event)
    count: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __call__(self) -> None:
        with self._lock:
            self.count += 1
        self.swept.set()

    def wait_for_next(self) -> None:
        self.swept.clear()
        assert self.swept.wait(_PATIENCE_SECONDS), "the sweep was never run"

    def counted(self) -> int:
        with self._lock:
            return self.count


def test_the_ticker_sweeps_again_and_again_on_its_own_clock() -> None:
    sweeps = _RecordedSweeps()
    ticker = QueueSweepTicker(sweeps, interval_seconds=_FAST_TICK_SECONDS)

    ticker.start()
    try:
        sweeps.wait_for_next()
        sweeps.wait_for_next()
    finally:
        ticker.stop()

    assert sweeps.counted() >= 2


def test_an_asked_for_sweep_does_not_wait_out_the_tick() -> None:
    sweeps = _RecordedSweeps()
    ticker = QueueSweepTicker(sweeps, interval_seconds=_SLOW_TICK_SECONDS)

    ticker.start()
    try:
        ticker.sweep_now()
        sweeps.wait_for_next()
    finally:
        ticker.stop()

    assert sweeps.counted() == 1


def test_a_stopped_ticker_sweeps_no_more() -> None:
    sweeps = _RecordedSweeps()
    ticker = QueueSweepTicker(sweeps, interval_seconds=_FAST_TICK_SECONDS)

    ticker.start()
    sweeps.wait_for_next()
    ticker.stop()
    settled = sweeps.counted()
    ticker.sweep_now()

    assert sweeps.counted() == settled


def test_stop_returns_only_once_a_sweep_under_way_has_ended() -> None:
    entered = threading.Event()
    may_end = threading.Event()

    def sweep_that_waits_to_end() -> None:
        entered.set()
        assert may_end.wait(_PATIENCE_SECONDS), "the sweep was never let go"

    ticker = QueueSweepTicker(
        sweep_that_waits_to_end, interval_seconds=_SLOW_TICK_SECONDS
    )
    ticker.start()
    ticker.sweep_now()
    assert entered.wait(_PATIENCE_SECONDS), "the sweep was never run"
    stopping = threading.Thread(target=ticker.stop)
    stopping.start()

    stopping.join(_A_MOMENT_SECONDS)
    assert stopping.is_alive(), "stop returned while a sweep was still under way"

    may_end.set()
    stopping.join(_PATIENCE_SECONDS)
    assert not stopping.is_alive(), "stop never returned after the sweep ended"
