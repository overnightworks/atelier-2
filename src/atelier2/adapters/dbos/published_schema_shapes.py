"""What a product table looked like at a schema version that is no longer current.

A migration step must materialise the shape of **its own** target, and the table
declarations in `schema.py` are only ever the shape of the current version. A
step that read a declaration instead would go on rebuilding a predecessor into
whatever the newest hop last changed -- correct until a second hop touches the
same table, and then a chain that dies in the middle with an error about a column
nobody was migrating. Every published shape a later hop moved away from is
therefore recorded here.

**This text is deliberately not derived, and deliberately not kept in step with
anything.** It is a record of what a published schema said, so the guards that
require a live bound to be written once and derived everywhere would be wrong
about it: an entry here must *not* follow the owner it once agreed with. That is
the whole reason it lives beside the schema rather than inside it. An entry is
appended when a hop leaves a shape behind, and is never edited afterwards.

An index set a hop moved away from is recorded the same way, in
`PUBLISHED_TABLE_INDEXES`, for the same reason. A version with no entry there is
one whose index set is still exactly the declaration -- most of them are -- and
the published fingerprint the migration runner takes after **every** step is
what refuses the day that stops being true, loudly, before the next step rather
than at the end. Triggers are taken from the declaration, or from the recorded
trigger text a hop whose target moved one passes in.

The predecessor shapes a *test* builds a store from are that test's own scenario
data and stay there: they are inputs to a fixture, and no production caller
reads them.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from atelier2.adapters.dbos.published_queue_shapes import (
    PUBLISHED_QUEUE_TABLE_SHAPES,
)

_V44_PROJECT_SOURCE_CONNECTION_REVISIONS = """
CREATE TABLE host_project_source_connection_revisions (
	revision_hash TEXT NOT NULL,
	project_id TEXT NOT NULL,
	source_kind TEXT NOT NULL,
	revision_number INTEGER NOT NULL,
	source_address TEXT NOT NULL,
	credential_directory TEXT NOT NULL,
	auth_method TEXT NOT NULL,
	connected_by TEXT NOT NULL,
	PRIMARY KEY (revision_hash),
	UNIQUE (project_id, source_kind, revision_number),
	UNIQUE (revision_hash, project_id, source_kind, revision_number),
	CHECK (length(revision_hash) = 64 AND revision_hash NOT GLOB '*[^0-9a-f]*'),
	CHECK (length(project_id) BETWEEN 1 AND 1024),
	CHECK (length(source_kind) BETWEEN 1 AND 64),
	CHECK (revision_number BETWEEN 1 AND 9223372036854775807),
	CHECK (length(source_address) BETWEEN 1 AND 1024),
	CHECK (length(credential_directory) BETWEEN 1 AND 4095),
	CHECK (auth_method IN ('personal-access-token')),
	CHECK (length(connected_by) BETWEEN 1 AND 1024)
)

"""


_AGENT_ATTEMPTS_BEFORE_THE_TRANSCRIPT = """
CREATE TABLE agent_attempts (
	attempt_id TEXT NOT NULL,
	node_execution_id TEXT NOT NULL,
	request_hash TEXT NOT NULL,
	executor_operational_identity TEXT NOT NULL,
	run_id TEXT NOT NULL,
	workflow_revision_hash TEXT NOT NULL,
	node_id TEXT NOT NULL,
	attempt_ordinal INTEGER NOT NULL,
	state TEXT NOT NULL,
	state_version INTEGER NOT NULL,
	process_phase TEXT NOT NULL,
	process_owner_id TEXT,
	watchdog_generation_id TEXT,
	cancellation_command_id TEXT,
	cancellation_expected_state_version INTEGER,
	replacement TEXT,
	redrive_state TEXT,
	cancellation_disposition TEXT,
	cancellation_workflow_id TEXT,
	failure_code TEXT,
	receipt_hash TEXT,
	runner_manifest_id TEXT,
	runner_generation_id TEXT,
	runner_invocation_id TEXT,
	runner_terminal_evidence_hash TEXT,
	runner_evidence_acceptance_phase TEXT NOT NULL,
	PRIMARY KEY (attempt_id),
	UNIQUE (node_execution_id, attempt_ordinal),
	FOREIGN KEY(run_id, workflow_revision_hash) REFERENCES runs (run_id, revision_hash),
	CHECK (length(attempt_id) = 64 AND attempt_id NOT GLOB '*[^0-9a-f]*'),
	CHECK (length(node_execution_id) = 64 AND node_execution_id NOT GLOB '*[^0-9a-f]*'),
	CHECK (length(request_hash) = 64 AND request_hash NOT GLOB '*[^0-9a-f]*'),
	CHECK (length(executor_operational_identity) BETWEEN 1 AND 1024),
	CHECK (length(run_id) > 0),
	CHECK (length(workflow_revision_hash) = 64 AND workflow_revision_hash NOT GLOB '*[^0-9a-f]*'),
	CHECK (length(node_id) BETWEEN 1 AND 1024),
	CHECK (attempt_ordinal IN (1, 2)),
	CHECK (process_phase IN ('NONE', 'WATCHDOG_READY', 'LAUNCH_AUTHORIZED', 'PROCESS_OBSERVED', 'CLEANUP_ATTESTED')),
	CHECK ((process_phase = 'NONE' AND process_owner_id IS NULL AND watchdog_generation_id IS NULL) OR (process_phase = 'CLEANUP_ATTESTED' AND cancellation_disposition = 'NEVER_LAUNCHED' AND process_owner_id IS NULL AND watchdog_generation_id IS NULL) OR (process_phase <> 'NONE' AND length(process_owner_id) BETWEEN 1 AND 1024 AND length(watchdog_generation_id) BETWEEN 1 AND 1024)),
	CHECK ((runner_manifest_id IS NULL AND runner_generation_id IS NULL) OR (length(runner_manifest_id) = 64 AND runner_manifest_id NOT GLOB '*[^0-9a-f]*' AND length(runner_generation_id) BETWEEN 1 AND 1024)),
	CHECK (runner_invocation_id IS NULL OR (runner_manifest_id IS NOT NULL AND length(runner_invocation_id) BETWEEN 1 AND 1024)),
	CHECK ((runner_evidence_acceptance_phase = 'NONE' AND runner_terminal_evidence_hash IS NULL) OR (runner_evidence_acceptance_phase IN ('CORE_COMMITTED', 'ACKNOWLEDGED') AND length(runner_terminal_evidence_hash) = 64 AND runner_terminal_evidence_hash NOT GLOB '*[^0-9a-f]*')),
	CHECK (runner_evidence_acceptance_phase = 'NONE' OR runner_invocation_id IS NOT NULL OR state = 'PREPARED'),
	CHECK (runner_manifest_id IS NULL OR (process_phase = 'NONE' AND process_owner_id IS NULL AND watchdog_generation_id IS NULL)),
	CHECK ((cancellation_command_id IS NULL AND cancellation_expected_state_version IS NULL AND replacement IS NULL AND redrive_state IS NULL AND cancellation_disposition IS NULL AND cancellation_workflow_id IS NULL) OR (length(cancellation_command_id) BETWEEN 1 AND 1024 AND cancellation_expected_state_version >= 0 AND replacement IN ('NONE', 'ONE') AND redrive_state IN ('PENDING', 'OWNER_NOT_LOCAL', 'CLEANUP_ATTESTED') AND length(cancellation_workflow_id) > 0 AND ((redrive_state = 'CLEANUP_ATTESTED' AND cancellation_disposition IN ('NEVER_LAUNCHED', 'EXITED_BEFORE_SIGNAL', 'REAPED_AFTER_TERM', 'REAPED_AFTER_KILL', 'OWNER_LOST_AFTER_PARENT_DEATH')) OR (redrive_state <> 'CLEANUP_ATTESTED' AND cancellation_disposition IS NULL)))),
	CHECK ((state = 'PREPARED' AND state_version = 0 AND process_phase = 'NONE' AND runner_manifest_id IS NULL AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'PREPARED' AND state_version = 1 AND process_phase = 'WATCHDOG_READY' AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'PREPARED' AND state_version >= 1 AND process_phase = 'NONE' AND runner_manifest_id IS NOT NULL AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'LAUNCH_ARMED' AND state_version = 1 AND process_phase IN ('NONE', 'LAUNCH_AUTHORIZED') AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'LAUNCH_ARMED' AND state_version >= 2 AND process_phase IN ('NONE', 'LAUNCH_AUTHORIZED', 'PROCESS_OBSERVED') AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'CANCEL_REQUESTED' AND state_version >= 1 AND cancellation_command_id IS NOT NULL AND cancellation_disposition IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state IN ('CANCELLED', 'INTERRUPTED') AND state_version >= 2 AND (process_phase = 'CLEANUP_ATTESTED' OR (process_phase = 'NONE' AND runner_manifest_id IS NOT NULL)) AND cancellation_command_id IS NOT NULL AND cancellation_disposition IS NOT NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'SUCCEEDED' AND state_version >= 2 AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NOT NULL) OR (state = 'FAILED' AND state_version >= 2 AND cancellation_command_id IS NULL AND failure_code IN ('PROCESS_EXITED_UNSUCCESSFULLY', 'PROCESS_OUTPUT_LIMIT_EXCEEDED', 'PROCESS_SUPERVISION_FAILED', 'OUTPUT_SCHEMA_REFUSED', 'AGENT_REFUSED', 'PROJECT_VERIFICATION_FAILED') AND receipt_hash IS NULL)),
	UNIQUE (cancellation_workflow_id),
	UNIQUE (receipt_hash),
	FOREIGN KEY(receipt_hash) REFERENCES agent_receipts_v2 (receipt_hash) ON DELETE RESTRICT
)

"""
"""The attempt table every schema from V27 to V36 published.

