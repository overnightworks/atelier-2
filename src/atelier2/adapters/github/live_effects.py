"""Publication and readback for the `open-pr` adapter operation against live GitHub.

The same head-branch listing also answers `ports.effects.HeadBranchPullRequests`
for the git transport, which asks whether anyone still reviews a branch before
replacing what it carries (ADR 0010 §5's 2026-09-05 amendment on #1224).

Same contract as the fake platform's `atelier2.adapters.github.effects`:
`readback` then `execute`, the request hash carried as a marker inside the
pull request's own body, and idempotency by that marker rather than by any
identifier GitHub assigns.

What an unmatched read means here is decided by which read is used, and when
(#1210). Listing pull requests by their exact head branch is not the eventually
consistent search index: it is a direct query about one branch, and a `200`
answering it with an empty list is GitHub's own statement that this branch
carries no pull request, in any state. Taken before anything was sent, that is
an authoritative absence, and reporting it as unknown was what made every live
run wait for an operator before it had sent anything at all. Taken after a
create was attempted -- `ReadbackPhase.AFTER_SEND` -- the same empty answer may
equally be a listing that has not caught up, so it stays unknown. So does a
read that failed: a refused status, any other status, a timeout, an answer that
is not a listing, or one naming pull requests none of which carries this
request's marker; each carries what GitHub said. A false absence still cannot
open a twin: GitHub's own head+base uniqueness refuses the second create with
`422`, and this adapter converges on the winner or reports its own outcome
unknown.

The client is `githubkit` (ADR 0010 §7): typed request construction, retries
and TLS are its job, not this module's. Which page of a listing is asked for
is this module's, because what it reads out of that answer decides whether a
send is licensed. This slice composes the personal-access-token method only
(ADR 0010 §2's low-friction path); the GitHub App method stays unbuilt here.

The credential reaches this adapter by reference, never by value (ADR 0009 §6,
ADR 0010 §3), the same pattern `ClaudeSubscriptionSettings.credential_directory`
already uses: the durable settings hold a directory, and the token itself is
read from it once, at `open()`, and lives nowhere durable afterward -- not in
a lease, a receipt, an event, a log, or an API projection.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import githubkit
import githubkit.exception
import httpx
from githubkit_schemas.latest.types import ReposOwnerRepoPullsPostBodyType

from atelier2.adapters.github.effects import (
    GitHubEffectRefused,
    OpenPullRequestRequest,
    ReviewedDocumentationPublisher,
    ReviewedDocumentationPublisherFactory,
    open_pull_request,
)
from atelier2.adapters.github.pull_request_prose import (
    ACCEPTANCE_LINE_PREFIX,
    neutralized_candidate_prose,
)
from atelier2.adapters.github.tracker_reference import github_issue_number
from atelier2.contracts.adapter_operations_v3 import AdapterOperationName
from atelier2.contracts.effect_markers import body_carries_request_hash, marker_line
from atelier2.contracts.effect_requests import (
    HeadBranch,
    OpenPullRequest,
    ReviewedDocumentationPullRequest,
)
from atelier2.contracts.effects import (
    AdapterOperationalIdentity,
    AdapterRevision,
    ConfirmationSource,
    EffectAdapterBinding,
    EffectDestination,
    EffectId,
    EffectIntent,
    EffectIntentMismatch,
    EffectReadback,
    EffectReceipt,
    EffectResult,
    EffectUnknownOutcome,
    PerformedEffect,
    ReadbackPhase,
    UnknownOutcomeReason,
    destination_holds_nothing,
)
from atelier2.ports.effects import (
    HeadBranchPullRequestState,
    HeadBranchPullRequestsUnreadable,
    NoPullRequestOpenOnHeadBranch,
    PullRequestOpenOnHeadBranch,
)

GITHUB_TOKEN_CREDENTIAL_ENTRY = "token"

# GitHub's own head+base uniqueness constraint on pull requests: the second of
# the two create-time races. A concurrent execute can create both the branch
# and the pull request between this adapter's search and its own create calls,
# and this is the status that create then answers with -- the create-branch
# race's exact counterpart, and equally not a refusal.
_PULL_REQUEST_ALREADY_EXISTS_STATUS = 422

_DEFAULT_PULL_REQUEST_TITLE = "Atelier open-pr"

# GitHub's own two states for a pull request: a merged one is closed carrying a
# merge, so "not open" is exactly the work no reviewer stands on any more.
_OPEN_PULL_REQUEST_STATE = "open"
_CLOSED_PULL_REQUEST_STATE = "closed"

# Only this status makes an empty listing GitHub's own answer that the branch
# carries no pull request (#1210). Any other status the client accepted says
# something this adapter did not ask for, and an absence is never read out of
# an answer to a different question.
_PULL_REQUEST_LISTING_STATUS = 200

# GitHub pages the head-branch listing, and several pull requests can stand on
# one head with different bases, so the marker is looked for across every page.
# The page bound turns a destination that never ends its listing into an
# unknown outcome the operator sees, rather than a request loop nobody sees.
PULL_REQUESTS_PER_LISTING_PAGE = 100
MAXIMUM_PULL_REQUEST_LISTING_PAGES = 10

# A GitHub pull request title reads as a headline, not a paragraph; 72
# characters is the conventional commit-summary width every reader here
# already expects (ADR-independent editorial choice, not a platform limit).
_MAXIMUM_RENDERED_TITLE_CHARACTERS = 72
_SENTENCE_TERMINATOR = re.compile(r"[.!?](?:\s|$)")

# A candidate's own summary is provider text: unbounded, and never rendered
# into Markdown without a ceiling. 4000 bounds the complete rendered body --
# prose, acceptance line, and trailer together -- keeping it readable and
# leaving ample room below GitHub's own much larger limit.
_MAXIMUM_RENDERED_BODY_CHARACTERS = 4000
_RENDERED_BODY_TRUNCATION_NOTE = "\n\n[truncated at 4000 characters]"


class GitHubCredentialUnresolvable(RuntimeError):
    """The bound token credential reference does not resolve (`platform-credential-unresolvable`)."""


class GitHubUnexpectedResponse(RuntimeError):
    """A platform response did not carry the shape this operation reads."""


@dataclass(frozen=True)
class GitHubTokenCredential:
    """Where the adapter resolves the personal-access-token credential.

    Pattern: `ClaudeSubscriptionSettings.credential_directory`
    (`atelier2.adapters.claude_subscription`). The directory is a deployment
    value, resolved once when the adapter opens; the token it names is never
    copied into anything durable this adapter writes.
    """

    credential_directory: Path

    def resolve(self) -> str:
        token_path = self.credential_directory / GITHUB_TOKEN_CREDENTIAL_ENTRY
        try:
            token = token_path.read_text(encoding="utf-8").strip()
        except OSError as error:
            raise GitHubCredentialUnresolvable(
                f"platform-credential-unresolvable: {token_path} did not "
                f"resolve a GitHub token: {error}"
            ) from error
        if not token:
            raise GitHubCredentialUnresolvable(
                f"platform-credential-unresolvable: {token_path} is empty"
            )
        return token


@dataclass(frozen=True)
class GitHubRepository:
    """The exact repository and base branch a connected project scopes to."""

    owner: str
    name: str
    base_branch: str

    def __post_init__(self) -> None:
        if not self.owner or not self.name or not self.base_branch:
            raise ValueError(
                "GitHubRepository requires a nonempty owner, name and base branch"
            )


@dataclass(frozen=True)
class _RecordedPullRequest:
    """One pull request this adapter found or created, as it names it."""

    branch: str
    pr_number: int
    body: str


@dataclass(frozen=True)
class _NoPullRequestOnBranch:
    """GitHub answered the head-branch listing, and it names no pull request.

    What that proves depends on when it was read, so it keeps what a reader
    after a send needs to report: the status GitHub answered with, and how long
    the whole listing took.
    """

    status_code: int
    duration_milliseconds: int

    def after_send(self) -> UnknownOutcomeReason:
        return UnknownOutcomeReason(
            self.status_code,
            self.duration_milliseconds,
            "the head branch listing named no pull request, "
            "and a create was already attempted",
        )


@dataclass(frozen=True)
class _PullRequestSearchFailed:
    """The listing resolved nothing this caller may act on."""

    reason: UnknownOutcomeReason


type _PullRequestSearch = (
    _RecordedPullRequest | _NoPullRequestOnBranch | _PullRequestSearchFailed
)


def _elapsed_milliseconds(started: float) -> int:
    return round((time.monotonic() - started) * 1_000)


def _refused_search(
    error: githubkit.exception.RequestError[Any], elapsed_milliseconds: int
) -> UnknownOutcomeReason:
    """Why a pull request listing did not resolve, in GitHub's own words.

    A refused request carries the status GitHub answered and the body it
    explained itself in; a timeout or a transport failure never reached a
    status at all, and then the client's own account of it is what there is.
    """

    if isinstance(error, githubkit.exception.RequestFailed):
        return UnknownOutcomeReason(
            error.response.status_code,
            elapsed_milliseconds,
            error.response.raw_response.text,
        )
    return UnknownOutcomeReason(None, elapsed_milliseconds, str(error.exc))


def _listed_page(response: httpx.Response) -> list[dict[str, Any]] | None:
    """One listing page as pull request objects, or nothing this adapter reads.

    Only the listing status answers the question that was asked; any other
    status the client accepted, an undecodable body, and a list holding
    anything but pull request objects are all answers to something else.
    """

    if response.status_code != _PULL_REQUEST_LISTING_STATUS:
        return None
    try:
        answered: Any = response.json()
    except ValueError:
        return None
    if not isinstance(answered, list) or not all(
        isinstance(pull_request, dict) for pull_request in answered
    ):
        return None
    return answered


@dataclass(frozen=True)
class _ListedPullRequestPage:
    """One page of the head-branch listing, as GitHub answered it."""

    pull_requests: tuple[dict[str, Any], ...]
    status_code: int
    duration_milliseconds: int

    @property
    def ends_the_listing(self) -> bool:
        return len(self.pull_requests) < PULL_REQUESTS_PER_LISTING_PAGE


def _list_head_branch_page(
    client: githubkit.GitHub[githubkit.TokenAuthStrategy],
    repository: GitHubRepository,
    branch: str,
    page: int,
    started: float,
) -> _ListedPullRequestPage | _PullRequestSearchFailed:
    """One page of the pull requests standing on one head branch, in any state.

    `started` is when the whole listing began, so a failure on a later page
    reports how long the caller waited altogether rather than how long its last
    page took.
    """

    try:
        response = client.rest.pulls.list(
            repository.owner,
            repository.name,
            head=f"{repository.owner}:{branch}",
            state="all",
            per_page=PULL_REQUESTS_PER_LISTING_PAGE,
            page=page,
        )
    except githubkit.exception.RequestError as error:
        return _PullRequestSearchFailed(
            _refused_search(error, _elapsed_milliseconds(started))
        )
    raw_response = response.raw_response
    elapsed = _elapsed_milliseconds(started)
    answered = _listed_page(raw_response)
    if answered is None:
        return _PullRequestSearchFailed(
            UnknownOutcomeReason(raw_response.status_code, elapsed, raw_response.text)
        )
    return _ListedPullRequestPage(tuple(answered), raw_response.status_code, elapsed)


def _listing_did_not_end(started: float) -> UnknownOutcomeReason:
    return UnknownOutcomeReason(
        None,
        _elapsed_milliseconds(started),
        "the head branch listing did not end within "
        f"{MAXIMUM_PULL_REQUEST_LISTING_PAGES} pages",
    )


def _github_client(
    token: str, transport: httpx.BaseTransport | None
) -> githubkit.GitHub[githubkit.TokenAuthStrategy]:
    return githubkit.GitHub(
        token,
        transport=transport,
        # A cached read could answer a retry's search from before an earlier
        # crashed attempt's create, which is exactly the twin the
        # readback-then-create rule exists to prevent.
        http_cache=False,
    )


def _body_for(request: OpenPullRequestRequest, request_hash: str) -> str:
    return f"{request.body}\n\n{marker_line(request_hash)}\n"


@dataclass(frozen=True)
class _RenderedOpenPullRequest:
    """The readable title and body an `OpenPullRequest`'s raw report renders to."""

    title: str
    body: str


