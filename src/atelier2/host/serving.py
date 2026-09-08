from __future__ import annotations

import asyncio
import json
import logging
import os
import selectors
import signal
import stat
import subprocess
import tempfile
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import assert_never

import uvicorn
from fastapi import FastAPI
from starlette.types import Lifespan

from atelier2.adapters.bounded_processes import (
    BoundedProcessFailure,
    bounded_process_streams,
)
from atelier2.adapters.candidate_store import CANDIDATE_STORE_DIRECTORY_NAME
from atelier2.adapters.claude_subscription import (
    ClaudeAtelierDoorsExecutorFactory,
    ClaudeAtelierDoorsSettings,
    ClaudeSubscriptionExecutorFactory,
    ClaudeSubscriptionSettings,
    ClaudeWorkspaceToolExecutorFactory,
)
from atelier2.adapters.codex_subscription import (
    CODEX_SUBSCRIPTION_EXECUTOR_KEY,
    CodexSubscriptionExecutorFactory,
    CodexSubscriptionSettings,
)
from atelier2.adapters.dbos.advancer import (
    legacy_agent_effect_runs_without_receipt,
)
from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.artifact_store import DbosArtifactStore
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.host_configuration import DbosHostConfigurationChannel
from atelier2.adapters.dbos.queries import DbosQueries
from atelier2.adapters.dbos.queue_projection_store import DbosQueueProjectionStore
from atelier2.adapters.dbos.reconciler import DbosEffectReconcileCommander
from atelier2.adapters.dbos.run_store import DbosWaitAnswerer
from atelier2.adapters.dbos.runtime import (
    AGENT_TERMINATION_GRACE_SECONDS,
    SQLITE_LOCK_TIMEOUT_SECONDS,
    DbosRuntime,
    DbosRuntimeSettings,
    create_canonical_engine,
)
from atelier2.adapters.dbos.schema import initialize_schema
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.adapters.github import (
    live_github_effect_registry,
    live_github_issue_source,
)
from atelier2.adapters.github.project_connections import GitHubProjectSourceConnector
from atelier2.adapters.grok_subscription import (
    GROK_SUBSCRIPTION_EXECUTOR_KEY,
    GROK_WORKSPACE_TOOLS_EXECUTOR_KEY,
    GrokSubscriptionExecutorFactory,
    GrokSubscriptionSettings,
    GrokWorkspaceToolExecutorFactory,
)
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.adapters.markdown_agent_definitions import (
    parse_agent_definition,
    render_agent_definition,
)
from atelier2.adapters.project_source_credentials import (
    MANAGED_PROJECT_SOURCE_CREDENTIALS_DIRECTORY,
    FilesystemProjectSourceCredentialStore,
)
from atelier2.adapters.redeploy_status import (
    filesystem_redeploy_status_reader,
    redeploy_status_path,
)
from atelier2.adapters.yaml_workflows import parse_workflow_document
from atelier2.api.app import create_app
from atelier2.api.context import ApiPorts
from atelier2.api.limits import (
    ApiLimits,
    base64_characters_for,
    durable_projection_limit,
)
from atelier2.api.seat import no_seat_declared
from atelier2.api.stream import EventPollBackoff
from atelier2.application.project_connections import (
    PlatformConnectionUnknown,
    ProjectSourceConnectionRead,
    get_project_source_connection,
)
from atelier2.application.refusals import DurableStateCorrupt, ReadUnavailable
from atelier2.contracts.agent_attempts import AgentAttemptId
from atelier2.contracts.agents import (
    MAXIMUM_AGENT_FIELD_CHARACTERS,
    MAXIMUM_AGENT_OUTPUT_BYTES_V2,
    AgentConfigurationRevision,
    AgentExecutionRequestV2,
    AgentExecutionResult,
    AgentExecutorOperationalIdentity,
    AgentRole,
    AuthProfileRevision,
    ResolvedAgentBinding,
)
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.executions import NodeExecutionId
from atelier2.contracts.host_configuration import (
    ProjectId,
    ProjectSourceConnectionRevision,
    ProviderModelCheck,
)
from atelier2.contracts.pages import PageLimit
from atelier2.contracts.runs import RunId, WorkflowRevisionHash
from atelier2.host.address import DEFAULT_HOST, DEFAULT_PORT, is_loopback_host
from atelier2.host.logging import configure_process_logging
from atelier2.host.mcp_command import stdio_door_command
from atelier2.host.mcp_tools import MCP_SERVER_NAME, McpToolName
from atelier2.host.provider_canary import (
    default_provider_canary_state_directory,
    provider_layer_digest,
)
from atelier2.host.run_command import REQUEST_TIMEOUT_SECONDS
from atelier2.host.served_seat import (
    SeatDeclaration,
    ServedSeat,
    composed_seat,
    refuse_unservable_seat,
    seat_lifespan,
)
from atelier2.ports.agent_executions import (
    MAXIMUM_AGENT_PROCESS_STANDARD_ERROR_BYTES,
    AgentAttemptWorkspaceLease,
    AgentExecutorFactoryV2,
    AgentExecutorKey,
    AgentExecutorRegistration,
    AgentExecutorRegistry,
    AgentProcessCompletion,
    AgentProcessInvocation,
    WorkspaceFileTools,
)
from atelier2.ports.effects import EffectAdapterFactory, EffectAdapterRegistry
from atelier2.ports.host_configuration import (
    ProviderModelDiscovery,
    ProviderModelDiscoveryResult,
    ProviderModelDiscoveryUnsupported,
    ProviderModelInspectionUnavailable,
    ProviderModelValidationResult,
)

# The edge must admit exactly the largest result the durable agent contract
# accepts, and nothing larger: a tighter bound refuses work the store would
# have kept, a looser one admits work the store then refuses. So both numbers
# are derivations of one owner rather than typed constants -- the decoded bound
# *is* the durable bound, and the base64 bound is that same number in transport
# form. As typed literals they drifted silently, because `api/stream.py`
# reports the resulting refusal as a clean end of stream.
MAXIMUM_DECODED_PAYLOAD_BYTES = MAXIMUM_AGENT_OUTPUT_BYTES_V2
MAXIMUM_BASE64_CHARACTERS = base64_characters_for(MAXIMUM_DECODED_PAYLOAD_BYTES)
# The HTTP body owns its transport envelope independently of any one field. This
# deployment default admits the largest supported answer together with its JSON
# keys, revision, and maximum node id; a behavior test crosses the real middleware
# seam so envelope growth cannot silently make that legal payload undeliverable.
MAXIMUM_REQUEST_BODY_BYTES = 68 * 1_024
MAXIMUM_FIELD_CHARACTERS = MAXIMUM_AGENT_FIELD_CHARACTERS
MAXIMUM_WORKFLOW_NODES = 100

