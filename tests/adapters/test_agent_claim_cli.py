from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import pytest

from atelier2.adapters.agent_claim_cli import AgentClaimCli
from atelier2.contracts.effect_requests import HeadBranch
from atelier2.contracts.runs import RunId
from atelier2.ports.work_item_claims import (
    Abandoned,
    ClaimReceipt,
    ClaimRefusal,
    ClaimRefusalReason,
    ClaimState,
    ClaimTouch,
    Merged,
)
from tests.scenarios.work_item_claims import FakeWorkItemClaims

ITEM = 1299
RUN_ID = RunId("run-42")
BRANCH = HeadBranch("atelier2/work-item/claim-port")
SCOPE = (PurePosixPath("src/atelier2/ports/work_item_claims.py"),)


@dataclass
class RecordedProcess:
    outputs: list[bytes]
    errors: list[bytes] = field(default_factory=list)
    commands: list[tuple[str, ...]] = field(default_factory=list)

    def start(self, arguments: tuple[str, ...], **_options: object) -> object:
        self.commands.append(arguments)
        return object()

    def streams(
        self, _process: object, _timeout: float, _maximum: int
    ) -> tuple[int, bytes, bytes]:
        error = self.errors.pop(0) if self.errors else b""
        return 0, self.outputs.pop(0), error


def _claim_payload(
    *,
    touches: list[object] | None = None,
    agent: str = "atelier2 run run-42",
    role: str = "builder",
    scope: list[str] | None = None,
    resource: str | None = None,
    resource_value: int | None = None,
) -> bytes:
    return json.dumps(
        {
            "issue": ITEM,
            "lane": None,
            "claim_id": "claim-42",
            "url": "https://example.invalid/claims/42",
            "agent": agent,
            "role": role,
            "base": "a" * 40,
            "branch": BRANCH.value,
            "scope": ([path.as_posix() for path in SCOPE] if scope is None else scope),
            "resource": resource,
            "resource_value": resource_value,
            "versioned_files": 1,
            "versioned_files_total": 252,
            "share": 0.01,
            "touches": [] if touches is None else touches,
            "checks": [],
        }
    ).encode()


def _status_payload(
    *,
    branch: str = BRANCH.value,
    whole: str | None = None,
    resource: str | None = None,
    resource_value: int | None = None,
) -> bytes:
    claim = {
        "issue": ITEM,
        "lane": None,
        "agent": "atelier2 run run-42",
        "role": "builder",
        "base": "a" * 40,
        "branch": branch,
        "claim_id": "claim-42",
        "scope": [path.as_posix() for path in SCOPE],
        "resource": resource,
        "resource_value": resource_value,
        "overlaps": [],
        "state": "CLAIMED",
        "age": "0m",
        "old": False,
    }
    if whole is not None:
        claim["whole"] = whole
    return json.dumps(
        {
            "ledger": 1,
            "issue": None,
            "state": "CLAIMED",
            "claims": [claim],
            "unreadable": [],
        }
    ).encode()


def _release_payload(
    *,
    branch: str = BRANCH.value,
    agent: str = "atelier2 run run-42",
    role: str = "builder",
    reason: str = "merged #44",
) -> bytes:
    return json.dumps(
        {
            "issue": ITEM,
            "lane": None,
            "branch": branch,
            "claim_id": "claim-42",
            "agent": agent,
            "role": role,
            "reason": reason,
        }
    ).encode()


@pytest.fixture
def recorded_process(monkeypatch: pytest.MonkeyPatch) -> RecordedProcess:
    recorded = RecordedProcess([])
    monkeypatch.setattr(
        "atelier2.adapters.agent_claim_cli.subprocess.Popen", recorded.start
    )
    monkeypatch.setattr(
        "atelier2.adapters.agent_claim_cli.bounded_process_streams", recorded.streams
    )
    return recorded


def _adapter() -> AgentClaimCli:
    return AgentClaimCli(Path("agent-claim"), Path("/workspace"), RUN_ID, BRANCH)


def test_adapter_builds_the_pinned_argv_for_each_operation(
    recorded_process: RecordedProcess,
) -> None:
    recorded_process.outputs.extend(
        (_claim_payload(), _status_payload(), _release_payload())
    )
    adapter = _adapter()

    assert isinstance(adapter.claim(ITEM, RUN_ID, BRANCH, SCOPE, None), ClaimReceipt)
    assert adapter.status(BRANCH) is ClaimState.CLAIMED
    assert adapter.release(ITEM, "claim-42", Merged(44)) is None

    assert recorded_process.commands == [
        (
            "agent-claim",
            "claim",
            "1299",
            "--agent",
            "atelier2 run run-42",
            "--role",
            "builder",
            "--branch",
            BRANCH.value,
            "--scope",
            SCOPE[0].as_posix(),
            "--json",
        ),
        ("agent-claim", "status", "--json"),
        (
            "agent-claim",
            "release",
            "1299",
            "--agent",
            "atelier2 run run-42",
            "--claim-id",
            "claim-42",
            "--merged",
            "44",
            "--json",
        ),
    ]


