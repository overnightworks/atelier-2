"""A reading process whose framing stops making sense: a length no record of
this reading could have, and nothing behind it.

Which length that is belongs to the test, not to this process.
"""

from __future__ import annotations

import os

from atelier2.host.instance_reader import FRAME_HEADER
from atelier2.host.instance_reader_main import reader_invocation

FRAME_LENGTH_CHANNEL = "ATELIER2_TEST_FRAME_LENGTH"
"""Where the test tells this process which length to promise."""

if __name__ == "__main__":
    promised = int(os.environ[FRAME_LENGTH_CHANNEL])
    os.write(reader_invocation().descriptor, FRAME_HEADER.pack(promised))
    os._exit(0)
