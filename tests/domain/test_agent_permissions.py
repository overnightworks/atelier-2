"""What a provider may do is decided against one bound policy revision, and kept.

ADR 0020 §2 and §3. No deployment grants anything before the first asking
provider channel (`#1177` step 2), so the refusals here are what a live run
answers; a granted receipt appears only where the record has to say that a yes
and a no are two different facts.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from atelier2.contracts.agent_attempts import AgentAttemptId
from atelier2.contracts.agent_permissions import (
    GRANTS_NOTHING,
    PermissionAuthority,
    PermissionCorrelationId,
    PermissionEffect,
    PermissionPolicyRevision,
    PermissionPolicyRevisionHash,
    PermissionReceipt,
    PermissionReceiptHash,
    PermissionRequest,
    PermissionScope,
    PermissionScopeKind,
    decide,
    refuse,
)
from atelier2.contracts.when import RecordedAt
from tests.scenarios.agents import agent_attempt_execution, agent_execution_request_v2

THE_LEASE = PermissionScope(PermissionScopeKind.PATH_PREFIX, "/lease/")
ANOTHER_DIRECTORY = PermissionScope(PermissionScopeKind.PATH_PREFIX, "/etc/")
THE_TEST_COMMAND = PermissionScope(PermissionScopeKind.COMMAND_NAME, "pytest")

MAY_READ_THE_LEASE = PermissionPolicyRevision(
    frozenset({(PermissionEffect.WORKSPACE_READ, THE_LEASE)})
)

A_KNOWN_ATTEMPT = AgentAttemptId("00112233445566778899aabbccddeeff" * 2)


def an_attempt_id() -> AgentAttemptId:
    return agent_attempt_execution(agent_execution_request_v2()).attempt_id


def a_question(effect: PermissionEffect, scope: PermissionScope) -> PermissionRequest:
    return PermissionRequest(
        effect, scope, PermissionCorrelationId.for_call(an_attempt_id(), 1)
    )


@pytest.mark.parametrize(
    ("effect", "scope"),
    [
        pytest.param(
            PermissionEffect.WORKSPACE_WRITE, THE_LEASE, id="effect-the-policy-omits"
        ),
        pytest.param(
            PermissionEffect.WORKSPACE_READ, ANOTHER_DIRECTORY, id="another-path"
        ),
        pytest.param(
            PermissionEffect.WORKSPACE_READ, THE_TEST_COMMAND, id="another-scope-kind"
        ),
        pytest.param(PermissionEffect.NETWORK, THE_TEST_COMMAND, id="neither"),
    ],
)
def test_a_question_the_policy_does_not_state_exactly_is_refused(
    effect: PermissionEffect, scope: PermissionScope
) -> None:
    question = a_question(effect, scope)

    decision = decide(MAY_READ_THE_LEASE, question)

    assert decision.granted is False
    assert decision.correlation_id == question.correlation_id
    assert decision.policy_revision_hash == MAY_READ_THE_LEASE.revision_hash
    assert decision.authority is PermissionAuthority.POLICY


def test_the_same_permission_stated_twice_is_the_same_revision() -> None:
    """Two constructions of one permission answer with one identity."""

    grants = (
        (PermissionEffect.WORKSPACE_READ, THE_LEASE),
        (PermissionEffect.COMMAND, THE_TEST_COMMAND),
    )

    written_one_way = PermissionPolicyRevision(frozenset(grants))
    written_the_other_way = PermissionPolicyRevision(frozenset(reversed(grants)))

    assert written_one_way.revision_hash == written_the_other_way.revision_hash
    assert written_one_way.revision_hash != MAY_READ_THE_LEASE.revision_hash


def test_a_policy_that_grants_nothing_still_has_its_own_identity() -> None:
    assert (
        PermissionPolicyRevision(frozenset()).revision_hash
        != MAY_READ_THE_LEASE.revision_hash
    )


def test_one_call_of_one_attempt_always_addresses_the_same_question() -> None:
    """The id comes from durable truth, so a provider cannot spell it away."""

    attempt_id = an_attempt_id()

    first_construction = PermissionCorrelationId.for_call(attempt_id, 1)
    second_construction = PermissionCorrelationId.for_call(attempt_id, 1)

    assert first_construction == second_construction


def test_each_call_of_one_attempt_is_its_own_question() -> None:
    attempt_id = an_attempt_id()

    assert PermissionCorrelationId.for_call(
        attempt_id, 1
    ) != PermissionCorrelationId.for_call(attempt_id, 2)


def test_the_first_file_request_and_the_first_question_are_two_questions() -> None:
    """A conversation counts file requests apart from permission questions, so
    the two families are framed apart: one attempt's first file request never
    lands on the row its first permission question wrote."""

    attempt_id = an_attempt_id()

    assert PermissionCorrelationId.for_file_call(
        attempt_id, 1
    ) != PermissionCorrelationId.for_call(attempt_id, 1)
    assert PermissionCorrelationId.for_file_call(
        attempt_id, 1
    ) == PermissionCorrelationId.for_file_call(attempt_id, 1)


def test_a_question_refused_unasked_names_the_revision_it_ran_under() -> None:
    """The fence, not the policy, found the path was never the workspace: the
    policy that grants the workspace is not asked, yet the refusal still stands
    on that revision's hash, because every receipt of one execution does."""

    question = a_question(PermissionEffect.WORKSPACE_READ, THE_LEASE)

    withheld = refuse(MAY_READ_THE_LEASE, question)

    assert decide(MAY_READ_THE_LEASE, question).granted is True
    assert withheld.granted is False
    assert withheld.correlation_id == question.correlation_id
    assert withheld.policy_revision_hash == MAY_READ_THE_LEASE.revision_hash
    assert withheld.authority is PermissionAuthority.POLICY


