from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum

from atelier2.contracts.agents import (
    AgentBinding,
    AgentBindingSet,
    AgentConfigurationRevisionHash,
    AgentExecutionCapability,
)
from atelier2.contracts.host_configuration import (
    ModelRegistryRevision,
    ModelResolutionUncastReason,
    ProjectModelDefault,
    ProjectModelDefaultsRevision,
    ProviderModelCheck,
)
from atelier2.contracts.workflows_v3 import AgentMode, DeclaredRole, RoleDifficulty
from atelier2.ports.agent_configurations import AgentConfigurationBindingReads


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


@dataclass(frozen=True)
class RegisteredConfiguration:
    """One published configuration, as the casting has to judge it.

    A registry entry names a configuration by hash alone, so what it runs as is
    read once beside it: the provider and model say which entry a pin or a
    project default means, and the capability is what a node's declared mode is
    cast against.
    """

    provider_id: str
    model_id: str
    capability: AgentExecutionCapability


def registered_configurations(
    registries: tuple[ModelRegistryRevision, ...],
    requested: AgentBindingSet,
    reads: AgentConfigurationBindingReads,
) -> dict[AgentConfigurationRevisionHash, RegisteredConfiguration]:
    """Every configuration one casting may have to judge, read once.

    A configuration the store no longer holds stays out of the answer, which
    leaves it exactly as eligible as an unregistered one: no candidate.
    """
    hashes = {
        entry.agent_configuration_revision_hash
        for registry in registries
        for entry in registry.entries
    }
    hashes.update(
        binding.agent_configuration_revision_hash for binding in requested.bindings
    )
    known: dict[AgentConfigurationRevisionHash, RegisteredConfiguration] = {}
    for revision_hash in hashes:
        found = reads.agent_configuration_revision(revision_hash)
        if found is not None:
            configuration, auth_profile = found
            known[revision_hash] = RegisteredConfiguration(
                auth_profile.provider_id.value,
                configuration.model,
                configuration.requested_capability,
            )
    return known


def _in_node_mode(
    candidates: tuple[_ModelCandidate, ...],
    mode: AgentMode,
    configurations: Mapping[AgentConfigurationRevisionHash, RegisteredConfiguration],
) -> tuple[_ModelCandidate, ...]:
    """Those candidates whose configuration runs in exactly the node's mode.

    The equality is the one the start's own mode check makes, so what a casting
    may choose and what a start accepts can never drift apart.
    """
    declared = AgentExecutionCapability(mode)
    return tuple(
        candidate
        for candidate in candidates
        if (known := configurations.get(candidate.configuration_hash)) is not None
        and known.capability is declared
    )


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
    configurations: Mapping[AgentConfigurationRevisionHash, RegisteredConfiguration],
) -> _RoleChoices:
    """Whether this start override names exactly one eligible registry tuple.

    An override is an exact configuration, so it is taken as it was written and
    never read for a mode: a start refuses the mismatch by name, which says far
    more than a role that quietly stays uncast.
    """
    matches = tuple(
        candidate
        for candidate in registered
        if candidate.configuration_hash == requested.agent_configuration_revision_hash
    )
    named = configurations.get(requested.agent_configuration_revision_hash)
    if len(matches) == 1 and (
        named is None
        or (matches[0].provider_id, matches[0].model_id)
        == (named.provider_id, named.model_id)
    ):
        return _RoleChoices(matches, None)
    return _RoleChoices((), ModelResolutionUncastReason.OVERRIDE_NOT_REGISTERED)


def _pinned_model_choices(
    model_id: str,
    mode: AgentMode,
    registered: tuple[_ModelCandidate, ...],
    configurations: Mapping[AgentConfigurationRevisionHash, RegisteredConfiguration],
) -> _RoleChoices:
    """The pinned model's one eligible configuration in the node's own mode."""
    pinned = tuple(
        candidate for candidate in registered if candidate.model_id == model_id
    )
    if not pinned:
        return _RoleChoices(
            (), ModelResolutionUncastReason.WORKFLOW_MODEL_NOT_REGISTERED
        )
    in_mode = _in_node_mode(pinned, mode, configurations)
    if len(in_mode) == 1:
        return _RoleChoices(
            (replace(in_mode[0], source=ModelResolutionSource.PINNED_IN_WORKFLOW),),
            None,
        )
    if not in_mode:
        return _RoleChoices((), ModelResolutionUncastReason.MODEL_NOT_IN_NODE_MODE)
    return _RoleChoices((), ModelResolutionUncastReason.WORKFLOW_MODEL_AMBIGUOUS)


def _default_model_candidates(
    default: ProjectModelDefault, registered: tuple[_ModelCandidate, ...]
) -> tuple[_ModelCandidate, ...]:
    """The model this default stands for, in every configuration the registry holds.

    The row names one exact configuration, and while that tuple is still a
    checked entry the row speaks for its model; which of that model's
    configurations fills a node is the node's mode to decide.
    """
    named = (default.provider_id.value, default.model_id)
    of_model = tuple(
        candidate
        for candidate in registered
        if (candidate.provider_id, candidate.model_id) == named
    )
    named_configuration_stands = any(
        candidate.configuration_hash == default.agent_configuration_revision_hash
        for candidate in of_model
    )
    return of_model if named_configuration_stands else ()


def _project_default_choices(
    declared_difficulty: RoleDifficulty,
    mode: AgentMode,
    defaults: ProjectModelDefaultsRevision | None,
    registered: tuple[_ModelCandidate, ...],
    configurations: Mapping[AgentConfigurationRevisionHash, RegisteredConfiguration],
) -> _RoleChoices:
    """Project defaults from this difficulty upward, each read in the node's mode.

    The role's own step names its model, so a model answering no single
    configuration in this mode leaves the role uncast rather than reaching for
    another capability or another step; a higher step only ever widens what a
    family rule may choose between.
    """
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
        of_model = _default_model_candidates(default, registered)
        if not of_model:
            continue
        in_mode = _in_node_mode(of_model, mode, configurations)
        if len(in_mode) == 1:
            choices.append(
                replace(
                    in_mode[0],
                    source=ModelResolutionSource.FROM_PROJECT,
                    difficulty=typed_difficulty,
                )
            )
        elif not choices:
            return _RoleChoices((), ModelResolutionUncastReason.MODEL_NOT_IN_NODE_MODE)
    if not choices:
        return _RoleChoices((), ModelResolutionUncastReason.NO_PROJECT_DEFAULT)
    return _RoleChoices(tuple(choices), None)


def _candidate_choices(
    declaration: DeclaredRole,
    mode: AgentMode,
    requested_by_role: dict[str, AgentBinding],
    configurations: Mapping[AgentConfigurationRevisionHash, RegisteredConfiguration],
    defaults: ProjectModelDefaultsRevision | None,
    registries: tuple[ModelRegistryRevision, ...],
) -> _RoleChoices:
    """Who may occupy this role: override, then pin, then project defaults."""
    registered = _eligible_registry_candidates(registries)
    requested = requested_by_role.get(declaration.role)
    if requested is not None:
        return _override_choices(requested, registered, configurations)
    if declaration.model is not None:
        return _pinned_model_choices(
            declaration.model, mode, registered, configurations
        )
    return _project_default_choices(
        declaration.difficulty, mode, defaults, registered, configurations
    )
