from __future__ import annotations

import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DatabaseError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from atelier2.adapters.dbos.schema import (
    catalog_intakes,
    catalog_lineage_aliases,
    catalog_lineage_members,
    catalog_lineage_retirements,
    catalog_lineages,
    published_revisions,
    workflow_revisions,
)
from atelier2.adapters.dbos.transactions import canonical_write_transaction
from atelier2.contracts.catalog_intakes import (
    CatalogIntake,
    CatalogIntakeId,
    CatalogIntakeKind,
)
from atelier2.contracts.catalog_v3 import (
    CatalogActivatedAt,
    CatalogActor,
    CatalogLineage,
    CatalogLineageDisplayName,
    CatalogLineageId,
    CatalogRetirementState,
)
from atelier2.contracts.pages import require_page_limit
from atelier2.contracts.revisions_v3 import (
    PublishedRevision,
    PublishedRevisionHash,
    RevisionKind,
)
from atelier2.contracts.runs import WorkflowRevision
from atelier2.ports.catalog_intakes import (
    CatalogIntakeExisting,
    CatalogIntakeFound,
    CatalogIntakeMissing,
    CatalogIntakeStored,
    ReadCatalogIntakeResult,
    StoreCatalogIntakeResult,
)
from atelier2.ports.durable_runs import DurableStateCorrupt, DurableWriteUnavailable
from atelier2.ports.published_revisions import (
    AddWorkflowToLibraryResult,
    AdmitCatalogMemberResult,
    CatalogAdmissionExisting,
    CatalogAdmissionKindMismatch,
    CatalogAdmissionLineageMissing,
    CatalogAdmissionNameHeld,
    CatalogAdmissionRetired,
    CatalogAdmissionRevisionOwned,
    CatalogAdmissionUnpublished,
    CatalogLineageFounded,
    CatalogLineageIdMismatch,
    CatalogLineageQuery,
    CatalogLineageRetired,
    CatalogMemberAdmitted,
    CatalogNameFound,
    CatalogNameMissing,
    CatalogRetirementExisting,
    CatalogRevisionPosition,
    FoundCatalogLineageResult,
    ListPublishedRevisionsResult,
    PublishedRevisionCollision,
    PublishedRevisionCreated,
    PublishedRevisionExisting,
    PublishedRevisionFound,
    PublishedRevisionMissing,
    PublishedRevisionPage,
    PublishedRevisionResolver,
    PublishedRevisionsUnavailable,
    PublishRevisionResult,
    ResolveCatalogNameResult,
    ResolvePublishedRevisionResult,
    RetireCatalogLineageResult,
)


def published_revision_from_record(record: Mapping[Any, Any]) -> PublishedRevision:
    revision = PublishedRevision(
        RevisionKind(str(record["kind"])),
        bytes(record["document"]),
    )
    if revision.revision_hash.value != record["revision_hash"]:
        raise ValueError("durable published revision hash disagrees with its bytes")
    return revision


def catalog_lineage_from_record(record: Mapping[Any, Any]) -> CatalogLineage:
    lineage = CatalogLineage(
        RevisionKind(str(record["kind"])),
        PublishedRevisionHash(str(record["founding_revision_hash"])),
    )
    if lineage.lineage_id.value != record["lineage_id"]:
        raise ValueError(
            "durable catalog lineage id disagrees with its founding identity"
        )
    return lineage


def _next_activation_number(
    connection: sa.Connection, table: sa.Table, lineage_id: str
) -> int:
    current = connection.scalar(
        sa.select(sa.func.max(table.c.activation_number)).where(
            table.c.lineage_id == lineage_id
        )
    )
    return 1 if current is None else int(current) + 1


def _intake_from_record(record: Mapping[Any, Any]) -> CatalogIntake:
    intake = CatalogIntake(
        CatalogIntakeKind(str(record["kind"])),
        bytes(record["document"]),
        CatalogActor(str(record["actor"])),
        CatalogActivatedAt(str(record["activated_at"])),
    )
    if intake.intake_id.value != record["intake_id"]:
        raise ValueError(
            "durable catalog intake identity disagrees with its declared kind and bytes"
        )
    return intake


def _published(
    connection: sa.Connection, revision: PublishedRevision
) -> PublishedRevision | None:
    return _select_published_revision(connection, revision.kind, revision.revision_hash)


