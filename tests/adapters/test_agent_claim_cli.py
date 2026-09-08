from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import pytest

from atelier2.adapters.agent_claim_cli import AgentClaimCli
from atelier2.contracts.effect_requests import ClaimReasons, HeadBranch
from atelier2.contracts.runs import RunId
from atelier2.contracts.secret_redaction import REDACTION_MARKER
from atelier2.ports.work_item_claims import (
    MAXIMUM_CLAIM_REFUSAL_DETAIL_BYTES,
    Abandoned,
    ClaimAbsent,
    ClaimReceipt,
    ClaimRefusal,
    ClaimRefusalReason,
    ClaimTouch,
    Merged,
)
from tests.scenarios.work_item_claims import FakeWorkItemClaims

ITEM = 1299
RUN_ID = RunId("run-42")
CLAIM_ID = "claim-42"
AGENT = "atelier2 run run-42"
BRANCH = HeadBranch("atelier2/work-item/claim-port")
SCOPE = (PurePosixPath("src/atelier2/ports/work_item_claims.py"),)
OTHER_SCOPE = (PurePosixPath("src/atelier2/adapters/agent_claim_cli.py"),)
WHOLE = "the scope is the item body's own cut"
OUT_OF_ORDER = "admitted by the operator through the queue label `bereit`"
REASONS = ClaimReasons(WHOLE, None)
CHECKOUT = Path("/claim-checkout")
PROJECT_CHECKOUT = Path("/workspace")


@dataclass
class RecordedProcess:
    outputs: list[bytes]
    errors: list[bytes] = field(default_factory=list)
    return_codes: list[int] = field(default_factory=list)
    commands: list[tuple[str, ...]] = field(default_factory=list)
    working_directories: list[object] = field(default_factory=list)

    def start(self, arguments: tuple[str, ...], **options: object) -> object:
        self.commands.append(arguments)
        self.working_directories.append(options["cwd"])
        return object()

    def streams(
        self, _process: object, _timeout: float, _maximum: int
    ) -> tuple[int, bytes, bytes]:
        error = self.errors.pop(0) if self.errors else b""
        return_code = self.return_codes.pop(0) if self.return_codes else 0
        return return_code, self.outputs.pop(0), error