V37 adds the transcript address (#666), so a rebuild that materialises one
of those versions has to be given the columns and checks they published
rather than the ones the declaration carries now. One record serves both
keys below because no hop between them moved this table.
"""


_AGENT_ATTEMPTS_WITH_THE_TRANSCRIPT = """
CREATE TABLE agent_attempts (
	attempt_id TEXT NOT NULL,
	node_execution_id TEXT NOT NULL,
	request_hash TEXT NOT NULL,
	executor_operational_identity TEXT NOT NULL,
	run_id TEXT NOT NULL,
	workflow_revision_hash TEXT NOT NULL,
	node_id TEXT NOT NULL,
	attempt_ordinal INTEGER NOT NULL,
	state TEXT NOT NULL,
	state_version INTEGER NOT NULL,
	process_phase TEXT NOT NULL,
	process_owner_id TEXT,
	watchdog_generation_id TEXT,
	cancellation_command_id TEXT,
	cancellation_expected_state_version INTEGER,
	replacement TEXT,
	redrive_state TEXT,
	cancellation_disposition TEXT,
	cancellation_workflow_id TEXT,
	failure_code TEXT,
	receipt_hash TEXT,
	runner_manifest_id TEXT,
	runner_generation_id TEXT,
	runner_invocation_id TEXT,
	runner_terminal_evidence_hash TEXT,
	runner_evidence_acceptance_phase TEXT NOT NULL,
	transcript_artifact_hash TEXT,
	PRIMARY KEY (attempt_id),
	UNIQUE (node_execution_id, attempt_ordinal),
	FOREIGN KEY(run_id, workflow_revision_hash) REFERENCES runs (run_id, revision_hash),
	CHECK (length(attempt_id) = 64 AND attempt_id NOT GLOB '*[^0-9a-f]*'),
	CHECK (length(node_execution_id) = 64 AND node_execution_id NOT GLOB '*[^0-9a-f]*'),
	CHECK (length(request_hash) = 64 AND request_hash NOT GLOB '*[^0-9a-f]*'),
	CHECK (length(executor_operational_identity) BETWEEN 1 AND 1024),
	CHECK (length(run_id) > 0),
	CHECK (length(workflow_revision_hash) = 64 AND workflow_revision_hash NOT GLOB '*[^0-9a-f]*'),
	CHECK (length(node_id) BETWEEN 1 AND 1024),
	CHECK (attempt_ordinal IN (1, 2)),
	CHECK (transcript_artifact_hash IS NULL OR (length(transcript_artifact_hash) = 64 AND transcript_artifact_hash NOT GLOB '*[^0-9a-f]*' AND state IN ('SUCCEEDED', 'FAILED'))),
	CHECK (process_phase IN ('NONE', 'WATCHDOG_READY', 'LAUNCH_AUTHORIZED', 'PROCESS_OBSERVED', 'CLEANUP_ATTESTED')),
	CHECK ((process_phase = 'NONE' AND process_owner_id IS NULL AND watchdog_generation_id IS NULL) OR (process_phase = 'CLEANUP_ATTESTED' AND cancellation_disposition = 'NEVER_LAUNCHED' AND process_owner_id IS NULL AND watchdog_generation_id IS NULL) OR (process_phase <> 'NONE' AND length(process_owner_id) BETWEEN 1 AND 1024 AND length(watchdog_generation_id) BETWEEN 1 AND 1024)),
	CHECK ((runner_manifest_id IS NULL AND runner_generation_id IS NULL) OR (length(runner_manifest_id) = 64 AND runner_manifest_id NOT GLOB '*[^0-9a-f]*' AND length(runner_generation_id) BETWEEN 1 AND 1024)),
	CHECK (runner_invocation_id IS NULL OR (runner_manifest_id IS NOT NULL AND length(runner_invocation_id) BETWEEN 1 AND 1024)),
	CHECK ((runner_evidence_acceptance_phase = 'NONE' AND runner_terminal_evidence_hash IS NULL) OR (runner_evidence_acceptance_phase IN ('CORE_COMMITTED', 'ACKNOWLEDGED') AND length(runner_terminal_evidence_hash) = 64 AND runner_terminal_evidence_hash NOT GLOB '*[^0-9a-f]*')),
	CHECK (runner_evidence_acceptance_phase = 'NONE' OR runner_invocation_id IS NOT NULL OR state = 'PREPARED'),
	CHECK (runner_manifest_id IS NULL OR (process_phase = 'NONE' AND process_owner_id IS NULL AND watchdog_generation_id IS NULL)),
	CHECK ((cancellation_command_id IS NULL AND cancellation_expected_state_version IS NULL AND replacement IS NULL AND redrive_state IS NULL AND cancellation_disposition IS NULL AND cancellation_workflow_id IS NULL) OR (length(cancellation_command_id) BETWEEN 1 AND 1024 AND cancellation_expected_state_version >= 0 AND replacement IN ('NONE', 'ONE') AND redrive_state IN ('PENDING', 'OWNER_NOT_LOCAL', 'CLEANUP_ATTESTED') AND length(cancellation_workflow_id) > 0 AND ((redrive_state = 'CLEANUP_ATTESTED' AND cancellation_disposition IN ('NEVER_LAUNCHED', 'EXITED_BEFORE_SIGNAL', 'REAPED_AFTER_TERM', 'REAPED_AFTER_KILL', 'OWNER_LOST_AFTER_PARENT_DEATH')) OR (redrive_state <> 'CLEANUP_ATTESTED' AND cancellation_disposition IS NULL)))),
	CHECK ((state = 'PREPARED' AND state_version = 0 AND process_phase = 'NONE' AND runner_manifest_id IS NULL AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'PREPARED' AND state_version = 1 AND process_phase = 'WATCHDOG_READY' AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'PREPARED' AND state_version >= 1 AND process_phase = 'NONE' AND runner_manifest_id IS NOT NULL AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'LAUNCH_ARMED' AND state_version = 1 AND process_phase IN ('NONE', 'LAUNCH_AUTHORIZED') AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'LAUNCH_ARMED' AND state_version >= 2 AND process_phase IN ('NONE', 'LAUNCH_AUTHORIZED', 'PROCESS_OBSERVED') AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'CANCEL_REQUESTED' AND state_version >= 1 AND cancellation_command_id IS NOT NULL AND cancellation_disposition IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state IN ('CANCELLED', 'INTERRUPTED') AND state_version >= 2 AND (process_phase = 'CLEANUP_ATTESTED' OR (process_phase = 'NONE' AND runner_manifest_id IS NOT NULL)) AND cancellation_command_id IS NOT NULL AND cancellation_disposition IS NOT NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'SUCCEEDED' AND state_version >= 2 AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NOT NULL) OR (state = 'FAILED' AND state_version >= 2 AND cancellation_command_id IS NULL AND failure_code IN ('PROCESS_EXITED_UNSUCCESSFULLY', 'PROCESS_OUTPUT_LIMIT_EXCEEDED', 'PROCESS_SUPERVISION_FAILED', 'OUTPUT_SCHEMA_REFUSED', 'AGENT_REFUSED', 'PROJECT_VERIFICATION_FAILED') AND receipt_hash IS NULL)),
	UNIQUE (cancellation_workflow_id),
	UNIQUE (receipt_hash),
	FOREIGN KEY(receipt_hash) REFERENCES agent_receipts_v2 (receipt_hash) ON DELETE RESTRICT,
	FOREIGN KEY(transcript_artifact_hash) REFERENCES artifacts (artifact_hash) ON DELETE RESTRICT
)

"""
"""The attempt table V37 published, the first schema to carry a transcript.

It equals the declaration today, and is recorded anyway: the V37 hop that
materialises it is no longer the current version's hop, so the next hop to
move this table would silently give V37 the newer shape.
"""


_EFFECT_INTENTS_BEFORE_ABANDONMENT = """
CREATE TABLE effect_intents (
	logical_key TEXT NOT NULL,
	run_id TEXT NOT NULL,
	canonical_request BLOB NOT NULL,
	request_hash TEXT NOT NULL,
	workflow_revision_hash TEXT NOT NULL,
	adapter_revision TEXT NOT NULL,
	destination_identity TEXT NOT NULL,
	adapter_operational_identity TEXT NOT NULL,
	state TEXT NOT NULL,
	state_version INTEGER NOT NULL,
	reconciliation_owner_command_id TEXT,
	PRIMARY KEY (logical_key),
	UNIQUE (logical_key, run_id, workflow_revision_hash),
	FOREIGN KEY(run_id, workflow_revision_hash) REFERENCES runs (run_id, revision_hash),
	CHECK (length(logical_key) > 0),
	CHECK (length(run_id) > 0),
	CHECK (length(request_hash) = 64 AND request_hash NOT GLOB '*[^0-9a-f]*'),
	CHECK (length(workflow_revision_hash) = 64 AND workflow_revision_hash NOT GLOB '*[^0-9a-f]*'),
	CHECK (length(adapter_revision) > 0),
	CHECK (length(destination_identity) > 0),
	CHECK (length(adapter_operational_identity) > 0),
	CHECK (state IN ('PREPARED', 'WAITING_RECONCILIATION', 'RECONCILING', 'CONFIRMED')),
	CHECK (state_version >= 0),
	CHECK ((state = 'RECONCILING' AND reconciliation_owner_command_id IS NOT NULL AND length(reconciliation_owner_command_id) > 0) OR (state <> 'RECONCILING' AND reconciliation_owner_command_id IS NULL)),
	FOREIGN KEY(run_id) REFERENCES runs (run_id),
	FOREIGN KEY(workflow_revision_hash) REFERENCES workflow_revisions (revision_hash),
	FOREIGN KEY(reconciliation_owner_command_id) REFERENCES reconcile_commands (command_id) ON DELETE RESTRICT
)

"""
"""The effect-intent table V37 published, with four words for an intent.

V38 admits ABANDONED as a fifth (#705), so a rebuild that materialises the
predecessor has to be given the vocabulary that version actually stated.
"""


_EFFECT_INTENTS_WITH_ABANDONMENT = """
CREATE TABLE effect_intents (
	logical_key TEXT NOT NULL,
	run_id TEXT NOT NULL,
	canonical_request BLOB NOT NULL,
	request_hash TEXT NOT NULL,
	workflow_revision_hash TEXT NOT NULL,
	adapter_revision TEXT NOT NULL,
	destination_identity TEXT NOT NULL,
	adapter_operational_identity TEXT NOT NULL,
	state TEXT NOT NULL,
	state_version INTEGER NOT NULL,
	reconciliation_owner_command_id TEXT,
	PRIMARY KEY (logical_key),
	UNIQUE (logical_key, run_id, workflow_revision_hash),
	FOREIGN KEY(run_id, workflow_revision_hash) REFERENCES runs (run_id, revision_hash),
	CHECK (length(logical_key) > 0),
	CHECK (length(run_id) > 0),
	CHECK (length(request_hash) = 64 AND request_hash NOT GLOB '*[^0-9a-f]*'),
	CHECK (length(workflow_revision_hash) = 64 AND workflow_revision_hash NOT GLOB '*[^0-9a-f]*'),
	CHECK (length(adapter_revision) > 0),
	CHECK (length(destination_identity) > 0),
	CHECK (length(adapter_operational_identity) > 0),
	CHECK (state IN ('PREPARED', 'WAITING_RECONCILIATION', 'RECONCILING', 'CONFIRMED', 'ABANDONED')),
	CHECK (state_version >= 0),
	CHECK ((state = 'RECONCILING' AND reconciliation_owner_command_id IS NOT NULL AND length(reconciliation_owner_command_id) > 0) OR (state <> 'RECONCILING' AND reconciliation_owner_command_id IS NULL)),
	FOREIGN KEY(run_id) REFERENCES runs (run_id),
	FOREIGN KEY(workflow_revision_hash) REFERENCES workflow_revisions (revision_hash),
	FOREIGN KEY(reconciliation_owner_command_id) REFERENCES reconcile_commands (command_id) ON DELETE RESTRICT
)

"""

_TOOL_REDEMPTIONS_BOUND_TO_THE_AGENT_RECEIPT = """
CREATE TABLE tool_redemptions (
	node_execution_id TEXT NOT NULL,
	run_id TEXT NOT NULL,
	workflow_revision_hash TEXT NOT NULL,
	node_id TEXT NOT NULL,
	attempt_id TEXT NOT NULL,
	tool_revision_hash TEXT NOT NULL,
	capability TEXT NOT NULL,
	command TEXT NOT NULL,
	exit_code INTEGER NOT NULL,
	standard_output_hash TEXT NOT NULL,
	receipt_hash TEXT NOT NULL,
	PRIMARY KEY (node_execution_id),
	FOREIGN KEY(run_id, workflow_revision_hash) REFERENCES runs (run_id, revision_hash),
	CHECK (length(node_execution_id) = 64 AND node_execution_id NOT GLOB '*[^0-9a-f]*'),
	CHECK (length(run_id) > 0),
	CHECK (length(workflow_revision_hash) = 64 AND workflow_revision_hash NOT GLOB '*[^0-9a-f]*'),
	CHECK (length(node_id) BETWEEN 1 AND 1024),
	CHECK (length(attempt_id) = 64 AND attempt_id NOT GLOB '*[^0-9a-f]*'),
	CHECK (length(tool_revision_hash) = 64 AND tool_revision_hash NOT GLOB '*[^0-9a-f]*'),
	CHECK (capability IN ('run-project-verification')),
	CHECK (length(command) > 0),
	CHECK (exit_code BETWEEN -9223372036854775808 AND 9223372036854775807),
	CHECK (length(standard_output_hash) = 64 AND standard_output_hash NOT GLOB '*[^0-9a-f]*'),
	CHECK (length(receipt_hash) = 64 AND receipt_hash NOT GLOB '*[^0-9a-f]*'),
	FOREIGN KEY(node_execution_id) REFERENCES agent_receipts_v2 (node_execution_id),
	FOREIGN KEY(attempt_id) REFERENCES agent_attempts (attempt_id),
	UNIQUE (receipt_hash)
)

"""


_AGENT_ATTEMPTS_WITH_CANDIDATE_CAPTURE_FAILURE = """
CREATE TABLE agent_attempts (
	attempt_id TEXT NOT NULL,\x20
	node_execution_id TEXT NOT NULL,\x20
	request_hash TEXT NOT NULL,\x20
	executor_operational_identity TEXT NOT NULL,\x20
	run_id TEXT NOT NULL,\x20
	workflow_revision_hash TEXT NOT NULL,\x20
	node_id TEXT NOT NULL,\x20
	attempt_ordinal INTEGER NOT NULL,\x20
	state TEXT NOT NULL,\x20
	state_version INTEGER NOT NULL,\x20
	process_phase TEXT NOT NULL,\x20
	process_owner_id TEXT,\x20
	watchdog_generation_id TEXT,\x20
	cancellation_command_id TEXT,\x20
	cancellation_expected_state_version INTEGER,\x20
	replacement TEXT,\x20
	redrive_state TEXT,\x20
	cancellation_disposition TEXT,\x20
	cancellation_workflow_id TEXT,\x20
	failure_code TEXT,\x20
	receipt_hash TEXT,\x20
	runner_manifest_id TEXT,\x20
	runner_generation_id TEXT,\x20
	runner_invocation_id TEXT,\x20
	runner_terminal_evidence_hash TEXT,\x20
	runner_evidence_acceptance_phase TEXT NOT NULL,\x20
	transcript_artifact_hash TEXT,\x20
	PRIMARY KEY (attempt_id),\x20
	UNIQUE (node_execution_id, attempt_ordinal),\x20
	FOREIGN KEY(run_id, workflow_revision_hash) REFERENCES runs (run_id, revision_hash),\x20
	CHECK (length(attempt_id) = 64 AND attempt_id NOT GLOB '*[^0-9a-f]*'),\x20
	CHECK (length(node_execution_id) = 64 AND node_execution_id NOT GLOB '*[^0-9a-f]*'),\x20
	CHECK (length(request_hash) = 64 AND request_hash NOT GLOB '*[^0-9a-f]*'),\x20
	CHECK (length(executor_operational_identity) BETWEEN 1 AND 1024),\x20
	CHECK (length(run_id) > 0),\x20
	CHECK (length(workflow_revision_hash) = 64 AND workflow_revision_hash NOT GLOB '*[^0-9a-f]*'),\x20
	CHECK (length(node_id) BETWEEN 1 AND 1024),\x20
	CHECK (attempt_ordinal IN (1, 2)),\x20
	CHECK (transcript_artifact_hash IS NULL OR (length(transcript_artifact_hash) = 64 AND transcript_artifact_hash NOT GLOB '*[^0-9a-f]*' AND state IN ('SUCCEEDED', 'FAILED'))),\x20
	CHECK (process_phase IN ('NONE', 'WATCHDOG_READY', 'LAUNCH_AUTHORIZED', 'PROCESS_OBSERVED', 'CLEANUP_ATTESTED')),\x20
	CHECK ((process_phase = 'NONE' AND process_owner_id IS NULL AND watchdog_generation_id IS NULL) OR (process_phase = 'CLEANUP_ATTESTED' AND cancellation_disposition = 'NEVER_LAUNCHED' AND process_owner_id IS NULL AND watchdog_generation_id IS NULL) OR (process_phase <> 'NONE' AND length(process_owner_id) BETWEEN 1 AND 1024 AND length(watchdog_generation_id) BETWEEN 1 AND 1024)),\x20
	CHECK ((runner_manifest_id IS NULL AND runner_generation_id IS NULL) OR (length(runner_manifest_id) = 64 AND runner_manifest_id NOT GLOB '*[^0-9a-f]*' AND length(runner_generation_id) BETWEEN 1 AND 1024)),\x20
	CHECK (runner_invocation_id IS NULL OR (runner_manifest_id IS NOT NULL AND length(runner_invocation_id) BETWEEN 1 AND 1024)),\x20
	CHECK ((runner_evidence_acceptance_phase = 'NONE' AND runner_terminal_evidence_hash IS NULL) OR (runner_evidence_acceptance_phase IN ('CORE_COMMITTED', 'ACKNOWLEDGED') AND length(runner_terminal_evidence_hash) = 64 AND runner_terminal_evidence_hash NOT GLOB '*[^0-9a-f]*')),\x20
	CHECK (runner_evidence_acceptance_phase = 'NONE' OR runner_invocation_id IS NOT NULL OR state = 'PREPARED'),\x20
	CHECK (runner_manifest_id IS NULL OR (process_phase = 'NONE' AND process_owner_id IS NULL AND watchdog_generation_id IS NULL)),\x20
	CHECK ((cancellation_command_id IS NULL AND cancellation_expected_state_version IS NULL AND replacement IS NULL AND redrive_state IS NULL AND cancellation_disposition IS NULL AND cancellation_workflow_id IS NULL) OR (length(cancellation_command_id) BETWEEN 1 AND 1024 AND cancellation_expected_state_version >= 0 AND replacement IN ('NONE', 'ONE') AND redrive_state IN ('PENDING', 'OWNER_NOT_LOCAL', 'CLEANUP_ATTESTED') AND length(cancellation_workflow_id) > 0 AND ((redrive_state = 'CLEANUP_ATTESTED' AND cancellation_disposition IN ('NEVER_LAUNCHED', 'EXITED_BEFORE_SIGNAL', 'REAPED_AFTER_TERM', 'REAPED_AFTER_KILL', 'OWNER_LOST_AFTER_PARENT_DEATH')) OR (redrive_state <> 'CLEANUP_ATTESTED' AND cancellation_disposition IS NULL)))),\x20
	CHECK ((state = 'PREPARED' AND state_version = 0 AND process_phase = 'NONE' AND runner_manifest_id IS NULL AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'PREPARED' AND state_version = 1 AND process_phase = 'WATCHDOG_READY' AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'PREPARED' AND state_version >= 1 AND process_phase = 'NONE' AND runner_manifest_id IS NOT NULL AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'LAUNCH_ARMED' AND state_version = 1 AND process_phase IN ('NONE', 'LAUNCH_AUTHORIZED') AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'LAUNCH_ARMED' AND state_version >= 2 AND process_phase IN ('NONE', 'LAUNCH_AUTHORIZED', 'PROCESS_OBSERVED') AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'CANCEL_REQUESTED' AND state_version >= 1 AND cancellation_command_id IS NOT NULL AND cancellation_disposition IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state IN ('CANCELLED', 'INTERRUPTED') AND state_version >= 2 AND (process_phase = 'CLEANUP_ATTESTED' OR (process_phase = 'NONE' AND runner_manifest_id IS NOT NULL)) AND cancellation_command_id IS NOT NULL AND cancellation_disposition IS NOT NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'SUCCEEDED' AND state_version >= 2 AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NOT NULL) OR (state = 'FAILED' AND state_version >= 2 AND cancellation_command_id IS NULL AND failure_code IN ('PROCESS_EXITED_UNSUCCESSFULLY', 'PROCESS_OUTPUT_LIMIT_EXCEEDED', 'PROCESS_SUPERVISION_FAILED', 'OUTPUT_SCHEMA_REFUSED', 'AGENT_REFUSED', 'PROJECT_VERIFICATION_FAILED', 'CANDIDATE_CAPTURE_FAILED') AND receipt_hash IS NULL)),\x20
	UNIQUE (cancellation_workflow_id),\x20
	UNIQUE (receipt_hash),\x20
	FOREIGN KEY(receipt_hash) REFERENCES agent_receipts_v2 (receipt_hash) ON DELETE RESTRICT,\x20
	FOREIGN KEY(transcript_artifact_hash) REFERENCES artifacts (artifact_hash) ON DELETE RESTRICT
)

"""
"""The attempt table V39 published before model configuration became V40."""


_TOOL_REDEMPTIONS_BOUND_TO_THE_ATTEMPT = """
CREATE TABLE tool_redemptions (
	attempt_id TEXT NOT NULL,\x20
	node_execution_id TEXT NOT NULL,\x20
	run_id TEXT NOT NULL,\x20
	workflow_revision_hash TEXT NOT NULL,\x20
	node_id TEXT NOT NULL,\x20
	tool_revision_hash TEXT NOT NULL,\x20
	capability TEXT NOT NULL,\x20
	command TEXT NOT NULL,\x20
	exit_code INTEGER NOT NULL,\x20
	standard_output_hash TEXT NOT NULL,\x20
	receipt_hash TEXT NOT NULL,\x20
	PRIMARY KEY (attempt_id),\x20
	FOREIGN KEY(run_id, workflow_revision_hash) REFERENCES runs (run_id, revision_hash),\x20
	CHECK (length(node_execution_id) = 64 AND node_execution_id NOT GLOB '*[^0-9a-f]*'),\x20
	CHECK (length(run_id) > 0),\x20
	CHECK (length(workflow_revision_hash) = 64 AND workflow_revision_hash NOT GLOB '*[^0-9a-f]*'),\x20
	CHECK (length(node_id) BETWEEN 1 AND 1024),\x20
	CHECK (length(attempt_id) = 64 AND attempt_id NOT GLOB '*[^0-9a-f]*'),\x20
	CHECK (length(tool_revision_hash) = 64 AND tool_revision_hash NOT GLOB '*[^0-9a-f]*'),\x20
	CHECK (capability IN ('run-project-verification')),\x20
	CHECK (length(command) > 0),\x20
	CHECK (exit_code = 0),\x20
	CHECK (length(standard_output_hash) = 64 AND standard_output_hash NOT GLOB '*[^0-9a-f]*'),\x20
	CHECK (length(receipt_hash) = 64 AND receipt_hash NOT GLOB '*[^0-9a-f]*'),\x20
	FOREIGN KEY(attempt_id) REFERENCES agent_attempts (attempt_id),\x20
	UNIQUE (receipt_hash)
)

"""
"""The redemption table V39 published after proof ownership moved to attempts."""


_HOST_OCCUPANCY_REVISIONS = """
CREATE TABLE host_occupancy_revisions (
	revision_hash TEXT NOT NULL,\x20
	project_id TEXT NOT NULL,\x20
	lineage_id TEXT NOT NULL,\x20
	revision_number INTEGER NOT NULL,\x20
	PRIMARY KEY (revision_hash),\x20
	UNIQUE (project_id, lineage_id, revision_number),\x20
	UNIQUE (revision_hash, project_id, lineage_id, revision_number),\x20
	CHECK (length(revision_hash) = 64 AND revision_hash NOT GLOB '*[^0-9a-f]*'),\x20
	CHECK (length(project_id) BETWEEN 1 AND 1024),\x20
	CHECK (length(lineage_id) = 64 AND lineage_id NOT GLOB '*[^0-9a-f]*'),\x20
	CHECK (revision_number BETWEEN 1 AND 9223372036854775807)
)

"""

_HOST_OCCUPANCY_BINDINGS = """
CREATE TABLE host_occupancy_bindings (
	revision_hash TEXT NOT NULL,\x20
	role TEXT NOT NULL,\x20
	agent_configuration_revision_hash TEXT NOT NULL,\x20
	PRIMARY KEY (revision_hash, role),\x20
	UNIQUE (revision_hash, role, agent_configuration_revision_hash),\x20
	CHECK (length(revision_hash) = 64 AND revision_hash NOT GLOB '*[^0-9a-f]*'),\x20
	CHECK (length(role) BETWEEN 1 AND 1024),\x20
	CHECK (length(agent_configuration_revision_hash) = 64 AND agent_configuration_revision_hash NOT GLOB '*[^0-9a-f]*'),\x20
	FOREIGN KEY(revision_hash) REFERENCES host_occupancy_revisions (revision_hash)
)

"""
"""What V26 through V39 published before #711 retired lineage occupancy."""


_EFFECT_RECEIPTS_BEFORE_FORK_REFERENCE = """
CREATE TABLE effect_receipts (
	logical_key TEXT NOT NULL, 
	run_id TEXT NOT NULL, 
	canonical_request BLOB NOT NULL, 
	request_hash TEXT NOT NULL, 
	workflow_revision_hash TEXT NOT NULL, 
	adapter_revision TEXT NOT NULL, 
	destination_identity TEXT NOT NULL, 
	adapter_operational_identity TEXT NOT NULL, 
	effect_id TEXT NOT NULL, 
	result BLOB NOT NULL, 
	result_hash TEXT NOT NULL, 
	confirmation_source TEXT NOT NULL, 
	reconcile_command_id TEXT, 
	PRIMARY KEY (logical_key), 
	UNIQUE (logical_key, run_id, workflow_revision_hash, result_hash), 
	FOREIGN KEY(logical_key, run_id, workflow_revision_hash) REFERENCES effect_intents (logical_key, run_id, workflow_revision_hash), 
	CHECK (length(logical_key) > 0), 
	CHECK (length(run_id) > 0), 
	CHECK (length(request_hash) = 64 AND request_hash NOT GLOB '*[^0-9a-f]*'), 
	CHECK (length(workflow_revision_hash) = 64 AND workflow_revision_hash NOT GLOB '*[^0-9a-f]*'), 
	CHECK (length(adapter_revision) > 0), 
	CHECK (length(destination_identity) > 0), 
	CHECK (length(adapter_operational_identity) > 0), 
	CHECK (length(effect_id) > 0), 
	CHECK (length(result_hash) = 64 AND result_hash NOT GLOB '*[^0-9a-f]*'), 
	CHECK (confirmation_source IN ('ADAPTER_READBACK', 'ADAPTER_EXECUTION', 'OPERATOR_FOUND', 'OPERATOR_AUTHORIZED_EXECUTION')), 
	CHECK ((confirmation_source IN ('ADAPTER_READBACK', 'ADAPTER_EXECUTION') AND reconcile_command_id IS NULL) OR (confirmation_source IN ('OPERATOR_FOUND', 'OPERATOR_AUTHORIZED_EXECUTION') AND reconcile_command_id IS NOT NULL AND length(reconcile_command_id) > 0)), 
	FOREIGN KEY(logical_key) REFERENCES effect_intents (logical_key), 
	FOREIGN KEY(run_id) REFERENCES runs (run_id), 
	FOREIGN KEY(workflow_revision_hash) REFERENCES workflow_revisions (revision_hash), 
	FOREIGN KEY(reconcile_command_id) REFERENCES reconcile_commands (command_id)
)

"""
"""The receipt table V40 published before a fork could reference one."""


_EFFECT_RECEIPTS_WITH_FORK_REFERENCE = """
CREATE TABLE effect_receipts (
	logical_key TEXT NOT NULL,
	run_id TEXT NOT NULL,
	canonical_request BLOB NOT NULL,
	request_hash TEXT NOT NULL,
	workflow_revision_hash TEXT NOT NULL,
	adapter_revision TEXT NOT NULL,
	destination_identity TEXT NOT NULL,
	adapter_operational_identity TEXT NOT NULL,
	effect_id TEXT NOT NULL,
	result BLOB NOT NULL,
	result_hash TEXT NOT NULL,
	confirmation_source TEXT NOT NULL,
	reconcile_command_id TEXT,
	fork_source_logical_key TEXT,
	fork_source_run_id TEXT,
	fork_source_workflow_revision_hash TEXT,
	fork_source_result_hash TEXT,
	PRIMARY KEY (logical_key),
	UNIQUE (logical_key, run_id, workflow_revision_hash, result_hash),
	FOREIGN KEY(logical_key, run_id, workflow_revision_hash) REFERENCES effect_intents (logical_key, run_id, workflow_revision_hash),
	FOREIGN KEY(fork_source_logical_key, fork_source_run_id, fork_source_workflow_revision_hash, fork_source_result_hash) REFERENCES effect_receipts (logical_key, run_id, workflow_revision_hash, result_hash),
	CHECK (length(logical_key) > 0),
	CHECK (length(run_id) > 0),
	CHECK (length(request_hash) = 64 AND request_hash NOT GLOB '*[^0-9a-f]*'),
	CHECK (length(workflow_revision_hash) = 64 AND workflow_revision_hash NOT GLOB '*[^0-9a-f]*'),
	CHECK (length(adapter_revision) > 0),
	CHECK (length(destination_identity) > 0),
	CHECK (length(adapter_operational_identity) > 0),
	CHECK (length(effect_id) > 0),
	CHECK (length(result_hash) = 64 AND result_hash NOT GLOB '*[^0-9a-f]*'),
	CHECK (confirmation_source IN ('ADAPTER_READBACK', 'ADAPTER_EXECUTION', 'OPERATOR_FOUND', 'OPERATOR_AUTHORIZED_EXECUTION', 'FORK_REFERENCE')),
	CHECK ((confirmation_source IN ('ADAPTER_READBACK', 'ADAPTER_EXECUTION') AND reconcile_command_id IS NULL) OR (confirmation_source IN ('OPERATOR_FOUND', 'OPERATOR_AUTHORIZED_EXECUTION') AND reconcile_command_id IS NOT NULL AND length(reconcile_command_id) > 0) OR (confirmation_source = 'FORK_REFERENCE' AND reconcile_command_id IS NULL)),
	CHECK ((confirmation_source = 'FORK_REFERENCE' AND fork_source_logical_key IS NOT NULL AND fork_source_run_id IS NOT NULL AND fork_source_workflow_revision_hash IS NOT NULL AND fork_source_result_hash IS NOT NULL AND fork_source_result_hash = result_hash) OR (confirmation_source <> 'FORK_REFERENCE' AND fork_source_logical_key IS NULL AND fork_source_run_id IS NULL AND fork_source_workflow_revision_hash IS NULL AND fork_source_result_hash IS NULL)),
	FOREIGN KEY(logical_key) REFERENCES effect_intents (logical_key),
	FOREIGN KEY(run_id) REFERENCES runs (run_id),
	FOREIGN KEY(workflow_revision_hash) REFERENCES workflow_revisions (revision_hash),
	FOREIGN KEY(reconcile_command_id) REFERENCES reconcile_commands (command_id)
)

"""
"""The receipt table V41 published with immutable fork provenance."""


_EFFECT_INTENTS_WITH_OPERATION = """
CREATE TABLE effect_intents (
	logical_key TEXT NOT NULL,
	run_id TEXT NOT NULL,
	canonical_request BLOB NOT NULL,
	request_hash TEXT NOT NULL,
	workflow_revision_hash TEXT NOT NULL,
	adapter_revision TEXT NOT NULL,
	destination_identity TEXT NOT NULL,
	adapter_operational_identity TEXT NOT NULL,
	operation_name TEXT NOT NULL,
	state TEXT NOT NULL,
	state_version INTEGER NOT NULL,
	reconciliation_owner_command_id TEXT,
	PRIMARY KEY (logical_key),
	UNIQUE (logical_key, run_id, workflow_revision_hash),
	FOREIGN KEY(run_id, workflow_revision_hash) REFERENCES runs (run_id, revision_hash),
	CHECK (length(logical_key) > 0),
	CHECK (length(run_id) > 0),
	CHECK (length(request_hash) = 64 AND request_hash NOT GLOB '*[^0-9a-f]*'),
	CHECK (length(workflow_revision_hash) = 64 AND workflow_revision_hash NOT GLOB '*[^0-9a-f]*'),
	CHECK (length(adapter_revision) > 0),
	CHECK (length(destination_identity) > 0),
	CHECK (length(adapter_operational_identity) > 0),
	CHECK (operation_name IN ('open-pr', 'push-atelier-commit')),
	CHECK (state IN ('PREPARED', 'WAITING_RECONCILIATION', 'RECONCILING', 'CONFIRMED', 'ABANDONED')),
	CHECK (state_version >= 0),
	CHECK ((state = 'RECONCILING' AND reconciliation_owner_command_id IS NOT NULL AND length(reconciliation_owner_command_id) > 0) OR (state <> 'RECONCILING' AND reconciliation_owner_command_id IS NULL)),
	FOREIGN KEY(run_id) REFERENCES runs (run_id),
	FOREIGN KEY(workflow_revision_hash) REFERENCES workflow_revisions (revision_hash),
	FOREIGN KEY(reconciliation_owner_command_id) REFERENCES reconcile_commands (command_id) ON DELETE RESTRICT
)

"""
"""The effect intent table V42 published with a closed operation name."""


_EFFECT_RECEIPTS_WITH_OPERATION = """
CREATE TABLE effect_receipts (
	logical_key TEXT NOT NULL,
	run_id TEXT NOT NULL,
	canonical_request BLOB NOT NULL,
	request_hash TEXT NOT NULL,
	workflow_revision_hash TEXT NOT NULL,
	adapter_revision TEXT NOT NULL,
	destination_identity TEXT NOT NULL,
	adapter_operational_identity TEXT NOT NULL,
	operation_name TEXT NOT NULL,
	effect_id TEXT NOT NULL,
	result BLOB NOT NULL,
	result_hash TEXT NOT NULL,
	confirmation_source TEXT NOT NULL,
	reconcile_command_id TEXT,
	fork_source_logical_key TEXT,
	fork_source_run_id TEXT,
	fork_source_workflow_revision_hash TEXT,
	fork_source_result_hash TEXT,
	PRIMARY KEY (logical_key),
	UNIQUE (logical_key, run_id, workflow_revision_hash, result_hash),
	FOREIGN KEY(logical_key, run_id, workflow_revision_hash) REFERENCES effect_intents (logical_key, run_id, workflow_revision_hash),
	FOREIGN KEY(fork_source_logical_key, fork_source_run_id, fork_source_workflow_revision_hash, fork_source_result_hash) REFERENCES effect_receipts (logical_key, run_id, workflow_revision_hash, result_hash),
	CHECK (length(logical_key) > 0),
	CHECK (length(run_id) > 0),
	CHECK (length(request_hash) = 64 AND request_hash NOT GLOB '*[^0-9a-f]*'),
	CHECK (length(workflow_revision_hash) = 64 AND workflow_revision_hash NOT GLOB '*[^0-9a-f]*'),
	CHECK (length(adapter_revision) > 0),
	CHECK (length(destination_identity) > 0),
	CHECK (length(adapter_operational_identity) > 0),
	CHECK (operation_name IN ('open-pr', 'push-atelier-commit')),
	CHECK (length(effect_id) > 0),
	CHECK (length(result_hash) = 64 AND result_hash NOT GLOB '*[^0-9a-f]*'),
	CHECK (confirmation_source IN ('ADAPTER_READBACK', 'ADAPTER_EXECUTION', 'OPERATOR_FOUND', 'OPERATOR_AUTHORIZED_EXECUTION', 'FORK_REFERENCE')),
	CHECK ((confirmation_source IN ('ADAPTER_READBACK', 'ADAPTER_EXECUTION') AND reconcile_command_id IS NULL) OR (confirmation_source IN ('OPERATOR_FOUND', 'OPERATOR_AUTHORIZED_EXECUTION') AND reconcile_command_id IS NOT NULL AND length(reconcile_command_id) > 0) OR (confirmation_source = 'FORK_REFERENCE' AND reconcile_command_id IS NULL)),
	CHECK ((confirmation_source = 'FORK_REFERENCE' AND fork_source_logical_key IS NOT NULL AND fork_source_run_id IS NOT NULL AND fork_source_workflow_revision_hash IS NOT NULL AND fork_source_result_hash IS NOT NULL AND fork_source_result_hash = result_hash) OR (confirmation_source <> 'FORK_REFERENCE' AND fork_source_logical_key IS NULL AND fork_source_run_id IS NULL AND fork_source_workflow_revision_hash IS NULL AND fork_source_result_hash IS NULL)),
	FOREIGN KEY(logical_key) REFERENCES effect_intents (logical_key),
	FOREIGN KEY(run_id) REFERENCES runs (run_id),
	FOREIGN KEY(workflow_revision_hash) REFERENCES workflow_revisions (revision_hash),
	FOREIGN KEY(reconcile_command_id) REFERENCES reconcile_commands (command_id)
)

"""
"""The effect receipt table V42 published with a closed operation name."""


_AGENT_ATTEMPTS_WITH_PRODUCED_VALUE_REFUSED = """

CREATE TABLE agent_attempts (
	attempt_id TEXT NOT NULL,\x20
	node_execution_id TEXT NOT NULL,\x20
	request_hash TEXT NOT NULL,\x20
	executor_operational_identity TEXT NOT NULL,\x20
	run_id TEXT NOT NULL,\x20
	workflow_revision_hash TEXT NOT NULL,\x20
	node_id TEXT NOT NULL,\x20
	attempt_ordinal INTEGER NOT NULL,\x20
	state TEXT NOT NULL,\x20
	state_version INTEGER NOT NULL,\x20
	process_phase TEXT NOT NULL,\x20
	process_owner_id TEXT,\x20
	watchdog_generation_id TEXT,\x20
	cancellation_command_id TEXT,\x20
	cancellation_expected_state_version INTEGER,\x20
	replacement TEXT,\x20
	redrive_state TEXT,\x20
	cancellation_disposition TEXT,\x20
	cancellation_workflow_id TEXT,\x20
	failure_code TEXT,\x20
	receipt_hash TEXT,\x20
	runner_manifest_id TEXT,\x20
	runner_generation_id TEXT,\x20
	runner_invocation_id TEXT,\x20
	runner_terminal_evidence_hash TEXT,\x20
	runner_evidence_acceptance_phase TEXT NOT NULL,\x20
	transcript_artifact_hash TEXT,\x20
	PRIMARY KEY (attempt_id),\x20
	UNIQUE (node_execution_id, attempt_ordinal),\x20
	FOREIGN KEY(run_id, workflow_revision_hash) REFERENCES runs (run_id, revision_hash),\x20
	CHECK (length(attempt_id) = 64 AND attempt_id NOT GLOB '*[^0-9a-f]*'),\x20
	CHECK (length(node_execution_id) = 64 AND node_execution_id NOT GLOB '*[^0-9a-f]*'),\x20
	CHECK (length(request_hash) = 64 AND request_hash NOT GLOB '*[^0-9a-f]*'),\x20
	CHECK (length(executor_operational_identity) BETWEEN 1 AND 1024),\x20
	CHECK (length(run_id) > 0),\x20
	CHECK (length(workflow_revision_hash) = 64 AND workflow_revision_hash NOT GLOB '*[^0-9a-f]*'),\x20
	CHECK (length(node_id) BETWEEN 1 AND 1024),\x20
	CHECK (attempt_ordinal IN (1, 2)),\x20
	CHECK (transcript_artifact_hash IS NULL OR (length(transcript_artifact_hash) = 64 AND transcript_artifact_hash NOT GLOB '*[^0-9a-f]*' AND state IN ('SUCCEEDED', 'FAILED'))),\x20
	CHECK (process_phase IN ('NONE', 'WATCHDOG_READY', 'LAUNCH_AUTHORIZED', 'PROCESS_OBSERVED', 'CLEANUP_ATTESTED')),\x20
	CHECK ((process_phase = 'NONE' AND process_owner_id IS NULL AND watchdog_generation_id IS NULL) OR (process_phase = 'CLEANUP_ATTESTED' AND cancellation_disposition = 'NEVER_LAUNCHED' AND process_owner_id IS NULL AND watchdog_generation_id IS NULL) OR (process_phase <> 'NONE' AND length(process_owner_id) BETWEEN 1 AND 1024 AND length(watchdog_generation_id) BETWEEN 1 AND 1024)),\x20
	CHECK ((runner_manifest_id IS NULL AND runner_generation_id IS NULL) OR (length(runner_manifest_id) = 64 AND runner_manifest_id NOT GLOB '*[^0-9a-f]*' AND length(runner_generation_id) BETWEEN 1 AND 1024)),\x20
	CHECK (runner_invocation_id IS NULL OR (runner_manifest_id IS NOT NULL AND length(runner_invocation_id) BETWEEN 1 AND 1024)),\x20
	CHECK ((runner_evidence_acceptance_phase = 'NONE' AND runner_terminal_evidence_hash IS NULL) OR (runner_evidence_acceptance_phase IN ('CORE_COMMITTED', 'ACKNOWLEDGED') AND length(runner_terminal_evidence_hash) = 64 AND runner_terminal_evidence_hash NOT GLOB '*[^0-9a-f]*')),\x20
	CHECK (runner_evidence_acceptance_phase = 'NONE' OR runner_invocation_id IS NOT NULL OR state = 'PREPARED'),\x20
	CHECK (runner_manifest_id IS NULL OR (process_phase = 'NONE' AND process_owner_id IS NULL AND watchdog_generation_id IS NULL)),\x20
	CHECK ((cancellation_command_id IS NULL AND cancellation_expected_state_version IS NULL AND replacement IS NULL AND redrive_state IS NULL AND cancellation_disposition IS NULL AND cancellation_workflow_id IS NULL) OR (length(cancellation_command_id) BETWEEN 1 AND 1024 AND cancellation_expected_state_version >= 0 AND replacement IN ('NONE', 'ONE') AND redrive_state IN ('PENDING', 'OWNER_NOT_LOCAL', 'CLEANUP_ATTESTED') AND length(cancellation_workflow_id) > 0 AND ((redrive_state = 'CLEANUP_ATTESTED' AND cancellation_disposition IN ('NEVER_LAUNCHED', 'EXITED_BEFORE_SIGNAL', 'REAPED_AFTER_TERM', 'REAPED_AFTER_KILL', 'OWNER_LOST_AFTER_PARENT_DEATH')) OR (redrive_state <> 'CLEANUP_ATTESTED' AND cancellation_disposition IS NULL)))),\x20
	CHECK ((state = 'PREPARED' AND state_version = 0 AND process_phase = 'NONE' AND runner_manifest_id IS NULL AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'PREPARED' AND state_version = 1 AND process_phase = 'WATCHDOG_READY' AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'PREPARED' AND state_version >= 1 AND process_phase = 'NONE' AND runner_manifest_id IS NOT NULL AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'LAUNCH_ARMED' AND state_version = 1 AND process_phase IN ('NONE', 'LAUNCH_AUTHORIZED') AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'LAUNCH_ARMED' AND state_version >= 2 AND process_phase IN ('NONE', 'LAUNCH_AUTHORIZED', 'PROCESS_OBSERVED') AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'CANCEL_REQUESTED' AND state_version >= 1 AND cancellation_command_id IS NOT NULL AND cancellation_disposition IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state IN ('CANCELLED', 'INTERRUPTED') AND state_version >= 2 AND (process_phase = 'CLEANUP_ATTESTED' OR (process_phase = 'NONE' AND runner_manifest_id IS NOT NULL)) AND cancellation_command_id IS NOT NULL AND cancellation_disposition IS NOT NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'SUCCEEDED' AND state_version >= 2 AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NOT NULL) OR (state = 'FAILED' AND state_version >= 2 AND cancellation_command_id IS NULL AND failure_code IN ('PROCESS_EXITED_UNSUCCESSFULLY', 'PROCESS_OUTPUT_LIMIT_EXCEEDED', 'PROCESS_SUPERVISION_FAILED', 'OUTPUT_SCHEMA_REFUSED', 'AGENT_REFUSED', 'PROJECT_VERIFICATION_FAILED', 'CANDIDATE_CAPTURE_FAILED', 'CANDIDATE_UNCHANGED', 'PRODUCED_VALUE_REFUSED') AND receipt_hash IS NULL)),\x20
	UNIQUE (cancellation_workflow_id),\x20
	UNIQUE (receipt_hash),\x20
	FOREIGN KEY(receipt_hash) REFERENCES agent_receipts_v2 (receipt_hash) ON DELETE RESTRICT,\x20
	FOREIGN KEY(transcript_artifact_hash) REFERENCES artifacts (artifact_hash) ON DELETE RESTRICT
)

"""
"""What V53 published: the attempt table before any later hop moved it."""


_AGENT_ATTEMPTS_WITH_CANDIDATE_UNCHANGED = """
CREATE TABLE agent_attempts (
	attempt_id TEXT NOT NULL,\x20
	node_execution_id TEXT NOT NULL,\x20
	request_hash TEXT NOT NULL,\x20
	executor_operational_identity TEXT NOT NULL,\x20
	run_id TEXT NOT NULL,\x20
	workflow_revision_hash TEXT NOT NULL,\x20
	node_id TEXT NOT NULL,\x20
	attempt_ordinal INTEGER NOT NULL,\x20
	state TEXT NOT NULL,\x20
	state_version INTEGER NOT NULL,\x20
	process_phase TEXT NOT NULL,\x20
	process_owner_id TEXT,\x20
	watchdog_generation_id TEXT,\x20
	cancellation_command_id TEXT,\x20
	cancellation_expected_state_version INTEGER,\x20
	replacement TEXT,\x20
	redrive_state TEXT,\x20
	cancellation_disposition TEXT,\x20
	cancellation_workflow_id TEXT,\x20
	failure_code TEXT,\x20
	receipt_hash TEXT,\x20
	runner_manifest_id TEXT,\x20
	runner_generation_id TEXT,\x20
	runner_invocation_id TEXT,\x20
	runner_terminal_evidence_hash TEXT,\x20
	runner_evidence_acceptance_phase TEXT NOT NULL,\x20
	transcript_artifact_hash TEXT,\x20
	PRIMARY KEY (attempt_id),\x20
	UNIQUE (node_execution_id, attempt_ordinal),\x20
	FOREIGN KEY(run_id, workflow_revision_hash) REFERENCES runs (run_id, revision_hash),\x20
	CHECK (length(attempt_id) = 64 AND attempt_id NOT GLOB '*[^0-9a-f]*'),\x20
	CHECK (length(node_execution_id) = 64 AND node_execution_id NOT GLOB '*[^0-9a-f]*'),\x20
	CHECK (length(request_hash) = 64 AND request_hash NOT GLOB '*[^0-9a-f]*'),\x20
	CHECK (length(executor_operational_identity) BETWEEN 1 AND 1024),\x20
	CHECK (length(run_id) > 0),\x20
	CHECK (length(workflow_revision_hash) = 64 AND workflow_revision_hash NOT GLOB '*[^0-9a-f]*'),\x20
	CHECK (length(node_id) BETWEEN 1 AND 1024),\x20
	CHECK (attempt_ordinal IN (1, 2)),\x20
	CHECK (transcript_artifact_hash IS NULL OR (length(transcript_artifact_hash) = 64 AND transcript_artifact_hash NOT GLOB '*[^0-9a-f]*' AND state IN ('SUCCEEDED', 'FAILED'))),\x20
	CHECK (process_phase IN ('NONE', 'WATCHDOG_READY', 'LAUNCH_AUTHORIZED', 'PROCESS_OBSERVED', 'CLEANUP_ATTESTED')),\x20
	CHECK ((process_phase = 'NONE' AND process_owner_id IS NULL AND watchdog_generation_id IS NULL) OR (process_phase = 'CLEANUP_ATTESTED' AND cancellation_disposition = 'NEVER_LAUNCHED' AND process_owner_id IS NULL AND watchdog_generation_id IS NULL) OR (process_phase <> 'NONE' AND length(process_owner_id) BETWEEN 1 AND 1024 AND length(watchdog_generation_id) BETWEEN 1 AND 1024)),\x20
	CHECK ((runner_manifest_id IS NULL AND runner_generation_id IS NULL) OR (length(runner_manifest_id) = 64 AND runner_manifest_id NOT GLOB '*[^0-9a-f]*' AND length(runner_generation_id) BETWEEN 1 AND 1024)),\x20
	CHECK (runner_invocation_id IS NULL OR (runner_manifest_id IS NOT NULL AND length(runner_invocation_id) BETWEEN 1 AND 1024)),\x20
	CHECK ((runner_evidence_acceptance_phase = 'NONE' AND runner_terminal_evidence_hash IS NULL) OR (runner_evidence_acceptance_phase IN ('CORE_COMMITTED', 'ACKNOWLEDGED') AND length(runner_terminal_evidence_hash) = 64 AND runner_terminal_evidence_hash NOT GLOB '*[^0-9a-f]*')),\x20
	CHECK (runner_evidence_acceptance_phase = 'NONE' OR runner_invocation_id IS NOT NULL OR state = 'PREPARED'),\x20
	CHECK (runner_manifest_id IS NULL OR (process_phase = 'NONE' AND process_owner_id IS NULL AND watchdog_generation_id IS NULL)),\x20
	CHECK ((cancellation_command_id IS NULL AND cancellation_expected_state_version IS NULL AND replacement IS NULL AND redrive_state IS NULL AND cancellation_disposition IS NULL AND cancellation_workflow_id IS NULL) OR (length(cancellation_command_id) BETWEEN 1 AND 1024 AND cancellation_expected_state_version >= 0 AND replacement IN ('NONE', 'ONE') AND redrive_state IN ('PENDING', 'OWNER_NOT_LOCAL', 'CLEANUP_ATTESTED') AND length(cancellation_workflow_id) > 0 AND ((redrive_state = 'CLEANUP_ATTESTED' AND cancellation_disposition IN ('NEVER_LAUNCHED', 'EXITED_BEFORE_SIGNAL', 'REAPED_AFTER_TERM', 'REAPED_AFTER_KILL', 'OWNER_LOST_AFTER_PARENT_DEATH')) OR (redrive_state <> 'CLEANUP_ATTESTED' AND cancellation_disposition IS NULL)))),\x20
	CHECK ((state = 'PREPARED' AND state_version = 0 AND process_phase = 'NONE' AND runner_manifest_id IS NULL AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'PREPARED' AND state_version = 1 AND process_phase = 'WATCHDOG_READY' AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'PREPARED' AND state_version >= 1 AND process_phase = 'NONE' AND runner_manifest_id IS NOT NULL AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'LAUNCH_ARMED' AND state_version = 1 AND process_phase IN ('NONE', 'LAUNCH_AUTHORIZED') AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'LAUNCH_ARMED' AND state_version >= 2 AND process_phase IN ('NONE', 'LAUNCH_AUTHORIZED', 'PROCESS_OBSERVED') AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'CANCEL_REQUESTED' AND state_version >= 1 AND cancellation_command_id IS NOT NULL AND cancellation_disposition IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state IN ('CANCELLED', 'INTERRUPTED') AND state_version >= 2 AND (process_phase = 'CLEANUP_ATTESTED' OR (process_phase = 'NONE' AND runner_manifest_id IS NOT NULL)) AND cancellation_command_id IS NOT NULL AND cancellation_disposition IS NOT NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'SUCCEEDED' AND state_version >= 2 AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NOT NULL) OR (state = 'FAILED' AND state_version >= 2 AND cancellation_command_id IS NULL AND failure_code IN ('PROCESS_EXITED_UNSUCCESSFULLY', 'PROCESS_OUTPUT_LIMIT_EXCEEDED', 'PROCESS_SUPERVISION_FAILED', 'OUTPUT_SCHEMA_REFUSED', 'AGENT_REFUSED', 'PROJECT_VERIFICATION_FAILED', 'CANDIDATE_CAPTURE_FAILED', 'CANDIDATE_UNCHANGED') AND receipt_hash IS NULL)),\x20
	UNIQUE (cancellation_workflow_id),\x20
	UNIQUE (receipt_hash),\x20
	FOREIGN KEY(receipt_hash) REFERENCES agent_receipts_v2 (receipt_hash) ON DELETE RESTRICT,\x20
	FOREIGN KEY(transcript_artifact_hash) REFERENCES artifacts (artifact_hash) ON DELETE RESTRICT
)


"""
"""The attempt table V50 published, admitting the unchanged tree."""


_TWO_EFFECT_OPERATIONS = "('open-pr', 'push-atelier-commit')"
_THREE_EFFECT_OPERATIONS = "('open-pr', 'push-atelier-commit', 'claim-work-item')"
_EFFECT_INTENTS_WITH_CLAIM = _EFFECT_INTENTS_WITH_OPERATION.replace(
    _TWO_EFFECT_OPERATIONS, _THREE_EFFECT_OPERATIONS
)
_EFFECT_RECEIPTS_WITH_CLAIM = _EFFECT_RECEIPTS_WITH_OPERATION.replace(
    _TWO_EFFECT_OPERATIONS, _THREE_EFFECT_OPERATIONS
)
"""The two effect tables V54 published, admitting the work-item claim.

Derived from the record above rather than from the declaration: V54 widened
that one vocabulary and moved nothing else, and a record derived from a record
stays as frozen as the text it reads.
"""


PUBLISHED_TABLE_SHAPES: Mapping[tuple[int, str], str] = {
    **PUBLISHED_QUEUE_TABLE_SHAPES,
    (33, "host_project_source_connection_revisions"): (
        _V44_PROJECT_SOURCE_CONNECTION_REVISIONS
    ),
    (34, "host_project_source_connection_revisions"): (
        _V44_PROJECT_SOURCE_CONNECTION_REVISIONS
    ),
    (35, "host_project_source_connection_revisions"): (
        _V44_PROJECT_SOURCE_CONNECTION_REVISIONS
    ),
    (36, "host_project_source_connection_revisions"): (
        _V44_PROJECT_SOURCE_CONNECTION_REVISIONS
    ),
    (37, "host_project_source_connection_revisions"): (
        _V44_PROJECT_SOURCE_CONNECTION_REVISIONS
    ),
    (38, "host_project_source_connection_revisions"): (
        _V44_PROJECT_SOURCE_CONNECTION_REVISIONS
    ),
    (39, "host_project_source_connection_revisions"): (
        _V44_PROJECT_SOURCE_CONNECTION_REVISIONS
    ),
    (40, "host_project_source_connection_revisions"): (
        _V44_PROJECT_SOURCE_CONNECTION_REVISIONS
    ),
    (41, "host_project_source_connection_revisions"): (
        _V44_PROJECT_SOURCE_CONNECTION_REVISIONS
    ),
    (42, "host_project_source_connection_revisions"): (
        _V44_PROJECT_SOURCE_CONNECTION_REVISIONS
    ),
    (43, "host_project_source_connection_revisions"): (
        _V44_PROJECT_SOURCE_CONNECTION_REVISIONS
    ),
    (44, "host_project_source_connection_revisions"): (
        _V44_PROJECT_SOURCE_CONNECTION_REVISIONS
    ),
    (40, "effect_receipts"): _EFFECT_RECEIPTS_BEFORE_FORK_REFERENCE,
    (41, "effect_intents"): _EFFECT_INTENTS_WITH_ABANDONMENT,
    (41, "effect_receipts"): _EFFECT_RECEIPTS_WITH_FORK_REFERENCE,
    (42, "effect_intents"): _EFFECT_INTENTS_WITH_OPERATION,
    (42, "effect_receipts"): _EFFECT_RECEIPTS_WITH_OPERATION,
    # V54 widens the operation vocabulary of both tables and moves nothing
    # else, so V43 to V53 published exactly the shape V42 did.
    (53, "effect_intents"): _EFFECT_INTENTS_WITH_OPERATION,
    (53, "effect_receipts"): _EFFECT_RECEIPTS_WITH_OPERATION,
    (54, "effect_intents"): _EFFECT_INTENTS_WITH_CLAIM,
    (54, "effect_receipts"): _EFFECT_RECEIPTS_WITH_CLAIM,
    (26, "host_occupancy_revisions"): _HOST_OCCUPANCY_REVISIONS,
    (26, "host_occupancy_bindings"): _HOST_OCCUPANCY_BINDINGS,
    (39, "host_occupancy_revisions"): _HOST_OCCUPANCY_REVISIONS,
    (39, "host_occupancy_bindings"): _HOST_OCCUPANCY_BINDINGS,
    (16, "run_events"): """
CREATE TABLE run_events (
	run_id TEXT NOT NULL, 
	revision_hash TEXT NOT NULL, 
	event_sequence INTEGER NOT NULL, 
	node_id TEXT NOT NULL, 
	node_execution_id TEXT NOT NULL, 
	event_kind TEXT NOT NULL, 
	payload BLOB NOT NULL, 
	payload_hash TEXT NOT NULL, 
	receipt_logical_key TEXT, 
	receipt_result_hash TEXT, 
	event_hash TEXT NOT NULL, 
	agent_attempt_id TEXT, 
	attempt_ordinal INTEGER, 
	cancellation_command_id TEXT, 
	replacement TEXT, 
	cancellation_disposition TEXT, 
	replacement_attempt_id TEXT, 
	agent_receipt_hash TEXT, 
	PRIMARY KEY (run_id, event_sequence), 
	FOREIGN KEY(run_id, revision_hash) REFERENCES runs (run_id, revision_hash), 
	FOREIGN KEY(receipt_logical_key, run_id, revision_hash, receipt_result_hash) REFERENCES effect_receipts (logical_key, run_id, workflow_revision_hash, result_hash), 
	CHECK (event_sequence > 0), 
	CHECK (length(node_id) > 0), 
	CHECK (length(node_execution_id) = 64 AND node_execution_id NOT GLOB '*[^0-9a-f]*'), 
	CHECK (event_kind IN ('AGENT_COMPLETED', 'AGENT_FAILED', 'AGENT_CANCEL_REQUESTED', 'AGENT_CANCELLED', 'AGENT_INTERRUPTED', 'ACTION_RECONCILIATION_REQUIRED', 'ACTION_RECONCILIATION_RESOLVED', 'ACTION_COMPLETED', 'WAITING_INPUT', 'WAIT_ANSWERED', 'SUBWORKFLOW_COMPLETED')), 
	CHECK (length(payload_hash) = 64 AND payload_hash NOT GLOB '*[^0-9a-f]*'), 
	CHECK (length(event_hash) = 64 AND event_hash NOT GLOB '*[^0-9a-f]*'), 
	CHECK ((event_kind IN ('ACTION_RECONCILIATION_RESOLVED', 'ACTION_COMPLETED') AND receipt_logical_key IS NOT NULL AND length(receipt_logical_key) > 0 AND receipt_result_hash IS NOT NULL AND length(receipt_result_hash) = 64 AND receipt_result_hash NOT GLOB '*[^0-9a-f]*' AND receipt_result_hash = payload_hash) OR (event_kind NOT IN ('ACTION_RECONCILIATION_RESOLVED', 'ACTION_COMPLETED') AND receipt_logical_key IS NULL AND receipt_result_hash IS NULL)), 
	CHECK ((agent_attempt_id IS NULL AND attempt_ordinal IS NULL AND cancellation_command_id IS NULL AND replacement IS NULL AND cancellation_disposition IS NULL AND replacement_attempt_id IS NULL) OR (length(agent_attempt_id) = 64 AND agent_attempt_id NOT GLOB '*[^0-9a-f]*' AND attempt_ordinal IN (1, 2) AND ((event_kind IN ('AGENT_COMPLETED', 'AGENT_FAILED') AND cancellation_command_id IS NULL AND replacement IS NULL AND cancellation_disposition IS NULL AND replacement_attempt_id IS NULL) OR (event_kind = 'AGENT_CANCEL_REQUESTED' AND length(cancellation_command_id) BETWEEN 1 AND 1024 AND replacement IN ('NONE', 'ONE') AND cancellation_disposition IS NULL AND replacement_attempt_id IS NULL) OR (event_kind IN ('AGENT_CANCELLED', 'AGENT_INTERRUPTED') AND length(cancellation_command_id) BETWEEN 1 AND 1024 AND replacement IN ('NONE', 'ONE') AND cancellation_disposition IS NOT NULL)))), 
	CHECK ((event_kind = 'AGENT_COMPLETED' AND (agent_receipt_hash IS NULL OR (length(agent_receipt_hash) = 64 AND agent_receipt_hash NOT GLOB '*[^0-9a-f]*'))) OR (event_kind <> 'AGENT_COMPLETED' AND agent_receipt_hash IS NULL))
)

""",
    (17, "agent_attempts"): """
CREATE TABLE agent_attempts (
	attempt_id TEXT NOT NULL, 
	node_execution_id TEXT NOT NULL, 
	request_hash TEXT NOT NULL, 
	executor_operational_identity TEXT NOT NULL, 
	run_id TEXT NOT NULL, 
	workflow_revision_hash TEXT NOT NULL, 
	node_id TEXT NOT NULL, 
	attempt_ordinal INTEGER NOT NULL, 
	state TEXT NOT NULL, 
	state_version INTEGER NOT NULL, 
	process_phase TEXT NOT NULL, 
	process_owner_id TEXT, 
	watchdog_generation_id TEXT, 
	cancellation_command_id TEXT, 
	cancellation_expected_state_version INTEGER, 
	replacement TEXT, 
	redrive_state TEXT, 
	cancellation_disposition TEXT, 
	cancellation_workflow_id TEXT, 
	failure_code TEXT, 
	receipt_hash TEXT, 
	PRIMARY KEY (attempt_id), 
	UNIQUE (node_execution_id, attempt_ordinal), 
	FOREIGN KEY(run_id, workflow_revision_hash) REFERENCES runs (run_id, revision_hash), 
	CHECK (length(attempt_id) = 64 AND attempt_id NOT GLOB '*[^0-9a-f]*'), 
	CHECK (length(node_execution_id) = 64 AND node_execution_id NOT GLOB '*[^0-9a-f]*'), 
	CHECK (length(request_hash) = 64 AND request_hash NOT GLOB '*[^0-9a-f]*'), 
	CHECK (length(executor_operational_identity) BETWEEN 1 AND 1024), 
	CHECK (length(run_id) > 0), 
	CHECK (length(workflow_revision_hash) = 64 AND workflow_revision_hash NOT GLOB '*[^0-9a-f]*'), 
	CHECK (length(node_id) BETWEEN 1 AND 1024), 
	CHECK (attempt_ordinal IN (1, 2)), 
	CHECK (process_phase IN ('NONE', 'WATCHDOG_READY', 'LAUNCH_AUTHORIZED', 'PROCESS_OBSERVED', 'CLEANUP_ATTESTED')), 
	CHECK ((process_phase = 'NONE' AND process_owner_id IS NULL AND watchdog_generation_id IS NULL) OR (process_phase = 'CLEANUP_ATTESTED' AND cancellation_disposition = 'NEVER_LAUNCHED' AND process_owner_id IS NULL AND watchdog_generation_id IS NULL) OR (process_phase <> 'NONE' AND length(process_owner_id) BETWEEN 1 AND 1024 AND length(watchdog_generation_id) BETWEEN 1 AND 1024)), 
	CHECK ((cancellation_command_id IS NULL AND cancellation_expected_state_version IS NULL AND replacement IS NULL AND redrive_state IS NULL AND cancellation_disposition IS NULL AND cancellation_workflow_id IS NULL) OR (length(cancellation_command_id) BETWEEN 1 AND 1024 AND cancellation_expected_state_version >= 0 AND replacement IN ('NONE', 'ONE') AND redrive_state IN ('PENDING', 'OWNER_NOT_LOCAL', 'CLEANUP_ATTESTED') AND length(cancellation_workflow_id) > 0 AND ((redrive_state = 'CLEANUP_ATTESTED' AND cancellation_disposition IN ('NEVER_LAUNCHED', 'EXITED_BEFORE_SIGNAL', 'REAPED_AFTER_TERM', 'REAPED_AFTER_KILL', 'OWNER_LOST_AFTER_PARENT_DEATH')) OR (redrive_state <> 'CLEANUP_ATTESTED' AND cancellation_disposition IS NULL)))), 
	CHECK ((state = 'PREPARED' AND state_version = 0 AND process_phase = 'NONE' AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'PREPARED' AND state_version = 1 AND process_phase = 'WATCHDOG_READY' AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'LAUNCH_ARMED' AND state_version = 1 AND process_phase IN ('NONE', 'LAUNCH_AUTHORIZED') AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'LAUNCH_ARMED' AND state_version >= 2 AND process_phase IN ('LAUNCH_AUTHORIZED', 'PROCESS_OBSERVED') AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'CANCEL_REQUESTED' AND state_version >= 1 AND cancellation_command_id IS NOT NULL AND cancellation_disposition IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state IN ('CANCELLED', 'INTERRUPTED') AND state_version >= 2 AND process_phase = 'CLEANUP_ATTESTED' AND cancellation_command_id IS NOT NULL AND cancellation_disposition IS NOT NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'SUCCEEDED' AND state_version >= 2 AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NOT NULL) OR (state = 'FAILED' AND state_version >= 2 AND cancellation_command_id IS NULL AND failure_code IN ('PROCESS_EXITED_UNSUCCESSFULLY', 'OUTPUT_SCHEMA_REFUSED') AND receipt_hash IS NULL)), 
	UNIQUE (cancellation_workflow_id), 
	UNIQUE (receipt_hash), 
	FOREIGN KEY(receipt_hash) REFERENCES agent_receipts_v2 (receipt_hash) ON DELETE RESTRICT
)


""",
    (18, "runs"): """
CREATE TABLE runs (
	run_id TEXT NOT NULL, 
	bootstrap_workflow_id TEXT NOT NULL, 
	revision_hash TEXT NOT NULL, 
	workflow_format_version INTEGER NOT NULL, 
	agent_binding_set_hash TEXT, 
	current_node_id TEXT NOT NULL, 
	state TEXT NOT NULL, 
	state_version INTEGER NOT NULL, 
	last_event_sequence INTEGER NOT NULL, 
	terminal_hash TEXT, 
	run_configuration_revision_hash TEXT, 
	PRIMARY KEY (run_id), 
	UNIQUE (run_id, revision_hash), 
	UNIQUE (run_id, revision_hash, agent_binding_set_hash), 
	CHECK (length(run_id) > 0), 
	CHECK (length(current_node_id) > 0), 
	CHECK (workflow_format_version IN (1, 2, 3)), 
	CHECK ((workflow_format_version = 1 AND agent_binding_set_hash IS NULL) OR (workflow_format_version = 2 AND agent_binding_set_hash IS NOT NULL AND length(agent_binding_set_hash) = 64 AND agent_binding_set_hash NOT GLOB '*[^0-9a-f]*') OR (workflow_format_version = 3 AND (agent_binding_set_hash IS NULL OR (length(agent_binding_set_hash) = 64 AND agent_binding_set_hash NOT GLOB '*[^0-9a-f]*')))), 
	CHECK (state IN ('STARTED', 'WAITING_RECONCILIATION', 'WAITING_INPUT', 'COMPLETED', 'FAILED')), 
	CHECK (state_version >= 0), 
	CHECK (last_event_sequence >= 0), 
	CHECK ((state IN ('COMPLETED', 'FAILED') AND terminal_hash IS NOT NULL AND length(terminal_hash) = 64 AND terminal_hash NOT GLOB '*[^0-9a-f]*') OR (state NOT IN ('COMPLETED', 'FAILED') AND terminal_hash IS NULL)), 
	CHECK ((workflow_format_version = 3 AND run_configuration_revision_hash IS NOT NULL AND length(run_configuration_revision_hash) = 64 AND run_configuration_revision_hash NOT GLOB '*[^0-9a-f]*') OR (workflow_format_version <> 3 AND run_configuration_revision_hash IS NULL)), 
	UNIQUE (bootstrap_workflow_id), 
	FOREIGN KEY(revision_hash) REFERENCES workflow_revisions (revision_hash), 
	FOREIGN KEY(run_configuration_revision_hash) REFERENCES run_configuration_revisions (revision_hash)
)


""",
    (20, "runs"): """
CREATE TABLE runs (
	run_id TEXT NOT NULL, 
	bootstrap_workflow_id TEXT NOT NULL, 
	revision_hash TEXT NOT NULL, 
	workflow_format_version INTEGER NOT NULL, 
	agent_binding_set_hash TEXT, 
	current_node_id TEXT NOT NULL, 
	current_round_ordinal INTEGER NOT NULL, 
	state TEXT NOT NULL, 
	state_version INTEGER NOT NULL, 
	last_event_sequence INTEGER NOT NULL, 
	terminal_hash TEXT, 
	run_configuration_revision_hash TEXT, 
	PRIMARY KEY (run_id), 
	UNIQUE (run_id, revision_hash), 
	UNIQUE (run_id, revision_hash, agent_binding_set_hash), 
	CHECK (length(run_id) > 0), 
	CHECK (length(current_node_id) > 0), 
	CHECK (current_round_ordinal >= 1), 
	CHECK (workflow_format_version IN (1, 2, 3)), 
	CHECK ((workflow_format_version = 1 AND agent_binding_set_hash IS NULL) OR (workflow_format_version = 2 AND agent_binding_set_hash IS NOT NULL AND length(agent_binding_set_hash) = 64 AND agent_binding_set_hash NOT GLOB '*[^0-9a-f]*') OR (workflow_format_version = 3 AND (agent_binding_set_hash IS NULL OR (length(agent_binding_set_hash) = 64 AND agent_binding_set_hash NOT GLOB '*[^0-9a-f]*')))), 
	CHECK (state IN ('STARTED', 'WAITING_RECONCILIATION', 'WAITING_INPUT', 'COMPLETED', 'FAILED')), 
	CHECK (state_version >= 0), 
	CHECK (last_event_sequence >= 0), 
	CHECK ((state IN ('COMPLETED', 'FAILED') AND terminal_hash IS NOT NULL AND length(terminal_hash) = 64 AND terminal_hash NOT GLOB '*[^0-9a-f]*') OR (state NOT IN ('COMPLETED', 'FAILED') AND terminal_hash IS NULL)), 
	CHECK ((workflow_format_version = 3 AND run_configuration_revision_hash IS NOT NULL AND length(run_configuration_revision_hash) = 64 AND run_configuration_revision_hash NOT GLOB '*[^0-9a-f]*') OR (workflow_format_version <> 3 AND run_configuration_revision_hash IS NULL)), 
	UNIQUE (bootstrap_workflow_id), 
	FOREIGN KEY(revision_hash) REFERENCES workflow_revisions (revision_hash), 
	FOREIGN KEY(run_configuration_revision_hash) REFERENCES run_configuration_revisions (revision_hash)
)


""",
    (20, "run_events"): """
CREATE TABLE run_events (
	run_id TEXT NOT NULL, 
	revision_hash TEXT NOT NULL, 
	event_sequence INTEGER NOT NULL, 
	node_id TEXT NOT NULL, 
	node_execution_id TEXT NOT NULL, 
	round_ordinal INTEGER NOT NULL, 
	event_kind TEXT NOT NULL, 
	payload BLOB NOT NULL, 
	payload_hash TEXT NOT NULL, 
	receipt_logical_key TEXT, 
	receipt_result_hash TEXT, 
	event_hash TEXT NOT NULL, 
	agent_attempt_id TEXT, 
	attempt_ordinal INTEGER, 
	cancellation_command_id TEXT, 
	replacement TEXT, 
	cancellation_disposition TEXT, 
	replacement_attempt_id TEXT, 
	agent_receipt_hash TEXT, 
	PRIMARY KEY (run_id, event_sequence), 
	FOREIGN KEY(run_id, revision_hash) REFERENCES runs (run_id, revision_hash), 
	FOREIGN KEY(receipt_logical_key, run_id, revision_hash, receipt_result_hash) REFERENCES effect_receipts (logical_key, run_id, workflow_revision_hash, result_hash), 
	CHECK (event_sequence > 0), 
	CHECK (length(node_id) > 0), 
	CHECK (length(node_execution_id) = 64 AND node_execution_id NOT GLOB '*[^0-9a-f]*'), 
	CHECK (round_ordinal >= 1), 
	CHECK (event_kind IN ('AGENT_COMPLETED', 'AGENT_FAILED', 'AGENT_CANCEL_REQUESTED', 'AGENT_CANCELLED', 'AGENT_INTERRUPTED', 'ACTION_RECONCILIATION_REQUIRED', 'ACTION_RECONCILIATION_RESOLVED', 'ACTION_COMPLETED', 'WAITING_INPUT', 'WAIT_ANSWERED', 'SUBWORKFLOW_COMPLETED')), 
	CHECK (length(payload_hash) = 64 AND payload_hash NOT GLOB '*[^0-9a-f]*'), 
	CHECK (length(event_hash) = 64 AND event_hash NOT GLOB '*[^0-9a-f]*'), 
	CHECK ((event_kind IN ('ACTION_RECONCILIATION_RESOLVED', 'ACTION_COMPLETED') AND receipt_logical_key IS NOT NULL AND length(receipt_logical_key) > 0 AND receipt_result_hash IS NOT NULL AND length(receipt_result_hash) = 64 AND receipt_result_hash NOT GLOB '*[^0-9a-f]*' AND receipt_result_hash = payload_hash) OR (event_kind NOT IN ('ACTION_RECONCILIATION_RESOLVED', 'ACTION_COMPLETED') AND receipt_logical_key IS NULL AND receipt_result_hash IS NULL)), 
	CHECK ((agent_attempt_id IS NULL AND attempt_ordinal IS NULL AND cancellation_command_id IS NULL AND replacement IS NULL AND cancellation_disposition IS NULL AND replacement_attempt_id IS NULL) OR (length(agent_attempt_id) = 64 AND agent_attempt_id NOT GLOB '*[^0-9a-f]*' AND attempt_ordinal IN (1, 2) AND ((event_kind IN ('AGENT_COMPLETED', 'AGENT_FAILED') AND cancellation_command_id IS NULL AND replacement IS NULL AND cancellation_disposition IS NULL AND replacement_attempt_id IS NULL) OR (event_kind = 'AGENT_CANCEL_REQUESTED' AND length(cancellation_command_id) BETWEEN 1 AND 1024 AND replacement IN ('NONE', 'ONE') AND cancellation_disposition IS NULL AND replacement_attempt_id IS NULL) OR (event_kind IN ('AGENT_CANCELLED', 'AGENT_INTERRUPTED') AND length(cancellation_command_id) BETWEEN 1 AND 1024 AND replacement IN ('NONE', 'ONE') AND cancellation_disposition IS NOT NULL)))), 
	CHECK ((event_kind = 'AGENT_COMPLETED' AND (agent_receipt_hash IS NULL OR (length(agent_receipt_hash) = 64 AND agent_receipt_hash NOT GLOB '*[^0-9a-f]*'))) OR (event_kind <> 'AGENT_COMPLETED' AND agent_receipt_hash IS NULL))
)


""",
    (20, "node_execution_requests_v3"): """
CREATE TABLE node_execution_requests_v3 (
	request_hash TEXT NOT NULL, 
	node_execution_id TEXT NOT NULL, 
	run_configuration_revision_hash TEXT NOT NULL, 
	context_package_hash TEXT NOT NULL, 
	preimage BLOB NOT NULL, 
	PRIMARY KEY (node_execution_id), 
	UNIQUE (node_execution_id, request_hash), 
	CHECK (length(request_hash) = 64 AND request_hash NOT GLOB '*[^0-9a-f]*'), 
	CHECK (length(node_execution_id) = 64 AND node_execution_id NOT GLOB '*[^0-9a-f]*'), 
	CHECK (length(context_package_hash) = 64 AND context_package_hash NOT GLOB '*[^0-9a-f]*'), 
	FOREIGN KEY(context_package_hash) REFERENCES context_packages_v3 (package_hash), 
	FOREIGN KEY(run_configuration_revision_hash) REFERENCES run_configuration_revisions (revision_hash)
)


""",
    (20, "agent_receipts_v2"): """
CREATE TABLE agent_receipts_v2 (
	node_execution_id TEXT NOT NULL, 
	request_hash TEXT NOT NULL, 
	run_id TEXT NOT NULL, 
	workflow_revision_hash TEXT NOT NULL, 
	node_id TEXT NOT NULL, 
	role TEXT NOT NULL, 
	binding_set_hash TEXT NOT NULL, 
	agent_configuration_revision_hash TEXT NOT NULL, 
	auth_profile_revision_hash TEXT NOT NULL, 
	profile_id TEXT NOT NULL, 
	revision_number INTEGER NOT NULL, 
	provider_id TEXT NOT NULL, 
	auth_mode TEXT NOT NULL, 
	model TEXT NOT NULL, 
	executor_revision TEXT NOT NULL, 
	executor_operational_identity TEXT NOT NULL, 
	output_bytes BLOB NOT NULL, 
	output_hash TEXT NOT NULL, 
	receipt_hash TEXT NOT NULL, 
	round_ordinal INTEGER NOT NULL, 
	PRIMARY KEY (node_execution_id), 
	FOREIGN KEY(run_id, workflow_revision_hash, binding_set_hash, role, agent_configuration_revision_hash) REFERENCES run_agent_bindings (run_id, revision_hash, binding_set_hash, role, agent_configuration_revision_hash), 
	FOREIGN KEY(agent_configuration_revision_hash, auth_profile_revision_hash, model, executor_revision) REFERENCES agent_configuration_revisions (revision_hash, auth_profile_revision_hash, model, executor_revision), 
	FOREIGN KEY(auth_profile_revision_hash, profile_id, revision_number, provider_id, auth_mode) REFERENCES auth_profile_revisions (revision_hash, profile_id, revision_number, provider_id, auth_mode), 
	CHECK (length(node_execution_id) = 64 AND node_execution_id NOT GLOB '*[^0-9a-f]*'), 
	CHECK (length(request_hash) = 64 AND request_hash NOT GLOB '*[^0-9a-f]*'), 
	CHECK (length(run_id) > 0), 
	CHECK (length(workflow_revision_hash) = 64 AND workflow_revision_hash NOT GLOB '*[^0-9a-f]*'), 
	CHECK (length(node_id) BETWEEN 1 AND 1024), 
	CHECK (length(role) BETWEEN 1 AND 1024), 
	CHECK (length(binding_set_hash) = 64 AND binding_set_hash NOT GLOB '*[^0-9a-f]*'), 
	CHECK (length(agent_configuration_revision_hash) = 64 AND agent_configuration_revision_hash NOT GLOB '*[^0-9a-f]*'), 
	CHECK (length(auth_profile_revision_hash) = 64 AND auth_profile_revision_hash NOT GLOB '*[^0-9a-f]*'), 
	CHECK (length(profile_id) BETWEEN 1 AND 1024), 
	CHECK (revision_number BETWEEN 1 AND 9223372036854775807), 
	CHECK (length(provider_id) BETWEEN 1 AND 64), 
	CHECK (provider_id GLOB '[a-z]*'), 
	CHECK (provider_id NOT GLOB '*[^a-z0-9._-]*'), 
	CHECK (auth_mode IN ('subscription', 'api_key')), 
	CHECK (length(model) BETWEEN 1 AND 1024), 
	CHECK (length(executor_revision) BETWEEN 1 AND 1024), 
	CHECK (length(executor_operational_identity) BETWEEN 1 AND 1024), 
	CHECK (typeof(output_bytes) = 'blob' AND length(output_bytes) <= 49152), 
	CHECK (length(output_hash) = 64 AND output_hash NOT GLOB '*[^0-9a-f]*'), 
	CHECK (length(receipt_hash) = 64 AND receipt_hash NOT GLOB '*[^0-9a-f]*'), 
	CHECK (round_ordinal >= 1), 
	UNIQUE (receipt_hash)
)


""",
    (21, "agent_configuration_revisions"): """
CREATE TABLE agent_configuration_revisions (
	revision_hash TEXT NOT NULL, 
	model TEXT NOT NULL, 
	auth_profile_revision_hash TEXT NOT NULL, 
	executor_revision TEXT NOT NULL, 
	revision_format_version INTEGER NOT NULL, 
	requested_capability TEXT NOT NULL, 
	PRIMARY KEY (revision_hash), 
	UNIQUE (revision_hash, auth_profile_revision_hash, model, executor_revision), 
	UNIQUE (revision_hash, auth_profile_revision_hash, model, executor_revision, revision_format_version, requested_capability), 
	CHECK (length(revision_hash) = 64 AND revision_hash NOT GLOB '*[^0-9a-f]*'), 
	CHECK (length(model) BETWEEN 1 AND 1024), 
	CHECK (length(auth_profile_revision_hash) = 64 AND auth_profile_revision_hash NOT GLOB '*[^0-9a-f]*'), 
	CHECK (length(executor_revision) BETWEEN 1 AND 1024), 
	CHECK (revision_format_version IN (1, 2)), 
	CHECK (requested_capability IN ('headless', 'headless_with_tools', 'interactive')), 
	CHECK (revision_format_version = 2 OR requested_capability = 'headless'), 
	FOREIGN KEY(auth_profile_revision_hash) REFERENCES auth_profile_revisions (revision_hash)
)


""",
    (22, "agent_attempts"): """
CREATE TABLE agent_attempts (
	attempt_id TEXT NOT NULL, 
	node_execution_id TEXT NOT NULL, 
	request_hash TEXT NOT NULL, 
	executor_operational_identity TEXT NOT NULL, 
	run_id TEXT NOT NULL, 
	workflow_revision_hash TEXT NOT NULL, 
	node_id TEXT NOT NULL, 
	attempt_ordinal INTEGER NOT NULL, 
	state TEXT NOT NULL, 
	state_version INTEGER NOT NULL, 
	process_phase TEXT NOT NULL, 
	process_owner_id TEXT, 
	watchdog_generation_id TEXT, 
	cancellation_command_id TEXT, 
	cancellation_expected_state_version INTEGER, 
	replacement TEXT, 
	redrive_state TEXT, 
	cancellation_disposition TEXT, 
	cancellation_workflow_id TEXT, 
	failure_code TEXT, 
	receipt_hash TEXT, 
	PRIMARY KEY (attempt_id), 
	UNIQUE (node_execution_id, attempt_ordinal), 
	FOREIGN KEY(run_id, workflow_revision_hash) REFERENCES runs (run_id, revision_hash), 
	CHECK (length(attempt_id) = 64 AND attempt_id NOT GLOB '*[^0-9a-f]*'), 
	CHECK (length(node_execution_id) = 64 AND node_execution_id NOT GLOB '*[^0-9a-f]*'), 
	CHECK (length(request_hash) = 64 AND request_hash NOT GLOB '*[^0-9a-f]*'), 
	CHECK (length(executor_operational_identity) BETWEEN 1 AND 1024), 
	CHECK (length(run_id) > 0), 
	CHECK (length(workflow_revision_hash) = 64 AND workflow_revision_hash NOT GLOB '*[^0-9a-f]*'), 
	CHECK (length(node_id) BETWEEN 1 AND 1024), 
	CHECK (attempt_ordinal IN (1, 2)), 
	CHECK (process_phase IN ('NONE', 'WATCHDOG_READY', 'LAUNCH_AUTHORIZED', 'PROCESS_OBSERVED', 'CLEANUP_ATTESTED')), 
	CHECK ((process_phase = 'NONE' AND process_owner_id IS NULL AND watchdog_generation_id IS NULL) OR (process_phase = 'CLEANUP_ATTESTED' AND cancellation_disposition = 'NEVER_LAUNCHED' AND process_owner_id IS NULL AND watchdog_generation_id IS NULL) OR (process_phase <> 'NONE' AND length(process_owner_id) BETWEEN 1 AND 1024 AND length(watchdog_generation_id) BETWEEN 1 AND 1024)), 
	CHECK ((cancellation_command_id IS NULL AND cancellation_expected_state_version IS NULL AND replacement IS NULL AND redrive_state IS NULL AND cancellation_disposition IS NULL AND cancellation_workflow_id IS NULL) OR (length(cancellation_command_id) BETWEEN 1 AND 1024 AND cancellation_expected_state_version >= 0 AND replacement IN ('NONE', 'ONE') AND redrive_state IN ('PENDING', 'OWNER_NOT_LOCAL', 'CLEANUP_ATTESTED') AND length(cancellation_workflow_id) > 0 AND ((redrive_state = 'CLEANUP_ATTESTED' AND cancellation_disposition IN ('NEVER_LAUNCHED', 'EXITED_BEFORE_SIGNAL', 'REAPED_AFTER_TERM', 'REAPED_AFTER_KILL', 'OWNER_LOST_AFTER_PARENT_DEATH')) OR (redrive_state <> 'CLEANUP_ATTESTED' AND cancellation_disposition IS NULL)))), 
	CHECK ((state = 'PREPARED' AND state_version = 0 AND process_phase = 'NONE' AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'PREPARED' AND state_version = 1 AND process_phase = 'WATCHDOG_READY' AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'LAUNCH_ARMED' AND state_version = 1 AND process_phase IN ('NONE', 'LAUNCH_AUTHORIZED') AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'LAUNCH_ARMED' AND state_version >= 2 AND process_phase IN ('LAUNCH_AUTHORIZED', 'PROCESS_OBSERVED') AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'CANCEL_REQUESTED' AND state_version >= 1 AND cancellation_command_id IS NOT NULL AND cancellation_disposition IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state IN ('CANCELLED', 'INTERRUPTED') AND state_version >= 2 AND process_phase = 'CLEANUP_ATTESTED' AND cancellation_command_id IS NOT NULL AND cancellation_disposition IS NOT NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'SUCCEEDED' AND state_version >= 2 AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NOT NULL) OR (state = 'FAILED' AND state_version >= 2 AND cancellation_command_id IS NULL AND failure_code IN ('PROCESS_EXITED_UNSUCCESSFULLY', 'OUTPUT_SCHEMA_REFUSED') AND receipt_hash IS NULL)), 
	UNIQUE (cancellation_workflow_id), 
	UNIQUE (receipt_hash), 
	FOREIGN KEY(receipt_hash) REFERENCES agent_receipts_v2 (receipt_hash) ON DELETE RESTRICT
)


""",
    (23, "agent_attempts"): """
CREATE TABLE agent_attempts (
	attempt_id TEXT NOT NULL, 
	node_execution_id TEXT NOT NULL, 
	request_hash TEXT NOT NULL, 
	executor_operational_identity TEXT NOT NULL, 
	run_id TEXT NOT NULL, 
	workflow_revision_hash TEXT NOT NULL, 
	node_id TEXT NOT NULL, 
	attempt_ordinal INTEGER NOT NULL, 
	state TEXT NOT NULL, 
	state_version INTEGER NOT NULL, 
	process_phase TEXT NOT NULL, 
	process_owner_id TEXT, 
	watchdog_generation_id TEXT, 
	cancellation_command_id TEXT, 
	cancellation_expected_state_version INTEGER, 
	replacement TEXT, 
	redrive_state TEXT, 
	cancellation_disposition TEXT, 
	cancellation_workflow_id TEXT, 
	failure_code TEXT, 
	receipt_hash TEXT, 
	PRIMARY KEY (attempt_id), 
	UNIQUE (node_execution_id, attempt_ordinal), 
	FOREIGN KEY(run_id, workflow_revision_hash) REFERENCES runs (run_id, revision_hash), 
	CHECK (length(attempt_id) = 64 AND attempt_id NOT GLOB '*[^0-9a-f]*'), 
	CHECK (length(node_execution_id) = 64 AND node_execution_id NOT GLOB '*[^0-9a-f]*'), 
	CHECK (length(request_hash) = 64 AND request_hash NOT GLOB '*[^0-9a-f]*'), 
	CHECK (length(executor_operational_identity) BETWEEN 1 AND 1024), 
	CHECK (length(run_id) > 0), 
	CHECK (length(workflow_revision_hash) = 64 AND workflow_revision_hash NOT GLOB '*[^0-9a-f]*'), 
	CHECK (length(node_id) BETWEEN 1 AND 1024), 
	CHECK (attempt_ordinal IN (1, 2)), 
	CHECK (process_phase IN ('NONE', 'WATCHDOG_READY', 'LAUNCH_AUTHORIZED', 'PROCESS_OBSERVED', 'CLEANUP_ATTESTED')), 
	CHECK ((process_phase = 'NONE' AND process_owner_id IS NULL AND watchdog_generation_id IS NULL) OR (process_phase = 'CLEANUP_ATTESTED' AND cancellation_disposition = 'NEVER_LAUNCHED' AND process_owner_id IS NULL AND watchdog_generation_id IS NULL) OR (process_phase <> 'NONE' AND length(process_owner_id) BETWEEN 1 AND 1024 AND length(watchdog_generation_id) BETWEEN 1 AND 1024)), 
	CHECK ((cancellation_command_id IS NULL AND cancellation_expected_state_version IS NULL AND replacement IS NULL AND redrive_state IS NULL AND cancellation_disposition IS NULL AND cancellation_workflow_id IS NULL) OR (length(cancellation_command_id) BETWEEN 1 AND 1024 AND cancellation_expected_state_version >= 0 AND replacement IN ('NONE', 'ONE') AND redrive_state IN ('PENDING', 'OWNER_NOT_LOCAL', 'CLEANUP_ATTESTED') AND length(cancellation_workflow_id) > 0 AND ((redrive_state = 'CLEANUP_ATTESTED' AND cancellation_disposition IN ('NEVER_LAUNCHED', 'EXITED_BEFORE_SIGNAL', 'REAPED_AFTER_TERM', 'REAPED_AFTER_KILL', 'OWNER_LOST_AFTER_PARENT_DEATH')) OR (redrive_state <> 'CLEANUP_ATTESTED' AND cancellation_disposition IS NULL)))), 
	CHECK ((state = 'PREPARED' AND state_version = 0 AND process_phase = 'NONE' AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'PREPARED' AND state_version = 1 AND process_phase = 'WATCHDOG_READY' AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'LAUNCH_ARMED' AND state_version = 1 AND process_phase IN ('NONE', 'LAUNCH_AUTHORIZED') AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'LAUNCH_ARMED' AND state_version >= 2 AND process_phase IN ('LAUNCH_AUTHORIZED', 'PROCESS_OBSERVED') AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'CANCEL_REQUESTED' AND state_version >= 1 AND cancellation_command_id IS NOT NULL AND cancellation_disposition IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state IN ('CANCELLED', 'INTERRUPTED') AND state_version >= 2 AND process_phase = 'CLEANUP_ATTESTED' AND cancellation_command_id IS NOT NULL AND cancellation_disposition IS NOT NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'SUCCEEDED' AND state_version >= 2 AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NOT NULL) OR (state = 'FAILED' AND state_version >= 2 AND cancellation_command_id IS NULL AND failure_code IN ('PROCESS_EXITED_UNSUCCESSFULLY', 'OUTPUT_SCHEMA_REFUSED', 'AGENT_REFUSED') AND receipt_hash IS NULL)), 
	UNIQUE (cancellation_workflow_id), 
	UNIQUE (receipt_hash), 
	FOREIGN KEY(receipt_hash) REFERENCES agent_receipts_v2 (receipt_hash) ON DELETE RESTRICT
)

""",
    (24, "agent_attempts"): """
CREATE TABLE agent_attempts (
	attempt_id TEXT NOT NULL, 
	node_execution_id TEXT NOT NULL, 
	request_hash TEXT NOT NULL, 
	executor_operational_identity TEXT NOT NULL, 
	run_id TEXT NOT NULL, 
	workflow_revision_hash TEXT NOT NULL, 
	node_id TEXT NOT NULL, 
	attempt_ordinal INTEGER NOT NULL, 
	state TEXT NOT NULL, 
	state_version INTEGER NOT NULL, 
	process_phase TEXT NOT NULL, 
	process_owner_id TEXT, 
	watchdog_generation_id TEXT, 
	cancellation_command_id TEXT, 
	cancellation_expected_state_version INTEGER, 
	replacement TEXT, 
	redrive_state TEXT, 
	cancellation_disposition TEXT, 
	cancellation_workflow_id TEXT, 
	failure_code TEXT, 
	receipt_hash TEXT, 
	PRIMARY KEY (attempt_id), 
	UNIQUE (node_execution_id, attempt_ordinal), 
	FOREIGN KEY(run_id, workflow_revision_hash) REFERENCES runs (run_id, revision_hash), 
	CHECK (length(attempt_id) = 64 AND attempt_id NOT GLOB '*[^0-9a-f]*'), 
	CHECK (length(node_execution_id) = 64 AND node_execution_id NOT GLOB '*[^0-9a-f]*'), 
	CHECK (length(request_hash) = 64 AND request_hash NOT GLOB '*[^0-9a-f]*'), 
	CHECK (length(executor_operational_identity) BETWEEN 1 AND 1024), 
	CHECK (length(run_id) > 0), 
	CHECK (length(workflow_revision_hash) = 64 AND workflow_revision_hash NOT GLOB '*[^0-9a-f]*'), 
	CHECK (length(node_id) BETWEEN 1 AND 1024), 
	CHECK (attempt_ordinal IN (1, 2)), 
	CHECK (process_phase IN ('NONE', 'WATCHDOG_READY', 'LAUNCH_AUTHORIZED', 'PROCESS_OBSERVED', 'CLEANUP_ATTESTED')), 
	CHECK ((process_phase = 'NONE' AND process_owner_id IS NULL AND watchdog_generation_id IS NULL) OR (process_phase = 'CLEANUP_ATTESTED' AND cancellation_disposition = 'NEVER_LAUNCHED' AND process_owner_id IS NULL AND watchdog_generation_id IS NULL) OR (process_phase <> 'NONE' AND length(process_owner_id) BETWEEN 1 AND 1024 AND length(watchdog_generation_id) BETWEEN 1 AND 1024)), 
	CHECK ((cancellation_command_id IS NULL AND cancellation_expected_state_version IS NULL AND replacement IS NULL AND redrive_state IS NULL AND cancellation_disposition IS NULL AND cancellation_workflow_id IS NULL) OR (length(cancellation_command_id) BETWEEN 1 AND 1024 AND cancellation_expected_state_version >= 0 AND replacement IN ('NONE', 'ONE') AND redrive_state IN ('PENDING', 'OWNER_NOT_LOCAL', 'CLEANUP_ATTESTED') AND length(cancellation_workflow_id) > 0 AND ((redrive_state = 'CLEANUP_ATTESTED' AND cancellation_disposition IN ('NEVER_LAUNCHED', 'EXITED_BEFORE_SIGNAL', 'REAPED_AFTER_TERM', 'REAPED_AFTER_KILL', 'OWNER_LOST_AFTER_PARENT_DEATH')) OR (redrive_state <> 'CLEANUP_ATTESTED' AND cancellation_disposition IS NULL)))), 
	CHECK ((state = 'PREPARED' AND state_version = 0 AND process_phase = 'NONE' AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'PREPARED' AND state_version = 1 AND process_phase = 'WATCHDOG_READY' AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'LAUNCH_ARMED' AND state_version = 1 AND process_phase IN ('NONE', 'LAUNCH_AUTHORIZED') AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'LAUNCH_ARMED' AND state_version >= 2 AND process_phase IN ('LAUNCH_AUTHORIZED', 'PROCESS_OBSERVED') AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'CANCEL_REQUESTED' AND state_version >= 1 AND cancellation_command_id IS NOT NULL AND cancellation_disposition IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state IN ('CANCELLED', 'INTERRUPTED') AND state_version >= 2 AND process_phase = 'CLEANUP_ATTESTED' AND cancellation_command_id IS NOT NULL AND cancellation_disposition IS NOT NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'SUCCEEDED' AND state_version >= 2 AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NOT NULL) OR (state = 'FAILED' AND state_version >= 2 AND cancellation_command_id IS NULL AND failure_code IN ('PROCESS_EXITED_UNSUCCESSFULLY', 'OUTPUT_SCHEMA_REFUSED', 'AGENT_REFUSED', 'PROJECT_VERIFICATION_FAILED') AND receipt_hash IS NULL)), 
	UNIQUE (cancellation_workflow_id), 
	UNIQUE (receipt_hash), 
	FOREIGN KEY(receipt_hash) REFERENCES agent_receipts_v2 (receipt_hash) ON DELETE RESTRICT
)

""",
    (26, "agent_attempts"): """
CREATE TABLE agent_attempts (
	attempt_id TEXT NOT NULL, 
	node_execution_id TEXT NOT NULL, 
	request_hash TEXT NOT NULL, 
	executor_operational_identity TEXT NOT NULL, 
	run_id TEXT NOT NULL, 
	workflow_revision_hash TEXT NOT NULL, 
	node_id TEXT NOT NULL, 
	attempt_ordinal INTEGER NOT NULL, 
	state TEXT NOT NULL, 
	state_version INTEGER NOT NULL, 
	process_phase TEXT NOT NULL, 
	process_owner_id TEXT, 
	watchdog_generation_id TEXT, 
	cancellation_command_id TEXT, 
	cancellation_expected_state_version INTEGER, 
	replacement TEXT, 
	redrive_state TEXT, 
	cancellation_disposition TEXT, 
	cancellation_workflow_id TEXT, 
	failure_code TEXT, 
	receipt_hash TEXT, 
	PRIMARY KEY (attempt_id), 
	UNIQUE (node_execution_id, attempt_ordinal), 
	FOREIGN KEY(run_id, workflow_revision_hash) REFERENCES runs (run_id, revision_hash), 
	CHECK (length(attempt_id) = 64 AND attempt_id NOT GLOB '*[^0-9a-f]*'), 
	CHECK (length(node_execution_id) = 64 AND node_execution_id NOT GLOB '*[^0-9a-f]*'), 
	CHECK (length(request_hash) = 64 AND request_hash NOT GLOB '*[^0-9a-f]*'), 
	CHECK (length(executor_operational_identity) BETWEEN 1 AND 1024), 
	CHECK (length(run_id) > 0), 
	CHECK (length(workflow_revision_hash) = 64 AND workflow_revision_hash NOT GLOB '*[^0-9a-f]*'), 
	CHECK (length(node_id) BETWEEN 1 AND 1024), 
	CHECK (attempt_ordinal IN (1, 2)), 
	CHECK (process_phase IN ('NONE', 'WATCHDOG_READY', 'LAUNCH_AUTHORIZED', 'PROCESS_OBSERVED', 'CLEANUP_ATTESTED')), 
	CHECK ((process_phase = 'NONE' AND process_owner_id IS NULL AND watchdog_generation_id IS NULL) OR (process_phase = 'CLEANUP_ATTESTED' AND cancellation_disposition = 'NEVER_LAUNCHED' AND process_owner_id IS NULL AND watchdog_generation_id IS NULL) OR (process_phase <> 'NONE' AND length(process_owner_id) BETWEEN 1 AND 1024 AND length(watchdog_generation_id) BETWEEN 1 AND 1024)), 
	CHECK ((cancellation_command_id IS NULL AND cancellation_expected_state_version IS NULL AND replacement IS NULL AND redrive_state IS NULL AND cancellation_disposition IS NULL AND cancellation_workflow_id IS NULL) OR (length(cancellation_command_id) BETWEEN 1 AND 1024 AND cancellation_expected_state_version >= 0 AND replacement IN ('NONE', 'ONE') AND redrive_state IN ('PENDING', 'OWNER_NOT_LOCAL', 'CLEANUP_ATTESTED') AND length(cancellation_workflow_id) > 0 AND ((redrive_state = 'CLEANUP_ATTESTED' AND cancellation_disposition IN ('NEVER_LAUNCHED', 'EXITED_BEFORE_SIGNAL', 'REAPED_AFTER_TERM', 'REAPED_AFTER_KILL', 'OWNER_LOST_AFTER_PARENT_DEATH')) OR (redrive_state <> 'CLEANUP_ATTESTED' AND cancellation_disposition IS NULL)))), 
	CHECK ((state = 'PREPARED' AND state_version = 0 AND process_phase = 'NONE' AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'PREPARED' AND state_version = 1 AND process_phase = 'WATCHDOG_READY' AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'LAUNCH_ARMED' AND state_version = 1 AND process_phase IN ('NONE', 'LAUNCH_AUTHORIZED') AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'LAUNCH_ARMED' AND state_version >= 2 AND process_phase IN ('LAUNCH_AUTHORIZED', 'PROCESS_OBSERVED') AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'CANCEL_REQUESTED' AND state_version >= 1 AND cancellation_command_id IS NOT NULL AND cancellation_disposition IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state IN ('CANCELLED', 'INTERRUPTED') AND state_version >= 2 AND process_phase = 'CLEANUP_ATTESTED' AND cancellation_command_id IS NOT NULL AND cancellation_disposition IS NOT NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'SUCCEEDED' AND state_version >= 2 AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NOT NULL) OR (state = 'FAILED' AND state_version >= 2 AND cancellation_command_id IS NULL AND failure_code IN ('PROCESS_EXITED_UNSUCCESSFULLY', 'OUTPUT_SCHEMA_REFUSED', 'AGENT_REFUSED', 'PROJECT_VERIFICATION_FAILED') AND receipt_hash IS NULL)), 
	UNIQUE (cancellation_workflow_id), 
	UNIQUE (receipt_hash), 
	FOREIGN KEY(receipt_hash) REFERENCES agent_receipts_v2 (receipt_hash) ON DELETE RESTRICT
)

""",
    (27, "agent_attempts"): _AGENT_ATTEMPTS_BEFORE_THE_TRANSCRIPT,
    (30, "runs"): """
CREATE TABLE runs (
	run_id TEXT NOT NULL, 
	bootstrap_workflow_id TEXT NOT NULL, 
	revision_hash TEXT NOT NULL, 
	workflow_format_version INTEGER NOT NULL, 
	agent_binding_set_hash TEXT, 
	current_node_id TEXT NOT NULL, 
	current_round_ordinal INTEGER NOT NULL, 
	state TEXT NOT NULL, 
	state_version INTEGER NOT NULL, 
	last_event_sequence INTEGER NOT NULL, 
	terminal_hash TEXT, 
	run_configuration_revision_hash TEXT, 
	PRIMARY KEY (run_id), 
	UNIQUE (run_id, revision_hash), 
	UNIQUE (run_id, revision_hash, agent_binding_set_hash), 
	CHECK (length(run_id) > 0), 
	CHECK (length(current_node_id) > 0), 
	CHECK (current_round_ordinal >= 1), 
	CHECK (workflow_format_version IN (1, 2, 3)), 
	CHECK ((workflow_format_version = 1 AND agent_binding_set_hash IS NULL) OR (workflow_format_version = 2 AND agent_binding_set_hash IS NOT NULL AND length(agent_binding_set_hash) = 64 AND agent_binding_set_hash NOT GLOB '*[^0-9a-f]*') OR (workflow_format_version = 3 AND (agent_binding_set_hash IS NULL OR (length(agent_binding_set_hash) = 64 AND agent_binding_set_hash NOT GLOB '*[^0-9a-f]*')))), 
	CHECK (state IN ('STARTED', 'WAITING_RECONCILIATION', 'WAITING_INPUT', 'COMPLETED', 'FAILED', 'CANCELLED')), 
	CHECK (state_version >= 0), 
	CHECK (last_event_sequence >= 0), 
	CHECK ((state IN ('COMPLETED', 'FAILED', 'CANCELLED') AND terminal_hash IS NOT NULL AND length(terminal_hash) = 64 AND terminal_hash NOT GLOB '*[^0-9a-f]*') OR (state NOT IN ('COMPLETED', 'FAILED', 'CANCELLED') AND terminal_hash IS NULL)), 
	CHECK ((workflow_format_version = 3 AND run_configuration_revision_hash IS NOT NULL AND length(run_configuration_revision_hash) = 64 AND run_configuration_revision_hash NOT GLOB '*[^0-9a-f]*') OR (workflow_format_version <> 3 AND run_configuration_revision_hash IS NULL)), 
	UNIQUE (bootstrap_workflow_id), 
	FOREIGN KEY(revision_hash) REFERENCES workflow_revisions (revision_hash), 
	FOREIGN KEY(run_configuration_revision_hash) REFERENCES run_configuration_revisions (revision_hash)
)

""",
    (34, "wait_answers"): """
CREATE TABLE wait_answers (
	run_id TEXT NOT NULL, 
	revision_hash TEXT NOT NULL, 
	node_id TEXT NOT NULL, 
	node_execution_id TEXT NOT NULL, 
	round_ordinal INTEGER NOT NULL, 
	answer_bytes BLOB NOT NULL, 
	answer_hash TEXT NOT NULL, 
	answer_workflow_id TEXT NOT NULL, 
	state TEXT NOT NULL, 
	state_version INTEGER NOT NULL, 
	PRIMARY KEY (node_execution_id), 
	FOREIGN KEY(run_id, revision_hash) REFERENCES runs (run_id, revision_hash), 
	CHECK (length(node_id) > 0), 
	CHECK (round_ordinal >= 1), 
	CHECK (length(node_execution_id) = 64 AND node_execution_id NOT GLOB '*[^0-9a-f]*'), 
	CHECK (length(answer_hash) = 64 AND answer_hash NOT GLOB '*[^0-9a-f]*'), 
	CHECK (length(answer_workflow_id) > 0), 
	CHECK (state IN ('PENDING', 'APPLIED')), 
	CHECK (state_version IN (0, 1)), 
	CHECK ((state = 'PENDING' AND state_version = 0) OR (state = 'APPLIED' AND state_version = 1)), 
	UNIQUE (answer_workflow_id)
)

""",
    (34, "run_events"): """
CREATE TABLE run_events (
	run_id TEXT NOT NULL, 
	revision_hash TEXT NOT NULL, 
	event_sequence INTEGER NOT NULL, 
	node_id TEXT NOT NULL, 
	node_execution_id TEXT NOT NULL, 
	round_ordinal INTEGER NOT NULL, 
	event_kind TEXT NOT NULL, 
	payload BLOB NOT NULL, 
	payload_hash TEXT NOT NULL, 
	receipt_logical_key TEXT, 
	receipt_result_hash TEXT, 
	event_hash TEXT NOT NULL, 
	agent_attempt_id TEXT, 
	attempt_ordinal INTEGER, 
	cancellation_command_id TEXT, 
	replacement TEXT, 
	cancellation_disposition TEXT, 
	replacement_attempt_id TEXT, 
	agent_receipt_hash TEXT, 
	PRIMARY KEY (run_id, event_sequence), 
	FOREIGN KEY(run_id, revision_hash) REFERENCES runs (run_id, revision_hash), 
	FOREIGN KEY(receipt_logical_key, run_id, revision_hash, receipt_result_hash) REFERENCES effect_receipts (logical_key, run_id, workflow_revision_hash, result_hash), 
	CHECK (event_sequence > 0), 
	CHECK (length(node_id) > 0), 
	CHECK (length(node_execution_id) = 64 AND node_execution_id NOT GLOB '*[^0-9a-f]*'), 
	CHECK (round_ordinal >= 1), 
	CHECK (event_kind IN ('AGENT_COMPLETED', 'AGENT_FAILED', 'AGENT_CANCEL_REQUESTED', 'AGENT_CANCELLED', 'AGENT_INTERRUPTED', 'ACTION_RECONCILIATION_REQUIRED', 'ACTION_RECONCILIATION_RESOLVED', 'ACTION_COMPLETED', 'WAITING_INPUT', 'WAIT_ANSWERED', 'SUBWORKFLOW_COMPLETED')), 
	CHECK (length(payload_hash) = 64 AND payload_hash NOT GLOB '*[^0-9a-f]*'), 
	CHECK (length(event_hash) = 64 AND event_hash NOT GLOB '*[^0-9a-f]*'), 
	CHECK ((event_kind IN ('ACTION_RECONCILIATION_RESOLVED', 'ACTION_COMPLETED') AND receipt_logical_key IS NOT NULL AND length(receipt_logical_key) > 0 AND receipt_result_hash IS NOT NULL AND length(receipt_result_hash) = 64 AND receipt_result_hash NOT GLOB '*[^0-9a-f]*' AND receipt_result_hash = payload_hash) OR (event_kind NOT IN ('ACTION_RECONCILIATION_RESOLVED', 'ACTION_COMPLETED') AND receipt_logical_key IS NULL AND receipt_result_hash IS NULL)), 
	CHECK ((agent_attempt_id IS NULL AND attempt_ordinal IS NULL AND cancellation_command_id IS NULL AND replacement IS NULL AND cancellation_disposition IS NULL AND replacement_attempt_id IS NULL) OR (length(agent_attempt_id) = 64 AND agent_attempt_id NOT GLOB '*[^0-9a-f]*' AND attempt_ordinal IN (1, 2) AND ((event_kind IN ('AGENT_COMPLETED', 'AGENT_FAILED') AND cancellation_command_id IS NULL AND replacement IS NULL AND cancellation_disposition IS NULL AND replacement_attempt_id IS NULL) OR (event_kind = 'AGENT_CANCEL_REQUESTED' AND length(cancellation_command_id) BETWEEN 1 AND 1024 AND replacement IN ('NONE', 'ONE') AND cancellation_disposition IS NULL AND replacement_attempt_id IS NULL) OR (event_kind IN ('AGENT_CANCELLED', 'AGENT_INTERRUPTED') AND length(cancellation_command_id) BETWEEN 1 AND 1024 AND replacement IN ('NONE', 'ONE') AND cancellation_disposition IS NOT NULL)))), 
	CHECK ((event_kind = 'AGENT_COMPLETED' AND (agent_receipt_hash IS NULL OR (length(agent_receipt_hash) = 64 AND agent_receipt_hash NOT GLOB '*[^0-9a-f]*'))) OR (event_kind <> 'AGENT_COMPLETED' AND agent_receipt_hash IS NULL))
)

""",
    (35, "run_events"): """
CREATE TABLE run_events (
	run_id TEXT NOT NULL, 
	revision_hash TEXT NOT NULL, 
	event_sequence INTEGER NOT NULL, 
	node_id TEXT NOT NULL, 
	node_execution_id TEXT NOT NULL, 
	round_ordinal INTEGER NOT NULL, 
	event_kind TEXT NOT NULL, 
	payload BLOB NOT NULL, 
	payload_hash TEXT NOT NULL, 
	receipt_logical_key TEXT, 
	receipt_result_hash TEXT, 
	event_hash TEXT NOT NULL, 
	agent_attempt_id TEXT, 
	attempt_ordinal INTEGER, 
	cancellation_command_id TEXT, 
	replacement TEXT, 
	cancellation_disposition TEXT, 
	replacement_attempt_id TEXT, 
	agent_receipt_hash TEXT, 
	PRIMARY KEY (run_id, event_sequence), 
	FOREIGN KEY(run_id, revision_hash) REFERENCES runs (run_id, revision_hash), 
	FOREIGN KEY(receipt_logical_key, run_id, revision_hash, receipt_result_hash) REFERENCES effect_receipts (logical_key, run_id, workflow_revision_hash, result_hash), 
	CHECK (event_sequence > 0), 
	CHECK (length(node_id) > 0), 
	CHECK (length(node_execution_id) = 64 AND node_execution_id NOT GLOB '*[^0-9a-f]*'), 
	CHECK (round_ordinal >= 1), 
	CHECK (event_kind IN ('AGENT_COMPLETED', 'AGENT_FAILED', 'AGENT_CANCEL_REQUESTED', 'AGENT_CANCELLED', 'AGENT_INTERRUPTED', 'ACTION_RECONCILIATION_REQUIRED', 'ACTION_RECONCILIATION_RESOLVED', 'ACTION_COMPLETED', 'WAITING_INPUT', 'WAIT_ANSWERED', 'WAIT_CANCELLED', 'SUBWORKFLOW_COMPLETED')), 
	CHECK (length(payload_hash) = 64 AND payload_hash NOT GLOB '*[^0-9a-f]*'), 
	CHECK (length(event_hash) = 64 AND event_hash NOT GLOB '*[^0-9a-f]*'), 
	CHECK ((event_kind IN ('ACTION_RECONCILIATION_RESOLVED', 'ACTION_COMPLETED') AND receipt_logical_key IS NOT NULL AND length(receipt_logical_key) > 0 AND receipt_result_hash IS NOT NULL AND length(receipt_result_hash) = 64 AND receipt_result_hash NOT GLOB '*[^0-9a-f]*' AND receipt_result_hash = payload_hash) OR (event_kind NOT IN ('ACTION_RECONCILIATION_RESOLVED', 'ACTION_COMPLETED') AND receipt_logical_key IS NULL AND receipt_result_hash IS NULL)), 
	CHECK ((agent_attempt_id IS NULL AND attempt_ordinal IS NULL AND cancellation_command_id IS NULL AND replacement IS NULL AND cancellation_disposition IS NULL AND replacement_attempt_id IS NULL) OR (length(agent_attempt_id) = 64 AND agent_attempt_id NOT GLOB '*[^0-9a-f]*' AND attempt_ordinal IN (1, 2) AND ((event_kind IN ('AGENT_COMPLETED', 'AGENT_FAILED') AND cancellation_command_id IS NULL AND replacement IS NULL AND cancellation_disposition IS NULL AND replacement_attempt_id IS NULL) OR (event_kind = 'AGENT_CANCEL_REQUESTED' AND length(cancellation_command_id) BETWEEN 1 AND 1024 AND replacement IN ('NONE', 'ONE') AND cancellation_disposition IS NULL AND replacement_attempt_id IS NULL) OR (event_kind IN ('AGENT_CANCELLED', 'AGENT_INTERRUPTED') AND length(cancellation_command_id) BETWEEN 1 AND 1024 AND replacement IN ('NONE', 'ONE') AND cancellation_disposition IS NOT NULL)))), 
	CHECK ((event_kind = 'AGENT_COMPLETED' AND (agent_receipt_hash IS NULL OR (length(agent_receipt_hash) = 64 AND agent_receipt_hash NOT GLOB '*[^0-9a-f]*'))) OR (event_kind <> 'AGENT_COMPLETED' AND agent_receipt_hash IS NULL))
)

""",
    (36, "agent_attempts"): _AGENT_ATTEMPTS_BEFORE_THE_TRANSCRIPT,
    (37, "agent_attempts"): _AGENT_ATTEMPTS_WITH_THE_TRANSCRIPT,
    (37, "effect_intents"): _EFFECT_INTENTS_BEFORE_ABANDONMENT,
    # V38 rebuilt `effect_intents` alone, so an attempt's shape at 38 is still
    # exactly the text 37 published, and the hop off 38 parks that same text.
    (38, "agent_attempts"): _AGENT_ATTEMPTS_WITH_THE_TRANSCRIPT,
    (38, "effect_intents"): _EFFECT_INTENTS_WITH_ABANDONMENT,
    # V39 re-owns a redemption from the success-only agent receipt to the
    # attempt itself, so 38 is the last version that published this shape.
    (38, "tool_redemptions"): _TOOL_REDEMPTIONS_BOUND_TO_THE_AGENT_RECEIPT,
    (39, "agent_attempts"): _AGENT_ATTEMPTS_WITH_CANDIDATE_CAPTURE_FAILURE,
    # V40 through V49 left the attempt table untouched, so the hop off 49 parks
    # exactly the text V39 published; V50 widens its failure-code vocabulary.
    (49, "agent_attempts"): _AGENT_ATTEMPTS_WITH_CANDIDATE_CAPTURE_FAILURE,
    # V51 adds the permission ledger and moves no table, so the shape V50
    # published is recorded here unchanged: the declaration speaks for the
    # current version alone, and the hop onto 50 must still materialise it.
    (50, "agent_attempts"): _AGENT_ATTEMPTS_WITH_CANDIDATE_UNCHANGED,
    # V53 widens the vocabulary again and moves no table, so V51 and V52
    # published the same shape V50 did.
    (52, "agent_attempts"): _AGENT_ATTEMPTS_WITH_CANDIDATE_UNCHANGED,
    # The hop onto V53 must still materialise the shape V53 published, which
    # the declaration stopped speaking for the moment V54 became current.
    (53, "agent_attempts"): _AGENT_ATTEMPTS_WITH_PRODUCED_VALUE_REFUSED,
    (39, "tool_redemptions"): _TOOL_REDEMPTIONS_BOUND_TO_THE_ATTEMPT,
    # V15 introduced the table in this shape and no hop before V39 moved it,
    # so the step that adds it builds the record rather than today's table.
    (15, "tool_redemptions"): _TOOL_REDEMPTIONS_BOUND_TO_THE_AGENT_RECEIPT,
}

# V34 through V45 published one identical wait-answer table, and V35 through
# V45 one identical run-event table. V46 adds attribution to both, so the
# immediate predecessor must remain materializable byte-for-byte; reusing the
# older frozen records is safe because they are published text, never today's
# declarations.
PUBLISHED_TABLE_SHAPES = {
    **PUBLISHED_TABLE_SHAPES,
    (45, "wait_answers"): PUBLISHED_TABLE_SHAPES[(34, "wait_answers")],
    (45, "run_events"): PUBLISHED_TABLE_SHAPES[(35, "run_events")],
    # V46 adds explicit answer attribution; V47 changes only catalog intakes.
    (46, "run_events"): """
CREATE TABLE run_events (
 run_id TEXT NOT NULL, revision_hash TEXT NOT NULL, event_sequence INTEGER NOT NULL, node_id TEXT NOT NULL, node_execution_id TEXT NOT NULL, round_ordinal INTEGER NOT NULL, event_kind TEXT NOT NULL, wait_answer_actor TEXT, payload BLOB NOT NULL, payload_hash TEXT NOT NULL, receipt_logical_key TEXT, receipt_result_hash TEXT, event_hash TEXT NOT NULL, agent_attempt_id TEXT, attempt_ordinal INTEGER, cancellation_command_id TEXT, replacement TEXT, cancellation_disposition TEXT, replacement_attempt_id TEXT, agent_receipt_hash TEXT, PRIMARY KEY (run_id, event_sequence), FOREIGN KEY(run_id, revision_hash) REFERENCES runs (run_id, revision_hash), FOREIGN KEY(receipt_logical_key, run_id, revision_hash, receipt_result_hash) REFERENCES effect_receipts (logical_key, run_id, workflow_revision_hash, result_hash), CHECK (event_sequence > 0), CHECK (length(node_id) > 0), CHECK (length(node_execution_id) = 64 AND node_execution_id NOT GLOB '*[^0-9a-f]*'), CHECK (round_ordinal >= 1), CHECK (event_kind IN ('AGENT_COMPLETED', 'AGENT_FAILED', 'AGENT_CANCEL_REQUESTED', 'AGENT_CANCELLED', 'AGENT_INTERRUPTED', 'ACTION_RECONCILIATION_REQUIRED', 'ACTION_RECONCILIATION_RESOLVED', 'ACTION_COMPLETED', 'WAITING_INPUT', 'WAIT_ANSWERED', 'WAIT_CANCELLED', 'SUBWORKFLOW_COMPLETED')), CHECK ((event_kind = 'WAITING_INPUT' AND wait_answer_actor IN ('operator')) OR (event_kind <> 'WAITING_INPUT' AND wait_answer_actor IS NULL)), CHECK (length(payload_hash) = 64 AND payload_hash NOT GLOB '*[^0-9a-f]*'), CHECK (length(event_hash) = 64 AND event_hash NOT GLOB '*[^0-9a-f]*'), CHECK ((event_kind IN ('ACTION_RECONCILIATION_RESOLVED', 'ACTION_COMPLETED') AND receipt_logical_key IS NOT NULL AND length(receipt_logical_key) > 0 AND receipt_result_hash IS NOT NULL AND length(receipt_result_hash) = 64 AND receipt_result_hash NOT GLOB '*[^0-9a-f]*' AND receipt_result_hash = payload_hash) OR (event_kind NOT IN ('ACTION_RECONCILIATION_RESOLVED', 'ACTION_COMPLETED') AND receipt_logical_key IS NULL AND receipt_result_hash IS NULL)), CHECK ((agent_attempt_id IS NULL AND attempt_ordinal IS NULL AND cancellation_command_id IS NULL AND replacement IS NULL AND cancellation_disposition IS NULL AND replacement_attempt_id IS NULL) OR (length(agent_attempt_id) = 64 AND agent_attempt_id NOT GLOB '*[^0-9a-f]*' AND attempt_ordinal IN (1, 2) AND ((event_kind IN ('AGENT_COMPLETED', 'AGENT_FAILED') AND cancellation_command_id IS NULL AND replacement IS NULL AND cancellation_disposition IS NULL AND replacement_attempt_id IS NULL) OR (event_kind = 'AGENT_CANCEL_REQUESTED' AND length(cancellation_command_id) BETWEEN 1 AND 1024 AND replacement IN ('NONE', 'ONE') AND cancellation_disposition IS NULL AND replacement_attempt_id IS NULL) OR (event_kind IN ('AGENT_CANCELLED', 'AGENT_INTERRUPTED') AND length(cancellation_command_id) BETWEEN 1 AND 1024 AND replacement IN ('NONE', 'ONE') AND cancellation_disposition IS NOT NULL)))), CHECK ((event_kind = 'AGENT_COMPLETED' AND (agent_receipt_hash IS NULL OR (length(agent_receipt_hash) = 64 AND agent_receipt_hash NOT GLOB '*[^0-9a-f]*'))) OR (event_kind <> 'AGENT_COMPLETED' AND agent_receipt_hash IS NULL))
)
""",
    (46, "wait_answers"): """
CREATE TABLE wait_answers (
 run_id TEXT NOT NULL, revision_hash TEXT NOT NULL, node_id TEXT NOT NULL, node_execution_id TEXT NOT NULL, round_ordinal INTEGER NOT NULL, actor TEXT, actor_attribution_kind TEXT NOT NULL, answer_bytes BLOB NOT NULL, answer_hash TEXT NOT NULL, answer_workflow_id TEXT NOT NULL, state TEXT NOT NULL, state_version INTEGER NOT NULL, PRIMARY KEY (node_execution_id), FOREIGN KEY(run_id, revision_hash) REFERENCES runs (run_id, revision_hash), CHECK (length(node_id) > 0), CHECK (round_ordinal >= 1), CHECK ((actor_attribution_kind = 'RECORDED' AND actor IN ('operator')) OR (actor_attribution_kind = 'LEGACY_UNATTRIBUTED' AND actor IS NULL)), CHECK (length(node_execution_id) = 64 AND node_execution_id NOT GLOB '*[^0-9a-f]*'), CHECK (length(answer_hash) = 64 AND answer_hash NOT GLOB '*[^0-9a-f]*'), CHECK (length(answer_workflow_id) > 0), CHECK (state IN ('PENDING', 'APPLIED')), CHECK (state_version IN (0, 1)), CHECK ((state = 'PENDING' AND state_version = 0) OR (state = 'APPLIED' AND state_version = 1)), UNIQUE (answer_workflow_id)
)
""",
}

_WAIT_ANSWER_TRIGGERS_V34_TO_V45: Mapping[str, str] = MappingProxyType(
    {
        "wait_answers_payload_no_update": """
        CREATE TRIGGER wait_answers_payload_no_update
        BEFORE UPDATE OF run_id, revision_hash, node_id, node_execution_id,
                         round_ordinal, answer_bytes, answer_hash,
                         answer_workflow_id
        ON wait_answers BEGIN
          SELECT RAISE(ABORT, 'wait answer bindings are immutable');
        END
    """,
        "wait_answers_state_transition": """
        CREATE TRIGGER wait_answers_state_transition
        BEFORE UPDATE OF state, state_version ON wait_answers
        WHEN NOT (OLD.state = 'PENDING' AND OLD.state_version = 0
                  AND NEW.state = 'APPLIED' AND NEW.state_version = 1)
        BEGIN
          SELECT RAISE(ABORT, 'invalid wait answer transition');
        END
    """,
        "wait_answers_no_delete": """
        CREATE TRIGGER wait_answers_no_delete
        BEFORE DELETE ON wait_answers BEGIN
          SELECT RAISE(ABORT, 'wait answers are immutable');
        END
    """,
    }
)

PUBLISHED_WAIT_ANSWER_TRIGGERS: Mapping[int, Mapping[str, str]] = dict.fromkeys(
    range(34, 46), _WAIT_ANSWER_TRIGGERS_V34_TO_V45
)

_RUN_EVENTS_INDEXES_BEFORE_THE_REPEATABLE_PAUSE = (
    (
        "CREATE UNIQUE INDEX run_events_attempt_kind_unique ON run_events "
        "(agent_attempt_id, event_kind) WHERE agent_attempt_id IS NOT NULL"
    ),
    (
        "CREATE UNIQUE INDEX run_events_legacy_execution_kind_unique ON run_events "
        "(node_execution_id, event_kind) WHERE agent_attempt_id IS NULL"
    ),
    (
        "CREATE UNIQUE INDEX run_events_legacy_kind_unique ON run_events "
        "(run_id, revision_hash, node_id, event_kind) WHERE agent_attempt_id IS NULL"
    ),
)
"""The three run-event keys every schema up to V35 published.

V36 re-scopes the last of them to the round, so a rebuild that materialises one
of those versions has to be given the set it published rather than the set the
declaration carries now.
"""

PUBLISHED_TABLE_INDEXES: Mapping[tuple[int, str], tuple[str, ...]] = {
    (16, "run_events"): _RUN_EVENTS_INDEXES_BEFORE_THE_REPEATABLE_PAUSE,
    (20, "run_events"): _RUN_EVENTS_INDEXES_BEFORE_THE_REPEATABLE_PAUSE,
    (34, "run_events"): _RUN_EVENTS_INDEXES_BEFORE_THE_REPEATABLE_PAUSE,
    (35, "run_events"): _RUN_EVENTS_INDEXES_BEFORE_THE_REPEATABLE_PAUSE,
}

PUBLISHED_QUEUE_ITEMS_STATE_TRANSITION_TRIGGER_BEFORE_OBSERVATION = """
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
        ) BEGIN
          SELECT RAISE(ABORT, 'invalid queue item transition');
        END
    """
"""The queue item transition trigger V44 through V47 published.

V48 adds a third branch letting an observation-only update pass without a
state transition (ADR 0016, 2026-09-01 amendment). `_apply_v43_to_v44` installs
this exact predecessor text rather than today's declaration, or the V44
fingerprint taken while migrating up from V43 would disagree with the one V44
actually published.
"""
