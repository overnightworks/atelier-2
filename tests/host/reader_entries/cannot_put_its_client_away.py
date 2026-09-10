"""A reading process whose client refuses to be put away.

Every door is read and reported first, so the only trouble in this reading is
in the one phase that comes after them. The client is the reading's boundary
to the network and this stands in for it here; everything else is the
production reader.
"""

from __future__ import annotations

import errno

from atelier2.host import instance_reader
from atelier2.host.instance_reader_main import reader_invocation


class ClientThatCannotBePutAway(instance_reader.AtelierApi):
    def close(self) -> None:
        raise OSError(errno.EIO, "this client will not be put away")


if __name__ == "__main__":
    instance_reader.AtelierApi = ClientThatCannotBePutAway
    instance_reader.read_as_a_child(reader_invocation())