def _select_published_revision(
    connection: sa.Connection, kind: RevisionKind, revision_hash: PublishedRevisionHash
) -> PublishedRevision | None:
    """The one lookup `resolve` answers, on a connection its caller already opened.

    Kept apart from `resolve` itself so a composed read that must not open a
    connection per lookup (`DbosCatalogStore.resolver_session`) can share this
    exact query on the one connection it holds for the whole read.
    """
    record = (
        connection.execute(
            sa.select(published_revisions).where(
                published_revisions.c.kind == kind.value,
                published_revisions.c.revision_hash == revision_hash.value,
            )
        )
        .mappings()
        .one_or_none()
    )
    if record is None:
        return None
    return published_revision_from_record(record)


def _workflow_publication(
    connection: sa.Connection, revision: PublishedRevision
) -> PublishedRevision | None:
    if revision.kind is not RevisionKind.WORKFLOW:
        return None
    record = (
        connection.execute(
            sa.select(workflow_revisions).where(
                workflow_revisions.c.revision_hash == revision.revision_hash.value
            )
        )
        .mappings()
        .one_or_none()
    )
    if record is None:
        return None
    stored = PublishedRevision(RevisionKind.WORKFLOW, bytes(record["document"]))
    if stored.revision_hash.value != record["revision_hash"]:
        raise ValueError("durable workflow revision hash disagrees with its bytes")
    if stored != revision:
        raise ValueError("handed workflow revision disagrees with its published bytes")
    return stored


def _record_publication(
    connection: sa.Connection, revision: PublishedRevision
) -> PublishedRevision | None:
    """The catalog FK needs published_revisions; reuse the existing publication.

    A workflow already published through workflow_revisions keeps that hash.
    This writes the same bytes under the same hash; it is not a second identity.
    """

    existing = _published(connection, revision)
    if existing is not None:
        if existing != revision:
            raise ValueError("handed revision disagrees with catalog published bytes")
        return existing
    workflow = _workflow_publication(connection, revision)
    if workflow is None:
        return None
    connection.execute(
        published_revisions.insert().values(
            kind=workflow.kind.value,
            revision_hash=workflow.revision_hash.value,
            document=workflow.document,
        )
    )
    return workflow


def _lineage_record(
    connection: sa.Connection, lineage_id: CatalogLineageId
) -> Mapping[Any, Any] | None:
    return (
        connection.execute(
            sa.select(catalog_lineages).where(
                catalog_lineages.c.lineage_id == lineage_id.value
            )
        )
        .mappings()
        .one_or_none()
    )


def _derived_lineage_or_mismatch(
    record: Mapping[Any, Any],
) -> CatalogLineage | CatalogLineageIdMismatch:
    lineage = CatalogLineage(
        RevisionKind(str(record["kind"])),
        PublishedRevisionHash(str(record["founding_revision_hash"])),
    )
    stored = CatalogLineageId(str(record["lineage_id"]))
    if lineage.lineage_id != stored:
        return CatalogLineageIdMismatch(stored, lineage.lineage_id)
    return lineage


def _name_holder(
    connection: sa.Connection,
    kind: RevisionKind,
    display_name: CatalogLineageDisplayName,
    *,
    except_lineage_id: CatalogLineageId | None = None,
) -> CatalogLineageId | None:
    statement = (
        sa.select(catalog_lineage_aliases.c.lineage_id)
        .select_from(
            catalog_lineage_aliases.join(
                catalog_lineages,
                catalog_lineages.c.lineage_id == catalog_lineage_aliases.c.lineage_id,
            )
        )
        .where(
            catalog_lineage_aliases.c.name == display_name.value,
            catalog_lineages.c.kind == kind.value,
        )
        .distinct()
    )
    if except_lineage_id is not None:
        statement = statement.where(
            catalog_lineage_aliases.c.lineage_id != except_lineage_id.value
        )
    holders = connection.execute(statement).scalars().all()
    if not holders:
        return None
    return CatalogLineageId(str(holders[0]))


def revision_owner(
    connection: sa.Connection, kind: RevisionKind, revision_hash: PublishedRevisionHash
) -> CatalogLineageId | None:
    """The lineage one published revision belongs to, read through one connection.

    Public because catalog publication decisions share this lookup. It asks by
    revision rather than by name because a document can carry no display name.
    """
    owner = connection.scalar(
        sa.select(catalog_lineage_members.c.lineage_id)
        .select_from(
            catalog_lineage_members.join(
                catalog_lineages,
                catalog_lineages.c.lineage_id == catalog_lineage_members.c.lineage_id,
            )
        )
        .where(
            catalog_lineage_members.c.revision_hash == revision_hash.value,
            catalog_lineages.c.kind == kind.value,
        )
    )
    if owner is None:
        return None
    return CatalogLineageId(str(owner))


