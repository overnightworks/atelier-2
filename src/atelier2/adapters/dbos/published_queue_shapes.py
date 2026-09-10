"""What a Phase-D queue table looked like at a schema version that has moved on.

The record and the reason it is kept unedited belong to
`published_schema_shapes.py`; the queue family lives here because five tables
publish a shape between them and the migration ladder rebuilds each of them
under its own version key.
"""

from __future__ import annotations

from collections.abc import Mapping

_V48_QUEUE_ITEMS = """

CREATE TABLE queue_items (
	item_id TEXT NOT NULL,
	project_id TEXT NOT NULL,
	tracker_item_reference TEXT NOT NULL,
	state TEXT NOT NULL,
	state_version INTEGER NOT NULL,
	workflow_lineage_id TEXT,
	admission_rationale TEXT,
	current_proposal_revision INTEGER,
	decision_authority TEXT,
	observed_title TEXT,
	title_observed_at TEXT,
	retired_at TEXT,
	PRIMARY KEY (item_id),
	UNIQUE (project_id, tracker_item_reference),
	UNIQUE (item_id, project_id),
	FOREIGN KEY(item_id, current_proposal_revision) REFERENCES queue_proposal_revisions (item_id, proposal_revision),
	CHECK (length(item_id) = 64 AND item_id NOT GLOB '*[^0-9a-f]*'),
	CHECK (length(project_id) BETWEEN 1 AND 1024),
	CHECK (length(tracker_item_reference) BETWEEN 1 AND 1024),
	CHECK (state IN ('OBSERVED', 'PROPOSED', 'ADMITTED')),
	CHECK (state_version >= 0),
	CHECK ((state = 'ADMITTED' AND workflow_lineage_id IS NOT NULL AND length(workflow_lineage_id) = 64 AND workflow_lineage_id NOT GLOB '*[^0-9a-f]*' AND admission_rationale IS NOT NULL AND length(admission_rationale) BETWEEN 1 AND 4096 AND ((current_proposal_revision IS NULL AND decision_authority IS NULL) OR (current_proposal_revision IS NOT NULL AND current_proposal_revision >= 1 AND state_version = current_proposal_revision + 1 AND decision_authority IS NOT NULL AND decision_authority IN ('OPERATOR', 'AUTOMATION_RULE')))) OR (state = 'PROPOSED' AND current_proposal_revision IS NOT NULL AND current_proposal_revision >= 1 AND state_version = current_proposal_revision AND workflow_lineage_id IS NULL AND admission_rationale IS NULL AND decision_authority IS NULL) OR (state = 'OBSERVED' AND state_version = 0 AND workflow_lineage_id IS NULL AND admission_rationale IS NULL AND current_proposal_revision IS NULL AND decision_authority IS NULL)),
	CHECK (observed_title IS NULL OR length(observed_title) BETWEEN 1 AND 256),
	CHECK ((observed_title IS NULL) = (title_observed_at IS NULL)),
	CHECK ((title_observed_at IS NULL OR (length(title_observed_at) = 20 AND title_observed_at LIKE '____-__-__T__:__:__Z'))),
	CHECK ((retired_at IS NULL OR (length(retired_at) = 20 AND retired_at LIKE '____-__-__T__:__:__Z'))),
	FOREIGN KEY(workflow_lineage_id) REFERENCES catalog_lineages (lineage_id)
)


"""
"""The queue item table V48 published, with its observation columns.

V49 adds three tables and moves none, so this text is the declaration as it
stands today -- recorded anyway, because `_apply_v47_to_v48` materialises
its own target and the declaration stops being that target the moment a
later hop touches `queue_items`.
"""
_V44_QUEUE_ITEMS = """
CREATE TABLE queue_items (
	item_id TEXT NOT NULL,
	project_id TEXT NOT NULL,
	tracker_item_reference TEXT NOT NULL,
	state TEXT NOT NULL,
	state_version INTEGER NOT NULL,
	workflow_lineage_id TEXT,
	admission_rationale TEXT,
	current_proposal_revision INTEGER,
	decision_authority TEXT,
	PRIMARY KEY (item_id),
	UNIQUE (project_id, tracker_item_reference),
	UNIQUE (item_id, project_id),
	FOREIGN KEY(item_id, current_proposal_revision) REFERENCES queue_proposal_revisions (item_id, proposal_revision),
	CHECK (length(item_id) = 64 AND item_id NOT GLOB '*[^0-9a-f]*'),
	CHECK (length(project_id) BETWEEN 1 AND 1024),
	CHECK (length(tracker_item_reference) BETWEEN 1 AND 1024),
	CHECK (state IN ('OBSERVED', 'PROPOSED', 'ADMITTED')),
	CHECK (state_version >= 0),
	CHECK ((state = 'ADMITTED' AND workflow_lineage_id IS NOT NULL AND length(workflow_lineage_id) = 64 AND workflow_lineage_id NOT GLOB '*[^0-9a-f]*' AND admission_rationale IS NOT NULL AND length(admission_rationale) BETWEEN 1 AND 4096 AND ((current_proposal_revision IS NULL AND decision_authority IS NULL) OR (current_proposal_revision IS NOT NULL AND current_proposal_revision >= 1 AND state_version = current_proposal_revision + 1 AND decision_authority IS NOT NULL AND decision_authority IN ('OPERATOR', 'AUTOMATION_RULE')))) OR (state = 'PROPOSED' AND current_proposal_revision IS NOT NULL AND current_proposal_revision >= 1 AND state_version = current_proposal_revision AND workflow_lineage_id IS NULL AND admission_rationale IS NULL AND decision_authority IS NULL) OR (state = 'OBSERVED' AND state_version = 0 AND workflow_lineage_id IS NULL AND admission_rationale IS NULL AND current_proposal_revision IS NULL AND decision_authority IS NULL)),
	FOREIGN KEY(workflow_lineage_id) REFERENCES catalog_lineages (lineage_id)
)

"""
_QUEUE_ITEMS_BEFORE_PHASE_D = """
CREATE TABLE queue_items (
	item_id TEXT NOT NULL,
	project_id TEXT NOT NULL,
	tracker_item_reference TEXT NOT NULL,
	state TEXT NOT NULL,
	state_version INTEGER NOT NULL,
	workflow_lineage_id TEXT,
	admission_rationale TEXT,
	PRIMARY KEY (item_id),
	UNIQUE (project_id, tracker_item_reference),
	CHECK (length(item_id) = 64 AND item_id NOT GLOB '*[^0-9a-f]*'),
	CHECK (length(project_id) BETWEEN 1 AND 1024),
	CHECK (length(tracker_item_reference) BETWEEN 1 AND 1024),
	CHECK (state IN ('OBSERVED', 'ADMITTED')),
	CHECK (state_version >= 0),
	CHECK ((state = 'ADMITTED' AND workflow_lineage_id IS NOT NULL AND length(workflow_lineage_id) = 64 AND workflow_lineage_id NOT GLOB '*[^0-9a-f]*' AND admission_rationale IS NOT NULL AND length(admission_rationale) BETWEEN 1 AND 4096) OR (state = 'OBSERVED' AND workflow_lineage_id IS NULL AND admission_rationale IS NULL)),
	FOREIGN KEY(workflow_lineage_id) REFERENCES catalog_lineages (lineage_id)
)

"""
"""The admission-only queue row V29 through V43 published before Phase D."""
_V51_QUEUE_PROJECT_POLICY_REVISIONS = """
CREATE TABLE queue_project_policy_revisions (
	project_id TEXT NOT NULL,
	revision_number INTEGER NOT NULL,
	maximum_active_runs INTEGER NOT NULL,
	automation_label TEXT,
	PRIMARY KEY (project_id, revision_number),
	CHECK (length(project_id) BETWEEN 1 AND 1024),
	CHECK (revision_number >= 1),
	CHECK (maximum_active_runs BETWEEN 1 AND 1000),
	CHECK (automation_label IS NULL OR length(automation_label) BETWEEN 1 AND 256)
)

"""
"""The policy table V44 introduced and every schema up to V51 published."""
_V51_QUEUE_PROPOSAL_REVISIONS = """
CREATE TABLE queue_proposal_revisions (
	item_id TEXT NOT NULL,
	proposal_revision INTEGER NOT NULL,
	project_id TEXT NOT NULL,
	priority_rank INTEGER NOT NULL,
	workflow_lineage_id TEXT NOT NULL,
	automation_disposition TEXT NOT NULL,
	policy_revision INTEGER,
	PRIMARY KEY (item_id, proposal_revision),
	UNIQUE (item_id, proposal_revision, project_id),
	FOREIGN KEY(item_id, project_id) REFERENCES queue_items (item_id, project_id),
	FOREIGN KEY(project_id, policy_revision) REFERENCES queue_project_policy_revisions (project_id, revision_number),
	FOREIGN KEY(workflow_lineage_id) REFERENCES catalog_lineages (lineage_id),
	CHECK (proposal_revision >= 1),
	CHECK (priority_rank >= 1),
	CHECK (automation_disposition IN ('HUMAN_REQUIRED', 'AUTOMATION_AUTHORIZED')),
	CHECK (policy_revision IS NULL OR policy_revision >= 1)
)

"""
"""The proposal table V44 introduced and every schema up to V51 published."""
_V52_QUEUE_PROJECT_POLICY_REVISIONS = """
CREATE TABLE queue_project_policy_revisions (
	project_id TEXT NOT NULL,
	revision_number INTEGER NOT NULL,
	maximum_active_runs INTEGER NOT NULL,
	automation_label TEXT,
	default_workflow_lineage_id TEXT,
	default_priority_rank INTEGER,
	automation_disposition_default TEXT,
	PRIMARY KEY (project_id, revision_number),
	CHECK (length(project_id) BETWEEN 1 AND 1024),
	CHECK (revision_number >= 1),
	CHECK (maximum_active_runs BETWEEN 1 AND 1000),
	CHECK (automation_label IS NULL OR length(automation_label) BETWEEN 1 AND 256),
	CHECK (length(default_workflow_lineage_id) = 64 AND default_workflow_lineage_id NOT GLOB '*[^0-9a-f]*'),
	CHECK (default_priority_rank >= 1),
	CHECK (automation_disposition_default IN ('HUMAN_REQUIRED', 'AUTOMATION_AUTHORIZED')),
	CHECK ((default_workflow_lineage_id IS NULL AND default_priority_rank IS NULL AND automation_disposition_default IS NULL) OR (default_workflow_lineage_id IS NOT NULL AND default_priority_rank IS NOT NULL AND automation_disposition_default IS NOT NULL))
)

"""
"""The policy table with the defaults V52 gave it, which V53 does not move."""
_V52_QUEUE_PROPOSAL_REVISIONS = """
CREATE TABLE queue_proposal_revisions (
	item_id TEXT NOT NULL,
	proposal_revision INTEGER NOT NULL,
	project_id TEXT NOT NULL,
	priority_rank INTEGER NOT NULL,
	workflow_lineage_id TEXT NOT NULL,
	automation_disposition TEXT NOT NULL,
	policy_revision INTEGER,
	source TEXT NOT NULL,
	PRIMARY KEY (item_id, proposal_revision),
	UNIQUE (item_id, proposal_revision, project_id),
	FOREIGN KEY(item_id, project_id) REFERENCES queue_items (item_id, project_id),
	FOREIGN KEY(project_id, policy_revision) REFERENCES queue_project_policy_revisions (project_id, revision_number),
	FOREIGN KEY(workflow_lineage_id) REFERENCES catalog_lineages (lineage_id),
	CHECK (proposal_revision >= 1),
	CHECK (priority_rank >= 1),
	CHECK (automation_disposition IN ('HUMAN_REQUIRED', 'AUTOMATION_AUTHORIZED')),
	CHECK (policy_revision IS NULL OR policy_revision >= 1),
	CHECK (source IN ('OPERATOR', 'POLICY_DEFAULT'))
)

"""
"""The proposal table with the source V52 gave it, which V53 does not move."""
_V44_QUEUE_DEPENDENCY_EDGES = """
CREATE TABLE queue_dependency_edges (
	item_id TEXT NOT NULL,
	proposal_revision INTEGER NOT NULL,
	project_id TEXT NOT NULL,
	prerequisite_item_id TEXT NOT NULL,
	PRIMARY KEY (item_id, proposal_revision, prerequisite_item_id),
	FOREIGN KEY(item_id, proposal_revision, project_id) REFERENCES queue_proposal_revisions (item_id, proposal_revision, project_id),
	FOREIGN KEY(prerequisite_item_id, project_id) REFERENCES queue_items (item_id, project_id),
	CHECK (item_id <> prerequisite_item_id)
)

"""
"""The dependency table V44 introduced, which no later hop has moved."""

