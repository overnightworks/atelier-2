"""Which failures a durable read owes to one row, and which to the read itself.

Every reader in this adapter meets the same two families, and the difference
between them decides what a page can still answer: a row's own unreadable state
can be named and stepped over, while an admitted-size bound or an unreachable
store says nothing about any row and must reach the page-level answer.
"""

from __future__ import annotations

from sqlalchemy.exc import DatabaseError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from atelier2.ports.workflow_revisions import ProjectionLimitExceeded

DURABLE_PROJECTION_FAILURES = (
    UnicodeEncodeError,
    TypeError,
    ValueError,
    RuntimeError,
    DatabaseError,
)
"""What unreadable durable state raises while this adapter projects it.

Every stored-shape disagreement it can meet is one of these: `RunTransitionConflict`
and `RevisionHashCollision` are `RuntimeError`, and a stored document that today's
parser refuses is a `ValueError`. Naming the family once is what lets a reader
that can isolate rows name the one row it belongs to instead of refusing the
whole page that row happens to sit on, and lets a reader that cannot say so in
the same words.
"""

READ_EDGE_FAILURES = (
    ProjectionLimitExceeded,
    OperationalError,
    PoolTimeoutError,
)
"""What belongs to the read edge itself rather than to any row it was reading.

An admitted-size bound and a store momentarily out of reach say nothing about
the row in hand, so they must reach the page-level answer instead of being told
as that row's defect. `ProjectionLimitExceeded` is a `ValueError`, so this
family is always caught before `DURABLE_PROJECTION_FAILURES`.
"""