def test_adapter_forwards_the_out_of_order_reason_to_argv(
    recorded_process: RecordedProcess,
) -> None:
    recorded_process.outputs.append(_claim_payload())

    _adapter().claim(ITEM, RUN_ID, BRANCH, SCOPE, "fixing a live outage first")

    assert recorded_process.commands[0][-3:-1] == (
        "--out-of-order",
        "fixing a live outage first",
    )


@pytest.mark.parametrize(
    "payload",
    (
        lambda: _claim_payload().replace(b'"claim_id"', b'"receipt_id"'),
        lambda: _claim_payload()[:-1] + b', "new_field": true}',
        lambda: b"not json",
    ),
)
def test_adapter_refuses_claim_output_outside_the_pinned_json_contract(
    recorded_process: RecordedProcess, payload: Callable[[], bytes]
) -> None:
    recorded_process.outputs.append(payload())

    assert _adapter().claim(ITEM, RUN_ID, BRANCH, SCOPE, None) == ClaimRefusal(
        ClaimRefusalReason.UNKNOWN
    )


@pytest.mark.parametrize(
    "payload",
    (
        lambda: _status_payload().replace(b'"old"', b'"new"'),
        lambda: _status_payload()[:-1] + b', "new_field": true}',
        lambda: b"not json",
    ),
)
def test_adapter_refuses_status_output_outside_the_pinned_json_contract(
    recorded_process: RecordedProcess, payload: Callable[[], bytes]
) -> None:
    recorded_process.outputs.append(payload())

    assert _adapter().status(BRANCH) is ClaimState.UNKNOWN


@pytest.mark.parametrize(
    "payload",
    (
        lambda: _status_payload(branch="atelier2/work-item/other").replace(
            b'"old"', b'"new"'
        ),
        lambda: _status_payload(branch="atelier2/work-item/other").replace(
            b'"state": "CLAIMED", "age": "0m"',
            b'"state": "INVALID", "age": "0m"',
        ),
    ),
)
def test_adapter_refuses_a_malformed_unrelated_status_claim(
    recorded_process: RecordedProcess, payload: Callable[[], bytes]
) -> None:
    recorded_process.outputs.append(payload())

    assert _adapter().status(BRANCH) is ClaimState.UNKNOWN


@pytest.mark.parametrize(
    "payload",
    (
        lambda: _release_payload().replace(b'"reason"', b'"outcome"'),
        lambda: _release_payload()[:-1] + b', "new_field": true}',
        lambda: b"not json",
    ),
)
def test_adapter_refuses_release_output_outside_the_pinned_json_contract(
    recorded_process: RecordedProcess, payload: Callable[[], bytes]
) -> None:
    recorded_process.outputs.append(payload())

    assert _adapter().release(ITEM, "claim-42", Merged(44)) == ClaimRefusal(
        ClaimRefusalReason.UNKNOWN
    )


def test_adapter_maps_a_priority_refusal(recorded_process: RecordedProcess) -> None:
    # Pinned verbatim against agent-claim 0.12.0's `_out_of_order_check`
    # (cli.py:1042-1058): the structured check id is "out-of-order", not a
    # word this adapter would have to find in prose.
    recorded_process.outputs.append(
        json.dumps(
            {
                "refused": True,
                "issue": ITEM,
                "checks": [
                    {
                        "level": "error",
                        "check": "out-of-order",
                        "text": (
                            "higher-priority actionable item #42 (score 7) is "
                            "free: fix the flake; use --out-of-order REASON to proceed"
                        ),
                        "slice": None,
                        "issue": 42,
                    }
                ],
            }
        ).encode()
    )

    assert _adapter().claim(ITEM, RUN_ID, BRANCH, SCOPE, None) == ClaimRefusal(
        ClaimRefusalReason.PRIORITY
    )


def test_adapter_maps_a_ledger_unreadable_diagnostic_refusal(
    recorded_process: RecordedProcess,
) -> None:
    # agent-claim 0.12.0 never reaches its `--json` refusal payload for an
    # unreadable ledger: `_reject_unreadable_claims` (protocol.py:1191-1200)
    # raises before one exists, and the top-level `ClaimError` handler
    # (cli.py:1990-1992) prints this documented sentence to stderr instead,
    # with empty stdout.
    recorded_process.outputs.append(b"")
    recorded_process.errors.append(
        b"ERROR: claim refused: claim 'claim-1' at "
        b"https://example.invalid/claims/1 is unreadable (unknown fields: "
        b"extra); upgrade the installed tool before claiming a scope that "
        b"could overlap it\n"
    )

    assert _adapter().claim(ITEM, RUN_ID, BRANCH, SCOPE, None) == ClaimRefusal(
        ClaimRefusalReason.LEDGER_UNREADABLE
    )


def test_adapter_accepts_the_optional_wide_claim_reason_in_status(
    recorded_process: RecordedProcess,
) -> None:
    recorded_process.outputs.append(_status_payload(whole="the run owns all source"))

    assert _adapter().status(BRANCH) is ClaimState.CLAIMED


