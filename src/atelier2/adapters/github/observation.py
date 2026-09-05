"""Reading the connected repository: which issues are open, and what one says.

The thin observation surface of #652: list the open issues of the repository
the project-source connection record names, and answer them in the reference
grammar this package owns -- `gh:<n>`, the same spelling the queue's admission
door already receives (ADR 0010: what a tracker reference means is the
connected platform adapter's contract). Nothing else of the listing crosses
this boundary; a title and the labels an item was listed with cross as
observations for the caller to decide what to do with, each dated by the one
instant this source finished reading the whole open listing, while an item's
own lifecycle stays GitHub's (REQ-QUEUE-14). GitHub's state field remains
deliberately unread; the importer derives closedness from the open-set
difference (ADR 0016, 2026-09-01 amendment).

Reading one named item is the port's second operation (ADR 0010 decision 1,
2026-08-26 amendment): it answers the observed revision of ADR 0010 §5 -- the
exact bytes GitHub served as that item's body, hashed as they are, with the
item's identity and the read's entity tag as provenance. Bytes cross here
because a run must be able to prove which text it read; the platform's own
words for the item do not, so a pull request answers as the neutral
`change_request` kind and never as a GitHub noun.

Provider output is external input: every payload shape this module reads is
validated, and a shape it refuses becomes a typed `TrackerPayloadMalformed`
rather than an unvalidated answer on its way to a durable write. The token
reaches GitHub by credential-directory reference exactly as the live `open-pr`
adapter's does (ADR 0009 §6) and lives nowhere durable afterward.

A durable cursor, conditional reads, and a poll loop are named deferrals of
#652 slice 1; every observation reads the full open list.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import githubkit
import githubkit.exception
import httpx

from atelier2.adapters.github.live_effects import (
    GitHubCredentialUnresolvable,
    GitHubRepository,
    GitHubTokenCredential,
)
from atelier2.adapters.github.tracker_reference import (
    github_issue_number_or_none as _issue_number,
)
from atelier2.adapters.github.tracker_reference import (
    github_tracker_reference,
)
from atelier2.contracts.queue_projection import TrackerItemReference
from atelier2.contracts.when import RecordedAt, recorded_instant
from atelier2.contracts.work_items import (
    MAXIMUM_WORK_ITEM_CHANGE_MARKER_CHARACTERS,
    ObservedWorkItemRevision,
    WorkItemChangeMarker,
    WorkItemKind,
)
from atelier2.ports.issue_observation import (
    ObservedOpenTrackerItem,
    ObserveOpenTrackerItemsResult,
    ObserveWorkItemRevisionResult,
    OpenTrackerItemsObserved,
    TrackerItemUnknown,
    TrackerPayloadMalformed,
    TrackerSourceUnavailable,
    WorkItemRevisionObserved,
)

# GitHub's own maximum page size; fewer round trips per observation, and the
# last short page is what ends the walk.
_ISSUES_PAGE_SIZE = 100

# GitHub marks a pull request inside the issue representation with this key,
# and it is the only thing that tells the two kinds apart there.
_PULL_REQUEST_KEY = "pull_request"


@dataclass(frozen=True)
class _DecodedPayload:
    """What one platform answer decoded to, when it decoded at all."""

    value: Any


def _decoded_payload(
    response: httpx.Response,
) -> _DecodedPayload | TrackerPayloadMalformed:
    """The JSON one answer carries, or the typed refusal of an answer that is none.

    A provider answer is external input all the way down to its encoding: bytes
    that are not UTF-8 and text that is not JSON are shapes this source refuses,
    not exceptions for a caller of a typed port to discover.
    """

    try:
        return _DecodedPayload(response.json())
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        return TrackerPayloadMalformed(f"the platform answer is not JSON: {error}")


def _label_names(payload: Any) -> tuple[str, ...] | None:
    """The label names this listing served, or `None` for a shape to refuse.

    GitHub spells a label either as its bare name or as an object carrying
    `name`; both say the same thing, and a listed label the payload spells
    some third way is refused rather than guessed at. An issue with no
    `labels` at all carries no label: the key is absent only where nothing
    named the item, and a missing label can authorize nothing (REQ-QUEUE-08),
    so reading it as the empty set withholds authority rather than inventing
    it.
    """

    if payload is None:
        return ()
    if not isinstance(payload, list):
        return None
    names: list[str] = []
    for entry in payload:
        if isinstance(entry, str):
            names.append(entry)
            continue
        name = entry.get("name") if isinstance(entry, dict) else None
        if not isinstance(name, str):
            return None
        names.append(name)
    return tuple(names)


@dataclass(frozen=True)
class LiveGitHubIssueSource:
    """The connected repository, read through `githubkit`: the list, and one item.

    `transport` is a test seam only, exactly as on
    `LiveGitHubEffectAdapterFactory`, and `clock` is the read-time seam a
    snapshot's provenance needs: production composition leaves both unset, so
    it reaches the real network and the real clock.
    """

    repository: GitHubRepository
    token_credential: GitHubTokenCredential
    transport: httpx.BaseTransport | None = None
    clock: Callable[[], RecordedAt] = recorded_instant

    def open_items(self) -> ObserveOpenTrackerItemsResult:
        token = self._resolved_token()
        if isinstance(token, TrackerSourceUnavailable):
            return token
        client = self._client(token)
        items: list[ObservedOpenTrackerItem] = []
        page = 1
        while True:
            try:
                response = client.rest.issues.list_for_repo(
                    self.repository.owner,
                    self.repository.name,
                    state="open",
                    per_page=_ISSUES_PAGE_SIZE,
                    page=page,
                )
            except githubkit.exception.RequestFailed as error:
                return TrackerSourceUnavailable(
                    "GitHub answered "
                    f"{error.response.status_code} listing open issues of "
                    f"{self.repository.owner}/{self.repository.name}"
                )
            except githubkit.exception.RequestError as error:
                # githubkit wraps every transport and timeout failure in this
                # type before httpx's own can surface.
                return TrackerSourceUnavailable(
                    "GitHub could not be reached listing open issues of "
                    f"{self.repository.owner}/{self.repository.name}: {error}"
                )
            decoded = _decoded_payload(response.raw_response)
            if isinstance(decoded, TrackerPayloadMalformed):
                return decoded
            payload = decoded.value
            malformed = self._collect_open_items(payload, items)
            if malformed is not None:
                return malformed
            if len(payload) < _ISSUES_PAGE_SIZE:
                # One instant for the whole walk, taken once it is known to
                # have completed: every title in this answer was read as part
                # of the one listing this instant marks, not one clock read
                # per page or per item.
                return OpenTrackerItemsObserved(tuple(items), self.clock())
            page += 1

    def snapshot(
        self, reference: TrackerItemReference
    ) -> ObserveWorkItemRevisionResult:
        number = _issue_number(reference)
        if number is None:
            return TrackerItemUnknown(reference)
        token = self._resolved_token()
        if isinstance(token, TrackerSourceUnavailable):
            return token
        # The client is held in a name of its own: `githubkit`'s namespaces
        # keep only a weak reference back to it, so a client that lives no
        # longer than the expression is collected mid-call.
        client = self._client(token)
        try:
            response = client.rest.issues.get(
                self.repository.owner, self.repository.name, number
            )
        except githubkit.exception.RequestFailed as error:
            if error.response.status_code == httpx.codes.NOT_FOUND:
                return TrackerItemUnknown(reference)
            return TrackerSourceUnavailable(
                f"GitHub answered {error.response.status_code} reading "
                f"{self.repository.owner}/{self.repository.name} item {number}"
            )
        except githubkit.exception.RequestError as error:
            # githubkit wraps every transport and timeout failure in this
            # type before httpx's own can surface.
            return TrackerSourceUnavailable(
                "GitHub could not be reached reading "
                f"{self.repository.owner}/{self.repository.name} item "
                f"{number}: {error}"
            )
        payload = _decoded_payload(response.raw_response)
        if isinstance(payload, TrackerPayloadMalformed):
            return payload
        return self._observed_revision(
            reference, number, payload.value, response.raw_response.headers
        )

    def _observed_revision(
        self,
        reference: TrackerItemReference,
        number: int,
        payload: Any,
        headers: httpx.Headers,
    ) -> ObserveWorkItemRevisionResult:
        if not isinstance(payload, dict):
            return TrackerPayloadMalformed("the item read was not an item object")
        # An answer about another item would pin one item's bytes under another
        # item's reference, so the identity the platform states is read rather
        # than assumed from the request. `type(...) is int` rather than
        # `isinstance`, because `True == 1` and `1.0 == 1` in Python and
        # neither is an item number GitHub served.
        answered = payload.get("number")
        if type(answered) is not int or answered != number:
            return TrackerPayloadMalformed(
                f"the item read answered for another item than {number}"
            )
        if "body" not in payload:
            return TrackerPayloadMalformed("the item read carried no body field")
        body = payload["body"]
        if body is not None and not isinstance(body, str):
            return TrackerPayloadMalformed("an item body was neither text nor absent")
        entity_tag = headers.get("etag", "")
        if not 1 <= len(entity_tag) <= MAXIMUM_WORK_ITEM_CHANGE_MARKER_CHARACTERS:
            # Without the platform's own marker the read cannot say which
            # state it saw, and inventing one would fake ADR 0010 §5's
            # provenance rather than answer it.
            return TrackerPayloadMalformed(
                "the item read carried no usable entity tag to pin it"
            )
        try:
            # GitHub serves an item with no text as a null body, which is an
            # empty read rather than a missing one. JSON text can also carry an
            # escaped lone surrogate, which is not encodable as UTF-8 -- a
            # payload this source refuses rather than a crash on its way out.
            served_bytes = ("" if body is None else body).encode("utf-8")
        except UnicodeEncodeError:
            return TrackerPayloadMalformed(
                "an item body carried text that is not encodable as UTF-8"
            )
        kind = (
            WorkItemKind.CHANGE_REQUEST
            if _PULL_REQUEST_KEY in payload
            else WorkItemKind.ISSUE
        )
        return WorkItemRevisionObserved(
            ObservedWorkItemRevision(
                reference,
                kind,
                served_bytes,
                WorkItemChangeMarker(entity_tag),
                self.clock(),
            )
        )

    def _resolved_token(self) -> str | TrackerSourceUnavailable:
        try:
            return self.token_credential.resolve()
        except GitHubCredentialUnresolvable as error:
            return TrackerSourceUnavailable(str(error))

    def _client(self, token: str) -> githubkit.GitHub[githubkit.TokenAuthStrategy]:
        return githubkit.GitHub(
            token,
            transport=self.transport,
            # A cached answer could carry a state from before the operator's
            # newest edit -- for a listing a missing issue, for a snapshot
            # bytes the platform no longer serves. The conditional-read design
            # that would make caching honest is a named deferral of slice 1.
            http_cache=False,
        )

    def _collect_open_items(
        self, payload: Any, items: list[ObservedOpenTrackerItem]
    ) -> TrackerPayloadMalformed | None:
        if not isinstance(payload, list):
            return TrackerPayloadMalformed(
                "the open-issue listing was not a list of issues"
            )
        for entry in payload:
            if not isinstance(entry, dict):
                return TrackerPayloadMalformed(
                    "the open-issue listing carried an entry that is not an object"
                )
            # GitHub's issue listing carries pull requests too, marked by this
            # key; a pull request is not a work item this queue observes.
            if _PULL_REQUEST_KEY in entry:
                continue
            number = entry.get("number")
            if not isinstance(number, int) or isinstance(number, bool) or number < 1:
                return TrackerPayloadMalformed(
                    "an open issue carried no positive integer number"
                )
            title = entry.get("title")
            if not isinstance(title, str):
                return TrackerPayloadMalformed("an open issue carried no text title")
            labels = _label_names(entry.get("labels"))
            if labels is None:
                return TrackerPayloadMalformed(
                    "an open issue carried labels this source cannot read names from"
                )
            items.append(
                ObservedOpenTrackerItem(github_tracker_reference(number), title, labels)
            )
        return None