def _member(
    connection: sa.Connection,
    lineage_id: CatalogLineageId,
    revision_hash: PublishedRevisionHash,
) -> Mapping[Any, Any] | None:
    return (
        connection.execute(
            sa.select(catalog_lineage_members).where(
                catalog_lineage_members.c.lineage_id == lineage_id.value,
                catalog_lineage_members.c.revision_hash == revision_hash.value,
            )
        )
        .mappings()
        .one_or_none()
    )


def current_display_name(
    connection: sa.Connection, lineage_id: CatalogLineageId
) -> CatalogLineageDisplayName:
    """The name a lineage carries now, read through one connection.

    Public alongside `revision_owner`: a caller composing on its own
    transaction (a source intake judging whether a name-held or
    revision-owned lineage is really the path's own) asks the same question
    `found_lineage_in` and `admit_member_in` already answer for themselves.
    """
    name = connection.scalar(
        sa.select(catalog_lineage_aliases.c.name)
        .where(catalog_lineage_aliases.c.lineage_id == lineage_id.value)
        .order_by(catalog_lineage_aliases.c.activation_number.desc())
        .limit(1)
    )
    if name is None:
        raise ValueError("catalog lineage has no alias history")
    return CatalogLineageDisplayName(str(name))


def current_head_revision_hash(
    connection: sa.Connection, lineage_id: CatalogLineageId
) -> PublishedRevisionHash:
    """The revision hash a lineage serves as its current head, read through one connection.

    Public alongside `current_display_name`: a source intake recognising bytes
    as already present must know whether they are what the lineage's name
    actually serves now, not merely some revision the lineage once held --
    the same "highest `revision_number`" a name lookup already serves as
    `head`.
    """
    revision_hash = connection.scalar(
        sa.select(catalog_lineage_members.c.revision_hash)
        .where(catalog_lineage_members.c.lineage_id == lineage_id.value)
        .order_by(catalog_lineage_members.c.revision_number.desc())
        .limit(1)
    )
    if revision_hash is None:
        raise ValueError("catalog lineage has no members")
    return PublishedRevisionHash(str(revision_hash))


def _is_retired(connection: sa.Connection, lineage_id: CatalogLineageId) -> bool:
    return (
        connection.scalar(
            sa.select(sa.func.count())
            .select_from(catalog_lineage_retirements)
            .where(catalog_lineage_retirements.c.lineage_id == lineage_id.value)
        )
        or 0
    ) > 0


def _append_alias(
    connection: sa.Connection,
    lineage_id: CatalogLineageId,
    display_name: CatalogLineageDisplayName,
    actor: CatalogActor,
    activated_at: CatalogActivatedAt,
) -> None:
    connection.execute(
        catalog_lineage_aliases.insert().values(
            lineage_id=lineage_id.value,
            activation_number=_next_activation_number(
                connection, catalog_lineage_aliases, lineage_id.value
            ),
            name=display_name.value,
            actor=actor.value,
            activated_at=activated_at.value,
        )
    )


def _append_member(
    connection: sa.Connection,
    lineage_id: CatalogLineageId,
    revision: PublishedRevision,
) -> int:
    current = connection.scalar(
        sa.select(sa.func.max(catalog_lineage_members.c.revision_number)).where(
            catalog_lineage_members.c.lineage_id == lineage_id.value
        )
    )
    revision_number = 1 if current is None else int(current) + 1
    connection.execute(
        catalog_lineage_members.insert().values(
            lineage_id=lineage_id.value,
            revision_number=revision_number,
            revision_hash=revision.revision_hash.value,
        )
    )
    return revision_number