# A listing that says what its revisions are called has to read and parse their
# documents, and the measurement says those are two costs in two units: the
# parse is paid per node -- 0.66 to 1.52 ms per node, holding across a 150x byte
# range -- and the read is paid per byte. So one page may parse no more nodes,
# and move no more document bytes, than this edge already admits for a single
# document. Both are derivations of those two owners rather than second literals,
# which is what stops a raised document bound from leaving a page bound behind.
# Neither implies the other: a hundred one-node documents still weigh megabytes,
# and a page bounded only by bytes still holds hundreds of nodes.
MAXIMUM_ENRICHED_PAGE_NODES = MAXIMUM_WORKFLOW_NODES
MAXIMUM_ENRICHED_PAGE_DOCUMENT_BYTES = MAXIMUM_REQUEST_BODY_BYTES

EVENT_PAGE_SIZE = 50
MAXIMUM_CONTROL_QUERIES = 8
MAXIMUM_EVENT_POLL_QUERIES = 2
MAXIMUM_QUERY_ADMISSION_WAIT_MILLISECONDS = 1_000

INITIAL_EVENT_POLL_DELAY_SECONDS = 0.05
MAXIMUM_EVENT_POLL_DELAY_SECONDS = 1.0
EVENT_POLL_DELAY_MULTIPLIER = 2.0


def api_limits(
    *,
    event_page_size: int = EVENT_PAGE_SIZE,
    maximum_control_queries: int = MAXIMUM_CONTROL_QUERIES,
    maximum_event_poll_queries: int = MAXIMUM_EVENT_POLL_QUERIES,
    maximum_query_admission_wait_milliseconds: int = (
        MAXIMUM_QUERY_ADMISSION_WAIT_MILLISECONDS
    ),
) -> ApiLimits:
    """The limits one served instance enforces, with this deployment's answers.

    The bounds above the signature are the wire's and the store's: they say what
    the product can represent, and an instance does not get to disagree with
    them. The four below it are this instance's own -- how much reading it admits
    at once and how large a page it answers with -- and they are the ones a second
    machine honestly wants differently.

    The defaults are the same values the host baked in before, in the one place
    that names them, so an instance that configures nothing behaves exactly as it
    did.
    """

    return ApiLimits(
        maximum_request_body_bytes=MAXIMUM_REQUEST_BODY_BYTES,
        maximum_field_characters=MAXIMUM_FIELD_CHARACTERS,
        maximum_base64_characters=MAXIMUM_BASE64_CHARACTERS,
        maximum_decoded_payload_bytes=MAXIMUM_DECODED_PAYLOAD_BYTES,
        maximum_workflow_nodes=MAXIMUM_WORKFLOW_NODES,
        maximum_enriched_page_nodes=MAXIMUM_ENRICHED_PAGE_NODES,
        maximum_enriched_page_document_bytes=MAXIMUM_ENRICHED_PAGE_DOCUMENT_BYTES,
        event_page_size=PageLimit(event_page_size),
        maximum_control_queries=maximum_control_queries,
        maximum_event_poll_queries=maximum_event_poll_queries,
        maximum_query_admission_wait_milliseconds=(
            maximum_query_admission_wait_milliseconds
        ),
    )


def event_poll_backoff(
    *,
    initial_delay_seconds: float = INITIAL_EVENT_POLL_DELAY_SECONDS,
    maximum_delay_seconds: float = MAXIMUM_EVENT_POLL_DELAY_SECONDS,
    multiplier: float = EVENT_POLL_DELAY_MULTIPLIER,
) -> EventPollBackoff:
    """How this instance waits between polls, refused by its own owner.

    Every range rule already lives on `EventPollBackoff` -- positive start, a
    ceiling no lower than the start, a multiplier above one. Nothing is restated
    here: what was missing was never the refusal, only a way to reach the values.
    """

    return EventPollBackoff(
        initial_delay_seconds=initial_delay_seconds,
        maximum_delay_seconds=maximum_delay_seconds,
        multiplier=multiplier,
    )


