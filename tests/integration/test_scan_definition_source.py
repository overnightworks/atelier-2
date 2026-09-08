"""Where a registered source stands, and that looking never changes anything."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass, field

import pytest

from atelier2.adapters.yaml_workflows import parse_workflow_document
from atelier2.application.publish_workflow_revision import WorkflowPublicationLimits
from atelier2.application.scan_definition_source import (
    DefinitionSourceScanned,
    DefinitionSourceUnknown,
    PathFreshness,
    ScanDefinitionSourceResult,
    ScannedDocumentInvalid,
    ScannedPath,
    ScanRefused,
    scan_definition_source,
)
from atelier2.contracts.definition_sources import (
    DefinitionSourceAccess,
    DefinitionSourceActor,
    DefinitionSourceConfiguration,
    DefinitionSourceId,
    DefinitionSourceKind,
    DefinitionSourceRefusal,
    DefinitionSourceRevision,
    DefinitionSourceSelection,
    RepositoryLocation,
    RepositoryPath,
    RepositoryRef,
    SelectionPattern,
    SourceCommit,
    SourceIntake,
)
from atelier2.contracts.revisions_v3 import PublishedRevisionHash, RevisionKind
from atelier2.ports.definition_sources import (
    DefinitionSourceFound,
    DefinitionSourceMissing,
    DefinitionSourceUnreadable,
    ReadDefinitionSourceResult,
    ReadSourceIntakesResult,
    ScannedSource,
    SelectedFile,
)
from tests.scenarios.workflows import declared_output

COMMIT = SourceCommit("c" * 40)
LATER_COMMIT = SourceCommit("d" * 40)
BUILD = RepositoryPath("workflows/build.yaml")
SHIP = RepositoryPath("workflows/ship.yaml")
LIMITS = WorkflowPublicationLimits(
    maximum_document_bytes=1_000_000,
    maximum_nodes=64,
    maximum_string_characters=4_096,
    maximum_payload_bytes=1_000_000,
)


def workflow_document(name: str) -> bytes:
    return (
        f"""format_version: 3
name: {name}
nodes:
  - id: only
    type: agent
    role: builder
    mode: headless
    instruction: Do the one thing this workflow is for.
