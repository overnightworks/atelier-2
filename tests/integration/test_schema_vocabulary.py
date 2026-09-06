from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from contextlib import closing
from enum import IntEnum, StrEnum

import pytest
import sqlalchemy as sa
from sqlglot import Expr, exp, parse_one

from atelier2.adapters.dbos import schema
from atelier2.contracts.adapter_operations_v3 import AdapterOperationName
from atelier2.contracts.agent_attempts import (
    AGENT_ATTEMPT_ORDINAL,
    REPLACEMENT_AGENT_ATTEMPT_ORDINAL,
    AgentAttemptCancellationDisposition,
    AgentAttemptFailureCode,
    AgentAttemptProcessPhase,
    AgentAttemptRedriveState,
    AgentAttemptReplacement,
    AgentAttemptState,
    RunnerEvidenceAcceptancePhase,
)
from atelier2.contracts.agent_permissions import (
    PermissionAuthority,
    PermissionEffect,
    PermissionScopeKind,
)
from atelier2.contracts.agents import (
    MAXIMUM_AGENT_FIELD_CHARACTERS,
    MAXIMUM_AGENT_OUTPUT_BYTES_V2,
    AgentConfigurationRevisionFormatVersion,
    AgentExecutionCapability,
    AuthMode,
    ProviderId,
)
from atelier2.contracts.artifacts import MAXIMUM_ARTIFACT_BYTES
from atelier2.contracts.catalog_intakes import CatalogIntakeKind
from atelier2.contracts.catalog_v3 import (
    MAXIMUM_LINEAGE_DISPLAY_NAME_CHARACTERS,
    CatalogRetirementState,
)
from atelier2.contracts.definition_sources import (
    MAXIMUM_DEFINITION_SOURCE_ACTOR_CHARACTERS,
    MAXIMUM_REPOSITORY_LOCATION_CHARACTERS,
    MAXIMUM_REPOSITORY_PATH_CHARACTERS,
    MAXIMUM_REPOSITORY_REF_CHARACTERS,
    MAXIMUM_SELECTION_PATTERN_CHARACTERS,
    DefinitionSourceAccess,
    DefinitionSourceKind,
)
from atelier2.contracts.effects import (
    ConfirmationSource,
    EffectIntentState,
    EffectOutcome,
    ReconcileCommandState,
)
from atelier2.contracts.executions import (
    RunEventKind,
    WaitAnswerActor,
    WaitAnswerAttributionKind,
    WaitAnswerState,
)
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.host_configuration import (
    MAXIMUM_CONNECTION_ACTOR_CHARACTERS,
    MAXIMUM_CREDENTIAL_DIRECTORY_CHARACTERS,
    MAXIMUM_EXACT_MODEL_ID_CHARACTERS,
    MAXIMUM_PROJECT_ID_CHARACTERS,
    MAXIMUM_PROJECT_ROOT_PATH_CHARACTERS,
    MAXIMUM_SOURCE_ADDRESS_CHARACTERS,
    MAXIMUM_SOURCE_KIND_CHARACTERS,
    MAXIMUM_SOURCE_REFERENCE_CHARACTERS,
    ModelRegistryEntrySource,
    ProjectSourceConnectionLifecycle,
    ProviderModelCheck,
    SourceConnectionAuthMethod,
)
from atelier2.contracts.node_records_v3 import (
    MAXIMUM_KIND_TOKEN_CHARACTERS,
    PersistedReceiptDisposition,
)
from atelier2.contracts.queue_projection import (
    MAXIMUM_QUEUE_ADMISSION_RATIONALE_CHARACTERS,
    MAXIMUM_QUEUE_AUTOMATION_LABEL_CHARACTERS,
    MAXIMUM_QUEUE_ITEM_TITLE_CHARACTERS,
    MAXIMUM_TRACKER_ITEM_REFERENCE_CHARACTERS,
    QueueAutomationDisposition,
    QueueDecisionAuthority,
    QueueItemState,
    QueueProjectionRevision,
    QueueProposalSource,
)
from atelier2.contracts.revisions_v3 import RevisionKind
from atelier2.contracts.runs import UNSUCCESSFUL_TERMINAL_RUN_STATES, RunState
from atelier2.contracts.tool_grants_v3 import ToolGrantCapability
from atelier2.contracts.workflow_formats import WorkflowFormatVersion

_LENGTH_BOUND = re.compile(
    r"length\(([a-z_0-9]+)\)\s*(?:BETWEEN\s+\d+\s+AND\s+(\d+)|<=\s*(\d+)|=\s*(\d+))"
)
_HEXADECIMAL_DIGITS = "NOT GLOB '*[^0-9a-f]*'"

Conditions = tuple[tuple[str, str], ...]


class UnsupportedVocabularyPredicate(ValueError):
    """A CHECK names an owned vocabulary through grammar the guard cannot prove."""


def _parse_condition(condition: str) -> Expr:
    return parse_one(condition, read="sqlite")