def persist_workflow_publication(
    connection: sa.Connection, revision: WorkflowRevision
) -> None:
    """Write one workflow document where the publication door writes it.

    The same row `DbosWorkflowRevisionPublisher` inserts, on a connection its
    caller owns, so that publishing a workflow and admitting it can share one
    transaction. Bytes already there are left alone; whether what is stored
    agrees with what was handed in is `_record_publication`'s check, and it runs
    on this connection right after.

    Public with `found_lineage_in`, `admit_member_in`, `revision_owner` and
    `current_display_name`: they are the catalog's writes and reads as a
    caller that already owns a transaction sees them, so a second door into
    the catalog -- a source intake admitting many files at once -- composes
    them instead of writing publication rows or re-deriving a lineage's name
    of its own. The store methods below are the same three writes for a
    caller that owns no transaction and wants one per call.
    """
    connection.execute(
        workflow_revisions.insert()
        .prefix_with("OR IGNORE")
        .values(
            revision_hash=revision.revision_hash.value,
            document=revision.document,
        )
    )


def found_lineage_in(
    connection: sa.Connection,
    revision: PublishedRevision,
    display_name: CatalogLineageDisplayName,
    actor: CatalogActor,
    activated_at: CatalogActivatedAt,
) -> FoundCatalogLineageResult:
    """Start a lineage at this revision, on a transaction the caller owns."""

    lineage = CatalogLineage(revision.kind, revision.revision_hash)
    if _record_publication(connection, revision) is None:
        return CatalogAdmissionUnpublished(revision.revision_hash)
    existing = _lineage_record(connection, lineage.lineage_id)
    if existing is not None:
        stored = _derived_lineage_or_mismatch(existing)
        if isinstance(stored, CatalogLineageIdMismatch):
            return stored
        if _is_retired(connection, stored.lineage_id):
            return CatalogAdmissionRetired(stored.lineage_id)
        member = _member(connection, lineage.lineage_id, revision.revision_hash)
        if member is None:
            owner = revision_owner(connection, revision.kind, revision.revision_hash)
            if owner is not None:
                return CatalogAdmissionRevisionOwned(revision.revision_hash, owner)
            raise ValueError("catalog founding lineage is missing its founding member")
        return CatalogAdmissionExisting(
            stored,
            revision,
            int(member["revision_number"]),
            current_display_name(connection, lineage.lineage_id),
        )
    owner = revision_owner(connection, revision.kind, revision.revision_hash)
    if owner is not None:
        return CatalogAdmissionRevisionOwned(revision.revision_hash, owner)
    holder = _name_holder(connection, revision.kind, display_name)
    if holder is not None:
        return CatalogAdmissionNameHeld(display_name, holder)
    connection.execute(
        catalog_lineages.insert().values(
            lineage_id=lineage.lineage_id.value,
            kind=revision.kind.value,
            founding_revision_hash=revision.revision_hash.value,
        )
    )
    _append_member(connection, lineage.lineage_id, revision)
    _append_alias(connection, lineage.lineage_id, display_name, actor, activated_at)
    return CatalogLineageFounded(lineage, revision, display_name)


def admit_member_in(
    connection: sa.Connection,
    lineage_id: CatalogLineageId,
    revision: PublishedRevision,
    display_name: CatalogLineageDisplayName,
    actor: CatalogActor,
    activated_at: CatalogActivatedAt,
) -> AdmitCatalogMemberResult:
    """Join this revision to a standing lineage, on the caller's transaction."""

    record = _lineage_record(connection, lineage_id)
    if record is None:
        return CatalogAdmissionLineageMissing(lineage_id)
    lineage = _derived_lineage_or_mismatch(record)
    if isinstance(lineage, CatalogLineageIdMismatch):
        return lineage
    if lineage.kind is not revision.kind:
        return CatalogAdmissionKindMismatch(
            lineage.lineage_id, lineage.kind, revision.kind
        )
    if _is_retired(connection, lineage.lineage_id):
        return CatalogAdmissionRetired(lineage.lineage_id)
    if _record_publication(connection, revision) is None:
        return CatalogAdmissionUnpublished(revision.revision_hash)
    existing_member = _member(connection, lineage.lineage_id, revision.revision_hash)
    if existing_member is not None:
        return CatalogAdmissionExisting(
            lineage,
            revision,
            int(existing_member["revision_number"]),
            current_display_name(connection, lineage.lineage_id),
        )
    owner = revision_owner(connection, revision.kind, revision.revision_hash)
    if owner is not None:
        return CatalogAdmissionRevisionOwned(revision.revision_hash, owner)
    holder = _name_holder(
        connection,
        revision.kind,
        display_name,
        except_lineage_id=lineage.lineage_id,
    )
    if holder is not None:
        return CatalogAdmissionNameHeld(display_name, holder)
    revision_number = _append_member(connection, lineage.lineage_id, revision)
    _append_alias(connection, lineage.lineage_id, display_name, actor, activated_at)
    return CatalogMemberAdmitted(lineage, revision, revision_number, display_name)


