"""A bound select yields one mapped row, or none."""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection, RowMapping


def one_record(connection: Connection, statement: sa.Select[Any]) -> RowMapping | None:
    return connection.execute(statement).mappings().one_or_none()