def _without_parentheses(expression: Expr) -> Expr:
    while isinstance(expression, exp.Paren):
        expression = expression.this
    return expression


def _literal_value(expression: Expr) -> str | int:
    expression = _without_parentheses(expression)
    if not isinstance(expression, exp.Literal):
        raise TypeError(f"not a literal: {expression.sql(dialect='sqlite')}")
    if not expression.is_string and not expression.is_int:
        raise TypeError(f"not an integer literal: {expression.sql(dialect='sqlite')}")
    return expression.this if expression.is_string else int(expression.this)


def _canonical_declaration(
    expression: Expr,
) -> tuple[str, frozenset[str | int]] | None:
    expression = _without_parentheses(expression)
    if not isinstance(expression, exp.In) or not isinstance(
        expression.this, exp.Column
    ):
        return None
    try:
        members = frozenset(_literal_value(member) for member in expression.expressions)
    except TypeError:
        return None
    return expression.this.name, members


def _check_conditions() -> Conditions:
    return tuple(
        (table.name, " ".join(str(constraint.sqltext).split()))
        for table in schema.metadata.tables.values()
        for constraint in table.constraints
        if isinstance(constraint, sa.CheckConstraint)
    )


def _declared_vocabularies(
    conditions: Conditions,
) -> Mapping[str, frozenset[str | int]]:
    """The closed set each column declares, read only from its own membership CHECK.

    A CHECK whose entire condition is `column IN (...)` is the schema saying what
    that column may ever hold. Every other CHECK that names the column branches
    on a part of that set, so reading those too would let a narrowed declaration
    stay green on the strength of a state-shape mention elsewhere.

    A table may spell more than one such CHECK, and SQLite requires every one of
    them, so what the database admits is their intersection, never their union.
    """

    declared: dict[str, frozenset[str | int]] = {}
    for table_name, condition in conditions:
        declaration = _canonical_declaration(_parse_condition(condition))
        if declaration is None:
            continue
        column, admitted = declaration
        qualified = f"{table_name}.{column}"
        still_admitted = declared.get(qualified)
        declared[qualified] = (
            admitted if still_admitted is None else still_admitted & admitted
        )
    return declared


def _compared_literals(conditions: Conditions) -> Mapping[str, frozenset[str | int]]:
    """Every literal a state-shape CHECK compares a column against."""

    compared: dict[str, set[str | int]] = {}
    for table_name, condition in conditions:
        expression = _parse_condition(condition)
        if _canonical_declaration(expression) is not None:
            continue
        for comparison in expression.walk():
            if isinstance(comparison, exp.In) and isinstance(
                comparison.this, exp.Column
            ):
                try:
                    literals = {
                        _literal_value(member) for member in comparison.expressions
                    }
                except TypeError:
                    continue
                compared.setdefault(
                    f"{table_name}.{comparison.this.name}", set()
                ).update(literals)
            elif isinstance(comparison, (exp.EQ, exp.NEQ)):
                left = _without_parentheses(comparison.this)
                right = _without_parentheses(comparison.expression)
                column_and_literal = (
                    (left, right)
                    if isinstance(left, exp.Column) and isinstance(right, exp.Literal)
                    else (right, left)
                )
                column, literal = column_and_literal
                if isinstance(column, exp.Column) and isinstance(literal, exp.Literal):
                    compared.setdefault(f"{table_name}.{column.name}", set()).add(
                        _literal_value(literal)
                    )
    return {column: frozenset(literals) for column, literals in compared.items()}


def _mentions_column(expression: Expr, column: str) -> bool:
    return any(
        isinstance(candidate, exp.Column) and candidate.name == column
        for candidate in expression.walk()
    )


def _referenced_columns(expression: Expr) -> frozenset[str]:
    return frozenset(
        candidate.name
        for candidate in expression.walk()
        if isinstance(candidate, exp.Column)
    )


def _target_comparison(expression: exp.Binary, column: str) -> str | int | None:
    left = _without_parentheses(expression.this)
    right = _without_parentheses(expression.expression)
    if isinstance(left, exp.Column) and left.name == column:
        try:
            return _literal_value(right)
        except TypeError:
            return None
    if isinstance(right, exp.Column) and right.name == column:
        try:
            return _literal_value(left)
        except TypeError:
            return None
    return None


def _target_only_between_values(
    expression: exp.Between,
    column: str,
    owned: frozenset[str | int],
) -> frozenset[str | int]:
    if _referenced_columns(expression) != {column}:
        raise UnsupportedVocabularyPredicate(expression.sql(dialect="sqlite"))
    sql = expression.sql(dialect="sqlite")
    quoted_column = column.replace('"', '""')
    admitted: set[str | int] = set()
    with closing(sqlite3.connect(":memory:")) as database:
        for member in owned:
            result = database.execute(
                f'SELECT ({sql}) FROM (SELECT ? AS "{quoted_column}")',
                (member,),
            ).fetchone()
            if result is not None and result[0] != 0:
                admitted.add(member)
    return frozenset(admitted)