def test_the_same_call_of_two_attempts_are_two_questions() -> None:
    other = agent_attempt_execution(
        agent_execution_request_v2("scenario/another-run")
    ).attempt_id

    assert PermissionCorrelationId.for_call(
        an_attempt_id(), 1
    ) != PermissionCorrelationId.for_call(other, 1)


@pytest.mark.parametrize(
    "call_ordinal",
    [pytest.param(0, id="before-the-first"), pytest.param(-1, id="negative")],
)
def test_a_call_ordinal_outside_the_counted_calls_is_refused(
    call_ordinal: int,
) -> None:
    attempt_id = an_attempt_id()

    with pytest.raises(ValueError, match="counts from one"):
        PermissionCorrelationId.for_call(attempt_id, call_ordinal)


def test_the_closed_policy_keeps_the_identity_it_was_landed_with() -> None:
    """A refusal names its authority by hash, so that hash is a pinned word.

    A change to the framing domain or to how a grant is spelled inside it would
    rewrite the authority every stored decision points at, silently, while every
    relative comparison stayed green.
    """

    assert GRANTS_NOTHING.revision_hash == PermissionPolicyRevisionHash(
        "981249295114b1d9d33963c58c7f802b8604d003608482d2befd259a1124a06b"
    )


def test_a_known_call_of_a_known_attempt_keeps_its_pinned_question_id() -> None:
    """The id of a question is durable, so its ordinal encoding is pinned too."""

    assert PermissionCorrelationId.for_call(
        A_KNOWN_ATTEMPT, 1
    ) == PermissionCorrelationId(
        "9209b5360192f7135218df0f6af1a830e0645848111a55d2dffd19b6653884e6"
    )


A_SECOND_ATTEMPT = AgentAttemptId("ffeeddccbbaa99887766554433221100" * 2)
WHEN_IT_WAS_DECIDED = RecordedAt("2026-09-05T08:00:00Z")


def a_receipt(
    policy: PermissionPolicyRevision = MAY_READ_THE_LEASE,
    effect: PermissionEffect = PermissionEffect.WORKSPACE_READ,
    scope: PermissionScope = THE_LEASE,
    attempt_id: AgentAttemptId = A_KNOWN_ATTEMPT,
    call_ordinal: int = 1,
    decided_at: RecordedAt = WHEN_IT_WAS_DECIDED,
) -> PermissionReceipt:
    """The receipt of one attempt asking one thing and being answered."""

    question = PermissionRequest(
        effect, scope, PermissionCorrelationId.for_call(attempt_id, call_ordinal)
    )
    return PermissionReceipt.of(
        attempt_id, question, decide(policy, question), decided_at
    )


def test_a_receipt_carries_what_was_asked_and_what_the_policy_answered() -> None:
    kept = a_receipt()

    assert kept.attempt_id == A_KNOWN_ATTEMPT
    assert kept.correlation_id == PermissionCorrelationId.for_call(A_KNOWN_ATTEMPT, 1)
    assert (kept.effect, kept.scope) == (PermissionEffect.WORKSPACE_READ, THE_LEASE)
    assert kept.granted is True
    assert kept.policy_revision_hash == MAY_READ_THE_LEASE.revision_hash
    assert kept.authority is PermissionAuthority.POLICY
    assert kept.decided_at == WHEN_IT_WAS_DECIDED


def test_a_refused_question_is_a_receipt_like_any_other() -> None:
    """What a run refused is a fact it records, not a gap a reader infers."""

    refused = a_receipt(policy=GRANTS_NOTHING)

    assert refused.granted is False
    assert refused.policy_revision_hash == GRANTS_NOTHING.revision_hash
    assert refused.correlation_id == a_receipt().correlation_id


def test_one_decision_written_at_two_instants_is_one_receipt() -> None:
    """The clock records the writing; it never decides which receipt this is.

    A recovered attempt asks its questions again and is answered again, seconds
    or days later. Were the instant part of the identity, that second write
    would be a second authorisation of a decision nobody made twice.
    """

    written_later = a_receipt(decided_at=RecordedAt("2026-09-06T09:30:00Z"))

    assert written_later.decided_at != a_receipt().decided_at
    assert written_later.receipt_hash == a_receipt().receipt_hash


@pytest.mark.parametrize(
    "other",
    [
        pytest.param(
            lambda: a_receipt(call_ordinal=2), id="another-question-of-this-attempt"
        ),
        pytest.param(
            lambda: a_receipt(attempt_id=A_SECOND_ATTEMPT), id="another-attempt"
        ),
        pytest.param(
            lambda: a_receipt(effect=PermissionEffect.WORKSPACE_WRITE),
            id="another-effect",
        ),
        pytest.param(lambda: a_receipt(scope=ANOTHER_DIRECTORY), id="another-scope"),
        pytest.param(
            lambda: a_receipt(policy=GRANTS_NOTHING), id="another-answer-and-authority"
        ),
    ],
)
def test_receipts_that_record_anything_different_are_different_receipts(
    other: Callable[[], PermissionReceipt],
) -> None:
    """Everything a reader would judge the decision by is inside its identity."""

    assert other().receipt_hash != a_receipt().receipt_hash


def test_a_known_decision_keeps_the_receipt_identity_it_was_landed_with() -> None:
    """A stored receipt is addressed by this hash, so its framing is pinned.

    A change to the framing domain, to the field order, or to how an answer is
    spelled inside it would silently re-address every receipt the store holds,
    while every relative comparison above stayed green.
    """

    assert a_receipt().receipt_hash == PermissionReceiptHash(
        "63b9c58079c8dc5f39f41582e0415e18e2d0cddc781951328d5b7ec83279a27d"
    )
