"""Where a registered definition source and its delivered paths are kept.

One configuration is one immutable row plus its ordered selection rows, and the
two are written inside the one canonical write transaction so a registration
can never stand with half its selections. Re-registering the configuration that
already stands changes nothing and says so -- whatever number it stands under,
because what is compared is the configuration and never the number it landed
with; a changed configuration of the same repository and ref appends the next
revision of that same source rather than founding a second one
(`#660` ruled line 5).

Reading intakes is what a write-free scan needs and all it needs: the highest
`intake_number` per path, which is what ADR 0007 calls the latest intake.

Recording intakes is the one write that reaches into the catalog, and it stays
one transaction: publication, lineage membership and the provenance row of every
selected path are written on a single connection, so a batch that stops at its
last file leaves nothing of the ones before it. It composes the catalog's own
connection-bound writes (`catalog_store`) rather than repeating them -- a second
publication writer would be a second answer to what a published revision is.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DatabaseError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from atelier2.adapters.dbos.catalog_store import (
    admit_member_in,
    current_display_name,
    current_head_revision_hash,
    found_lineage_in,
    persist_workflow_publication,
    revision_owner,
)
from atelier2.adapters.dbos.schema import (
    catalog_lineage_members,
    catalog_source_intakes,
    host_definition_source_revisions,
    host_definition_source_selections,
)
from atelier2.adapters.dbos.transactions import canonical_write_transaction
from atelier2.contracts.catalog_v3 import (
    CatalogActivatedAt,
    CatalogActor,
    CatalogAdmissionExisting,
    CatalogAdmissionNameHeld,
    CatalogAdmissionRetired,
    CatalogAdmissionRevisionOwned,
    CatalogLineageFounded,
    CatalogLineageId,
    CatalogMemberAdmitted,
)
from atelier2.contracts.definition_sources import (
    DefinitionSourceAccess,
    DefinitionSourceActor,
    DefinitionSourceConfiguration,
    DefinitionSourceId,
    DefinitionSourceKind,
    DefinitionSourceRevision,
    DefinitionSourceSelection,
    RepositoryLocation,
    RepositoryPath,
    RepositoryRef,
    SelectionPattern,
    SourceCommit,
    SourceIntake,
)
from atelier2.contracts.revisions_v3 import (
    PublishedRevision,
    PublishedRevisionHash,
    RevisionKind,
)
from atelier2.ports.definition_sources import (
    DefinitionSourceFound,
    DefinitionSourceMissing,
    DefinitionSourceRegistered,
    DefinitionSourceUnchanged,
    PathAdopted,
    PathAlreadyInCatalog,
    PathIntaken,
    ReadDefinitionSourceResult,
    ReadSourceIntakesResult,
    RecordedPath,
    RecordSourceIntakesResult,
    RegisterDefinitionSourceResult,
    SelectedIntake,
    SourceIntakeRecorded,
    SourceIntakeRefused,
)
from atelier2.ports.durable_runs import DurableStateCorrupt, DurableWriteUnavailable

_FIRST_REVISION_NUMBER = 1
_FIRST_SELECTION_ORDINAL = 1
_FIRST_INTAKE_NUMBER = 1


class DbosDefinitionSources:
    """The canonical store's answer for registered definition sources."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def register(
        self, configuration: DefinitionSourceConfiguration
    ) -> RegisterDefinitionSourceResult:
        try:
            with canonical_write_transaction(self._engine) as connection:
                standing = self._newest(connection, configuration.source_id)
                if standing is not None and standing.configuration == configuration:
                    return DefinitionSourceUnchanged(standing)
                appended = DefinitionSourceRevision(
                    configuration,
                    _FIRST_REVISION_NUMBER
                    if standing is None
                    else standing.revision_number + 1,
                )
                self._insert(connection, appended)
                return DefinitionSourceRegistered(appended)
        except (OperationalError, PoolTimeoutError):
            return DurableWriteUnavailable()
        except (ValueError, TypeError, DatabaseError):
            return DurableStateCorrupt()

    def read_source(self, source_id: DefinitionSourceId) -> ReadDefinitionSourceResult:
        try:
            with self._engine.connect() as connection:
                newest = self._newest(connection, source_id)
                if newest is None:
                    return DefinitionSourceMissing(source_id)
                return DefinitionSourceFound(newest)
        except (OperationalError, PoolTimeoutError):
            return DurableWriteUnavailable()
        except (ValueError, TypeError, DatabaseError):
            return DurableStateCorrupt()

    def latest_intakes(self, source_id: DefinitionSourceId) -> ReadSourceIntakesResult:
        try:
            with self._engine.connect() as connection:
                return self._latest_intakes(connection, source_id)
        except (OperationalError, PoolTimeoutError):
            return DurableWriteUnavailable()
        except (ValueError, TypeError, DatabaseError):
            return DurableStateCorrupt()

    def record_intakes(
        self,
        source_id: DefinitionSourceId,
        commit: SourceCommit,
        selected: tuple[SelectedIntake, ...],
        actor: CatalogActor,
        intaken_at: CatalogActivatedAt,
    ) -> RecordSourceIntakesResult:
        try:
            with canonical_write_transaction(self._engine) as connection:
                standing = self._latest_intakes(connection, source_id)
                recorded: list[RecordedPath] = []
                for one in selected:
                    entered = self._take_in(
                        connection, source_id, commit, one, standing, actor, intaken_at
                    )
                    if isinstance(entered, SourceIntakeRefused):
                        # One refused path makes the whole commit refused: the
                        # operator was promised a pull that lands whole or not
                        # at all, so the paths already written go back too.
                        connection.rollback()
                        return entered
                    recorded.append(entered)
                return SourceIntakeRecorded(tuple(recorded))
        except (OperationalError, PoolTimeoutError):
            return DurableWriteUnavailable()
        except (ValueError, TypeError, DatabaseError):
            return DurableStateCorrupt()

    def _take_in(
        self,
        connection: Connection,
        source_id: DefinitionSourceId,
        commit: SourceCommit,
        selected: SelectedIntake,
        standing: Mapping[RepositoryPath, SourceIntake],
        actor: CatalogActor,
        intaken_at: CatalogActivatedAt,
    ) -> RecordedPath | SourceIntakeRefused:
        """Publish, admit and record one path on the batch's own transaction."""

        published = PublishedRevision(RevisionKind.WORKFLOW, selected.revision.document)
        persist_workflow_publication(connection, selected.revision)
        previous = standing.get(selected.path)
        adopted: CatalogLineageId | None = None
        if previous is None:
            admission = found_lineage_in(
                connection, published, selected.display_name, actor, intaken_at
            )
            if isinstance(
                admission, CatalogAdmissionNameHeld
            ) and not _a_source_has_fed(connection, admission.holder):
                # #660 A3: nobody has ever fed this name's lineage from a
                # source, so it is a manual import -- adopt it rather than
                # refuse, becoming its new head and leaving its history alone.
                adopted = admission.holder
                admission = admit_member_in(
                    connection,
                    admission.holder,
                    published,
                    selected.display_name,
                    actor,
                    intaken_at,
                )
        else:
            admission = admit_member_in(
                connection,
                _lineage_holding(connection, previous),
                published,
                selected.display_name,
                actor,
                intaken_at,
            )
        match admission:
            case CatalogLineageFounded() | CatalogMemberAdmitted():
                intake = SourceIntake(
                    source_id,
                    selected.path,
                    _FIRST_INTAKE_NUMBER
                    if previous is None
                    else previous.intake_number + 1,
                    RevisionKind.WORKFLOW,
                    published.revision_hash,
                    commit,
                )
                _insert_intake(connection, intake, actor, intaken_at)
                if adopted is not None:
                    return PathAdopted(intake, adopted)
                return PathIntaken(intake)
            case CatalogAdmissionExisting():
                return _existing_revision_outcome(
                    connection,
                    source_id,
                    selected,
                    commit,
                    published,
                    previous,
                    admission,
                    actor,
                    intaken_at,
                )
            case CatalogAdmissionRevisionOwned(revision_hash, owner) if (
                previous is None
                and not _a_source_has_fed(connection, owner)
                and current_display_name(connection, owner) == selected.display_name
                and revision_hash == current_head_revision_hash(connection, owner)
            ):
                # #660 A3's sibling case: these exact bytes already sit as the
                # current head of the lineage this path's own name holds.
                # Nobody has ever fed that lineage from a source, so it is the
                # path's own lineage under another guise, not a foreign entry
                # -- report it present and record provenance rather than
                # refusing the whole batch. A match against anything but the
                # head falls through to the loud refusal below: it is a real
                # revision of that lineage, but not the one the name serves.
                _insert_intake(
                    connection,
                    _founding_intake(source_id, selected, commit, revision_hash),
                    actor,
                    intaken_at,
                )
                return PathAlreadyInCatalog(
                    selected.path, RevisionKind.WORKFLOW, revision_hash
                )
            case (
                CatalogAdmissionNameHeld()
                | CatalogAdmissionRevisionOwned()
                | CatalogAdmissionRetired()
            ):
                return SourceIntakeRefused(selected.path, admission)
            case _:
                raise ValueError(
                    "the catalog refused a source intake for a reason no operator "
                    f"can resolve: {admission}"
                )

    def _newest(
        self, connection: Connection, source_id: DefinitionSourceId
    ) -> DefinitionSourceRevision | None:
        record = (
            connection.execute(
                sa.select(host_definition_source_revisions)
                .where(host_definition_source_revisions.c.source_id == source_id.value)
                .order_by(host_definition_source_revisions.c.revision_number.desc())
                .limit(1)
            )
            .mappings()
            .one_or_none()
        )
        if record is None:
            return None
        return _revision_from_records(
            record,
            connection.execute(
                sa.select(host_definition_source_selections)
                .where(
                    host_definition_source_selections.c.revision_hash
                    == record["revision_hash"]
                )
                .order_by(host_definition_source_selections.c.selection_ordinal)
            )
            .mappings()
            .all(),
        )

    def _insert(
        self, connection: Connection, revision: DefinitionSourceRevision
    ) -> None:
        connection.execute(
            host_definition_source_revisions.insert().values(
                revision_hash=revision.revision_hash.value,
                source_id=revision.configuration.source_id.value,
                revision_number=revision.revision_number,
                source_kind=revision.configuration.kind.value,
                repository_location=revision.configuration.location.value,
                repository_ref=revision.configuration.ref.value,
                access=revision.configuration.access.value,
                connected_by=revision.configuration.connected_by.value,
            )
        )
        connection.execute(
            host_definition_source_selections.insert(),
            [
                {
                    "revision_hash": revision.revision_hash.value,
                    "selection_ordinal": ordinal,
                    "path_pattern": selection.pattern.value,
                    "revision_kind": selection.kind.value,
                }
                for ordinal, selection in enumerate(
                    revision.configuration.selections,
                    start=_FIRST_SELECTION_ORDINAL,
                )
            ],
        )

    def _latest_intakes(
        self, connection: Connection, source_id: DefinitionSourceId
    ) -> Mapping[RepositoryPath, SourceIntake]:
        highest = (
            sa.select(
                catalog_source_intakes.c.source_path,
                sa.func.max(catalog_source_intakes.c.intake_number).label(
                    "intake_number"
                ),
            )
            .where(catalog_source_intakes.c.source_id == source_id.value)
            .group_by(catalog_source_intakes.c.source_path)
            .subquery()
        )
        records = (
            connection.execute(
                sa.select(catalog_source_intakes)
                .join(
                    highest,
                    sa.and_(
                        catalog_source_intakes.c.source_path == highest.c.source_path,
                        catalog_source_intakes.c.intake_number
                        == highest.c.intake_number,
                    ),
                )
                .where(catalog_source_intakes.c.source_id == source_id.value)
            )
            .mappings()
            .all()
        )
        return {
            intake.path: intake
            for intake in (_intake_from_record(record) for record in records)
        }


