"""The five durable tables one project's queue keeps its lifecycle in.

An item is observed, proposed, and admitted in `queue_items`; the proposal it
was admitted under, the prerequisites that proposal named, the run one
admission reserved, and the project policy those proposals stand on each keep
their own append-only table. They declare each other's foreign keys, so they
are read and changed as one family.
"""

from __future__ import annotations

import sqlalchemy as sa

from atelier2.adapters.dbos.table_vocabulary import (
    closed_vocabulary_sql,
    metadata,
    rfc3339_utc_or_null,
)
from atelier2.contracts.host_configuration import MAXIMUM_PROJECT_ID_CHARACTERS
from atelier2.contracts.queue_projection import (
    MAXIMUM_QUEUE_ACTIVE_RUNS,
    MAXIMUM_QUEUE_ADMISSION_RATIONALE_CHARACTERS,
    MAXIMUM_QUEUE_AUTOMATION_LABEL_CHARACTERS,
    MAXIMUM_QUEUE_ITEM_TITLE_CHARACTERS,
    MAXIMUM_TRACKER_ITEM_REFERENCE_CHARACTERS,
    QueueAutomationDisposition,
    QueueDecisionAuthority,
    QueueProposalSource,
)

_QUEUE_PROPOSAL_REVISIONS_ITEM_ID = "queue_proposal_revisions.item_id"
_QUEUE_PROPOSAL_REVISIONS_PROPOSAL_REVISION = (
    "queue_proposal_revisions.proposal_revision"
)

