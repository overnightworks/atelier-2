"""The catalog revisions an issue-to-pr-shaped document pins before it starts.

`workflows/issue-to-pr.yaml` and `workflows/head-loop.yaml` end in the same
build, review, release and open-PR nodes, so both name the same grants, budget,
adapter operations and result schemas by hash. The live push operation names
the operator as author and one exact model as committer, and the shipped push
grant's hash derives from those bytes, so a start resolves the grant a document
names only when the same pair is published here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.engine import Engine

from atelier2.contracts.adapter_operations_v3 import AdapterOperationName
from atelier2.contracts.effect_requests import GitCommitIdentity
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.contracts.tool_grants_v3 import ToolGrantCapability
from atelier2.contracts.work_items import WORK_ITEM_ORDER_SCHEMA_DOCUMENT
from tests.scenarios.runs import publish_pinned_revisions

_SCHEMA_DIRECTORY = Path("workflows/schemas")
_BUDGET_PATH = Path("workflows/budgets/push-implement.json")

_SHARED_SCHEMA_NAMES = (
    "issue_to_pr_candidate_report",
    "code_review_result",
    "issue_to_pr_release_decision",
)


@dataclass(frozen=True)
class PublishedIssueToPrCatalog:
    """The revisions and commit identities such a document's start resolves."""

    push_grant: PublishedRevision
    verification_grant: PublishedRevision
    author: GitCommitIdentity
    committer: GitCommitIdentity


def published_schema(name: str) -> PublishedRevision:
    """One shipped schema document, published under the hash its author pinned."""
    return PublishedRevision(
        RevisionKind.SCHEMA, (_SCHEMA_DIRECTORY / f"{name}.json").read_bytes()
    )


def publish_issue_to_pr_catalog(
    engine: Engine, *also_pinned: PublishedRevision
) -> PublishedIssueToPrCatalog:
    """Publish the shared set, and whatever else the calling document pins."""
    connected_account_address = "44832414+FlexOr2@users.noreply.github.com"
    author = GitCommitIdentity("Felix Hummert", connected_account_address)
    committer = GitCommitIdentity("Grok 4.6", connected_account_address)
    push_operation = _push_operation(author, committer)
    catalog = PublishedIssueToPrCatalog(
        _push_grant(push_operation), _verification_grant(), author, committer
    )
    publish_pinned_revisions(
        engine,
        PublishedRevision(RevisionKind.SCHEMA, WORK_ITEM_ORDER_SCHEMA_DOCUMENT),
        *(published_schema(name) for name in _SHARED_SCHEMA_NAMES),
        PublishedRevision(RevisionKind.BUDGET_POLICY, _BUDGET_PATH.read_bytes()),
        push_operation,
        PublishedRevision(RevisionKind.ADAPTER_OPERATION, b'{"operation":"open-pr"}'),
        catalog.push_grant,
        catalog.verification_grant,
        *also_pinned,
    )
    return catalog


def _push_operation(
    author: GitCommitIdentity, committer: GitCommitIdentity
) -> PublishedRevision:
    return PublishedRevision(
        RevisionKind.ADAPTER_OPERATION,
        _canonical(
            {
                "operation": AdapterOperationName.PUSH_ATELIER_COMMIT.value,
                "author": author.as_json(),
                "committer": committer.as_json(),
            }
        ),
    )


def _push_grant(operation: PublishedRevision) -> PublishedRevision:
    return PublishedRevision(
        RevisionKind.TOOL,
        _canonical(
            {
                "capability": ToolGrantCapability.PUSH_ATELIER_COMMIT.value,
                "operation": {
                    "ref": "push-atelier-commit",
                    "revision": operation.revision_hash.value,
                },
            }
        ),
    )


def _verification_grant() -> PublishedRevision:
    return PublishedRevision(
        RevisionKind.TOOL,
        _canonical({"capability": ToolGrantCapability.RUN_PROJECT_VERIFICATION.value}),
    )


def _canonical(document: dict[str, object]) -> bytes:
    """The bytes a published revision is hashed as: sorted keys, tight separators."""
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