_RESOLVED_REVISION_CACHE_CAPACITY = 4_096
"""Comfortably above the tens of revisions one catalog holds today.

The cap bounds memory for a pathological catalog; it never bounds
correctness. A found revision is content-addressed (`revision_hash` is
derived from its bytes) and published revisions never change once they
exist, so a found answer stays correct for the store's whole process
lifetime -- there is no staleness to invalidate.
"""


class _ResolvedRevisionCache:
    """Published revisions this store's resolve seam has already read once.

    #937 (round 2): profiling a described page found the dominant cost was
    not the connection-per-reference checkout round 1 fixed, but the
    downstream schema read and validation every caller does with the bytes
    this seam returns -- run once per resolved reference, uncached, even
    when the same schema is referenced by many revisions on one page. This
    store cannot cache that downstream work (it happens in the application
    layer, on bytes this store has already handed back), but it owns the one
    lookup every caller shares first: resolving `(kind, revision_hash)` to
    the published bytes. Caching *that* here means the second, third, and
    Nth caller asking for the same reference on a page -- or across pages,
    for the process's lifetime -- gets the bytes without a query, which is
    this store's honest share of the fix.

    A missing answer is deliberately never cached: the hash it names may be
    published moments after the miss, and a cached miss would then keep
    answering stale long after that publish committed.
    """

    def __init__(self) -> None:
        self._found: dict[
            tuple[RevisionKind, PublishedRevisionHash], PublishedRevision
        ] = {}
        self._lock = threading.Lock()

    def found(
        self, kind: RevisionKind, revision_hash: PublishedRevisionHash
    ) -> PublishedRevision | None:
        with self._lock:
            return self._found.get((kind, revision_hash))

    def remember(self, revision: PublishedRevision) -> None:
        with self._lock:
            if len(self._found) < _RESOLVED_REVISION_CACHE_CAPACITY:
                self._found[(revision.kind, revision.revision_hash)] = revision


class _ConnectionBoundResolver:
    """`PublishedRevisionResolver` bound to one connection its caller already holds.

    Yielded by `DbosCatalogStore.resolver_session` for the length of one
    composed read; every `resolve` call here runs on that one connection
    instead of checking out (and later returning) a fresh one per call. A
    reference this session (or an earlier one on the same store) already
    found answers from `cache` without touching that connection at all.
    """

    def __init__(
        self, connection: sa.Connection, cache: _ResolvedRevisionCache
    ) -> None:
        self._connection = connection
        self._cache = cache

    def resolve(
        self, kind: RevisionKind, revision_hash: PublishedRevisionHash
    ) -> ResolvePublishedRevisionResult:
        cached = self._cache.found(kind, revision_hash)
        if cached is not None:
            return PublishedRevisionFound(cached)
        try:
            revision = _select_published_revision(self._connection, kind, revision_hash)
        except (OperationalError, PoolTimeoutError):
            return PublishedRevisionsUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return DurableStateCorrupt()
        if revision is None:
            return PublishedRevisionMissing()
        self._cache.remember(revision)
        return PublishedRevisionFound(revision)


class _RefusingResolver:
    """A `PublishedRevisionResolver` that answers every lookup with one refusal.

    Yielded when `resolver_session` could not open the connection a page
    needs: the same typed refusal a single `resolve` call already answers
    with for the same failure, so a connection outage never escapes a session
    as a raw exception out of `__enter__` -- every reference the page tries to
    resolve through this answers that refusal instead.
    """

    def __init__(self, refusal: ResolvePublishedRevisionResult) -> None:
        self._refusal = refusal

    def resolve(
        self, kind: RevisionKind, revision_hash: PublishedRevisionHash
    ) -> ResolvePublishedRevisionResult:
        del kind, revision_hash
        return self._refusal


