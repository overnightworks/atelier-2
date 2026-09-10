"""A reading process carrying the worst case for silence: a handler hung on
`httpcore.http11` itself, at `DEBUG`, with that logger's own level set -- so
neither a level on its parents nor a filter on the root's handlers could keep
a reply's headers out of that file."""

from __future__ import annotations

import logging
import os

from atelier2.host.instance_reader import read_as_a_child
from atelier2.host.instance_reader_main import reader_invocation

CHATTER_LOG_CHANNEL = "ATELIER2_TEST_CHATTER_LOG"
"""Where the test tells this process to write what it hears."""

if __name__ == "__main__":
    chatter = logging.getLogger("httpcore.http11")
    chatter.setLevel(logging.DEBUG)
    chatter.addHandler(logging.FileHandler(os.environ[CHATTER_LOG_CHANNEL]))
    read_as_a_child(reader_invocation())