def _possible_owned_values(
    expression: Expr,
    column: str,
    owned: frozenset[str | int],
) -> frozenset[str | int]:
    expression = _without_parentheses(expression)
    if not _mentions_column(expression, column):
        return owned
    if isinstance(expression, exp.And):
        return _possible_owned_values(expression.this, column, owned) & (
            _possible_owned_values(expression.expression, column, owned)
        )
    if isinstance(expression, exp.Or):
        return _possible_owned_values(expression.this, column, owned) | (
            _possible_owned_values(expression.expression, column, owned)
        )
    if (
        isinstance(expression, exp.In)
        and isinstance(expression.this, exp.Column)
        and expression.this.name == column
    ):
        try:
            return owned & frozenset(
                _literal_value(member) for member in expression.expressions
            )
        except TypeError as error:
            raise UnsupportedVocabularyPredicate(
                expression.sql(dialect="sqlite")
            ) from error
    if isinstance(expression, (exp.EQ, exp.NEQ)):
        comparison = _target_comparison(expression, column)
        if comparison is not None:
            matched = owned & {comparison}
            return matched if isinstance(expression, exp.EQ) else owned - matched
    if isinstance(expression, exp.Is):
        target = _without_parentheses(expression.this)
        if (
            isinstance(target, exp.Column)
            and target.name == column
            and isinstance(expression.expression, exp.Null)
        ):
            return frozenset()
    if isinstance(expression, exp.Not):
        negated = _without_parentheses(expression.this)
        if isinstance(negated, exp.In):
            return owned - _possible_owned_values(negated, column, owned)
        if isinstance(negated, exp.Is):
            target = _without_parentheses(negated.this)
            if (
                isinstance(target, exp.Column)
                and target.name == column
                and isinstance(negated.expression, exp.Null)
            ):
                return owned
    if isinstance(expression, exp.Between):
        return _target_only_between_values(expression, column, owned)
    raise UnsupportedVocabularyPredicate(
        f"{column}: {expression.sql(dialect='sqlite')}"
    )


def _admitted_vocabularies(
    conditions: Conditions,
) -> Mapping[str, frozenset[str | int]]:
    admitted = dict(OWNED_VOCABULARIES)
    for table_name, condition in conditions:
        expression = _parse_condition(condition)
        for qualified, owned in OWNED_VOCABULARIES.items():
            owner_table, column = qualified.split(".", maxsplit=1)
            if owner_table == table_name and _mentions_column(expression, column):
                admitted[qualified] &= _possible_owned_values(expression, column, owned)
    return admitted


def _conditions_with_declaration_rewritten(
    column: str, original: str, replacement: str
) -> Conditions:
    """The schema's conditions, with one column's own membership CHECK edited."""

    rewritten: list[tuple[str, str]] = []
    for table_name, condition in SCHEMA_CONDITIONS:
        declared = _canonical_declaration(_parse_condition(condition))
        declares_column = (
            declared is not None and f"{table_name}.{declared[0]}" == column
        )
        rewritten.append(
            (
                table_name,
                condition.replace(original, replacement)
                if declares_column
                else condition,
            )
        )
    return tuple(rewritten)


def _schema_length_bounds() -> tuple[Mapping[str, int], Mapping[str, int]]:
    """The upper length bound of every constrained column, hash columns apart."""
    hashes: dict[str, int] = {}
    fields: dict[str, int] = {}
    for table_name, condition in SCHEMA_CONDITIONS:
        for column, between, at_most, exactly in _LENGTH_BOUND.findall(condition):
            bound = int(between or at_most or exactly)
            hexadecimal = f"{column} {_HEXADECIMAL_DIGITS}" in condition
            target = hashes if hexadecimal else fields
            target[f"{table_name}.{column}"] = bound
    return hashes, fields


def _values(vocabulary: Iterable[StrEnum | IntEnum]) -> frozenset[str | int]:
    return frozenset(member.value for member in vocabulary)


SCHEMA_CONDITIONS = _check_conditions()
HASH_LENGTH_BOUNDS, FIELD_LENGTH_BOUNDS = _schema_length_bounds()
PROVIDER_ID_BOUND = FIELD_LENGTH_BOUNDS["auth_profile_revisions.provider_id"]

OWNED_ATTEMPT_ORDINALS: frozenset[str | int] = frozenset(
    {AGENT_ATTEMPT_ORDINAL, REPLACEMENT_AGENT_ATTEMPT_ORDINAL}
)
"""The only ordinals `AgentAttemptId.for_execution` mints an attempt for."""

