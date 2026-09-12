from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy.engine import Connection, Engine

_KEEP_NOTHING = "atelier2_keep_nothing"


def keeping_nothing(engine: Engine) -> Engine:
    """The same database, where every canonical write transaction is rolled back.

    A queue start is judged before its launch is reserved, and the judge is the
    start itself over this engine: its answer comes from the one decision the
    real start makes, and nothing that decision wrote is kept.
    """
    return engine.execution_options(**{_KEEP_NOTHING: True})


@contextmanager
def canonical_write_transaction(engine: Engine) -> Iterator[Connection]:
    """Serialize a read-decide-write invariant from its first observation."""

    with engine.connect() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            yield connection
        except BaseException:
            connection.rollback()
            raise
        else:
            if engine.get_execution_options().get(_KEEP_NOTHING, False):
                connection.rollback()
            else:
                connection.commit()
