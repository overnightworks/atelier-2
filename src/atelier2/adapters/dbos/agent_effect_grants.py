"""The pinned-grant decision shared by agent completion and effect redemption."""

from __future__ import annotations

import sqlalchemy as sa

from atelier2.adapters.dbos.schema import published_revisions
from atelier2.adapters.dbos.sql_executor import SqlExecutor
from atelier2.contracts.revisions_v3 import PublishedRevisionHash, RevisionKind
from atelier2.contracts.run_bindings import RunBindingConflict
from atelier2.contracts.tool_grants_v3 import (
    DeclaredToolGrant,
    ToolGrantCapability,
    ToolGrantCapabilityNotRedeemed,
    ToolGrantRefused,
    read_tool_grant_document,
    redeems_as_platform_effect,
)
from atelier2.contracts.workflows_v3 import AgentNodeV3, VersionedReference


def read_pinned_tool_grants(
    session: SqlExecutor, node: AgentNodeV3
) -> tuple[DeclaredToolGrant, ...]:
    """Every grant this node pinned, read from the revisions it names by hash."""
    return tuple(_read_one_pinned_grant(session, pinned) for pinned in node.tools)


def _read_one_pinned_grant(
    session: SqlExecutor, pinned: VersionedReference
) -> DeclaredToolGrant:
    document = session.scalar(
        sa.select(published_revisions.c.document).where(
            published_revisions.c.kind == RevisionKind.TOOL.value,
            published_revisions.c.revision_hash == pinned.revision,
        )
    )
    if document is None:
        raise RunBindingConflict("the pinned tool revision left the registry")
    grant = read_tool_grant_document(bytes(document))
    if isinstance(grant, ToolGrantRefused):
        raise RunBindingConflict(f"the pinned tool revision is no grant: {grant}")
    return DeclaredToolGrant(
        PublishedRevisionHash(pinned.revision), grant.capability, grant.operation
    )


def _one_grant_of_shape(
    grants: tuple[DeclaredToolGrant, ...], *, platform_effect: bool
) -> DeclaredToolGrant | None:
    """The one bound grant of this shape among a node's resolved grants.

    A node may pin at most one exec-shaped and at most one effect-shaped
    grant; which shape a pin is is only known once its published bytes are
    read, so this is where two of the same shape are refused, once their
    capabilities are resolved rather than merely counted.
    """
    matching = tuple(
        grant
        for grant in grants
        if redeems_as_platform_effect(grant.capability) is platform_effect
    )
    if len(matching) > 1:
        shape = "effect-shaped" if platform_effect else "exec-shaped"
        raise RunBindingConflict(f"a node pins more than one {shape} grant")
    return matching[0] if matching else None


def read_pinned_exec_tool_grant(
    session: SqlExecutor, node: AgentNodeV3
) -> DeclaredToolGrant | None:
    """The one exec-shaped grant this node pinned, redeemed inside its own attempt."""
    return _one_grant_of_shape(
        read_pinned_tool_grants(session, node), platform_effect=False
    )


def read_pinned_effect_tool_grant(
    session: SqlExecutor, node: AgentNodeV3
) -> DeclaredToolGrant | None:
    """The one effect-shaped grant this node pinned, redeemed after it succeeds."""
    return _one_grant_of_shape(
        read_pinned_tool_grants(session, node), platform_effect=True
    )


def open_pr_capability_for(
    grant: DeclaredToolGrant | None,
) -> ToolGrantCapability | None:
    if grant is None or not redeems_as_platform_effect(grant.capability):
        return None
    if grant.capability is ToolGrantCapability.OPEN_PR:
        return grant.capability
    if grant.capability is ToolGrantCapability.PUSH_ATELIER_COMMIT:
        return None
    raise ToolGrantCapabilityNotRedeemed(grant.capability)


def push_atelier_commit_capability_for(
    grant: DeclaredToolGrant | None,
) -> ToolGrantCapability | None:
    if grant is None or not redeems_as_platform_effect(grant.capability):
        return None
    if grant.capability is ToolGrantCapability.OPEN_PR:
        return None
    if grant.capability is not ToolGrantCapability.PUSH_ATELIER_COMMIT:
        raise ToolGrantCapabilityNotRedeemed(grant.capability)
    if grant.operation is None:
        raise ToolGrantCapabilityNotRedeemed(grant.capability)
    return grant.capability


def agent_node_redeems_platform_effect(session: SqlExecutor, node: AgentNodeV3) -> bool:
    return read_pinned_effect_tool_grant(session, node) is not None
