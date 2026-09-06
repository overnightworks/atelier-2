"""The e2e harness's own copy of the conductor conversation-loop document.

No production caller publishes this document, so this module is its only
owner. `/__e2e/seed-conductor` (`serve_cockpit.py`) publishes it to give one
served instance a real "conductor" catalog revision and a role bound to the
harness's fixed-answer executor. It carries exactly what that seeding needs:
the wait and agent node ids, the message schema the wait's answer is checked
against, and the report schema `CONDUCTOR_FAKE_REPORT` (`serve_cockpit.py`)
answers with.
"""

from __future__ import annotations

import json

CONDUCTOR_WORKFLOW_NAME = "conductor"
CONDUCTOR_ROLE = "conductor"

CONDUCTOR_WAIT_NODE_ID = "next_message"
CONDUCTOR_AGENT_NODE_ID = "conduct"
CONDUCTOR_LOOP_ID = "conversation"

# The seeded conversation's own round ceiling: harness scenario data, never a
# product limit to read back out of here.
CONDUCTOR_LOOP_MAXIMUM_ROUNDS = 24

CONDUCTOR_MESSAGE_OUTPUT = "message"
_REPORT_OUTPUT = "report"
_PREVIOUS_REPORT_INPUT = "previous_report"

_REPORT_ANSWER_FIELD = "answer"
_REPORT_STARTED_RUN_IDS_FIELD = "started_run_ids"
_REPORT_CARRIED_CONTEXT_FIELD = "carried_context"
_REPORT_CARRIED_CONTEXT_TRUNCATED_FIELD = "carried_context_truncated"


def _canonical_schema_bytes(schema: dict[str, object]) -> bytes:
    return json.dumps(schema, sort_keys=True, separators=(",", ":")).encode()


CONDUCTOR_MESSAGE_SCHEMA = _canonical_schema_bytes({"type": "string", "minLength": 1})

# The declared shape `CONDUCTOR_FAKE_REPORT` (`serve_cockpit.py`) answers with.
CONDUCTOR_REPORT_SCHEMA = _canonical_schema_bytes(
    {
        "type": "object",
        "required": [
            _REPORT_ANSWER_FIELD,
            _REPORT_STARTED_RUN_IDS_FIELD,
            _REPORT_CARRIED_CONTEXT_FIELD,
            _REPORT_CARRIED_CONTEXT_TRUNCATED_FIELD,
        ],
        "additionalProperties": False,
        "properties": {
            _REPORT_ANSWER_FIELD: {"type": "string", "minLength": 1},
            _REPORT_STARTED_RUN_IDS_FIELD: {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
            },
            _REPORT_CARRIED_CONTEXT_FIELD: {"type": "string"},
            _REPORT_CARRIED_CONTEXT_TRUNCATED_FIELD: {"type": "boolean"},
        },
    }
)

# The e2e harness's executor is the fixed-answer fake `RecordingAgentExecutorFactoryV2`
# (`serve_cockpit.py`), never a real model reading this text, so it only needs
# to be a real, nonempty instruction -- not the product's own conductor orders.
_INSTRUCTION = (
    "You run one round of an ongoing conversation loop. Read the operator's "
    f"message this round from the {CONDUCTOR_WAIT_NODE_ID!r} answer and "
    "answer with exactly one JSON object naming "
    f'"{_REPORT_ANSWER_FIELD}", "{_REPORT_STARTED_RUN_IDS_FIELD}", '
    f'"{_REPORT_CARRIED_CONTEXT_FIELD}" and '
    f'"{_REPORT_CARRIED_CONTEXT_TRUNCATED_FIELD}".'
)


def conductor_workflow_document(
    message_schema_revision: str, report_schema_revision: str
) -> bytes:
    """The e2e harness's publishable conductor document.

    The two schema revisions are published-catalog facts the caller resolves
    (the hash of the published schema the wait answer and the report agree
    to), so they arrive as parameters rather than being invented here.
    """

    return f"""format_version: 3
name: {CONDUCTOR_WORKFLOW_NAME}
description: >-
  Answers your workshop messages round after round: it reads what you just
  said, starts the real run you ask for, and reports back with the run
  reference -- up to {CONDUCTOR_LOOP_MAXIMUM_ROUNDS} rounds per conversation.
nodes:
  - id: {CONDUCTOR_WAIT_NODE_ID}
    type: wait
    prompt: What would you like the conductor to do?
    outputs:
      - name: {CONDUCTOR_MESSAGE_OUTPUT}
        schema: {{ref: conductor-message, revision: "{message_schema_revision}"}}
  - id: {CONDUCTOR_AGENT_NODE_ID}
    type: agent
    role: {CONDUCTOR_ROLE}
    mode: headless_with_tools
    instruction: >-
      {_INSTRUCTION}
    depends_on: [{CONDUCTOR_WAIT_NODE_ID}]
    inputs:
      - name: {CONDUCTOR_MESSAGE_OUTPUT}
        from: {{node: {CONDUCTOR_WAIT_NODE_ID}, output: {CONDUCTOR_MESSAGE_OUTPUT}}}
      - name: {_PREVIOUS_REPORT_INPUT}
        from: {{node: {CONDUCTOR_AGENT_NODE_ID}, output: {_REPORT_OUTPUT}}}
    outputs:
      - name: {_REPORT_OUTPUT}
        schema: {{ref: conductor-report, revision: "{report_schema_revision}"}}
loops:
  - id: {CONDUCTOR_LOOP_ID}
    body: [{CONDUCTOR_WAIT_NODE_ID}, {CONDUCTOR_AGENT_NODE_ID}]
    maximum_rounds: {CONDUCTOR_LOOP_MAXIMUM_ROUNDS}
""".encode()
