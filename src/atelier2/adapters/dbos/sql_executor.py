"""The executor a durable-store function needs: Core `execute`, satisfied by an engine connection and a datasource session."""

from __future__ import annotations

from typing import Protocol

from sqlalchemy.engine import Result
from sqlalchemy.sql import Executable


class SqlExecutor(Protocol):
    def execute(self, statement: Executable, /) -> Result: ...