""".encode()
        + declared_output()
    )


def published_hash(document: bytes) -> PublishedRevisionHash:
    return PublishedRevisionHash.of(document)


def registration(*patterns: str) -> DefinitionSourceConfiguration:
    return DefinitionSourceConfiguration(
        DefinitionSourceKind.GIT,
        RepositoryLocation("/srv/definitions.git"),
        RepositoryRef("refs/heads/main"),
        DefinitionSourceAccess.ANONYMOUS,
        DefinitionSourceActor("felix"),
        tuple(
            DefinitionSourceSelection(SelectionPattern(pattern), RevisionKind.WORKFLOW)
            for pattern in (patterns or ("workflows/*.yaml",))
        ),
    )


def intake(path: RepositoryPath, document: bytes, number: int = 1) -> SourceIntake:
    return SourceIntake(
        registration().source_id,
        path,
        number,
        RevisionKind.WORKFLOW,
        published_hash(document),
        COMMIT,
    )


@dataclass
class SourcesKeptInMemory:
    """The read-only durable side of a scan, and only that.

    It has no `register`, because the port a scan is handed has none: what
    proves the scan writes nothing is that nothing it holds can write.
    """

    registered: dict[str, DefinitionSourceRevision] = field(default_factory=dict)
    intaken: dict[str, dict[RepositoryPath, SourceIntake]] = field(default_factory=dict)

    def read_source(self, source_id: DefinitionSourceId) -> ReadDefinitionSourceResult:
        standing = self.registered.get(source_id.value)
        if standing is None:
            return DefinitionSourceMissing(source_id)
        return DefinitionSourceFound(standing)

    def latest_intakes(self, source_id: DefinitionSourceId) -> ReadSourceIntakesResult:
        return self.intaken.get(source_id.value, {})


@dataclass
class SourceReadingOnce:
    """A reader answering one prepared scan, or refusing with one named word."""

    scanned: ScannedSource | None = None
    refusal: DefinitionSourceRefusal | None = None

    def resolve(self, configuration: DefinitionSourceConfiguration) -> SourceCommit:
        return self.scan(configuration).commit

    def scan(self, configuration: DefinitionSourceConfiguration) -> ScannedSource:
        del configuration
        if self.refusal is not None:
            raise DefinitionSourceUnreadable(self.refusal, "as the scenario prepared")
        assert self.scanned is not None
        return self.scanned


def selected(
    configuration: DefinitionSourceConfiguration,
    files: Mapping[RepositoryPath, bytes],
) -> ScannedSource:
    return ScannedSource(
        COMMIT,
        tuple(
            SelectedFile(path, configuration.selections[0], document)
            for path, document in files.items()
        ),
    )


def scanning(
    sources: SourcesKeptInMemory, reader: SourceReadingOnce
) -> ScanDefinitionSourceResult:
    return scan_definition_source(
        registration().source_id, sources, reader, parse_workflow_document, LIMITS
    )


def connected(
    *, intakes: Mapping[RepositoryPath, SourceIntake] | None = None
) -> SourcesKeptInMemory:
    configured = registration()
    sources = SourcesKeptInMemory()
    sources.registered[configured.source_id.value] = DefinitionSourceRevision(
        configured, 1
    )
    sources.intaken[configured.source_id.value] = dict(intakes or {})
    return sources


def test_a_source_nobody_registered_is_unknown() -> None:
    result = scanning(SourcesKeptInMemory(), SourceReadingOnce())

    assert result == DefinitionSourceUnknown(registration().source_id)


def test_a_path_the_catalog_never_took_in_is_reported_as_source_ahead() -> None:
    document = workflow_document("build")
    reader = SourceReadingOnce(selected(registration(), {BUILD: document}))

    result = scanning(connected(), reader)

    assert isinstance(result, DefinitionSourceScanned)
    assert result.commit == COMMIT
    assert result.paths == (
        ScannedPath(
            BUILD,
            RevisionKind.WORKFLOW,
            PathFreshness.SOURCE_AHEAD,
            published_hash(document),
        ),
    )


def test_a_path_whose_latest_intake_holds_these_exact_bytes_is_in_sync() -> None:
    document = workflow_document("build")
    reader = SourceReadingOnce(selected(registration(), {BUILD: document}))

    result = scanning(connected(intakes={BUILD: intake(BUILD, document)}), reader)

    assert isinstance(result, DefinitionSourceScanned)
    assert result.paths[0].freshness is PathFreshness.IN_SYNC


def test_a_path_whose_latest_intake_holds_other_bytes_is_source_ahead() -> None:
    reader = SourceReadingOnce(
        selected(registration(), {BUILD: workflow_document("rebuilt")})
    )

    result = scanning(
        connected(intakes={BUILD: intake(BUILD, workflow_document("build"))}), reader
    )

    assert isinstance(result, DefinitionSourceScanned)
    assert result.paths[0].freshness is PathFreshness.SOURCE_AHEAD


def test_a_path_the_source_stopped_carrying_is_reported_without_a_hash() -> None:
    document = workflow_document("build")
    reader = SourceReadingOnce(selected(registration(), {BUILD: document}))

    result = scanning(
        connected(
            intakes={
                BUILD: intake(BUILD, document),
                SHIP: intake(SHIP, workflow_document("ship")),
            }
        ),
        reader,
    )

    assert isinstance(result, DefinitionSourceScanned)
    assert [
        (path.path.value, path.freshness, path.revision_hash) for path in result.paths
    ] == [
        (BUILD.value, PathFreshness.IN_SYNC, published_hash(document)),
        (SHIP.value, PathFreshness.SOURCE_ABSENT, None),
    ]


def test_every_reported_path_carries_the_kind_its_selection_configured() -> None:
    reader = SourceReadingOnce(
        selected(registration(), {BUILD: workflow_document("build")})
    )

    result = scanning(connected(), reader)

    assert isinstance(result, DefinitionSourceScanned)
    assert result.paths[0].kind is RevisionKind.WORKFLOW


def test_a_file_the_publication_door_refuses_stops_the_scan_at_that_path() -> None:
    reader = SourceReadingOnce(
        selected(
            registration(),
            {BUILD: b"format_version: 3\nname: build\nnodes: []\n"},
        )
    )

    result = scanning(connected(), reader)

    assert isinstance(result, ScannedDocumentInvalid)
    assert result.path == BUILD
    assert result.detail


def test_a_source_that_cannot_be_read_is_refused_in_its_own_words() -> None:
    reader = SourceReadingOnce(refusal=DefinitionSourceRefusal.REF_UNRESOLVED)

    result = scanning(connected(), reader)

    assert result == ScanRefused(
        DefinitionSourceRefusal.REF_UNRESOLVED, "as the scenario prepared"
    )


@pytest.mark.parametrize(
    "reader",
    [
        SourceReadingOnce(refusal=DefinitionSourceRefusal.UNREACHABLE),
        SourceReadingOnce(
            ScannedSource(
                LATER_COMMIT,
                (SelectedFile(BUILD, registration().selections[0], b"not a workflow"),),
            )
        ),
    ],
    ids=["refused", "invalid-document"],
)
def test_a_scan_leaves_the_store_exactly_as_it_found_it(
    reader: SourceReadingOnce,
) -> None:
    document = workflow_document("build")
    sources = connected(intakes={BUILD: intake(BUILD, document)})
    before = copy.deepcopy((sources.registered, sources.intaken))

    scanning(sources, reader)

    assert before == (sources.registered, sources.intaken)


def test_a_successful_scan_leaves_the_store_exactly_as_it_found_it() -> None:
    document = workflow_document("build")
    sources = connected(intakes={BUILD: intake(BUILD, document)})
    before = copy.deepcopy((sources.registered, sources.intaken))

    result = scanning(
        sources, SourceReadingOnce(selected(registration(), {BUILD: document}))
    )

    assert isinstance(result, DefinitionSourceScanned)
    assert before == (sources.registered, sources.intaken)