@dataclass(frozen=True)
class HostSettings:
    database_path: Path
    effect_store_path: Path
    effect_adapter_revision: str
    effect_destination: str
    application_version: str
    source_commit: str
    source_tree: str
    frontend_dist: Path
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    limits: ApiLimits = field(default_factory=api_limits)
    # The two store- and process-side answers, beside the two API-side ones above.
    # Their range rules live on `DbosRuntimeSettings`, which is built from them.
    sqlite_lock_timeout_seconds: float = SQLITE_LOCK_TIMEOUT_SECONDS
    agent_termination_grace_seconds: float = AGENT_TERMINATION_GRACE_SECONDS
    model_inspection_timeout_seconds: float = REQUEST_TIMEOUT_SECONDS
    event_poll_backoff: EventPollBackoff = field(default_factory=event_poll_backoff)
    agent_scratch_root: Path | None = None
    project_id: ProjectId | None = None
    project_root: Path | None = None
    aco_executable: Path | None = None
    """The `aco` command a run holds its work item's lane claim with.

    Without it a node that would change the project refuses instead of building
    unclaimed: the claim is a precondition of the work, not a decoration on it.
    """
    claude_subscription: ClaudeSubscriptionSettings | None = None
    claude_workspace_tools: bool = False
    """Whether the Claude deployment also serves its tool-bearing executor.

    A separate answer from the deployment itself, because it is a separate
    grant: the tool-free executor is what a Claude deployment is, and the
    tool-bearing one lets a node's own process read, write and run commands as
    the serving user. An operator says yes to that once, here, and never as a
    side effect of naming an executable.
    """
    claude_atelier_doors: bool = False
    """Whether the Claude deployment also serves the atelier-doors executor.

    A third, separately armed grant of the same deployment: it lets a node's
    own process choose, start and observe catalog runs through the serving
    host's own MCP door -- real billed children behind one node. An operator
    says yes to that once, here, and never as a side effect of naming an
    executable. Routine use additionally waits on the billed conformance probe
    the executor's docstring names.
    """
    claude_start_refusal: str | None = None
    claude_workspace_tools_start_refusal: str | None = None
    claude_atelier_doors_start_refusal: str | None = None
    grok_subscription: GrokSubscriptionSettings | None = None
    grok_workspace_tools: bool = False
    """Whether the Grok deployment also serves its tool-bearing executor.

    A separate answer from the deployment itself, because it is a separate
    grant: the tool-free executor is what a Grok deployment is, and the
    tool-bearing one lets a node's own process read, write and run commands as
    the serving user. An operator says yes to that once, here, and never as a
    side effect of naming an executable.
    """
    grok_start_refusal: str | None = None
    grok_workspace_tools_start_refusal: str | None = None
    codex_subscription: CodexSubscriptionSettings | None = None
    codex_start_refusal: str | None = None
    # The provider-probe receipt gate's evidence directory (`#1013`): `None`
    # takes the same default `atelier2 provider-canary` already writes to
    # (`default_provider_canary_state_directory`, reused rather than a second
    # constant), so a real deployment arms the gate without naming a flag.
    # Overriding it is for isolating a fixture's own receipts, never for
    # turning the gate off -- `source_commit` above always travels with it.
    provider_probe_receipt_directory: Path | None = None
    terminal_seat: SeatDeclaration | None = None
    """The terminal seat this serve holds open, or none.

    Declared once, here, because a seat is a process tree this deployment owns:
    the session outlives the serve, the terminal server does not.
    """

    @property
    def billed_providers(self) -> tuple[str, ...]:
        """Name every configured provider whose attempts spend a subscription."""

        configured = (
            ("Claude", self.claude_subscription),
            ("Grok", self.grok_subscription),
            ("Codex", self.codex_subscription),
        )
        return tuple(name for name, settings in configured if settings is not None)

    def runtime_settings(self) -> DbosRuntimeSettings:
        """The durable runtime's own answers, built by the record that holds them.

        Built rather than re-checked, and built here rather than deep inside the
        composition: the range rules live on `DbosRuntimeSettings`, and asking it
        early is what puts its refusal on the same path as every other one --
        where the command line can turn it into a named error instead of a
        traceback. Copying the rules up here would have been the other way, and
        the wrong one.
        """

        return DbosRuntimeSettings(
            self.database_path,
            self.application_version,
            agent_scratch_root=self.agent_scratch_root,
            project_id=self.project_id,
            bootstrap_project_root=self.project_root,
            aco_executable=self.aco_executable,
            agent_termination_grace_seconds=self.agent_termination_grace_seconds,
            sqlite_lock_timeout_seconds=self.sqlite_lock_timeout_seconds,
            provider_probe_receipt_directory=(
                self.provider_probe_receipt_directory
                if self.provider_probe_receipt_directory is not None
                else default_provider_canary_state_directory()
            ),
            provider_probe_receipt_provider_layer_digest=provider_layer_digest(),
        )

    def __post_init__(self) -> None:
        object.__setattr__(self, "database_path", self.database_path.resolve())
        object.__setattr__(self, "effect_store_path", self.effect_store_path.resolve())
        object.__setattr__(self, "frontend_dist", self.frontend_dist.resolve())
        if self.provider_probe_receipt_directory is not None:
            object.__setattr__(
                self,
                "provider_probe_receipt_directory",
                self.provider_probe_receipt_directory.resolve(),
            )
        self._require_well_formed_fields()
        self._require_coherent_executors()
        self._require_start_refusals()
        # Asked last, once every path this record resolves is settled. Its
        # refusals belong to the durable runtime and are raised here so they
        # travel the same way as the ones above -- the command line catches this
        # constructor, and nothing below it.
        self.runtime_settings()

    def _require_well_formed_fields(self) -> None:
        if self.database_path == self.effect_store_path:
            raise ValueError("durable database and effect store must be distinct")
        if self.project_root is not None and self.project_id is None:
            raise ValueError(
                "--project-root writes the host configuration channel, so it "
                "needs --project-id"
            )
        if self.project_id is not None and not isinstance(self.project_id, ProjectId):
            raise TypeError("project id must use its typed contract")
        if self.model_inspection_timeout_seconds <= 0:
            raise ValueError("model inspection timeout must be positive")
        for name in (
            "effect_adapter_revision",
            "effect_destination",
            "application_version",
            "source_commit",
            "source_tree",
            "host",
        ):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must be nonempty")
        if type(self.port) is not int or not 1 <= self.port <= 65_535:
            raise ValueError("port must be an integer between 1 and 65535")
        if (
            not (self.frontend_dist / "index.html").is_file()
            or not (self.frontend_dist / "assets").is_dir()
        ):
            raise ValueError("frontend distribution must contain index.html and assets")

    def _require_coherent_executors(self) -> None:
        billed = self.billed_providers
        if billed and self.agent_scratch_root is None:
            raise ValueError(
                "serving a provider executor and declaring --agent-scratch-root "
                "go together: every attempt runs in a scratch workspace of its "
                "own, and a provider without a scratch root would share one "
                "directory across every attempt"
            )
        if self.claude_workspace_tools and self.claude_subscription is None:
            raise ValueError(
                "serving the Claude workspace-tool executor needs the Claude "
                "deployment it is a second executor of"
            )
        if self.claude_atelier_doors and self.claude_subscription is None:
            raise ValueError(
                "serving the Claude atelier-doors executor needs the Claude "
                "deployment it is a third executor of"
            )
        if self.grok_workspace_tools and self.grok_subscription is None:
            raise ValueError(
                "serving the Grok workspace-tool executor needs the Grok "
                "deployment it is a second executor of"
            )
        refuse_unservable_seat(
            self.terminal_seat,
            project_id=self.project_id,
            project_root=self.project_root,
            api_host=self.host,
        )
        if self.agent_scratch_root is not None and not billed:
            raise ValueError(
                "a scratch root without a provider executor serves nothing"
            )
        if billed and not is_loopback_host(self.host):
            raise ValueError(
                f"serving {' and '.join(billed)} subscription agents requires a "
                f"loopback bind, not {self.host!r}: starting a billed provider is "
                "unauthenticated on this API, so the billed boundary stays on this "
                "machine until an authenticated boundary exists"
            )

    def _require_start_refusals(self) -> None:
        _require_start_refusal(
            "Claude", self.claude_subscription, self.claude_start_refusal
        )
        _require_start_refusal(
            "Claude workspace-tool",
            self.claude_subscription if self.claude_workspace_tools else None,
            self.claude_workspace_tools_start_refusal,
        )
        _require_start_refusal(
            "Claude atelier-doors",
            self.claude_subscription if self.claude_atelier_doors else None,
            self.claude_atelier_doors_start_refusal,
        )
        _require_start_refusal("Grok", self.grok_subscription, self.grok_start_refusal)
        _require_start_refusal(
            "Grok workspace-tool",
            self.grok_subscription if self.grok_workspace_tools else None,
            self.grok_workspace_tools_start_refusal,
        )
        _require_start_refusal(
            "Codex", self.codex_subscription, self.codex_start_refusal
        )


