"""Every name this adapter hands to DBOS: workflows, the queue, and steps.

These are persisted tokens, not labels. A workflow name is written into
`workflow_status` and is what a recovering executor matches a queued row
against; the queue name is what a running executor polls. They therefore have
the same durability as an id, and one module is where they belong.

Before this owner existed the names lived beside the workflow bodies that used
them, so a store that needed only a name had to import the module that defines
the whole run — and did it from inside a function to escape the import cycle
that created. A leaf every module may import removes both.

Step names are documentation and nothing more. DBOS replays a recorded step
output by its ordinal within the workflow (`operation_outputs` is keyed by
`(workflow_uuid, function_id)`), so renaming a step neither invalidates a
recorded row nor migrates one. The trailing digits are the hand-kept reminder of
that ordinal; adding, removing or reordering a step inside a landed workflow is
what actually breaks replay.
"""

from __future__ import annotations

WORKFLOW_NAME = "atelier2_durable_run"
NODE_WORKFLOW_NAME = "atelier2_graph_node"
EFFECT_WORKFLOW_NAME = "atelier2_effect"
RECONCILE_WORKFLOW_NAME = "atelier2_reconcile_effect"
ANSWER_WORKFLOW_NAME = "atelier2_wait_answer"
SUBWORKFLOW_WORKFLOW_NAME = "atelier2_add_subworkflow"
ACTION_CONTINUATION_WORKFLOW_NAME = "atelier2_action_continuation"
CANCELLATION_WORKFLOW_NAME = "atelier2_agent_attempt_cancellation"
REPLACEMENT_WORKFLOW_NAME = "atelier2_agent_attempt_replacement"

QUEUE_NAME = "atelier2-durable-runs"

BOOTSTRAP_STEP_NAME = "bootstrap-run-binding"
NODE_BINDING_STEP_NAME = "node-binding/0"
ACTION_PREPARE_STEP_NAME = "action-prepare/1"
ACTION_CHECKPOINT_STEP_NAME = "action-checkpoint/0"
WAIT_COMMIT_STEP_NAME = "wait-commit/1"
SUBWORKFLOW_COMMIT_STEP_NAME = "subworkflow-commit/1"
ANSWER_COMMIT_STEP_NAME = "answer-commit/1"
OBSERVE_STEP_NAME = "observe/0"
RESOLVE_STEP_NAME = "resolve/1"
COMMIT_STEP_NAME = "commit/2"
AGENT_EFFECT_PREPARE_STEP_NAME = "agent-effect-prepare/1"
AGENT_EFFECT_REDEEM_STEP_NAME = "agent-effect-redeem/2"
WORK_ITEM_CLAIM_PREPARE_STEP_NAME = "work-item-claim-prepare/0"
WORK_ITEM_CLAIM_INTENT_STEP_NAME = "work-item-claim-intent/1"
WORK_ITEM_CLAIM_CONFIRM_STEP_NAME = "work-item-claim-confirm/2"
WORK_ITEM_CLAIM_REFUSE_STEP_NAME = "work-item-claim-refuse/3"
