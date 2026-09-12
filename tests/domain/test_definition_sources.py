from __future__ import annotations

from typing import cast

import pytest

from atelier2.contracts.definition_sources import (
    AmbiguousSelection,
    DefinitionSourceAccess,
    DefinitionSourceActor,
    DefinitionSourceConfiguration,
    DefinitionSourceKind,
    DefinitionSourceRevision,
    DefinitionSourceSelection,
    RepositoryLocation,
    RepositoryPath,
    RepositoryRef,
    SelectionPattern,
    SourceCommit,
)
from atelier2.contracts.revisions_v3 import RevisionKind

WORKFLOWS = SelectionPattern("workflows/*.yaml")
SKILLS = SelectionPattern("skills/*/SKILL.md")


def selection(pattern: str) -> DefinitionSourceSelection:
    return DefinitionSourceSelection(SelectionPattern(pattern), RevisionKind.WORKFLOW)


def registration(
    *,
    location: str = "/srv/definitions.git",
    ref: str = "refs/heads/main",
    actor: str = "felix",
    patterns: tuple[str, ...] = ("workflows/*.yaml",),
) -> DefinitionSourceConfiguration:
    return DefinitionSourceConfiguration(
        DefinitionSourceKind.GIT,
        RepositoryLocation(location),
        RepositoryRef(ref),
        DefinitionSourceAccess.ANONYMOUS,
        DefinitionSourceActor(actor),
        tuple(selection(pattern) for pattern in patterns),
    )


def test_connecting_the_same_repository_and_ref_again_is_the_same_source() -> None:
    assert registration().source_id == registration(actor="somebody else").source_id


def test_a_different_ref_of_the_same_repository_is_a_different_source() -> None:
    assert registration().source_id != registration(ref="refs/heads/next").source_id


def test_a_different_repository_is_a_different_source() -> None:
    assert registration().source_id != registration(location="/srv/other.git").source_id


def test_a_changed_selection_set_is_another_configuration_of_one_source() -> None:
    widened = registration(patterns=("workflows/*.yaml", "flows/*.yaml"))

    assert widened.source_id == registration().source_id
    assert widened != registration()


def test_the_revision_hash_covers_the_number_the_store_gave_it() -> None:
    configured = registration()

    assert DefinitionSourceRevision(configured, 1).revision_hash != (
        DefinitionSourceRevision(configured, 2).revision_hash
    )


def test_the_configuration_covers_the_connecting_actor() -> None:
    assert registration() != registration(actor="dana")


def test_selections_written_in_another_order_are_the_same_configuration() -> None:
    typed_one_way = registration(patterns=("workflows/*.yaml", "flows/*.yaml"))
    typed_the_other = registration(patterns=("flows/*.yaml", "workflows/*.yaml"))

    assert typed_one_way == typed_the_other
    assert typed_one_way.selections == typed_the_other.selections


def test_a_configuration_names_no_revision_number_of_its_own() -> None:
    """Only the store knows what already stands, so only the store numbers it."""

    assert not hasattr(registration(), "revision_number")


@pytest.mark.parametrize("declared", ["workflow", "skill"])
def test_a_selection_kind_that_is_only_a_string_is_refused(declared: str) -> None:
    """`RevisionKind` is a `StrEnum`, so a bare string would pass membership."""

    with pytest.raises(TypeError, match="typed contract"):
        DefinitionSourceSelection(WORKFLOWS, cast(RevisionKind, declared))


def test_a_registration_keeps_its_selections_in_one_stored_order() -> None:
    assert tuple(
        stored.pattern.value
        for stored in registration(patterns=("workflows/*.yaml", "a/*.yaml")).selections
    ) == ("a/*.yaml", "workflows/*.yaml")


def test_one_pattern_configured_twice_is_refused_as_ambiguous() -> None:
    with pytest.raises(ValueError, match="definition_source_selection_ambiguous"):
        registration(patterns=("workflows/*.yaml", "workflows/*.yaml"))


def test_a_registration_without_a_selection_is_refused() -> None:
    with pytest.raises(ValueError, match="1..64 selections"):
        registration(patterns=())


@pytest.mark.parametrize(
    "readable",
    [RevisionKind.WORKFLOW, RevisionKind.SCHEMA, RevisionKind.BUDGET_POLICY],
)
def test_a_selection_may_declare_every_kind_an_intake_reads(
    readable: RevisionKind,
) -> None:
    assert DefinitionSourceSelection(WORKFLOWS, readable).kind is readable


def test_a_selection_declares_a_kind_this_build_can_read() -> None:
    with pytest.raises(ValueError, match="no reader in this build"):
        DefinitionSourceSelection(WORKFLOWS, RevisionKind.SKILL)


def test_two_selections_claiming_one_path_refuse_rather_than_pick() -> None:
    both = registration(patterns=("workflows/*.yaml", "workflows/build*"))

    with pytest.raises(
        AmbiguousSelection, match="definition_source_selection_ambiguous"
    ):
        both.selection_for(RepositoryPath("workflows/build.yaml"))


def test_a_path_no_selection_claims_is_claimed_by_none() -> None:
    assert registration().selection_for(RepositoryPath("README.md")) is None


@pytest.mark.parametrize(
    ("pattern", "path", "claimed"),
    [
        (WORKFLOWS.value, "workflows/build.yaml", True),
        (WORKFLOWS.value, "workflows/nested/build.yaml", False),
        (WORKFLOWS.value, "workflows/build.yml", False),
        (WORKFLOWS.value, "other/build.yaml", False),
        (SKILLS.value, "skills/review/SKILL.md", True),
        (SKILLS.value, "skills/SKILL.md", False),
        ("build.yaml", "build.yaml", True),
        ("build.yaml", "a/build.yaml", False),
        ("a.b/*.yaml", "aXb/one.yaml", False),
    ],
)
def test_a_pattern_claims_exactly_the_paths_it_names(
    pattern: str, path: str, claimed: bool
) -> None:
    assert SelectionPattern(pattern).matches(RepositoryPath(path)) is claimed


@pytest.mark.parametrize(
    "escaping", ["/absolute/build.yaml", "../outside/build.yaml", "a/../b.yaml", "a//b"]
)
def test_a_path_that_leaves_the_repository_is_refused(escaping: str) -> None:
    with pytest.raises(ValueError, match="definition_source_path_escapes_repository"):
        RepositoryPath(escaping)


@pytest.mark.parametrize("escaping", ["/workflows/*.yaml", "../*.yaml", "./*.yaml"])
def test_a_pattern_that_leaves_the_repository_is_refused(escaping: str) -> None:
    with pytest.raises(ValueError, match="definition_source_path_escapes_repository"):
        SelectionPattern(escaping)


@pytest.mark.parametrize(
    "named", ["", "0" * 39, "0" * 65, "g" * 40, "0" * 39 + "G", "0" * 40 + " "]
)
def test_a_commit_must_be_a_git_object_name(named: str) -> None:
    with pytest.raises(ValueError, match="git object name"):
        SourceCommit(named)


@pytest.mark.parametrize("named", ["a" * 40, "b" * 64])
def test_a_git_object_name_of_either_format_is_a_commit(named: str) -> None:
    assert SourceCommit(named).value == named
