"""What one node of a started run durably binds, as a closed union of five forms.

**Why this exists.** What a node means was decided inside the DBOS adapter, in a
`TypedDict` that had become the storage format, and read back by a hand-written
version branch over the presence of a key. A persisted shape whose legal forms
nobody declares is a shape every reader may interpret differently.

**What lives here and what does not.** These types are the decision: the role a
node resolved to, the job it was given, the grant and the project source it
pinned. How that decision is written into a durable step output is an adapter
representation and lives with the adapter that writes it
(`adapters/dbos/node_binding_codec.py`); which of them the graph and the run
produce is the use-case (`application/bind_node.py`).

Every field is the type its owner already declares -- the format version is a
field of `AgentConfigurationRevision`, never a key that is present or absent.
"""

from __future__ import annotations

from dataclasses import dataclass

from atelier2.contracts.agents import ResolvedAgentBinding
from atelier2.contracts.project_sources import CandidateTree, ProjectSourcePin
from atelier2.contracts.run_bindings import RunBindingConflict
from atelier2.contracts.runs import FIRST_ROUND_ORDINAL, require_exact_round_ordinal
from atelier2.contracts.tool_grants_v3 import DeclaredToolGrant


@dataclass(frozen=True)
class AgentNodeBindingV2:
    """A V2 or V3 Agent node: the binding its role reached, and what it pinned.

    A V3 Agent node binds through this same form rather than one of its own: same
    role matrix, same attempt store, same executor registry, and a second form
    saying the same thing is how the two would drift apart. What differs between
    them is only how the job was composed and whether an output schema was
    declared, and both are decided before they arrive here.
    """

    resolved: ResolvedAgentBinding
    job: str
    tool_grant: DeclaredToolGrant | None = None
    project_source: ProjectSourcePin | None = None
    declared_output_schema_document: str | None = None
    round_ordinal: int = FIRST_ROUND_ORDINAL
    """Which round of a declared loop this binding was composed in.

    It travels inside the binding because the binding is what a recovery
    replays: reading the run again at launch would ask a row that may already
    stand in the next round, and the recovered node would silently bind an
    execution it never was.
    """
    maximum_assistant_turns: int | None = None
    """The hard turn bound the pinned budget named, or nothing where none was.

    Recovery replays this binding, not the catalog, so the bound has to live
    here for a workspace-tool launch to see the same ceiling the pin named.
    """
    start_candidate: CandidateTree | None = None
    """The work of an earlier publication this node goes on in, or nothing.

    It travels beside the pin rather than in its place: the pin stays what the
    work is compared and committed against, so what a later publisher pushes is
    the cumulative diff and an empty continuation is no failure, while this names
    the tree the attempt actually begins in. A replacement attempt is composed
    from the same publication and therefore starts from the same candidate.
    """

    def __post_init__(self) -> None:
        require_exact_round_ordinal(self.round_ordinal)
        if self.tool_grant is not None and self.project_source is None:
            raise RunBindingConflict(
                "a node redeeming a tool grant requires the project source its "
                "binding pinned, and this durable binding pinned none"
            )
        if self.start_candidate is not None and self.project_source is None:
            raise RunBindingConflict(
                "a node continuing an earlier publication requires the project "
                "source its binding pinned, and this durable binding pinned none"
            )


@dataclass(frozen=True)
class ActionNodeBinding:
    """An Action node: what it does is decided where its effect is prepared."""


@dataclass(frozen=True)
class WaitNodeBinding:
    """A Wait node: its round and the exact bound question, when it has one.

    Which answer the node admits is decided where the answer arrives, so the two
    document formats have nothing to disagree about here. A V3 Wait that reads
    material carries the composed question so recovery asks exactly what the
    original execution asked. Earlier formats and a V3 Wait with no inputs keep
    `question` absent and continue reading the authored prompt.
    """

    round_ordinal: int = FIRST_ROUND_ORDINAL
    """Which round of a declared loop this pause was entered in.

    It travels here for the reason the Agent form's does: the binding is what a
    recovery replays, and reading the run again at launch would ask a row that
    may already stand in the next round, so a recovered pause would answer for
    an execution it never was.
    """
    question: str | None = None
    """The exact question composed for this execution, or no bound composition."""

    def __post_init__(self) -> None:
        require_exact_round_ordinal(self.round_ordinal)
        if self.question is not None and not isinstance(self.question, str):
            raise TypeError("a bound wait question must be text")


@dataclass(frozen=True)
class SubworkflowNodeBinding:
    """A Subworkflow node: the two operands its author wrote."""

    operands: tuple[int, int]


type NodeBinding = (
    AgentNodeBindingV2 | ActionNodeBinding | WaitNodeBinding | SubworkflowNodeBinding
)
