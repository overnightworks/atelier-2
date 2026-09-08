"""Publication and readback for the `open-pr` adapter operation.

The factory is an `EffectAdapterFactory`. This slice's destination is a
recorded fake platform: it can list every pull request it ever created, so a
missing marker is an authoritative absence rather than an unmatched search.
Live GitHub cannot prove that negative; that path is not composed here.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from atelier2.contracts.adapter_operations_v3 import AdapterOperationName
from atelier2.contracts.effect_markers import body_carries_request_hash, marker_line
from atelier2.contracts.effect_requests import (
    OpenPullRequest,
    ReviewedDocumentationPullRequest,
    ReviewedDocumentReplacement,
)
from atelier2.contracts.effects import (
    AdapterOperationalIdentity,
    AdapterRevision,
    CanonicalRequest,
    ConfirmationSource,
    EffectAbsence,
    EffectAdapterBinding,
    EffectDestination,
    EffectId,
    EffectIntent,
    EffectIntentMismatch,
    EffectReceipt,
    EffectResult,
    EffectUnknownOutcome,
    PerformedEffect,
    ReadbackPhase,
    destination_holds_nothing,
)

_SQLITE_LOCK_TIMEOUT_SECONDS = 30.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pull_requests(
  request_hash TEXT PRIMARY KEY,
  branch TEXT NOT NULL,
  pr_number INTEGER NOT NULL UNIQUE,
  body TEXT NOT NULL,
  effect_id TEXT UNIQUE NOT NULL,
  result BLOB NOT NULL,
  result_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pull_request_creates(
  request_hash TEXT PRIMARY KEY,
  creates INTEGER NOT NULL CHECK(creates > 0)
);
CREATE TABLE IF NOT EXISTS documentation_pushes(
  request_hash TEXT PRIMARY KEY,
  branch TEXT NOT NULL,
  base_revision TEXT NOT NULL,
  replacements BLOB NOT NULL
);
"""


@dataclass(frozen=True)
class RecordedPullRequest:
    """One pull request the fake platform recorded, as the adapter names it."""

    branch: str
    pr_number: int
    body: str
    request_hash: str


@dataclass(frozen=True)
class RecordedDocumentationPush:
    branch: str
    base_revision: str
    replacements: tuple[ReviewedDocumentReplacement, ...]
    request_hash: str


class GitHubEffectRefused(RuntimeError):
    """The durable request cannot be performed by the GitHub adapter."""


class ReviewedDocumentationPublisher(Protocol):
    def publish(
        self, intent: EffectIntent, request: ReviewedDocumentationPullRequest
    ) -> None: ...

    def close(self) -> None: ...


class ReviewedDocumentationPublisherFactory(Protocol):
    def open(self) -> ReviewedDocumentationPublisher: ...


type OpenPullRequestRequest = OpenPullRequest | ReviewedDocumentationPullRequest


def open_pull_request(request: CanonicalRequest) -> OpenPullRequestRequest:
    try:
        return OpenPullRequest.from_canonical_bytes(request.payload)
    except (TypeError, ValueError):
        try:
            return ReviewedDocumentationPullRequest.from_canonical_bytes(
                request.payload
            )
        except (TypeError, ValueError) as reviewed_error:
            raise GitHubEffectRefused(
                "open-pr effect requires one canonical open-pr request"
            ) from reviewed_error


def _result_payload(branch: str, pr_number: int) -> bytes:
    return json.dumps(
        {"branch": branch, "pr_number": pr_number},
        separators=(",", ":"),
    ).encode("utf-8")


def _row(record: Any) -> tuple[str, int, str, str, bytes, str] | None:
    if record is None:
        return None
    return (
        str(record[0]),
        int(record[1]),
        str(record[2]),
        str(record[3]),
        bytes(record[4]),
        str(record[5]),
    )


def _body_for(request: OpenPullRequestRequest, request_hash: str) -> str:
    return f"{request.body}\n\n{marker_line(request_hash)}\n"


