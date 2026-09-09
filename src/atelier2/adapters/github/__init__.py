"""The GitHub platform adapter, platform-blind above its composition boundary.

Two `EffectAdapterFactory` implementations share the one `open-pr` contract:
`GitHubEffectAdapterFactory` records against a fake platform, and
`LiveGitHubEffectAdapterFactory` publishes and reads back through `githubkit`
against real GitHub (ADR 0010). The live one is composed on serve from the
served project's source-connection record, whose opaque address this package
alone decodes (`atelier2.adapters.github.composition`), together with the
forge-neutral git transport that publishes the candidate before `open-pr`; the
fake stays a test double. Neither live operation treats an unmatched readback
as authoritative absence, so it waits for operator reconciliation.
"""

from atelier2.adapters.github.composition import (
    GITHUB_SOURCE_KIND,
    GitHubConnectionUncomposable,
    live_github_effect_adapter_factory,
    live_github_effect_registry,
    live_github_head_branch_pull_requests,
    live_github_issue_source,
)
from atelier2.adapters.github.effects import GitHubEffectAdapterFactory
from atelier2.adapters.github.live_effects import (
    GitHubCredentialUnresolvable,
    GitHubRepository,
    GitHubTokenCredential,
    GitHubUnexpectedResponse,
    LiveGitHubEffectAdapterFactory,
    LiveGitHubHeadBranchPullRequests,
)
from atelier2.adapters.github.observation import LiveGitHubIssueSource

__all__ = (
    "GITHUB_SOURCE_KIND",
    "GitHubConnectionUncomposable",
    "GitHubCredentialUnresolvable",
    "GitHubEffectAdapterFactory",
    "GitHubRepository",
    "GitHubTokenCredential",
    "GitHubUnexpectedResponse",
    "LiveGitHubEffectAdapterFactory",
    "LiveGitHubHeadBranchPullRequests",
    "LiveGitHubIssueSource",
    "live_github_effect_adapter_factory",
    "live_github_effect_registry",
    "live_github_head_branch_pull_requests",
    "live_github_issue_source",
)