def _summary_and_changed_paths(raw_body: str) -> tuple[str, tuple[str, ...]]:
    """Read the builder's own summary and changed paths from its raw report.

    `raw_body` is provider output carried verbatim in the request (the
    `issue_to_pr_candidate_report` schema's `summary`/`changed_paths`
    document, when the workflow that produced it declares that shape). A
    request bound to a looser body schema, or an answer that failed its own
    contract, still owes a pull request: its whole text becomes the summary
    and no path list renders, rather than refusing the effect over a report
    this adapter does not own the shape of.
    """

    try:
        decoded = json.loads(raw_body)
    except json.JSONDecodeError:
        return raw_body.strip(), ()
    if not isinstance(decoded, dict):
        return raw_body.strip(), ()
    summary = decoded.get("summary")
    if not isinstance(summary, str) or not summary:
        return raw_body.strip(), ()
    changed_paths = decoded.get("changed_paths")
    if isinstance(changed_paths, list) and all(
        isinstance(path, str) and path for path in changed_paths
    ):
        return summary, tuple(changed_paths)
    return summary, ()


def _rendered_title(summary: str) -> str:
    stripped = summary.strip()
    if not stripped:
        return _DEFAULT_PULL_REQUEST_TITLE
    first_line = stripped.splitlines()[0]
    ending = _SENTENCE_TERMINATOR.search(first_line)
    sentence = first_line[: ending.start() + 1] if ending else first_line
    title = sentence.strip()[:_MAXIMUM_RENDERED_TITLE_CHARACTERS].strip()
    return title or _DEFAULT_PULL_REQUEST_TITLE