def _revision_from_records(
    record: Mapping[Any, Any], selections: Sequence[Mapping[Any, Any]]
) -> DefinitionSourceRevision:
    revision = DefinitionSourceRevision(
        DefinitionSourceConfiguration(
            DefinitionSourceKind(str(record["source_kind"])),
            RepositoryLocation(str(record["repository_location"])),
            RepositoryRef(str(record["repository_ref"])),
            DefinitionSourceAccess(str(record["access"])),
            DefinitionSourceActor(str(record["connected_by"])),
            tuple(
                DefinitionSourceSelection(
                    SelectionPattern(str(selection["path_pattern"])),
                    RevisionKind(str(selection["revision_kind"])),
                )
                for selection in selections
            ),
        ),
        int(str(record["revision_number"])),
    )
    if revision.revision_hash.value != record["revision_hash"]:
        raise ValueError("durable definition source hash disagrees with its fields")
    return revision


def _intake_from_record(record: Mapping[Any, Any]) -> SourceIntake:
    return SourceIntake(
        DefinitionSourceId(str(record["source_id"])),
        RepositoryPath(str(record["source_path"])),
        int(str(record["intake_number"])),
        RevisionKind(str(record["revision_kind"])),
        PublishedRevisionHash(str(record["revision_hash"])),
        SourceCommit(str(record["source_commit"])),
    )