class _RecordedDocumentationPublisher:
    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path

    def publish(
        self, intent: EffectIntent, request: ReviewedDocumentationPullRequest
    ) -> None:
        replacements = json.dumps(
            [entry.as_json() for entry in request.replacements],
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        with (
            closing(
                sqlite3.connect(
                    self._database_path, timeout=_SQLITE_LOCK_TIMEOUT_SECONDS
                )
            ) as connection,
            connection,
        ):
            standing = connection.execute(
                "SELECT branch, base_revision, replacements "
                "FROM documentation_pushes WHERE request_hash=?",
                (intent.request.request_hash.value,),
            ).fetchone()
            expected = (request.head_branch.value, request.base_revision, replacements)
            if standing is not None:
                recorded = (str(standing[0]), str(standing[1]), bytes(standing[2]))
                if recorded != expected:
                    raise EffectIntentMismatch(
                        "recorded documentation push differs from its exact request"
                    )
                return
            connection.execute(
                "INSERT INTO documentation_pushes VALUES(?, ?, ?, ?)",
                (intent.request.request_hash.value, *expected),
            )

    def close(self) -> None:
        # Connections open per publish; this publisher holds nothing to release.
        return


@dataclass(frozen=True)
class RecordedDocumentationPublisherFactory:
    database_path: Path

    def open(self) -> ReviewedDocumentationPublisher:
        return _RecordedDocumentationPublisher(self.database_path.resolve())


@dataclass(frozen=True)
class GitHubEffectAdapterFactory:
    database_path: Path
    adapter_revision: AdapterRevision
    destination: EffectDestination
    documentation_publisher_factory: ReviewedDocumentationPublisherFactory | None = None

    @property
    def binding(self) -> EffectAdapterBinding:
        return EffectAdapterBinding(
            self.adapter_revision,
            self.destination,
            AdapterOperationalIdentity(str(self.database_path.resolve())),
            AdapterOperationName.OPEN_PR,
        )

    @property
    def proves_absence(self) -> bool:
        # The fake platform lists every pull request it ever created, so a
        # missing marker before a send is an authoritative absence (ADR 0010
        # §5). After one, no destination's silence proves anything (#1210).
        return True

    def open(self) -> GitHubEffectAdapter:
        database_path = self.database_path.resolve()
        database_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(database_path)) as connection, connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(_SCHEMA)
        publisher = (
            RecordedDocumentationPublisherFactory(database_path).open()
            if self.documentation_publisher_factory is None
            else self.documentation_publisher_factory.open()
        )
        return GitHubEffectAdapter(database_path, self.binding, publisher)

    def recorded_pull_requests(self) -> tuple[RecordedPullRequest, ...]:
        """Every pull request this fake has created, in number order."""
        database_path = self.database_path.resolve()
        if not database_path.is_file():
            return ()
        with closing(
            sqlite3.connect(database_path, timeout=_SQLITE_LOCK_TIMEOUT_SECONDS)
        ) as connection:
            rows = connection.execute(
                "SELECT branch, pr_number, body, request_hash "
                "FROM pull_requests ORDER BY pr_number"
            ).fetchall()
        return tuple(
            RecordedPullRequest(str(row[0]), int(row[1]), str(row[2]), str(row[3]))
            for row in rows
        )

    def recorded_documentation_pushes(self) -> tuple[RecordedDocumentationPush, ...]:
        database_path = self.database_path.resolve()
        if not database_path.is_file():
            return ()
        with closing(
            sqlite3.connect(database_path, timeout=_SQLITE_LOCK_TIMEOUT_SECONDS)
        ) as connection:
            rows = connection.execute(
                "SELECT branch, base_revision, replacements, request_hash "
                "FROM documentation_pushes ORDER BY request_hash"
            ).fetchall()
        return tuple(
            RecordedDocumentationPush(
                str(row[0]),
                str(row[1]),
                tuple(
                    ReviewedDocumentReplacement.from_json(entry)
                    for entry in json.loads(bytes(row[2]))
                ),
                str(row[3]),
            )
            for row in rows
        )


