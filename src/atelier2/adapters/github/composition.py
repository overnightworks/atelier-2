"""Composing the live GitHub surfaces from a project-source connection record.

A connection revision carries its source address as an opaque value (ADR 0010
decision 1), and this module is that address's one decode owner for the
`github` source kind: branchless `owner/name` identity plus an adapter-owned
base ref. Serving hands the whole revision over and receives the effect
registry or open-issue observation source; no repository fact travels back
above this package.

The revision's credential directory stays a reference here exactly as it is in
the record (ADR 0009 §6): the token it names is read when the composed surface
reaches GitHub, never during composition.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import assert_never

from atelier2.adapters.git_transport.composition import (
    GitTransportConfiguration,
    compose_git_transport_effect_adapter,
)
from atelier2.adapters.github.live_effects import (
    GITHUB_TOKEN_CREDENTIAL_ENTRY,
    GitHubRepository,
    GitHubTokenCredential,
    LiveGitHubEffectAdapterFactory,
    LiveGitHubHeadBranchPullRequests,
)
from atelier2.adapters.github.observation import LiveGitHubIssueSource
from atelier2.contracts.adapter_operations_v3 import AdapterOperationName
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.host_configuration import (
    ProjectSourceConnectionRevision,
    SourceAddress,
    SourceConnectionAuthMethod,
    SourceKind,
    SourceReference,
)
from atelier2.ports.effects import (
    EffectAdapterRegistration,
    EffectAdapterRegistry,
)

GITHUB_SOURCE_KIND = SourceKind("github")


class GitHubConnectionUncomposable(ValueError):
    """The connection record does not compose this package's live surfaces."""


def github_public_source_address(source_address: SourceAddress) -> str:
    if "@" in source_address.value:
        raise GitHubConnectionUncomposable(
            "a github source address stores only owner/name; its ref is a detail"
        )
    owner, slash, name = source_address.value.partition("/")
    if not slash or not owner or not name or "/" in name:
        raise GitHubConnectionUncomposable(
            "a github source address stores only owner/name; its ref is a detail"
        )
    return source_address.value


def migrate_v44_github_source_location(
    source_kind: SourceKind, source_address: SourceAddress
) -> tuple[SourceAddress, SourceReference | None]:
    if source_kind != GITHUB_SOURCE_KIND:
        return source_address, None
    public_address, separator, source_ref = source_address.value.partition("@")
    if not separator:
        github_public_source_address(source_address)
        return source_address, None
    if not source_ref:
        raise GitHubConnectionUncomposable(
            "a legacy github source location requires a nonempty base ref"
        )
    migrated_address = SourceAddress(public_address)
    github_public_source_address(migrated_address)
    return migrated_address, SourceReference(source_ref)


def live_github_effect_adapter_factory(
    connection: ProjectSourceConnectionRevision,
    adapter_revision: AdapterRevision,
    destination: EffectDestination,
) -> LiveGitHubEffectAdapterFactory:
    return LiveGitHubEffectAdapterFactory(
        adapter_revision,
        destination,
        _connected_repository(connection),
        _token_credential(connection),
    )


def live_github_effect_registry(
    connection: ProjectSourceConnectionRevision,
    candidate_store: Path,
    adapter_revision: AdapterRevision,
    destination: EffectDestination,
) -> EffectAdapterRegistry:
    repository = _connected_repository(connection)
    push = compose_git_transport_effect_adapter(
        candidate_store,
        GitTransportConfiguration(
            f"github:{repository.owner}/{repository.name}",
            f"https://github.com/{repository.owner}/{repository.name}.git",
            connection.credential_directory / GITHUB_TOKEN_CREDENTIAL_ENTRY,
        ),
        LiveGitHubHeadBranchPullRequests(repository, _token_credential(connection)),
    )
    open_pr = replace(
        live_github_effect_adapter_factory(connection, adapter_revision, destination),
        documentation_publisher_factory=push,
    )
    return EffectAdapterRegistry(
        (
            EffectAdapterRegistration(AdapterOperationName.OPEN_PR, open_pr),
            EffectAdapterRegistration(AdapterOperationName.PUSH_ATELIER_COMMIT, push),
        )
    )


def live_github_head_branch_pull_requests(
    connection: ProjectSourceConnectionRevision,
) -> LiveGitHubHeadBranchPullRequests:
    """Compose the reader that says which branch still carries an open review.

    The same surface the publishing adapter is composed with, given its own
    factory because a second caller asks the question about a branch it did
    not publish to: the runtime, about a lane whose run has ended.
    """

    return LiveGitHubHeadBranchPullRequests(
        _connected_repository(connection), _token_credential(connection)
    )


def live_github_issue_source(
    connection: ProjectSourceConnectionRevision,
) -> LiveGitHubIssueSource:
    return LiveGitHubIssueSource(
        _connected_repository(connection),
        _token_credential(connection),
    )


def _connected_repository(
    connection: ProjectSourceConnectionRevision,
) -> GitHubRepository:
    if connection.source_kind != GITHUB_SOURCE_KIND:
        raise GitHubConnectionUncomposable(
            f"source kind {connection.source_kind.value!r} has no live "
            f"composition; only {GITHUB_SOURCE_KIND.value!r} composes here"
        )
    return _repository(connection.source_address, connection.source_ref)


def _token_credential(
    connection: ProjectSourceConnectionRevision,
) -> GitHubTokenCredential:
    match connection.auth_method:
        case SourceConnectionAuthMethod.PERSONAL_ACCESS_TOKEN:
            return GitHubTokenCredential(connection.credential_directory)
        case _ as unreachable:
            assert_never(unreachable)


def _repository(
    address: SourceAddress, source_ref: SourceReference | None
) -> GitHubRepository:
    repository = github_public_source_address(address)
    owner, _slash, name = repository.partition("/")
    if source_ref is None or not source_ref.value:
        raise GitHubConnectionUncomposable(
            "a github source reads owner/name with one base ref, "
            f"not {address.value!r}; reconnect the project with that address"
        )
    return GitHubRepository(owner, name, source_ref.value)
