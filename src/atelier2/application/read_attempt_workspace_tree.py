"""Read what tree currently stands in an attempt's leased workspace.

**Why here and not inline in attempt execution.** Whether a leased tree still
matches its pin is a fact about the lease and the pin alone, asked at more
than one moment across an attempt's ending: once before a granted check, to
save paying for one where nothing changed, and once after, because the check
runs in the same workspace and may leave a tree the earlier reading never saw.
Each caller only ever asks this question; none of them derive the answer
themselves, so the one place that reads it also carries why it may be asked
more than once and what each asking is allowed to conclude.
"""

from __future__ import annotations

from atelier2.ports.agent_executions import AgentAttemptWorkspaceLease
from atelier2.ports.candidate_store import LeasedWorkingTree
from atelier2.ports.project_verification import PinnedProjectSource


def read_attempt_workspace_tree(
    lease: AgentAttemptWorkspaceLease, project: PinnedProjectSource | None
) -> LeasedWorkingTree | None:
    """What stands in the lease now, named against the pin it started from.

    Nothing is anchored under the attempt: this reads the lease and must not by
    itself keep work no ending has decided to keep.

    Asked only of an attempt that redeems a grant, because that is the only
    attempt for which "changed nothing" is a failure. A node that pinned no
    grant may honestly answer without touching a file -- a reviewer reading a
    candidate and judging it is exactly that -- and there is no verification
    cost to save there either. A runtime pointed at no project has no pin the
    work would be a change to and no store to name a tree in.
    """

    if project is None or project.grant is None:
        return None
    return project.candidates.written(project.pin, lease)
