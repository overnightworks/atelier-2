from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from atelier2.application.role_candidates import (
    ModelResolutionSource,
    _candidate_choices,
    _ModelCandidate,
    _RoleChoices,
)
from atelier2.contracts.agents import (
    AgentBinding,
    AgentBindingSet,
    AgentConfigurationRevision,
    AgentConfigurationRevisionHash,
    AgentRole,
    AuthProfileRevisionHash,
    ResolvedAgentBinding,
)
from atelier2.contracts.host_configuration import (
    ModelRegistryRevision,
    ModelResolutionUncastReason,
    ProjectModelDefaultsRevision,
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


@dataclass(frozen=True)
class _ComponentSelection:
    states: tuple[int, ...]
    assigned_count: int


@dataclass
class _ComponentSearch:
    """Family-assignment search for one connected component: a tree or one cycle."""

    roles: tuple[str, ...]
    role_index: dict[str, int]
    options: dict[str, tuple[_ModelCandidate | None, ...]]
    neighbours: dict[str, set[str]]
    relevant_edges: tuple[tuple[str, str], ...]
    missing_state: int
    tree_memo: dict[
        tuple[str, str | None, frozenset[str]], dict[int, _ComponentSelection]
    ] = field(default_factory=dict)

    @classmethod
    def of(
        cls,
        roles: tuple[str, ...],
        choices: dict[str, _RoleChoices],
        edges: tuple[tuple[str, str], ...],
    ) -> _ComponentSearch:
        relevant_edges = tuple(
            (left, right) for left, right in edges if left in roles and right in roles
        )
        options = {role: (*choices[role].candidates, None) for role in roles}
        neighbours = {role: set[str]() for role in roles}
        for left, right in relevant_edges:
            neighbours[left].add(right)
            neighbours[right].add(left)
        index = {role: position for position, role in enumerate(roles)}
        missing = max(len(value) for value in options.values()) + 1
        return cls(roles, index, options, neighbours, relevant_edges, missing)

    def best_of(self, selections: list[_ComponentSelection]) -> _ComponentSelection:
        missing = self.missing_state
        return min(
            selections,
            key=lambda item: (
                -item.assigned_count,
                tuple(missing if index < 0 else index for index in item.states),
            ),
        )

    def one_state(self, role: str, state_index: int) -> _ComponentSelection:
        states = [-1] * len(self.roles)
        states[self.role_index[role]] = state_index
        assigned = int(self.options[role][state_index] is not None)
        return _ComponentSelection(tuple(states), assigned)

    @staticmethod
    def merged(
        left: _ComponentSelection, right: _ComponentSelection
    ) -> _ComponentSelection:
        states = tuple(
            right_state if left_state < 0 else left_state
            for left_state, right_state in zip(left.states, right.states, strict=True)
        )
        return _ComponentSelection(states, left.assigned_count + right.assigned_count)

    def family_pair_may_stand(
        self,
        left_role: str,
        left_state_index: int,
        right_role: str,
        right_state_index: int,
    ) -> bool:
        """Whether these two role-states may stand together under the family edges that join them."""
        states = {
            left_role: self.options[left_role][left_state_index],
            right_role: self.options[right_role][right_state_index],
        }
        for declarer, referenced in self.relevant_edges:
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

    def subtree_assignment(
        self, role: str, parent: str | None, blocked: frozenset[str]
    ) -> dict[int, _ComponentSelection]:
        """Precedence-first maximum of this role's subtree, keyed by this role's state."""
        key = (role, parent, blocked)
        cached = self.tree_memo.get(key)
        if cached is not None:
            return cached
        descendants = tuple(
            neighbour
            for neighbour in self.neighbours[role]
            if neighbour != parent and neighbour not in blocked
        )
        selections: dict[int, _ComponentSelection] = {}
        for state_index in range(len(self.options[role])):
            selected = self.one_state(role, state_index)
            viable = True
            for descendant in descendants:
                descendant_options = self.subtree_assignment(descendant, role, blocked)
                compatible_descendants = [
                    descendant_selection
                    for descendant_state, descendant_selection in descendant_options.items()
                    if self.family_pair_may_stand(
                        role, state_index, descendant, descendant_state
                    )
                ]
                if not compatible_descendants:
                    viable = False
                    break
                selected = self.merged(selected, self.best_of(compatible_descendants))
            if viable:
                selections[state_index] = selected
        self.tree_memo[key] = selections
        return selections

    def roles_left_on_the_cycle(self) -> frozenset[str]:
        """After peeling every degree-1 role, the roles that remain on the one cycle."""
        remaining_degree = {role: len(self.neighbours[role]) for role in self.roles}
        leaves = deque(role for role in self.roles if remaining_degree[role] <= 1)
        peeled: set[str] = set()
        while leaves:
            role = leaves.popleft()
            if role in peeled:
                continue
            peeled.add(role)
            for neighbour in self.neighbours[role]:
                if neighbour in peeled:
                    continue
                remaining_degree[neighbour] -= 1
                if remaining_degree[neighbour] == 1:
                    leaves.append(neighbour)
        return frozenset(role for role in self.roles if role not in peeled)

    def cycle_roles_from(self, start: str, cycle: frozenset[str]) -> list[str]:
        """The remaining cycle in walk order, starting at `start`."""
        cycle_order = [start]
        previous: str | None = None
        current = start
        while True:
            following = next(
                neighbour
                for neighbour in self.neighbours[current]
                if neighbour in cycle and neighbour != previous
            )
            if following == start:
                break
            cycle_order.append(following)
            previous, current = current, following
        return cycle_order

    def best_assignment_around(self, cycle: frozenset[str]) -> _ComponentSelection:
        """Precedence-first maximum assignment around the remaining cycle."""
        start = next(role for role in self.roles if role in cycle)
        cycle_order = self.cycle_roles_from(start, cycle)
        attached = {
            role: self.subtree_assignment(role, None, cycle) for role in cycle_order
        }
        completed: list[_ComponentSelection] = []
        for start_state, start_selection in attached[start].items():
            paths = {start_state: start_selection}
            previous_role = start
            for role in cycle_order[1:]:
                next_paths: dict[int, _ComponentSelection] = {}
                for state_index, attachment in attached[role].items():
                    possible = [
                        self.merged(path, attachment)
                        for previous_state, path in paths.items()
                        if self.family_pair_may_stand(
                            previous_role, previous_state, role, state_index
                        )
                    ]
                    if possible:
                        next_paths[state_index] = self.best_of(possible)
                paths = next_paths
                previous_role = role
            completed.extend(
                path
                for final_state, path in paths.items()
                if self.family_pair_may_stand(
                    cycle_order[-1], final_state, start, start_state
                )
            )
        return self.best_of(completed)

    def select(self) -> dict[str, _ModelCandidate | None]:
        cycle = self.roles_left_on_the_cycle()
        if not cycle:
            selection = self.best_of(
                list(self.subtree_assignment(self.roles[0], None, frozenset()).values())
            )
        else:
            selection = self.best_assignment_around(cycle)
        return {
            role: self.options[role][selection.states[self.role_index[role]]]
            for role in self.roles
        }


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
        selected.update(_ComponentSearch.of(component, choices_by_role, edges).select())
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


def _receipt_gate_refuses(
    executor_key: AgentExecutorKey,
    configuration: AgentConfigurationRevision,
    registry: AgentExecutorRegistry,
    workflow_hash: WorkflowRevisionHash,
) -> bool:
    """Whether a missing receipt refuses this start; the canary exemption waives only this gate."""
    capability = configuration.requested_capability
    if registry.is_startable(executor_key, capability, configuration.revision_hash):
        return False
    if not registry.is_structurally_startable(executor_key, capability):
        return True
    return not registry.reprobe_exempt(workflow_hash)


def _resolved_or_refused_binding(
    binding: AgentBinding,
    reads: AgentConfigurationBindingReads,
    registry: AgentExecutorRegistry,
    workflow_hash: WorkflowRevisionHash,
    working_on_the_project: frozenset[str],
) -> (
    ResolvedAgentBinding
    | DurableAgentConfigurationRevisionMissing
    | DurableAgentExecutorBindingUnavailable
    | DurableAgentExecutorCapabilityUnavailable
    | DurableAgentExecutorWithoutWorkspaceFileTools
):
    """This one requested binding, or the first refusal that stops it."""
    found = reads.agent_configuration_revision(
        binding.agent_configuration_revision_hash
    )
    if found is None:
        return DurableAgentConfigurationRevisionMissing()
    configuration, auth = found
    executor_key = AgentExecutorKey(auth.provider_id, configuration.executor_revision)
    if not registry.contains(executor_key):
        return DurableAgentExecutorBindingUnavailable()
    if configuration.requested_capability not in registry.declared_capabilities(
        executor_key
    ):
        return DurableAgentExecutorCapabilityUnavailable()
    if (
        binding.role.value in working_on_the_project
        and registry.workspace_file_tools(executor_key) is WorkspaceFileTools.WITHHELD
    ):
        return DurableAgentExecutorWithoutWorkspaceFileTools(
            binding.role.value, configuration.executor_revision.value
        )
    if _receipt_gate_refuses(executor_key, configuration, registry, workflow_hash):
        return DurableAgentExecutorBindingUnavailable()
    return ResolvedAgentBinding(binding.role, configuration, auth)


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
        outcome = _resolved_or_refused_binding(
            binding, reads, registry, workflow_hash, working_on_the_project
        )
        if not isinstance(outcome, ResolvedAgentBinding):
            return outcome
        resolved.append(outcome)

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
