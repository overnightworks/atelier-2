"""The `gh:<n>` tracker-reference grammar this GitHub adapter package owns.

ADR 0010: what a tracker reference means is the connected platform adapter's
contract, so a `TrackerItemReference` stays opaque outside this package. Both
directions of the grammar -- an issue number to its reference, and a
reference back to its issue number -- share one owner here so that reading
(`observation.py`) and rendering (`live_effects.py`) parse the same spelling
without depending on each other.
"""

from __future__ import annotations

from atelier2.contracts.queue_projection import TrackerItemReference

GITHUB_TRACKER_REFERENCE_PREFIX = "gh:"


def github_tracker_reference(issue_number: int) -> TrackerItemReference:
    """The `gh:<n>` spelling this adapter owns for one GitHub issue."""

    return TrackerItemReference(f"{GITHUB_TRACKER_REFERENCE_PREFIX}{issue_number}")


def github_issue_number_or_none(reference: TrackerItemReference) -> int | None:
    """The item this reference addresses here, or nothing if it addresses none.

    A reference in another adapter's grammar is not an error to raise: it names
    no item in this repository, which is what the caller is told.
    """

    if not reference.value.startswith(GITHUB_TRACKER_REFERENCE_PREFIX):
        return None
    digits = reference.value.removeprefix(GITHUB_TRACKER_REFERENCE_PREFIX)
    if not (digits.isascii() and digits.isdigit()):
        return None
    number = int(digits)
    return number if number >= 1 else None


def github_issue_number(reference: TrackerItemReference) -> int:
    """The positive GitHub issue number a GitHub tracker reference carries.

    Unlike `github_issue_number_or_none`, a reference this adapter bound to an
    `open-pr` intent is never meant to name another adapter's grammar: one that
    does is a durable-binding defect, not external input to report softly.
    """

    number = github_issue_number_or_none(reference)
    if number is None:
        raise ValueError("an open-pr work item reference names one GitHub issue")
    return number
