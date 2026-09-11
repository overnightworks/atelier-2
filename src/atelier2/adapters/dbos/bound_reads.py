"""A bound select yields one mapped row, or none."""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import RowMapping

from atelier2.adapters.dbos.sql_executor import SqlExecutor


def one_record(connection: SqlExecutor, statement: sa.Select[Any]) -> RowMapping | None:
    return connection.execute(statement).mappings().one_or_none()
