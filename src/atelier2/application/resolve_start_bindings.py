from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from functools import cache

from atelier2.contracts.agents import (
    AgentBinding,
    AgentBindingSet,
    AgentConfigurationRevisionHash,
    AgentRole,
    AuthProfileRevisionHash,
    ResolvedAgentBinding,
)
from atelier2.contracts.host_configuration import (
    ModelRegistryRevision,
    ModelResolutionUncastReason,
    ProjectModelDefault,
    ProjectModelDefaultsRevision,
    ProviderModelCheck,
    UncastRole,
)
from atelier2.contracts.runs import WorkflowRevisionHash
from atelier2.contracts.workflows_v3 import (
    AgentNodeV3,
    DeclaredRole,
    RoleDifficulty,
    WorkflowGraphV3,
    declared_roles_of,
)
from atelier2.ports.agent_configurations import AgentConfigurationBindingReads
from atelier2.ports.agent_executions import (
    AgentExecutorKey,
    AgentExecutorRegistry,
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

type ResolveStartBindingsResult = (
    tuple[ResolvedAgentBinding, ...]
    | DurableInvalidAgentBindings
    | DurableAgentConfigurationRevisionMissing
    | DurableAgentExecutorBindingUnavailable
    | DurableAgentExecutorCapabilityUnavailable
    | DurableAgentExecutorWithoutWorkspaceFileTools
    | DurableBindingConstraintRefused
)


class AuthProfileMissingForConfiguration(Exception):
    """A published agent configuration's own auth profile is absent.

    A configuration is never published without its auth profile hash already
    resolving (`AgentConfigurationCatalog.publish_agent_configuration_revision`
    refuses one that does not), so this is never a request a caller could have
    made honestly wrong. It is the durable store disagreeing with itself, and
    the one fail-loud word for that state: every caller of
    `resolve_start_bindings` maps it to its own "the store is corrupt" answer
    rather than a refusal a retry could fix.
    """

    def __init__(self, auth_profile_revision_hash: AuthProfileRevisionHash) -> None:
        super().__init__(
            "agent configuration auth profile "
            f"{auth_profile_revision_hash.value} is missing"
        )
        self.auth_profile_revision_hash = auth_profile_revision_hash


def declared_agent_roles(graph: WorkflowGraphV3) -> frozenset[str]:
    """Every role this document declares an `Agent` node for."""
    return frozenset(node.role for node in graph.nodes if isinstance(node, AgentNodeV3))


def agent_role_completeness_refusal(
    graph: WorkflowGraphV3, agent_bindings: AgentBindingSet
) -> DurableInvalidAgentBindings | None:
    """Whether every declared `Agent` role has exactly one requested binding.

    Split out from `resolve_start_bindings` because it is that decision's
    first question and its only one that reads nothing: a caller with its own
    reason to ask it before paying for a read -- `DbosDurableRunStarter` asks
    it before its existing-run retry check, so an invalid request is refused
    by role before it is ever compared against a stored run -- may ask it
    alone. `resolve_start_bindings` still asks it first internally for every
    other caller, so the one answer has one owner either way.
    """
    requested_roles = {binding.role.value for binding in agent_bindings.bindings}
    if declared_agent_roles(graph) != requested_roles:
        return DurableInvalidAgentBindings()
    return None


def undeclared_agent_role_refusal(
    graph: WorkflowGraphV3, agent_bindings: AgentBindingSet
) -> DurableInvalidAgentBindings | None:
    """Whether a partial V3 override names a role the workflow lacks."""
    requested_roles = {binding.role.value for binding in agent_bindings.bindings}
    if requested_roles - declared_agent_roles(graph):
        return DurableInvalidAgentBindings()
    return None


class ModelResolutionSource(StrEnum):
    """The closed provenance vocabulary exposed to the start sheet."""

    CHOSEN_NOW = "chosen-now"
    PINNED_IN_WORKFLOW = "pinned-in-workflow"
    FROM_PROJECT = "from-project"
    UNCAST = "uncast"


@dataclass(frozen=True)
class RoleModelResolution:
    role: AgentRole
    agent_configuration_revision_hash: AgentConfigurationRevisionHash | None
    source: ModelResolutionSource
    model_id: str | None
    declared_difficulty: RoleDifficulty | None
    difficulty: RoleDifficulty | None
    uncast_reason: ModelResolutionUncastReason | None
    family_differs_from: AgentRole | None


@dataclass(frozen=True)
class CastUnboundRolesResult:
    agent_bindings: AgentBindingSet
    resolutions: tuple[RoleModelResolution, ...]

    @property
    def uncast_roles(self) -> tuple[UncastRole, ...]:
        return tuple(
            UncastRole(
                resolution.role.value,
                resolution.uncast_reason,
                (
                    None
                    if resolution.family_differs_from is None
                    else resolution.family_differs_from.value
                ),
            )
            for resolution in self.resolutions
            if resolution.uncast_reason is not None
        )


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


def _candidate_choices(
    declaration: DeclaredRole,
    requested_by_role: dict[str, AgentBinding],
    override_models: dict[AgentConfigurationRevisionHash, tuple[str, str]],
    defaults: ProjectModelDefaultsRevision | None,
    registries: tuple[ModelRegistryRevision, ...],
) -> _RoleChoices:
    registered = _eligible_registry_candidates(registries)
    requested = requested_by_role.get(declaration.role)
    if requested is not None:
        matches = tuple(
            candidate
            for candidate in registered
            if candidate.configuration_hash
            == requested.agent_configuration_revision_hash
        )
        metadata = override_models.get(requested.agent_configuration_revision_hash)
        if len(matches) == 1 and (
            metadata is None
            or (matches[0].provider_id, matches[0].model_id) == metadata
        ):
            return _RoleChoices(matches, None)
        return _RoleChoices((), ModelResolutionUncastReason.OVERRIDE_NOT_REGISTERED)
    if declaration.model is not None:
        pinned = tuple(
            _ModelCandidate(
                candidate.configuration_hash,
                candidate.provider_id,
                candidate.model_id,
                ModelResolutionSource.PINNED_IN_WORKFLOW,
                None,
            )
            for candidate in registered
            if candidate.model_id == declaration.model
        )
        if len(pinned) == 1:
            uncast_reason = None
        elif not pinned:
            uncast_reason = ModelResolutionUncastReason.WORKFLOW_MODEL_NOT_REGISTERED
        else:
            uncast_reason = ModelResolutionUncastReason.WORKFLOW_MODEL_AMBIGUOUS
        return _RoleChoices(pinned if len(pinned) == 1 else (), uncast_reason)
    choices: list[_ModelCandidate] = []
    if defaults is not None:
        by_difficulty: dict[RoleDifficulty, ProjectModelDefault] = {
            default.difficulty: default for default in defaults.defaults
        }
        for typed_difficulty in (1, 2, 3):
            if typed_difficulty < declaration.difficulty:
                continue
            default = by_difficulty.get(typed_difficulty)
            if default is None:
                continue
            matching_registry_tuples = tuple(
                candidate
                for candidate in registered
                if (
                    candidate.provider_id == default.provider_id.value
                    and candidate.model_id == default.model_id
                    and candidate.configuration_hash
                    == default.agent_configuration_revision_hash
                )
            )
            if len(matching_registry_tuples) == 1:
                choices.append(
                    _ModelCandidate(
                        default.agent_configuration_revision_hash,
                        default.provider_id.value,
                        default.model_id,
                        ModelResolutionSource.FROM_PROJECT,
                        typed_difficulty,
                    )
                )
    return _RoleChoices(
        tuple(choices),
        None if choices else ModelResolutionUncastReason.NO_PROJECT_DEFAULT,
    )


def _family_edges(
    declarations: tuple[DeclaredRole, ...],
) -> tuple[tuple[str, str], ...]:
    return tuple(
        (declaration.role, declaration.family_differs_from)
        for declaration in declarations
        if declaration.family_differs_from is not None
    )


def _connected_roles(
    declarations: tuple[DeclaredRole, ...], edges: tuple[tuple[str, str], ...]
) -> tuple[tuple[str, ...], ...]:
    order = tuple(declaration.role for declaration in declarations)
    neighbours = {role: set[str]() for role in order}
    for left, right in edges:
        neighbours[left].add(right)
        neighbours[right].add(left)
    remaining = set(order)
    components: list[tuple[str, ...]] = []
    for role in order:
        if role not in remaining:
            continue
        reached: set[str] = set()
        pending = [role]
        while pending:
            current = pending.pop()
            if current in reached:
                continue
            reached.add(current)
            pending.extend(neighbours[current] - reached)
        remaining -= reached
        components.append(
            tuple(candidate for candidate in order if candidate in reached)
        )
    return tuple(components)


def _select_component(
    roles: tuple[str, ...],
    choices: dict[str, _RoleChoices],
    edges: tuple[tuple[str, str], ...],
) -> dict[str, _ModelCandidate | None]:
    """Keep the precedence-first maximum assignment of a tree or one cycle."""
    relevant_edges = tuple(
        (left, right) for left, right in edges if left in roles and right in roles
    )
    role_index = {role: index for index, role in enumerate(roles)}
    options = {role: (*choices[role].candidates, None) for role in roles}
    neighbours = {role: set[str]() for role in roles}
    for left, right in relevant_edges:
        neighbours[left].add(right)
        neighbours[right].add(left)

    @dataclass(frozen=True)
    class Selection:
        states: tuple[int, ...]
        assigned_count: int

    missing_state = max(len(value) for value in options.values()) + 1

    def selection_key(selection: Selection) -> tuple[int, tuple[int, ...]]:
        return (
            -selection.assigned_count,
            tuple(
                missing_state if state_index < 0 else state_index
                for state_index in selection.states
            ),
        )

    def best_of(selections: list[Selection]) -> Selection:
        return min(selections, key=selection_key)

    def one_state(role: str, state_index: int) -> Selection:
        states = [-1] * len(roles)
        states[role_index[role]] = state_index
        return Selection(
            tuple(states),
            int(options[role][state_index] is not None),
        )

    def merge(left: Selection, right: Selection) -> Selection:
        return Selection(
            tuple(
                right_state if left_state < 0 else left_state
                for left_state, right_state in zip(
                    left.states, right.states, strict=True
                )
            ),
            left.assigned_count + right.assigned_count,
        )

    def compatible(
        left_role: str,
        left_state_index: int,
        right_role: str,
        right_state_index: int,
    ) -> bool:
        states = {
            left_role: options[left_role][left_state_index],
            right_role: options[right_role][right_state_index],
        }
        for declarer, referenced in relevant_edges:
            if {declarer, referenced} != {left_role, right_role}:
                continue
            declarer_candidate = states[declarer]
            referenced_candidate = states[referenced]
            if declarer_candidate is not None and (
                referenced_candidate is None
                or declarer_candidate.provider_id == referenced_candidate.provider_id
            ):
                return False
        return True

    @cache
    def tree_options(
        role: str,
        parent: str | None,
        blocked: frozenset[str],
    ) -> dict[int, Selection]:
        descendants = tuple(
            neighbour
            for neighbour in neighbours[role]
            if neighbour != parent and neighbour not in blocked
        )
        selections: dict[int, Selection] = {}
        for state_index in range(len(options[role])):
            selected = one_state(role, state_index)
            viable = True
            for descendant in descendants:
                descendant_options = tree_options(descendant, role, blocked)
                compatible_descendants = [
                    descendant_selection
                    for descendant_state, descendant_selection in descendant_options.items()
                    if compatible(
                        role,
                        state_index,
                        descendant,
                        descendant_state,
                    )
                ]
                if not compatible_descendants:
                    viable = False
                    break
                selected = merge(selected, best_of(compatible_descendants))
            if viable:
                selections[state_index] = selected
        return selections

    remaining_degree = {role: len(neighbours[role]) for role in roles}
    leaves = deque(role for role in roles if remaining_degree[role] <= 1)
    peeled: set[str] = set()
    while leaves:
        role = leaves.popleft()
        if role in peeled:
            continue
        peeled.add(role)
        for neighbour in neighbours[role]:
            if neighbour in peeled:
                continue
            remaining_degree[neighbour] -= 1
            if remaining_degree[neighbour] == 1:
                leaves.append(neighbour)
    cycle = frozenset(role for role in roles if role not in peeled)

    if not cycle:
        selection = best_of(list(tree_options(roles[0], None, frozenset()).values()))
    else:
        start = next(role for role in roles if role in cycle)
        cycle_order = [start]
        previous: str | None = None
        current = start
        while True:
            following = next(
                neighbour
                for neighbour in neighbours[current]
                if neighbour in cycle and neighbour != previous
            )
            if following == start:
                break
            cycle_order.append(following)
            previous, current = current, following

        attached = {role: tree_options(role, None, cycle) for role in cycle_order}
        completed: list[Selection] = []
        for start_state, start_selection in attached[start].items():
            paths = {start_state: start_selection}
            previous_role = start
            for role in cycle_order[1:]:
                next_paths: dict[int, Selection] = {}
                for state_index, attachment in attached[role].items():
                    possible = [
                        merge(path, attachment)
                        for previous_state, path in paths.items()
                        if compatible(
                            previous_role,
                            previous_state,
                            role,
                            state_index,
                        )
                    ]
                    if possible:
                        next_paths[state_index] = best_of(possible)
                paths = next_paths
                previous_role = role
            completed.extend(
                path
                for final_state, path in paths.items()
                if compatible(
                    cycle_order[-1],
                    final_state,
                    start,
                    start_state,
                )
            )
        selection = best_of(completed)

    return {role: options[role][selection.states[role_index[role]]] for role in roles}


def cast_unbound_roles(
    graph: WorkflowGraphV3,
    requested: AgentBindingSet,
    defaults: ProjectModelDefaultsRevision | None,
    registries: tuple[ModelRegistryRevision, ...],
    override_models: dict[AgentConfigurationRevisionHash, tuple[str, str]]
    | None = None,
) -> CastUnboundRolesResult:
    """Resolve every role once under the workshop's fixed model precedence.

    Start overrides stand first, followed by the workflow's exact pin, the
    project's row for the declared difficulty, and only then rows for a higher
    difficulty. A family rule filters those candidates in the same order. No
    candidate means an explicit `uncast` resolution and no binding, so the
    existing completeness guard remains the start gate.
    """
    requested_by_role = {binding.role.value: binding for binding in requested.bindings}
    declarations = declared_roles_of(graph)
    known_override_models = {} if override_models is None else override_models
    choices_by_role = {
        declaration.role: _candidate_choices(
            declaration,
            requested_by_role,
            known_override_models,
            defaults,
            registries,
        )
        for declaration in declarations
    }
    selected: dict[str, _ModelCandidate | None] = {}
    uncast_reasons = {
        role: choices.uncast_reason
        for role, choices in choices_by_role.items()
        if choices.uncast_reason is not None
    }
    edges = _family_edges(declarations)
    for component in _connected_roles(declarations, edges):
        selected.update(_select_component(component, choices_by_role, edges))
    for left, _right in edges:
        if selected[left] is None and choices_by_role[left].uncast_reason is None:
            uncast_reasons[left] = (
                ModelResolutionUncastReason.FAMILY_DIFFERENCE_UNAVAILABLE
            )

    resolutions = tuple(
        RoleModelResolution(
            AgentRole(declaration.role),
            None
            if (candidate := selected[declaration.role]) is None
            else candidate.configuration_hash,
            ModelResolutionSource.UNCAST if candidate is None else candidate.source,
            None if candidate is None else candidate.model_id,
            declaration.difficulty,
            None if candidate is None else candidate.difficulty,
            uncast_reasons.get(declaration.role),
            (
                None
                if declaration.family_differs_from is None
                else AgentRole(declaration.family_differs_from)
            ),
        )
        for declaration in declarations
    )
    bindings = AgentBindingSet(
        tuple(
            AgentBinding(resolution.role, resolution.agent_configuration_revision_hash)
            for resolution in resolutions
            if resolution.agent_configuration_revision_hash is not None
        )
    )
    return CastUnboundRolesResult(bindings, resolutions)


def _roles_working_on_the_project(graph: WorkflowGraphV3) -> frozenset[str]:
    """Every role a node asks to work on the pinned project's own files.

    A node pins a `tools` reference to name one published tool grant, and every
    capability that closed vocabulary carries is about the project tree the
    attempt stands in: the project's own verification runs on it, the Atelier
    commit is pushed out of it, and the pull request opens over that commit
    (`contracts.tool_grants_v3`). So a node that pins one is a node whose
    attempt must be able to read and write its workspace, whatever else it
    does -- which is read from the document alone, without resolving the
    grants a second time.
    """

    return frozenset(
        node.role
        for node in graph.nodes
        if isinstance(node, AgentNodeV3) and node.tools
    )


def resolve_start_bindings(
    graph: WorkflowGraphV3,
    workflow_hash: WorkflowRevisionHash,
    agent_bindings: AgentBindingSet,
    reads: AgentConfigurationBindingReads,
    registry: AgentExecutorRegistry,
) -> ResolveStartBindingsResult:
    """The one binding decision a V2 or V3 start makes, in its fixed order.

    Role completeness is judged first and alone
    (`agent_role_completeness_refusal`). Only once that holds does each
    requested binding resolve, in the binding set's own order, the first
    refusal winning: its agent configuration must exist
    (`DurableAgentConfigurationRevisionMissing`), its own auth profile must
    exist (a corrupt state, so it fails loud as
    `AuthProfileMissingForConfiguration` rather than joining the refusals a
    caller could act on), the executor it names must be registered
    (`DurableAgentExecutorBindingUnavailable`), must declare the requested
    capability (`DurableAgentExecutorCapabilityUnavailable`), must reach the
    attempt's own files where the role's node pins a tool grant
    (`DurableAgentExecutorWithoutWorkspaceFileTools`), and must be
    currently startable (`DurableAgentExecutorBindingUnavailable`). Only after
    every binding has resolved are a V3 graph's `distinct_from` constraints
    checked, last, because they compare resolutions nothing before this point
    has produced.

    `workflow_hash` is the published identity of `graph` itself -- a
    `WorkflowRevisionHash` hashes source document bytes, so it cannot be
    recovered from the parsed graph and must travel from the caller that
    resolved it (`starter.py`'s `request.revision_hash`). It feeds exactly one
    decision here: a binding an armed registry would otherwise refuse for
    missing or stale receipt evidence still resolves when this exact start is
    itself a reprobe of a currently admitted `provider-canary-*` workflow
    (`AgentExecutorRegistry.reprobe_exempt`) -- the run that would produce the
    missing evidence cannot be the run the missing evidence blocks. The
    exemption reaches only the receipt gate: it is asked only once
    `is_structurally_startable` already holds, so an executor the operator
    never registered, or marked unavailable, refuses every start including a
    canary's own -- the exemption waives evidence, never structure.
    """
    role_refusal = agent_role_completeness_refusal(graph, agent_bindings)
    if role_refusal is not None:
        return role_refusal

    working_on_the_project = (
        _roles_working_on_the_project(graph)
        if isinstance(graph, WorkflowGraphV3)
        else frozenset()
    )
    resolved: list[ResolvedAgentBinding] = []
    for binding in agent_bindings.bindings:
        found = reads.agent_configuration_revision(
            binding.agent_configuration_revision_hash
        )
        if found is None:
            return DurableAgentConfigurationRevisionMissing()
        configuration, auth = found
        executor_key = AgentExecutorKey(
            auth.provider_id, configuration.executor_revision
        )
        if not registry.contains(executor_key):
            return DurableAgentExecutorBindingUnavailable()
        if configuration.requested_capability not in registry.declared_capabilities(
            executor_key
        ):
            return DurableAgentExecutorCapabilityUnavailable()
        if (
            binding.role.value in working_on_the_project
            and registry.workspace_file_tools(executor_key)
            is WorkspaceFileTools.WITHHELD
        ):
            return DurableAgentExecutorWithoutWorkspaceFileTools(
                binding.role.value, configuration.executor_revision.value
            )
        # The one exemption bypasses the receipt gate alone: it may only
        # rescue a start that `is_structurally_startable` already admits. An
        # executor never registered or marked unavailable refuses here
        # regardless of the exemption -- no run, canary included, could ever
        # produce evidence for a factory that does not exist.
        if not registry.is_startable(
            executor_key,
            configuration.requested_capability,
            configuration.revision_hash,
        ) and (
            not registry.is_structurally_startable(
                executor_key, configuration.requested_capability
            )
            or not registry.reprobe_exempt(workflow_hash)
        ):
            return DurableAgentExecutorBindingUnavailable()
        resolved.append(ResolvedAgentBinding(binding.role, configuration, auth))

    resolved_bindings = tuple(resolved)
    if isinstance(graph, WorkflowGraphV3):
        occupied = _refused_distinct_occupation(graph, resolved_bindings)
        if occupied is not None:
            return occupied
    return resolved_bindings


def _refused_distinct_occupation(
    graph: WorkflowGraphV3, resolved: tuple[ResolvedAgentBinding, ...]
) -> DurableBindingConstraintRefused | None:
    """Refuse a start whose `distinct_from` pair resolved to one occupation.

    The document already held the names. This is the binding seam: same
    configuration hash means the same agent sits on both nodes. Nothing
    about judgment is compared. No process has started.
    """

    by_role = {binding.role.value: binding for binding in resolved}
    for node in graph.nodes:
        if not isinstance(node, AgentNodeV3) or node.binding_constraint is None:
            continue
        other = graph.node(node.binding_constraint.distinct_from)
        assert isinstance(other, AgentNodeV3)
        left = by_role[node.role]
        right = by_role[other.role]
        if left.configuration.revision_hash == right.configuration.revision_hash:
            return DurableBindingConstraintRefused(
                node.id, node.binding_constraint.distinct_from
            )
    return None
