"""Recorded answers for application scenarios that need the claim-ledger port."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from atelier2.contracts.effect_requests import ClaimReasons, HeadBranch
from atelier2.contracts.runs import RunId
from atelier2.ports.work_item_claims import (
    ClaimAbsent,
    ClaimReadback,
    ClaimReceipt,
    ClaimRefusal,
    ClaimReleaseOutcome,
)


@dataclass
class ClaimRequest:
    """One claim its caller asked the ledger for, exactly as it asked."""

    item: int
    agent: RunId
    branch: HeadBranch
    scope: tuple[PurePosixPath, ...]
    claim_id: str
    reasons: ClaimReasons
    checkout: Path


@dataclass
class FakeWorkItemClaims:
    """Returns arranged port answers and records each request its caller made."""

    claim_answer: ClaimReceipt | ClaimRefusal | None = None
    read_back_answer: ClaimReadback = field(default_factory=ClaimAbsent)
    release_answer: ClaimRefusal | None = None
    claim_requests: list[ClaimRequest] = field(default_factory=list)
    read_back_requests: list[tuple[int, str, Path]] = field(default_factory=list)
    release_requests: list[tuple[int, RunId, str, ClaimReleaseOutcome]] = field(
        default_factory=list
    )

    def claim(
        self,
        item: int,
        agent: RunId,
        branch: HeadBranch,
        scope: tuple[PurePosixPath, ...],
        claim_id: str,
        reasons: ClaimReasons,
        checkout: Path,
    ) -> ClaimReceipt | ClaimRefusal:
        self.claim_requests.append(
            ClaimRequest(item, agent, branch, scope, claim_id, reasons, checkout)
        )
        if self.claim_answer is None:
            raise AssertionError("this scenario did not arrange a claim answer")
        return self.claim_answer

    def read_back(self, item: int, claim_id: str, checkout: Path) -> ClaimReadback:
        self.read_back_requests.append((item, claim_id, checkout))
        return self.read_back_answer

    def release(
        self,
        item: int,
        agent: RunId,
        claim_id: str,
        outcome: ClaimReleaseOutcome,
    ) -> ClaimRefusal | None:
        self.release_requests.append((item, agent, claim_id, outcome))
        return self.release_answer


_LEDGER_STUB = '''
"""A pinned `agent-claim` stand-in: one JSON ledger file, no network."""

import json
import os
import subprocess
import sys
from pathlib import Path

LEDGER = Path(__file__).with_name("claim-ledger.json")
ANSWER = Path(__file__).with_name("claim-answer")
INVOCATIONS = Path(__file__).with_name("claim-invocations")
GIT_ENVIRONMENT = {
    **os.environ,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "LC_ALL": "C",
}


class CheckoutRefused(Exception):
    pass


def _git(*arguments: str) -> str:
    completed = subprocess.run(
        ("git", *arguments), capture_output=True, text=True, env=GIT_ENVIRONMENT
    )
    if completed.returncode != 0:
        raise CheckoutRefused(completed.stderr.strip() or "git failed")
    return completed.stdout.strip()


def _checkout_refusal(branch: str) -> str | None:
    """agent-coordination's checkout checks against the working directory."""

    try:
        _git("rev-parse", "HEAD")
        current = _git("branch", "--show-current")
        if current != branch:
            return f"claim branch {branch!r} does not match checkout branch {current!r}"
        git_directory = Path(_git("rev-parse", "--git-dir")).resolve()
        common_directory = Path(_git("rev-parse", "--git-common-dir")).resolve()
        if git_directory == common_directory:
            return "build claims require a linked isolated worktree checkout"
        if _git("status", "--porcelain"):
            return "claim must be acquired before the first worktree edit"
        if not _git("ls-files"):
            return "the checkout holds no tracked file"
    except CheckoutRefused as refused:
        return str(refused)
    return None


def _scripted() -> str:
    return ANSWER.read_text().strip() if ANSWER.is_file() else "grant"


def _claims() -> list[dict[str, object]]:
    return json.loads(LEDGER.read_text()) if LEDGER.is_file() else []


def _option(arguments: list[str], name: str) -> str:
    return arguments[arguments.index(name) + 1]


def _optional(arguments: list[str], name: str) -> str | None:
    return _option(arguments, name) if name in arguments else None


