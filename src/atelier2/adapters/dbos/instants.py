"""Recording instants sit beside the row they describe, never inside its hash."""

from __future__ import annotations

from atelier2.adapters.dbos.schema import attempt_instants, event_instants, run_instants
from atelier2.adapters.dbos.sql_executor import SqlExecutor
from atelier2.contracts.when import RecordedAt, recorded_instant


def record_run_started(
    session: SqlExecutor, run_id: str, at: RecordedAt | None = None
) -> None:
    session.execute(
        run_instants.insert().values(
            run_id=run_id,
            started_at=(at or recorded_instant()).value,
            ended_at=None,
        )
    )


def record_run_ended(
    session: SqlExecutor, run_id: str, at: RecordedAt | None = None
) -> None:
    session.execute(
        run_instants.update()
        .where(run_instants.c.run_id == run_id, run_instants.c.ended_at.is_(None))
        .values(ended_at=(at or recorded_instant()).value)
    )


def record_attempt_started(
    session: SqlExecutor, attempt_id: str, at: RecordedAt | None = None
) -> None:
    session.execute(
        attempt_instants.insert().values(
            attempt_id=attempt_id,
            started_at=(at or recorded_instant()).value,
            ended_at=None,
        )
    )


def record_attempt_ended(
    session: SqlExecutor, attempt_id: str, at: RecordedAt | None = None
) -> None:
    session.execute(
        attempt_instants.update()
        .where(
            attempt_instants.c.attempt_id == attempt_id,
            attempt_instants.c.ended_at.is_(None),
        )
        .values(ended_at=(at or recorded_instant()).value)
    )


def record_event_instant(
    session: SqlExecutor, run_id: str, event_sequence: int, at: RecordedAt | None = None
) -> None:
    session.execute(
        event_instants.insert().values(
            run_id=run_id,
            event_sequence=event_sequence,
            recorded_at=(at or recorded_instant()).value,
        )
    )
