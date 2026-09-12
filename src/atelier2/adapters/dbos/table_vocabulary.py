"""The one metadata every product table registers in, and how a column is spelled.

A table declaration says the same three things over and over: which metadata
holds it, which closed vocabulary a text column admits, and how an instant is
written. They live in a leaf every table module may import, so a family of
tables can have its own module without importing the schema that composes them.
"""

from __future__ import annotations

from enum import StrEnum

import sqlalchemy as sa

metadata = sa.MetaData()


def rfc3339_utc(column: str) -> str:
    """RFC 3339 UTC at second precision."""

    return f"(length({column}) = 20 AND {column} LIKE '____-__-__T__:__:__Z')"


def closed_vocabulary_sql(column: str, vocabulary: type[StrEnum]) -> str:
    """One contract's closed vocabulary, asserted of one named column.

    Spelled from the enum rather than beside it, because a vocabulary written
    by hand is how a column quietly stops admitting a word its contract owns.
    """

    admitted = ", ".join(
        f"'{member.value}'" for member in vocabulary.__members__.values()
    )
    return f"{column} IN ({admitted})"


def rfc3339_utc_or_null(column: str) -> str:
    """A recording instant is absent, or RFC 3339 UTC at second precision."""

    return f"({column} IS NULL OR {rfc3339_utc(column)})"