def _claim(arguments: list[str]) -> int:
    scripted = _scripted()
    if scripted == "checkout-refused":
        branch = _option(arguments, "--branch")
        print(
            f"ERROR: claim branch '{branch}' does not match checkout branch 'main'",
            file=sys.stderr,
        )
        return 2
    if scripted == "checkout":
        refusal = _checkout_refusal(_option(arguments, "--branch"))
        if refusal is not None:
            print(f"ERROR: {refusal}", file=sys.stderr)
            return 2
    if scripted == "priority":
        json.dump(
            {
                "refused": True,
                "issue": int(arguments[1]),
                "checks": [
                    {
                        "level": "error",
                        "check": "out-of-order",
                        "text": "a higher-priority item is free",
                        "slice": None,
                        "issue": 1,
                    }
                ],
            },
            sys.stdout,
        )
        return 2
    if scripted == "unknown":
        json.dump({"refused": True, "issue": int(arguments[1]), "checks": []}, sys.stdout)
        return 2
    scope = [
        arguments[index + 1]
        for index, value in enumerate(arguments)
        if value == "--scope"
    ]
    claim = {
        "issue": int(arguments[1]),
        "lane": None,
        "claim_id": _option(arguments, "--claim-id"),
        "url": "https://example.invalid/claims/1",
        "agent": _option(arguments, "--agent"),
        "role": _option(arguments, "--role"),
        "base": "0" * 40,
        "branch": _option(arguments, "--branch"),
        "scope": scope,
        "resource": None,
        "resource_value": None,
        "versioned_files": len(scope),
        "versioned_files_total": 100,
        "share": 0.01,
        "touches": (
            [
                {
                    "issue": 77,
                    "lane": None,
                    "claim_id": "another-lane",
                    "agent": "atelier2 run another",
                    "scope": scope,
                }
            ]
            if scripted == "touches"
            else []
        ),
        "checks": [],
    }
    standing = _claims()
    if any(held["claim_id"] == claim["claim_id"] for held in standing):
        json.dump({"refused": True, "issue": claim["issue"], "checks": []}, sys.stdout)
        return 2
    reasons = {
        "whole": _option(arguments, "--whole"),
        "out_of_order": _optional(arguments, "--out-of-order"),
    }
    standing.append({**claim, "reasons": reasons, "checkout": os.getcwd()})
    LEDGER.write_text(json.dumps(standing))
    json.dump(claim, sys.stdout)
    return 0


def _status() -> int:
    json.dump(
        {
            "ledger": 1,
            "issue": None,
            "state": "CLAIMED" if _claims() else "UNCLAIMED",
            "claims": [
                {
                    "issue": claim["issue"],
                    "lane": None,
                    "agent": claim["agent"],
                    "role": claim["role"],
                    "base": claim["base"],
                    "branch": claim["branch"],
                    "claim_id": claim["claim_id"],
                    "scope": claim["scope"],
                    "resource": None,
                    "resource_value": None,
                    "overlaps": [],
                    "state": "CLAIMED",
                    "age": "0m",
                    "old": False,
                }
                for claim in _claims()
            ],
            "unreadable": [],
        },
        sys.stdout,
    )
    return 0


def main(arguments: list[str]) -> int:
    with INVOCATIONS.open("a") as log:
        log.write(f"{arguments[0]}\\n")
    if arguments[0] == "claim":
        return _claim(arguments)
    if arguments[0] == "status":
        return _status()
    released = [
        claim
        for claim in _claims()
        if claim["claim_id"] != _option(arguments, "--claim-id")
    ]
    LEDGER.write_text(json.dumps(released))
    return 0


sys.exit(main(sys.argv[1:]))
'''


def fake_agent_claim_executable(root: Path, answer: str = "grant") -> Path:
    """A claim command a run can really invoke, holding its ledger in one file.

    The stub answers `agent-claim` 0.12.0's pinned JSON for the commands a run
    uses, and it remembers: a claim already posted is refused a second time and
    read back by `status`, exactly as the real ledger behaves, so a scenario
    proves the retry path rather than assuming it. It also keeps the `--whole`
    and `--out-of-order` reasons each claim arrived with, which the real tool
    records on the ledger comment, so `claimed_ledger` answers them.

    `answer` scripts what the ledger says to a claim: `grant` posts it,
    `priority` refuses it with the tool's own out-of-order check, `unknown`
    refuses it without one, `checkout-refused` refuses it before any JSON
    exists with one `ERROR:` line on standard error, as the tool does for a
    checkout that fails its preconditions, `checkout` runs the tool's own
    checks against its working directory -- a clean linked worktree on the
    lane branch with a tracked file -- and grants only what passes them, and
    `touches` grants it while naming a foreign lane on the same paths.
    """

    executable = root / "agent-claim"
    executable.write_text(f"#!{sys.executable}\n{_LEDGER_STUB}")
    executable.chmod(0o755)
    (root / "claim-answer").write_text(answer)
    return executable


def ledger_invocations(executable: Path) -> tuple[str, ...]:
    """Every command the stub ledger was asked, in order."""

    log = executable.with_name("claim-invocations")
    return tuple(log.read_text().split()) if log.is_file() else ()


def claimed_ledger(executable: Path) -> tuple[ClaimRequest, ...]:
    """Every claim the stub ledger holds, as the requests that created them."""

    ledger = executable.with_name("claim-ledger.json")
    if not ledger.is_file():
        return ()
    return tuple(
        ClaimRequest(
            int(claim["issue"]),
            RunId(str(claim["agent"]).removeprefix("atelier2 run ")),
            HeadBranch(str(claim["branch"])),
            tuple(PurePosixPath(path) for path in claim["scope"]),
            str(claim["claim_id"]),
            ClaimReasons(
                str(claim["reasons"]["whole"]), claim["reasons"]["out_of_order"]
            ),
            Path(str(claim["checkout"])),
        )
        for claim in json.loads(ledger.read_text())
    )