def _require_start_refusal(
    name: str, declared: object | None, refusal: str | None
) -> None:
    if refusal is None:
        return
    if declared is None:
        raise ValueError(f"a {name} start refusal names a declared {name} deployment")
    if not refusal.strip():
        raise ValueError(f"a {name} start refusal must be nonempty")


def _subscription_executor_registrations(
    settings: HostSettings,
) -> tuple[AgentExecutorRegistration, ...]:
    claude_subscription = settings.claude_subscription
    grok_subscription = settings.grok_subscription
    codex_subscription = settings.codex_subscription
    return (
        *(
            (
                _subscription_registration(
                    ClaudeSubscriptionExecutorFactory(claude_subscription),
                    settings.claude_start_refusal is not None,
                    WorkspaceFileTools.WITHHELD,
                ),
            )
            if claude_subscription is not None
            else ()
        ),
        *(
            (
                _subscription_registration(
                    ClaudeWorkspaceToolExecutorFactory(claude_subscription),
                    settings.claude_start_refusal is not None
                    or settings.claude_workspace_tools_start_refusal is not None,
                    WorkspaceFileTools.GRANTED,
                ),
            )
            if (claude_subscription is not None and settings.claude_workspace_tools)
            else ()
        ),
        *(
            (
                _subscription_registration(
                    ClaudeAtelierDoorsExecutorFactory(
                        _atelier_doors_settings(claude_subscription, settings)
                    ),
                    settings.claude_start_refusal is not None
                    or settings.claude_atelier_doors_start_refusal is not None,
                    WorkspaceFileTools.WITHHELD,
                ),
            )
            if (claude_subscription is not None and settings.claude_atelier_doors)
            else ()
        ),
        *(
            (
                _subscription_registration(
                    GrokSubscriptionExecutorFactory(grok_subscription),
                    settings.grok_start_refusal is not None,
                    WorkspaceFileTools.WITHHELD,
                ),
            )
            if grok_subscription is not None
            else ()
        ),
        *(
            (
                _subscription_registration(
                    GrokWorkspaceToolExecutorFactory(grok_subscription),
                    settings.grok_start_refusal is not None
                    or settings.grok_workspace_tools_start_refusal is not None,
                    WorkspaceFileTools.GRANTED,
                ),
            )
            if (grok_subscription is not None and settings.grok_workspace_tools)
            else ()
        ),
        *(
            (
                _subscription_registration(
                    CodexSubscriptionExecutorFactory(codex_subscription),
                    settings.codex_start_refusal is not None,
                    WorkspaceFileTools.WITHHELD,
                ),
            )
            if codex_subscription is not None
            else ()
        ),
    )


def _subscription_registration(
    factory: AgentExecutorFactoryV2,
    unavailable: bool,
    workspace_file_tools: WorkspaceFileTools,
) -> AgentExecutorRegistration:
    """Register one subscription executor, saying what its invocation may touch.

    `workspace_file_tools` is the executor's own sentence, restated where the
    deployment composes it: a call that removes every built-in tool
    (`--tools=`) reaches no file of the attempt, whatever else it is granted,
    and a run whose node pins a tool grant is refused against that rather than
    cast onto it (`resolve_start_bindings`).
    """

    if unavailable:
        return AgentExecutorRegistration.unavailable(
            factory, workspace_file_tools=workspace_file_tools
        )
    return AgentExecutorRegistration.startable(
        factory, workspace_file_tools=workspace_file_tools
    )


ATELIER_DOOR_TOOLS: tuple[McpToolName, ...] = (
    McpToolName.LIST_WORKFLOWS,
    McpToolName.START_RUN,
    McpToolName.RUN_STATUS,
)


def _atelier_doors_settings(
    claude_subscription: ClaudeSubscriptionSettings, settings: HostSettings
) -> ClaudeAtelierDoorsSettings:
    """The doors deployment, composed from facts each of their own owners holds.

    The grant is the three read-and-start doors of the MCP vocabulary, never a
    re-spelled literal: humans answer the waits of started runs, so a
    choose/start/observe role gets no write door. The door command launches
    this process's own stdio door (`atelier2 mcp`) with the serving interpreter
    against this deployment's address; that it is loopback is the child's refusal.
    """

    return ClaudeAtelierDoorsSettings(
        claude_subscription,
        MCP_SERVER_NAME,
        tuple(tool.value for tool in ATELIER_DOOR_TOOLS),
        stdio_door_command(_own_service_url(settings)),
    )


def _own_service_url(settings: HostSettings) -> str:
    """Where this deployment's own API answers, as a client address.

    The bracket form is IPv6's URL grammar: a bare colon-carrying host would
    read as a port separator.
    """

    host = settings.host
    address = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"http://{address}:{settings.port}"


_MODEL_DISCOVERY_OUTPUT_BYTES = 1_048_576
_MODEL_VALIDATION_JOB = b"Reply with exactly OK."
_MODEL_VALIDATION_RUN_ID = RunId("provider-model-validation")
_MODEL_VALIDATION_WORKFLOW_HASH = WorkflowRevisionHash("0" * 64)
_MODEL_VALIDATION_NODE_ID = "provider-model-validation"


