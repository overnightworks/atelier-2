from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from atelier2.contracts.agents import AgentBinding, AgentConfigurationRevisionHash
from atelier2.contracts.host_configuration import (
    ModelRegistryRevision,
    ModelResolutionUncastReason,
    ProjectModelDefault,
    ProjectModelDefaultsRevision,
    ProviderModelCheck,
)
from atelier2.contracts.workflows_v3 import DeclaredRole, RoleDifficulty


class ModelResolutionSource(StrEnum):
    """The closed provenance vocabulary exposed to the start sheet."""

    CHOSEN_NOW = "chosen-now"
    PINNED_IN_WORKFLOW = "pinned-in-workflow"
    FROM_PROJECT = "from-project"
    UNCAST = "uncast"


@dataclass(frozen=True)
class _ModelCandidate:
    configuration_hash: AgentConfigurationRevisionHash
    provider_id: str | None
    model_id: str | None
    source: ModelResolutionSource
    difficulty: RoleDifficulty | None


@dataclass(frozen=True)
class _RoleChoices:
    candidates: tuple[_ModelCandidate, ...]
    uncast_reason: ModelResolutionUncastReason | None


def _eligible_registry_candidates(
    registries: tuple[ModelRegistryRevision, ...],
) -> tuple[_ModelCandidate, ...]:
    """The only registry tuples a start may name.

    A provider's rejected exact id remains visible configuration, but cannot
    become a run binding through an override, workflow pin, or default.
    """
    return tuple(
        _ModelCandidate(
            entry.agent_configuration_revision_hash,
            registry.provider_id.value,
            entry.model_id,
            ModelResolutionSource.CHOSEN_NOW,
            None,
        )
        for registry in registries
        for entry in registry.entries
        if entry.provider_check is ProviderModelCheck.CHECKED
    )


def configuration_registered(
    configuration_hash: AgentConfigurationRevisionHash,
    provider_id: str,
    model_id: str,
    registries: tuple[ModelRegistryRevision, ...],
) -> bool:
    """Whether the model registry still points to this exact configuration.

    The same registry lookup and uniqueness `cast_unbound_roles` enforces for
    an explicit override (`_candidate_choices`'s `requested` branch): exactly
    one checked entry across every registry must match both the hash and this
    configuration's own provider and model. The listing's `model_registered`
    answer asks this exact question, so a start's cast and the catalog's own
    listing can never silently disagree.
    """
    matches = tuple(
        candidate
        for candidate in _eligible_registry_candidates(registries)
        if candidate.configuration_hash == configuration_hash
    )
    return len(matches) == 1 and (matches[0].provider_id, matches[0].model_id) == (
        provider_id,
        model_id,
    )


def _override_choices(
    requested: AgentBinding,
    registered: tuple[_ModelCandidate, ...],
    override_models: dict[AgentConfigurationRevisionHash, tuple[str, str]],
) -> _RoleChoices:
    """Whether this start override names exactly one eligible registry tuple."""
    matches = tuple(
        candidate
        for candidate in registered
        if candidate.configuration_hash == requested.agent_configuration_revision_hash
    )
    metadata = override_models.get(requested.agent_configuration_revision_hash)
    if len(matches) == 1 and (
        metadata is None or (matches[0].provider_id, matches[0].model_id) == metadata
    ):
        return _RoleChoices(matches, None)
    return _RoleChoices((), ModelResolutionUncastReason.OVERRIDE_NOT_REGISTERED)


def _pinned_model_choices(
    model_id: str, registered: tuple[_ModelCandidate, ...]
) -> _RoleChoices:
    """Whether this workflow pin names exactly one eligible registry tuple."""
    pinned = tuple(
        _ModelCandidate(
            candidate.configuration_hash,
            candidate.provider_id,
            candidate.model_id,
            ModelResolutionSource.PINNED_IN_WORKFLOW,
            None,
        )
        for candidate in registered
        if candidate.model_id == model_id
    )
    if len(pinned) == 1:
        return _RoleChoices(pinned, None)
    if not pinned:
        return _RoleChoices(
            (), ModelResolutionUncastReason.WORKFLOW_MODEL_NOT_REGISTERED
        )
    return _RoleChoices((), ModelResolutionUncastReason.WORKFLOW_MODEL_AMBIGUOUS)


def _project_default_choices(
    declared_difficulty: RoleDifficulty,
    defaults: ProjectModelDefaultsRevision | None,
    registered: tuple[_ModelCandidate, ...],
) -> _RoleChoices:
    """Project defaults from this difficulty upward that the registry still holds."""
    if defaults is None:
        return _RoleChoices((), ModelResolutionUncastReason.NO_PROJECT_DEFAULT)
    by_difficulty: dict[RoleDifficulty, ProjectModelDefault] = {
        default.difficulty: default for default in defaults.defaults
    }
    choices: list[_ModelCandidate] = []
    for typed_difficulty in (1, 2, 3):
        if typed_difficulty < declared_difficulty:
            continue
        default = by_difficulty.get(typed_difficulty)
        if default is None:
            continue
        matching = tuple(
            candidate
            for candidate in registered
            if candidate.provider_id == default.provider_id.value
            and candidate.model_id == default.model_id
            and candidate.configuration_hash
            == default.agent_configuration_revision_hash
        )
        if len(matching) == 1:
            choices.append(
                _ModelCandidate(
                    default.agent_configuration_revision_hash,
                    default.provider_id.value,
                    default.model_id,
                    ModelResolutionSource.FROM_PROJECT,
                    typed_difficulty,
                )
            )
    if not choices:
        return _RoleChoices((), ModelResolutionUncastReason.NO_PROJECT_DEFAULT)
    return _RoleChoices(tuple(choices), None)


def _candidate_choices(
    declaration: DeclaredRole,
    requested_by_role: dict[str, AgentBinding],
    override_models: dict[AgentConfigurationRevisionHash, tuple[str, str]],
    defaults: ProjectModelDefaultsRevision | None,
    registries: tuple[ModelRegistryRevision, ...],
) -> _RoleChoices:
    """Who may occupy this role: override, then pin, then project defaults."""
    registered = _eligible_registry_candidates(registries)
    requested = requested_by_role.get(declaration.role)
    if requested is not None:
        return _override_choices(requested, registered, override_models)
    if declaration.model is not None:
        return _pinned_model_choices(declaration.model, registered)
    return _project_default_choices(declaration.difficulty, defaults, registered)
