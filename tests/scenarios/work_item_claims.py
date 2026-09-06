"""Recorded answers for application scenarios that need the claim-ledger port."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from atelier2.contracts.effect_requests import HeadBranch
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
    out_of_order_reason: str | None


@dataclass
class FakeWorkItemClaims:
    """Returns arranged port answers and records each request its caller made."""

    claim_answer: ClaimReceipt | ClaimRefusal | None = None
    read_back_answer: ClaimReadback = field(default_factory=ClaimAbsent)
    release_answer: ClaimRefusal | None = None
    claim_requests: list[ClaimRequest] = field(default_factory=list)
    read_back_requests: list[tuple[int, str]] = field(default_factory=list)
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
        out_of_order_reason: str | None,
    ) -> ClaimReceipt | ClaimRefusal:
        self.claim_requests.append(
            ClaimRequest(item, agent, branch, scope, claim_id, out_of_order_reason)
        )
        if self.claim_answer is None:
            raise AssertionError("this scenario did not arrange a claim answer")
        return self.claim_answer

    def read_back(self, item: int, claim_id: str) -> ClaimReadback:
        self.read_back_requests.append((item, claim_id))
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
import sys
from pathlib import Path

LEDGER = Path(__file__).with_name("claim-ledger.json")


def _claims() -> list[dict[str, object]]:
    return json.loads(LEDGER.read_text()) if LEDGER.is_file() else []


def _option(arguments: list[str], name: str) -> str:
    return arguments[arguments.index(name) + 1]


def _claim(arguments: list[str]) -> int:
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
        "touches": [],
        "checks": [],
    }
    standing = _claims()
    if any(held["claim_id"] == claim["claim_id"] for held in standing):
        json.dump({"refused": True, "issue": claim["issue"], "checks": []}, sys.stdout)
        return 2
    standing.append(claim)
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


def fake_agent_claim_executable(root: Path) -> Path:
    """A claim command a run can really invoke, holding its ledger in one file.

    The stub answers `agent-claim` 0.12.0's pinned JSON for the commands a run
    uses, and it remembers: a claim already posted is refused a second time and
    read back by `status`, exactly as the real ledger behaves, so a scenario
    proves the retry path rather than assuming it.
    """

    executable = root / "agent-claim"
    executable.write_text(f"#!{sys.executable}\n{_LEDGER_STUB}")
    executable.chmod(0o755)
    return executable


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
            None,
        )
        for claim in json.loads(ledger.read_text())
    )