OWNED_VOCABULARIES: Mapping[str, frozenset[str | int]] = {
    "agent_attempts.attempt_ordinal": OWNED_ATTEMPT_ORDINALS,
    "agent_attempts.cancellation_disposition": _values(
        AgentAttemptCancellationDisposition
    ),
    "agent_attempts.failure_code": _values(AgentAttemptFailureCode),
    "agent_attempts.process_phase": _values(AgentAttemptProcessPhase),
    "agent_attempts.runner_evidence_acceptance_phase": _values(
        RunnerEvidenceAcceptancePhase
    ),
    "agent_attempts.redrive_state": _values(AgentAttemptRedriveState),
    "agent_attempts.replacement": _values(AgentAttemptReplacement),
    "agent_attempts.state": _values(AgentAttemptState),
    "agent_configuration_revisions.requested_capability": _values(
        AgentExecutionCapability
    ),
    "agent_configuration_revisions.revision_format_version": _values(
        AgentConfigurationRevisionFormatVersion
    ),
    "agent_receipts_v2.auth_mode": _values(AuthMode),
    "permission_receipts.effect": _values(PermissionEffect),
    "permission_receipts.scope_kind": _values(PermissionScopeKind),
    "permission_receipts.authority": _values(PermissionAuthority),
    # An answer is one of two, and the row spells it the way SQLite spells a
    # boolean; what the two stand for is the receipt's own `granted`.
    "permission_receipts.granted": frozenset({0, 1}),
    "auth_profile_revisions.auth_mode": _values(AuthMode),
    "effect_intents.state": _values(EffectIntentState),
    "effect_intents.operation_name": _values(AdapterOperationName),
    "effect_receipts.confirmation_source": _values(ConfirmationSource),
    "effect_receipts.operation_name": _values(AdapterOperationName),
    # An operator determination is a decision, and UNKNOWN is what a readback
    # reports when no source can decide yet, so no command may ever persist it.
    "reconcile_commands.determination": _values(EffectOutcome)
    - {EffectOutcome.UNKNOWN.value},
    "reconcile_commands.state": _values(ReconcileCommandState),
    "run_events.attempt_ordinal": OWNED_ATTEMPT_ORDINALS,
    "run_events.event_kind": _values(RunEventKind),
    "run_events.replacement": _values(AgentAttemptReplacement),
    "run_events.wait_answer_actor": _values(WaitAnswerActor),
    "runs.state": _values(RunState),
    # The ending a released launch binding names is the run contract's own
    # word for a run that ended without an answer, never a list of its own.
    "queue_launch_bindings.ended_run_state": _values(UNSUCCESSFUL_TERMINAL_RUN_STATES),
    "runs.workflow_format_version": _values(WorkflowFormatVersion),
    "wait_answers.state": _values(WaitAnswerState),
    "wait_answers.actor": _values(WaitAnswerActor),
    "wait_answers.actor_attribution_kind": _values(WaitAnswerAttributionKind),
    "node_receipts_v3.disposition": _values(PersistedReceiptDisposition),
    "published_revisions.kind": _values(RevisionKind),
    # A tool redemption is the exec-shaped record -- a command, an exit code, a
    # hash of what it said -- so only an exec-shaped capability ever lands here.
    # Both external effects answer with an EffectReceipt, so neither belongs to
    # this exec-shaped column.
    "tool_redemptions.capability": _values(ToolGrantCapability)
    - {
        ToolGrantCapability.OPEN_PR.value,
        ToolGrantCapability.PUSH_ATELIER_COMMIT.value,
    },
    "catalog_lineages.kind": _values(RevisionKind),
    "catalog_intakes.kind": _values(CatalogIntakeKind),
    "catalog_lineage_retirements.state": _values(CatalogRetirementState),
    "queue_items.state": _values(QueueItemState),
    "queue_items.decision_authority": _values(QueueDecisionAuthority),
    "queue_proposal_revisions.automation_disposition": _values(
        QueueAutomationDisposition
    ),
    "queue_proposal_revisions.source": _values(QueueProposalSource),
    "queue_project_policy_revisions.automation_disposition_default": _values(
        QueueAutomationDisposition
    ),
    "host_project_source_connection_revisions.auth_method": _values(
        SourceConnectionAuthMethod
    ),
    "host_project_source_connection_revisions.lifecycle": _values(
        ProjectSourceConnectionLifecycle
    ),
    "host_definition_source_revisions.source_kind": _values(DefinitionSourceKind),
    "host_definition_source_revisions.access": _values(DefinitionSourceAccess),
    "host_definition_source_selections.revision_kind": _values(RevisionKind),
    "catalog_source_intakes.revision_kind": _values(RevisionKind),
    "host_model_registry_entries.source": _values(ModelRegistryEntrySource),
    "host_model_registry_entries.provider_check": _values(ProviderModelCheck),
    "host_project_model_defaults.difficulty": frozenset({1, 2, 3}),
}

UNDECLARED_VOCABULARIES: frozenset[str] = frozenset(
    {
        "agent_attempts.cancellation_disposition",
        "agent_attempts.failure_code",
        "agent_attempts.redrive_state",
        "agent_attempts.replacement",
        "agent_attempts.runner_evidence_acceptance_phase",
        "agent_attempts.state",
        "queue_items.decision_authority",
        "run_events.attempt_ordinal",
        "run_events.replacement",
        "run_events.wait_answer_actor",
        "wait_answers.actor",
        "wait_answers.actor_attribution_kind",
    }
)
"""Owned columns the schema constrains only inside a state-shape CHECK.

For these the schema never spells the closed set in one place, so the suite can
prove that no CHECK compares them against a value their contract does not own,
but not that the schema still admits every value it does. Naming them keeps that
gap visible and makes a column that loses its own membership CHECK a red test.
"""

