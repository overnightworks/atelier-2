from __future__ import annotations

import re
from pathlib import Path

from atelier2.api.problem_vocabulary import PROBLEM_DEFINITIONS
from atelier2.contracts.agent_attempts import AgentAttemptFailureCode

PROJECT_ROOT = Path(__file__).parents[2]
FRONTEND_CLIENT = PROJECT_ROOT / "frontend" / "src" / "api" / "client.ts"
"""The hand-maintained frontend decoder that must mirror every backend problem.

The exact bidirectional match is enforced by the Cockpit vitest against the
frozen OpenAPI document. That job is slow and frontend-only, so a backend
addition that forgets its mirror stays invisible to the fast Python suite until
a full frontend/e2e cycle runs. This guard reads the decoder as text and fails
here instead, naming the missing keys.
"""


def test_every_backend_problem_type_is_mirrored_in_the_frontend_decoder() -> None:
    client_source = FRONTEND_CLIENT.read_text(encoding="utf-8")

    missing = sorted(
        code for code in PROBLEM_DEFINITIONS if f'"{code}"' not in client_source
    )

    assert not missing, (
        "The backend publishes problem types the frontend decoder does not "
        f'mirror. Add each as a "<key>" literal to problemDefinitions and the '
        f"problemSchema union in {FRONTEND_CLIENT.relative_to(PROJECT_ROOT)}: "
        + ", ".join(missing)
    )


SERVED_FAILURE_CODES = re.compile(r"failure_code: z\.enum\(\[(?P<codes>[^\]]*)\]\)")
"""The decoder's own list of the names a failed attempt can end under.

Read as text for the same reason as the problem keys above, and matched exactly
rather than by containment: a name the decoder does not know makes the event
unreadable to the cockpit, and a name nothing serves would let a dead branch
sit there claiming a state no run can reach.
"""


def test_the_frontend_decoder_mirrors_every_served_attempt_failure_code() -> None:
    matched = SERVED_FAILURE_CODES.search(FRONTEND_CLIENT.read_text(encoding="utf-8"))

    assert matched is not None, (
        "The frontend decoder no longer declares the AGENT_FAILED failure_code "
        f"vocabulary as a z.enum in {FRONTEND_CLIENT.relative_to(PROJECT_ROOT)}."
    )
    decoded = set(re.findall(r'"([A-Z_]+)"', matched.group("codes")))

    assert decoded == {code.value for code in AgentAttemptFailureCode}, (
        "The names an attempt can fail under and the ones the frontend decodes "
        f"differ. Make the failure_code z.enum in "
        f"{FRONTEND_CLIENT.relative_to(PROJECT_ROOT)} the owner's whole "
        "membership."
    )
