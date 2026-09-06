"""Names a production site does reach, at a site `vulture` cannot see.

Read as data by `scripts/check_dead_code.py`; never imported at runtime. Every
entry is `module/path.py:symbol`, relative to `src/atelier2`, exactly as
vulture reports it: qualifying by module is what stops excusing a name in one
module from vouching for a dead namesake in another. Every group names the
site that reaches it, because an entry without a site is an excuse rather than
a fact. An entry the gate no longer needs is deleted, not kept "just in
case" -- the gate refuses a name it no longer reports.
"""

REACHED_BY_A_SITE_VULTURE_CANNOT_SEE = (
    {
        "names": ("adapters/runner_child.py:install_landlock_guard",),
        "why": (
            "adapters/runner_child.py builds the child's `-c` program as text and "
            "imports this name inside that text, so the only caller is a string."
        ),
    },
    {
        "names": ("contracts/agents.py:API_KEY",),
        "why": (
            "An AuthMode member; claude_subscription.py, codex_subscription.py "
            "and grok_subscription.py each refuse a bound profile whose auth "
            "mode `is not AuthMode.SUBSCRIPTION`, so the negative member is "
            "selected by comparison, never named, and its own tests construct "
            "it directly to prove that refusal."
        ),
    },
    {
        "names": (
            "adapters/agent_client_protocol.py:END_TURN",
            "adapters/agent_client_protocol.py:MAX_TOKENS",
            "adapters/agent_client_protocol.py:MAX_TURN_REQUESTS",
            "adapters/acp_vocabulary.py:IN_PROGRESS",
        ),
        "why": (
            "AcpStopReason and AcpToolCallStatus members; "
            "adapters/agent_client_protocol.py reads a stop reason and a tool "
            "call's progress back from the wire by the value the protocol "
            "publishes, never by attribute."
        ),
    },
    {
        "names": ("contracts/agents.py:INTERACTIVE",),
        "why": (
            "An AgentExecutionCapability member; a manifest declares capabilities "
            "by value, and the vocabulary must stay whole to refuse the rest."
        ),
    },
    {
        "names": (
            "contracts/node_records_v3.py:DETERMINISTIC",
            "contracts/node_records_v3.py:WAIT",
            "contracts/node_records_v3.py:SUBWORKFLOW",
            "contracts/node_records_v3.py:ACTION",
        ),
        "why": (
            "NodeKindV3 members; a workflow document names a node kind by value "
            "and the wire vocabulary must carry all five."
        ),
    },
    {
        "names": ("contracts/node_records_v3.py:BLOCKED",),
        "why": (
            "A member of both NodeStateName and PersistedReceiptDisposition; a "
            "stored state is read back by value."
        ),
    },
    {
        "names": (
            "contracts/revisions_v3.py:SCORECARD_POLICY",
            "contracts/revisions_v3.py:SELECTION_POLICY",
            "contracts/revisions_v3.py:ADMISSION_POLICY",
        ),
        "why": (
            "RevisionKind members; a published revision names its kind by value, "
            "and the catalog refuses a kind this vocabulary does not carry."
        ),
    },
    {
        "names": ("contracts/verdicts.py:REVISE",),
        "why": (
            "A Verdict member; a review answer is decoded from its value, and "
            "ACCEPTED alone would not be a verdict."
        ),
    },
    {
        "names": (
            "contracts/budgets_v3.py:attempt_deadline_seconds",
            "contracts/budgets_v3.py:reported_input_token_threshold",
            "contracts/budgets_v3.py:reported_output_token_threshold",
        ),
        "why": (
            "Budget document fields read through BudgetField, whose members carry "
            "these exact names as their values (contracts/budgets_v3.py)."
        ),
    },
    {
        "names": ("contracts/workflows_v3.py:from_output",),
        "why": (
            "A handover field pydantic binds from the workflow document; the "
            "document names the key, no Python reader names the attribute."
        ),
    },
    {
        "names": (
            "contracts/budgets_v3.py:content_hash",
            "contracts/agent_definitions.py:definition_hash",
        ),
        "why": (
            "`field(init=False)` digests their own frozen dataclass writes with "
            '`object.__setattr__(self, "content_hash", ...)` in __post_init__.'
        ),
    },
    {
        "names": ("adapters/dbos/runtime.py:canonical_database_path",),
        "why": (
            "Read by DbosRuntimeBinding's generated `__eq__`: adapters/dbos/"
            "runtime.py refuses a second, incompatible binding by comparing the "
            "whole record."
        ),
    },
    {
        "names": ("adapters/dbos/schema.py:create_sql",),
        "why": (
            "Read by `asdict()` in adapters/dbos/schema.py's product-schema "
            "fingerprint, whose sha256 is what refuses a malformed store."
        ),
    },
    {
        "names": (
            "adapters/dbos/node_binding_codec.py:tool_capability",
            "adapters/dbos/node_binding_codec.py:project_commit",
            "adapters/dbos/node_binding_codec.py:project_tree",
            "adapters/dbos/node_binding_codec.py:output_schema_document",
        ),
        "why": (
            "TypedDict keys adapters/dbos/node_binding_codec.py reads as string "
            "subscripts of the encoded binding, never as attributes."
        ),
    },
    {
        "names": ("adapters/dbos/schema.py:agent_receipts",),
        "why": (
            "A sa.Table that registers itself in adapters/dbos/schema.py's shared "
            "MetaData on construction; every table in that module keeps a name."
        ),
    },
    {
        "names": ("contracts/schemas_v3.py:checker",),
        "why": (
            "The TypeChecker jsonschema passes to a registered type check "
            "(contracts/schemas_v3.py); the library owns the signature."
        ),
    },
    {
        "names": ("api/openapi.py:openapi",),
        "why": "FastAPI reads `app.openapi` when it serves the document.",
    },
    {
        "names": ("host/logging.py:disabled",),
        "why": "The stdlib `logging.Logger.disabled` flag host/logging.py sets.",
    },
)