_V44_QUEUE_LAUNCH_BINDINGS = """
CREATE TABLE queue_launch_bindings (
	item_id TEXT NOT NULL,
	proposal_revision INTEGER NOT NULL,
	project_id TEXT NOT NULL,
	run_id TEXT NOT NULL,
	workflow_revision_hash TEXT NOT NULL,
	PRIMARY KEY (item_id),
	FOREIGN KEY(item_id, proposal_revision, project_id) REFERENCES queue_proposal_revisions (item_id, proposal_revision, project_id),
	FOREIGN KEY(workflow_revision_hash) REFERENCES workflow_revisions (revision_hash),
	CHECK (proposal_revision >= 1),
	CHECK (length(run_id) > 0),
	CHECK (length(workflow_revision_hash) = 64 AND workflow_revision_hash NOT GLOB '*[^0-9a-f]*'),
	UNIQUE (run_id)
)

"""
"""The launch-binding table V44 introduced, unmoved until the V55 release."""

_V55_QUEUE_LAUNCH_BINDINGS = """
CREATE TABLE queue_launch_bindings (
	item_id TEXT NOT NULL,
	proposal_revision INTEGER NOT NULL,
	project_id TEXT NOT NULL,
	run_id TEXT NOT NULL,
	workflow_revision_hash TEXT NOT NULL,
	ended_run_state TEXT,
	restart_ordinal INTEGER NOT NULL,
	PRIMARY KEY (item_id, proposal_revision),
	FOREIGN KEY(item_id, proposal_revision, project_id) REFERENCES queue_proposal_revisions (item_id, proposal_revision, project_id),
	FOREIGN KEY(workflow_revision_hash) REFERENCES workflow_revisions (revision_hash),
	CHECK (proposal_revision >= 1),
	CHECK (length(run_id) > 0),
	CHECK (length(workflow_revision_hash) = 64 AND workflow_revision_hash NOT GLOB '*[^0-9a-f]*'),
	CHECK (ended_run_state IN ('CANCELLED', 'FAILED')),
	CHECK (restart_ordinal >= 0),
	UNIQUE (run_id)
)

"""
"""The launch-binding table V55 published, keyed by the proposal revision."""


