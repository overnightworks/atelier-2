"""The GitHub adapter's open-pr effect: readback-then-create, one PR, no twin."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from atelier2.adapters.github.effects import (
    GitHubEffectAdapterFactory,
    GitHubEffectRefused,
)
from atelier2.contracts.effect_markers import body_carries_request_hash, marker_line
from atelier2.contracts.effect_requests import HeadBranch, OpenPullRequest
from atelier2.contracts.effects import (
    AdapterOperationalIdentity,
    AdapterRevision,
    CanonicalRequest,
    ConfirmationSource,
    EffectAbsence,
    EffectBinding,
    EffectDestination,
    EffectIntent,
    EffectReceipt,
    LogicalEffectKey,
    ReadbackPhase,
)
from atelier2.contracts.queue_projection import TrackerItemReference
from atelier2.contracts.runs import RunId, WorkflowRevision

ADAPTER_REVISION = AdapterRevision("github-open-pr-v1")
DESTINATION = EffectDestination("platform")
LOGICAL_KEY = LogicalEffectKey("run-1/open-pr")
TREE = json.dumps({"files": {"hello.txt": "from the builder"}}).encode("utf-8")
HEAD_BRANCH = HeadBranch("atelier2/work-item/" + "a" * 64)
CANARY_TOKEN = "gho_atelier2_canary_token_must_not_appear"


@pytest.fixture
def factory(tmp_path: Path) -> GitHubEffectAdapterFactory:
    return GitHubEffectAdapterFactory(
        tmp_path / "github.sqlite",
        ADAPTER_REVISION,
        DESTINATION,
    )


def effect_intent(factory: GitHubEffectAdapterFactory) -> EffectIntent:
    return EffectIntent(
        EffectBinding(
            logical_key=LOGICAL_KEY,
            run_id=RunId("run-1"),
            workflow_revision_hash=WorkflowRevision(b"workflow-v1").revision_hash,
            adapter_revision=ADAPTER_REVISION,
            destination=DESTINATION,
            adapter_operational_identity=AdapterOperationalIdentity(
                str(factory.database_path.resolve())
            ),
        ),
        CanonicalRequest(
            OpenPullRequest(TREE.decode("utf-8"), HEAD_BRANCH).canonical_bytes()
        ),
    )


def test_execute_records_one_pull_request_with_the_request_hash_in_its_body(
    factory: GitHubEffectAdapterFactory,
) -> None:
    intent = effect_intent(factory)
    adapter = factory.open()
    try:
        assert isinstance(
            adapter.readback(intent, ReadbackPhase.BEFORE_SEND), EffectAbsence
        )
        performed = adapter.execute(intent)
    finally:
        adapter.close()

    recorded = factory.recorded_pull_requests()
    assert len(recorded) == 1
    pull_request = recorded[0]
    result = json.loads(performed.result.payload.decode("utf-8"))
    assert result == {
        "branch": pull_request.branch,
        "pr_number": pull_request.pr_number,
    }
    assert pull_request.pr_number == 1
    assert pull_request.branch == HEAD_BRANCH.value
    assert body_carries_request_hash(
        pull_request.body, intent.request.request_hash.value
    )
    assert marker_line(intent.request.request_hash.value) in pull_request.body
    assert CANARY_TOKEN not in pull_request.body
    assert CANARY_TOKEN.encode() not in performed.result.payload
    assert CANARY_TOKEN.encode() not in factory.database_path.read_bytes()


def test_a_second_execute_finds_the_same_pull_request_and_does_not_create_a_twin(
    factory: GitHubEffectAdapterFactory,
) -> None:
    intent = effect_intent(factory)
    adapter = factory.open()
    try:
        first = adapter.execute(intent)
        second = adapter.execute(intent)
        read = adapter.readback(intent, ReadbackPhase.AFTER_SEND)
    finally:
        adapter.close()

    assert first.effect_id == second.effect_id
    assert first.result == second.result
    assert isinstance(read, EffectReceipt)
    assert read.effect_id == first.effect_id
    assert read.confirmation_source is ConfirmationSource.ADAPTER_READBACK
    assert factory.recorded_pull_requests()[0].pr_number == 1
    assert len(factory.recorded_pull_requests()) == 1


def test_an_open_pr_request_round_trips_its_typed_work_item_reference() -> None:
    request = OpenPullRequest(
        TREE.decode("utf-8"), HEAD_BRANCH, TrackerItemReference("gh:1232")
    )

    restored = OpenPullRequest.from_canonical_bytes(request.canonical_bytes())

    assert restored == request


def test_an_open_pr_request_without_a_work_item_reference_remains_readable() -> None:
    legacy = json.dumps(
        {"body": TREE.decode("utf-8"), "head_branch": HEAD_BRANCH.value},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")

    restored = OpenPullRequest.from_canonical_bytes(legacy)

    assert restored == OpenPullRequest(TREE.decode("utf-8"), HEAD_BRANCH)


def test_a_reference_less_open_pr_requests_canonical_bytes_are_unchanged() -> None:
    """A reconciled in-flight intent's identity outlives #1290's new field.

    The canonical bytes (and the hash derived from them) are the durable
    identity an in-flight `open-pr` intent is reconciled by; an intent opened
    before #1290 carried no `work_item_reference` key at all, so one without a
    reference today must still encode to exactly that two-field form, not to
    the same fields plus a `null`.
    """
    request = OpenPullRequest(TREE.decode("utf-8"), HEAD_BRANCH)

    assert request.canonical_bytes() == json.dumps(
        {"body": TREE.decode("utf-8"), "head_branch": HEAD_BRANCH.value},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


@pytest.mark.parametrize("operation", ["readback", "execute"])
def test_a_malformed_open_pr_payload_is_refused_before_the_recorded_adapter_writes(
    factory: GitHubEffectAdapterFactory,
    operation: str,
    malformed_open_pr_payload: bytes,
) -> None:
    original = effect_intent(factory)
    intent = EffectIntent(original.binding, CanonicalRequest(malformed_open_pr_payload))
    adapter = factory.open()
    reaching_the_destination = {
        "readback": lambda: adapter.readback(intent, ReadbackPhase.BEFORE_SEND),
        "execute": lambda: adapter.execute(intent),
    }
    try:
        with pytest.raises(GitHubEffectRefused, match="canonical open-pr request"):
            reaching_the_destination[operation]()
    finally:
        adapter.close()

    assert factory.recorded_pull_requests() == ()
    assert factory.recorded_documentation_pushes() == ()
