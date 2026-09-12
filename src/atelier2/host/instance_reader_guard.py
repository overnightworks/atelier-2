"""What a reading process does to itself before it reads anything: go quiet,
and arm its own death for the moment nobody is left to read its report.

Both are pokes at state the whole process shares -- the logging tree, and the
kernel's own parent-death signal -- and both are about this process's
disposition rather than about any instance it reads, which is why they stand
apart from the reading (`instance_reader`) that runs under them.
"""

from __future__ import annotations

import ctypes
import logging
import os
import signal
from typing import Final

TRANSPORT_LOGGER_NAMES: Final = ("httpx", "httpcore")
"""The libraries the reading process silences in itself: httpx logs every
request's status line -- the far side's own reason phrase included -- at
`INFO`, and httpcore logs a reply's headers at `DEBUG`."""

_SILENT_LEVEL: Final = logging.CRITICAL + 1
"""Above every level `logging` defines, so a logger set to it makes no record
at all."""

_PARENT_DEATH_SIGNAL_OPTION: Final = 1
"""`PR_SET_PDEATHSIG` (`linux/prctl.h`), the same arming
`adapters/agent_process_exec_guard.py` gives an agent's own child."""

_ORPHANED_EXIT_CODE: Final = 0
"""How a reading process ends when the process that would read its report is
already gone: quietly, because nothing it did would be looked at."""


def go_quiet() -> None:
    """Leave this process nothing to say anywhere but its pipe.

    The reading process is private: it reports through its pipe and has no
    reader for anything else, so nothing in it may log at all.
    `logging.disable` is what makes that true whoever asks -- a level on the
    two library loggers is walked past by a child logger that sets its own,
    and a handler hung directly on `httpcore.http11` would then carry the far
    side's headers to wherever it points -- and the root keeps a handler that
    drops what is left. Where a write below Python would land is not decided
    here: the standard streams of a reading process belong to whoever starts
    it, and `supervised_reading` gives them the null device.
    """

    logging.disable(logging.CRITICAL)
    logging.getLogger().handlers = [logging.NullHandler()]
    for name in TRANSPORT_LOGGER_NAMES:
        logging.getLogger(name).setLevel(_SILENT_LEVEL)


def die_with_the_parent(parent_process_id: int) -> None:
    """Ask the kernel to kill this process when the process that reads its
    report is gone, and leave at once if it already is.

    A parent that died between this process starting and this arming would
    leave the arming pointing at whoever adopted this process instead -- an
    ask that would never come -- so the parent is checked again afterwards,
    and a reading nobody is waiting for ends here without a word.

    The same arming an agent's own child gets before it execs
    (`adapters/agent_process_exec_guard.py`), which cannot be called here:
    that function never returns, and wants a cgroup and a watchdog this
    reading has neither of.
    """

    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PARENT_DEATH_SIGNAL_OPTION, signal.SIGKILL) != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno))
    if os.getppid() != parent_process_id:
        os._exit(_ORPHANED_EXIT_CODE)
