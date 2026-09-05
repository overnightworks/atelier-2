"""Neutralizing reserved tokens and GitHub closing keywords in candidate prose.

A rendered pull request body carries this adapter's own control lines -- the
acceptance line, the effect-request marker, and (#1290) the `Work-Item`/
`Closes` classification -- around a candidate's own free-text summary and
changed-paths listing. That candidate text is provider output this adapter
does not own the intent of: it must not be able to fake a control line, nor
close or (mis)classify an issue the way GitHub's own merge-time keyword scan
would otherwise read it (`agent-claim`'s `closing_references` scans the same
keywords, case-insensitively, anywhere on a line). Every such token found in
candidate prose is broken here, before the adapter's own control lines and
classification are appended around it.
"""

from __future__ import annotations

import re

from atelier2.contracts.effect_markers import EFFECT_REQUEST_MARKER_KEY

ACCEPTANCE_LINE_PREFIX = "Literal acceptance sentence(s)"

# Candidate prose cannot supply reserved control lines.
_RESERVED_LINE_PATTERN = re.compile(
    r"^[ \t]*[-*]?[ \t]*"
    rf"({re.escape(ACCEPTANCE_LINE_PREFIX)}|{re.escape(EFFECT_REQUEST_MARKER_KEY)})"
)

# GitHub retires an issue whenever a merged pull request's body carries one of
# these keywords immediately before a `#n` (or `owner/repo#n`) reference,
# anywhere on the same line -- not only at a line's start, and regardless of
# case.
_GITHUB_CLOSING_KEYWORDS = (
    "close",
    "closes",
    "closed",
    "fix",
    "fixes",
    "fixed",
    "resolve",
    "resolves",
    "resolved",
)
_CLOSING_REFERENCE_PATTERN = re.compile(
    rf"\b({'|'.join(_GITHUB_CLOSING_KEYWORDS)})([ \t]*:?[ \t]+)"
    r"((?:[\w.-]+/[\w.-]+)?#[1-9]\d*)",
    re.IGNORECASE,
)
# The classification label this adapter renders itself; a candidate writing
# the same label anywhere is broken so only the one this adapter appends is
# ever read as a classification.
_CLASSIFICATION_LABEL_PATTERN = re.compile(r"\b(Work-Item|No-Item)(:)", re.IGNORECASE)
# Invisible in rendered Markdown and plain text alike, but enough to keep a
# keyword-plus-reference or a label-plus-colon from matching literally.
_ZERO_WIDTH_BREAK = "\u200b"


def neutralized_candidate_prose(text: str) -> str:
    """Candidate prose with every reserved line and closing keyword broken."""

    quoted = "\n".join(
        f"> {line}" if _RESERVED_LINE_PATTERN.match(line) else line
        for line in text.splitlines()
    )
    broken_references = _CLOSING_REFERENCE_PATTERN.sub(
        rf"\1\2{_ZERO_WIDTH_BREAK}\3", quoted
    )
    return _CLASSIFICATION_LABEL_PATTERN.sub(
        rf"\1{_ZERO_WIDTH_BREAK}\2", broken_references
    )
