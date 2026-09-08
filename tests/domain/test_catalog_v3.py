from __future__ import annotations

from typing import cast

import pytest

from atelier2.contracts.catalog_v3 import (
    CatalogActivatedAt,
    CatalogActor,
    CatalogLineage,
    CatalogLineageDisplayName,
    CatalogLineageId,
    CatalogRetirementState,
    catalog_lineage_query,
)
from atelier2.contracts.revisions_v3 import PublishedRevisionHash, RevisionKind

FOUNDING_REVISION_HASH = PublishedRevisionHash(
    "dbf022d2a6cddb05a22caf99140e9a856404c528ee2edca6537a76c19ce8655d"
)

LINEAGE_ID_VECTORS: tuple[tuple[RevisionKind, str], ...] = (
    (
        RevisionKind.WORKFLOW,
        "d392f5891160350355c3cb56a70c66afc725c19a5a39c7616198fcd3cf1e731e",
    ),
    (
        RevisionKind.SCHEMA,
        "3bbea9dce190030e6cec035c1202bb6b8fed1f54058c6e0ffb7835c7590cf18f",
    ),
    (
        RevisionKind.DETERMINISTIC_OPERATION,
        "9a25160d3a83c1a1fbb2a0d94b27ebcbe8d52922bd940b9751a0560030146631",
    ),
    (
        RevisionKind.ADAPTER_OPERATION,
        "0e574405c3ad833ba2a664af7bb96ac27c3606c21a5671eb4844dbb368c42e3f",
    ),
    (
        RevisionKind.CONTEXT_SOURCE,
        "4edb6486557bfbc55e09063860f70bcc7f63b7b24876ee1c36110f2dfb503fe9",
    ),
    (
        RevisionKind.READ_OPERATION,
        "4b5ac952fbd59e96cc90bed2b877025b4821aa3770ea39f1109534665e2daab9",
    ),
    (
        RevisionKind.PROFILE,
        "57f9c9cbcd24027e6aa14197d342cf8a0756ab2663da057e78a5325292a6c9ce",
    ),
    (
        RevisionKind.SKILL,
        "60585b06d99b5bc611e35861ea606b67116d2b341f55b12efc9e7230b9f9451b",
    ),
    (
        RevisionKind.TOOL,
        "bdcd273f73c7c63f2994e6c3ff1d3d0a7c060c51627f0ac0fbc60ce4aae026ef",
    ),
    (
        RevisionKind.POLICY,
        "87c5b5e7f7ee55371b755c93c6846eacd74a3765b3fed41162368733a36af318",
    ),
    (
        RevisionKind.BUDGET_POLICY,
        "6654e812a2135b35f13a7d025fc9e819ebf3df27905c16d6e99386030f262868",
    ),
    (
        RevisionKind.RETRY_POLICY,
        "7bc8df3d8a1f25eff766ef9661936b4c671119f346e0fc8cb75781ca590a9be9",
    ),
    (
        RevisionKind.CANCELLATION_POLICY,
        "d20401614a6fa28784c569c7f58857641df6a0b190e806b98635e86ffe09efe2",
    ),
    (
        RevisionKind.SCORECARD_POLICY,
        "32fa1280c2f85e41301e98f9c87869cd5ff4af8369d482c109b566dc860dbbfb",
    ),
    (
        RevisionKind.SELECTION_POLICY,
        "11ea40d8fcb25ab8e530fce2d45ffb3cb52e112f33f4d278489aeea66789cd88",
    ),
    (
        RevisionKind.ADMISSION_POLICY,
        "2323d754f500b51453081ea4012e91cb0038058456312d9a283bff73add4264e",
    ),
    (
        RevisionKind.AGENT_DEFINITION,
        "c9f73978714f76442090154d0654924a4a78b7f0997d227f74b5223e1c3994c0",
    ),
)


def test_literal_catalog_lineage_vectors_cover_the_closed_kind_set() -> None:
    assert {kind for kind, _ in LINEAGE_ID_VECTORS} == set(RevisionKind)