PUBLISHED_QUEUE_TABLE_SHAPES: Mapping[tuple[int, str], str] = {
    (44, "queue_items"): _V44_QUEUE_ITEMS,
    # No hop between V44 and V47 moved queue_items; V48 is the first to add a
    # column, so V47 published the same bytes V44 did.
    (47, "queue_items"): _V44_QUEUE_ITEMS,
    (48, "queue_items"): _V48_QUEUE_ITEMS,
    (29, "queue_items"): _QUEUE_ITEMS_BEFORE_PHASE_D,
    (30, "queue_items"): _QUEUE_ITEMS_BEFORE_PHASE_D,
    (31, "queue_items"): _QUEUE_ITEMS_BEFORE_PHASE_D,
    (32, "queue_items"): _QUEUE_ITEMS_BEFORE_PHASE_D,
    (33, "queue_items"): _QUEUE_ITEMS_BEFORE_PHASE_D,
    (34, "queue_items"): _QUEUE_ITEMS_BEFORE_PHASE_D,
    (35, "queue_items"): _QUEUE_ITEMS_BEFORE_PHASE_D,
    (36, "queue_items"): _QUEUE_ITEMS_BEFORE_PHASE_D,
    (37, "queue_items"): _QUEUE_ITEMS_BEFORE_PHASE_D,
    (38, "queue_items"): _QUEUE_ITEMS_BEFORE_PHASE_D,
    (39, "queue_items"): _QUEUE_ITEMS_BEFORE_PHASE_D,
    (40, "queue_items"): _QUEUE_ITEMS_BEFORE_PHASE_D,
    (41, "queue_items"): _QUEUE_ITEMS_BEFORE_PHASE_D,
    (42, "queue_items"): _QUEUE_ITEMS_BEFORE_PHASE_D,
    (43, "queue_items"): _QUEUE_ITEMS_BEFORE_PHASE_D,
    # V52 gives the policy its proposal defaults and the proposal its source;
    # V44 introduced both tables and no hop between moved either, so this one
    # text is what the step that creates them materialises and what a step
    # onto V51 rebuilds.
    (44, "queue_project_policy_revisions"): _V51_QUEUE_PROJECT_POLICY_REVISIONS,
    (44, "queue_proposal_revisions"): _V51_QUEUE_PROPOSAL_REVISIONS,
    # The step that creates the four Phase-D tables materialises the record for
    # each of them, so a later hop that moves one of the two below cannot make
    # that step build a shape V44 never published.
    (44, "queue_dependency_edges"): _V44_QUEUE_DEPENDENCY_EDGES,
    (44, "queue_launch_bindings"): _V44_QUEUE_LAUNCH_BINDINGS,
    # V55 gives the binding its ending and its restart ordinal and keys it by
    # the proposal revision, so the shape every version from V44 to V54
    # published is the one text above.
    (54, "queue_launch_bindings"): _V44_QUEUE_LAUNCH_BINDINGS,
    # V56 moves no queue table, but the declaration now speaks for V56, so the
    # hop onto V55 builds the shape V55 published from this record.
    (55, "queue_launch_bindings"): _V55_QUEUE_LAUNCH_BINDINGS,
    (51, "queue_project_policy_revisions"): _V51_QUEUE_PROJECT_POLICY_REVISIONS,
    (51, "queue_proposal_revisions"): _V51_QUEUE_PROPOSAL_REVISIONS,
    # V53 widens the attempt table's vocabulary and moves neither of these, so
    # the shape V52 published is recorded here for the hop onto V52 to build.
    (52, "queue_project_policy_revisions"): _V52_QUEUE_PROJECT_POLICY_REVISIONS,
    (52, "queue_proposal_revisions"): _V52_QUEUE_PROPOSAL_REVISIONS,
}