@dataclass(frozen=True)
class HostProviderModelInspector:
    """Derive registry trust from the composed provider operations.

    Discovery is a non-billed pinned-CLI operation. Validation deliberately
    travels through the same prepared command and decoder as a real attempt;
    the host therefore marks a model checked only when that adapter can use a
    provider answer, not because a child happened to exit zero.
    """

    registry: AgentExecutorRegistry
    codex_settings: CodexSubscriptionSettings | None
    grok_settings: GrokSubscriptionSettings | None
    inspection_timeout_seconds: float
    termination_grace_seconds: float

    def discover_models(
        self,
        configuration: AgentConfigurationRevision,
        auth_profile: AuthProfileRevision,
    ) -> ProviderModelDiscoveryResult:
        key = AgentExecutorKey(
            auth_profile.provider_id, configuration.executor_revision
        )
        try:
            if key == CODEX_SUBSCRIPTION_EXECUTOR_KEY:
                if self.codex_settings is None:
                    return ProviderModelInspectionUnavailable()
                return ProviderModelDiscovery(
                    frozenset(
                        _discover_codex_models(
                            self.codex_settings,
                            self.inspection_timeout_seconds,
                            self.termination_grace_seconds,
                        )
                    )
                )
            if key in (
                GROK_SUBSCRIPTION_EXECUTOR_KEY,
                GROK_WORKSPACE_TOOLS_EXECUTOR_KEY,
            ):
                if self.grok_settings is None:
                    return ProviderModelInspectionUnavailable()
                return ProviderModelDiscovery(
                    frozenset(
                        _discover_grok_models(
                            self.grok_settings, self.inspection_timeout_seconds
                        )
                    )
                )
            return ProviderModelDiscoveryUnsupported()
        except (
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
            BoundedProcessFailure,
            subprocess.SubprocessError,
        ):
            return ProviderModelInspectionUnavailable()

    def validate_model(
        self,
        configuration: AgentConfigurationRevision,
        auth_profile: AuthProfileRevision,
    ) -> ProviderModelValidationResult:
        key = AgentExecutorKey(
            auth_profile.provider_id, configuration.executor_revision
        )
        entry = next((item for item in self.registry.entries if item.key == key), None)
        if entry is None or entry.factory is None:
            return ProviderModelInspectionUnavailable()
        executor = entry.factory.open()
        command = None
        result: ProviderModelValidationResult = ProviderModelInspectionUnavailable()
        try:
            request = _model_validation_request(
                configuration, auth_profile, entry.manifest_entry.operational_identity
            )
            command = executor.prepare_process(request)
            with tempfile.TemporaryDirectory(
                prefix="atelier2-model-validation-"
            ) as working_directory:
                path = Path(working_directory)
                status = path.stat()
                lease = AgentAttemptWorkspaceLease(
                    AgentAttemptId.for_execution(
                        request.node_execution_id, request.request_hash, 1
                    ),
                    path,
                    status.st_dev,
                    status.st_ino,
                )
                process = subprocess.Popen(
                    command.arguments,
                    cwd=path,
                    env=dict(command.environment),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )
                assert process.stdin is not None
                try:
                    process.stdin.write(command.standard_input)
                    process.stdin.close()
                    return_code, standard_output, standard_error = (
                        bounded_process_streams(
                            process,
                            self.inspection_timeout_seconds,
                            max(
                                command.standard_output_frame_bytes,
                                MAXIMUM_AGENT_PROCESS_STANDARD_ERROR_BYTES,
                            ),
                        )
                    )
                finally:
                    if not process.stdin.closed:
                        process.stdin.close()
                if (
                    len(standard_output) > command.standard_output_frame_bytes
                    or len(standard_error) > MAXIMUM_AGENT_PROCESS_STANDARD_ERROR_BYTES
                ):
                    result = ProviderModelCheck.UNKNOWN_AT_PROVIDER
                else:
                    decoded = executor.decode_process_completion(
                        AgentProcessInvocation(command, lease),
                        AgentProcessCompletion(
                            return_code, standard_output, standard_error
                        ),
                    )
                    result = (
                        ProviderModelCheck.CHECKED
                        if isinstance(decoded, AgentExecutionResult)
                        else ProviderModelCheck.UNKNOWN_AT_PROVIDER
                    )
        except (
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
            BoundedProcessFailure,
            subprocess.SubprocessError,
        ):
            result = ProviderModelInspectionUnavailable()
        finally:
            try:
                if command is not None:
                    executor.release_credential_channel(command)
            except (OSError, ValueError):
                result = ProviderModelInspectionUnavailable()
            finally:
                executor.close()
        return result


def _model_validation_request(
    configuration: AgentConfigurationRevision,
    auth_profile: AuthProfileRevision,
    operational_identity: AgentExecutorOperationalIdentity,
) -> AgentExecutionRequestV2:
    binding = ResolvedAgentBinding(
        AgentRole(_MODEL_VALIDATION_NODE_ID), configuration, auth_profile
    )
    return AgentExecutionRequestV2(
        NodeExecutionId.for_node(
            _MODEL_VALIDATION_RUN_ID,
            _MODEL_VALIDATION_WORKFLOW_HASH,
            _MODEL_VALIDATION_NODE_ID,
        ),
        _MODEL_VALIDATION_RUN_ID,
        _MODEL_VALIDATION_WORKFLOW_HASH,
        _MODEL_VALIDATION_NODE_ID,
        binding,
        operational_identity,
        _MODEL_VALIDATION_JOB,
        maximum_assistant_turns=1,
    )


_MODEL_DISCOVERY_JOB_DIRECTORY_PREFIX = "atelier2-model-discovery-"
_MODEL_DISCOVERY_JOB_DIRECTORY_MODE = 0o700
# Both the Grok and Codex CLIs keep this file, at private (0600) permissions,
# as their credential record -- established from the pinned executables' own
# strings (Codex: "Paste or type your API key below. It will be stored
# locally in auth.json") and from each CLI's own live credential directory,
# the same way #993 established Claude's credential file. Model discovery
# asks a provider account what it may serve; it runs no job and grants no
# tool, so this is the only file it needs, whatever else the operator's
# directory also holds.
_MODEL_DISCOVERY_CREDENTIAL_FILE_NAME = "auth.json"
_MODEL_DISCOVERY_CREDENTIAL_FILE_MODE = 0o400
_MODEL_DISCOVERY_PRIVATE_FILE_FLAGS = (
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
)
_MAXIMUM_MODEL_DISCOVERY_CREDENTIAL_FILE_BYTES = 1_048_576


def _model_discovery_credential_bytes(credential_directory: Path) -> bytes:
    """Read one provider's own `auth.json`, and nothing else it holds."""

    path = credential_directory / _MODEL_DISCOVERY_CREDENTIAL_FILE_NAME
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or stat.S_IMODE(status.st_mode) & 0o077:
            raise ValueError(
                f"the {_MODEL_DISCOVERY_CREDENTIAL_FILE_NAME} credential file "
                "must be a private regular file"
            )
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(descriptor, 65_536)
            if not chunk:
                return b"".join(chunks)
            size += len(chunk)
            if size > _MAXIMUM_MODEL_DISCOVERY_CREDENTIAL_FILE_BYTES:
                raise ValueError(
                    f"the {_MODEL_DISCOVERY_CREDENTIAL_FILE_NAME} credential "
                    "file exceeds its private copy bound"
                )
            chunks.append(chunk)
    finally:
        os.close(descriptor)


