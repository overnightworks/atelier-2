"""The host configuration channel's own records and named refusals."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from pathlib import Path

import pytest

from atelier2.api.references import (
    decode_public_project_reference,
    encode_public_project_reference,
)
from atelier2.contracts.agents import AgentConfigurationRevisionHash, ProviderId
from atelier2.contracts.host_configuration import (
    MAXIMUM_MODEL_REGISTRY_ENTRIES,
    MAXIMUM_PROJECT_ID_CHARACTERS,
    PROJECT_UNKNOWN,
    ModelRegistryEntry,
    ModelRegistryEntrySource,
    ModelRegistryRevision,
    ProjectId,
    ProjectModelDefault,
    ProjectModelDefaultsRevision,
    ProjectRootRevision,
    ProjectSourceConnectionRevision,
    ProjectUnknown,
    ProviderModelCheck,
)
from atelier2.contracts.workflows_v3 import RoleDifficulty


def test_a_project_id_is_the_exact_characters_it_was_given() -> None:
    assert ProjectId("studio").value == "studio"


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("", id="empty"),
        pytest.param("x" * (MAXIMUM_PROJECT_ID_CHARACTERS + 1), id="too long"),
    ],
)
def test_a_bad_project_id_is_refused_as_project_unknown(value: str) -> None:
    with pytest.raises(ProjectUnknown, match=PROJECT_UNKNOWN):
        ProjectId(value)


@pytest.mark.parametrize("value", ["\ud800", "studio\udfff"])
@pytest.mark.proves("a-project-id-is-exact-utf8-before-it-enters-configuration")
def test_a_project_id_that_is_not_unicode_scalar_text_is_refused_before_hashing(
    value: str,
) -> None:
    with pytest.raises(ProjectUnknown, match=PROJECT_UNKNOWN):
        ProjectId(value)


@pytest.mark.proves("a-project-id-is-exact-utf8-before-it-enters-configuration")
def test_the_widest_maximum_project_id_round_trips_every_configuration_boundary(
    tmp_path: Path,
) -> None:
    project_id = ProjectId("\U0010ffff" * MAXIMUM_PROJECT_ID_CHARACTERS)
    revision = ProjectRootRevision(project_id, 1, tmp_path)

    assert revision.project_id == project_id
    assert (
        decode_public_project_reference(encode_public_project_reference(project_id))
        == project_id
    )


def test_the_same_project_root_revision_is_the_same_hash(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    first = ProjectRootRevision(ProjectId("studio"), 1, root)
    second = ProjectRootRevision(ProjectId("studio"), 1, root)

    assert first.revision_hash == second.revision_hash
    assert first.root_path == root.resolve()


def test_every_project_source_revision_must_explicitly_name_its_lifecycle_and_time() -> (
    None
):
    parameters = inspect.signature(ProjectSourceConnectionRevision).parameters

    assert parameters["lifecycle"].default is inspect.Parameter.empty
    assert parameters["connected_at"].default is inspect.Parameter.empty
    assert parameters["source_ref"].default is inspect.Parameter.empty


def test_a_later_revision_or_another_project_is_a_different_hash(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    first = ProjectRootRevision(ProjectId("studio"), 1, root)
    later = ProjectRootRevision(ProjectId("studio"), 2, root)
    other = ProjectRootRevision(ProjectId("other"), 1, root)

    assert first.revision_hash != later.revision_hash
    assert first.revision_hash != other.revision_hash


def _registry_entry(
    model_id: str,
    configuration_hash: str,
    source: ModelRegistryEntrySource = ModelRegistryEntrySource.OPERATOR,
    provider_check: ProviderModelCheck = ProviderModelCheck.CHECKED,
) -> ModelRegistryEntry:
    return ModelRegistryEntry(
        model_id,
        AgentConfigurationRevisionHash(configuration_hash),
        source,
        provider_check,
    )


def _registry(
    *entries: ModelRegistryEntry,
    provider: str = "anthropic",
    revision_number: int = 1,
) -> ModelRegistryRevision:
    return ModelRegistryRevision(
        ProviderId(provider),
        revision_number,
        entries,
    )


def test_model_registry_entries_are_exact_and_canonical_by_model_id() -> None:
    opus = _registry_entry("claude-opus-5", "cd" * 32)
    sonnet = _registry_entry(
        "claude-sonnet-4-6", "ef" * 32, ModelRegistryEntrySource.DISCOVERED
    )

    first = _registry(opus, sonnet)
    second = _registry(sonnet, opus)

    assert first.revision_hash == second.revision_hash
    assert first.entries == (opus, sonnet)


def test_provider_check_is_part_of_the_immutable_registry_fact() -> None:
    unchecked = _registry(
        _registry_entry(
            "claude-opus-5",
            "cd" * 32,
            provider_check=ProviderModelCheck.NOT_CHECKED,
        )
    )
    checked = _registry(
        _registry_entry(
            "claude-opus-5",
            "cd" * 32,
            provider_check=ProviderModelCheck.CHECKED,
        )
    )

    assert unchecked.entries[0].provider_check is ProviderModelCheck.NOT_CHECKED
    assert checked.entries[0].provider_check is ProviderModelCheck.CHECKED
    assert unchecked.revision_hash != checked.revision_hash


@pytest.mark.parametrize("model_id", ["", "newest opus", "\tmodel"])
def test_a_registry_refuses_a_non_exact_model_id(model_id: str) -> None:
    with pytest.raises(ValueError, match="model id"):
        _registry_entry(model_id, "cd" * 32)


def test_one_model_stands_under_several_configurations_ordered_by_configuration() -> (
    None
):
    tooled = _registry_entry("claude-opus-5", "ef" * 32)
    headless = _registry_entry("claude-opus-5", "cd" * 32)
    fable = _registry_entry("claude-fable-5", "ff" * 32)

    first = _registry(tooled, fable, headless)
    second = _registry(headless, tooled, fable)

    assert first.entries == (fable, headless, tooled)
    assert first.revision_hash == second.revision_hash


def test_a_model_repeated_under_the_same_configuration_is_refused() -> None:
    with pytest.raises(ValueError, match="unique"):
        _registry(
            _registry_entry("claude-opus-5", "cd" * 32),
            _registry_entry(
                "claude-opus-5",
                "cd" * 32,
                ModelRegistryEntrySource.DISCOVERED,
                ProviderModelCheck.NOT_CHECKED,
            ),
        )


# Computed by the production contract at 65091ce1, when a revision still held
# each model once; a revision published then must keep its identity now.
@pytest.mark.parametrize(
    ("provider", "revision_number", "entries", "published_hash"),
    [
        pytest.param(
            "anthropic",
            1,
            (("claude-opus-5", "cd" * 32, "operator", "checked"),),
            "9d09ff8a87b69e72de67097f9b291c0dfebf92f29f44fc6ba2d75b1867b450e5",
            id="one model",
        ),
        pytest.param(
            "anthropic",
            2,
            (
                ("claude-sonnet-4-6", "ab" * 32, "discovered", "unknown-at-provider"),
                ("claude-opus-5", "cd" * 32, "discovered", "checked"),
                ("claude-fable-5", "ef" * 32, "operator", "not-checked"),
            ),
            "b65a6f7f2484c1aae5de655ea3cac239faad94d79f50ebf3ef79cf9ee160786b",
            id="three models",
        ),
        pytest.param(
            "xai",
            3,
            (
                ("grok-4.6", "12" * 32, "operator", "checked"),
                ("Grok-4.6", "34" * 32, "operator", "checked"),
                ("grök-4.6", "56" * 32, "operator", "checked"),
                ("grok-4.6-mini", "78" * 32, "operator", "checked"),
            ),
            "c54d696b7608425ab028edca8220c31a6166c1786d6954e29cee39d994f5a114",
            id="utf-8 byte order",
        ),
        pytest.param(
            "openai",
            2,
            (),
            "f9956595960a9ce589a8835b5f1402d7fe780c46cb694a6626dd446aa0307daf",
            id="no model",
        ),
    ],
)
@pytest.mark.parametrize(
    "given_order",
    [
        pytest.param(tuple, id="as written"),
        pytest.param(lambda entries: entries[::-1], id="reversed"),
    ],
)
def test_a_revision_published_with_one_configuration_per_model_keeps_its_hash(
    provider: str,
    revision_number: int,
    entries: tuple[tuple[str, str, str, str], ...],
    published_hash: str,
    given_order: Callable[
        [tuple[ModelRegistryEntry, ...]], tuple[ModelRegistryEntry, ...]
    ],
) -> None:
    typed = tuple(
        _registry_entry(
            model_id,
            configuration_hash,
            ModelRegistryEntrySource(source),
            ProviderModelCheck(provider_check),
        )
        for model_id, configuration_hash, source, provider_check in entries
    )

    revision = _registry(
        *given_order(typed), provider=provider, revision_number=revision_number
    )

    assert revision.revision_hash.value == published_hash


def test_registry_entries_are_bounded_before_they_are_hashed() -> None:
    allowed = tuple(
        _registry_entry(f"model-{index}", f"{index:064x}")
        for index in range(MAXIMUM_MODEL_REGISTRY_ENTRIES)
    )

    assert len(_registry(*allowed).entries) == MAXIMUM_MODEL_REGISTRY_ENTRIES
    with pytest.raises(ValueError, match="at most"):
        _registry(*allowed, _registry_entry("one-too-many", "f" * 64))


def _defaults(
    *defaults: ProjectModelDefault,
    project: str = "studio",
    revision_number: int = 1,
) -> ProjectModelDefaultsRevision:
    return ProjectModelDefaultsRevision(ProjectId(project), revision_number, defaults)


def _default(
    difficulty: RoleDifficulty,
    registry: ModelRegistryRevision,
    entry: ModelRegistryEntry,
) -> ProjectModelDefault:
    return ProjectModelDefault(
        difficulty,
        registry.revision_hash,
        registry.provider_id,
        entry.model_id,
        entry.agent_configuration_revision_hash,
    )


def test_project_defaults_are_three_operator_chosen_registry_references() -> None:
    easy = _registry_entry("claude-haiku-4-5", "11" * 32)
    standard = _registry_entry("claude-sonnet-4-6", "22" * 32)
    hard = _registry_entry("claude-opus-5", "33" * 32)
    registry = _registry(easy, standard, hard)
    defaults = (
        _default(3, registry, hard),
        _default(1, registry, easy),
        _default(2, registry, standard),
    )

    first = _defaults(*defaults)
    second = _defaults(*reversed(defaults))

    assert first.revision_hash == second.revision_hash
    assert tuple(item.difficulty for item in first.defaults) == (
        1,
        2,
        3,
    )


def test_a_project_default_difficulty_is_unique() -> None:
    entry = _registry_entry("claude-opus-5", "33" * 32)
    registry = _registry(entry)

    with pytest.raises(ValueError, match="unique"):
        _defaults(
            _default(2, registry, entry),
            _default(2, registry, entry),
        )
