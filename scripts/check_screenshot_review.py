from __future__ import annotations

import hashlib
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# REQ-UIQ-14 ("the surface may look great and be fun; the screenshot yardstick
# is Mockup v9, and the operator has the last word") cannot be proven by a
# test, so this gate proves the *freshness* of the operator's judgement
# instead: it pins the operator's approval to an exact digest over the exact
# source files that render the surfaces being judged. #994 owns the design;
# `scripts/requirement_contract.py` is the sibling pattern for requirement
# revisions -- this is a different governance subject (a screenshot verdict,
# not a requirement document) with its own registry and its own scope.

LEDGER_LOCATION = Path("docs/requirements/0003-ziel-ui-screenshot-reviews.toml")
GOVERNED_REQUIREMENT = "REQ-UIQ-14"
REQUIREMENT_IDENTIFIER = re.compile(r"^REQ-[A-Z0-9]+-[0-9]{2}$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")
ROOT_FIELDS = frozenset({"schema_version"})
ENTRY_FIELDS = frozenset(
    {"requirement", "scope_sha256", "approval_comment_id", "approval_sha256"}
)

# The digest scope: exactly the files that render REQ-UI-25's five core
# surfaces (REQ-UIQ-12: the three rooms Workbench/Catalog/History, Settings,
# and the Run view) plus the mockup they are judged against. Nothing else --
# a wider scope (any frontend change) would force a rubber-stamped
# re-approval on every PR and make this gate theatre (#994).
#
# Each group below is derived, not guessed:
#   pages       -- the one Svelte component App.svelte mounts per core
#                  surface (`frontend/src/lib/route.ts`, `App.svelte`).
#   shell       -- WorkshopShell (the rail, in every surface), ConnectionNotice
#                  (shown on every surface but Workbench), and App.svelte,
#                  which composes shell plus page and decides ConnectionNotice's
#                  placement (`{#if route.page !== "workbench"}`) -- all three
#                  wrap or govern every core surface's rendered pixels.
#   components  -- every `.svelte` file the five pages import, transitively,
#                  following only `.svelte`-to-`.svelte` imports (the same
#                  render-tree walk `frontend/tests/support/workshopSources.ts`
#                  already performs for REQ-UIQ-03's copy-ownership gate),
#                  seeded from the five pages plus the shell above and
#                  excluding everything reachable only through the non-core
#                  WorkflowDetailPage.
#   copy        -- every `*Copy.ts` module that render-tree reaches (the
#                  codebase's own convention for owning visible strings, see
#                  `frontend/tests/app/roomCopyOwner.test.ts`), plus the three
#                  leaf constant modules the shared shell reads its own
#                  visible strings from (`productName.ts`, `project.ts`,
#                  `workshop.ts` -- none of them named `*Copy.ts`, all three
#                  holding literal rail/room/project text).
#   tokens      -- the one file every colour, length, weight, and beat is
#                  declared in; editing it re-skins every surface.
#   mockup      -- the frozen picture owner (ADR 0019) the surfaces are
#                  judged against, plus the frozen version before it: v8 is
#                  still read by `frontend/tests/e2e/run-log.spec.ts`, whose
#                  frame `#v8-14-run-log` is a judged surface's yardstick, so
#                  editing either picture must reopen the verdict.
_PAGES = (
    Path("frontend/src/pages/WorkbenchPage.svelte"),
    Path("frontend/src/pages/CatalogPage.svelte"),
    Path("frontend/src/pages/HistoryPage.svelte"),
    Path("frontend/src/pages/SettingsPage.svelte"),
    Path("frontend/src/pages/RunCockpitPage.svelte"),
)
_SHELL = (
    Path("frontend/src/App.svelte"),
    Path("frontend/src/components/WorkshopShell.svelte"),
    Path("frontend/src/components/ConnectionNotice.svelte"),
)
_COMPONENTS = (
    Path("frontend/src/components/AddModelSheet.svelte"),
    Path("frontend/src/components/AttemptTranscript.svelte"),
    Path("frontend/src/components/BackLink.svelte"),
    Path("frontend/src/components/CatalogImportSheet.svelte"),
    Path("frontend/src/components/CatalogTile.svelte"),
    Path("frontend/src/components/ConnectSourceSheet.svelte"),
    Path("frontend/src/components/DefectiveRunRow.svelte"),
    Path("frontend/src/components/DisconnectSourceSheet.svelte"),
    Path("frontend/src/components/InfoHint.svelte"),
    Path("frontend/src/components/LoadingState.svelte"),
    Path("frontend/src/components/NodeDetailPanel.svelte"),
    Path("frontend/src/components/PinnedDecision.svelte"),
    Path("frontend/src/components/PoisonedJournalDiscardSheet.svelte"),
    Path("frontend/src/components/PoisonedJournalDoor.svelte"),
    Path("frontend/src/components/ProblemNotice.svelte"),
    Path("frontend/src/components/ProofAnchor.svelte"),
    Path("frontend/src/components/ProviderAccounts.svelte"),
    Path("frontend/src/components/ReadState.svelte"),
    Path("frontend/src/components/ReadableResult.svelte"),
    Path("frontend/src/components/RenewSourceTokenSheet.svelte"),
    Path("frontend/src/components/RunCancelCard.svelte"),
    Path("frontend/src/components/RunForkSheet.svelte"),
    Path("frontend/src/components/StateMark.svelte"),
    Path("frontend/src/components/ThreeFactConfirmSheet.svelte"),
    Path("frontend/src/components/V3AnswerCard.svelte"),
    Path("frontend/src/components/V3RunView.svelte"),
    Path("frontend/src/components/WorkflowGraphDrawing.svelte"),
)
_COPY = (
    Path("frontend/src/lib/backLinkCopy.ts"),
    Path("frontend/src/lib/catalogPageCopy.ts"),
    Path("frontend/src/lib/decisionStatusCopy.ts"),
    Path("frontend/src/lib/displayCopy.ts"),
    Path("frontend/src/lib/historyPageCopy.ts"),
    Path("frontend/src/lib/journalPoisonedCopy.ts"),
    Path("frontend/src/lib/problemNoticeCopy.ts"),
    Path("frontend/src/lib/proofAnchorCopy.ts"),
    Path("frontend/src/lib/railCopy.ts"),
    Path("frontend/src/lib/readStateCopy.ts"),
    Path("frontend/src/lib/runPageCopy.ts"),
    Path("frontend/src/lib/runResultCopy.ts"),
    Path("frontend/src/lib/seatCopy.ts"),
    Path("frontend/src/lib/settingsPageCopy.ts"),
    Path("frontend/src/lib/stateMarkCopy.ts"),
    Path("frontend/src/lib/workbenchPageCopy.ts"),
    Path("frontend/src/lib/workflowGraphCopy.ts"),
    Path("frontend/src/lib/productName.ts"),
    Path("frontend/src/lib/project.ts"),
    Path("frontend/src/lib/workshop.ts"),
)
_TOKENS = (Path("frontend/src/styles.css"),)
_MOCKUP = (
    Path("docs/requirements/0003-ziel-ui-mockup-v8.html"),
    Path("docs/requirements/0003-ziel-ui-mockup-v9.html"),
)

