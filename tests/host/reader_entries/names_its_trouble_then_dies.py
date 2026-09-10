"""A reading process that reports trouble it did not expect and then dies
anyway.

What a reading named and what ended its process are two different facts, and
a report that kept only the first would be reading a death as a clean exit.
"""

from __future__ import annotations

import os

from atelier2.host.atelier_api_client import TransportFailureCategory
from atelier2.host.instance_reader import (
    ReaderFailure,
    ReaderPhase,
    framed,
    reader_invocation,
)

DEATH_CODE = 4
TROUBLE = ReaderFailure(ReaderPhase.READING, TransportFailureCategory.RESET)

if __name__ == "__main__":
    os.write(reader_invocation().descriptor, framed(TROUBLE))
    os._exit(DEATH_CODE)