class GitHubEffectAdapter:
    def __init__(
        self,
        database_path: Path,
        binding: EffectAdapterBinding,
        documentation_publisher: ReviewedDocumentationPublisher,
    ) -> None:
        self._database_path = database_path
        self._binding = binding
        self._documentation_publisher = documentation_publisher
        self._closed = False

    def readback(
        self, intent: EffectIntent, phase: ReadbackPhase
    ) -> EffectReceipt | EffectAbsence | EffectUnknownOutcome:
        self._authorize_binding(intent)
        open_pull_request(intent.request)
        record = self._load(intent.request.request_hash.value)
        if record is None:
            # Nothing failed and nothing was measured here, so the phase alone
            # decides, and there is nothing this store could quote as a reason.
            return destination_holds_nothing(intent.reference, phase, None)
        return self._receipt(intent, record)

    def execute(self, intent: EffectIntent) -> PerformedEffect:
        self._authorize_binding(intent)
        request = open_pull_request(intent.request)
        request_hash = intent.request.request_hash.value
        existing = self._load(request_hash)
        if existing is not None:
            self._verify_recorded_request(intent, existing)
            return PerformedEffect(
                EffectId(existing[3]),
                EffectResult.from_durable_record(
                    existing[4], EffectResult.payload_hash_type(existing[5])
                ),
            )
        if isinstance(request, ReviewedDocumentationPullRequest):
            self._documentation_publisher.publish(intent, request)
        with (
            closing(
                sqlite3.connect(
                    self._database_path,
                    timeout=_SQLITE_LOCK_TIMEOUT_SECONDS,
                    isolation_level=None,
                )
            ) as connection,
            connection,
        ):
            connection.execute("PRAGMA busy_timeout=30000")
            connection.execute("BEGIN IMMEDIATE")
            record = _row(
                connection.execute(
                    "SELECT branch, pr_number, body, effect_id, result, result_hash "
                    "FROM pull_requests WHERE request_hash=?",
                    (request_hash,),
                ).fetchone()
            )
            if record is not None:
                self._verify_recorded_request(intent, record)
                connection.commit()
                return PerformedEffect(
                    EffectId(record[3]),
                    EffectResult.from_durable_record(
                        record[4], EffectResult.payload_hash_type(record[5])
                    ),
                )
            pr_number = self._next_pr_number(connection)
            branch = request.head_branch.value
            body = _body_for(request, request_hash)
            result = EffectResult(_result_payload(branch, pr_number))
            effect_id = EffectId(str(pr_number))
            connection.execute(
                "INSERT INTO pull_requests VALUES(?, ?, ?, ?, ?, ?, ?)",
                (
                    request_hash,
                    branch,
                    pr_number,
                    body,
                    effect_id.value,
                    result.payload,
                    result.payload_hash.value,
                ),
            )
            connection.execute(
                "INSERT INTO pull_request_creates VALUES(?, 1)",
                (request_hash,),
            )
            connection.commit()
            return PerformedEffect(effect_id, result)

    def close(self) -> None:
        self._documentation_publisher.close()
        self._closed = True

    def _authorize_binding(self, intent: EffectIntent) -> None:
        self._require_open()
        if intent.binding.adapter_binding != self._binding:
            raise EffectIntentMismatch(
                "effect intent does not belong to this adapter binding"
            )

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("github effect adapter is closed")

    def _load(self, request_hash: str) -> tuple[str, int, str, str, bytes, str] | None:
        with (
            closing(
                sqlite3.connect(
                    self._database_path, timeout=_SQLITE_LOCK_TIMEOUT_SECONDS
                )
            ) as connection,
            connection,
        ):
            return _row(
                connection.execute(
                    "SELECT branch, pr_number, body, effect_id, result, result_hash "
                    "FROM pull_requests WHERE request_hash=?",
                    (request_hash,),
                ).fetchone()
            )

    def _receipt(
        self,
        intent: EffectIntent,
        record: tuple[str, int, str, str, bytes, str],
    ) -> EffectReceipt:
        self._verify_recorded_request(intent, record)
        result = EffectResult.from_durable_record(
            record[4], EffectResult.payload_hash_type(record[5])
        )
        return EffectReceipt(
            intent,
            EffectId(record[3]),
            result,
            ConfirmationSource.ADAPTER_READBACK,
        )

    @staticmethod
    def _verify_recorded_request(
        intent: EffectIntent, record: tuple[str, int, str, str, bytes, str]
    ) -> None:
        body = str(record[2])
        request_hash = intent.request.request_hash.value
        if not body_carries_request_hash(body, request_hash):
            raise EffectIntentMismatch(
                "recorded pull request does not carry this request's marker"
            )

    @staticmethod
    def _next_pr_number(connection: sqlite3.Connection) -> int:
        highest = connection.execute(
            "SELECT COALESCE(MAX(pr_number), 0) FROM pull_requests"
        ).fetchone()
        return int(highest[0]) + 1