def _acceptance_line(head_branch: HeadBranch) -> str:
    # The default exemption every Atelier-opened pull request states until an
    # item with `proves(...)` sentences supplies the real identifiers; the
    # branch name is the one work-item identity already carried this far.
    return (
        f"{ACCEPTANCE_LINE_PREFIX}: none: opened by the Atelier "
        f"from work item {head_branch.value}"
    )


def _bounded_prose(prose: str, tail: str) -> str:
    if len(prose) + len(tail) <= _MAXIMUM_RENDERED_BODY_CHARACTERS:
        return f"{prose}{tail}"
    budget = max(
        0,
        _MAXIMUM_RENDERED_BODY_CHARACTERS
        - len(tail)
        - len(_RENDERED_BODY_TRUNCATION_NOTE),
    )
    return f"{prose[:budget]}{_RENDERED_BODY_TRUNCATION_NOTE}{tail}"


def _rendered_open_pull_request(
    request: OpenPullRequest, request_hash: str
) -> _RenderedOpenPullRequest:
    summary, changed_paths = _summary_and_changed_paths(request.body)
    sections = [summary]
    if changed_paths:
        sections.append(
            "Changed paths:\n" + "\n".join(f"- {path}" for path in changed_paths)
        )
    prose = neutralized_candidate_prose("\n\n".join(sections))
    classification = ""
    if request.work_item_reference is not None:
        issue_number = github_issue_number(request.work_item_reference)
        classification = f"\n\nWork-Item: #{issue_number}\n\nCloses #{issue_number}"
    # The classification lines live in the untruncatable tail, alongside the
    # acceptance line and marker: truncating a long candidate summary must
    # never also drop the lines a queue landing classifies this pull request by.
    tail = (
        f"{classification}\n\n{_acceptance_line(request.head_branch)}"
        f"\n\n{marker_line(request_hash)}\n"
    )
    return _RenderedOpenPullRequest(
        _rendered_title(summary), _bounded_prose(prose, tail)
    )