class DbosCatalogStore:
    """Published revisions and admitted named lineages over the V12 catalog tables."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._resolved_revisions = _ResolvedRevisionCache()

    def store_intake(self, intake: CatalogIntake) -> StoreCatalogIntakeResult:
        try:
            with canonical_write_transaction(self._engine) as connection:
                record = (
                    connection.execute(
                        sa.select(catalog_intakes).where(
                            catalog_intakes.c.intake_id == intake.intake_id.value
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if record is not None:
                    stored = _intake_from_record(record)
                    return (
                        CatalogIntakeExisting(stored)
                        if stored == intake
                        else DurableStateCorrupt()
                    )
                connection.execute(
                    catalog_intakes.insert().values(
                        intake_id=intake.intake_id.value,
                        kind=intake.kind.value,
                        document=intake.document,
                        actor=intake.actor.value,
                        activated_at=intake.activated_at.value,
                    )
                )
                return CatalogIntakeStored(intake)
        except (OperationalError, PoolTimeoutError):
            return DurableWriteUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return DurableStateCorrupt()

    def read_intake(self, intake_id: CatalogIntakeId) -> ReadCatalogIntakeResult:
        try:
            with self._engine.connect() as connection:
                record = (
                    connection.execute(
                        sa.select(catalog_intakes).where(
                            catalog_intakes.c.intake_id == intake_id.value
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
            return (
                CatalogIntakeMissing(intake_id)
                if record is None
                else CatalogIntakeFound(_intake_from_record(record))
            )
        except (OperationalError, PoolTimeoutError):
            return DurableWriteUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return DurableStateCorrupt()

    def publish_revision(self, revision: PublishedRevision) -> PublishRevisionResult:
        try:
            with canonical_write_transaction(self._engine) as connection:
                existing = (
                    connection.execute(
                        sa.select(published_revisions).where(
                            published_revisions.c.kind == revision.kind.value,
                            published_revisions.c.revision_hash
                            == revision.revision_hash.value,
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if existing is not None:
                    durable = published_revision_from_record(existing)
                    if durable == revision:
                        return PublishedRevisionExisting(durable)
                    return PublishedRevisionCollision()
                connection.execute(
                    published_revisions.insert().values(
                        kind=revision.kind.value,
                        revision_hash=revision.revision_hash.value,
                        document=revision.document,
                    )
                )
                return PublishedRevisionCreated(revision)
        except (OperationalError, PoolTimeoutError):
            return DurableWriteUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return DurableStateCorrupt()

    def resolve(
        self, kind: RevisionKind, revision_hash: PublishedRevisionHash
    ) -> ResolvePublishedRevisionResult:
        cached = self._resolved_revisions.found(kind, revision_hash)
        if cached is not None:
            return PublishedRevisionFound(cached)
        try:
            with self._engine.connect() as connection:
                revision = _select_published_revision(connection, kind, revision_hash)
            if revision is None:
                return PublishedRevisionMissing()
            self._resolved_revisions.remember(revision)
            return PublishedRevisionFound(revision)
        except (OperationalError, PoolTimeoutError):
            return PublishedRevisionsUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return DurableStateCorrupt()

    @contextmanager
    def resolver_session(self) -> Iterator[PublishedRevisionResolver]:
        """One connection, reused by every `resolve` call made inside the block.

        `resolve` alone checks out (and returns) a fresh connection per call.
        A composed read that resolves many references for one page --
        `list_described_workflow_revisions` judging each listed revision's
        executability -- pays that checkout once per reference instead of
        once for the whole page. This session is that one connection: every
        lookup made through the resolver it yields runs on it, and it closes
        when the `with` block does. A connection this store cannot open right
        now answers the same refusal a single `resolve` call already gives for
        that failure, rather than raising out of `__enter__`. A reference
        already found by an earlier call on this store -- in this session or a
        prior one -- answers from `_resolved_revisions` without touching the
        connection at all.
        """
        try:
            connection = self._engine.connect()
        except (OperationalError, PoolTimeoutError):
            yield _RefusingResolver(PublishedRevisionsUnavailable())
            return
        except (ValueError, RuntimeError, DatabaseError):
            yield _RefusingResolver(DurableStateCorrupt())
            return
        try:
            yield _ConnectionBoundResolver(connection, self._resolved_revisions)
        finally:
            connection.close()

    def list_revisions(
        self, kind: RevisionKind, after: PublishedRevisionHash | None, limit: int
    ) -> ListPublishedRevisionsResult:
        require_page_limit(limit, "revision")
        try:
            with self._engine.connect() as connection:
                statement = sa.select(published_revisions).where(
                    published_revisions.c.kind == kind.value
                )
                if after is not None:
                    statement = statement.where(
                        published_revisions.c.revision_hash > after.value
                    )
                records = tuple(
                    connection.execute(
                        statement.order_by(published_revisions.c.revision_hash).limit(
                            limit + 1
                        )
                    ).mappings()
                )
            page = records[:limit]
            revisions = tuple(published_revision_from_record(record) for record in page)
            next_after = (
                revisions[-1].revision_hash
                if len(records) > limit and revisions
                else None
            )
            return PublishedRevisionPage(revisions, next_after)
        except (OperationalError, PoolTimeoutError):
            return PublishedRevisionsUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return DurableStateCorrupt()

    def resolve_reference(
        self,
        kind: RevisionKind,
        lineage_id: CatalogLineageId,
        revision_hash: PublishedRevisionHash,
    ) -> ResolvePublishedRevisionResult:
        try:
            with self._engine.connect() as connection:
                lineage_record = (
                    connection.execute(
                        sa.select(catalog_lineages).where(
                            catalog_lineages.c.lineage_id == lineage_id.value
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if lineage_record is None:
                    return PublishedRevisionMissing()
                lineage = catalog_lineage_from_record(lineage_record)
                if lineage.kind is not kind:
                    return PublishedRevisionMissing()
                member = (
                    connection.execute(
                        sa.select(catalog_lineage_members).where(
                            catalog_lineage_members.c.lineage_id
                            == lineage.lineage_id.value,
                            catalog_lineage_members.c.revision_hash
                            == revision_hash.value,
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if member is None:
                    return PublishedRevisionMissing()
                revision_record = (
                    connection.execute(
                        sa.select(published_revisions).where(
                            published_revisions.c.kind == kind.value,
                            published_revisions.c.revision_hash == revision_hash.value,
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
            if revision_record is None:
                raise ValueError(
                    "catalog lineage member names a published revision that is missing"
                )
            return PublishedRevisionFound(
                published_revision_from_record(revision_record)
            )
        except (OperationalError, PoolTimeoutError):
            return PublishedRevisionsUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return DurableStateCorrupt()

    def found_lineage(
        self,
        revision: PublishedRevision,
        display_name: CatalogLineageDisplayName,
        actor: CatalogActor,
        activated_at: CatalogActivatedAt,
        claimed_lineage_id: CatalogLineageId | None = None,
    ) -> FoundCatalogLineageResult:
        lineage = CatalogLineage(revision.kind, revision.revision_hash)
        if claimed_lineage_id is not None and claimed_lineage_id != lineage.lineage_id:
            return CatalogLineageIdMismatch(claimed_lineage_id, lineage.lineage_id)
        try:
            with canonical_write_transaction(self._engine) as connection:
                return found_lineage_in(
                    connection, revision, display_name, actor, activated_at
                )
        except (OperationalError, PoolTimeoutError):
            return DurableWriteUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return DurableStateCorrupt()

    def admit_member(
        self,
        lineage_id: CatalogLineageId,
        revision: PublishedRevision,
        display_name: CatalogLineageDisplayName,
        actor: CatalogActor,
        activated_at: CatalogActivatedAt,
    ) -> AdmitCatalogMemberResult:
        try:
            with canonical_write_transaction(self._engine) as connection:
                return admit_member_in(
                    connection, lineage_id, revision, display_name, actor, activated_at
                )
        except (OperationalError, PoolTimeoutError):
            return DurableWriteUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return DurableStateCorrupt()

    def add_workflow(
        self,
        revision: WorkflowRevision,
        display_name: CatalogLineageDisplayName,
        actor: CatalogActor,
        activated_at: CatalogActivatedAt,
    ) -> AddWorkflowToLibraryResult:
        published = PublishedRevision(RevisionKind.WORKFLOW, revision.document)
        try:
            with canonical_write_transaction(self._engine) as connection:
                persist_workflow_publication(connection, revision)
                holder = _name_holder(connection, RevisionKind.WORKFLOW, display_name)
                admission = (
                    found_lineage_in(
                        connection, published, display_name, actor, activated_at
                    )
                    if holder is None
                    else admit_member_in(
                        connection,
                        holder,
                        published,
                        display_name,
                        actor,
                        activated_at,
                    )
                )
                if not isinstance(
                    admission,
                    CatalogLineageFounded
                    | CatalogMemberAdmitted
                    | CatalogAdmissionExisting,
                ):
                    # The document and its name are one act (ADR 0018 §2), so a
                    # refused admission must not leave the bytes published alone.
                    connection.rollback()
                return admission
        except (OperationalError, PoolTimeoutError):
            return DurableWriteUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return DurableStateCorrupt()

    def retire_lineage(
        self,
        lineage_id: CatalogLineageId,
        state: CatalogRetirementState,
        actor: CatalogActor,
        activated_at: CatalogActivatedAt,
    ) -> RetireCatalogLineageResult:
        try:
            with canonical_write_transaction(self._engine) as connection:
                record = _lineage_record(connection, lineage_id)
                if record is None:
                    return CatalogAdmissionLineageMissing(lineage_id)
                lineage = _derived_lineage_or_mismatch(record)
                if isinstance(lineage, CatalogLineageIdMismatch):
                    return lineage
                if _is_retired(connection, lineage.lineage_id):
                    return CatalogRetirementExisting(lineage.lineage_id)
                connection.execute(
                    catalog_lineage_retirements.insert().values(
                        lineage_id=lineage.lineage_id.value,
                        activation_number=_next_activation_number(
                            connection,
                            catalog_lineage_retirements,
                            lineage.lineage_id.value,
                        ),
                        state=state.value,
                        actor=actor.value,
                        activated_at=activated_at.value,
                    )
                )
                return CatalogLineageRetired(lineage.lineage_id)
        except (OperationalError, PoolTimeoutError):
            return DurableWriteUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return DurableStateCorrupt()

    def resolve_name(
        self,
        kind: RevisionKind,
        lineage_id_or_name: CatalogLineageQuery,
        position: CatalogRevisionPosition,
    ) -> ResolveCatalogNameResult:
        try:
            with self._engine.connect() as connection:
                if isinstance(lineage_id_or_name, CatalogLineageId):
                    record = _lineage_record(connection, lineage_id_or_name)
                    if record is None:
                        return CatalogNameMissing(lineage_id_or_name, position)
                    lineage = catalog_lineage_from_record(record)
                    if lineage.kind is not kind:
                        return CatalogNameMissing(lineage_id_or_name, position)
                else:
                    holders = (
                        connection.execute(
                            sa.select(catalog_lineage_aliases.c.lineage_id)
                            .select_from(
                                catalog_lineage_aliases.join(
                                    catalog_lineages,
                                    catalog_lineages.c.lineage_id
                                    == catalog_lineage_aliases.c.lineage_id,
                                )
                            )
                            .where(
                                catalog_lineage_aliases.c.name
                                == lineage_id_or_name.value,
                                catalog_lineages.c.kind == kind.value,
                            )
                            .distinct()
                        )
                        .scalars()
                        .all()
                    )
                    if len(holders) > 1:
                        raise ValueError(
                            "catalog name is held by more than one lineage of one kind"
                        )
                    if not holders:
                        return CatalogNameMissing(lineage_id_or_name, position)
                    record = _lineage_record(
                        connection, CatalogLineageId(str(holders[0]))
                    )
                    if record is None:
                        raise ValueError(
                            "catalog alias names a lineage that is missing"
                        )
                    lineage = catalog_lineage_from_record(record)
                member_statement = sa.select(catalog_lineage_members).where(
                    catalog_lineage_members.c.lineage_id == lineage.lineage_id.value
                )
                if position == "head":
                    member_statement = member_statement.order_by(
                        catalog_lineage_members.c.revision_number.desc()
                    ).limit(1)
                else:
                    member_statement = member_statement.where(
                        catalog_lineage_members.c.revision_number == position
                    )
                member = connection.execute(member_statement).mappings().one_or_none()
                if member is None:
                    return CatalogNameMissing(lineage_id_or_name, position)
                revision_hash = PublishedRevisionHash(str(member["revision_hash"]))
                published = (
                    connection.execute(
                        sa.select(published_revisions).where(
                            published_revisions.c.kind == kind.value,
                            published_revisions.c.revision_hash == revision_hash.value,
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if published is None:
                    raise ValueError(
                        "catalog lineage member names a published revision that is missing"
                    )
                published_revision_from_record(published)
                return CatalogNameFound(
                    lineage.lineage_id,
                    revision_hash,
                    int(member["revision_number"]),
                    current_display_name(connection, lineage.lineage_id),
                    retired=_is_retired(connection, lineage.lineage_id),
                )
        except (OperationalError, PoolTimeoutError):
            return PublishedRevisionsUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return DurableStateCorrupt()