@pytest.mark.parametrize(("kind", "expected_lineage_id"), LINEAGE_ID_VECTORS)
def test_every_revision_kind_has_a_literal_catalog_lineage_vector(
    kind: RevisionKind, expected_lineage_id: str
) -> None:
    assert CatalogLineage(kind, FOUNDING_REVISION_HASH).lineage_id == (
        CatalogLineageId(expected_lineage_id)
    )


def test_changing_only_the_founding_revision_changes_the_literal_lineage_id() -> None:
    other_founding_revision = PublishedRevisionHash(
        "97388e64d7ffbc5bf85b1d1c2b62e33dacef556242f4828e906ec4ecf80e1c86"
    )

    assert CatalogLineage(
        RevisionKind.WORKFLOW, other_founding_revision
    ).lineage_id == CatalogLineageId(
        "e2ae4c35f302294853bc6312d0cc400b12197b6c4a5be0912bbd44f13e652933"
    )
    assert (
        CatalogLineage(RevisionKind.WORKFLOW, other_founding_revision).lineage_id
        != CatalogLineage(RevisionKind.WORKFLOW, FOUNDING_REVISION_HASH).lineage_id
    )


@pytest.mark.parametrize(
    "name",
    ["a", "workflow_one", "workflow.one", "workflow-one", "a0", "a-._0", "x" * 128],
)
def test_catalog_lineage_display_name_accepts_its_exact_grammar(name: str) -> None:
    assert CatalogLineageDisplayName(name).value == name


@pytest.mark.parametrize(
    "name",
    [
        "",
        "A",
        "1workflow",
        ".workflow",
        "-workflow",
        "_workflow",
        "workflow space",
        "workflow/name",
        "workflow\nname",
        "é",
        "x" * 129,
        "a" * 64,
    ],
)
def test_catalog_lineage_display_name_refuses_invalid_or_ambiguous_text(
    name: str,
) -> None:
    with pytest.raises(ValueError):
        CatalogLineageDisplayName(name)


def test_catalog_lineage_display_name_refuses_a_non_string() -> None:
    with pytest.raises(TypeError):
        CatalogLineageDisplayName(cast(str, None))


def test_catalog_lineage_requires_the_typed_kind_and_founding_hash() -> None:
    with pytest.raises(TypeError):
        CatalogLineage(cast(RevisionKind, "workflow"), FOUNDING_REVISION_HASH)

    with pytest.raises(TypeError):
        CatalogLineage(
            RevisionKind.WORKFLOW,
            cast(PublishedRevisionHash, FOUNDING_REVISION_HASH.value),
        )


def test_a_64_hex_query_is_a_lineage_id_and_anything_else_is_a_display_name() -> None:
    lineage_id = CatalogLineage(
        RevisionKind.WORKFLOW, FOUNDING_REVISION_HASH
    ).lineage_id

    assert catalog_lineage_query(lineage_id.value) == lineage_id
    assert catalog_lineage_query("a" * 64) == CatalogLineageId("a" * 64)
    assert catalog_lineage_query("lasagne") == CatalogLineageDisplayName("lasagne")
    assert catalog_lineage_query("g" * 64) == CatalogLineageDisplayName("g" * 64)
    with pytest.raises(ValueError):
        catalog_lineage_query("Lasagne")


def test_catalog_actor_and_activation_time_refuse_invalid_text() -> None:
    assert CatalogActor("operator").value == "operator"
    assert CatalogActivatedAt("2026-08-16T12:00:00Z").value == "2026-08-16T12:00:00Z"
    assert CatalogRetirementState.RETIRED == "retired"
    with pytest.raises(ValueError):
        CatalogActor("")
    with pytest.raises(TypeError):
        CatalogActor(cast(str, None))
    with pytest.raises(ValueError):
        CatalogActivatedAt("2026-08-16T12:00:00+00:00")
    with pytest.raises(ValueError):
        CatalogActivatedAt("2026-13-01T00:00:00Z")
    with pytest.raises(ValueError):
        CatalogActivatedAt("٢٠٢٦-٠٨-١٦T١٢:٠٠:٠٠Z")