UNOWNED_VOCABULARIES: Mapping[str, str] = {
    "agent_attempts.state_version": (
        "a monotonic state-machine counter, not a closed vocabulary"
    ),
    "wait_answers.state_version": (
        "a monotonic state-machine counter, not a closed vocabulary"
    ),
    "webhook_delivery_cursor.cursor_id": (
        "the fixed identity of the one durable delivery-cursor row, not a "
        "closed vocabulary of interchangeable values"
    ),
    "tool_redemptions.exit_code": (
        "the one exit a stored redemption can record, because a redemption is "
        "the record of a check that passed; a nonzero exit is the verdict that "
        "ends the attempt instead, not another member of a vocabulary"
    ),
}

OWNED_SCALAR_DOMAINS: Mapping[str, Callable[[int], object]] = {
    "queue_items.state_version": QueueProjectionRevision,
}
"""Non-vocabulary CHECK operands validated by their production value contract."""

DECLARED_VOCABULARIES = _declared_vocabularies(SCHEMA_CONDITIONS)
COMPARED_LITERALS = _compared_literals(SCHEMA_CONDITIONS)
ADMITTED_VOCABULARIES = _admitted_vocabularies(SCHEMA_CONDITIONS)

OWNED_HASH_COLUMNS: frozenset[str] = frozenset(
    {
        "agent_attempts.attempt_id",
        "attempt_instants.attempt_id",
        "agent_attempts.node_execution_id",
        "agent_attempts.request_hash",
        "agent_attempts.runner_manifest_id",
        "agent_attempts.runner_terminal_evidence_hash",
        "agent_attempts.transcript_artifact_hash",
        "agent_attempts.workflow_revision_hash",
        "permission_receipts.attempt_id",
        "permission_receipts.correlation_id",
        "permission_receipts.policy_revision_hash",
        "permission_receipts.receipt_hash",
        "agent_attempt_receipts_v3.artifact_hash",
        "agent_attempt_receipts_v3.receipt_hash",
        "agent_attempt_receipts_v3.schema_revision_hash",
        "agent_attempt_receipts_v3.value_hash",
        "agent_configuration_revisions.auth_profile_revision_hash",
        "agent_configuration_revisions.revision_hash",
        "agent_receipts.node_execution_id",
        "agent_receipts.output_hash",
        "agent_receipts.receipt_hash",
        "agent_receipts.request_hash",
        "agent_receipts.workflow_revision_hash",
        "agent_receipts_v2.agent_configuration_revision_hash",
        "agent_receipts_v2.auth_profile_revision_hash",
        "agent_receipts_v2.binding_set_hash",
        "agent_receipts_v2.node_execution_id",
        "agent_receipts_v2.output_hash",
        "agent_receipts_v2.receipt_hash",
        "agent_receipts_v2.request_hash",
        "tool_redemptions.attempt_id",
        "tool_redemptions.node_execution_id",
        "tool_redemptions.receipt_hash",
        "tool_redemptions.standard_output_hash",
        "tool_redemptions.tool_revision_hash",
        "tool_redemptions.workflow_revision_hash",
        "agent_receipts_v2.workflow_revision_hash",
        "artifacts.artifact_hash",
        "auth_profile_revisions.revision_hash",
        "host_project_root_revisions.revision_hash",
        "host_project_source_connection_revisions.revision_hash",
        "host_model_registry_revisions.revision_hash",
        "host_model_registry_entries.revision_hash",
        "host_model_registry_entries.agent_configuration_revision_hash",
        "host_project_model_defaults_revisions.revision_hash",
        "host_project_model_defaults.revision_hash",
        "host_project_model_defaults.model_registry_revision_hash",
        "host_project_model_defaults.agent_configuration_revision_hash",
        "context_packages_v3.package_hash",
        "node_execution_requests_v3.context_package_hash",
        "node_execution_requests_v3.node_execution_id",
        "node_execution_requests_v3.request_hash",
        "effect_intents.request_hash",
        "effect_intents.workflow_revision_hash",
        "effect_receipts.request_hash",
        "effect_receipts.result_hash",
        "effect_receipts.workflow_revision_hash",
        "run_fork_effect_fences.source_result_hash",
        "run_fork_effect_fences.source_workflow_revision_hash",
        "run_fork_reused_nodes.source_agent_receipt_hash",
        "run_fork_reused_nodes.source_declared_context_package_hash",
        "run_fork_reused_nodes.source_event_hash",
        "run_fork_reused_nodes.source_node_execution_id",
        "run_fork_reused_nodes.source_receipt_hash",
        "run_fork_reused_nodes.source_workflow_revision_hash",
        "run_forks.command_id",
        "run_forks.fork_hash",
        "run_forks.origin_terminal_hash",
        "run_forks.run_configuration_revision_hash",
        "run_forks.workflow_revision_hash",
        "reconcile_commands.found_result_hash",
        "run_agent_bindings.agent_configuration_revision_hash",
        "run_configuration_revisions.revision_hash",
        "run_inputs_v3.schema_revision_hash",
        "run_inputs_v3.value_hash",
        "run_agent_bindings.binding_set_hash",
        "run_agent_bindings.revision_hash",
        "run_events.agent_attempt_id",
        "run_events.agent_receipt_hash",
        "run_events.event_hash",
        "run_events.node_execution_id",
        "run_events.payload_hash",
        "run_events.receipt_result_hash",
        "runs.agent_binding_set_hash",
        "runs.run_configuration_revision_hash",
        "runs.terminal_hash",
        "wait_answers.answer_hash",
        "wait_answers.node_execution_id",
        "workflow_revisions.revision_hash",
        "published_revisions.revision_hash",
        "catalog_lineages.lineage_id",
        "catalog_lineages.founding_revision_hash",
        "catalog_intakes.intake_id",
        "catalog_lineage_members.revision_hash",
        "node_artifacts_v3.artifact_hash",
        "node_artifacts_v3.node_execution_id",
        "node_artifacts_v3.schema_revision_hash",
        "node_artifacts_v3.value_hash",
        "node_receipt_outputs_v3.node_execution_id",
        "node_receipt_outputs_v3.schema_revision_hash",
        "node_receipt_outputs_v3.value_hash",
        "node_receipts_v3.node_execution_id",
        "node_receipts_v3.request_hash",
        "node_receipts_v3.context_package_hash",
        "node_receipts_v3.receipt_hash",
        "queue_items.item_id",
        "queue_items.workflow_lineage_id",
        "queue_launch_bindings.workflow_revision_hash",
        "queue_project_policy_revisions.default_workflow_lineage_id",
        "host_definition_source_revisions.revision_hash",
        "host_definition_source_revisions.source_id",
        "host_definition_source_selections.revision_hash",
        "catalog_source_intakes.revision_hash",
        "catalog_source_intakes.source_id",
        # A git object name is a digest too, and both formats git writes fit
        # the same upper bound as an Atelier hash.
        "catalog_source_intakes.source_commit",
    }
)

