"""Names built ahead of their caller and kept on purpose.

Operator ruling 04.09.2026: code built before a caller exists is frozen, not
thrown away -- we would only build it again. Frozen means no hardening and no
new tests, not silent rot: `scripts/check_dead_code.py` reports every entry on
every run without failing, and each group names the open item that owns the
caller it waits for. When that item lands the caller, the entry is deleted;
when it decides against the caller, the code is deleted with it.

A name is written as `module/path.py:symbol`, relative to `src/atelier2`: the
gate excuses that symbol where it was built and nowhere else, so freezing one
vocabulary word never vouches for a dead namesake in another module.

Read as data by the gate; never imported at runtime.
"""

WAITING_FOR_A_CALLER = (
    {
        "names": (
            "ports/project_source.py:detach_from_repository",
            "adapters/project_source.py:detach_from_repository",
        ),
        "why": (
            "The lease is a linked worktree so a claim can be held from it; the "
            "operation that removes its worktree pointer again is called by the "
            "build node once the claim is held, and that ordering (lease -> "
            "branch -> claim -> provider) is its own slice."
        ),
        "item": "#1271 Zeile 12 (Scheibe D, Reihenfolge Lease -> Claim -> Provider)",
    },
    {
        "names": (
            "ports/agent_executions.py:terminal_outcome",
            "adapters/agent_client_protocol.py:AgentClientProtocolConversation",
        ),
        "why": (
            "The duplex conversation seam (ADR 0020 step 2): the ACP client "
            "reads the wire, correlates every question and composes the typed "
            "terminal outcome, but every executor this product runs still "
            "answers in print mode -- so nothing opens a conversation, and "
            "nothing reads the outcome a completion carries, until the first "
            "speaking executor revision is registered."
        ),
        "item": "#1177 Schritt 2 (2-D Grok-Executor als Aufrufer)",
    },
    {
        "names": (
            "adapters/runner_child.py:start_runner_child",
            "adapters/runner_child.py:reap_cancelled_runner_child",
            "adapters/runner_child.py:landlock_kernel_abi",
        ),
        "why": (
            "The one runner-cluster module #1252 kept: #1177 Schritt 0 already "
            "names `start_runner_child` as the subprocess primitive the future "
            "AgentSession duplex driver spawns a provider child through, and "
            "the cancel/landlock-ABI helpers beside it serve the same seam. "
            "Every other caller was the frozen Agent Runner deleted with #1252."
        ),
        "item": "#1177 Schritt 2 (2-B/2-C duplex driver)",
    },
    {
        "names": ("contracts/agents.py:AuthReference",),
        "why": (
            "#1177 F names this exact shape as the Runner's future credential "
            'reference ("eine logische Referenz (AuthReference)"); its one '
            "production constructor, the fake-free candidate, was deleted with "
            "#1252 and no other provider builds one yet."
        ),
        "item": "#1177 Schritt 2/F (Credential-Referenz)",
    },
    {
        "names": (
            "contracts/effects.py:confirm_execution",
            "contracts/effects.py:authorize_retry",
            "adapters/dbos/run_store.py:commit_action_completed",
        ),
        "why": (
            "The effect-reconciliation half of an Action node: an authorization "
            "confirms an execution and a retry is authorized against the same "
            "intent, but no route or workflow calls either yet -- the run store's "
            "completion writer waits with them."
        ),
        "item": "#1168 Befund 7 (test-only-lebendig, Owner beim Dispatch)",
    },
    {
        "names": (
            "adapters/dbos/host_configuration.py:latest_model_registry_revisions",
            "ports/host_configuration.py:latest_model_registry_revisions",
            "adapters/dbos/host_configuration.py:publish_project_root_revision",
        ),
        "why": (
            "Host-configuration reads and writes declared on the port and "
            "implemented in the DBOS adapter, with no route asking for them yet."
        ),
        "item": "#1168 Befund 7 (test-only-lebendig, Owner beim Dispatch)",
    },
    {
        "names": (
            "api/stream.py:peak_active_queries",
            "api/stream.py:abandoned_queries",
            "adapters/dbos/runtime.py:effect_adapter",
        ),
        "why": (
            "Instrumentation the SSE runner and the DBOS runtime expose for a "
            "reader that does not exist yet; today only their tests observe them."
        ),
        "item": "#1168 Befund 7 (test-only-lebendig, Owner beim Dispatch)",
    },
    {
        "names": (
            "adapters/github/effects.py:recorded_pull_requests",
            "adapters/github/effects.py:recorded_documentation_pushes",
        ),
        "why": (
            "Recorders on the GitHub effect fake that lives in the production "
            "adapter module; their callers are acceptance tests, and moving the "
            "fake out of src is a cut that item owns."
        ),
        "item": "#1168 Befund 7 (test-only-lebendig, Owner beim Dispatch)",
    },
    {
        "names": (
            "application/resolve_references.py:resolve_declared_reference",
            "contracts/agent_definitions.py:agent_configuration_revision_for",
            "contracts/workflows_v3.py:join_of",
            "contracts/run_bindings.py:AnyBoundRun",
            "contracts/node_records_v3.py:MAXIMUM_KIND_TOKEN_CHARACTERS",
            "contracts/catalog_v3.py:derived",
            "contracts/catalog_v3.py:claimed",
        ),
        "why": (
            "Contract helpers a caller was planned for and has not arrived at: the "
            "scheduler that applies a join, the reader that reports which lineage "
            "id was claimed and which was derived, the bound-run alias and the "
            "kind token bound. Each is proven by a domain test and named by an "
            "ADR."
        ),
        "item": "#1168 Befund 7 (test-only-lebendig, Owner beim Dispatch)",
    },
    {
        "names": (
            "contracts/agent_permissions.py:COMMAND",
            "contracts/agent_permissions.py:NETWORK",
            "contracts/agent_permissions.py:SECRET_READ",
            "contracts/agent_permissions.py:COMMAND_NAME",
            "contracts/agent_permissions.py:HOST",
        ),
        "why": (
            "The words no question can be spelled in yet "
            "(contracts/agent_permissions.py): the ACP client asks a workspace "
            "read or write scoped by path, so those words have a speaker, but a "
            "standard permission request names no command and no host and a "
            "credential channel is opened by nothing. They wait for the vendor "
            "vocabulary that can scope a command (ADR 0020 step 2)."
        ),
        "item": "#1177 Schritt 2 (erster fragender Provider-Kanal)",
    },
    {
        "names": (
            "host/mcp_tools.py:METHOD_INITIALIZED",
            "host/mcp_tools.py:MCP_TOOL_HTTP_DOORS",
        ),
        "why": (
            "The MCP door table and the initialized notification: the server "
            "answers the methods it serves today, and the table that maps every "
            "tool to its HTTP door waits for the router that reads it."
        ),
        "item": "#1168 Befund 7 (test-only-lebendig, Owner beim Dispatch)",
    },
)