SCOPE: tuple[Path, ...] = tuple(
    sorted({*_PAGES, *_SHELL, *_COMPONENTS, *_COPY, *_TOKENS, *_MOCKUP})
)


class ScreenshotReviewError(Exception):
    pass


Refusal = ScreenshotReviewError


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    requirement: str
    scope_sha256: str
    approval_comment_id: int
    approval_sha256: str


def approval_bytes(requirement: str, scope_digest: str) -> bytes:
    return f"APPROVE SCREENSHOT REVIEW {requirement} sha256:{scope_digest}".encode()


def compute_scope_digest(project_root: Path) -> str:
    """The scope digest: one SHA-256 over every scoped file's exact path and
    content digest, in sorted order -- reproducible across machines and
    sensitive to any scoped file's content, addition, or removal."""
    hasher = hashlib.sha256()
    for relative in SCOPE:
        content = _regular_scoped_file(project_root, relative).read_bytes()
        hasher.update(relative.as_posix().encode())
        hasher.update(b"\0")
        hasher.update(hashlib.sha256(content).hexdigest().encode())
        hasher.update(b"\n")
    return hasher.hexdigest()


def _regular_scoped_file(project_root: Path, relative: Path) -> Path:
    target = project_root / relative
    if target.is_symlink() or not target.is_file():
        raise Refusal(
            f"{relative} is not a regular non-symlink file; the digest scope "
            "cannot be computed"
        )
    return target