queue_items = sa.Table(
    "queue_items",
    metadata,
    sa.Column("item_id", sa.Text, primary_key=True),
    sa.Column("project_id", sa.Text, nullable=False),
    sa.Column("tracker_item_reference", sa.Text, nullable=False),
    sa.Column("state", sa.Text, nullable=False),
    sa.Column("state_version", sa.Integer, nullable=False),
    sa.Column(
        "workflow_lineage_id",
        sa.Text,
        sa.ForeignKey("catalog_lineages.lineage_id"),
        nullable=True,
    ),
    sa.Column("admission_rationale", sa.Text, nullable=True),
    sa.Column("current_proposal_revision", sa.Integer, nullable=True),
    sa.Column("decision_authority", sa.Text, nullable=True),
    # Dated observations of a tracker-owned fact, never core truth (ADR 0016,
    # 2026-09-01 amendment): `observed_title` is the title as the tracker last
    # served it and `title_observed_at` is when that read happened, so a reader
    # can tell a fresh title from a stale one instead of taking either as
    # current. `retired_at` is not an observed fact either -- closedness is
    # derived by set difference at import (ADR 0016 line 120) -- but the marker
    # of when import derived that retirement is durable state the import
    # records (ADR 0016 line 121). None of the three enters the proposal or
    # admission state machine below.
    sa.Column("observed_title", sa.Text, nullable=True),
    sa.Column("title_observed_at", sa.Text, nullable=True),
    sa.Column("retired_at", sa.Text, nullable=True),
    sa.UniqueConstraint("project_id", "tracker_item_reference"),
    sa.UniqueConstraint("item_id", "project_id"),
    sa.ForeignKeyConstraint(
        ("item_id", "current_proposal_revision"),
        (
            _QUEUE_PROPOSAL_REVISIONS_ITEM_ID,
            _QUEUE_PROPOSAL_REVISIONS_PROPOSAL_REVISION,
        ),
    ),
    sa.CheckConstraint("length(item_id) = 64 AND item_id NOT GLOB '*[^0-9a-f]*'"),
    sa.CheckConstraint(
        f"length(project_id) BETWEEN 1 AND {MAXIMUM_PROJECT_ID_CHARACTERS}"
    ),
    sa.CheckConstraint(
        "length(tracker_item_reference) BETWEEN 1 AND "
        f"{MAXIMUM_TRACKER_ITEM_REFERENCE_CHARACTERS}"
    ),
    sa.CheckConstraint("state IN ('OBSERVED', 'PROPOSED', 'ADMITTED')"),
    sa.CheckConstraint("state_version >= 0"),
    sa.CheckConstraint(
        "(state = 'ADMITTED' "
        "AND workflow_lineage_id IS NOT NULL "
        "AND length(workflow_lineage_id) = 64 "
        "AND workflow_lineage_id NOT GLOB '*[^0-9a-f]*' "
        "AND admission_rationale IS NOT NULL "
        f"AND length(admission_rationale) BETWEEN 1 AND "
        f"{MAXIMUM_QUEUE_ADMISSION_RATIONALE_CHARACTERS} "
        "AND ((current_proposal_revision IS NULL AND decision_authority IS NULL) "
        "OR (current_proposal_revision IS NOT NULL "
        "AND current_proposal_revision >= 1 "
        "AND state_version = current_proposal_revision + 1 "
        "AND decision_authority IS NOT NULL "
        f"AND decision_authority IN ('{QueueDecisionAuthority.OPERATOR.value}', "
        f"'{QueueDecisionAuthority.AUTOMATION_RULE.value}')))) "
        "OR (state = 'PROPOSED' "
        "AND current_proposal_revision IS NOT NULL "
        "AND current_proposal_revision >= 1 "
        "AND state_version = current_proposal_revision "
        "AND workflow_lineage_id IS NULL "
        "AND admission_rationale IS NULL "
        "AND decision_authority IS NULL) "
        "OR (state = 'OBSERVED' "
        "AND state_version = 0 "
        "AND workflow_lineage_id IS NULL "
        "AND admission_rationale IS NULL "
        "AND current_proposal_revision IS NULL "
        "AND decision_authority IS NULL)"
    ),
    sa.CheckConstraint(
        "observed_title IS NULL OR length(observed_title) BETWEEN 1 AND "
        f"{MAXIMUM_QUEUE_ITEM_TITLE_CHARACTERS}"
    ),
    sa.CheckConstraint("(observed_title IS NULL) = (title_observed_at IS NULL)"),
    sa.CheckConstraint(rfc3339_utc_or_null("title_observed_at")),
    sa.CheckConstraint(rfc3339_utc_or_null("retired_at")),
)
queue_project_policy_revisions = sa.Table(
    "queue_project_policy_revisions",
    metadata,
    sa.Column("project_id", sa.Text, nullable=False),
    sa.Column("revision_number", sa.Integer, nullable=False),
    sa.Column("maximum_active_runs", sa.Integer, nullable=False),
    sa.Column("automation_label", sa.Text, nullable=True),
    # The three defaults a labelled item with no proposal is proposed under.
    # They stand or fall together, because a proposal carries a workflow, a
    # rank and a disposition at once.
    sa.Column("default_workflow_lineage_id", sa.Text, nullable=True),
    sa.Column("default_priority_rank", sa.Integer, nullable=True),
    sa.Column("automation_disposition_default", sa.Text, nullable=True),
    sa.PrimaryKeyConstraint("project_id", "revision_number"),
    sa.CheckConstraint(
        f"length(project_id) BETWEEN 1 AND {MAXIMUM_PROJECT_ID_CHARACTERS}"
    ),
    sa.CheckConstraint("revision_number >= 1"),
    sa.CheckConstraint(
        f"maximum_active_runs BETWEEN 1 AND {MAXIMUM_QUEUE_ACTIVE_RUNS}"
    ),
    sa.CheckConstraint(
        "automation_label IS NULL OR length(automation_label) BETWEEN 1 AND "
        f"{MAXIMUM_QUEUE_AUTOMATION_LABEL_CHARACTERS}"
    ),
    # A CHECK refuses a row only when its condition is false, and each of the
    # three below is NULL rather than false for a policy that states no
    # defaults; the shape constraint after them is what decides when that
    # absence is allowed.
    sa.CheckConstraint(
        "length(default_workflow_lineage_id) = 64 "
        "AND default_workflow_lineage_id NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint("default_priority_rank >= 1"),
    sa.CheckConstraint(
        closed_vocabulary_sql(
            "automation_disposition_default", QueueAutomationDisposition
        )
    ),
    sa.CheckConstraint(
        "(default_workflow_lineage_id IS NULL "
        "AND default_priority_rank IS NULL "
        "AND automation_disposition_default IS NULL) "
        "OR (default_workflow_lineage_id IS NOT NULL "
        "AND default_priority_rank IS NOT NULL "
        "AND automation_disposition_default IS NOT NULL)"
    ),
)
queue_proposal_revisions = sa.Table(
    "queue_proposal_revisions",
    metadata,
    sa.Column("item_id", sa.Text, nullable=False),
    sa.Column("proposal_revision", sa.Integer, nullable=False),
    sa.Column("project_id", sa.Text, nullable=False),
    sa.Column("priority_rank", sa.Integer, nullable=False),
    sa.Column("workflow_lineage_id", sa.Text, nullable=False),
    sa.Column("automation_disposition", sa.Text, nullable=False),
    sa.Column("policy_revision", sa.Integer, nullable=True),
    sa.Column("source", sa.Text, nullable=False),
    sa.PrimaryKeyConstraint("item_id", "proposal_revision"),
    sa.UniqueConstraint("item_id", "proposal_revision", "project_id"),
    sa.ForeignKeyConstraint(
        ("item_id", "project_id"),
        ("queue_items.item_id", "queue_items.project_id"),
    ),
    sa.ForeignKeyConstraint(
        ("project_id", "policy_revision"),
        (
            "queue_project_policy_revisions.project_id",
            "queue_project_policy_revisions.revision_number",
        ),
    ),
    sa.ForeignKeyConstraint(("workflow_lineage_id",), ("catalog_lineages.lineage_id",)),
    sa.CheckConstraint("proposal_revision >= 1"),
    sa.CheckConstraint("priority_rank >= 1"),
    sa.CheckConstraint(
        "automation_disposition IN "
        f"('{QueueAutomationDisposition.HUMAN_REQUIRED.value}', "
        f"'{QueueAutomationDisposition.AUTOMATION_AUTHORIZED.value}')"
    ),
    sa.CheckConstraint("policy_revision IS NULL OR policy_revision >= 1"),
    sa.CheckConstraint(closed_vocabulary_sql("source", QueueProposalSource)),
)
queue_dependency_edges = sa.Table(
    "queue_dependency_edges",
    metadata,
    sa.Column("item_id", sa.Text, nullable=False),
    sa.Column("proposal_revision", sa.Integer, nullable=False),
    sa.Column("project_id", sa.Text, nullable=False),
    sa.Column("prerequisite_item_id", sa.Text, nullable=False),
    sa.PrimaryKeyConstraint("item_id", "proposal_revision", "prerequisite_item_id"),
    sa.ForeignKeyConstraint(
        ("item_id", "proposal_revision", "project_id"),
        (
            _QUEUE_PROPOSAL_REVISIONS_ITEM_ID,
            _QUEUE_PROPOSAL_REVISIONS_PROPOSAL_REVISION,
            "queue_proposal_revisions.project_id",
        ),
    ),
    sa.ForeignKeyConstraint(
        ("prerequisite_item_id", "project_id"),
        ("queue_items.item_id", "queue_items.project_id"),
    ),
    sa.CheckConstraint("item_id <> prerequisite_item_id"),
)
queue_launch_bindings = sa.Table(
    "queue_launch_bindings",
    metadata,
    sa.Column("item_id", sa.Text, primary_key=True),
    sa.Column("proposal_revision", sa.Integer, nullable=False),
    sa.Column("project_id", sa.Text, nullable=False),
    sa.Column("run_id", sa.Text, nullable=False, unique=True),
    sa.Column("workflow_revision_hash", sa.Text, nullable=False),
    sa.ForeignKeyConstraint(
        ("item_id", "proposal_revision", "project_id"),
        (
            _QUEUE_PROPOSAL_REVISIONS_ITEM_ID,
            _QUEUE_PROPOSAL_REVISIONS_PROPOSAL_REVISION,
            "queue_proposal_revisions.project_id",
        ),
    ),
    sa.ForeignKeyConstraint(
        ("workflow_revision_hash",), ("workflow_revisions.revision_hash",)
    ),
    sa.CheckConstraint("proposal_revision >= 1"),
    sa.CheckConstraint("length(run_id) > 0"),
    sa.CheckConstraint(
        "length(workflow_revision_hash) = 64 "
        "AND workflow_revision_hash NOT GLOB '*[^0-9a-f]*'"
    ),
)