OWNED_FIELD_BOUNDS: Mapping[str, int] = {
    "agent_attempts.cancellation_command_id": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "agent_attempts.executor_operational_identity": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "agent_attempts.node_id": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "agent_attempts.process_owner_id": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "agent_attempts.runner_generation_id": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "agent_attempts.runner_invocation_id": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "agent_attempts.watchdog_generation_id": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "agent_configuration_revisions.executor_revision": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "agent_configuration_revisions.model": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "agent_receipts_v2.executor_operational_identity": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "agent_receipts_v2.executor_revision": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "agent_receipts_v2.model": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "agent_receipts_v2.node_id": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "agent_receipts_v2.output_bytes": MAXIMUM_AGENT_OUTPUT_BYTES_V2,
    "agent_receipts_v2.profile_id": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "agent_receipts_v2.provider_id": PROVIDER_ID_BOUND,
    "agent_receipts_v2.role": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "artifacts.content": MAXIMUM_ARTIFACT_BYTES,
    "auth_profile_revisions.profile_id": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "auth_profile_revisions.provider_id": PROVIDER_ID_BOUND,
    "host_project_root_revisions.project_id": MAXIMUM_PROJECT_ID_CHARACTERS,
    "host_project_root_revisions.root_path": MAXIMUM_PROJECT_ROOT_PATH_CHARACTERS,
    "host_project_source_connection_revisions.project_id": (
        MAXIMUM_PROJECT_ID_CHARACTERS
    ),
    "host_project_source_connection_revisions.source_kind": (
        MAXIMUM_SOURCE_KIND_CHARACTERS
    ),
    "host_project_source_connection_revisions.source_address": (
        MAXIMUM_SOURCE_ADDRESS_CHARACTERS
    ),
    "host_project_source_connection_revisions.source_ref": (
        MAXIMUM_SOURCE_REFERENCE_CHARACTERS
    ),
    "host_project_source_connection_revisions.credential_directory": (
        MAXIMUM_CREDENTIAL_DIRECTORY_CHARACTERS
    ),
    "host_project_source_connection_revisions.connected_by": (
        MAXIMUM_CONNECTION_ACTOR_CHARACTERS
    ),
    "host_project_source_connection_revisions.source_id": 36,
    "host_project_source_connection_revisions.connected_at": 20,
    "host_model_registry_revisions.provider_id": PROVIDER_ID_BOUND,
    "host_model_registry_entries.provider_id": PROVIDER_ID_BOUND,
    "host_model_registry_entries.model_id": MAXIMUM_EXACT_MODEL_ID_CHARACTERS,
    "host_project_model_defaults_revisions.project_id": MAXIMUM_PROJECT_ID_CHARACTERS,
    "host_project_model_defaults.provider_id": PROVIDER_ID_BOUND,
    "host_project_model_defaults.model_id": MAXIMUM_EXACT_MODEL_ID_CHARACTERS,
    "run_agent_bindings.role": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "tool_redemptions.node_id": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "permission_receipts.scope_value": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "permission_receipts.decided_at": 20,
    "run_events.cancellation_command_id": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "catalog_lineages.kind": MAXIMUM_KIND_TOKEN_CHARACTERS,
    "published_revisions.kind": MAXIMUM_KIND_TOKEN_CHARACTERS,
    "catalog_lineage_aliases.name": MAXIMUM_LINEAGE_DISPLAY_NAME_CHARACTERS,
    "run_instants.started_at": 20,
    "run_instants.ended_at": 20,
    "attempt_instants.started_at": 20,
    "attempt_instants.ended_at": 20,
    "event_instants.recorded_at": 20,
    "queue_items.project_id": MAXIMUM_PROJECT_ID_CHARACTERS,
    "queue_items.tracker_item_reference": MAXIMUM_TRACKER_ITEM_REFERENCE_CHARACTERS,
    "queue_items.admission_rationale": MAXIMUM_QUEUE_ADMISSION_RATIONALE_CHARACTERS,
    "queue_items.observed_title": MAXIMUM_QUEUE_ITEM_TITLE_CHARACTERS,
    "queue_items.title_observed_at": 20,
    "queue_items.retired_at": 20,
    "host_definition_source_revisions.repository_location": (
        MAXIMUM_REPOSITORY_LOCATION_CHARACTERS
    ),
    "host_definition_source_revisions.repository_ref": (
        MAXIMUM_REPOSITORY_REF_CHARACTERS
    ),
    "host_definition_source_revisions.connected_by": (
        MAXIMUM_DEFINITION_SOURCE_ACTOR_CHARACTERS
    ),
    "host_definition_source_selections.path_pattern": (
        MAXIMUM_SELECTION_PATTERN_CHARACTERS
    ),
    "catalog_source_intakes.source_path": MAXIMUM_REPOSITORY_PATH_CHARACTERS,
    "catalog_source_intakes.intaken_by": MAXIMUM_DEFINITION_SOURCE_ACTOR_CHARACTERS,
    "catalog_source_intakes.intaken_at": 20,
    "queue_project_policy_revisions.project_id": MAXIMUM_PROJECT_ID_CHARACTERS,
    "queue_project_policy_revisions.automation_label": (
        MAXIMUM_QUEUE_AUTOMATION_LABEL_CHARACTERS
    ),
}


