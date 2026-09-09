"""What a human reads: an `OpenPullRequest` candidate report rendered to title and body.

Rendering is a pure text transformation -- title cap and word-boundary
truncation, prose neutralization, acceptance line, body length cap -- decided
once here, so `live_effects.py`'s readback-then-create protocol against
`githubkit` stays about that protocol rather than about prose.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from atelier2.adapters.github.pull_request_prose import (
    ACCEPTANCE_LINE_PREFIX,
    neutralized_candidate_prose,
)
from atelier2.adapters.github.tracker_reference import github_issue_number
from atelier2.contracts.effect_markers import marker_line
from atelier2.contracts.effect_requests import OpenPullRequest

_DEFAULT_PULL_REQUEST_TITLE = "Atelier open-pr"

# A GitHub pull request title reads as a headline, not a paragraph; 72
# characters is the conventional commit-summary width every reader here
# already expects (ADR-independent editorial choice, not a platform limit).
_MAXIMUM_RENDERED_TITLE_CHARACTERS = 72
_SENTENCE_TERMINATOR = re.compile(r"[.!?](?:\s|$)")

# Marks a title cut before its sentence ended, so a reader never mistakes the
# cut for the sentence's own end. It counts as one of the 72 characters like
# every other glyph, so the marked title still fits the same cap.
_TITLE_TRUNCATION_MARK = "…"

# A candidate's own summary is provider text: unbounded, and never rendered
# into Markdown without a ceiling. 4000 bounds the complete rendered body --
# prose, acceptance line, and trailer together -- keeping it readable and
# leaving ample room below GitHub's own much larger limit.
_MAXIMUM_RENDERED_BODY_CHARACTERS = 4000
_RENDERED_BODY_TRUNCATION_NOTE = "\n\n[truncated at 4000 characters]"


@dataclass(frozen=True)
class RenderedOpenPullRequest:
    """The readable title and body an `OpenPullRequest`'s raw report renders to."""

    title: str
    body: str


def _summary_and_changed_paths(raw_body: str) -> tuple[str, tuple[str, ...]]:
    """Read the builder's own summary and changed paths from its raw report.

    `raw_body` is provider output carried verbatim in the request (the
    `issue_to_pr_candidate_report` schema's `summary`/`changed_paths`
    document, when the workflow that produced it declares that shape). A
    request bound to a looser body schema, or an answer that failed its own
    contract, still owes a pull request: its whole text becomes the summary
    and no path list renders, rather than refusing the effect over a report
    this adapter does not own the shape of.
    """

    try:
        decoded = json.loads(raw_body)
    except json.JSONDecodeError:
        return raw_body.strip(), ()
    if not isinstance(decoded, dict):
        return raw_body.strip(), ()
    summary = decoded.get("summary")
    if not isinstance(summary, str) or not summary:
        return raw_body.strip(), ()
    changed_paths = decoded.get("changed_paths")
    if isinstance(changed_paths, list) and all(
        isinstance(path, str) and path for path in changed_paths
    ):
        return summary, tuple(changed_paths)
    return summary, ()


def _truncated_at_word_boundary(sentence: str) -> str:
    """Cut a sentence that does not fit the title cap at its last whole word.

    A character cut at the cap can land inside a word; this instead gives up
    the width the truncation mark itself needs, then backs off to the last
    space still inside that budget. A sentence with no space that early keeps
    its own character cut rather than collapsing to an empty title.
    """

    budget = _MAXIMUM_RENDERED_TITLE_CHARACTERS - len(_TITLE_TRUNCATION_MARK)
    truncated = sentence[:budget]
    boundary = truncated.rfind(" ")
    if boundary > 0:
        truncated = truncated[:boundary]
    return f"{truncated.rstrip()}{_TITLE_TRUNCATION_MARK}"


def _rendered_title(summary: str) -> str:
    stripped = summary.strip()
    if not stripped:
        return _DEFAULT_PULL_REQUEST_TITLE
    first_line = stripped.splitlines()[0]
    ending = _SENTENCE_TERMINATOR.search(first_line)
    sentence = (first_line[: ending.start() + 1] if ending else first_line).strip()
    if len(sentence) > _MAXIMUM_RENDERED_TITLE_CHARACTERS:
        title = _truncated_at_word_boundary(sentence)
    else:
        title = sentence
    return title or _DEFAULT_PULL_REQUEST_TITLE


def _acceptance_line(request: OpenPullRequest) -> str:
    # The default exemption every Atelier-opened pull request states until an
    # item with `proves(...)` sentences supplies the real identifiers. A
    # reader recognizes the work-item reference; the branch name is only the
    # fallback identity when the request carries no reference at all.
    identity = (
        request.work_item_reference.value
        if request.work_item_reference is not None
        else request.head_branch.value
    )
    return f"{ACCEPTANCE_LINE_PREFIX}: none: opened by the Atelier from work item {identity}"


def _bounded_prose(prose: str, tail: str) -> str:
    if len(prose) + len(tail) <= _MAXIMUM_RENDERED_BODY_CHARACTERS:
        return f"{prose}{tail}"
    budget = max(
        0,
        _MAXIMUM_RENDERED_BODY_CHARACTERS
        - len(tail)
        - len(_RENDERED_BODY_TRUNCATION_NOTE),
    )
    return f"{prose[:budget]}{_RENDERED_BODY_TRUNCATION_NOTE}{tail}"


def rendered_open_pull_request(
    request: OpenPullRequest, request_hash: str
) -> RenderedOpenPullRequest:
    summary, changed_paths = _summary_and_changed_paths(request.body)
    sections = [summary]
    if changed_paths:
        sections.append(
            "Changed paths:\n" + "\n".join(f"- {path}" for path in changed_paths)
        )
    prose = neutralized_candidate_prose("\n\n".join(sections))
    classification = ""
    if request.work_item_reference is not None:
        issue_number = github_issue_number(request.work_item_reference)
        classification = f"\n\nWork-Item: #{issue_number}\n\nCloses #{issue_number}"
    # The classification lines live in the untruncatable tail, alongside the
    # acceptance line and marker: truncating a long candidate summary must
    # never also drop the lines a queue landing classifies this pull request by.
    tail = (
        f"{classification}\n\n{_acceptance_line(request)}"
        f"\n\n{marker_line(request_hash)}\n"
    )
    return RenderedOpenPullRequest(
        _rendered_title(summary), _bounded_prose(prose, tail)
    )