PUBLISHED_QUEUE_LAUNCH_BINDINGS_NO_UPDATE_BEFORE_RELEASE = """
        CREATE TRIGGER queue_launch_bindings_no_update
        BEFORE UPDATE ON queue_launch_bindings BEGIN
          SELECT RAISE(ABORT, 'queue launch bindings are immutable');
        END
    """
"""The launch-binding update guard V44 through V54 published.

V55 replaces it with `queue_launch_bindings_release_only`, which lets a held
binding take the ending of its run and nothing else. `_apply_v43_to_v44`
installs this exact predecessor text, or the V44 fingerprint taken on the way
up from V43 would disagree with the one V44 actually published.
"""

PUBLISHED_QUEUE_ITEMS_TRANSITION_BEFORE_RELEASE = """
        CREATE TRIGGER queue_items_state_transition
        BEFORE UPDATE ON queue_items
        WHEN NOT (
          (OLD.state = 'OBSERVED'
           AND NEW.state = 'PROPOSED'
           AND NEW.state_version = OLD.state_version + 1
           AND NEW.current_proposal_revision = NEW.state_version
           AND NEW.workflow_lineage_id IS NULL
           AND NEW.admission_rationale IS NULL
           AND NEW.decision_authority IS NULL
           AND EXISTS (
             SELECT 1 FROM queue_proposal_revisions AS proposal
             WHERE proposal.item_id = OLD.item_id
               AND proposal.proposal_revision = NEW.current_proposal_revision
           ))
          OR
          (OLD.state = 'PROPOSED'
           AND NEW.state = 'ADMITTED'
           AND NEW.state_version = OLD.state_version + 1
           AND NEW.current_proposal_revision = OLD.current_proposal_revision
           AND NEW.workflow_lineage_id = (
             SELECT proposal.workflow_lineage_id
             FROM queue_proposal_revisions AS proposal
             WHERE proposal.item_id = OLD.item_id
               AND proposal.proposal_revision = OLD.current_proposal_revision
           )
           AND NEW.admission_rationale IS NOT NULL
           AND NEW.decision_authority IN ('OPERATOR', 'AUTOMATION_RULE')
           AND (NEW.decision_authority = 'OPERATOR' OR EXISTS (
             SELECT 1 FROM queue_proposal_revisions AS proposal
             WHERE proposal.item_id = OLD.item_id
               AND proposal.proposal_revision = OLD.current_proposal_revision
               AND proposal.automation_disposition = 'AUTOMATION_AUTHORIZED'
           )))
          OR
          (NEW.state = OLD.state
           AND NEW.state_version = OLD.state_version
           AND NEW.workflow_lineage_id IS OLD.workflow_lineage_id
           AND NEW.admission_rationale IS OLD.admission_rationale
           AND NEW.current_proposal_revision IS OLD.current_proposal_revision
           AND NEW.decision_authority IS OLD.decision_authority)
        ) BEGIN
          SELECT RAISE(ABORT, 'invalid queue item transition');
        END
    """
"""The queue item transition trigger V48 through V54 published.

V55 adds a fourth branch, letting an admitted item advance one proposal
revision when the launch it held has been given back. `_apply_v47_to_v48`
installs this exact predecessor text for the same reason `_apply_v43_to_v44`
installs the one before it.
"""
