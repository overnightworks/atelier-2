"""Whether every agent node of a graph is bound to a configuration of its own mode.

A node declares the mode it requires; the agent configuration its role binds
declares the one capability it runs with. The two must be equal (ADR 0006),
because the capability decides what the invocation may touch: a `headless`
node bound to a tool-bearing configuration would be handed tools its document
never asked for, with nothing but its instruction between them and the attempt.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from atelier2.contracts.agents import AgentExecutionCapability, ResolvedAgentBinding
from atelier2.contracts.workflows_v3 import AgentMode, AgentNodeV3, WorkflowGraphV3


@dataclass(frozen=True)
class AgentModeMismatch:
    """One node whose declared mode is not the capability its binding declares."""

    node: str
    mode: AgentMode
    capability: AgentExecutionCapability


def agent_mode_mismatch(
    graph: WorkflowGraphV3, bindings: Iterable[ResolvedAgentBinding]
) -> AgentModeMismatch | None:
    """The first agent node, in document order, bound outside its declared mode."""
    capability_by_role = {
        binding.role.value: binding.configuration.requested_capability
        for binding in bindings
    }
    for node in graph.nodes:
        if not isinstance(node, AgentNodeV3):
            continue
        capability = capability_by_role[node.role]
        if capability is not AgentExecutionCapability(node.mode):
            return AgentModeMismatch(node.id, node.mode, capability)
    return None