def _claim_payload(
    *,
    touches: list[object] | None = None,
    agent: str = AGENT,
    claim_id: str = CLAIM_ID,
    role: str = "builder",
    scope: list[str] | None = None,
    resource: str | None = None,
    resource_value: int | None = None,
) -> bytes:
    return json.dumps(
        {
            "issue": ITEM,
            "lane": None,
            "claim_id": claim_id,
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


def _standing_claim(
    *,
    claim_id: str = CLAIM_ID,
    item: int = ITEM,
    agent: str = AGENT,
    branch: str = BRANCH.value,
    scope: tuple[PurePosixPath, ...] = SCOPE,
    overlaps: list[object] | None = None,
    whole: str | None = None,
    resource: str | None = None,
    resource_value: int | None = None,
) -> dict[str, object]:
    return {
        "issue": item,
        "lane": None,
        "agent": agent,
        "role": "builder",
        "base": "a" * 40,
        "branch": branch,
        "claim_id": claim_id,
        "scope": [path.as_posix() for path in scope],
        "resource": resource,
        "resource_value": resource_value,
        "whole": whole,
        "overlaps": [] if overlaps is None else overlaps,
        "state": "CLAIMED",
        "age": "0m",
        "old": False,
    }


def _status_payload(
    *,
    claims: list[object] | None = None,
    claim_id: str = CLAIM_ID,
    whole: str | None = None,
    resource: str | None = None,
    resource_value: int | None = None,
) -> bytes:
    standing = (
        [
            _standing_claim(
                claim_id=claim_id,
                whole=whole,
                resource=resource,
                resource_value=resource_value,
            )
        ]
        if claims is None
        else claims
    )
    return json.dumps(
        {
            "issue": None,
            "state": "CLAIMED",
            "claims": standing,
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


JSON_ERROR_SENTENCE = (
    "the claim state ref does not exist yet; run bootstrap before claim, "
    "rescope, or release"
)


def _ok_false_payload(sentence: str = JSON_ERROR_SENTENCE) -> bytes:
    return json.dumps({"ok": False, "error": sentence}).encode()


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
    return AgentClaimCli(Path("aco"), PROJECT_CHECKOUT)


@pytest.mark.parametrize(
    ("reasons", "reason_flags"),
    (
        (ClaimReasons(WHOLE, None), ("--whole", WHOLE)),
        (
            ClaimReasons(WHOLE, OUT_OF_ORDER),
            ("--whole", WHOLE, "--out-of-order", OUT_OF_ORDER),
        ),
    ),
    ids=("whole-only", "whole-and-out-of-order"),
)
def test_adapter_builds_the_pinned_argv_for_each_operation(
    recorded_process: RecordedProcess,
    reasons: ClaimReasons,
    reason_flags: tuple[str, ...],
) -> None:
    """`--whole` always travels; `--out-of-order` exactly when a reason exists."""

    recorded_process.outputs.extend(
        (_claim_payload(), _status_payload(), _release_payload())
    )
    adapter = _adapter()

    assert isinstance(
        adapter.claim(ITEM, RUN_ID, BRANCH, SCOPE, CLAIM_ID, reasons, CHECKOUT),
        ClaimReceipt,
    )
    assert isinstance(adapter.read_back(ITEM, CLAIM_ID, CHECKOUT), ClaimReceipt)
    assert adapter.release(ITEM, RUN_ID, CLAIM_ID, Merged(44)) is None

    assert recorded_process.working_directories == [
        CHECKOUT,
        CHECKOUT,
        PROJECT_CHECKOUT,
    ]
    assert recorded_process.commands == [
        (
            "aco",
            "claim",
            "1299",
            "--agent",
            "atelier2 run run-42",
            "--role",
            "builder",
            "--branch",
            BRANCH.value,
            "--claim-id",
            CLAIM_ID,
            "--scope",
            SCOPE[0].as_posix(),
            *reason_flags,
            "--json",
        ),
        ("aco", "status", "--json"),
        (
            "aco",
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


PINNED_CONTRACT_VIOLATION = "aco returned fields outside its pinned contract"


@pytest.mark.parametrize(
    ("payload", "detail"),
    (
        (
            lambda: _claim_payload().replace(b'"claim_id"', b'"receipt_id"'),
            PINNED_CONTRACT_VIOLATION,
        ),
        (
            lambda: _claim_payload()[:-1] + b', "new_field": true}',
            PINNED_CONTRACT_VIOLATION,
        ),
        (lambda: b"not json", ""),
    ),
)
def test_adapter_refuses_claim_output_outside_the_pinned_json_contract(
    recorded_process: RecordedProcess, payload: Callable[[], bytes], detail: str
) -> None:
    """A contract violation is refused, and the refusal says which one."""

    recorded_process.outputs.append(payload())

    assert _adapter().claim(
        ITEM, RUN_ID, BRANCH, SCOPE, CLAIM_ID, REASONS, CHECKOUT
    ) == ClaimRefusal(ClaimRefusalReason.UNKNOWN, detail)


@pytest.mark.parametrize(
    "payload",
    (
        lambda: _status_payload().replace(b'"old"', b'"new"'),
        lambda: _status_payload()[:-1] + b', "new_field": true}',
        lambda: b"not json",
    ),
)
def test_adapter_refuses_read_back_output_outside_the_pinned_json_contract(
    recorded_process: RecordedProcess, payload: Callable[[], bytes]
) -> None:
    recorded_process.outputs.append(payload())

    refused = _adapter().read_back(ITEM, CLAIM_ID, CHECKOUT)

    assert isinstance(refused, ClaimRefusal)
    assert refused.reason is ClaimRefusalReason.UNKNOWN


@pytest.mark.parametrize(
    "payload",
    (
        lambda: _status_payload(claim_id="another-claim").replace(b'"old"', b'"new"'),
        lambda: _status_payload(claim_id="another-claim").replace(
            b'"issue": 1299', b'"issue": "1299"'
        ),
    ),
)
def test_adapter_refuses_a_malformed_foreign_claim_it_reads_past(
    recorded_process: RecordedProcess, payload: Callable[[], bytes]
) -> None:
    """A ledger this reader cannot read at all answers nothing about our claim."""

    recorded_process.outputs.append(payload())

    refused = _adapter().read_back(ITEM, CLAIM_ID, CHECKOUT)

    assert isinstance(refused, ClaimRefusal)
    assert refused.reason is ClaimRefusalReason.UNKNOWN


def test_adapter_reads_back_the_claim_this_run_already_holds(
    recorded_process: RecordedProcess,
) -> None:
    """A retry finds its own claim by id, with the scope the ledger recorded."""

    recorded_process.outputs.append(
        _status_payload(
            claims=[
                _standing_claim(
                    overlaps=[
                        {
                            "issue": 77,
                            "lane": None,
                            "claim_id": "other-claim",
                            "agent": "other agent",
                        }
                    ]
                ),
                _standing_claim(
                    claim_id="other-claim",
                    item=77,
                    agent="other agent",
                    branch="other/lane",
                    scope=OTHER_SCOPE,
                ),
            ]
        )
    )

    assert _adapter().read_back(ITEM, CLAIM_ID, CHECKOUT) == ClaimReceipt(
        ITEM,
        CLAIM_ID,
        AGENT,
        BRANCH,
        SCOPE,
        (ClaimTouch(77, "other-claim", "other agent", OTHER_SCOPE),),
    )


def test_adapter_reads_back_nothing_when_the_ledger_holds_no_such_claim(
    recorded_process: RecordedProcess,
) -> None:
    recorded_process.outputs.append(_status_payload(claim_id="another-claim"))

    assert _adapter().read_back(ITEM, CLAIM_ID, CHECKOUT) == ClaimAbsent()


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

    refused = _adapter().release(ITEM, RUN_ID, CLAIM_ID, Merged(44))

    assert refused is not None
    assert refused.reason is ClaimRefusalReason.UNKNOWN


def test_adapter_maps_a_priority_refusal(recorded_process: RecordedProcess) -> None:
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

    assert _adapter().claim(
        ITEM, RUN_ID, BRANCH, SCOPE, CLAIM_ID, REASONS, CHECKOUT
    ) == ClaimRefusal(
        ClaimRefusalReason.PRIORITY,
        "higher-priority actionable item #42 (score 7) is free: fix the flake; "
        "use --out-of-order REASON to proceed",
    )


@pytest.mark.parametrize(
    "call",
    (
        lambda adapter: adapter.claim(
            ITEM, RUN_ID, BRANCH, SCOPE, CLAIM_ID, REASONS, CHECKOUT
        ),
        lambda adapter: adapter.read_back(ITEM, CLAIM_ID, CHECKOUT),
        lambda adapter: adapter.release(ITEM, RUN_ID, CLAIM_ID, Merged(44)),
    ),
    ids=("claim", "status", "release"),
)
def test_a_nonzero_json_refusal_carries_the_error_sentence(
    recorded_process: RecordedProcess,
    call: Callable[[AgentClaimCli], object],
) -> None:
    """A `--json` command that exits non-zero with ok-false is that sentence,
    not a contract violation of a success payload."""

    recorded_process.outputs.append(_ok_false_payload())
    recorded_process.return_codes.append(2)

    assert call(_adapter()) == ClaimRefusal(
        ClaimRefusalReason.UNKNOWN, JSON_ERROR_SENTENCE
    )


def test_a_json_refusal_detail_is_scrubbed_and_bounded(
    recorded_process: RecordedProcess,
) -> None:
    token = "ghp_" + "a" * 36
    recorded_process.outputs.extend(
        (
            _ok_false_payload(f"origin refused the token {token}"),
            _ok_false_payload("x" * (2 * MAXIMUM_CLAIM_REFUSAL_DETAIL_BYTES)),
        )
    )
    recorded_process.return_codes.extend((2, 2))

    scrubbed = _adapter().claim(
        ITEM, RUN_ID, BRANCH, SCOPE, CLAIM_ID, REASONS, CHECKOUT
    )
    cut = _adapter().claim(ITEM, RUN_ID, BRANCH, SCOPE, CLAIM_ID, REASONS, CHECKOUT)

    assert isinstance(scrubbed, ClaimRefusal)
    assert scrubbed.detail == f"origin refused the token {REDACTION_MARKER}"
    assert isinstance(cut, ClaimRefusal)
    assert cut.detail == "x" * MAXIMUM_CLAIM_REFUSAL_DETAIL_BYTES


def test_a_nonzero_exit_without_json_keeps_the_stderr_sentence(
    recorded_process: RecordedProcess,
) -> None:
    recorded_process.outputs.append(b"")
    recorded_process.return_codes.append(2)
    recorded_process.errors.append(
        b"ERROR: claim branch 'x' does not match checkout branch 'main'\n"
    )

    assert _adapter().claim(
        ITEM, RUN_ID, BRANCH, SCOPE, CLAIM_ID, REASONS, CHECKOUT
    ) == ClaimRefusal(
        ClaimRefusalReason.UNKNOWN,
        "claim branch 'x' does not match checkout branch 'main'",
    )


def test_a_checkout_precondition_the_tool_refuses_names_its_sentence(
    recorded_process: RecordedProcess,
) -> None:
    """The one line the tool printed is the refusal's detail; nothing else is.

    aco checks the checkout before it reads any board, and every check it
    fails ends as one `ERROR:` sentence on standard error with no JSON at
    all. The progress lines it printed before are not the reason.
    """

    recorded_process.outputs.append(b"")
    recorded_process.errors.append(
        b"checking checkout /workspace\n"
        b"ERROR: claim branch 'x' does not match checkout branch 'main'\n"
    )

    assert _adapter().claim(
        ITEM, RUN_ID, BRANCH, SCOPE, CLAIM_ID, REASONS, CHECKOUT
    ) == ClaimRefusal(
        ClaimRefusalReason.UNKNOWN,
        "claim branch 'x' does not match checkout branch 'main'",
    )


def test_a_refusal_detail_carries_no_credential_and_stays_bounded(
    recorded_process: RecordedProcess,
) -> None:
    """A token the tool echoed never reaches durable state, and a sentence
    longer than a refusal keeps is cut after it was scrubbed."""

    token = "ghp_" + "a" * 36
    recorded_process.outputs.extend((b"", b""))
    recorded_process.errors.extend(
        (
            f"ERROR: origin refused the token {token}\n".encode(),
            b"ERROR: " + b"x" * (2 * MAXIMUM_CLAIM_REFUSAL_DETAIL_BYTES) + b"\n",
        )
    )

    scrubbed = _adapter().claim(
        ITEM, RUN_ID, BRANCH, SCOPE, CLAIM_ID, REASONS, CHECKOUT
    )
    cut = _adapter().claim(ITEM, RUN_ID, BRANCH, SCOPE, CLAIM_ID, REASONS, CHECKOUT)

    assert isinstance(scrubbed, ClaimRefusal)
    assert scrubbed.detail == f"origin refused the token {REDACTION_MARKER}"
    assert isinstance(cut, ClaimRefusal)
    assert cut.detail == "x" * MAXIMUM_CLAIM_REFUSAL_DETAIL_BYTES


def test_adapter_reads_back_a_claim_that_carries_a_whole_reason(
    recorded_process: RecordedProcess,
) -> None:
    recorded_process.outputs.append(_status_payload(whole="the run owns all source"))

    assert _adapter().read_back(ITEM, CLAIM_ID, CHECKOUT) == ClaimReceipt(
        ITEM,
        CLAIM_ID,
        AGENT,
        BRANCH,
        SCOPE,
        (),
    )


def test_adapter_accepts_an_allocated_resource_value(
    recorded_process: RecordedProcess,
) -> None:
    recorded_process.outputs.extend(
        (
            _claim_payload(resource="claim-number", resource_value=3),
            _status_payload(resource="claim-number", resource_value=3),
        )
    )

    assert isinstance(
        _adapter().claim(ITEM, RUN_ID, BRANCH, SCOPE, CLAIM_ID, REASONS, CHECKOUT),
        ClaimReceipt,
    )
    assert isinstance(_adapter().read_back(ITEM, CLAIM_ID, CHECKOUT), ClaimReceipt)


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

    assert _adapter().claim(
        ITEM, RUN_ID, BRANCH, SCOPE, CLAIM_ID, REASONS, CHECKOUT
    ) == ClaimRefusal(
        ClaimRefusalReason.UNKNOWN, "aco returned an invalid resource pair"
    )
    assert _adapter().read_back(ITEM, CLAIM_ID, CHECKOUT) == ClaimRefusal(
        ClaimRefusalReason.UNKNOWN, "aco returned an invalid resource pair"
    )


ANOTHER_CLAIM_POSTED = "aco posted a claim other than the one requested"


@pytest.mark.parametrize(
    ("payload", "detail"),
    (
        (lambda: _claim_payload(agent="atelier2 run other"), ANOTHER_CLAIM_POSTED),
        (
            lambda: _claim_payload(role="reviewer"),
            "aco posted a claim under another role",
        ),
        (lambda: _claim_payload(claim_id="another-claim"), ANOTHER_CLAIM_POSTED),
    ),
)
def test_adapter_refuses_claim_receipts_that_do_not_match_the_request(
    recorded_process: RecordedProcess, payload: Callable[[], bytes], detail: str
) -> None:
    recorded_process.outputs.append(payload())

    assert _adapter().claim(
        ITEM, RUN_ID, BRANCH, SCOPE, CLAIM_ID, REASONS, CHECKOUT
    ) == ClaimRefusal(ClaimRefusalReason.UNKNOWN, detail)


@pytest.mark.parametrize(
    "payload",
    (
        lambda: _release_payload(agent="atelier2 run other"),
        lambda: _release_payload(role="reviewer"),
    ),
)
def test_adapter_refuses_release_receipts_that_do_not_match_the_request(
    recorded_process: RecordedProcess, payload: Callable[[], bytes]
) -> None:
    recorded_process.outputs.append(payload())

    assert _adapter().release(ITEM, RUN_ID, CLAIM_ID, Merged(44)) == ClaimRefusal(
        ClaimRefusalReason.UNKNOWN,
        "aco released a claim other than the one requested",
    )


def test_a_claim_receipt_carries_the_scope_the_ledger_recorded(
    recorded_process: RecordedProcess,
) -> None:
    """The ledger's own answer travels on, so its reader compares rather than assumes."""

    recorded_process.outputs.append(_claim_payload(scope=["src/other.py"]))

    receipt = _adapter().claim(ITEM, RUN_ID, BRANCH, SCOPE, CLAIM_ID, REASONS, CHECKOUT)

    assert isinstance(receipt, ClaimReceipt)
    assert receipt.claimed_scope == (PurePosixPath("src/other.py"),)


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

    assert _adapter().claim(
        ITEM, RUN_ID, BRANCH, SCOPE, CLAIM_ID, REASONS, CHECKOUT
    ) == ClaimRefusal(ClaimRefusalReason.UNKNOWN)


def test_fake_and_adapter_meet_the_same_port_expectations(
    recorded_process: RecordedProcess,
) -> None:
    touch = ClaimTouch(77, "other-claim", "other agent", SCOPE)
    expected = ClaimReceipt(ITEM, CLAIM_ID, AGENT, BRANCH, SCOPE, (touch,))
    fake = FakeWorkItemClaims(claim_answer=expected, read_back_answer=expected)
    touching_claim = {
        "issue": 77,
        "lane": None,
        "claim_id": "other-claim",
        "agent": "other agent",
        "scope": [path.as_posix() for path in SCOPE],
    }
    recorded_process.outputs.extend(
        (
            _claim_payload(touches=[touching_claim]),
            _status_payload(
                claims=[
                    _standing_claim(
                        overlaps=[
                            {
                                "issue": 77,
                                "lane": None,
                                "claim_id": "other-claim",
                                "agent": "other agent",
                            }
                        ]
                    ),
                    _standing_claim(
                        claim_id="other-claim",
                        item=77,
                        agent="other agent",
                        branch="other/lane",
                    ),
                ]
            ),
            _release_payload(),
            _release_payload(reason="abandoned: no longer needed"),
        )
    )

    assert (
        fake.claim(ITEM, RUN_ID, BRANCH, SCOPE, CLAIM_ID, REASONS, CHECKOUT) == expected
    )
    receipt = _adapter().claim(ITEM, RUN_ID, BRANCH, SCOPE, CLAIM_ID, REASONS, CHECKOUT)

    assert receipt == expected
    assert not isinstance(receipt, ClaimRefusal)
    assert fake.read_back(ITEM, CLAIM_ID, CHECKOUT) == _adapter().read_back(
        ITEM, CLAIM_ID, CHECKOUT
    )
    assert fake.release(ITEM, RUN_ID, CLAIM_ID, Merged(44)) is _adapter().release(
        ITEM, RUN_ID, CLAIM_ID, Merged(44)
    )
    assert fake.release(
        ITEM, RUN_ID, CLAIM_ID, Abandoned("no longer needed")
    ) is _adapter().release(ITEM, RUN_ID, CLAIM_ID, Abandoned("no longer needed"))

    refused = ClaimRefusal(
        ClaimRefusalReason.UNKNOWN,
        "claim branch 'x' does not match checkout branch 'main'",
    )
    refusing = FakeWorkItemClaims(claim_answer=refused)
    recorded_process.outputs.append(b"")
    recorded_process.errors.append(
        b"ERROR: claim branch 'x' does not match checkout branch 'main'\n"
    )

    assert refusing.claim(
        ITEM, RUN_ID, BRANCH, SCOPE, CLAIM_ID, REASONS, CHECKOUT
    ) == _adapter().claim(ITEM, RUN_ID, BRANCH, SCOPE, CLAIM_ID, REASONS, CHECKOUT)


def test_adapter_builds_an_abandon_release(recorded_process: RecordedProcess) -> None:
    recorded_process.outputs.append(
        _release_payload(reason="abandoned: no longer needed")
    )

    assert (
        _adapter().release(ITEM, RUN_ID, CLAIM_ID, Abandoned("no longer needed"))
        is None
    )
    assert recorded_process.commands == [
        (
            "aco",
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
