"""A definition source registered, scanned and taken in, against a real store.

The dogfood is this repository's own `workflows/*.yaml`: one connect claims the
whole directory, one scan reports every file in it, and one intake takes every
one of them into the catalog. That is the sentence the operator was promised --
a source delivers many pieces in one act, not one piece per act -- proved
against real documents rather than a fixture that only ever says yes.

The intake scenarios author their own workflows where the assertion is about a
name: this repository's authored names are its own, and a test that needed one
of them to collide would break the moment a workflow here is renamed.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.definition_source_store import DbosDefinitionSources
from atelier2.adapters.dbos.runtime import create_canonical_engine
from atelier2.adapters.dbos.schema import (
    catalog_lineage_aliases,
    catalog_lineage_members,
    catalog_source_intakes,
    host_definition_source_revisions,
    host_definition_source_selections,
    initialize_schema,
    published_revisions,
)
from atelier2.adapters.dbos.starter import DbosWorkflowRevisionPublisher
from atelier2.contracts.catalog_v3 import (
    CatalogActivatedAt,
    CatalogActor,
    CatalogLineage,
    CatalogLineageDisplayName,
    CatalogLineageFounded,
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
    RevisionProvenance,
    SelectionPattern,
    SourceCommit,
)
from atelier2.contracts.revisions_v3 import (
    PublishedRevision,
    PublishedRevisionHash,
    RevisionKind,
)
from atelier2.contracts.runs import WorkflowRevision
from atelier2.contracts.workflow_projections import (
    DescribedWorkflowRevisionPage,
    EnrichedPageBudget,
)
from atelier2.contracts.workflow_refusals import WorkflowRefusalReason
from atelier2.host import main
from atelier2.ports.definition_sources import (
    DefinitionSourceFound,
    DefinitionSourceMissing,
    DefinitionSourceRegistered,
    DefinitionSourceUnchanged,
)
from atelier2.ports.published_revisions import CatalogNameFound
from atelier2.ports.workflow_revisions import WorkflowRevisionFound
from tests.scenarios.api import durable_queries
from tests.scenarios.workflows import declared_output

MAIN = "refs/heads/main"
WORKFLOW_SELECTION = "workflows/*.yaml"
PAGE_BUDGET = EnrichedPageBudget(maximum_nodes=1_000, maximum_document_bytes=1 << 20)
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_AUTHORED_BY_THE_SCENARIO = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "scenario",
    "GIT_AUTHOR_EMAIL": "scenario@invalid",
    "GIT_COMMITTER_NAME": "scenario",
    "GIT_COMMITTER_EMAIL": "scenario@invalid",
}


@pytest.fixture
def database(tmp_path: Path) -> Path:
    path = tmp_path / "atelier.sqlite"
    engine = create_canonical_engine(path)
    initialize_schema(engine)
    engine.dispose()
    return path


@pytest.fixture
def engine(database: Path) -> Iterator[Engine]:
    opened = create_canonical_engine(database)
    try:
        yield opened
    finally:
        opened.dispose()


def registration(
    *patterns: str, location: str = "/srv/definitions.git", ref: str = MAIN
) -> DefinitionSourceConfiguration:
    return DefinitionSourceConfiguration(
        DefinitionSourceKind.GIT,
        RepositoryLocation(location),
        RepositoryRef(ref),
        DefinitionSourceAccess.ANONYMOUS,
        DefinitionSourceActor("felix"),
        tuple(
            DefinitionSourceSelection(SelectionPattern(pattern), RevisionKind.WORKFLOW)
            for pattern in (patterns or (WORKFLOW_SELECTION,))
        ),
    )


def bare_repository_of(path: Path, files: Mapping[str, bytes]) -> str:
    """A bare repository carrying exactly these files at `refs/heads/main`."""

    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "--bare", "--quiet", "--initial-branch=main", ".")
    return commit_to(path, files)


def commit_to(path: Path, files: Mapping[str, bytes]) -> str:
    described = "".join(
        f"100644 {_git(path, 'hash-object', '-w', '--stdin', stdin=content)}\t{name}\n"
        for name, content in files.items()
    )
    (path / "scenario.index").unlink(missing_ok=True)
    _git(path, "update-index", "--index-info", stdin=described.encode("utf-8"))
    commit = _git(path, "commit-tree", _git(path, "write-tree"), "-m", "scenario")
    _git(path, "update-ref", MAIN, commit)
    return commit


def _git(path: Path, *arguments: str, stdin: bytes = b"") -> str:
    completed = subprocess.run(
        ("git", *arguments),
        cwd=path,
        env={
            **os.environ,
            **_AUTHORED_BY_THE_SCENARIO,
            "GIT_DIR": str(path),
            "GIT_INDEX_FILE": str(path / "scenario.index"),
        },
        input=stdin,
        capture_output=True,
        check=True,
    )
    return completed.stdout.decode("utf-8").strip()


def authored_workflows() -> Mapping[str, bytes]:
    """Every workflow this repository authors, as the source would serve them."""

    return {
        f"workflows/{authored.name}": authored.read_bytes()
        for authored in sorted((REPOSITORY_ROOT / "workflows").glob("*.yaml"))
    }


def logical_dump(database: Path) -> tuple[str, ...]:
    with sqlite3.connect(database) as connection:
        return tuple(connection.iterdump())


def stored_intake(
    engine: Engine,
    configuration: DefinitionSourceConfiguration,
    path: str,
    document: bytes,
    intake_number: int,
    commit: str = "a" * 40,
    intaken_at: str = "2026-09-02T00:00:00Z",
) -> None:
    """One provenance row as the intake operation writes it.

    Written here rather than through the intake door where a scenario needs a
    delivery that door cannot reach today -- two sources delivering one
    revision -- and where only the row itself is what a reader is asked about.
    """

    published = PublishedRevisionHash.of(document)
    with engine.begin() as connection:
        connection.execute(
            published_revisions.insert()
            .prefix_with("OR IGNORE")
            .values(
                kind=RevisionKind.WORKFLOW.value,
                revision_hash=published.value,
                document=document,
            )
        )
        connection.execute(
            catalog_source_intakes.insert().values(
                source_id=configuration.source_id.value,
                source_path=path,
                intake_number=intake_number,
                revision_kind=RevisionKind.WORKFLOW.value,
                revision_hash=published.value,
                source_commit=commit,
                intaken_by="felix",
                intaken_at=intaken_at,
            )
        )


def test_a_connected_source_and_its_selections_survive_a_new_store_reader(
    engine: Engine, database: Path
) -> None:
    configured = registration("workflows/*.yaml", "flows/*.yaml")

    stored = DbosDefinitionSources(engine).register(configured)
    engine.dispose()

    assert stored == DefinitionSourceRegistered(DefinitionSourceRevision(configured, 1))
    reopened = create_canonical_engine(database)
    try:
        read = DbosDefinitionSources(reopened).read_source(configured.source_id)
    finally:
        reopened.dispose()
    assert read == DefinitionSourceFound(DefinitionSourceRevision(configured, 1))


def test_connecting_the_same_source_again_keeps_one_source_and_one_revision(
    engine: Engine,
) -> None:
    sources = DbosDefinitionSources(engine)
    sources.register(registration())

    again = sources.register(registration())

    assert again == DefinitionSourceUnchanged(
        DefinitionSourceRevision(registration(), 1)
    )
    assert _recorded_revisions(engine) == 1


def test_the_standing_selection_set_registered_again_adds_no_revision(
    engine: Engine,
) -> None:
    """The number a configuration stands under is the store's, never a caller's.

    Widening and then narrowing back is the case a caller-numbered revision got
    wrong: the second registration of the original claim would hash under the
    number the caller happened to name and be appended a third time.
    """

    sources = DbosDefinitionSources(engine)
    sources.register(registration())
    sources.register(registration("workflows/*.yaml", "flows/*.yaml"))

    again = sources.register(registration("flows/*.yaml", "workflows/*.yaml"))

    assert isinstance(again, DefinitionSourceUnchanged)
    assert again.revision.revision_number == 2
    assert _recorded_revisions(engine) == 2


def _recorded_revisions(engine: Engine) -> int:
    with engine.connect() as connection:
        return connection.execute(
            sa.select(sa.func.count()).select_from(host_definition_source_revisions)
        ).scalar_one()


def test_a_changed_selection_set_appends_a_revision_of_the_same_source(
    engine: Engine,
) -> None:
    sources = DbosDefinitionSources(engine)
    sources.register(registration())

    widened = sources.register(registration("workflows/*.yaml", "flows/*.yaml"))

    assert isinstance(widened, DefinitionSourceRegistered)
    assert widened.revision.configuration.source_id == registration().source_id
    assert widened.revision.revision_number == 2
    assert sources.read_source(registration().source_id) == DefinitionSourceFound(
        widened.revision
    )


def test_a_source_nobody_registered_is_missing(engine: Engine) -> None:
    unknown = DefinitionSourceId("f" * 64)

    assert DbosDefinitionSources(engine).read_source(unknown) == (
        DefinitionSourceMissing(unknown)
    )


def test_the_latest_intake_of_a_path_is_the_highest_intake_number(
    engine: Engine,
) -> None:
    configured = registration()
    DbosDefinitionSources(engine).register(configured)
    stored_intake(engine, configured, "workflows/build.yaml", b"first bytes", 1)
    stored_intake(engine, configured, "workflows/build.yaml", b"second bytes", 2)
    stored_intake(engine, configured, "workflows/ship.yaml", b"ship bytes", 1)

    latest = DbosDefinitionSources(engine).latest_intakes(configured.source_id)

    assert isinstance(latest, Mapping)
    assert {
        path.value: (intake.intake_number, intake.revision_hash)
        for path, intake in latest.items()
    } == {
        "workflows/build.yaml": (2, PublishedRevisionHash.of(b"second bytes")),
        "workflows/ship.yaml": (1, PublishedRevisionHash.of(b"ship bytes")),
    }
    assert latest[RepositoryPath("workflows/build.yaml")].source_commit == (
        SourceCommit("a" * 40)
    )


@pytest.mark.parametrize(
    "table",
    [
        host_definition_source_revisions,
        host_definition_source_selections,
        catalog_source_intakes,
    ],
    ids=lambda table: str(table.name),
)
def test_what_a_definition_source_recorded_cannot_be_edited_or_removed(
    engine: Engine, table: sa.Table
) -> None:
    configured = registration()
    DbosDefinitionSources(engine).register(configured)
    stored_intake(engine, configured, "workflows/build.yaml", b"bytes", 1)

    for statement in (table.delete(), table.update().values(revision_hash="0" * 64)):
        with pytest.raises(IntegrityError, match="immutable"), engine.begin() as opened:
            opened.execute(statement)


def test_the_command_connects_a_repository_and_scans_every_workflow_in_it(
    tmp_path: Path, database: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    authored = authored_workflows()
    repository = tmp_path / "definitions.git"
    commit = bare_repository_of(repository, {**authored, "README.md": b"not selected"})

    assert (
        main(
            [
                "definition-source",
                "connect",
                "--database",
                str(database),
                "--location",
                str(repository),
                "--ref",
                MAIN,
                "--select",
                f"{WORKFLOW_SELECTION}=workflow",
                "--actor",
                "felix",
            ]
        )
        == 0
    )
    source_id = registration(location=str(repository)).source_id.value
    assert source_id in capsys.readouterr().out
    before = logical_dump(database)

    assert (
        main(
            [
                "definition-source",
                "scan",
                "--database",
                str(database),
                "--source-id",
                source_id,
            ]
        )
        == 0
    )

    scanned = capsys.readouterr().out
    assert commit in scanned
    assert [line.split() for line in scanned.splitlines()[1:]] == [
        ["source_ahead", "workflow", path] for path in sorted(authored)
    ]
    assert logical_dump(database) == before


def test_a_moved_ref_changes_the_commit_the_command_reports(
    tmp_path: Path, database: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = tmp_path / "definitions.git"
    document = (REPOSITORY_ROOT / "workflows" / "hello-atelier.yaml").read_bytes()
    first = bare_repository_of(repository, {"workflows/hello.yaml": document})
    main(
        [
            "definition-source",
            "connect",
            "--database",
            str(database),
            "--location",
            str(repository),
            "--ref",
            MAIN,
            "--select",
            f"{WORKFLOW_SELECTION}=workflow",
            "--actor",
            "felix",
        ]
    )
    source_id = registration(location=str(repository)).source_id.value
    assert source_id in capsys.readouterr().out
    scan = [
        "definition-source",
        "scan",
        "--database",
        str(database),
        "--source-id",
        source_id,
    ]
    assert main(scan) == 0
    standing = capsys.readouterr().out

    second = commit_to(repository, {"workflows/hello.yaml": document + b"\n"})
    assert main(scan) == 0

    moved = capsys.readouterr().out
    assert first != second
    assert first in standing
    assert second in moved


def test_the_command_refuses_to_register_a_location_that_is_no_repository(
    tmp_path: Path, database: Path, engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    """There is no disconnect yet, so a wire to nowhere is never recorded."""

    (tmp_path / "not-a-repository").mkdir()

    exit_code = main(
        [
            "definition-source",
            "connect",
            "--database",
            str(database),
            "--location",
            str(tmp_path / "not-a-repository"),
            "--ref",
            MAIN,
            "--select",
            f"{WORKFLOW_SELECTION}=workflow",
            "--actor",
            "felix",
        ]
    )

    assert exit_code == 1
    assert "definition_source_unreachable" in capsys.readouterr().err
    assert _recorded_revisions(engine) == 0


def test_the_command_refuses_to_register_a_ref_that_resolves_nowhere(
    tmp_path: Path, database: Path, engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = tmp_path / "definitions.git"
    bare_repository_of(repository, {"workflows/build.yaml": b"name: build\n"})

    exit_code = main(
        [
            "definition-source",
            "connect",
            "--database",
            str(database),
            "--location",
            str(repository),
            "--ref",
            "refs/heads/absent",
            "--select",
            f"{WORKFLOW_SELECTION}=workflow",
            "--actor",
            "felix",
        ]
    )

    assert exit_code == 1
    assert "definition_source_ref_unresolved" in capsys.readouterr().err
    assert _recorded_revisions(engine) == 0


def test_the_command_registers_a_source_whose_selections_match_nothing_yet(
    tmp_path: Path, database: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Connect answers for the location and the ref; selections show at scan."""

    repository = tmp_path / "definitions.git"
    bare_repository_of(repository, {"README.md": b"no workflow here"})

    assert (
        main(
            [
                "definition-source",
                "connect",
                "--database",
                str(database),
                "--location",
                str(repository),
                "--ref",
                MAIN,
                "--select",
                f"{WORKFLOW_SELECTION}=workflow",
                "--actor",
                "felix",
            ]
        )
        == 0
    )
    capsys.readouterr()

    exit_code = main(
        [
            "definition-source",
            "scan",
            "--database",
            str(database),
            "--source-id",
            registration(location=str(repository)).source_id.value,
        ]
    )

    assert exit_code == 1
    assert "definition_source_no_selected_files" in capsys.readouterr().err