def _a_source_has_fed(connection: Connection, lineage_id: CatalogLineageId) -> bool:
    """Whether any source has ever taken a revision into this lineage.

    A lineage a source founded or joined carries a `catalog_source_intakes`
    row under every revision hash it holds as a member; a lineage a manual
    import founded carries none. `#660` A3 lets the intake adopt a name-held
    or revision-owned lineage only while this answers `False` -- once any
    source, this one included, has fed it, adopting it again would let a name
    silently move which lineage a path's own continuity means. No caller
    needs which source fed it, only whether one has.
    """

    return (
        connection.scalar(
            sa.select(catalog_source_intakes.c.source_id)
            .select_from(
                catalog_source_intakes.join(
                    catalog_lineage_members,
                    sa.and_(
                        catalog_lineage_members.c.revision_hash
                        == catalog_source_intakes.c.revision_hash,
                        catalog_source_intakes.c.revision_kind
                        == RevisionKind.WORKFLOW.value,
                    ),
                )
            )
            .where(catalog_lineage_members.c.lineage_id == lineage_id.value)
            .limit(1)
        )
        is not None
    )


def _lineage_holding(
    connection: Connection, previous: SourceIntake
) -> CatalogLineageId:
    """The lineage this path already delivers into, read from its last revision.

    Asked of the lineage members rather than of the intake row, because the
    intake names a revision and a lineage is what holds one; from the second
    intake on, the path's own history is a chain of revisions and only its
    membership says where they belong.
    """

    owner = revision_owner(connection, previous.revision_kind, previous.revision_hash)
    if owner is None:
        raise ValueError(
            "a recorded source intake names a revision no catalog lineage holds"
        )
    return owner