@contextmanager
def _private_model_discovery_home(credential_directory: Path) -> Iterator[Path]:
    """One private, disposable directory holding a copy of `auth.json` alone.

    Model discovery spawns a real provider process, so it is never handed the
    operator's own credential directory to run in -- mirroring the discipline
    the job path already carries for a real attempt (`claude_subscription`,
    `grok_subscription`, `codex_subscription`). The directory is removed on
    every exit from this context, success, refusal or exception alike,
    because `TemporaryDirectory.__exit__` runs unconditionally.
    """

    payload = _model_discovery_credential_bytes(credential_directory)
    with tempfile.TemporaryDirectory(
        prefix=_MODEL_DISCOVERY_JOB_DIRECTORY_PREFIX
    ) as directory_name:
        directory = Path(directory_name)
        os.chmod(directory, _MODEL_DISCOVERY_JOB_DIRECTORY_MODE)
        descriptor = os.open(
            directory / _MODEL_DISCOVERY_CREDENTIAL_FILE_NAME,
            _MODEL_DISCOVERY_PRIVATE_FILE_FLAGS,
            _MODEL_DISCOVERY_CREDENTIAL_FILE_MODE,
        )
        try:
            remaining = memoryview(payload)
            while remaining:
                written = os.write(descriptor, remaining)
                if written < 1:
                    raise OSError(
                        "private model-discovery credential file write made no progress"
                    )
                remaining = remaining[written:]
        finally:
            os.close(descriptor)
        yield directory


def _discover_grok_models(
    settings: GrokSubscriptionSettings, timeout_seconds: float
) -> tuple[str, ...]:
    with _private_model_discovery_home(settings.credential_directory) as home:
        process = subprocess.Popen(
            (str(settings.executable), "models"),
            cwd=settings.workspace,
            env={
                "HOME": str(home),
                "GROK_HOME": str(home),
                "PATH": settings.search_path,
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        return_code, standard_output, _standard_error = bounded_process_streams(
            process, timeout_seconds, _MODEL_DISCOVERY_OUTPUT_BYTES
        )
    if return_code != 0:
        raise ValueError("Grok model discovery failed")
    models: list[str] = []
    in_models = False
    for line in standard_output.decode("utf-8").splitlines():
        if line.strip() == "Available models:":
            in_models = True
            continue
        stripped = line.strip()
        if not in_models or not stripped:
            continue
        if not stripped.startswith(("* ", "- ")):
            raise ValueError("Grok model discovery returned an unknown shape")
        model_id = stripped[2:].removesuffix(" (default)")
        if not model_id:
            raise ValueError("Grok model discovery returned an empty id")
        models.append(model_id)
    if not models:
        raise ValueError("Grok model discovery returned no models")
    return tuple(models)


def _discover_codex_models(
    settings: CodexSubscriptionSettings,
    timeout_seconds: float,
    termination_grace_seconds: float,
) -> tuple[str, ...]:
    with _private_model_discovery_home(settings.credential_directory) as home:
        process = subprocess.Popen(
            (str(settings.executable), "app-server"),
            cwd=home,
            env={
                "HOME": str(home),
                "CODEX_HOME": str(home),
                "PATH": settings.search_path,
            },
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        assert process.stdin is not None and process.stdout is not None
        deadline = time.monotonic() + timeout_seconds
        try:
            _send_json_rpc(
                process,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "clientInfo": {
                            "name": "atelier2",
                            "title": "Atelier 2",
                            "version": "1",
                        },
                        "capabilities": {},
                    },
                },
            )
            _read_json_rpc_result(process, 1, deadline)
            _send_json_rpc(
                process,
                {"jsonrpc": "2.0", "method": "initialized", "params": {}},
            )
            _send_json_rpc(
                process,
                {"jsonrpc": "2.0", "id": 2, "method": "model/list", "params": {}},
            )
            result = _read_json_rpc_result(process, 2, deadline)
            data = result.get("data")
            if not isinstance(data, list):
                raise TypeError("Codex model discovery returned no data list")
            models: list[str] = []
            for item in data:
                if not isinstance(item, dict):
                    continue
                model_id = item.get("model")
                if isinstance(model_id, str):
                    models.append(model_id)
            if len(models) != len(data) or not models:
                raise ValueError("Codex model discovery returned an unknown shape")
            return tuple(models)
        finally:
            process.stdin.close()
            _terminate_inspection_process(process, termination_grace_seconds)


def _send_json_rpc(process: subprocess.Popen[bytes], message: object) -> None:
    assert process.stdin is not None
    process.stdin.write(json.dumps(message, separators=(",", ":")).encode("utf-8"))
    process.stdin.write(b"\n")
    process.stdin.flush()


def _read_json_rpc_result(
    process: subprocess.Popen[bytes], request_id: int, deadline: float
) -> dict[str, object]:
    assert process.stdout is not None
    descriptor = process.stdout.fileno()
    os.set_blocking(descriptor, False)
    buffered = bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(descriptor, selectors.EVENT_READ)
        while time.monotonic() < deadline:
            ready = selector.select(max(0, deadline - time.monotonic()))
            if not ready:
                break
            chunk = os.read(
                descriptor, _MODEL_DISCOVERY_OUTPUT_BYTES + 1 - len(buffered)
            )
            if not chunk:
                break
            buffered.extend(chunk)
            if len(buffered) > _MODEL_DISCOVERY_OUTPUT_BYTES:
                raise BoundedProcessFailure(
                    "model discovery response exceeded its bound"
                )
            while b"\n" in buffered:
                line, _, remainder = buffered.partition(b"\n")
                buffered = bytearray(remainder)
                result = _json_rpc_result_of(line, request_id)
                if result is not None:
                    return result
    raise BoundedProcessFailure("model discovery did not answer in time")


def _json_rpc_result_of(
    line: bytes | bytearray, request_id: int
) -> dict[str, object] | None:
    """The result this line answers `request_id` with; any other message is passed over."""
    try:
        message = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Codex model discovery returned invalid JSON") from error
    if not isinstance(message, dict) or message.get("id") != request_id:
        return None
    result = message.get("result")
    if not isinstance(result, dict):
        raise TypeError("Codex model discovery returned no result")
    return result


def _terminate_inspection_process(
    process: subprocess.Popen[bytes], termination_grace_seconds: float
) -> None:
    try:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=termination_grace_seconds)
    except ProcessLookupError:
        process.wait(timeout=termination_grace_seconds)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=termination_grace_seconds)
    finally:
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()


