from __future__ import annotations

import re
from pathlib import Path

from atelier2.contracts.agent_attempts import AgentAttemptFailureCode

PROJECT_ROOT = Path(__file__).parents[2]
FRONTEND_CLIENT = PROJECT_ROOT / "frontend" / "src" / "api" / "client.ts"
"""The hand-maintained frontend decoder, read as text below for the
failure-code vocabulary it still declares by hand.

A backend problem type needs no such hand mirror: the frontend's
`orval.transform.ts` synthesizes the decoder's problem union from
`tests/api/openapi_frozen.json` itself, `scripts/write_openapi_frozen.py
--check` proves that document matches `PROBLEM_DEFINITIONS`, and
`npm run check:generated` proves the generated union matches that document.
"""


SERVED_FAILURE_CODES = re.compile(r"failure_code: z\.enum\(\[(?P<codes>[^\]]*)\]\)")
"""The decoder's own list of the names a failed attempt can end under.

Read as text because this field stays hand-declared, and matched exactly
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