def test_adapter_accepts_an_allocated_resource_value(
    recorded_process: RecordedProcess,
) -> None:
    recorded_process.outputs.extend(
        (
            _claim_payload(resource="claim-number", resource_value=3),
            _status_payload(resource="claim-number", resource_value=3),
        )
    )

    assert isinstance(_adapter().claim(ITEM, RUN_ID, BRANCH, SCOPE, None), ClaimReceipt)
    assert _adapter().status(BRANCH) is ClaimState.CLAIMED


@pytest.mark.parametrize(
    ("resource", "resource_value"),
    (("claim-number", None), (None, 3), ("claim-number", 0)),
)
def test_adapter_refuses_infeasible_resource_pairs(
    recorded_process: RecordedProcess, resource: str | None, resource_value: int | None
) -> None:
    recorded_process.outputs.extend(
        (
            _claim_payload(resource=resource, resource_value=resource_value),
            _status_payload(resource=resource, resource_value=resource_value),
        )
    )

    assert _adapter().claim(ITEM, RUN_ID, BRANCH, SCOPE, None) == ClaimRefusal(
        ClaimRefusalReason.UNKNOWN
    )
    assert _adapter().status(BRANCH) is ClaimState.UNKNOWN


@pytest.mark.parametrize(
    "payload",
    (
        lambda: _claim_payload(agent="atelier2 run other"),
        lambda: _claim_payload(role="reviewer"),
        lambda: _claim_payload(scope=["src/other.py"]),
    ),
)
def test_adapter_refuses_claim_receipts_that_do_not_match_the_request(
    recorded_process: RecordedProcess, payload: Callable[[], bytes]
) -> None:
    recorded_process.outputs.append(payload())

    assert _adapter().claim(ITEM, RUN_ID, BRANCH, SCOPE, None) == ClaimRefusal(
        ClaimRefusalReason.UNKNOWN
    )


@pytest.mark.parametrize(
    "payload",
    (
        lambda: _release_payload(branch="atelier2/work-item/other"),
        lambda: _release_payload(agent="atelier2 run other"),
        lambda: _release_payload(role="reviewer"),
    ),
)
def test_adapter_refuses_release_receipts_that_do_not_match_the_request(
    recorded_process: RecordedProcess, payload: Callable[[], bytes]
) -> None:
    recorded_process.outputs.append(payload())

    assert _adapter().release(ITEM, "claim-42", Merged(44)) == ClaimRefusal(
        ClaimRefusalReason.UNKNOWN
    )


def test_adapter_returns_unknown_refusal_when_the_bounded_process_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def timeout(
        _process: object, _timeout: float, _maximum: int
    ) -> tuple[int, bytes, bytes]:
        raise OSError("process did not answer in time")

    monkeypatch.setattr(
        "atelier2.adapters.agent_claim_cli.subprocess.Popen",
        lambda *_arguments, **_options: object(),
    )
    monkeypatch.setattr(
        "atelier2.adapters.agent_claim_cli.bounded_process_streams", timeout
    )

    assert _adapter().claim(ITEM, RUN_ID, BRANCH, SCOPE, None) == ClaimRefusal(
        ClaimRefusalReason.UNKNOWN
    )


def test_fake_and_adapter_meet_the_same_port_expectations(
    recorded_process: RecordedProcess,
) -> None:
    touch = ClaimTouch(77, "other-claim", "other agent", SCOPE)
    expected = ClaimReceipt(ITEM, "claim-42", BRANCH, (touch,))
    fake = FakeWorkItemClaims(claim_answer=expected, status_answer=ClaimState.CLAIMED)
    recorded_process.outputs.extend(
        (
            _claim_payload(
                touches=[
                    {
                        "issue": 77,
                        "lane": None,
                        "claim_id": "other-claim",
                        "agent": "other agent",
                        "scope": [path.as_posix() for path in SCOPE],
                    }
                ]
            ),
            _status_payload(),
            _release_payload(),
            _release_payload(reason="abandoned: no longer needed"),
        )
    )

    assert fake.claim(ITEM, RUN_ID, BRANCH, SCOPE, None) == expected
    receipt = _adapter().claim(ITEM, RUN_ID, BRANCH, SCOPE, None)

    assert receipt == expected
    assert not isinstance(receipt, ClaimRefusal)
    assert fake.status(BRANCH) is _adapter().status(BRANCH)
    assert fake.release(ITEM, "claim-42", Merged(44)) is _adapter().release(
        ITEM, "claim-42", Merged(44)
    )
    assert fake.release(
        ITEM, "claim-42", Abandoned("no longer needed")
    ) is _adapter().release(ITEM, "claim-42", Abandoned("no longer needed"))


def test_adapter_builds_an_abandon_release(recorded_process: RecordedProcess) -> None:
    recorded_process.outputs.append(
        _release_payload(reason="abandoned: no longer needed")
    )

    assert _adapter().release(ITEM, "claim-42", Abandoned("no longer needed")) is None
    assert recorded_process.commands == [
        (
            "agent-claim",
            "release",
            "1299",
            "--agent",
            "atelier2 run run-42",
            "--claim-id",
            "claim-42",
            "--abandoned",
            "no longer needed",
            "--json",
        )
    ]