def _log_unstartable_executors(settings: HostSettings) -> None:
    logger = logging.getLogger("atelier2")
    seen: set[str] = set()
    for refusal in (
        settings.claude_start_refusal,
        settings.claude_workspace_tools_start_refusal,
        settings.claude_atelier_doors_start_refusal,
        settings.grok_start_refusal,
        settings.grok_workspace_tools_start_refusal,
        settings.codex_start_refusal,
    ):
        if refusal is not None and refusal not in seen:
            seen.add(refusal)
            logger.warning(refusal)


class LegacyAgentOpenPrCompletionWithoutReceipt(RuntimeError):
    """A pre-reconciliation agent effect advanced its run without a receipt."""


def _refuse_legacy_agent_effect_runs_without_receipt(runtime: DbosRuntime) -> None:
    blocking = legacy_agent_effect_runs_without_receipt(runtime.engine)
    if blocking:
        named = ", ".join(run.value for run in blocking)
        raise LegacyAgentOpenPrCompletionWithoutReceipt(
            "refusing to serve live GitHub open-pr while these pre-reconciliation "
            "agent grants have advanced without an effect receipt: "
            f"{named}. Migrate or repair those runs before serving the connected "
            "project."
        )


def _project_source_connection(
    settings: HostSettings,
) -> ProjectSourceConnectionRevision | None:
    """The served project's connection record, read before the runtime exists.

    The effect adapter is a constructor answer to the runtime, so the record
    that composes it is read here through a short-lived engine on the same
    store -- the `connect` command's own pattern. A serve that binds no project,
    or a store that does not exist yet, has nothing connected; an unreadable or
    corrupt channel fails the start loudly rather than quietly composing the
    loopback adapter over a recorded connection.
    """

    if settings.project_id is None:
        return None
    database = settings.database_path
    if not database.is_file() or database.stat().st_size == 0:
        return None
    engine = create_canonical_engine(database)
    try:
        initialize_schema(engine)
        channel = DbosHostConfigurationChannel(engine)
        match get_project_source_connection(
            settings.project_id.value, channel, GitHubProjectSourceConnector()
        ):
            case ProjectSourceConnectionRead(revision):
                return revision
            case PlatformConnectionUnknown():
                return None
            case ReadUnavailable(detail):
                raise ValueError(
                    detail or "the project-source connection record could not be read"
                )
            case DurableStateCorrupt():
                raise ValueError("the project-source connection record is corrupt")
            case _ as unreachable:
                assert_never(unreachable)
    finally:
        engine.dispose()


def _effect_adapters(
    settings: HostSettings, connection: ProjectSourceConnectionRevision | None
) -> EffectAdapterFactory | EffectAdapterRegistry:
    """The effect adapters this instance drives.

    The live registry composes from the served project's source-connection
    record (`atelier2 connect`, ADR 0010 decision 2): the connected platform's
    own adapter package decodes the record's opaque source address and yields
    the factory, so no platform identifier surfaces here. An unconnected
    project keeps the loopback adapter exactly as before. The token, when the
    live adapter opens it, is read from the record's credential directory by
    reference and never returns here (ADR 0009 §6).

    A live adapter's non-authoritative not-found readback enters the durable
    reconciliation path. Only the pre-reconciliation completion shape remains
    a startup refusal until an explicit compatibility transition owns it.
    """

    adapter_revision = AdapterRevision(settings.effect_adapter_revision)
    destination = EffectDestination(settings.effect_destination)
    if connection is not None:
        if not is_loopback_host(settings.host):
            raise ValueError(
                f"serving the live GitHub open-pr effect requires a loopback "
                f"bind, not {settings.host!r}: starting a run is unauthenticated "
                "on this API, so the operator's GitHub token stays on this "
                "machine until an authenticated boundary exists"
            )
        return live_github_effect_registry(
            connection,
            settings.database_path.parent / CANDIDATE_STORE_DIRECTORY_NAME,
            adapter_revision,
            destination,
        )
    return LoopbackEffectAdapterFactory(
        settings.effect_store_path, adapter_revision, destination
    )


def _close_runtime_at_shutdown(
    runtime: DbosRuntime, inner: Lifespan[FastAPI] | None
) -> Lifespan[FastAPI]:
    """Close the runtime when the ASGI application's own lifespan shuts down.

    Uvicorn's `Server.serve()` restores each signal's original disposition
    and re-raises it on this process right after `Server.run()` returns
    (`capture_signals()` in `uvicorn.server`) -- so a stop signal kills the
    process before any of `serve()`'s own code past `.run()` executes, no
    matter how that call's own `try/finally` reads (issue #1117). The ASGI
    lifespan's shutdown event is awaited *inside* that same call, before the
    signal is re-raised, so it is the one hook a stop signal cannot outrun.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            if inner is None:
                yield
            else:
                async with inner(app):
                    yield
        finally:
            try:
                await asyncio.to_thread(runtime.close)
            except BaseException:
                logging.getLogger("atelier2").exception(
                    "atelier2 serve runtime failed to close"
                )
                raise
            logging.getLogger("atelier2").info("atelier2 serve runtime closed")

    return lifespan


def _seat_of(settings: HostSettings) -> ServedSeat | None:
    """This deployment's seat, read out of the settings that declare it."""

    return composed_seat(
        settings.terminal_seat,
        project_id=settings.project_id,
        project_root=settings.project_root,
        database_path=settings.database_path,
        service_url=_own_service_url(settings),
    )


def _serve_lifespan(
    runtime: DbosRuntime, seat: ServedSeat | None, close_runtime_at_shutdown: bool
) -> Lifespan[FastAPI] | None:
    """What this application opens and closes with itself: its seat, its lease.

    The seat's terminal server is innermost, so it is started after and
    stopped before the runtime this deployment leases -- a terminal answering
    a store that is already closing would be the one order that lies.
    """

    lifespan = None if seat is None else seat_lifespan(seat, None)
    if not close_runtime_at_shutdown:
        return lifespan
    return _close_runtime_at_shutdown(runtime, lifespan)