@pytest.mark.parametrize(
    "column", sorted(set(OWNED_VOCABULARIES) - UNDECLARED_VOCABULARIES)
)
def test_a_declared_vocabulary_holds_exactly_what_its_contract_owns(
    column: str,
) -> None:
    assert DECLARED_VOCABULARIES[column] == OWNED_VOCABULARIES[column]


@pytest.mark.parametrize("column", sorted(OWNED_VOCABULARIES))
def test_no_check_compares_a_column_against_a_value_its_contract_refuses(
    column: str,
) -> None:
    assert COMPARED_LITERALS.get(column, frozenset()) <= OWNED_VOCABULARIES[column]


def test_the_columns_named_undeclared_are_exactly_the_ones_without_their_own_check() -> (
    None
):
    assert UNDECLARED_VOCABULARIES == frozenset(OWNED_VOCABULARIES) - set(
        DECLARED_VOCABULARIES
    )


def test_every_vocabulary_the_schema_spells_has_an_owner_or_a_named_exemption() -> None:
    assert set(DECLARED_VOCABULARIES) | set(COMPARED_LITERALS) == set(
        OWNED_VOCABULARIES
    ) | set(OWNED_SCALAR_DOMAINS) | set(UNOWNED_VOCABULARIES)


@pytest.mark.parametrize("column", sorted(OWNED_SCALAR_DOMAINS))
def test_every_compared_scalar_literal_is_admitted_by_its_contract(
    column: str,
) -> None:
    for literal in COMPARED_LITERALS[column]:
        assert type(literal) is int
        OWNED_SCALAR_DOMAINS[column](literal)


def test_a_kind_dropped_from_its_own_check_is_drift_though_another_check_names_it() -> (
    None
):
    narrowed = _conditions_with_declaration_rewritten(
        "run_events.event_kind", "'AGENT_FAILED', ", ""
    )

    assert _declared_vocabularies(narrowed)["run_events.event_kind"] == (
        OWNED_VOCABULARIES["run_events.event_kind"] - {RunEventKind.AGENT_FAILED.value}
    )
    assert (
        RunEventKind.AGENT_FAILED.value
        in _compared_literals(narrowed)["run_events.event_kind"]
    )


