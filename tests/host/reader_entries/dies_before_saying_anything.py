"""A reading process that dies where a real one would read.

`os._exit` skips every handler on the way out, which is what a crashed reader
looks like from the outside.
"""

from __future__ import annotations

import os

DEATH_CODE = 3

if __name__ == "__main__":
    os._exit(DEATH_CODE)