def _existing_revision_outcome(
    connection: Connection,
    source_id: DefinitionSourceId,
    selected: SelectedIntake,
    commit: SourceCommit,
    published: PublishedRevision,
    previous: SourceIntake | None,
    existing: CatalogAdmissionExisting,
    actor: CatalogActor,
    intaken_at: CatalogActivatedAt,
) -> PathAlreadyInCatalog | SourceIntakeRefused:
    """What bytes the catalog already holds mean for the path delivering them.

    A re-intake is simply present: its own lineage already answered. A first
    intake founded a lineage whose name may be somebody else's, and calling a
    foreign entry this source's delivery would commit the paths behind it.
    """

    present = PathAlreadyInCatalog(
        selected.path, RevisionKind.WORKFLOW, published.revision_hash
    )
    if previous is not None:
        return present
    lineage_id = existing.lineage.lineage_id
    if existing.display_name != selected.display_name:
        return SourceIntakeRefused(
            selected.path,
            CatalogAdmissionRevisionOwned(published.revision_hash, lineage_id),
        )
    if _a_source_has_fed(connection, lineage_id):
        # ADR 0007 (A3) only lets an *unsourced* lineage be adopted. Once any
        # source -- this one included -- has fed it, its name is genuinely held:
        # a byte-identical delivery may not attach its own provenance to
        # somebody else's continuity, or its next, differing delivery could
        # re-head that lineage as if it had always been this source's own.
        return SourceIntakeRefused(
            selected.path,
            CatalogAdmissionNameHeld(existing.display_name, lineage_id),
        )
    if published.revision_hash != current_head_revision_hash(connection, lineage_id):
        # These bytes founded the lineage, but a later revision now stands as
        # its head: the repository no longer carries what this name serves, so
        # reporting it present would let a stale delivery answer for served
        # bytes it is not.
        return SourceIntakeRefused(
            selected.path,
            CatalogAdmissionRevisionOwned(published.revision_hash, lineage_id),
        )
    # A first intake byte-identical to the lineage's own founding revision
    # changes nothing in the catalog, but this source has still taken the path
    # in for the first time and earns the same provenance any other first
    # intake would.
    _insert_intake(
        connection,
        _founding_intake(source_id, selected, commit, published.revision_hash),
        actor,
        intaken_at,
    )
    return present


def _founding_intake(
    source_id: DefinitionSourceId,
    selected: SelectedIntake,
    commit: SourceCommit,
    revision_hash: PublishedRevisionHash,
) -> SourceIntake:
    """The first intake row a path earns when it is recognised present, not admitted.

    Shared by the `CatalogAdmissionExisting` and `CatalogAdmissionRevisionOwned`
    recognise-as-present arms of `_take_in`: both run only for a path's first
    delivery (`previous is None`), so the intake number is always
    `_FIRST_INTAKE_NUMBER`. The admitted arm (`CatalogLineageFounded` |
    `CatalogMemberAdmitted`) also serves a path's later deliveries and keeps
    its own numbering instead of calling this.
    """

    return SourceIntake(
        source_id,
        selected.path,
        _FIRST_INTAKE_NUMBER,
        RevisionKind.WORKFLOW,
        revision_hash,
        commit,
    )


def _insert_intake(
    connection: Connection,
    intake: SourceIntake,
    actor: CatalogActor,
    intaken_at: CatalogActivatedAt,
) -> None:
    """Record where one revision came from, on the batch's own transaction."""

    connection.execute(
        catalog_source_intakes.insert().values(
            source_id=intake.source_id.value,
            source_path=intake.path.value,
            intake_number=intake.intake_number,
            revision_kind=intake.revision_kind.value,
            revision_hash=intake.revision_hash.value,
            source_commit=intake.source_commit.value,
            intaken_by=actor.value,
            intaken_at=intaken_at.value,
        )
    )