def _title_and_content_for(
    request: OpenPullRequestRequest, request_hash: str
) -> tuple[str, str]:
    if isinstance(request, ReviewedDocumentationPullRequest):
        return request.title, _body_with_trailer(request.body, request_hash)
    rendered = _rendered_open_pull_request(request, request_hash)
    return rendered.title, rendered.body


def _body_with_trailer(content: str, request_hash: str) -> str:
    return f"{content}\n\n{marker_line(request_hash)}\n"


def _result_payload(branch: str, pr_number: int) -> bytes:
    return json.dumps(
        {"branch": branch, "pr_number": pr_number},
        separators=(",", ":"),
    ).encode("utf-8")


def _string_field(data: dict[str, Any], key: str, context: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise GitHubUnexpectedResponse(
            f"{context} did not carry a {key!r} string field"
        )
    return value


def _integer_field(data: dict[str, Any], key: str, context: str) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise GitHubUnexpectedResponse(
            f"{context} did not carry an integer {key!r} field"
        )
    return value


@dataclass(frozen=True)
class LiveGitHubEffectAdapterFactory:
    """The host-composed factory for one live-GitHub `open-pr` adapter.

    `transport` is a test seam only (ADR 0010 §7's client is `githubkit`,
    which accepts an injectable `httpx` transport): production composition
    leaves it unset and reaches the real network. It is never a durable
    field a lease or receipt copies.
    """

    adapter_revision: AdapterRevision
    destination: EffectDestination
    repository: GitHubRepository
    token_credential: GitHubTokenCredential
    transport: httpx.BaseTransport | None = None
    documentation_publisher_factory: ReviewedDocumentationPublisherFactory | None = None

    @property
    def binding(self) -> EffectAdapterBinding:
        return EffectAdapterBinding(
            self.adapter_revision,
            self.destination,
            AdapterOperationalIdentity(
                f"{self.repository.owner}/{self.repository.name}"
            ),
            AdapterOperationName.OPEN_PR,
        )

    @property
    def proves_absence(self) -> bool:
        # Listing pull requests by their exact head branch is a direct query,
        # not the eventually consistent search index: a `200` with an empty
        # list is GitHub's own answer that this branch carries none (#1210).
        # Only before a send: after one, `ReadbackPhase` keeps it unknown.
        return True

    def open(self) -> LiveGitHubEffectAdapter:
        client = _github_client(self.token_credential.resolve(), self.transport)
        publisher = (
            None
            if self.documentation_publisher_factory is None
            else self.documentation_publisher_factory.open()
        )
        return LiveGitHubEffectAdapter(client, self.repository, self.binding, publisher)


class LiveGitHubEffectAdapter:
    def __init__(
        self,
        client: githubkit.GitHub[githubkit.TokenAuthStrategy],
        repository: GitHubRepository,
        binding: EffectAdapterBinding,
        documentation_publisher: ReviewedDocumentationPublisher | None,
    ) -> None:
        self._client = client
        self._repository = repository
        self._binding = binding
        self._documentation_publisher = documentation_publisher
        self._closed = False

    def readback(self, intent: EffectIntent, phase: ReadbackPhase) -> EffectReadback:
        request = self._authorized_request(intent)
        found = self._find_recorded_pull_request(intent, request)
        if isinstance(found, _PullRequestSearchFailed):
            return EffectUnknownOutcome(intent.reference, found.reason)
        if isinstance(found, _NoPullRequestOnBranch):
            return destination_holds_nothing(
                intent.reference, phase, found.after_send()
            )
        return self._receipt(intent, found)

    def execute(self, intent: EffectIntent) -> PerformedEffect | EffectUnknownOutcome:
        request = self._authorized_request(intent)
        found = self._find_recorded_pull_request(intent, request)
        if isinstance(found, _PullRequestSearchFailed):
            return EffectUnknownOutcome(intent.reference, found.reason)
        if isinstance(found, _RecordedPullRequest):
            return self._performed(found)
        if isinstance(request, ReviewedDocumentationPullRequest):
            self._verify_reviewed_base(request)
            if self._documentation_publisher is None:
                raise GitHubEffectRefused(
                    "reviewed documentation open-pr requires its push publisher"
                )
            self._documentation_publisher.publish(intent, request)
        created = self._create_pull_request(intent, request)
        if isinstance(created, UnknownOutcomeReason):
            return EffectUnknownOutcome(intent.reference, created)
        return self._performed(created)

    def close(self) -> None:
        if self._documentation_publisher is not None:
            self._documentation_publisher.close()
        self._closed = True

    def _authorize_binding(self, intent: EffectIntent) -> None:
        self._require_open()
        if intent.binding.adapter_binding != self._binding:
            raise EffectIntentMismatch(
                "effect intent does not belong to this adapter binding"
            )

    def _authorized_request(self, intent: EffectIntent) -> OpenPullRequestRequest:
        self._authorize_binding(intent)
        return open_pull_request(intent.request)

    def _verify_reviewed_base(self, request: ReviewedDocumentationPullRequest) -> None:
        response = self._client.rest.repos.get_branch(
            self._repository.owner,
            self._repository.name,
            self._repository.base_branch,
        )
        branch = response.raw_response.json()
        if not isinstance(branch, dict):
            raise GitHubUnexpectedResponse(
                "base branch read did not return a branch object"
            )
        commit = branch.get("commit")
        if not isinstance(commit, dict):
            raise GitHubUnexpectedResponse(
                "base branch read did not return a commit object"
            )
        if _string_field(commit, "sha", "base branch commit") != request.base_revision:
            raise GitHubEffectRefused(
                "reviewed documentation base differs from the connected base branch"
            )

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("github live effect adapter is closed")

    def _find_recorded_pull_request(
        self, intent: EffectIntent, request: OpenPullRequestRequest
    ) -> _PullRequestSearch:
        """Which pull request on this head branch carries this request's marker.

        The branch answers about itself as a whole: several pull requests can
        stand on one head with different bases, so the marker decides which of
        them is this request's, and a listing that names others but not this
        one is no absence -- it is a state this adapter may not act on. Each
        page is examined for the marker as it arrives, so a marker already
        proven on an earlier page is returned at once: a later page's failure
        never discards it. Only a failure reached before the marker is found
        leaves the outcome unknown.
        """

        branch = request.head_branch.value
        request_hash = intent.request.request_hash.value
        started = time.monotonic()
        listed_any = False
        for page_number in range(1, MAXIMUM_PULL_REQUEST_LISTING_PAGES + 1):
            page = _list_head_branch_page(
                self._client, self._repository, branch, page_number, started
            )
            if isinstance(page, _PullRequestSearchFailed):
                return page
            listed_any = listed_any or bool(page.pull_requests)
            for pull_request in page.pull_requests:
                body = pull_request.get("body")
                body = body if isinstance(body, str) else ""
                if body_carries_request_hash(body, request_hash):
                    number = _integer_field(
                        pull_request, "number", "pull request search result"
                    )
                    return _RecordedPullRequest(branch, number, body)
            if page.ends_the_listing:
                if not listed_any:
                    return _NoPullRequestOnBranch(
                        page.status_code, page.duration_milliseconds
                    )
                return _PullRequestSearchFailed(
                    UnknownOutcomeReason(
                        page.status_code,
                        page.duration_milliseconds,
                        "the head branch carries pull requests, "
                        "none of them this request's",
                    )
                )
        return _PullRequestSearchFailed(_listing_did_not_end(started))

    def _create_pull_request(
        self, intent: EffectIntent, request: OpenPullRequestRequest
    ) -> _RecordedPullRequest | UnknownOutcomeReason:
        branch = request.head_branch.value
        title, body = _title_and_content_for(request, intent.request.request_hash.value)
        create_body: ReposOwnerRepoPullsPostBodyType = {
            "title": title,
            "head": branch,
            "base": self._repository.base_branch,
            "body": body,
        }
        if isinstance(request, ReviewedDocumentationPullRequest):
            create_body["draft"] = request.draft
        started = time.monotonic()
        try:
            response = self._client.rest.pulls.create(
                self._repository.owner,
                self._repository.name,
                data=create_body,
            )
        except githubkit.exception.RequestFailed as error:
            if error.response.status_code != _PULL_REQUEST_ALREADY_EXISTS_STATUS:
                raise
            # A concurrent execute created the pull request between this
            # attempt's search and this create; the same marker search
            # converges on its result rather than this attempt creating a twin
            # GitHub's own constraint would have refused anyway. A listing that
            # still does not name the winner leaves this attempt's own outcome
            # unknown -- the create was sent, so its result is a reconciliation
            # for the operator, never an exception thrown over a sent request.
            found = self._find_recorded_pull_request(intent, request)
            if isinstance(found, _RecordedPullRequest):
                return found
            return _refused_search(error, _elapsed_milliseconds(started))
        created = response.raw_response.json()
        if not isinstance(created, dict):
            raise GitHubUnexpectedResponse(
                "pull request creation did not return a pull request object"
            )
        number = _integer_field(created, "number", "pull request creation result")
        return _RecordedPullRequest(branch, number, body)

    def _performed(self, record: _RecordedPullRequest) -> PerformedEffect:
        return PerformedEffect(
            EffectId(str(record.pr_number)),
            EffectResult(_result_payload(record.branch, record.pr_number)),
        )

    def _receipt(
        self, intent: EffectIntent, record: _RecordedPullRequest
    ) -> EffectReceipt:
        return EffectReceipt(
            intent,
            EffectId(str(record.pr_number)),
            EffectResult(_result_payload(record.branch, record.pr_number)),
            ConfirmationSource.ADAPTER_READBACK,
        )


@dataclass(frozen=True)
class LiveGitHubHeadBranchPullRequests:
    """Which pull requests still stand open on one head branch, live from GitHub.

    The same head-branch listing the `open-pr` adapter reads its own marker out
    of, asked the other question a publisher has about a branch: not which pull
    request is this request's, but whether anyone is still reviewing what the
    branch carries. Nothing here is decided about the branch -- the caller owns
    that -- and an answer this module cannot read is unreadable rather than
    quietly counted as nobody reviewing.

    The token is resolved per question rather than held (ADR 0009 §6's
    by-reference discipline, and the question is asked only about a branch that
    already stands at a foreign commit).
    """

    repository: GitHubRepository
    token_credential: GitHubTokenCredential
    transport: httpx.BaseTransport | None = None

    def open_pull_requests_on(
        self, head_branch: HeadBranch
    ) -> HeadBranchPullRequestState:
        client = _github_client(self.token_credential.resolve(), self.transport)
        started = time.monotonic()
        for page_number in range(1, MAXIMUM_PULL_REQUEST_LISTING_PAGES + 1):
            page = _list_head_branch_page(
                client, self.repository, head_branch.value, page_number, started
            )
            if isinstance(page, _PullRequestSearchFailed):
                return HeadBranchPullRequestsUnreadable(page.reason)
            for pull_request in page.pull_requests:
                state = pull_request.get("state")
                if state == _OPEN_PULL_REQUEST_STATE:
                    number = pull_request.get("number")
                    if not isinstance(number, int) or isinstance(number, bool):
                        return HeadBranchPullRequestsUnreadable(
                            UnknownOutcomeReason(
                                page.status_code,
                                page.duration_milliseconds,
                                "an open pull request on this head branch names "
                                f"no integer 'number' field: {number!r}",
                            )
                        )
                    return PullRequestOpenOnHeadBranch(number)
                if state != _CLOSED_PULL_REQUEST_STATE:
                    return HeadBranchPullRequestsUnreadable(
                        UnknownOutcomeReason(
                            page.status_code,
                            page.duration_milliseconds,
                            "a pull request on this head branch names no state "
                            f"this adapter reads: {state!r}",
                        )
                    )
            if page.ends_the_listing:
                return NoPullRequestOpenOnHeadBranch()
        return HeadBranchPullRequestsUnreadable(_listing_did_not_end(started))