def compose_application(
    settings: HostSettings, *, close_runtime_at_shutdown: bool = False
) -> tuple[FastAPI, DbosRuntime]:
    """Build the served app and its runtime lease from one settings record.

    `close_runtime_at_shutdown` folds `runtime.close()` into the app's own
    ASGI lifespan shutdown; off by default, because most callers -- every
    test that composes an app, drives it through a `TestClient`, and keeps
    reading `runtime` afterwards -- own the runtime's lifetime themselves and
    close it on their own terms, independently of the app's. `serve()` is
    the one caller that turns it on, for its own served process's own
    runtime (issue #1117).
    """
    subscription_executors = _subscription_executor_registrations(settings)
    # Read once: the same connection record composes the effect adapter, the
    # queue sweep's own work-item reads, and the import door's tracker source.
    source_connection = _project_source_connection(settings)
    tracker_item_source = (
        None
        if source_connection is None
        else live_github_issue_source(source_connection)
    )
    runtime = DbosRuntime(
        settings.runtime_settings(),
        _effect_adapters(settings, source_connection),
        subscription_executors,
        tracker_item_source=tracker_item_source,
    )
    try:
        if source_connection is not None:
            _refuse_legacy_agent_effect_runs_without_receipt(runtime)
        # One expression feeds both the reader's bound and the API's own, so the
        # promise that they cannot describe different numbers holds by
        # construction rather than by two readings agreeing today.
        limits = settings.limits
        queries = DbosQueries(runtime.engine, durable_projection_limit(limits))
        seat = _seat_of(settings)
        lifespan = _serve_lifespan(runtime, seat, close_runtime_at_shutdown)
        artifact_store = DbosArtifactStore(runtime.engine)
        provider_models = HostProviderModelInspector(
            runtime.agent_executor_registry,
            settings.codex_subscription,
            settings.grok_subscription,
            settings.model_inspection_timeout_seconds,
            settings.agent_termination_grace_seconds,
        )
        app = create_app(
            source_commit=settings.source_commit,
            source_tree=settings.source_tree,
            lifespan=lifespan,
            ports=ApiPorts(
                workflow_revision_publisher=DbosWorkflowRevisionPublisher(
                    runtime.engine
                ),
                published_run_starter=DbosDurableRunStarter(
                    runtime.engine, runtime.settings, runtime.agent_executor_registry
                ),
                wait_answerer=DbosWaitAnswerer(
                    runtime.engine, runtime.settings.application_version
                ),
                reconcile_commander=DbosEffectReconcileCommander(
                    runtime.engine, runtime.settings
                ),
                workflow_revision_queries=queries,
                run_queries=queries,
                run_event_queries=queries,
                workflow_document_parser=parse_workflow_document,
                agent_definition_parser=parse_agent_definition,
                agent_definition_renderer=render_agent_definition,
                agent_configuration_catalog=DbosAgentConfigurationCatalog(
                    runtime.engine, runtime.agent_executor_registry
                ),
                agent_attempt_canceller=DbosAgentAttemptStore(
                    runtime.engine, runtime.settings.application_version
                ),
                catalog_resolver=DbosCatalogStore(runtime.engine),
                catalog_admissions=DbosCatalogStore(runtime.engine),
                library_additions=DbosCatalogStore(runtime.engine),
                catalog_intakes=DbosCatalogStore(runtime.engine),
                published_revision_registry=DbosCatalogStore(runtime.engine),
                published_revision_resolver_sessions=DbosCatalogStore(runtime.engine),
                published_revision_listing=DbosCatalogStore(runtime.engine),
                artifact_publisher=artifact_store,
                artifact_reader=artifact_store,
                host_configuration_channel=DbosHostConfigurationChannel(runtime.engine),
                project_source_connection_channel=DbosHostConfigurationChannel(
                    runtime.engine
                ),
                project_source_connector=GitHubProjectSourceConnector(),
                project_source_credential_store=FilesystemProjectSourceCredentialStore(
                    settings.database_path.parent
                    / MANAGED_PROJECT_SOURCE_CREDENTIALS_DIRECTORY
                ),
                queue_projection=DbosQueueProjectionStore(runtime.engine),
                request_queue_sweep=runtime.request_queue_sweep,
                tracker_item_source=tracker_item_source,
                model_registry_discoverer=provider_models,
                model_registry_validator=provider_models,
                redeploy_status_reader=filesystem_redeploy_status_reader(
                    redeploy_status_path(settings.database_path)
                ),
            ),
            limits=limits,
            event_poll_backoff=settings.event_poll_backoff,
            frontend_dist=settings.frontend_dist,
            served_project_id=settings.project_id,
            seat_reader=no_seat_declared if seat is None else seat.reading,
        )
        runtime.launch()
        return app, runtime
    except BaseException:
        runtime.close()
        raise


# The Workbench holds `GET /atelier/api/v1/events` (server-sent) open for as
# long as it is on screen, and that stream never ends on its own. Without a
# bound, uvicorn's graceful shutdown waits it out, so a redeploy with an open
# tab rides `systemctl --user stop` all the way to the live unit's
# TimeoutStopSec (90s, systemd's default; the unit sets none -- see
# docs/OPERATIONS.md's serve/redeploy section) before SIGKILL (issue #1117).
# This is the grace an open connection gets before uvicorn drops its sockets
# anyway, kept well under TimeoutStopSec so a stop always finishes on its own.
# 10s is also long enough for the longest legitimate in-flight request this
# process serves: the project-source connect POST, which reaches out to a
# remote (GitHub) before it can answer. Cutting that connect mid-flight is
# acceptable because the redeploy that triggers this grace already checked
# for running runs before it started, and a cut connect is simply retried by
# the operator.
SERVE_SHUTDOWN_CONNECTION_GRACE_SECONDS = 10


def serve(settings: HostSettings) -> None:
    configure_process_logging()
    _log_unstartable_executors(settings)
    app, runtime = compose_application(settings, close_runtime_at_shutdown=True)
    try:
        uvicorn.Server(
            uvicorn.Config(
                app,
                host=settings.host,
                port=settings.port,
                log_config=None,
                access_log=False,
                timeout_graceful_shutdown=SERVE_SHUTDOWN_CONNECTION_GRACE_SECONDS,
            )
        ).run()
    finally:
        # A stop that reaches this point at all -- an exception before the
        # ASGI lifespan ever started, most likely -- has not already closed
        # the runtime through it, and close() is idempotent, so this is a
        # safety net rather than the primary path. The primary path is
        # `_close_runtime_at_shutdown`, composed into `app` above through
        # `close_runtime_at_shutdown=True`: uvicorn re-raises whatever signal
        # it caught right after `Server.run()` returns, with that signal's
        # default disposition restored (`capture_signals()` in
        # `uvicorn.server`), which ends this process before any of *this*
        # function's own code past `.run()` executes.
        try:
            runtime.close()
        except BaseException:
            logging.getLogger("atelier2").exception(
                "atelier2 serve runtime failed to close"
            )
            raise
