"""`resolve_start_bindings`: the one binding decision a start makes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import pytest

from atelier2.application.resolve_start_bindings import (
    AuthProfileMissingForConfiguration,
    agent_role_completeness_refusal,
    cast_unbound_roles,
    resolve_start_bindings,
)
from atelier2.application.role_candidates import ModelResolutionSource
from atelier2.contracts.agents import (
    AgentBinding,
    AgentBindingSet,
    AgentConfigurationRevision,
    AgentConfigurationRevisionFormatVersion,
    AgentConfigurationRevisionHash,
    AgentExecutionCapability,
    AgentExecutorOperationalIdentity,
    AgentExecutorRevision,
    AgentRole,
    AuthMode,
    AuthProfileRevision,
    ProviderId,
    ResolvedAgentBinding,
)
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.host_configuration import (
    ModelRegistryEntry,
    ModelRegistryEntrySource,
    ModelRegistryRevision,
    ModelResolutionUncastReason,
    ProjectId,
    ProjectModelDefault,
    ProjectModelDefaultsRevision,
    ProviderModelCheck,
)
from atelier2.contracts.runs import WorkflowRevisionHash
from atelier2.contracts.when import RecordedAt
from atelier2.contracts.workflows_v3 import (
    AgentNodeV3,
    BindingConstraint,
    RoleDifficulty,
    VersionedReference,
    WorkflowGraphV3,
)
from atelier2.ports.agent_executions import (
    AgentExecutorKey,
    AgentExecutorRegistration,
    AgentExecutorRegistry,
    AgentExecutorV2,
    ProviderProbeReceiptGate,
    WorkspaceFileTools,
)
from atelier2.ports.durable_runs import (
    DurableAgentConfigurationRevisionMissing,
    DurableAgentExecutorBindingUnavailable,
    DurableAgentExecutorCapabilityUnavailable,
    DurableAgentExecutorWithoutWorkspaceFileTools,
    DurableBindingConstraintRefused,
    DurableInvalidAgentBindings,
)


@dataclass(frozen=True)
class FakeExecutorFactory:
    """The narrow shape `AgentExecutorRegistry` reads from a factory, faked."""

    provider: str
    revision: str = "v1"
    capabilities: frozenset[AgentExecutionCapability] = frozenset(
        {AgentExecutionCapability.HEADLESS}
    )

    @property
    def key(self) -> AgentExecutorKey:
        return AgentExecutorKey(
            ProviderId(self.provider), AgentExecutorRevision(self.revision)
        )

    @property
    def operational_identity(self) -> AgentExecutorOperationalIdentity:
        return AgentExecutorOperationalIdentity(f"{self.provider}-operation")

    @property
    def declared_capabilities(self) -> frozenset[AgentExecutionCapability]:
        return self.capabilities

    def open(self) -> AgentExecutorV2:
        raise AssertionError("resolving a binding never opens an executor")


BindingAnswer = tuple[AgentConfigurationRevision, AuthProfileRevision] | None


@dataclass
class ScriptedBindingReads:
    """Answers `agent_configuration_revision` from a fixed hash-keyed table.

    A hash mapped to `AuthProfileMissingForConfiguration` raises it, exactly as
    a real implementation does for a configuration whose own auth profile the
    store no longer holds.
    """

    answers: dict[
        AgentConfigurationRevisionHash,
        BindingAnswer | AuthProfileMissingForConfiguration,
    ]
    calls: list[AgentConfigurationRevisionHash] = field(default_factory=list)

    def agent_configuration_revision(
        self, revision_hash: AgentConfigurationRevisionHash
    ) -> BindingAnswer:
        self.calls.append(revision_hash)
        answer = self.answers.get(revision_hash)
        if isinstance(answer, AuthProfileMissingForConfiguration):
            raise answer
        return answer


def _auth(profile_id: str = "max") -> AuthProfileRevision:
    return AuthProfileRevision(
        profile_id, 1, ProviderId("exact"), AuthMode.SUBSCRIPTION
    )


def _configuration(
    auth: AuthProfileRevision,
    *,
    model: str = "opus",
    executor_revision: str = "v1",
    capability: AgentExecutionCapability = AgentExecutionCapability.HEADLESS,
) -> AgentConfigurationRevision:
    return AgentConfigurationRevision(
        model,
        auth.revision_hash,
        AgentExecutorRevision(executor_revision),
        capability,
        AgentConfigurationRevisionFormatVersion.V2,
    )


# `resolve_start_bindings` reads `workflow_hash` for its reprobe exemption,
# but every `_registry()` below carries no receipt gate, so `is_startable`
# never refuses and the exemption is never reached (`and` short-circuits on
# its first, always-true operand) -- this hash stays a genuine non-participant
# across every scenario in this module. `test_reprobe_exemption_*` below owns
# the scenarios where an armed registry actually consults it.
_UNCONSULTED_WORKFLOW_HASH = WorkflowRevisionHash("0" * 64)


def _registry(
    *factories: FakeExecutorFactory,
    unavailable: bool = False,
    workspace_file_tools: WorkspaceFileTools = WorkspaceFileTools.GRANTED,
    receipt_gate: ProviderProbeReceiptGate | None = None,
    reprobe_exempt_workflow_revisions: Callable[[], frozenset[WorkflowRevisionHash]]
    | None = None,
) -> AgentExecutorRegistry:
    build = (
        AgentExecutorRegistration.unavailable
        if unavailable
        else AgentExecutorRegistration.startable
    )
    return AgentExecutorRegistry(
        tuple(
            build(factory, workspace_file_tools=workspace_file_tools)
            for factory in factories
        ),
        receipt_gate=receipt_gate,
        reprobe_exempt_workflow_revisions=(
            reprobe_exempt_workflow_revisions
            if reprobe_exempt_workflow_revisions is not None
            else (lambda: frozenset())
        ),
    )


def _single_role_graph(
    role: str = "builder", *, pins_a_tool_grant: bool = False
) -> WorkflowGraphV3:
    """One agent node, one role to bind, optionally working on the project."""
    return WorkflowGraphV3(
        format_version=3,
        name="One role to bind",
        nodes=(
            AgentNodeV3(
                id="agent",
                type="agent",
                role=role,
                mode="headless",
                instruction="Build",
                tools=(
                    (VersionedReference(ref="verify", revision="d4" * 32),)
                    if pins_a_tool_grant
                    else ()
                ),
            ),
        ),
    )


def _v3_graph(
    *,
    distinct_from: bool = False,
    builder_difficulty: RoleDifficulty = 2,
    builder_model: str | None = None,
    merger_difficulty: RoleDifficulty = 2,
    merger_model: str | None = None,
    merger_family_differs_from: str | None = None,
) -> WorkflowGraphV3:
    """implement -> merge: two roles, optionally held to distinct occupations."""
    return WorkflowGraphV3(
        format_version=3,
        name="Implement, then land",
        nodes=(
            AgentNodeV3(
                id="implement",
                type="agent",
                role="builder",
                mode="headless",
                instruction="Write the change.",
                difficulty=builder_difficulty,
                model=builder_model,
            ),
            AgentNodeV3(
                id="merge",
                type="agent",
                role="merger",
                mode="headless",
                instruction="Land the change.",
                depends_on=("implement",),
                difficulty=merger_difficulty,
                model=merger_model,
                family_differs_from=merger_family_differs_from,
                binding_constraint=(
                    BindingConstraint(distinct_from="implement")
                    if distinct_from
                    else None
                ),
            ),
        ),
    )


def _model_entry(
    model_id: str,
    configuration_hash: str,
    provider_check: ProviderModelCheck = ProviderModelCheck.CHECKED,
) -> ModelRegistryEntry:
    return ModelRegistryEntry(
        model_id,
        AgentConfigurationRevisionHash(configuration_hash),
        ModelRegistryEntrySource.OPERATOR,
        provider_check,
    )


def _model_registry(
    provider: str, *entries: ModelRegistryEntry
) -> ModelRegistryRevision:
    return ModelRegistryRevision(ProviderId(provider), 1, entries)


def _revised_registry(
    provider: str, revision_number: int, *entries: ModelRegistryEntry
) -> ModelRegistryRevision:
    return ModelRegistryRevision(ProviderId(provider), revision_number, entries)


def _project_defaults(
    *selections: tuple[RoleDifficulty, ModelRegistryRevision, ModelRegistryEntry],
) -> ProjectModelDefaultsRevision:
    return ProjectModelDefaultsRevision(
        ProjectId("atelier"),
        1,
        tuple(
            ProjectModelDefault(
                difficulty,
                registry.revision_hash,
                registry.provider_id,
                entry.model_id,
                entry.agent_configuration_revision_hash,
            )
            for difficulty, registry, entry in selections
        ),
    )


def _family_graph_for_roles(
    roles: tuple[str, ...], links: dict[str, str]
) -> WorkflowGraphV3:
    return WorkflowGraphV3(
        format_version=3,
        name="Family casting",
        nodes=tuple(
            AgentNodeV3(
                id=role,
                type="agent",
                role=role,
                mode="headless",
                instruction=f"Do {role} work.",
                depends_on=(() if index == 0 else (roles[index - 1],)),
                difficulty=2,
                family_differs_from=links.get(role),
            )
            for index, role in enumerate(roles)
        ),
    )


def _family_graph(links: dict[str, str]) -> WorkflowGraphV3:
    return _family_graph_for_roles(("alpha", "beta", "gamma"), links)


def test_start_override_beats_workflow_pin_and_project_default() -> None:
    override = AgentConfigurationRevisionHash("a" * 64)
    overridden = ModelRegistryEntry(
        "override",
        override,
        ModelRegistryEntrySource.OPERATOR,
        ProviderModelCheck.CHECKED,
    )
    pinned = _model_entry("pinned", "b" * 64)
    project = _model_entry("project", "c" * 64)
    registry = _model_registry("anthropic", overridden, pinned, project)

    cast = cast_unbound_roles(
        _v3_graph(builder_model="pinned"),
        AgentBindingSet((AgentBinding(AgentRole("builder"), override),)),
        _project_defaults((2, registry, project)),
        (registry,),
    )

    assert cast.agent_bindings.bindings[0] == AgentBinding(
        AgentRole("builder"), override
    )
    assert cast.resolutions[0].source is ModelResolutionSource.CHOSEN_NOW


def test_workflow_pin_beats_project_default() -> None:
    pinned = _model_entry("pinned", "b" * 64)
    project = _model_entry("project", "c" * 64)
    registry = _model_registry("anthropic", pinned, project)

    cast = cast_unbound_roles(
        _v3_graph(builder_model="pinned"),
        AgentBindingSet(()),
        _project_defaults((2, registry, project)),
        (registry,),
    )

    assert cast.agent_bindings.bindings[0].agent_configuration_revision_hash == (
        pinned.agent_configuration_revision_hash
    )
    assert cast.resolutions[0].source is ModelResolutionSource.PINNED_IN_WORKFLOW


def test_missing_difficulty_uses_only_the_next_higher_project_default() -> None:
    hard = _model_entry("hard", "d" * 64)
    easy = _model_entry("easy", "e" * 64)
    registry = _model_registry("anthropic", hard, easy)

    cast = cast_unbound_roles(
        _v3_graph(builder_difficulty=2),
        AgentBindingSet(()),
        _project_defaults((1, registry, easy), (3, registry, hard)),
        (registry,),
    )

    assert cast.agent_bindings.bindings[0].agent_configuration_revision_hash == (
        hard.agent_configuration_revision_hash
    )
    assert cast.resolutions[0].source is ModelResolutionSource.FROM_PROJECT
    assert cast.resolutions[0].difficulty == 3


def test_a_family_rule_tries_a_higher_default_then_leaves_the_role_uncast() -> None:
    shared = _model_entry("shared", "1" * 64)
    alternate = _model_entry("alternate", "2" * 64)
    anthropic = _model_registry("anthropic", shared)
    openai = _model_registry("openai", alternate)
    defaults = _project_defaults(
        (2, anthropic, shared),
        (3, openai, alternate),
    )

    cast = cast_unbound_roles(
        _v3_graph(
            builder_difficulty=2,
            merger_difficulty=2,
            merger_family_differs_from="builder",
        ),
        AgentBindingSet(()),
        defaults,
        (anthropic, openai),
    )

    assert tuple(binding.role.value for binding in cast.agent_bindings.bindings) == (
        "builder",
        "merger",
    )
    assert (
        next(
            binding
            for binding in cast.agent_bindings.bindings
            if binding.role.value == "merger"
        ).agent_configuration_revision_hash
        == alternate.agent_configuration_revision_hash
    )
    assert (
        next(
            resolution
            for resolution in cast.resolutions
            if resolution.role.value == "merger"
        ).difficulty
        == 3
    )

    without_alternate = cast_unbound_roles(
        _v3_graph(merger_family_differs_from="builder"),
        AgentBindingSet(()),
        _project_defaults((2, anthropic, shared)),
        (anthropic,),
    )
    merger = next(
        resolution
        for resolution in without_alternate.resolutions
        if resolution.role.value == "merger"
    )
    assert merger.agent_configuration_revision_hash is None
    assert merger.source is ModelResolutionSource.UNCAST


def test_a_removed_registry_entry_invalidates_its_default_and_tries_higher() -> None:
    removed = _model_entry("removed", "1" * 64)
    replacement = _model_entry("replacement", "2" * 64)
    old_registry = _revised_registry("anthropic", 1, removed)
    latest_registry = _revised_registry("anthropic", 2, replacement)

    cast = cast_unbound_roles(
        _v3_graph(builder_difficulty=2),
        AgentBindingSet(()),
        _project_defaults(
            (2, old_registry, removed),
            (3, latest_registry, replacement),
        ),
        (latest_registry,),
    )

    builder = next(item for item in cast.resolutions if item.role.value == "builder")
    assert builder.agent_configuration_revision_hash == (
        replacement.agent_configuration_revision_hash
    )
    assert builder.difficulty == 3


def test_an_account_change_invalidates_the_old_exact_default_tuple() -> None:
    old_account = _model_entry("opus", "3" * 64)
    new_account = _model_entry("opus", "4" * 64)
    old_registry = _revised_registry("anthropic", 1, old_account)
    latest_registry = _revised_registry("anthropic", 2, new_account)

    cast = cast_unbound_roles(
        _v3_graph(),
        AgentBindingSet(()),
        _project_defaults((2, old_registry, old_account)),
        (latest_registry,),
    )

    assert cast.agent_bindings == AgentBindingSet(())
    assert {item.uncast_reason for item in cast.resolutions} == {
        ModelResolutionUncastReason.NO_PROJECT_DEFAULT
    }


def test_a_default_survives_an_additive_and_reordered_registry_revision() -> None:
    chosen = _model_entry("opus", "a" * 64)
    added = _model_entry("sonnet", "b" * 64)
    saved_registry = _revised_registry("anthropic", 1, chosen)
    latest_registry = _revised_registry("anthropic", 2, added, chosen)

    cast = cast_unbound_roles(
        _v3_graph(),
        AgentBindingSet(()),
        _project_defaults((2, saved_registry, chosen)),
        (latest_registry,),
    )

    assert cast.agent_bindings.bindings == (
        AgentBinding(AgentRole("builder"), chosen.agent_configuration_revision_hash),
        AgentBinding(AgentRole("merger"), chosen.agent_configuration_revision_hash),
    )


def test_an_unrelated_provider_revision_does_not_invalidate_a_default() -> None:
    chosen = _model_entry("opus", "5" * 64)
    unrelated = _model_entry("gpt", "6" * 64)
    anthropic = _revised_registry("anthropic", 1, chosen)
    openai = _revised_registry("openai", 9, unrelated)

    cast = cast_unbound_roles(
        _v3_graph(),
        AgentBindingSet(()),
        _project_defaults((2, anthropic, chosen)),
        (anthropic, openai),
    )

    assert {
        binding.agent_configuration_revision_hash
        for binding in cast.agent_bindings.bindings
    } == {chosen.agent_configuration_revision_hash}


def test_a_missing_override_is_terminal_and_never_falls_back_to_defaults() -> None:
    chosen = _model_entry("opus", "7" * 64)
    registry = _model_registry("anthropic", chosen)
    missing = AgentConfigurationRevisionHash("8" * 64)

    cast = cast_unbound_roles(
        _v3_graph(),
        AgentBindingSet((AgentBinding(AgentRole("builder"), missing),)),
        _project_defaults((2, registry, chosen)),
        (registry,),
    )

    builder = next(item for item in cast.resolutions if item.role.value == "builder")
    assert builder.agent_configuration_revision_hash is None
    assert builder.uncast_reason is ModelResolutionUncastReason.OVERRIDE_NOT_REGISTERED


def test_catalog_metadata_cannot_make_an_absent_override_eligible() -> None:
    configured = _model_entry("opus", "7" * 64)
    absent = AgentConfigurationRevisionHash("8" * 64)
    registry = _model_registry("anthropic", configured)

    cast = cast_unbound_roles(
        _v3_graph(),
        AgentBindingSet((AgentBinding(AgentRole("builder"), absent),)),
        _project_defaults((2, registry, configured)),
        (registry,),
        {absent: ("anthropic", "unregistered")},
    )

    builder = next(item for item in cast.resolutions if item.role.value == "builder")
    assert builder.uncast_reason is ModelResolutionUncastReason.OVERRIDE_NOT_REGISTERED


def test_catalog_metadata_must_match_the_one_eligible_registry_tuple() -> None:
    configured = _model_entry("opus", "7" * 64)
    registry = _model_registry("anthropic", configured)

    cast = cast_unbound_roles(
        _v3_graph(),
        AgentBindingSet(
            (
                AgentBinding(
                    AgentRole("builder"), configured.agent_configuration_revision_hash
                ),
            )
        ),
        None,
        (registry,),
        {configured.agent_configuration_revision_hash: ("anthropic", "sonnet")},
    )

    builder = next(item for item in cast.resolutions if item.role.value == "builder")
    assert builder.uncast_reason is ModelResolutionUncastReason.OVERRIDE_NOT_REGISTERED


@pytest.mark.parametrize(
    ("provider_check", "branch", "expected_reason"),
    [
        (check, "override", ModelResolutionUncastReason.OVERRIDE_NOT_REGISTERED)
        for check in (
            ProviderModelCheck.NOT_CHECKED,
            ProviderModelCheck.UNKNOWN_AT_PROVIDER,
        )
    ]
    + [
        (check, "pin", ModelResolutionUncastReason.WORKFLOW_MODEL_NOT_REGISTERED)
        for check in (
            ProviderModelCheck.NOT_CHECKED,
            ProviderModelCheck.UNKNOWN_AT_PROVIDER,
        )
    ]
    + [
        (check, "default", ModelResolutionUncastReason.NO_PROJECT_DEFAULT)
        for check in (
            ProviderModelCheck.NOT_CHECKED,
            ProviderModelCheck.UNKNOWN_AT_PROVIDER,
        )
    ],
)
def test_unchecked_provider_models_are_ineligible_at_every_precedence_branch(
    provider_check: ProviderModelCheck,
    branch: str,
    expected_reason: ModelResolutionUncastReason,
) -> None:
    rejected = _model_entry("opus", "8" * 64, provider_check)
    registry = _model_registry("anthropic", rejected)
    overrides = (
        AgentBindingSet(
            (
                AgentBinding(
                    AgentRole("builder"), rejected.agent_configuration_revision_hash
                ),
            )
        )
        if branch == "override"
        else AgentBindingSet(())
    )
    graph = _v3_graph(builder_model="opus" if branch == "pin" else None)
    defaults = (
        _project_defaults((2, registry, rejected)) if branch == "default" else None
    )

    cast = cast_unbound_roles(graph, overrides, defaults, (registry,))

    builder = next(item for item in cast.resolutions if item.role.value == "builder")
    assert builder.uncast_reason is expected_reason


def test_a_missing_or_ambiguous_workflow_pin_is_terminal() -> None:
    fallback = _model_entry("fallback", "9" * 64)
    duplicate_a = _model_entry("pinned", "a" * 64)
    duplicate_b = _model_entry("pinned", "b" * 64)
    anthropic = _model_registry("anthropic", fallback, duplicate_a)
    openai = _model_registry("openai", duplicate_b)
    defaults = _project_defaults((2, anthropic, fallback))

    missing = cast_unbound_roles(
        _v3_graph(builder_model="missing"), AgentBindingSet(()), defaults, (anthropic,)
    )
    ambiguous = cast_unbound_roles(
        _v3_graph(builder_model="pinned"),
        AgentBindingSet(()),
        defaults,
        (anthropic, openai),
    )

    assert missing.resolutions[0].uncast_reason is (
        ModelResolutionUncastReason.WORKFLOW_MODEL_NOT_REGISTERED
    )
    assert ambiguous.resolutions[0].uncast_reason is (
        ModelResolutionUncastReason.WORKFLOW_MODEL_AMBIGUOUS
    )


def test_family_relation_preserves_each_intrinsic_missing_candidate_reason() -> None:
    cast = cast_unbound_roles(
        _v3_graph(
            builder_model="missing",
            merger_family_differs_from="builder",
        ),
        AgentBindingSet(()),
        None,
        (),
    )

    assert {
        resolution.role.value: resolution.uncast_reason
        for resolution in cast.resolutions
    } == {
        "builder": ModelResolutionUncastReason.WORKFLOW_MODEL_NOT_REGISTERED,
        "merger": ModelResolutionUncastReason.NO_PROJECT_DEFAULT,
    }


def test_a_family_declarer_is_uncast_when_its_final_peer_is_uncast() -> None:
    available = _model_entry("opus", "b" * 64)
    registry = _model_registry("anthropic", available)

    cast = cast_unbound_roles(
        _v3_graph(
            builder_model="missing",
            merger_family_differs_from="builder",
        ),
        AgentBindingSet(()),
        _project_defaults((2, registry, available)),
        (registry,),
    )

    assert {role.role: role.reason for role in cast.uncast_roles} == {
        "builder": ModelResolutionUncastReason.WORKFLOW_MODEL_NOT_REGISTERED,
        "merger": ModelResolutionUncastReason.FAMILY_DIFFERENCE_UNAVAILABLE,
    }


def test_family_chain_is_solved_against_the_final_assignments() -> None:
    anthropic_entry = _model_entry("opus", "c" * 64)
    openai_entry = _model_entry("gpt", "d" * 64)
    anthropic = _model_registry("anthropic", anthropic_entry)
    openai = _model_registry("openai", openai_entry)

    cast = cast_unbound_roles(
        _family_graph({"beta": "alpha", "gamma": "beta"}),
        AgentBindingSet(()),
        _project_defaults((2, anthropic, anthropic_entry), (3, openai, openai_entry)),
        (anthropic, openai),
    )

    hashes = {
        binding.role.value: binding.agent_configuration_revision_hash
        for binding in cast.agent_bindings.bindings
    }
    assert hashes == {
        "alpha": anthropic_entry.agent_configuration_revision_hash,
        "beta": openai_entry.agent_configuration_revision_hash,
        "gamma": anthropic_entry.agent_configuration_revision_hash,
    }


def test_an_unsatisfiable_family_chain_keeps_its_largest_valid_tail() -> None:
    alpha_entry = _model_entry("alpha", "1" * 64)
    preferred_entry = _model_entry("preferred", "2" * 64)
    anthropic = _model_registry("anthropic", alpha_entry)
    openai = _model_registry("openai", preferred_entry)
    graph = WorkflowGraphV3(
        format_version=3,
        name="Partial family chain",
        nodes=(
            AgentNodeV3(
                id="alpha",
                type="agent",
                role="alpha",
                mode="headless",
                instruction="Do alpha work.",
                difficulty=2,
                model="alpha",
                family_differs_from="beta",
            ),
            AgentNodeV3(
                id="beta",
                type="agent",
                role="beta",
                mode="headless",
                instruction="Do beta work.",
                depends_on=("alpha",),
                difficulty=2,
                family_differs_from="gamma",
            ),
            AgentNodeV3(
                id="gamma",
                type="agent",
                role="gamma",
                mode="headless",
                instruction="Do gamma work.",
                depends_on=("beta",),
                difficulty=2,
                model="preferred",
            ),
        ),
    )

    cast = cast_unbound_roles(
        graph,
        AgentBindingSet(()),
        _project_defaults(
            (2, openai, preferred_entry),
            (3, anthropic, alpha_entry),
        ),
        (anthropic, openai),
    )

    assert {
        binding.role.value: binding.agent_configuration_revision_hash
        for binding in cast.agent_bindings.bindings
    } == {
        "beta": alpha_entry.agent_configuration_revision_hash,
        "gamma": preferred_entry.agent_configuration_revision_hash,
    }
    assert {
        resolution.role.value: resolution.uncast_reason
        for resolution in cast.resolutions
    } == {
        "alpha": ModelResolutionUncastReason.FAMILY_DIFFERENCE_UNAVAILABLE,
        "beta": None,
        "gamma": None,
    }
    beta = next(
        resolution for resolution in cast.resolutions if resolution.role.value == "beta"
    )
    assert beta.difficulty == 3


def test_family_cycle_names_every_role_when_no_final_assignment_exists() -> None:
    anthropic_entry = _model_entry("opus", "e" * 64)
    openai_entry = _model_entry("gpt", "f" * 64)
    anthropic = _model_registry("anthropic", anthropic_entry)
    openai = _model_registry("openai", openai_entry)

    cast = cast_unbound_roles(
        _family_graph({"alpha": "gamma", "beta": "alpha", "gamma": "beta"}),
        AgentBindingSet(()),
        _project_defaults((2, anthropic, anthropic_entry), (3, openai, openai_entry)),
        (anthropic, openai),
    )

    assert cast.agent_bindings == AgentBindingSet(())
    assert {role.role for role in cast.uncast_roles} == {"alpha", "beta", "gamma"}
    assert {role.reason for role in cast.uncast_roles} == {
        ModelResolutionUncastReason.FAMILY_DIFFERENCE_UNAVAILABLE
    }


@pytest.mark.parametrize("cycle", (False, True), ids=("chain", "cycle"))
def test_one_hundred_family_roles_keep_the_maximal_precedence_assignment(
    cycle: bool,
) -> None:
    roles = tuple(f"role-{index:03d}" for index in range(100))
    links = {role: roles[index - 1] for index, role in enumerate(roles) if index > 0}
    if cycle:
        links[roles[0]] = roles[-1]
    anthropic_entry = _model_entry("opus", "2" * 64)
    openai_entry = _model_entry("gpt", "3" * 64)
    anthropic = _model_registry("anthropic", anthropic_entry)
    openai = _model_registry("openai", openai_entry)

    cast = cast_unbound_roles(
        _family_graph_for_roles(roles, links),
        AgentBindingSet(()),
        _project_defaults(
            (2, anthropic, anthropic_entry),
            (3, openai, openai_entry),
        ),
        (anthropic, openai),
    )

    assert len(cast.agent_bindings.bindings) == 100
    assert tuple(
        binding.agent_configuration_revision_hash
        for binding in cast.agent_bindings.bindings
    ) == tuple(
        (
            anthropic_entry.agent_configuration_revision_hash
            if index % 2 == 0
            else openai_entry.agent_configuration_revision_hash
        )
        for index in range(100)
    )


def test_a_registered_override_participates_in_family_selection() -> None:
    anthropic_entry = _model_entry("opus", "0" * 64)
    openai_entry = _model_entry("gpt", "1" * 64)
    anthropic = _model_registry("anthropic", anthropic_entry)
    openai = _model_registry("openai", openai_entry)

    cast = cast_unbound_roles(
        _v3_graph(merger_family_differs_from="builder"),
        AgentBindingSet(
            (
                AgentBinding(
                    AgentRole("builder"), openai_entry.agent_configuration_revision_hash
                ),
            )
        ),
        _project_defaults((2, openai, openai_entry), (3, anthropic, anthropic_entry)),
        (anthropic, openai),
    )

    by_role = {
        binding.role.value: binding.agent_configuration_revision_hash
        for binding in cast.agent_bindings.bindings
    }
    assert by_role["builder"] == openai_entry.agent_configuration_revision_hash
    assert by_role["merger"] == anthropic_entry.agent_configuration_revision_hash


def test_without_project_defaults_every_open_role_is_named_uncast() -> None:
    cast = cast_unbound_roles(_v3_graph(), AgentBindingSet(()), None, ())

    assert cast.agent_bindings == AgentBindingSet(())
    assert {resolution.source for resolution in cast.resolutions} == {
        ModelResolutionSource.UNCAST
    }


@pytest.mark.parametrize(
    "bindings",
    [
        AgentBindingSet(()),
        AgentBindingSet(
            (
                AgentBinding(
                    AgentRole("reviewer"), AgentConfigurationRevisionHash("a" * 64)
                ),
            )
        ),
    ],
    ids=["missing_role", "unknown_role"],
)
def test_role_completeness_refuses_before_any_read(bindings: AgentBindingSet) -> None:
    reads = ScriptedBindingReads({})

    result = resolve_start_bindings(
        _single_role_graph(), _UNCONSULTED_WORKFLOW_HASH, bindings, reads, _registry()
    )

    assert result == DurableInvalidAgentBindings()
    assert reads.calls == []


def test_agent_role_completeness_refusal_is_the_same_decision_standalone() -> None:
    graph = _single_role_graph()
    complete = AgentBindingSet(
        (AgentBinding(AgentRole("builder"), AgentConfigurationRevisionHash("a" * 64)),)
    )

    assert agent_role_completeness_refusal(graph, complete) is None
    assert agent_role_completeness_refusal(graph, AgentBindingSet(())) == (
        DurableInvalidAgentBindings()
    )


def test_missing_agent_configuration_refuses() -> None:
    missing_hash = AgentConfigurationRevisionHash("a" * 64)
    bindings = AgentBindingSet((AgentBinding(AgentRole("builder"), missing_hash),))
    reads = ScriptedBindingReads({})

    result = resolve_start_bindings(
        _single_role_graph(), _UNCONSULTED_WORKFLOW_HASH, bindings, reads, _registry()
    )

    assert result == DurableAgentConfigurationRevisionMissing()


def test_missing_auth_profile_fails_loud_rather_than_refusing() -> None:
    auth = _auth()
    configuration = _configuration(auth)
    bindings = AgentBindingSet(
        (AgentBinding(AgentRole("builder"), configuration.revision_hash),)
    )
    reads = ScriptedBindingReads(
        {
            configuration.revision_hash: AuthProfileMissingForConfiguration(
                auth.revision_hash
            )
        }
    )

    with pytest.raises(AuthProfileMissingForConfiguration):
        resolve_start_bindings(
            _single_role_graph(),
            _UNCONSULTED_WORKFLOW_HASH,
            bindings,
            reads,
            _registry(),
        )


def test_unregistered_executor_refuses() -> None:
    auth = _auth()
    configuration = _configuration(auth)
    bindings = AgentBindingSet(
        (AgentBinding(AgentRole("builder"), configuration.revision_hash),)
    )
    reads = ScriptedBindingReads({configuration.revision_hash: (configuration, auth)})

    result = resolve_start_bindings(
        _single_role_graph(), _UNCONSULTED_WORKFLOW_HASH, bindings, reads, _registry()
    )

    assert result == DurableAgentExecutorBindingUnavailable()


@pytest.mark.parametrize(
    ("pins_a_tool_grant", "workspace_file_tools", "refused"),
    [
        pytest.param(
            True,
            WorkspaceFileTools.WITHHELD,
            True,
            id="a granted node cast to a file-blind executor",
        ),
        pytest.param(
            True,
            WorkspaceFileTools.GRANTED,
            False,
            id="a granted node cast to a workspace executor",
        ),
        pytest.param(
            False,
            WorkspaceFileTools.WITHHELD,
            False,
            id="a node pinning nothing, cast to a file-blind executor",
        ),
    ],
)
def test_a_node_that_pins_a_tool_grant_needs_an_executor_that_reaches_its_files(
    pins_a_tool_grant: bool, workspace_file_tools: WorkspaceFileTools, refused: bool
) -> None:
    """A grant is about the project's tree, so casting asks who can touch it.

    The doors executor is the case this exists for: it declares
    `HEADLESS_WITH_TOOLS` truthfully -- its tools are the atelier's own API
    doors -- and removes every built-in with `--tools=`, so a build node
    pinning verification and a push would be cast onto a call that cannot open
    a file (#1166). A node pinning no grant is untouched: that is the same
    executor doing the work it exists for.
    """

    auth = _auth()
    configuration = _configuration(auth)
    bindings = AgentBindingSet(
        (AgentBinding(AgentRole("builder"), configuration.revision_hash),)
    )
    reads = ScriptedBindingReads({configuration.revision_hash: (configuration, auth)})

    result = resolve_start_bindings(
        _single_role_graph(pins_a_tool_grant=pins_a_tool_grant),
        _UNCONSULTED_WORKFLOW_HASH,
        bindings,
        reads,
        _registry(
            FakeExecutorFactory("exact"), workspace_file_tools=workspace_file_tools
        ),
    )

    if refused:
        assert result == DurableAgentExecutorWithoutWorkspaceFileTools("builder", "v1")
    else:
        assert result == (
            ResolvedAgentBinding(AgentRole("builder"), configuration, auth),
        )


def test_undeclared_capability_refuses() -> None:
    auth = _auth()
    configuration = _configuration(auth, capability=AgentExecutionCapability.HEADLESS)
    bindings = AgentBindingSet(
        (AgentBinding(AgentRole("builder"), configuration.revision_hash),)
    )
    reads = ScriptedBindingReads({configuration.revision_hash: (configuration, auth)})
    registry = _registry(
        FakeExecutorFactory(
            "exact",
            capabilities=frozenset({AgentExecutionCapability.HEADLESS_WITH_TOOLS}),
        )
    )

    result = resolve_start_bindings(
        _single_role_graph(), _UNCONSULTED_WORKFLOW_HASH, bindings, reads, registry
    )

    assert result == DurableAgentExecutorCapabilityUnavailable()


def test_declared_but_unstartable_executor_refuses() -> None:
    auth = _auth()
    configuration = _configuration(auth)
    bindings = AgentBindingSet(
        (AgentBinding(AgentRole("builder"), configuration.revision_hash),)
    )
    reads = ScriptedBindingReads({configuration.revision_hash: (configuration, auth)})
    registry = _registry(FakeExecutorFactory("exact"), unavailable=True)

    result = resolve_start_bindings(
        _single_role_graph(), _UNCONSULTED_WORKFLOW_HASH, bindings, reads, registry
    )

    assert result == DurableAgentExecutorBindingUnavailable()


class _NoReceipts:
    """A receipt store that has never seen any evidence at all."""

    def receipt_for(self, configuration_hash: AgentConfigurationRevisionHash) -> None:
        return None


def _armed_but_unproven_registry(
    *, reprobe_exempt_workflow_revisions: Callable[[], frozenset[WorkflowRevisionHash]]
) -> AgentExecutorRegistry:
    """A structurally startable registry whose receipt gate proves nothing.

    The factory is registered and declares the capability, so the only
    question left for `resolve_start_bindings` to refuse or admit on is the
    receipt gate -- exactly what the reprobe exemption exists to answer.
    """

    return _registry(
        FakeExecutorFactory("exact"),
        receipt_gate=ProviderProbeReceiptGate(
            _NoReceipts(),
            Sha256Hash("a" * 64),
            lambda: RecordedAt("2026-01-01T00:00:00Z"),
        ),
        reprobe_exempt_workflow_revisions=reprobe_exempt_workflow_revisions,
    )


def test_reprobe_exemption_admits_a_start_with_no_receipt_for_an_admitted_canary_workflow() -> (
    None
):
    """A fresh canary run of an admitted workflow needs no receipt yet."""
    auth = _auth()
    configuration = _configuration(auth)
    bindings = AgentBindingSet(
        (AgentBinding(AgentRole("builder"), configuration.revision_hash),)
    )
    reads = ScriptedBindingReads({configuration.revision_hash: (configuration, auth)})
    canary_workflow_hash = WorkflowRevisionHash("1" * 64)
    registry = _armed_but_unproven_registry(
        reprobe_exempt_workflow_revisions=lambda: frozenset({canary_workflow_hash})
    )

    result = resolve_start_bindings(
        _single_role_graph(), canary_workflow_hash, bindings, reads, registry
    )

    assert result == (ResolvedAgentBinding(AgentRole("builder"), configuration, auth),)


def test_reprobe_exemption_refuses_a_workflow_the_admitted_set_does_not_name() -> None:
    """A missing receipt still refuses when this start is not itself a canary."""
    auth = _auth()
    configuration = _configuration(auth)
    bindings = AgentBindingSet(
        (AgentBinding(AgentRole("builder"), configuration.revision_hash),)
    )
    reads = ScriptedBindingReads({configuration.revision_hash: (configuration, auth)})
    admitted_canary_workflow_hash = WorkflowRevisionHash("1" * 64)
    started_workflow_hash = WorkflowRevisionHash("2" * 64)
    registry = _armed_but_unproven_registry(
        reprobe_exempt_workflow_revisions=lambda: frozenset(
            {admitted_canary_workflow_hash}
        )
    )

    result = resolve_start_bindings(
        _single_role_graph(), started_workflow_hash, bindings, reads, registry
    )

    assert result == DurableAgentExecutorBindingUnavailable()


def test_an_empty_reprobe_exemption_set_refuses_every_workflow_including_the_canary() -> (
    None
):
    """A misconfigured or unresolved admitted set exempts nothing -- not even
    the workflow whose own run would have produced the missing evidence."""
    auth = _auth()
    configuration = _configuration(auth)
    bindings = AgentBindingSet(
        (AgentBinding(AgentRole("builder"), configuration.revision_hash),)
    )
    reads = ScriptedBindingReads({configuration.revision_hash: (configuration, auth)})
    registry = _armed_but_unproven_registry(
        reprobe_exempt_workflow_revisions=lambda: frozenset()
    )

    result = resolve_start_bindings(
        _single_role_graph(), WorkflowRevisionHash("1" * 64), bindings, reads, registry
    )

    assert result == DurableAgentExecutorBindingUnavailable()


def test_reprobe_exemption_never_waives_a_structural_refusal() -> None:
    """An admitted canary workflow does not resolve an executor the operator
    marked unavailable -- the exemption bypasses only the receipt gate, never
    the factory-and-capability decision (real in production: Claude with a
    declared version mismatch)."""
    auth = _auth()
    configuration = _configuration(auth)
    bindings = AgentBindingSet(
        (AgentBinding(AgentRole("builder"), configuration.revision_hash),)
    )
    reads = ScriptedBindingReads({configuration.revision_hash: (configuration, auth)})
    admitted_canary_workflow_hash = WorkflowRevisionHash("1" * 64)
    registry = _registry(
        FakeExecutorFactory("exact"),
        unavailable=True,
        reprobe_exempt_workflow_revisions=lambda: frozenset(
            {admitted_canary_workflow_hash}
        ),
    )

    result = resolve_start_bindings(
        _single_role_graph(),
        admitted_canary_workflow_hash,
        bindings,
        reads,
        registry,
    )

    assert result == DurableAgentExecutorBindingUnavailable()


def test_resolves_every_binding_for_a_single_role_graph() -> None:
    auth = _auth()
    configuration = _configuration(auth)
    bindings = AgentBindingSet(
        (AgentBinding(AgentRole("builder"), configuration.revision_hash),)
    )
    reads = ScriptedBindingReads({configuration.revision_hash: (configuration, auth)})
    registry = _registry(FakeExecutorFactory("exact"))

    result = resolve_start_bindings(
        _single_role_graph(), _UNCONSULTED_WORKFLOW_HASH, bindings, reads, registry
    )

    assert result == (ResolvedAgentBinding(AgentRole("builder"), configuration, auth),)


def test_first_refusal_in_request_binding_order_wins() -> None:
    """Two bindings can both refuse; the first in the set's own order does."""
    auth = _auth()
    missing_hash = AgentConfigurationRevisionHash("a" * 64)
    merger_configuration = _configuration(auth, model="sonnet")
    # `AgentBindingSet` orders its bindings by role, so "builder" resolves
    # before "merger" -- and "builder" is the one whose configuration is
    # missing, while "merger" would refuse too (its executor is never
    # registered) if it were ever reached.
    bindings = AgentBindingSet(
        (
            AgentBinding(AgentRole("merger"), merger_configuration.revision_hash),
            AgentBinding(AgentRole("builder"), missing_hash),
        )
    )
    reads = ScriptedBindingReads(
        {merger_configuration.revision_hash: (merger_configuration, auth)}
    )

    result = resolve_start_bindings(
        _v3_graph(), _UNCONSULTED_WORKFLOW_HASH, bindings, reads, _registry()
    )

    assert result == DurableAgentConfigurationRevisionMissing()
    assert reads.calls == [missing_hash]


def test_distinct_from_refuses_only_after_both_bindings_resolve() -> None:
    auth = _auth()
    configuration = _configuration(auth)
    bindings = AgentBindingSet(
        (
            AgentBinding(AgentRole("builder"), configuration.revision_hash),
            AgentBinding(AgentRole("merger"), configuration.revision_hash),
        )
    )
    reads = ScriptedBindingReads({configuration.revision_hash: (configuration, auth)})
    registry = _registry(FakeExecutorFactory("exact"))

    result = resolve_start_bindings(
        _v3_graph(distinct_from=True),
        _UNCONSULTED_WORKFLOW_HASH,
        bindings,
        reads,
        registry,
    )

    assert result == DurableBindingConstraintRefused("merge", "implement")
    assert reads.calls == [configuration.revision_hash, configuration.revision_hash]


def test_distinct_configurations_satisfy_distinct_from() -> None:
    auth = _auth()
    builder_configuration = _configuration(auth, model="opus")
    merger_configuration = _configuration(auth, model="sonnet")
    bindings = AgentBindingSet(
        (
            AgentBinding(AgentRole("builder"), builder_configuration.revision_hash),
            AgentBinding(AgentRole("merger"), merger_configuration.revision_hash),
        )
    )
    reads = ScriptedBindingReads(
        {
            builder_configuration.revision_hash: (builder_configuration, auth),
            merger_configuration.revision_hash: (merger_configuration, auth),
        }
    )
    registry = _registry(FakeExecutorFactory("exact"))

    result = resolve_start_bindings(
        _v3_graph(distinct_from=True),
        _UNCONSULTED_WORKFLOW_HASH,
        bindings,
        reads,
        registry,
    )

    assert result == (
        ResolvedAgentBinding(AgentRole("builder"), builder_configuration, auth),
        ResolvedAgentBinding(AgentRole("merger"), merger_configuration, auth),
    )