def read_ledger(project_root: Path) -> tuple[LedgerEntry, ...]:
    location = project_root / LEDGER_LOCATION
    if location.is_symlink() or not location.is_file():
        raise Refusal(f"{LEDGER_LOCATION} is not a regular non-symlink file")
    try:
        parsed = tomllib.loads(location.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        raise Refusal(f"{LEDGER_LOCATION} is unreadable: {error}") from error
    if unknown := sorted(set(parsed) - {*ROOT_FIELDS, "approval"}):
        raise Refusal(f"{LEDGER_LOCATION} has unknown fields {unknown}")
    if parsed.get("schema_version") != 1:
        raise Refusal(
            f"{LEDGER_LOCATION} has unsupported schema {parsed.get('schema_version')!r}"
        )
    raw_entries = parsed.get("approval", [])
    if not isinstance(raw_entries, list) or not all(
        isinstance(item, dict) for item in raw_entries
    ):
        raise Refusal(f"{LEDGER_LOCATION} approval must be an array of tables")
    return tuple(_ledger_entry(item) for item in raw_entries)


def _ledger_entry(raw: dict[str, Any]) -> LedgerEntry:
    if unknown := sorted(set(raw) - ENTRY_FIELDS):
        raise Refusal(f"{LEDGER_LOCATION} approval entry has unknown fields {unknown}")
    if missing := sorted(ENTRY_FIELDS - set(raw)):
        raise Refusal(f"{LEDGER_LOCATION} approval entry lacks fields {missing}")
    requirement = raw.get("requirement")
    if (
        not isinstance(requirement, str)
        or REQUIREMENT_IDENTIFIER.match(requirement) is None
    ):
        raise Refusal(
            f"{LEDGER_LOCATION} approval entry has invalid requirement {requirement!r}"
        )
    scope_digest = raw.get("scope_sha256")
    if not isinstance(scope_digest, str) or DIGEST.match(scope_digest) is None:
        raise Refusal(
            f"{LEDGER_LOCATION} approval entry has invalid scope_sha256 {scope_digest!r}"
        )
    comment_id = raw.get("approval_comment_id")
    if (
        isinstance(comment_id, bool)
        or not isinstance(comment_id, int)
        or comment_id <= 0
    ):
        raise Refusal(
            f"{LEDGER_LOCATION} approval entry has invalid approval_comment_id {comment_id!r}"
        )
    approval_digest = raw.get("approval_sha256")
    if not isinstance(approval_digest, str) or DIGEST.match(approval_digest) is None:
        raise Refusal(
            f"{LEDGER_LOCATION} approval entry has invalid approval_sha256 {approval_digest!r}"
        )
    expected = hashlib.sha256(approval_bytes(requirement, scope_digest)).hexdigest()
    if approval_digest != expected:
        raise Refusal(
            f"{LEDGER_LOCATION} approval entry for {requirement} {scope_digest} has "
            f"approval digest {approval_digest}; expected {expected} for the exact "
            "approval comment"
        )
    return LedgerEntry(requirement, scope_digest, comment_id, approval_digest)


def refusal_instructions(scope_digest: str) -> str:
    comment = approval_bytes(GOVERNED_REQUIREMENT, scope_digest).decode()
    return (
        f"no ledger entry in {LEDGER_LOCATION} approves {GOVERNED_REQUIREMENT} at the "
        f"current scope digest sha256:{scope_digest}. Look at the uploaded screenshot "
        f"artifact, then post exactly this pull-request comment (no added text, no "
        f"added newline):\n"
        f"  {comment}\n"
        f"and add a matching [[approval]] entry to {LEDGER_LOCATION} with "
        f'requirement = "{GOVERNED_REQUIREMENT}", scope_sha256 = "{scope_digest}", '
        f"approval_comment_id set to that comment's id, and approval_sha256 set to "
        f"the SHA-256 of the exact comment line above -- before this merges."
    )


def main() -> int:
    project_root = Path.cwd()
    try:
        scope_digest = compute_scope_digest(project_root)
        entries = read_ledger(project_root)
    except ScreenshotReviewError as error:
        print(f"Screenshot review gate refused: {error}", file=sys.stderr)
        return 1
    matched = next(
        (
            entry
            for entry in entries
            if entry.requirement == GOVERNED_REQUIREMENT
            and entry.scope_sha256 == scope_digest
        ),
        None,
    )
    if matched is None:
        print(
            f"Screenshot review gate refused: {refusal_instructions(scope_digest)}",
            file=sys.stderr,
        )
        return 1
    print(
        f"Screenshot review gate: {GOVERNED_REQUIREMENT} approved at sha256:{scope_digest} "
        f"(comment {matched.approval_comment_id})",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