def test_the_command_names_the_publication_refusal_a_selected_file_earns(
    tmp_path: Path, database: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The scan repeats the publication door's own token, not a paraphrase."""

    repository = tmp_path / "definitions.git"
    bare_repository_of(
        repository,
        {"workflows/broken.yaml": b"format_version: 3\nname: b\nnodes: []\n"},
    )
    assert (
        main(
            [
                "definition-source",
                "connect",
                "--database",
                str(database),
                "--location",
                str(repository),
                "--ref",
                MAIN,
                "--select",
                f"{WORKFLOW_SELECTION}=workflow",
                "--actor",
                "felix",
            ]
        )
        == 0
    )
    capsys.readouterr()

    exit_code = main(
        [
            "definition-source",
            "scan",
            "--database",
            str(database),
            "--source-id",
            registration(location=str(repository)).source_id.value,
        ]
    )

    assert exit_code == 1
    refused = capsys.readouterr().err
    assert "workflows/broken.yaml" in refused
    assert WorkflowRefusalReason.INVALID_VALUE.value in refused
    assert "'nodes'" in refused


def test_the_command_refuses_a_source_id_nobody_registered(
    database: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(
        [
            "definition-source",
            "scan",
            "--database",
            str(database),
            "--source-id",
            "f" * 64,
        ]
    )

    assert exit_code == 1
    assert "no definition source is registered" in capsys.readouterr().err


def connect(database: Path, repository: Path) -> str:
    """The source id of a repository this scenario just connected."""

    assert (
        main(
            [
                "definition-source",
                "connect",
                "--database",
                str(database),
                "--location",
                str(repository),
                "--ref",
                MAIN,
                "--select",
                f"{WORKFLOW_SELECTION}=workflow",
                "--actor",
                "felix",
            ]
        )
        == 0
    )
    return registration(location=str(repository)).source_id.value


def intake(database: Path, source_id: str, *at_position: str) -> int:
    return main(
        [
            "definition-source",
            "intake",
            "--database",
            str(database),
            "--source-id",
            source_id,
            "--actor",
            "felix",
            *(("--source-position", *at_position) if at_position else ()),
        ]
    )


def recorded_intakes(engine: Engine) -> list[tuple[str, str, int, str, str]]:
    with engine.connect() as connection:
        return [
            (
                str(record["source_id"]),
                str(record["source_path"]),
                int(record["intake_number"]),
                str(record["revision_hash"]),
                str(record["source_commit"]),
            )
            for record in connection.execute(
                sa.select(catalog_source_intakes).order_by(
                    catalog_source_intakes.c.source_path,
                    catalog_source_intakes.c.intake_number,
                )
            ).mappings()
        ]


def lineage_members(engine: Engine, name: str) -> list[str]:
    """Every revision of the lineage holding one authored name, oldest first."""

    with engine.connect() as connection:
        return [
            str(record["revision_hash"])
            for record in connection.execute(
                sa.select(catalog_lineage_members)
                .select_from(
                    catalog_lineage_members.join(
                        catalog_lineage_aliases,
                        catalog_lineage_aliases.c.lineage_id
                        == catalog_lineage_members.c.lineage_id,
                    )
                )
                .where(catalog_lineage_aliases.c.name == name)
                .order_by(catalog_lineage_members.c.revision_number)
                .distinct()
            ).mappings()
        ]


def workflow_named(name: str) -> bytes:
    """One workflow this scenario authors, under a name it chooses."""

    return f"""format_version: 3
name: {name}
nodes:
  - id: only
    type: agent
    role: builder
    mode: headless
    instruction: Do the one thing this workflow is for.
{declared_output().decode("utf-8")}""".encode()


def test_one_intake_takes_every_authored_workflow_in_with_its_provenance(
    tmp_path: Path, database: Path, engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    """The dogfood: this repository's own workflows, in one act, with their source."""

    authored = authored_workflows()
    repository = tmp_path / "definitions.git"
    commit = bare_repository_of(repository, {**authored, "README.md": b"not selected"})
    source_id = connect(database, repository)
    capsys.readouterr()

    assert intake(database, source_id) == 0

    reported = capsys.readouterr().out
    assert commit in reported
    assert [line.split() for line in reported.splitlines()[1:]] == [
        ["published", "workflow", path] for path in sorted(authored)
    ]
    assert recorded_intakes(engine) == [
        (
            source_id,
            path,
            1,
            PublishedRevisionHash.of(document).value,
            commit,
        )
        for path, document in sorted(authored.items())
    ]
    assert set(_published_workflow_hashes(engine)) == {
        PublishedRevisionHash.of(document).value for document in authored.values()
    }


def _published_workflow_hashes(engine: Engine) -> list[str]:
    with engine.connect() as connection:
        return [
            str(record)
            for record in connection.execute(
                sa.select(published_revisions.c.revision_hash).where(
                    published_revisions.c.kind == RevisionKind.WORKFLOW.value
                )
            ).scalars()
        ]


def test_the_same_commit_taken_in_again_writes_nothing_and_says_so(
    tmp_path: Path, database: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = tmp_path / "definitions.git"
    bare_repository_of(repository, {"workflows/build.yaml": workflow_named("build")})
    source_id = connect(database, repository)
    assert intake(database, source_id) == 0
    capsys.readouterr()
    settled = logical_dump(database)

    assert intake(database, source_id) == 0

    assert [line.split() for line in capsys.readouterr().out.splitlines()[1:]] == [
        ["present", "workflow", "workflows/build.yaml"]
    ]
    assert logical_dump(database) == settled


def test_a_moved_ref_takes_the_new_revision_in_and_keeps_the_one_before_it(
    tmp_path: Path, database: Path, engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    """`#660` ruled line 7: what came in stays what it was."""

    repository = tmp_path / "definitions.git"
    first_document = workflow_named("build")
    first = bare_repository_of(repository, {"workflows/build.yaml": first_document})
    source_id = connect(database, repository)
    assert intake(database, source_id) == 0
    second_document = first_document + b"\n"
    second = commit_to(repository, {"workflows/build.yaml": second_document})
    capsys.readouterr()

    assert intake(database, source_id) == 0

    assert [line.split() for line in capsys.readouterr().out.splitlines()[1:]] == [
        ["published", "workflow", "workflows/build.yaml"]
    ]
    assert recorded_intakes(engine) == [
        (
            source_id,
            "workflows/build.yaml",
            1,
            PublishedRevisionHash.of(first_document).value,
            first,
        ),
        (
            source_id,
            "workflows/build.yaml",
            2,
            PublishedRevisionHash.of(second_document).value,
            second,
        ),
    ]
    assert lineage_members(engine, "build") == [
        PublishedRevisionHash.of(first_document).value,
        PublishedRevisionHash.of(second_document).value,
    ]


def test_an_intake_refused_at_its_last_file_leaves_the_store_byte_identical(
    tmp_path: Path, database: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A batch is one act: the files admitted before the refusal go back too."""

    held = tmp_path / "held.git"
    bare_repository_of(held, {"workflows/build.yaml": workflow_named("build")})
    assert intake(database, connect(database, held)) == 0
    colliding = tmp_path / "colliding.git"
    bare_repository_of(
        colliding,
        {
            "workflows/a-first.yaml": workflow_named("ship"),
            "workflows/z-last.yaml": workflow_named("build") + b"\n",
        },
    )
    source_id = connect(database, colliding)
    capsys.readouterr()
    settled = logical_dump(database)

    assert intake(database, source_id) == 1

    refused = capsys.readouterr().err
    assert refused.startswith("refused workflows/z-last.yaml")
    assert "'build'" in refused
    assert logical_dump(database) == settled


def test_a_name_the_catalog_cannot_hold_refuses_before_anything_is_written(
    tmp_path: Path, database: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = tmp_path / "definitions.git"
    bare_repository_of(
        repository,
        {
            "workflows/build.yaml": workflow_named("build"),
            "workflows/shout.yaml": workflow_named("SHOUT"),
        },
    )
    source_id = connect(database, repository)
    capsys.readouterr()
    settled = logical_dump(database)

    assert intake(database, source_id) == 1

    refused = capsys.readouterr().err
    assert refused.startswith("refused workflows/shout.yaml")
    assert "'SHOUT'" in refused
    assert logical_dump(database) == settled


def test_an_intake_refuses_a_commit_the_ref_has_moved_away_from(
    tmp_path: Path, database: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The operator takes in the commit a scan showed them, not whatever is there now."""

    repository = tmp_path / "definitions.git"
    document = workflow_named("build")
    scanned = bare_repository_of(repository, {"workflows/build.yaml": document})
    source_id = connect(database, repository)
    moved = commit_to(repository, {"workflows/build.yaml": document + b"\n"})
    capsys.readouterr()
    settled = logical_dump(database)

    assert intake(database, source_id, scanned) == 1

    refused = capsys.readouterr().err
    assert scanned in refused
    assert moved in refused
    assert logical_dump(database) == settled


def test_bytes_the_catalog_holds_under_another_name_refuse_the_whole_batch(
    tmp_path: Path, database: Path, engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    """Identical bytes under a foreign name are somebody else's entry, not a delivery."""

    elsewhere = workflow_named("alpha")
    assert isinstance(
        DbosCatalogStore(engine).add_workflow(
            WorkflowRevision(elsewhere),
            CatalogLineageDisplayName("elsewhere"),
            CatalogActor("felix"),
            CatalogActivatedAt("2026-09-03T08:00:00Z"),
        ),
        CatalogLineageFounded,
    )
    repository = tmp_path / "definitions.git"
    bare_repository_of(
        repository,
        {
            "workflows/a-first.yaml": workflow_named("ship"),
            "workflows/z-last.yaml": elsewhere,
        },
    )
    source_id = connect(database, repository)
    capsys.readouterr()
    settled = logical_dump(database)

    assert intake(database, source_id) == 1

    refused = capsys.readouterr().err
    assert refused.startswith("refused workflows/z-last.yaml")
    assert "already belong to lineage" in refused
    assert logical_dump(database) == settled


def test_a_manually_imported_name_is_adopted_as_the_sources_new_head(
    tmp_path: Path, database: Path, engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    """`#660` A3: the live deploy-intake belief this slice fixes.

    Every repository workflow whose name a manual import once typed by hand
    was refused; now the source revision becomes that lineage's new head, the
    manual revision stays its history, and the name never changes lineage.
    """

    manual = workflow_named("build")
    assert isinstance(
        DbosCatalogStore(engine).add_workflow(
            WorkflowRevision(manual),
            CatalogLineageDisplayName("build"),
            CatalogActor("felix"),
            CatalogActivatedAt("2026-09-01T00:00:00Z"),
        ),
        CatalogLineageFounded,
    )
    repository = tmp_path / "definitions.git"
    delivered = workflow_named("build") + b"\n"
    commit = bare_repository_of(repository, {"workflows/build.yaml": delivered})
    source_id = connect(database, repository)
    capsys.readouterr()

    assert intake(database, source_id) == 0

    manual_lineage = CatalogLineage(
        RevisionKind.WORKFLOW, PublishedRevisionHash.of(manual)
    ).lineage_id.value

    reported = capsys.readouterr().out
    assert commit in reported
    assert reported.splitlines()[1:] == [
        f"  published workflow workflows/build.yaml (adopted lineage {manual_lineage})"
    ]
    assert lineage_members(engine, "build") == [
        PublishedRevisionHash.of(manual).value,
        PublishedRevisionHash.of(delivered).value,
    ]
    assert recorded_intakes(engine) == [
        (
            source_id,
            "workflows/build.yaml",
            1,
            PublishedRevisionHash.of(delivered).value,
            commit,
        )
    ]
    provenance = listed_provenance(engine)
    assert provenance[PublishedRevisionHash.of(delivered).value] == RevisionProvenance(
        DefinitionSourceId(source_id),
        SourceCommit(commit),
        RepositoryPath("workflows/build.yaml"),
        CatalogActivatedAt(recorded_instant(engine, "workflows/build.yaml")),
    )
    assert provenance[PublishedRevisionHash.of(manual).value] is None


def test_a_first_intake_byte_identical_to_a_manual_founding_revision_gains_provenance(
    tmp_path: Path, database: Path, engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    """The founding-revision half of `#660` A3's coverage: present still records.

    Byte-identical to a manually imported lineage's own founding revision,
    under its own name, changes nothing in the catalog -- but this source has
    still taken the path in for the first time. Recording that provenance
    matters beyond the described view: without it the lineage still looks
    unsourced to the next name-held check, and a second source delivering a
    genuinely different revision under this name would wrongly adopt it as an
    unclaimed manual import instead of refusing.
    """

    manual = workflow_named("build")
    assert isinstance(
        DbosCatalogStore(engine).add_workflow(
            WorkflowRevision(manual),
            CatalogLineageDisplayName("build"),
            CatalogActor("felix"),
            CatalogActivatedAt("2026-09-01T00:00:00Z"),
        ),
        CatalogLineageFounded,
    )
    repository = tmp_path / "definitions.git"
    commit = bare_repository_of(repository, {"workflows/build.yaml": manual})
    source_id = connect(database, repository)
    capsys.readouterr()

    assert intake(database, source_id) == 0

    reported = capsys.readouterr().out
    assert commit in reported
    assert reported.splitlines()[1:] == ["  present workflow workflows/build.yaml"]
    assert recorded_intakes(engine) == [
        (
            source_id,
            "workflows/build.yaml",
            1,
            PublishedRevisionHash.of(manual).value,
            commit,
        )
    ]
    provenance = listed_provenance(engine)
    assert provenance[PublishedRevisionHash.of(manual).value] == RevisionProvenance(
        DefinitionSourceId(source_id),
        SourceCommit(commit),
        RepositoryPath("workflows/build.yaml"),
        CatalogActivatedAt(recorded_instant(engine, "workflows/build.yaml")),
    )

    second_repository = tmp_path / "second.git"
    bare_repository_of(second_repository, {"workflows/build.yaml": manual + b"\n"})
    second_source_id = connect(database, second_repository)
    capsys.readouterr()
    settled = logical_dump(database)

    assert intake(database, second_source_id) == 1

    refused = capsys.readouterr().err
    assert refused.startswith("refused workflows/build.yaml")
    assert "already held by lineage" in refused
    assert logical_dump(database) == settled


def test_bytes_a_manual_import_holds_as_a_later_member_are_adopted_as_present(
    tmp_path: Path, database: Path, engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    """`#660` A3's other sibling: a later, unsourced revision of the path's own name.

    The name-held case adopts a manual import's *founding* revision as a
    lineage's new head. This is the same belief reached from the other side:
    the repository's bytes already sit inside that unsourced lineage one
    member down from the top, so `found_lineage_in` answers
    `CatalogAdmissionRevisionOwned` before a name is ever consulted. That
    answer must still resolve to the path's own lineage, not a foreign one,
    or every later manual revision would wrongly refuse the whole batch.
    """

    store = DbosCatalogStore(engine)
    founding = workflow_named("build")
    assert isinstance(
        store.add_workflow(
            WorkflowRevision(founding),
            CatalogLineageDisplayName("build"),
            CatalogActor("felix"),
            CatalogActivatedAt("2026-09-01T00:00:00Z"),
        ),
        CatalogLineageFounded,
    )
    later = founding + b"\n"
    DbosWorkflowRevisionPublisher(engine).publish(WorkflowRevision(later))
    lineage = CatalogLineage(
        RevisionKind.WORKFLOW, PublishedRevisionHash.of(founding)
    ).lineage_id
    assert isinstance(
        store.admit_member(
            lineage,
            PublishedRevision(RevisionKind.WORKFLOW, later),
            CatalogLineageDisplayName("build"),
            CatalogActor("felix"),
            CatalogActivatedAt("2026-09-01T00:05:00Z"),
        ),
        CatalogMemberAdmitted,
    )
    repository = tmp_path / "definitions.git"
    commit = bare_repository_of(repository, {"workflows/build.yaml": later})
    source_id = connect(database, repository)
    capsys.readouterr()

    assert intake(database, source_id) == 0

    reported = capsys.readouterr().out
    assert commit in reported
    assert reported.splitlines()[1:] == ["  present workflow workflows/build.yaml"]
    assert lineage_members(engine, "build") == [
        PublishedRevisionHash.of(founding).value,
        PublishedRevisionHash.of(later).value,
    ]
    assert recorded_intakes(engine) == [
        (
            source_id,
            "workflows/build.yaml",
            1,
            PublishedRevisionHash.of(later).value,
            commit,
        )
    ]
    provenance = listed_provenance(engine)
    assert provenance[PublishedRevisionHash.of(later).value] == RevisionProvenance(
        DefinitionSourceId(source_id),
        SourceCommit(commit),
        RepositoryPath("workflows/build.yaml"),
        CatalogActivatedAt(recorded_instant(engine, "workflows/build.yaml")),
    )


def test_a_name_a_different_source_already_intook_still_refuses(
    tmp_path: Path, database: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A lineage a source already feeds keeps its refusal for a second source."""

    first_repository = tmp_path / "first.git"
    bare_repository_of(
        first_repository, {"workflows/build.yaml": workflow_named("build")}
    )
    assert intake(database, connect(database, first_repository)) == 0

    second_repository = tmp_path / "second.git"
    bare_repository_of(
        second_repository,
        {"workflows/build.yaml": workflow_named("build") + b"\n"},
    )
    source_id = connect(database, second_repository)
    capsys.readouterr()
    settled = logical_dump(database)

    assert intake(database, source_id) == 1

    refused = capsys.readouterr().err
    assert refused.startswith("refused workflows/build.yaml")
    assert "already held by lineage" in refused
    assert logical_dump(database) == settled


def test_a_name_a_different_source_already_intook_still_refuses_byte_identical_content(
    tmp_path: Path, database: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The byte-identical twin of the refusal above.

    There a second source's *differing* bytes hit `CatalogAdmissionNameHeld`.
    Bytes byte-identical to the lineage's own founding revision reach the
    same refusal through a different door -- `CatalogAdmissionExisting` --
    and must refuse for the same reason: a source has already fed this
    lineage, so a second source recognising it present would attach its own
    provenance to somebody else's continuity, and a later, differing delivery
    from that second source could then re-head the lineage as if it always
    owned it.
    """

    first_repository = tmp_path / "first.git"
    bare_repository_of(
        first_repository, {"workflows/build.yaml": workflow_named("build")}
    )
    assert intake(database, connect(database, first_repository)) == 0

    second_repository = tmp_path / "second.git"
    bare_repository_of(
        second_repository, {"workflows/build.yaml": workflow_named("build")}
    )
    source_id = connect(database, second_repository)
    capsys.readouterr()
    settled = logical_dump(database)

    assert intake(database, source_id) == 1

    refused = capsys.readouterr().err
    assert refused.startswith("refused workflows/build.yaml")
    assert "already held by lineage" in refused
    assert logical_dump(database) == settled


def _founded_three_member_lineage(engine: Engine) -> tuple[bytes, bytes, bytes]:
    """An unsourced, manually imported lineage with three revisions.

    Founding, then a middle and a head revision admitted the same way a
    manual import stands before any source has ever fed it -- built once so
    the two "present must agree with the served head" tests below share one
    arrangement and differ only in which revision the repository delivers.
    """

    store = DbosCatalogStore(engine)
    founding = workflow_named("build")
    assert isinstance(
        store.add_workflow(
            WorkflowRevision(founding),
            CatalogLineageDisplayName("build"),
            CatalogActor("felix"),
            CatalogActivatedAt("2026-09-01T00:00:00Z"),
        ),
        CatalogLineageFounded,
    )
    lineage = CatalogLineage(
        RevisionKind.WORKFLOW, PublishedRevisionHash.of(founding)
    ).lineage_id
    middle = founding + b"\n"
    newest = founding + b"\n\n"
    for ordinal, revision in enumerate((middle, newest), start=1):
        DbosWorkflowRevisionPublisher(engine).publish(WorkflowRevision(revision))
        assert isinstance(
            store.admit_member(
                lineage,
                PublishedRevision(RevisionKind.WORKFLOW, revision),
                CatalogLineageDisplayName("build"),
                CatalogActor("felix"),
                CatalogActivatedAt(f"2026-09-01T00:0{ordinal + 5}:00Z"),
            ),
            CatalogMemberAdmitted,
        )
    return founding, middle, newest


def _served_head(engine: Engine, name: str) -> CatalogNameFound:
    served = DbosCatalogStore(engine).resolve_name(
        RevisionKind.WORKFLOW, CatalogLineageDisplayName(name), "head"
    )
    assert isinstance(served, CatalogNameFound)
    return served


def test_a_present_answer_still_serves_the_lineages_current_head(
    tmp_path: Path, database: Path, engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    """Recognising bytes present must agree with what the name actually serves.

    Delivering the lineage's own current head as an unsourced later member
    still answers present and records its provenance -- and the name still
    resolves to that exact head afterwards, unmoved by the recognition.
    """

    _, _, newest = _founded_three_member_lineage(engine)
    repository = tmp_path / "definitions.git"
    commit = bare_repository_of(repository, {"workflows/build.yaml": newest})
    source_id = connect(database, repository)
    capsys.readouterr()

    assert intake(database, source_id) == 0

    reported = capsys.readouterr().out
    assert commit in reported
    assert reported.splitlines()[1:] == ["  present workflow workflows/build.yaml"]
    assert _served_head(engine, "build").revision_hash == PublishedRevisionHash.of(
        newest
    )


def test_repo_bytes_matching_a_non_head_member_refuse_instead_of_present(
    tmp_path: Path, database: Path, engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    """A stale, non-head revision is refused rather than reported present.

    The lineage has moved past this revision: the repository no longer
    carries what the name serves, so recognising it present would let a
    stale delivery answer for served bytes it is not. The catalog keeps
    serving its real head and records no provenance for the stale one.
    """

    _, middle, newest = _founded_three_member_lineage(engine)
    repository = tmp_path / "definitions.git"
    bare_repository_of(repository, {"workflows/build.yaml": middle})
    source_id = connect(database, repository)
    capsys.readouterr()
    settled = logical_dump(database)

    assert intake(database, source_id) == 1

    refused = capsys.readouterr().err
    assert refused.startswith("refused workflows/build.yaml")
    assert "already belong to lineage" in refused
    assert logical_dump(database) == settled
    assert _served_head(engine, "build").revision_hash == PublishedRevisionHash.of(
        newest
    )


def test_an_adopted_lineage_intake_is_idempotent_on_a_repeat_commit(
    tmp_path: Path, database: Path, engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    manual = workflow_named("build")
    assert isinstance(
        DbosCatalogStore(engine).add_workflow(
            WorkflowRevision(manual),
            CatalogLineageDisplayName("build"),
            CatalogActor("felix"),
            CatalogActivatedAt("2026-09-01T00:00:00Z"),
        ),
        CatalogLineageFounded,
    )
    repository = tmp_path / "definitions.git"
    bare_repository_of(
        repository, {"workflows/build.yaml": workflow_named("build") + b"\n"}
    )
    source_id = connect(database, repository)
    assert intake(database, source_id) == 0
    capsys.readouterr()
    settled = logical_dump(database)

    assert intake(database, source_id) == 0

    assert [line.split() for line in capsys.readouterr().out.splitlines()[1:]] == [
        ["present", "workflow", "workflows/build.yaml"]
    ]
    assert logical_dump(database) == settled


def described_page(engine: Engine, limit: int = 50) -> DescribedWorkflowRevisionPage:
    listed = durable_queries(engine).list_described_workflow_revisions(
        None, limit, PAGE_BUDGET
    )
    assert isinstance(listed, DescribedWorkflowRevisionPage)
    return listed


def recorded_instant(engine: Engine, path: str) -> str:
    """When the store says one path was taken in, in its own words."""

    with engine.connect() as connection:
        return str(
            connection.execute(
                sa.select(catalog_source_intakes.c.intaken_at).where(
                    catalog_source_intakes.c.source_path == path
                )
            ).scalar_one()
        )


def listed_provenance(engine: Engine) -> dict[str, RevisionProvenance | None]:
    """Where each listed revision came from, as the described catalog answers it."""

    return {
        listed.projection.revision.revision_hash.value: listed.provenance
        for listed in described_page(engine).items
    }


class ExecutedStatements:
    """Every statement a real engine's cursor ran while this was listening."""

    def __init__(self, engine: Engine) -> None:
        self.statements: list[str] = []
        event.listens_for(engine, "before_cursor_execute")(self._record)

    def _record(
        self, _connection: object, _cursor: object, statement: str, *_rest: object
    ) -> None:
        self.statements.append(statement)

    @property
    def selects(self) -> tuple[str, ...]:
        return tuple(
            statement
            for statement in self.statements
            if statement.lstrip().upper().startswith("SELECT")
        )


def test_the_described_catalog_names_the_source_an_intaken_revision_came_from(
    tmp_path: Path, database: Path, engine: Engine
) -> None:
    """Whoever pulled a piece out of a source sees that on the revision itself."""

    document = workflow_named("build")
    repository = tmp_path / "definitions.git"
    commit = bare_repository_of(repository, {"workflows/build.yaml": document})
    source_id = connect(database, repository)
    assert intake(database, source_id) == 0
    authored_here = WorkflowRevision(workflow_named("authored-here"))
    DbosWorkflowRevisionPublisher(engine).publish(authored_here)

    provenance = listed_provenance(engine)

    assert provenance[WorkflowRevision(document).revision_hash.value] == (
        RevisionProvenance(
            DefinitionSourceId(source_id),
            SourceCommit(commit),
            RepositoryPath("workflows/build.yaml"),
            CatalogActivatedAt(recorded_instant(engine, "workflows/build.yaml")),
        )
    )
    assert provenance[authored_here.revision_hash.value] is None


def test_the_first_intake_of_one_revision_is_the_origin_it_keeps(
    engine: Engine,
) -> None:
    """The same bytes may arrive twice; where they came from is where they began.

    Written straight into the store because the schema permits what the intake
    door does not reach today: one revision hash delivered by two sources, or at
    two paths. What the catalog read may never do is answer with whichever row
    the store happened to return first.
    """

    document = workflow_named("build")
    DbosWorkflowRevisionPublisher(engine).publish(WorkflowRevision(document))
    first = registration(location="/srv/first.git")
    second = registration(location="/srv/second.git")
    sources = DbosDefinitionSources(engine)
    assert isinstance(sources.register(first), DefinitionSourceRegistered)
    assert isinstance(sources.register(second), DefinitionSourceRegistered)
    stored_intake(
        engine,
        second,
        "flows/late.yaml",
        document,
        1,
        commit="b" * 40,
        intaken_at="2026-09-02T12:00:00Z",
    )
    stored_intake(
        engine,
        first,
        "workflows/early.yaml",
        document,
        1,
        commit="a" * 40,
        intaken_at="2026-09-02T08:00:00Z",
    )

    assert listed_provenance(engine) == {
        WorkflowRevision(document).revision_hash.value: RevisionProvenance(
            first.source_id,
            SourceCommit("a" * 40),
            RepositoryPath("workflows/early.yaml"),
            CatalogActivatedAt("2026-09-02T08:00:00Z"),
        )
    }


def test_the_same_commit_taken_in_again_leaves_the_origin_alone(
    tmp_path: Path, database: Path, engine: Engine
) -> None:
    repository = tmp_path / "definitions.git"
    bare_repository_of(repository, {"workflows/build.yaml": workflow_named("build")})
    source_id = connect(database, repository)
    assert intake(database, source_id) == 0
    after_the_first = listed_provenance(engine)

    assert intake(database, source_id) == 0

    assert listed_provenance(engine) == after_the_first


def test_one_revision_read_alone_names_the_origin_the_listing_names(
    tmp_path: Path, database: Path, engine: Engine
) -> None:
    """A detail read that never joined the origin would answer null where a list does not."""

    document = workflow_named("build")
    repository = tmp_path / "definitions.git"
    bare_repository_of(repository, {"workflows/build.yaml": document})
    assert intake(database, connect(database, repository)) == 0
    revision_hash = WorkflowRevision(document).revision_hash

    found = durable_queries(engine).get_workflow_revision(revision_hash)

    assert isinstance(found, WorkflowRevisionFound)
    assert found.provenance == listed_provenance(engine)[revision_hash.value]


def test_a_described_page_costs_one_statement_whatever_it_carries(
    tmp_path: Path, database: Path, engine: Engine
) -> None:
    """The origin travels on the page's own statement, not on a read per revision."""

    repository = tmp_path / "definitions.git"
    bare_repository_of(
        repository,
        {f"workflows/{name}.yaml": workflow_named(name) for name in ("a", "b", "c")},
    )
    assert intake(database, connect(database, repository)) == 0

    assert {limit: _selects_listing(engine, limit) for limit in (1, 3)} == {1: 1, 3: 1}


def _selects_listing(engine: Engine, limit: int) -> int:
    executed = ExecutedStatements(engine)
    assert len(described_page(engine, limit).items) == limit
    return len(executed.selects)