def test_a_kind_added_to_its_own_check_without_a_contract_is_drift() -> None:
    widened = _conditions_with_declaration_rewritten(
        "run_events.event_kind",
        "'SUBWORKFLOW_COMPLETED'",
        "'SUBWORKFLOW_COMPLETED', 'AGENT_ABANDONED'",
    )

    assert _declared_vocabularies(widened)["run_events.event_kind"] == (
        OWNED_VOCABULARIES["run_events.event_kind"] | {"AGENT_ABANDONED"}
    )


def test_a_column_under_two_membership_checks_admits_only_their_intersection() -> None:
    """The premise a declaration is read under: SQLite requires every CHECK at once."""

    with closing(sqlite3.connect(":memory:")) as database:
        database.execute(
            "CREATE TABLE two_declarations (kind TEXT NOT NULL, "
            "CHECK (kind IN ('KEPT', 'SHUT_OUT')), CHECK (kind IN ('KEPT')))"
        )

        database.execute("INSERT INTO two_declarations VALUES ('KEPT')")
        with pytest.raises(sqlite3.IntegrityError):
            database.execute("INSERT INTO two_declarations VALUES ('SHUT_OUT')")


def test_a_second_check_narrowing_a_column_is_drift_though_the_first_names_every_kind() -> (
    None
):
    narrowed = (
        *SCHEMA_CONDITIONS,
        ("run_events", f"event_kind IN ('{RunEventKind.AGENT_COMPLETED.value}')"),
    )

    assert _declared_vocabularies(narrowed)["run_events.event_kind"] == {
        RunEventKind.AGENT_COMPLETED.value
    }


@pytest.mark.parametrize(
    ("condition", "admitted"),
    [
        (
            "event_kind IN (('AGENT_COMPLETED'))",
            frozenset({RunEventKind.AGENT_COMPLETED.value}),
        ),
        (
            "event_kind = 'AGENT_COMPLETED'",
            frozenset({RunEventKind.AGENT_COMPLETED.value}),
        ),
        (
            "'AGENT_COMPLETED' = event_kind",
            frozenset({RunEventKind.AGENT_COMPLETED.value}),
        ),
        (
            "event_kind NOT IN ('AGENT_COMPLETED')",
            OWNED_VOCABULARIES["run_events.event_kind"]
            - {RunEventKind.AGENT_COMPLETED.value},
        ),
        (
            "event_kind <> 'AGENT_COMPLETED'",
            OWNED_VOCABULARIES["run_events.event_kind"]
            - {RunEventKind.AGENT_COMPLETED.value},
        ),
        (
            "event_kind != 'AGENT_COMPLETED'",
            OWNED_VOCABULARIES["run_events.event_kind"]
            - {RunEventKind.AGENT_COMPLETED.value},
        ),
    ],
)
def test_every_sqlite_spelling_that_narrows_an_owned_vocabulary_is_drift(
    condition: str, admitted: frozenset[str | int]
) -> None:
    narrowed = (*SCHEMA_CONDITIONS, ("run_events", condition))

    assert _admitted_vocabularies(narrowed)["run_events.event_kind"] == admitted


@pytest.mark.proves("a-schema-check-cannot-silently-narrow-an-owned-vocabulary")
@pytest.mark.parametrize("column", sorted(OWNED_VOCABULARIES))
def test_every_check_preserves_the_complete_vocabulary_its_contract_owns(
    column: str,
) -> None:
    assert ADMITTED_VOCABULARIES[column] == OWNED_VOCABULARIES[column]


def test_exhaustive_boolean_branches_preserve_the_complete_vocabulary() -> None:
    exhaustive = (
        *SCHEMA_CONDITIONS,
        (
            "run_events",
            "event_kind = 'AGENT_COMPLETED' OR event_kind != 'AGENT_COMPLETED'",
        ),
    )

    assert (
        _admitted_vocabularies(exhaustive)["run_events.event_kind"]
        == (OWNED_VOCABULARIES["run_events.event_kind"])
    )


def test_an_unrecognised_owned_vocabulary_predicate_fails_by_name() -> None:
    unrecognised = (*SCHEMA_CONDITIONS, ("run_events", "event_kind LIKE 'AGENT_%'"))

    with pytest.raises(UnsupportedVocabularyPredicate, match="event_kind"):
        _admitted_vocabularies(unrecognised)


def test_every_persisted_hash_column_is_bounded_at_the_length_of_a_real_digest() -> (
    None
):
    assert set(HASH_LENGTH_BOUNDS) == OWNED_HASH_COLUMNS
    assert set(HASH_LENGTH_BOUNDS.values()) == {len(Sha256Hash.of(b"").value)}


def test_every_field_length_bound_is_the_bound_its_contract_owns() -> None:
    assert FIELD_LENGTH_BOUNDS == OWNED_FIELD_BOUNDS


def test_the_persisted_provider_id_bound_is_the_longest_the_contract_accepts() -> None:
    assert ProviderId("a" * PROVIDER_ID_BOUND).value == "a" * PROVIDER_ID_BOUND
    with pytest.raises(ValueError):
        ProviderId("a" * (PROVIDER_ID_BOUND + 1))
