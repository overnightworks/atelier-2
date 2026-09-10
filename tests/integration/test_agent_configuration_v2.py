from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Never, cast

import pytest
import sqlalchemy as sa
from dbos import DBOSClient
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response

import atelier2.adapters.dbos.runtime as dbos_runtime
from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.names import NODE_WORKFLOW_NAME, QUEUE_NAME
from atelier2.adapters.dbos.node_binding_codec import (
    decode_node_binding,
    encode_node_binding,
)
from atelier2.adapters.dbos.run_store import DbosWaitAnswerer
from atelier2.adapters.dbos.runtime import (
    DbosRuntime,
    DbosRuntimeBindingConflict,
    DbosRuntimeSettings,
)
from atelier2.adapters.dbos.schema import (
    agent_attempts,
    agent_receipts_v2,
    host_model_registry_entries,
    run_agent_bindings,
    run_events,
    runs,
)
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.adapters.dbos.workflow import _node_binding
from atelier2.adapters.dbos.workflow_ids import node_workflow_id_for
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.api.app import create_app
from atelier2.api.openapi import API_PREFIX
from atelier2.api.references import encode_public_run_reference
from atelier2.application.bind_node import agent_execution_request_v2
from atelier2.application.cancel_agent_attempt import (
    continue_agent_attempt_cancellation,
)
from atelier2.contracts.agent_attempts import (
    TERMINAL_AGENT_ATTEMPT_STATES,
    AgentAttemptCancellationDisposition,
    AgentAttemptFailureCode,
    AgentAttemptReplacement,
    AgentAttemptState,
    AgentProcessOwnerId,
    CancelAgentAttemptRequest,
    WatchdogGenerationId,
)
from atelier2.contracts.agents import (
    MAXIMUM_AGENT_PROCESS_INPUT_BYTES,
    AgentBinding,
    AgentBindingSet,
    AgentConfigurationNotStartableReason,
    AgentConfigurationRevision,
    AgentConfigurationRevisionFormatVersion,
    AgentConfigurationRevisionHash,
    AgentExecutionCapability,
    AgentExecutionRequestV2,
    AgentExecutionResult,
    AgentExecutorOperationalIdentity,
    AgentExecutorRevision,
    AgentRole,
    AuthMode,
    AuthProfileRevision,
    ProviderId,
    ResolvedAgentBinding,
)
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.executions import (
    AgentExecutionRefusal,
    NodeExecutionId,
    SubmitWaitAnswerRequest,
    WaitAnswerActor,
)
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.node_bindings import AgentNodeBindingV2
from atelier2.contracts.provider_probe_receipts import (
    ProviderProbeProblemCode,
    ProviderProbeReceipt,
    ProviderProbeResult,
    ProviderProbeVectorId,
)
from atelier2.contracts.run_bindings import (
    AnyBoundRun,
    RunBindingConflict,
    RunV2,
    RunV3,
)
from atelier2.contracts.run_projections import (
    RunPage,
)
from atelier2.contracts.runs import (
    RunId,
    RunState,
    WorkflowRevision,
    WorkflowRevisionHash,
)
from atelier2.contracts.when import RecordedAt
from atelier2.ports.agent_attempts import (
    AgentAttemptCancellationAccepted,
    AgentAttemptCancellationStale,
    AgentAttemptClaimedByThisCall,
    AgentAttemptFailed,
    AgentAttemptSucceeded,
    AgentExecutorBindingRefusalFenced,
    AgentExecutorBindingRefusalNeedsPreparedCleanup,
    AgentExecutorBindingRefusalWritten,
)
from atelier2.ports.agent_configurations import (
    AgentConfigurationRevisionCreated,
    AgentConfigurationRevisionPage,
    AuthProfileRevisionCreated,
    AuthProfileRevisionPage,
)
from atelier2.ports.agent_executions import (
    AgentExecutorKey,
    AgentExecutorRegistration,
    AgentExecutorRegistry,
    ProviderProbeReceiptGate,
)
from atelier2.ports.durable_runs import (
    DurableAgentExecutorBindingUnavailable,
    DurableAgentExecutorCapabilityUnavailable,
    DurableAnswerCreated,
    DurableRunCreated,
    StartPublishedRunRequestV2,
)
from atelier2.ports.published_revisions import (
    PublishedRevisionCreated,
    PublishedRevisionExisting,
)
from atelier2.ports.run_queries import (
    RunFound,
)
from tests.integration.test_agent_attempts import attempt_request, attempt_runtime
from tests.scenarios.agents import (
    RecordingAgentExecutorFactoryV2,
    agent_attempt_execution,
    agent_scratch_root,
    publish_checked_model_registry,
)
from tests.scenarios.api import (
    api_limits,
    durable_ports,
    durable_queries,
    event_poll_backoff,
)
from tests.scenarios.run_waiting import wait_for_run_state
from tests.scenarios.workflows import ANY_JSON_SCHEMA, declared_output

_DOCUMENT = (
    b"""format_version: 3
name: Builder then reviewer
nodes:
  - id: build
    type: agent
    role: builder
    mode: headless
    instruction: build
"""
    + declared_output()
    + b"""  - id: review
    type: agent
    role: reviewer
    mode: headless
    instruction: review
    depends_on: [build]
"""
    + declared_output()
)

_V3_DOCUMENT = b"""format_version: 3
name: One agent
nodes:
  - id: build
    type: agent
    role: builder
    mode: headless
    instruction: Build the one thing.
""" + declared_output()
"""The thinnest executable format-3 line: one agent whose one output is any JSON.

Every scenario below that needs a run to exist wants the smallest run there is.
What each of them is about -- a refusal, a cleaned attempt, a channel that must
stay free of provider material -- is not the document, so the document says as
little as an executable one can.
"""


def _effect_factory(root: Path) -> LoopbackEffectAdapterFactory:
    return LoopbackEffectAdapterFactory(
        root / "effects.sqlite",
        AdapterRevision("loopback-v1"),
        EffectDestination("test"),
    )


def _runtime(
    root: Path,
    factories: tuple[RecordingAgentExecutorFactoryV2 | AgentExecutorRegistration, ...],
) -> DbosRuntime:
    return DbosRuntime(
        DbosRuntimeSettings(
            root / "atelier.sqlite",
            "v2-test",
            agent_scratch_root=agent_scratch_root(root),
        ),
        _effect_factory(root),
        factories,
    )


def _publish_output_schema(runtime: DbosRuntime) -> None:
    """Publish the schema `_V3_DOCUMENT` pins, without which it cannot execute."""
    published = DbosCatalogStore(runtime.engine).publish_revision(ANY_JSON_SCHEMA)
    assert isinstance(
        published, (PublishedRevisionCreated, PublishedRevisionExisting)
    ), published


def _api_client(runtime: DbosRuntime) -> TestClient:
    return TestClient(
        create_app(
            source_commit="commit",
            source_tree="tree",
            ports=durable_ports(
                runtime.engine, runtime.settings, runtime.agent_executor_registry
            ),
            limits=api_limits(),
            event_poll_backoff=event_poll_backoff(),
        )
    )


def _publish_matrix(
    runtime: DbosRuntime,
) -> tuple[WorkflowRevision, AgentBindingSet]:
    catalog = DbosAgentConfigurationCatalog(
        runtime.engine, runtime.agent_executor_registry
    )
    bindings = []
    for role, profile, provider, model, revision in (
        ("builder", "max", "anthropic", "opus", "claude-cli/v1"),
        ("reviewer", "chatgpt", "openai", "gpt-5.6", "codex-cli/v1"),
    ):
        auth = AuthProfileRevision(
            profile, 1, ProviderId(provider), AuthMode.SUBSCRIPTION
        )
        assert isinstance(
            catalog.publish_auth_profile_revision(auth), AuthProfileRevisionCreated
        )
        configuration = AgentConfigurationRevision(
            model,
            auth.revision_hash,
            AgentExecutorRevision(revision),
            AgentExecutionCapability.HEADLESS,
            AgentConfigurationRevisionFormatVersion.V2,
        )
        assert isinstance(
            catalog.publish_agent_configuration_revision(configuration),
            AgentConfigurationRevisionCreated,
        )
        publish_checked_model_registry(
            runtime.engine, ProviderId(provider), (configuration,)
        )
        bindings.append(AgentBinding(AgentRole(role), configuration.revision_hash))
    _publish_output_schema(runtime)
    workflow = WorkflowRevision(_DOCUMENT)
    DbosWorkflowRevisionPublisher(runtime.engine).publish(workflow)
    return workflow, AgentBindingSet(tuple(bindings))


def _publish_single_capability(
    runtime: DbosRuntime,
    capability: AgentExecutionCapability,
    catalog_registry: AgentExecutorRegistry | None = None,
    document: bytes | None = None,
) -> tuple[WorkflowRevision, AgentBindingSet]:
    catalog = DbosAgentConfigurationCatalog(
        runtime.engine,
        runtime.agent_executor_registry
        if catalog_registry is None
        else catalog_registry,
    )
    auth = AuthProfileRevision("max", 1, ProviderId("anthropic"), AuthMode.SUBSCRIPTION)
    assert isinstance(
        catalog.publish_auth_profile_revision(auth), AuthProfileRevisionCreated
    )
    configuration = AgentConfigurationRevision(
        "opus",
        auth.revision_hash,
        AgentExecutorRevision("claude-cli/v1"),
        capability,
        AgentConfigurationRevisionFormatVersion.V2,
    )
    assert isinstance(
        catalog.publish_agent_configuration_revision(configuration),
        AgentConfigurationRevisionCreated,
    )
    publish_checked_model_registry(
        runtime.engine, ProviderId("anthropic"), (configuration,)
    )
    workflow = WorkflowRevision(_V3_DOCUMENT if document is None else document)
    _publish_output_schema(runtime)
    DbosWorkflowRevisionPublisher(runtime.engine).publish(workflow)
    return workflow, AgentBindingSet(
        (AgentBinding(AgentRole("builder"), configuration.revision_hash),)
    )


def _encoded_binding(
    configuration: AgentConfigurationRevision,
    auth: AuthProfileRevision,
    *,
    include_contract: bool,
    job: str = "build",
) -> dict[str, object]:
    encoded = dict(
        encode_node_binding(
            AgentNodeBindingV2(
                ResolvedAgentBinding(AgentRole("builder"), configuration, auth), job
            )
        )
    )
    if not include_contract:
        del encoded["revision_format_version"]
        del encoded["requested_capability"]
    return encoded


def _replayed_request(
    encoded: dict[str, object],
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    node_id: str,
    operational_identity: AgentExecutorOperationalIdentity,
    declared_capabilities: frozenset[AgentExecutionCapability],
) -> AgentExecutionRequestV2:
    """The request a recovered node makes of a recorded row, the way the node does."""
    binding = decode_node_binding(encoded)
    assert isinstance(binding, AgentNodeBindingV2)
    return agent_execution_request_v2(
        binding,
        run_id,
        revision_hash,
        node_id,
        operational_identity,
        declared_capabilities,
    )


def test_old_node_binding_payload_replays_and_new_payload_carries_contract() -> None:
    auth = AuthProfileRevision("max", 1, ProviderId("anthropic"), AuthMode.SUBSCRIPTION)
    legacy = AgentConfigurationRevision(
        "opus",
        auth.revision_hash,
        AgentExecutorRevision("claude-cli/v1"),
        AgentExecutionCapability.HEADLESS,
        AgentConfigurationRevisionFormatVersion.V1,
    )
    current = AgentConfigurationRevision(
        "opus",
        auth.revision_hash,
        AgentExecutorRevision("claude-cli/v1"),
        AgentExecutionCapability.HEADLESS,
        AgentConfigurationRevisionFormatVersion.V2,
    )
    run_id = RunId("capability/replay")
    revision_hash = WorkflowRevision(
        b"format_version: 1\nstart: x\nnodes: []\n"
    ).revision_hash
    operational_identity = AgentExecutorOperationalIdentity("executor/test")

    replayed = _replayed_request(
        _encoded_binding(legacy, auth, include_contract=False),
        run_id,
        revision_hash,
        "build",
        operational_identity,
        frozenset({AgentExecutionCapability.HEADLESS}),
    )
    newly_encoded = _replayed_request(
        _encoded_binding(current, auth, include_contract=True),
        run_id,
        revision_hash,
        "build",
        operational_identity,
        frozenset({AgentExecutionCapability.HEADLESS}),
    )

    assert replayed.resolved_binding.configuration == legacy
    assert newly_encoded.resolved_binding.configuration == current
    assert replayed.declared_output_schema_bytes is None
    assert newly_encoded.declared_output_schema_bytes is None


@pytest.mark.proves("every-round-of-a-loop-is-its-own-durable-execution")
def test_a_recorded_binding_that_names_no_round_is_refused_by_name() -> None:
    """Which execution a recovered node is may not be guessed back into place.

    A binding without a round is a binding this build never wrote, and reading
    it as the first round would silently make a later round answer for the
    first. The refusal says so instead of filling the gap in.
    """
    auth = AuthProfileRevision("max", 1, ProviderId("anthropic"), AuthMode.SUBSCRIPTION)
    configuration = AgentConfigurationRevision(
        "opus",
        auth.revision_hash,
        AgentExecutorRevision("claude-cli/v1"),
        AgentExecutionCapability.HEADLESS,
        AgentConfigurationRevisionFormatVersion.V2,
    )
    roundless = _encoded_binding(configuration, auth, include_contract=True)
    del roundless["round_ordinal"]

    with pytest.raises(RunBindingConflict) as refused:
        decode_node_binding(roundless)

    assert "missing a key" in str(refused.value)


def test_a_declared_schema_document_reaches_the_request_as_its_published_bytes() -> (
    None
):
    auth = AuthProfileRevision("max", 1, ProviderId("anthropic"), AuthMode.SUBSCRIPTION)
    configuration = AgentConfigurationRevision(
        "opus",
        auth.revision_hash,
        AgentExecutorRevision("claude-cli/v1"),
        AgentExecutionCapability.HEADLESS,
        AgentConfigurationRevisionFormatVersion.V2,
    )
    encoded = dict(_encoded_binding(configuration, auth, include_contract=True))
    encoded["output_schema_document"] = '{"type": "string"}'

    request = _replayed_request(
        encoded,
        RunId("schema/declared"),
        WorkflowRevision(b"format_version: 1\nstart: x\nnodes: []\n").revision_hash,
        "build",
        AgentExecutorOperationalIdentity("executor/test"),
        frozenset({AgentExecutionCapability.HEADLESS}),
    )
    without_schema = _replayed_request(
        _encoded_binding(configuration, auth, include_contract=True),
        RunId("schema/declared"),
        WorkflowRevision(b"format_version: 1\nstart: x\nnodes: []\n").revision_hash,
        "build",
        AgentExecutorOperationalIdentity("executor/test"),
        frozenset({AgentExecutionCapability.HEADLESS}),
    )

    assert request.declared_output_schema_bytes == b'{"type": "string"}'
    assert request.request_hash == without_schema.request_hash


def test_a_non_text_schema_document_on_the_binding_is_a_named_conflict() -> None:
    auth = AuthProfileRevision("max", 1, ProviderId("anthropic"), AuthMode.SUBSCRIPTION)
    configuration = AgentConfigurationRevision(
        "opus",
        auth.revision_hash,
        AgentExecutorRevision("claude-cli/v1"),
        AgentExecutionCapability.HEADLESS,
        AgentConfigurationRevisionFormatVersion.V2,
    )
    encoded = dict(_encoded_binding(configuration, auth, include_contract=True))
    encoded["output_schema_document"] = {"type": "string"}

    with pytest.raises(RunBindingConflict, match="output schema document"):
        _replayed_request(
            encoded,
            RunId("schema/wrong-type"),
            WorkflowRevision(b"format_version: 1\nstart: x\nnodes: []\n").revision_hash,
            "build",
            AgentExecutorOperationalIdentity("executor/test"),
            frozenset({AgentExecutionCapability.HEADLESS}),
        )


@pytest.mark.parametrize(
    ("revision_format_version", "requested_capability"),
    (
        (1, "interactive"),
        (1, None),
        (None, "headless"),
        (3, "headless"),
        (True, "headless"),
        (2.0, "headless"),
        (2, 1),
    ),
)
def test_node_binding_payload_refuses_partial_or_invalid_capability_contract(
    revision_format_version: object | None,
    requested_capability: object | None,
) -> None:
    auth = AuthProfileRevision("max", 1, ProviderId("anthropic"), AuthMode.SUBSCRIPTION)
    current = AgentConfigurationRevision(
        "opus",
        auth.revision_hash,
        AgentExecutorRevision("claude-cli/v1"),
        AgentExecutionCapability.HEADLESS,
        AgentConfigurationRevisionFormatVersion.V2,
    )
    encoded = dict(_encoded_binding(current, auth, include_contract=False))
    if revision_format_version is not None:
        encoded["revision_format_version"] = revision_format_version
    if requested_capability is not None:
        encoded["requested_capability"] = requested_capability

    with pytest.raises(RunBindingConflict):
        _replayed_request(
            encoded,
            RunId("capability/invalid-replay"),
            WorkflowRevision(b"format_version: 1\nstart: x\nnodes: []\n").revision_hash,
            "build",
            AgentExecutorOperationalIdentity("executor/test"),
            frozenset(
                {
                    AgentExecutionCapability.HEADLESS,
                    AgentExecutionCapability.INTERACTIVE,
                }
            ),
        )


@pytest.mark.parametrize(
    "encoded_contract",
    (
        {"revision_format_version": None, "requested_capability": None},
        {"revision_format_version": None, "requested_capability": "headless"},
        {"revision_format_version": 2, "requested_capability": None},
    ),
)
def test_node_binding_payload_refuses_explicit_null_contract_values(
    encoded_contract: dict[str, object],
) -> None:
    auth = AuthProfileRevision("max", 1, ProviderId("anthropic"), AuthMode.SUBSCRIPTION)
    configuration = AgentConfigurationRevision(
        "opus",
        auth.revision_hash,
        AgentExecutorRevision("claude-cli/v1"),
        AgentExecutionCapability.HEADLESS,
        AgentConfigurationRevisionFormatVersion.V2,
    )
    encoded = dict(_encoded_binding(configuration, auth, include_contract=False))
    encoded.update(encoded_contract)

    with pytest.raises(RunBindingConflict, match="wrong type"):
        _replayed_request(
            encoded,
            RunId("capability/null-replay"),
            WorkflowRevision(b"format_version: 1\nstart: x\nnodes: []\n").revision_hash,
            "build",
            AgentExecutorOperationalIdentity("executor/test"),
            frozenset(
                {
                    AgentExecutionCapability.HEADLESS,
                    AgentExecutionCapability.INTERACTIVE,
                }
            ),
        )


def test_live_request_refuses_capability_missing_from_the_consuming_host() -> None:
    auth = AuthProfileRevision("max", 1, ProviderId("anthropic"), AuthMode.SUBSCRIPTION)
    configuration = AgentConfigurationRevision(
        "opus",
        auth.revision_hash,
        AgentExecutorRevision("claude-cli/v1"),
        AgentExecutionCapability.INTERACTIVE,
        AgentConfigurationRevisionFormatVersion.V2,
    )

    with pytest.raises(RunBindingConflict, match="runtime executor lacks"):
        _replayed_request(
            _encoded_binding(configuration, auth, include_contract=True),
            RunId("capability/host-mismatch"),
            WorkflowRevision(b"format_version: 1\nstart: x\nnodes: []\n").revision_hash,
            "build",
            AgentExecutorOperationalIdentity("executor/headless-only"),
            frozenset({AgentExecutionCapability.HEADLESS}),
        )


def test_an_oversized_job_is_a_named_binding_conflict() -> None:
    auth = AuthProfileRevision("max", 1, ProviderId("anthropic"), AuthMode.SUBSCRIPTION)
    configuration = AgentConfigurationRevision(
        "opus",
        auth.revision_hash,
        AgentExecutorRevision("claude-cli/v1"),
        AgentExecutionCapability.HEADLESS,
        AgentConfigurationRevisionFormatVersion.V2,
    )
    encoded = dict(_encoded_binding(configuration, auth, include_contract=True))
    encoded["job"] = "x" * (MAXIMUM_AGENT_PROCESS_INPUT_BYTES + 1)

    with pytest.raises(RunBindingConflict) as raised:
        _replayed_request(
            encoded,
            RunId("job/oversized"),
            WorkflowRevision(b"format_version: 1\nstart: x\nnodes: []\n").revision_hash,
            "build",
            AgentExecutorOperationalIdentity("executor/test"),
            frozenset({AgentExecutionCapability.HEADLESS}),
        )
    assert isinstance(raised.value.__cause__, ValueError)
    assert str(MAXIMUM_AGENT_PROCESS_INPUT_BYTES) in str(raised.value.__cause__)


def test_start_refuses_unattested_capability_before_enqueue_or_provider_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = RecordingAgentExecutorFactoryV2(
        "anthropic", "claude-cli/v1", "headless-only", b"unused"
    )
    runtime = _runtime(tmp_path, (factory,))
    runtime.initialize_storage()
    workflow, bindings = _publish_single_capability(
        runtime, AgentExecutionCapability.INTERACTIVE
    )

    def unexpected_enqueue(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("unattested capability reached the durable queue")

    monkeypatch.setattr(DBOSClient, "enqueue_in_transaction", unexpected_enqueue)
    result = DbosDurableRunStarter(
        runtime.engine,
        runtime.settings,
        runtime.agent_executor_registry,
    ).start_published(
        StartPublishedRunRequestV2(
            RunId("capability/refused"), workflow.revision_hash, bindings
        )
    )

    assert isinstance(result, DurableAgentExecutorCapabilityUnavailable)
    assert factory.opened is not None
    assert factory.opened.requests == []
    with runtime.engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(runs)) == 0
        assert (
            connection.scalar(
                sa.select(sa.func.count()).select_from(run_agent_bindings)
            )
            == 0
        )
    runtime.close()


def test_v2_start_against_an_empty_registry_creates_no_durable_run_or_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden_process_authority() -> Never:
        raise AssertionError("empty registry resolved process authority")

    monkeypatch.setattr(
        dbos_runtime, "delegated_cgroup_root", forbidden_process_authority
    )
    runtime = _runtime(tmp_path, ())
    try:
        runtime.initialize_storage()
        binding = runtime.settings.binding(
            runtime.agent_executor_registry.manifest,
            (runtime.effect_adapter_binding,),
        )
        assert binding.agent_process_control_root is None
        assert binding.agent_process_cgroup_root is None
        assert binding.agent_scratch_root is None
        assert binding.agent_termination_grace_seconds is None
        publication_factory = RecordingAgentExecutorFactoryV2(
            "anthropic", "claude-cli/v1", "publication", b"unused"
        )
        workflow, bindings = _publish_single_capability(
            runtime,
            AgentExecutionCapability.HEADLESS,
            AgentExecutorRegistry((publication_factory,)),
        )
        result = DbosDurableRunStarter(
            runtime.engine,
            runtime.settings,
            runtime.agent_executor_registry,
        ).start_published(
            StartPublishedRunRequestV2(
                RunId("empty-registry/refused"), workflow.revision_hash, bindings
            )
        )

        assert isinstance(result, DurableAgentExecutorBindingUnavailable)
        assert publication_factory.opens == 0
        with runtime.engine.connect() as connection:
            assert connection.scalar(sa.select(sa.func.count()).select_from(runs)) == 0
            assert (
                connection.scalar(
                    sa.select(sa.func.count()).select_from(run_agent_bindings)
                )
                == 0
            )
            assert (
                connection.scalar(
                    sa.select(sa.func.count()).select_from(agent_attempts)
                )
                == 0
            )
    finally:
        runtime.close()


def test_nonterminal_v2_restart_with_an_empty_registry_refuses_before_cgroup_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded_factory = RecordingAgentExecutorFactoryV2(
        "anthropic", "claude-cli/v1", "seed", b"unused"
    )
    seeded = _runtime(tmp_path, (seeded_factory,))
    seeded.initialize_storage()
    workflow, bindings = _publish_single_capability(
        seeded, AgentExecutionCapability.HEADLESS
    )
    result = DbosDurableRunStarter(
        seeded.engine,
        seeded.settings,
        seeded.agent_executor_registry,
    ).start_published(
        StartPublishedRunRequestV2(
            RunId("empty-registry/restart"), workflow.revision_hash, bindings
        )
    )
    assert isinstance(result, DurableRunCreated)
    seeded.close()

    def forbidden_process_authority() -> Never:
        raise AssertionError("missing durable executor reached cgroup access")

    monkeypatch.setattr(
        dbos_runtime, "delegated_cgroup_root", forbidden_process_authority
    )
    with pytest.raises(
        DbosRuntimeBindingConflict, match="nonterminal durable executor"
    ):
        _runtime(tmp_path, ())


def test_nonterminal_v3_restart_with_an_empty_registry_refuses_before_cgroup_or_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = _runtime(
        tmp_path,
        (RecordingAgentExecutorFactoryV2("anthropic", "claude-cli/v1", "seed", b""),),
    )
    run_id = RunId("empty-registry/v3-restart")
    try:
        seeded.initialize_storage()
        _publish_output_schema(seeded)
        workflow, bindings = _publish_single_capability(
            seeded, AgentExecutionCapability.HEADLESS, document=_V3_DOCUMENT
        )
        started = DbosDurableRunStarter(
            seeded.engine,
            seeded.settings,
            seeded.agent_executor_registry,
        ).start_published(
            StartPublishedRunRequestV2(run_id, workflow.revision_hash, bindings)
        )
        assert isinstance(started, DurableRunCreated)
        with seeded.engine.connect() as connection:
            before = tuple(
                connection.scalar(sa.select(sa.func.count()).select_from(table))
                for table in (runs, run_agent_bindings, agent_attempts)
            )
    finally:
        seeded.close()

    def forbidden_process_authority(*_args: object) -> Never:
        raise AssertionError("missing durable executor reached process authority")

    monkeypatch.setattr(
        dbos_runtime, "delegated_cgroup_root", forbidden_process_authority
    )
    monkeypatch.setattr(
        LoopbackEffectAdapterFactory, "open", forbidden_process_authority
    )

    with pytest.raises(
        DbosRuntimeBindingConflict, match="nonterminal durable executor"
    ):
        _runtime(tmp_path, ())

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'atelier.sqlite'}")
    try:
        with engine.connect() as connection:
            after = tuple(
                connection.scalar(sa.select(sa.func.count()).select_from(table))
                for table in (runs, run_agent_bindings, agent_attempts)
            )
    finally:
        engine.dispose()
    assert after == before


def test_restart_refuses_unattested_nonterminal_capability_before_factory_open(
    tmp_path: Path,
) -> None:
    supported = RecordingAgentExecutorFactoryV2(
        "anthropic",
        "claude-cli/v1",
        "tools-seed",
        b"unused",
        capability_set=frozenset(
            {
                AgentExecutionCapability.HEADLESS,
                AgentExecutionCapability.HEADLESS_WITH_TOOLS,
            }
        ),
    )
    seeded = _runtime(tmp_path, (supported,))
    seeded.initialize_storage()
    workflow, bindings = _publish_single_capability(
        seeded,
        AgentExecutionCapability.HEADLESS_WITH_TOOLS,
        document=_V3_DOCUMENT.replace(b"mode: headless", b"mode: headless_with_tools"),
    )
    started = DbosDurableRunStarter(
        seeded.engine,
        seeded.settings,
        seeded.agent_executor_registry,
    ).start_published(
        StartPublishedRunRequestV2(
            RunId("capability/restart"), workflow.revision_hash, bindings
        )
    )
    assert isinstance(started, DurableRunCreated)
    assert supported.opened is not None
    assert supported.opened.requests == []
    seeded.close()

    headless_only = RecordingAgentExecutorFactoryV2(
        "anthropic", "claude-cli/v1", "headless-restart", b"unused"
    )
    with pytest.raises(DbosRuntimeBindingConflict, match="durable capability"):
        _runtime(tmp_path, (headless_only,))

    assert headless_only.opens == 0


def _run_at_state(runtime: DbosRuntime, run_id: RunId, state: RunState) -> AnyBoundRun:
    """Load the typed durable run after the shared state wait completes."""
    wait_for_run_state(runtime.engine, run_id, state)
    result = durable_queries(runtime.engine).get_run(run_id)
    assert isinstance(result, RunFound)
    assert result.projection.run.state is state
    assert isinstance(result.projection.run, (RunV2, RunV3))
    return result.projection.run


def _wait_completed(runtime: DbosRuntime, run_id: RunId) -> AnyBoundRun:
    return _run_at_state(runtime, run_id, RunState.COMPLETED)


def _wait_failed(runtime: DbosRuntime, run_id: RunId) -> AnyBoundRun:
    return _run_at_state(runtime, run_id, RunState.FAILED)


def test_registry_startability_is_one_declared_factory_and_capability_decision() -> (
    None
):
    startable = RecordingAgentExecutorFactoryV2(
        "anthropic", "claude-cli/v1", "startable", b"ok"
    )
    unavailable = RecordingAgentExecutorFactoryV2(
        "anthropic", "claude-cli-unstartable/v1", "unavailable", b"must-not-run"
    )
    registry = AgentExecutorRegistry(
        (startable, AgentExecutorRegistration.unavailable(unavailable))
    )
    startable_key = startable.key
    unavailable_key = unavailable.key
    missing_key = AgentExecutorKey(
        ProviderId("missing"), AgentExecutorRevision("missing/v1")
    )
    # This registry is built with no receipt gate wired, so `is_startable`
    # keeps its factory-and-capability decision alone
    # (`test_is_startable_reads_the_receipt_state` below owns the armed
    # registry's receipt behaviour); this hash is genuinely never consulted
    # here, not a placeholder standing in for one that should be.
    unconsulted_configuration_hash = AgentConfigurationRevisionHash("0" * 64)

    assert registry.contains(startable_key)
    assert registry.contains(unavailable_key)
    assert not registry.contains(missing_key)
    assert registry.is_startable(
        startable_key, AgentExecutionCapability.HEADLESS, unconsulted_configuration_hash
    )
    assert not registry.is_startable(
        startable_key,
        AgentExecutionCapability.HEADLESS_WITH_TOOLS,
        unconsulted_configuration_hash,
    )
    assert not registry.is_startable(
        unavailable_key,
        AgentExecutionCapability.HEADLESS,
        unconsulted_configuration_hash,
    )
    assert not registry.is_startable(
        missing_key, AgentExecutionCapability.HEADLESS, unconsulted_configuration_hash
    )
    assert startable.opens == 0
    assert unavailable.opens == 0


_PROBE_CONFIGURATION_HASH = AgentConfigurationRevisionHash("e" * 64)
_PROBE_DEPLOYMENT_SOURCE_COMMIT = "a" * 40
_PROBE_FOREIGN_SOURCE_COMMIT = "b" * 40
_PROBE_DEPLOYMENT_DIGEST = Sha256Hash("1" * 64)
_PROBE_FOREIGN_DIGEST = Sha256Hash("2" * 64)


@dataclass
class _FakeProviderProbeReceiptReads:
    """A fixed-answer stand-in for the filesystem receipt store."""

    receipt: ProviderProbeReceipt | None

    def receipt_for(
        self, configuration_hash: AgentConfigurationRevisionHash
    ) -> ProviderProbeReceipt | None:
        if (
            self.receipt is None
            or self.receipt.configuration_hash != configuration_hash
        ):
            return None
        return self.receipt


def _probe_receipt(
    *,
    configuration_hash: AgentConfigurationRevisionHash = _PROBE_CONFIGURATION_HASH,
    source_commit: str = _PROBE_DEPLOYMENT_SOURCE_COMMIT,
    provider_layer_digest: Sha256Hash = _PROBE_DEPLOYMENT_DIGEST,
    valid_until: str = "2026-01-02T00:00:00Z",
    result: ProviderProbeResult = ProviderProbeResult.SUCCEEDED,
) -> ProviderProbeReceipt:
    return ProviderProbeReceipt(
        ProviderProbeVectorId("headless-fixture"),
        configuration_hash,
        WorkflowRevisionHash("f" * 64),
        provider_layer_digest,
        source_commit,
        RecordedAt("2026-01-01T00:00:00Z"),
        RecordedAt(valid_until),
        result,
        RunId("provider-canary/fixture"),
        terminal_hash=(
            Sha256Hash("c" * 64) if result is ProviderProbeResult.SUCCEEDED else None
        ),
        problem_code=(
            None
            if result is ProviderProbeResult.SUCCEEDED
            else ProviderProbeProblemCode("run-failed")
        ),
    )


@pytest.mark.parametrize(
    ("receipt", "expected"),
    [
        pytest.param(None, False, id="missing_receipt"),
        pytest.param(
            _probe_receipt(result=ProviderProbeResult.FAILED), False, id="red_receipt"
        ),
        pytest.param(
            _probe_receipt(valid_until="2026-01-01T06:00:00Z"),
            False,
            id="expired_receipt",
        ),
        pytest.param(
            _probe_receipt(source_commit=_PROBE_FOREIGN_SOURCE_COMMIT),
            True,
            id="foreign_source_commit_same_provider_layer",
        ),
        pytest.param(
            _probe_receipt(provider_layer_digest=_PROBE_FOREIGN_DIGEST),
            False,
            id="foreign_provider_layer_digest",
        ),
        pytest.param(_probe_receipt(), True, id="good_receipt"),
    ],
)
def test_is_startable_reads_the_receipt_state(
    receipt: ProviderProbeReceipt | None, expected: bool
) -> None:
    """Missing, red, expired, and foreign-digest receipts each answer unproven.

    A receipt whose `source_commit` differs from this deployment's own but
    whose `provider_layer_digest` matches still proves the configuration
    (#1124): a redeploy that never touches the provider layer must not
    invalidate evidence that is still true.
    """

    factory = RecordingAgentExecutorFactoryV2(
        "anthropic", "claude-cli/v1", "receipt-gated", b"ok"
    )
    registry = AgentExecutorRegistry(
        (factory,),
        receipt_gate=ProviderProbeReceiptGate(
            _FakeProviderProbeReceiptReads(receipt),
            _PROBE_DEPLOYMENT_DIGEST,
            lambda: RecordedAt("2026-01-01T12:00:00Z"),
        ),
    )

    assert (
        registry.is_startable(
            factory.key, AgentExecutionCapability.HEADLESS, _PROBE_CONFIGURATION_HASH
        )
        is expected
    )


def test_is_structurally_startable_asks_nothing_about_receipt_state() -> None:
    """The one question a reprobe exemption or canary discovery may ask.

    Armed with a receipt gate that would refuse every configuration (no
    receipt on file anywhere), the structural answer still says yes for a
    registered, available, capable factory -- and still says no for one the
    operator marked unavailable, regardless of the same missing evidence.
    """

    available = RecordingAgentExecutorFactoryV2(
        "anthropic", "claude-cli/v1", "available", b"ok"
    )
    unavailable = RecordingAgentExecutorFactoryV2(
        "anthropic", "claude-cli-unavailable/v1", "unavailable", b"must-not-run"
    )
    registry = AgentExecutorRegistry(
        (available, AgentExecutorRegistration.unavailable(unavailable)),
        receipt_gate=ProviderProbeReceiptGate(
            _FakeProviderProbeReceiptReads(None),
            _PROBE_DEPLOYMENT_DIGEST,
            lambda: RecordedAt("2026-01-01T12:00:00Z"),
        ),
    )

    assert registry.is_structurally_startable(
        available.key, AgentExecutionCapability.HEADLESS
    )
    assert not registry.is_startable(
        available.key, AgentExecutionCapability.HEADLESS, _PROBE_CONFIGURATION_HASH
    )
    assert not registry.is_structurally_startable(
        unavailable.key, AgentExecutionCapability.HEADLESS
    )


@pytest.mark.proves("a-listed-agent-configuration-names-current-startability")
def test_list_publication_and_start_share_the_current_registry_decision(
    tmp_path: Path,
) -> None:
    unavailable = RecordingAgentExecutorFactoryV2(
        "anthropic", "claude-cli/v1", "unavailable", b"must-not-run"
    )
    sibling = RecordingAgentExecutorFactoryV2(
        "openai", "codex-cli/v1", "sibling", b"review"
    )
    runtime = _runtime(
        tmp_path,
        (AgentExecutorRegistration.unavailable(unavailable), sibling),
    )
    runtime.initialize_storage()
    client = _api_client(runtime)
    try:
        catalog = DbosAgentConfigurationCatalog(
            runtime.engine, runtime.agent_executor_registry
        )
        claude_auth = AuthProfileRevision(
            "max", 1, ProviderId("anthropic"), AuthMode.SUBSCRIPTION
        )
        sibling_auth = AuthProfileRevision(
            "codex", 1, ProviderId("openai"), AuthMode.SUBSCRIPTION
        )
        assert isinstance(
            catalog.publish_auth_profile_revision(claude_auth),
            AuthProfileRevisionCreated,
        )
        assert isinstance(
            catalog.publish_auth_profile_revision(sibling_auth),
            AuthProfileRevisionCreated,
        )
        claude = AgentConfigurationRevision(
            "opus",
            claude_auth.revision_hash,
            AgentExecutorRevision("claude-cli/v1"),
            AgentExecutionCapability.HEADLESS,
            AgentConfigurationRevisionFormatVersion.V2,
        )
        sibling_configuration = AgentConfigurationRevision(
            "gpt-5.6",
            sibling_auth.revision_hash,
            AgentExecutorRevision("codex-cli/v1"),
            AgentExecutionCapability.HEADLESS,
            AgentConfigurationRevisionFormatVersion.V2,
        )
        assert isinstance(
            catalog.publish_agent_configuration_revision(claude),
            AgentConfigurationRevisionCreated,
        )
        assert isinstance(
            catalog.publish_agent_configuration_revision(sibling_configuration),
            AgentConfigurationRevisionCreated,
        )
        publish_checked_model_registry(
            runtime.engine, ProviderId("anthropic"), (claude,)
        )
        publish_checked_model_registry(
            runtime.engine, ProviderId("openai"), (sibling_configuration,)
        )
        workflow = WorkflowRevision(_V3_DOCUMENT)
        _publish_output_schema(runtime)
        DbosWorkflowRevisionPublisher(runtime.engine).publish(workflow)

        listed = client.get(API_PREFIX + "/agent-configuration-revisions")
        assert listed.status_code == 200
        by_model = {item["model"]: item for item in listed.json()["items"]}
        assert by_model["opus"]["startable"] is False
        assert (
            by_model["opus"]["not_startable_reason"]
            == AgentExecutionRefusal.EXECUTOR_BINDING_UNAVAILABLE.value
        )
        assert by_model["gpt-5.6"]["startable"] is True
        assert by_model["gpt-5.6"]["not_startable_reason"] is None

        refused = client.post(
            API_PREFIX + "/runs",
            json={
                "workflow_format_version": 3,
                "run_id": "unavailable-new-draft",
                "workflow_revision_hash": workflow.revision_hash.value,
                "agent_bindings": [
                    {
                        "role": "builder",
                        "agent_configuration_revision_hash": claude.revision_hash.value,
                    }
                ],
                "orders": [],
            },
        )
        assert refused.status_code == 409
        assert refused.json()["type"].endswith(":agent-executor-binding-unavailable")
        assert unavailable.opens == 0
        with runtime.engine.connect() as connection:
            assert connection.scalar(sa.select(sa.func.count()).select_from(runs)) == 0
            assert (
                connection.scalar(
                    sa.select(sa.func.count()).select_from(agent_attempts)
                )
                == 0
            )
    finally:
        runtime.close()


def test_a_tampered_model_registry_entry_answers_durable_state_corrupt_over_http(
    tmp_path: Path,
) -> None:
    """A registry row whose bytes disagree with its own revision hash fails loud.

    The listing now reads the model registry to compute `model_registered`;
    a row corrupted beneath it must surface as the durable-state-corrupt
    problem the rest of the catalog already answers with, never a bare
    internal error.
    """

    factory = RecordingAgentExecutorFactoryV2(
        "anthropic", "claude-cli/v1", "claude-registry-corrupt", b"build"
    )
    runtime = _runtime(tmp_path, (factory,))
    runtime.initialize_storage()
    client = _api_client(runtime)
    try:
        catalog = DbosAgentConfigurationCatalog(
            runtime.engine, runtime.agent_executor_registry
        )
        auth = AuthProfileRevision(
            "max", 1, ProviderId("anthropic"), AuthMode.SUBSCRIPTION
        )
        assert isinstance(
            catalog.publish_auth_profile_revision(auth), AuthProfileRevisionCreated
        )
        configuration = AgentConfigurationRevision(
            "opus",
            auth.revision_hash,
            AgentExecutorRevision("claude-cli/v1"),
            AgentExecutionCapability.HEADLESS,
            AgentConfigurationRevisionFormatVersion.V2,
        )
        assert isinstance(
            catalog.publish_agent_configuration_revision(configuration),
            AgentConfigurationRevisionCreated,
        )
        publish_checked_model_registry(
            runtime.engine, ProviderId("anthropic"), (configuration,)
        )
        with runtime.engine.begin() as connection:
            connection.execute(
                sa.text("DROP TRIGGER host_model_registry_entries_no_update")
            )
            connection.execute(
                host_model_registry_entries.update().values(model_id="tampered-model")
            )

        response = client.get(API_PREFIX + "/agent-configuration-revisions")

        assert response.status_code == 500
        assert response.json()["type"].endswith("durable-state-corrupt")
    finally:
        runtime.close()


@pytest.mark.proves("a-bound-unstarted-run-refuses-when-its-executor-is-unavailable")
def test_bound_unstarted_run_fails_without_an_attempt_when_executor_is_unavailable(
    tmp_path: Path,
) -> None:
    seeded_factory = RecordingAgentExecutorFactoryV2(
        "anthropic", "claude-cli/v1", "seed", b"unused"
    )
    seeded = _runtime(tmp_path, (seeded_factory,))
    run_id = RunId("unavailable-executor/bound-run")
    try:
        seeded.initialize_storage()
        _publish_output_schema(seeded)
        workflow, bindings = _publish_single_capability(
            seeded, AgentExecutionCapability.HEADLESS, document=_V3_DOCUMENT
        )
        started = DbosDurableRunStarter(
            seeded.engine,
            seeded.settings,
            seeded.agent_executor_registry,
        ).start_published(
            StartPublishedRunRequestV2(run_id, workflow.revision_hash, bindings)
        )
        assert isinstance(started, DurableRunCreated)
    finally:
        seeded.close()

    unavailable_factory = RecordingAgentExecutorFactoryV2(
        "anthropic", "claude-cli/v1", "unavailable", b"must-not-run"
    )
    restarted = _runtime(
        tmp_path, (AgentExecutorRegistration.unavailable(unavailable_factory),)
    )
    try:
        with restarted.engine.connect() as connection:
            restart_head = connection.execute(
                sa.select(
                    runs.c.state,
                    runs.c.current_node_id,
                    runs.c.state_version,
                    runs.c.last_event_sequence,
                ).where(runs.c.run_id == run_id.value)
            ).one()
        assert tuple(restart_head) == ("STARTED", "build", 0, 0)
        restarted.launch()
        failed = _wait_failed(restarted, run_id)

        assert unavailable_factory.opens == 0
        assert failed.terminal_hash is not None
        with restarted.engine.connect() as connection:
            events = tuple(
                connection.execute(
                    sa.select(run_events).where(run_events.c.run_id == run_id.value)
                ).mappings()
            )
            assert (
                connection.scalar(
                    sa.select(sa.func.count()).select_from(agent_attempts)
                )
                == 0
            )
        assert len(events) == 1
        assert events[0]["event_kind"] == "AGENT_FAILED"
        assert events[0]["payload"] == (
            AgentExecutionRefusal.EXECUTOR_BINDING_UNAVAILABLE.value.encode("ascii")
        )
        assert events[0]["agent_attempt_id"] is None
        assert events[0]["attempt_ordinal"] is None
        response = _api_client(restarted).get(
            API_PREFIX + "/runs/" + encode_public_run_reference(run_id) + "/events"
        )
        assert response.status_code == 200
        event = json.loads(
            next(
                line.removeprefix("data: ")
                for line in response.text.splitlines()
                if line.startswith("data: ")
            )
        )
        assert event["event"] == "AGENT_FAILED"
        assert (
            event["reason"] == AgentExecutionRefusal.EXECUTOR_BINDING_UNAVAILABLE.value
        )
        assert "failure_code" not in event
        assert "attempt_id" not in event
        assert "attempt_ordinal" not in event
        detail = _api_client(restarted).get(
            API_PREFIX + "/runs/" + encode_public_run_reference(run_id) + "/nodes/build"
        )
        assert detail.status_code == 200
        assert (
            detail.json()["refusal"]
            == AgentExecutionRefusal.EXECUTOR_BINDING_UNAVAILABLE.value
        )
    finally:
        restarted.close()


@pytest.mark.proves("a-bound-unstarted-run-refuses-when-its-executor-is-unavailable")
def test_wait_predecessor_stays_intact_until_it_reaches_an_unavailable_executor(
    tmp_path: Path,
) -> None:
    wait_then_agent = (
        b"""format_version: 3
name: Wait then agent
nodes:
  - id: ask
    type: wait
    prompt: Approve before the agent runs.
"""
        + declared_output(name="approval")
        + b"""  - id: build
    type: agent
    role: builder
    mode: headless
    instruction: build
    depends_on: [ask]
"""
        + declared_output()
    )
    seeded_factory = RecordingAgentExecutorFactoryV2(
        "anthropic", "claude-cli/v1", "seed", b"unused"
    )
    run_id = RunId("unavailable-executor/wait-predecessor")
    seeded = _runtime(tmp_path, (seeded_factory,))
    try:
        seeded.initialize_storage()
        workflow, bindings = _publish_single_capability(
            seeded,
            AgentExecutionCapability.HEADLESS,
            document=wait_then_agent,
        )
        assert isinstance(
            DbosDurableRunStarter(
                seeded.engine,
                seeded.settings,
                seeded.agent_executor_registry,
            ).start_published(
                StartPublishedRunRequestV2(run_id, workflow.revision_hash, bindings)
            ),
            DurableRunCreated,
        )
        seeded.launch()
        assert (
            _run_at_state(seeded, run_id, RunState.WAITING_INPUT).current_node_id
            == "ask"
        )
    finally:
        seeded.close()

    unavailable_factory = RecordingAgentExecutorFactoryV2(
        "anthropic", "claude-cli/v1", "unavailable", b"must-not-run"
    )
    restarted = _runtime(
        tmp_path, (AgentExecutorRegistration.unavailable(unavailable_factory),)
    )
    try:
        restarted.launch()
        assert (
            _run_at_state(restarted, run_id, RunState.WAITING_INPUT).current_node_id
            == "ask"
        )

        answer = DbosWaitAnswerer(
            restarted.engine, restarted.settings.application_version
        ).submit_result(
            SubmitWaitAnswerRequest(
                run_id,
                workflow.revision_hash,
                "ask",
                NodeExecutionId.for_node(run_id, workflow.revision_hash, "ask"),
                WaitAnswerActor.OPERATOR,
                b"5",
            )
        )
        assert isinstance(answer, DurableAnswerCreated)
        _wait_failed(restarted, run_id)

        assert unavailable_factory.opens == 0
        with restarted.engine.connect() as connection:
            assert tuple(
                connection.scalars(
                    sa.select(run_events.c.event_kind)
                    .where(run_events.c.run_id == run_id.value)
                    .order_by(run_events.c.event_sequence)
                )
            ) == ("WAITING_INPUT", "WAIT_ANSWERED", "AGENT_FAILED")
    finally:
        restarted.close()


@pytest.mark.proves("a-bound-unstarted-run-refuses-when-its-executor-is-unavailable")
def test_prepared_v2_attempt_is_cleaned_before_unavailable_executor_refusal(
    tmp_path: Path,
) -> None:
    seeded_factory = RecordingAgentExecutorFactoryV2(
        "anthropic", "claude-cli/v1", "seed", b"unused"
    )
    seeded = _runtime(tmp_path, (seeded_factory,))
    run_id = RunId("unavailable-executor/prepared-run")
    try:
        seeded.initialize_storage()
        workflow, bindings = _publish_single_capability(
            seeded, AgentExecutionCapability.HEADLESS
        )
        started = DbosDurableRunStarter(
            seeded.engine,
            seeded.settings,
            seeded.agent_executor_registry,
        ).start_published(
            StartPublishedRunRequestV2(run_id, workflow.revision_hash, bindings)
        )
        assert isinstance(started, DurableRunCreated)
        auth = AuthProfileRevision(
            "max", 1, ProviderId("anthropic"), AuthMode.SUBSCRIPTION
        )
        configuration = AgentConfigurationRevision(
            "opus",
            auth.revision_hash,
            AgentExecutorRevision("claude-cli/v1"),
            AgentExecutionCapability.HEADLESS,
            AgentConfigurationRevisionFormatVersion.V2,
        )
        request = _replayed_request(
            _encoded_binding(
                configuration, auth, include_contract=True, job="Build the one thing."
            ),
            run_id,
            workflow.revision_hash,
            "build",
            seeded_factory.operational_identity,
            seeded_factory.declared_capabilities,
        )
        execution = agent_attempt_execution(request)
        prepared = DbosAgentAttemptStore(
            seeded.engine, seeded.settings.application_version
        ).prepare(execution)
        assert prepared.state.value == "PREPARED"
        with seeded.engine.connect() as connection:
            run_head = connection.execute(
                sa.select(
                    runs.c.state,
                    runs.c.current_node_id,
                    runs.c.state_version,
                    runs.c.last_event_sequence,
                ).where(runs.c.run_id == run_id.value)
            ).one()
        assert tuple(run_head) == ("STARTED", "build", 0, 0)
        store = DbosAgentAttemptStore(
            seeded.engine, seeded.settings.application_version
        )
        requested_cleanup = store.refuse_unstartable_node(
            execution, AgentExecutionRefusal.EXECUTOR_BINDING_UNAVAILABLE
        )
        assert isinstance(
            requested_cleanup, AgentExecutorBindingRefusalNeedsPreparedCleanup
        )
        accepted = store.request_cancellation(requested_cleanup.cleanup_request)
        assert isinstance(accepted, AgentAttemptCancellationAccepted)
        in_progress = store.refuse_unstartable_node(
            execution, AgentExecutionRefusal.EXECUTOR_BINDING_UNAVAILABLE
        )
        assert isinstance(in_progress, AgentExecutorBindingRefusalNeedsPreparedCleanup)
        assert in_progress.cleanup_request == requested_cleanup.cleanup_request
        workspace_owner = seeded.agent_workspace_owner
        assert workspace_owner is not None
        terminal = continue_agent_attempt_cancellation(
            requested_cleanup.cleanup_request,
            store,
            seeded.agent_process_supervisor,
            workspace_owner,
        )
        assert terminal is not None
        assert isinstance(
            store.refuse_unstartable_node(
                execution, AgentExecutionRefusal.EXECUTOR_BINDING_UNAVAILABLE
            ),
            AgentExecutorBindingRefusalWritten,
        )
        assert isinstance(
            store.refuse_unstartable_node(
                execution, AgentExecutionRefusal.EXECUTOR_BINDING_UNAVAILABLE
            ),
            AgentExecutorBindingRefusalWritten,
        )
        failed = durable_queries(seeded.engine).get_run(run_id)
        assert isinstance(failed, RunFound)
        assert failed.projection.run.state is RunState.FAILED
        with seeded.engine.connect() as connection:
            attempt = connection.execute(sa.select(agent_attempts)).mappings().one()
            event_kinds = tuple(
                connection.scalars(
                    sa.select(run_events.c.event_kind)
                    .where(run_events.c.run_id == run_id.value)
                    .order_by(run_events.c.event_sequence)
                )
            )
        assert attempt["state"] == "CANCELLED"
        assert attempt["process_phase"] == "CLEANUP_ATTESTED"
        assert attempt["cancellation_disposition"] == "NEVER_LAUNCHED"
        assert event_kinds == (
            "AGENT_CANCEL_REQUESTED",
            "AGENT_CANCELLED",
            "AGENT_FAILED",
        )
    finally:
        seeded.close()


@pytest.mark.proves("a-bound-unstarted-run-refuses-when-its-executor-is-unavailable")
def test_prepared_attempt_is_cleaned_through_durable_node_when_executor_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded_factory = RecordingAgentExecutorFactoryV2(
        "anthropic", "claude-cli/v1", "seed", b"unused"
    )
    seeded = _runtime(tmp_path, (seeded_factory,))
    run_id = RunId("unavailable-executor/prepared-durable-node")
    try:
        seeded.initialize_storage()
        _publish_output_schema(seeded)
        workflow, bindings = _publish_single_capability(
            seeded, AgentExecutionCapability.HEADLESS, document=_V3_DOCUMENT
        )
        started = DbosDurableRunStarter(
            seeded.engine,
            seeded.settings,
            seeded.agent_executor_registry,
        ).start_published(
            StartPublishedRunRequestV2(run_id, workflow.revision_hash, bindings)
        )
        assert isinstance(started, DurableRunCreated)
        # The attempt the restarted node has to find again is the one that node
        # composes, so it is prepared from the run's own durable binding rather
        # than from a request built beside it.
        request = _replayed_request(
            dict(
                _node_binding(
                    seeded.datasource, run_id, workflow.revision_hash, "build", None
                )
            ),
            run_id,
            workflow.revision_hash,
            "build",
            seeded_factory.operational_identity,
            seeded_factory.declared_capabilities,
        )
        prepared = DbosAgentAttemptStore(
            seeded.engine, seeded.settings.application_version
        ).prepare(agent_attempt_execution(request))
        assert prepared.state.value == "PREPARED"
    finally:
        seeded.close()

    cancel_calls = {"count": 0}
    original_request_cancellation = DbosAgentAttemptStore.request_cancellation

    def request_cancellation(
        self: DbosAgentAttemptStore, cleanup_request: CancelAgentAttemptRequest
    ) -> object:
        cancel_calls["count"] += 1
        if cancel_calls["count"] == 1:
            return AgentAttemptCancellationStale()
        return original_request_cancellation(self, cleanup_request)

    monkeypatch.setattr(
        DbosAgentAttemptStore, "request_cancellation", request_cancellation
    )
    unavailable_factory = RecordingAgentExecutorFactoryV2(
        "anthropic", "claude-cli/v1", "seed", b"must-not-run"
    )
    restarted = _runtime(
        tmp_path, (AgentExecutorRegistration.unavailable(unavailable_factory),)
    )
    try:
        restarted.launch()
        failed = _wait_failed(restarted, run_id)

        assert cancel_calls["count"] >= 2
        assert unavailable_factory.opens == 0
        assert failed.terminal_hash is not None
        with restarted.engine.connect() as connection:
            attempt = connection.execute(sa.select(agent_attempts)).mappings().one()
            events = tuple(
                connection.execute(
                    sa.select(run_events)
                    .where(run_events.c.run_id == run_id.value)
                    .order_by(run_events.c.event_sequence)
                ).mappings()
            )
        assert attempt["state"] == "CANCELLED"
        assert attempt["process_phase"] == "CLEANUP_ATTESTED"
        assert attempt["cancellation_disposition"] == "NEVER_LAUNCHED"
        assert tuple(event["event_kind"] for event in events) == (
            "AGENT_CANCEL_REQUESTED",
            "AGENT_CANCELLED",
            "AGENT_FAILED",
        )
        assert events[-1]["payload"] == (
            AgentExecutionRefusal.EXECUTOR_BINDING_UNAVAILABLE.value.encode("ascii")
        )
        assert events[-1]["agent_attempt_id"] is None
        assert events[-1]["attempt_ordinal"] is None
        client = _api_client(restarted)
        public_ref = encode_public_run_reference(run_id)
        listed = client.get(API_PREFIX + "/runs/" + public_ref)
        assert listed.status_code == 200
        listed_body = listed.json()
        failed_rail = [("build", "failed", None)]
        assert [
            (entry["node_id"], entry["state"], entry["attempt"])
            for entry in listed_body["node_rail"]
        ] == failed_rail
        detail = client.get(API_PREFIX + "/runs/" + public_ref + "/nodes/build")
        assert detail.status_code == 200
        assert detail.json()["state"] == "failed"
        assert (
            detail.json()["refusal"]
            == AgentExecutionRefusal.EXECUTOR_BINDING_UNAVAILABLE.value
        )
        events_response = client.get(API_PREFIX + "/runs/" + public_ref + "/events")
        assert events_response.status_code == 200
        streamed = [
            json.loads(line.removeprefix("data: "))
            for line in events_response.text.splitlines()
            if line.startswith("data: ")
        ]
        assert streamed[-1]["event"] == "AGENT_FAILED"
        assert (
            streamed[-1]["reason"]
            == AgentExecutionRefusal.EXECUTOR_BINDING_UNAVAILABLE.value
        )
        assert "attempt_id" not in streamed[-1]
        assert [
            (entry["node_id"], entry["state"], entry["attempt"])
            for entry in streamed[-1]["node_rail"]
        ] == failed_rail
    finally:
        restarted.close()


@pytest.mark.parametrize(
    "predecessor",
    ("launch-armed", "cancel-requested"),
)
@pytest.mark.proves("a-bound-unstarted-run-refuses-when-its-executor-is-unavailable")
def test_unavailable_executor_refusal_leaves_launch_fences_unchanged(
    tmp_path: Path, predecessor: str
) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        execution = agent_attempt_execution(
            attempt_request(runtime, f"unavailable-executor/fence/{predecessor}")
        )
        store = DbosAgentAttemptStore(
            runtime.engine, runtime.settings.application_version
        )
        prepared = store.prepare(execution)
        match predecessor:
            case "launch-armed":
                assert isinstance(store.claim(execution), AgentAttemptClaimedByThisCall)
            case "cancel-requested":
                assert isinstance(
                    store.request_cancellation(
                        CancelAgentAttemptRequest(
                            execution.request.run_id,
                            execution.attempt_id,
                            "existing-cancel",
                            prepared.state_version,
                            AgentAttemptReplacement.NONE,
                        )
                    ),
                    AgentAttemptCancellationAccepted,
                )
            case _ as unreachable:
                raise AssertionError(f"unexpected fence predecessor: {unreachable}")

        before = store.load(execution.attempt_id)
        with runtime.engine.connect() as connection:
            events_before = tuple(
                connection.execute(
                    sa.select(run_events.c.event_kind, run_events.c.event_hash)
                    .where(run_events.c.run_id == execution.request.run_id.value)
                    .order_by(run_events.c.event_sequence)
                )
            )

        fenced = store.refuse_unstartable_node(
            execution, AgentExecutionRefusal.EXECUTOR_BINDING_UNAVAILABLE
        )

        assert isinstance(fenced, AgentExecutorBindingRefusalFenced)
        assert fenced.attempt == before
        assert store.load(execution.attempt_id) == before
        with runtime.engine.connect() as connection:
            events_after = tuple(
                connection.execute(
                    sa.select(run_events.c.event_kind, run_events.c.event_hash)
                    .where(run_events.c.run_id == execution.request.run_id.value)
                    .order_by(run_events.c.event_sequence)
                )
            )
        assert events_after == events_before
    finally:
        runtime.close()


def test_completed_v2_history_reopens_without_process_supervision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _runtime(
        tmp_path,
        (
            RecordingAgentExecutorFactoryV2(
                "anthropic", "claude-cli/v1", "completed-history", b'"built"'
            ),
        ),
    )
    run_id = RunId("empty-registry/completed-history")
    try:
        first.initialize_storage()
        workflow, bindings = _publish_single_capability(
            first, AgentExecutionCapability.HEADLESS
        )
        started = DbosDurableRunStarter(
            first.engine,
            first.settings,
            first.agent_executor_registry,
        ).start_published(
            StartPublishedRunRequestV2(run_id, workflow.revision_hash, bindings)
        )
        assert isinstance(started, DurableRunCreated)
        first.launch()
        _wait_completed(first, run_id)
    finally:
        first.close()

    def forbidden_process_authority() -> Never:
        raise AssertionError("completed history resolved process authority")

    monkeypatch.setattr(
        dbos_runtime, "delegated_cgroup_root", forbidden_process_authority
    )
    monkeypatch.setattr(
        dbos_runtime, "AgentProcessSupervisor", forbidden_process_authority
    )
    restarted = _runtime(tmp_path, ())
    try:
        restarted.launch()
        found = durable_queries(restarted.engine).get_run(run_id)
        assert isinstance(found, RunFound)
        assert found.projection.run.state is RunState.COMPLETED
    finally:
        restarted.close()


def test_two_provider_configs_survive_restart_and_drive_their_exact_executors(
    tmp_path: Path,
) -> None:
    """Two roles, two providers, restarted mid-line: each keeps its own executor.

    Retired 2026-09-01 (#901 slice 5, #934): this test once answered the
    reviewer role with a raw non-UTF-8 byte string (b"\xffreview") and
    asserted it survived a restart byte-exact, proving V1/V2's free-form
    output carried anything. A V3 declared output is schema-validated JSON
    (ANY_JSON_SCHEMA), so that claim is no longer representable -- an agent's
    answer must decode as JSON before this product ever durably keeps it. The
    surviving invariant -- each role's exact answer reaches its own receipt,
    keyed to the provider that restarted with it -- is proven the same way,
    with a JSON-valid answer standing in for the retired raw one.
    """
    first_factories = (
        RecordingAgentExecutorFactoryV2(
            "openai", "codex-cli/v1", "codex-before-restart", b'"review"'
        ),
        RecordingAgentExecutorFactoryV2(
            "anthropic", "claude-cli/v1", "claude-before-restart", b'"build"'
        ),
    )
    first = _runtime(tmp_path, first_factories)
    first.initialize_storage()
    workflow, bindings = _publish_matrix(first)
    run_id = RunId("provider-neutral/restart")
    result = DbosDurableRunStarter(
        first.engine,
        first.settings,
        first.agent_executor_registry,
    ).start_published(
        StartPublishedRunRequestV2(run_id, workflow.revision_hash, bindings)
    )
    assert isinstance(result, DurableRunCreated)
    first.close()

    restarted_factories = (
        RecordingAgentExecutorFactoryV2(
            "anthropic", "claude-cli/v1", "claude-after-restart", b'"build"'
        ),
        RecordingAgentExecutorFactoryV2(
            "openai", "codex-cli/v1", "codex-after-restart", b'"review"'
        ),
    )
    restarted = _runtime(tmp_path, restarted_factories)
    try:
        restarted.launch()
        completed = _wait_completed(restarted, run_id)
        queried = durable_queries(restarted.engine)
        found = queried.get_run(run_id)
        page = queried.list_runs(None, 100)
        assert isinstance(found, RunFound)
        assert isinstance(found.projection.run, RunV3)
        assert isinstance(page, RunPage)
        assert page.runs == (found.projection,)
        assert completed.binding_set_hash == bindings.binding_set_hash
        assert tuple(binding.role.value for binding in completed.agent_bindings) == (
            "builder",
            "reviewer",
        )
        with restarted.engine.connect() as connection:
            assert connection.scalar(sa.select(sa.func.count()).select_from(runs)) == 1
            assert (
                connection.scalar(
                    sa.select(sa.func.count()).select_from(run_agent_bindings)
                )
                == 2
            )
            receipts = tuple(
                connection.execute(
                    sa.select(agent_receipts_v2).order_by(agent_receipts_v2.c.role)
                ).mappings()
            )
        assert [record["executor_operational_identity"] for record in receipts] == [
            "claude-after-restart",
            "codex-after-restart",
        ]
        assert bytes(receipts[1]["output_bytes"]) == b'"review"'
    finally:
        restarted.close()


def test_private_factory_canary_never_enters_any_public_or_durable_channel(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    canary = "private-provider-material-7f7b0d8b"
    factory = RecordingAgentExecutorFactoryV2(
        "anthropic", "claude-cli/v1", "public-operation", b'"built"'
    )
    factory.__dict__["private_material"] = canary
    runtime = _runtime(tmp_path, (factory,))
    runtime.initialize_storage()
    _publish_output_schema(runtime)
    workflow, bindings = _publish_single_capability(
        runtime, AgentExecutionCapability.HEADLESS, document=_V3_DOCUMENT
    )
    run_id = RunId("secret-free/channels")
    started = DbosDurableRunStarter(
        runtime.engine,
        runtime.settings,
        runtime.agent_executor_registry,
    ).start_published(
        StartPublishedRunRequestV2(run_id, workflow.revision_hash, bindings)
    )
    assert isinstance(started, DurableRunCreated)

    try:
        runtime.launch()
        _wait_completed(runtime, run_id)
        client = _api_client(runtime)
        public_reference = encode_public_run_reference(run_id)
        public_channels = (
            client.get(API_PREFIX + f"/runs/{public_reference}").text,
            client.get(API_PREFIX + "/runs").text,
            client.get(API_PREFIX + f"/runs/{public_reference}/events").text,
            str(cast(FastAPI, client.app).openapi()),
        )
        assert all(canary not in channel for channel in public_channels)

        with runtime.engine.connect() as connection:
            table_names = tuple(sa.inspect(connection).get_table_names())
            assert {
                "runs",
                "run_events",
                "agent_receipts_v2",
                "reconcile_commands",
                "workflow_status",
                "operation_outputs",
            }.issubset(table_names)
            durable_channels = {
                table_name: tuple(
                    tuple(row)
                    for row in connection.exec_driver_sql(
                        f'SELECT * FROM "{table_name}"'
                    )
                )
                for table_name in table_names
            }
        assert len(durable_channels["agent_receipts_v2"]) == 1
        assert len(durable_channels["run_events"]) == 1
        assert durable_channels["workflow_status"]
        assert durable_channels["operation_outputs"]
        assert all(canary not in repr(rows) for rows in durable_channels.values())
        assert canary not in "\n".join(record.getMessage() for record in caplog.records)
    finally:
        runtime.close()


def test_catalog_workflow_start_get_and_list_roundtrip_through_the_real_api(
    tmp_path: Path,
) -> None:
    factory = RecordingAgentExecutorFactoryV2(
        "anthropic", "claude-cli/v1", "claude-api-test", b'"build"'
    )
    runtime = _runtime(tmp_path, (factory,))
    runtime.initialize_storage()
    client = _api_client(runtime)
    auth_profile = AuthProfileRevision(
        "max", 1, ProviderId("anthropic"), AuthMode.SUBSCRIPTION
    )
    catalogued = AgentConfigurationRevision(
        "opus",
        auth_profile.revision_hash,
        AgentExecutorRevision("claude-cli/v1"),
        AgentExecutionCapability.HEADLESS,
        AgentConfigurationRevisionFormatVersion.V2,
    )
    try:
        schema = client.post(
            API_PREFIX + "/schema-revisions",
            content=ANY_JSON_SCHEMA.document,
            headers={"content-type": "application/json"},
        )
        assert schema.status_code == 201
        auth = client.post(
            API_PREFIX + "/auth-profile-revisions",
            json={
                "profile_id": auth_profile.profile_id,
                "revision_number": auth_profile.revision_number,
                "provider_id": auth_profile.provider_id.value,
                "auth_mode": auth_profile.auth_mode.value,
            },
        )
        assert auth.status_code == 201
        auth_hash = auth.json()["auth_profile_revision_hash"]
        configuration = client.post(
            API_PREFIX + "/agent-configuration-revisions",
            json={
                "model": catalogued.model,
                "auth_profile_revision_hash": auth_hash,
                "executor_revision": catalogued.executor_revision.value,
            },
        )
        assert configuration.status_code == 201
        configuration_hash = configuration.json()["agent_configuration_revision_hash"]
        # A start binds a role only to a model the host has checked, so the
        # registry an operator would have published stands here before the start.
        publish_checked_model_registry(
            runtime.engine, auth_profile.provider_id, (catalogued,)
        )
        workflow = client.post(
            API_PREFIX + "/workflow-revisions",
            content=_V3_DOCUMENT,
            headers={"content-type": "application/yaml"},
        )
        assert workflow.status_code == 201
        revision_hash = workflow.json()["workflow_revision_hash"]
        request = {
            "workflow_format_version": 3,
            "run_id": "api/catalog-roundtrip",
            "workflow_revision_hash": revision_hash,
            "agent_bindings": [
                {
                    "role": "builder",
                    "agent_configuration_revision_hash": configuration_hash,
                }
            ],
            "orders": [],
        }

        created = client.post(API_PREFIX + "/runs", json=request)
        retry = client.post(API_PREFIX + "/runs", json=request)
        found = client.get(
            API_PREFIX + "/runs/" + created.json()["public_run_reference"]
        )
        page = client.get(API_PREFIX + "/runs")

        assert created.status_code == 201, created.text
        assert retry.status_code == 200
        assert found.status_code == 200
        assert page.status_code == 200
        assert (
            created.json()
            == retry.json()
            == found.json()
            == page.json()["items"][0]["run"]
        )
        assert page.json()["items"][0]["kind"] == "run"
        assert created.json()["workflow_format_version"] == 3
        assert created.json()["agent_bindings"] == [
            {
                "role": "builder",
                "agent_configuration_revision_hash": configuration_hash,
                "auth_profile_revision_hash": auth_hash,
                "profile_id": "max",
                "revision_number": 1,
                "provider_id": "anthropic",
                "auth_mode": "subscription",
                "model": "opus",
                "executor_revision": "claude-cli/v1",
            }
        ]
        with runtime.engine.connect() as connection:
            assert connection.scalar(sa.select(sa.func.count()).select_from(runs)) == 1
    finally:
        runtime.close()


def test_published_configurations_are_listed_over_the_api(tmp_path: Path) -> None:
    factory = RecordingAgentExecutorFactoryV2(
        "anthropic", "claude-cli/v1", "claude-api-test", b"build"
    )
    runtime = _runtime(tmp_path, (factory,))
    runtime.initialize_storage()
    catalog = DbosAgentConfigurationCatalog(
        runtime.engine, runtime.agent_executor_registry
    )
    client = _api_client(runtime)
    try:
        first_auth = AuthProfileRevision(
            "max", 1, ProviderId("anthropic"), AuthMode.SUBSCRIPTION
        )
        second_auth = AuthProfileRevision(
            "team", 2, ProviderId("anthropic"), AuthMode.API_KEY
        )
        assert isinstance(
            catalog.publish_auth_profile_revision(first_auth),
            AuthProfileRevisionCreated,
        )
        assert isinstance(
            catalog.publish_auth_profile_revision(second_auth),
            AuthProfileRevisionCreated,
        )
        first = AgentConfigurationRevision(
            "opus",
            first_auth.revision_hash,
            AgentExecutorRevision("claude-cli/v1"),
            AgentExecutionCapability.HEADLESS,
            AgentConfigurationRevisionFormatVersion.V2,
        )
        second = AgentConfigurationRevision(
            "sonnet",
            second_auth.revision_hash,
            AgentExecutorRevision("claude-cli/v1"),
            AgentExecutionCapability.HEADLESS,
            AgentConfigurationRevisionFormatVersion.V2,
        )
        assert isinstance(
            catalog.publish_agent_configuration_revision(first),
            AgentConfigurationRevisionCreated,
        )
        assert isinstance(
            catalog.publish_agent_configuration_revision(second),
            AgentConfigurationRevisionCreated,
        )
        publish_checked_model_registry(
            runtime.engine, ProviderId("anthropic"), (first, second)
        )
        stored = catalog.list_agent_configuration_revisions(None, 1)
        assert isinstance(stored, AgentConfigurationRevisionPage)
        assert len(stored.items) == 1
        assert stored.next_after is not None
        stored_auth = catalog.list_auth_profile_revisions(None, 1)
        assert isinstance(stored_auth, AuthProfileRevisionPage)
        assert len(stored_auth.items) == 1
        assert stored_auth.next_after is not None

        auth_first = client.get(
            API_PREFIX + "/auth-profile-revisions", params={"limit": "1"}
        )
        assert auth_first.status_code == 200
        assert len(auth_first.json()["items"]) == 1
        assert auth_first.json()["next_after_revision_hash"] is not None
        auth_second = client.get(
            API_PREFIX + "/auth-profile-revisions",
            params={
                "limit": "1",
                "after_revision_hash": auth_first.json()["next_after_revision_hash"],
            },
        )
        assert auth_second.status_code == 200
        assert len(auth_second.json()["items"]) == 1
        assert auth_second.json()["next_after_revision_hash"] is None
        listed_profiles = {
            item["profile_id"]
            for item in auth_first.json()["items"] + auth_second.json()["items"]
        }
        assert listed_profiles == {"max", "team"}
        for item in auth_first.json()["items"] + auth_second.json()["items"]:
            assert item["provider_id"] == "anthropic"
            assert item["auth_mode"] in {"subscription", "api_key"}
            assert "secret" not in str(item).lower()

        empty = client.get(
            API_PREFIX + "/agent-configuration-revisions", params={"limit": "1"}
        )
        assert empty.status_code == 200
        first_page = empty.json()
        assert len(first_page["items"]) == 1
        assert first_page["next_after_revision_hash"] is not None
        second_page = client.get(
            API_PREFIX + "/agent-configuration-revisions",
            params={
                "limit": "1",
                "after_revision_hash": first_page["next_after_revision_hash"],
            },
        )
        assert second_page.status_code == 200
        assert len(second_page.json()["items"]) == 1
        assert second_page.json()["next_after_revision_hash"] is None
        listed = {
            item["model"] for item in first_page["items"] + second_page.json()["items"]
        }
        assert listed == {"opus", "sonnet"}
        for item in first_page["items"] + second_page.json()["items"]:
            assert item["provider_id"] == "anthropic"
            assert item["auth_mode"] in {"subscription", "api_key"}
            assert item["startable"] is True
            assert item["not_startable_reason"] is None
            assert "secret" not in str(item).lower()
    finally:
        runtime.close()


def test_listing_names_a_filesystem_receipts_own_recorded_failure(
    tmp_path: Path,
) -> None:
    """#1103: the production receipt gate's own store proves this end to end.

    A configuration whose live evidence is a filed `.json` receipt recording
    a failure lists as `provider-probe-failed`, carrying that receipt's own
    problem code and instant -- never the same `provider-probe-receipt-
    missing` a configuration with no receipt at all would get.
    """

    factory = RecordingAgentExecutorFactoryV2(
        "anthropic", "claude-cli/v1", "receipt-failed-test", b"build"
    )
    receipt_directory = tmp_path / "provider-probes"
    receipt_directory.mkdir()
    runtime = DbosRuntime(
        DbosRuntimeSettings(
            tmp_path / "atelier.sqlite",
            "v2-test",
            agent_scratch_root=agent_scratch_root(tmp_path),
            provider_probe_receipt_directory=receipt_directory,
            provider_probe_receipt_provider_layer_digest=_PROBE_DEPLOYMENT_DIGEST,
        ),
        _effect_factory(tmp_path),
        (factory,),
    )
    runtime.initialize_storage()
    catalog = DbosAgentConfigurationCatalog(
        runtime.engine, runtime.agent_executor_registry
    )
    try:
        auth = AuthProfileRevision(
            "max", 1, ProviderId("anthropic"), AuthMode.SUBSCRIPTION
        )
        assert isinstance(
            catalog.publish_auth_profile_revision(auth), AuthProfileRevisionCreated
        )
        configuration = AgentConfigurationRevision(
            "opus",
            auth.revision_hash,
            AgentExecutorRevision("claude-cli/v1"),
            AgentExecutionCapability.HEADLESS,
            AgentConfigurationRevisionFormatVersion.V2,
        )
        assert isinstance(
            catalog.publish_agent_configuration_revision(configuration),
            AgentConfigurationRevisionCreated,
        )
        publish_checked_model_registry(
            runtime.engine, ProviderId("anthropic"), (configuration,)
        )
        receipt = ProviderProbeReceipt(
            ProviderProbeVectorId("headless-fixture"),
            configuration.revision_hash,
            WorkflowRevisionHash("f" * 64),
            _PROBE_DEPLOYMENT_DIGEST,
            _PROBE_DEPLOYMENT_SOURCE_COMMIT,
            RecordedAt("2026-09-03T16:17:00Z"),
            RecordedAt("2026-09-04T16:17:00Z"),
            ProviderProbeResult.FAILED,
            RunId("provider-canary/fixture"),
            problem_code=ProviderProbeProblemCode("provider-overloaded"),
        )
        (receipt_directory / "headless-fixture.json").write_bytes(
            receipt.canonical_bytes()
        )

        page = catalog.list_agent_configuration_revisions(None, 50)
        assert isinstance(page, AgentConfigurationRevisionPage)
        assert len(page.items) == 1
        item = page.items[0]
        assert item.startable is False
        assert item.has_valid_receipt is False
        assert (
            item.not_startable_reason
            == AgentConfigurationNotStartableReason.PROVIDER_PROBE_FAILED
        )
        assert item.probe_failure is not None
        assert item.probe_failure.problem_code.value == "provider-overloaded"
        assert item.probe_failure.observed_at.value == "2026-09-03T16:17:00Z"
    finally:
        runtime.close()


def test_a_receipt_survives_a_redeploy_that_does_not_touch_the_provider_layer(
    tmp_path: Path,
) -> None:
    """#1124: the production receipt gate reads the filesystem end to end.

    A receipt proven under a `source_commit` this deployment does not share
    still lists as startable, because the gate now compares
    `provider_layer_digest`, not the commit -- exactly the redeploy that must
    not invalidate every receipt at once. A second receipt proven under a
    foreign digest still lists as unproven.
    """

    factory = RecordingAgentExecutorFactoryV2(
        "anthropic", "claude-cli/v1", "receipt-digest-test", b"build"
    )
    receipt_directory = tmp_path / "provider-probes"
    receipt_directory.mkdir()
    runtime = DbosRuntime(
        DbosRuntimeSettings(
            tmp_path / "atelier.sqlite",
            "v2-test",
            agent_scratch_root=agent_scratch_root(tmp_path),
            provider_probe_receipt_directory=receipt_directory,
            provider_probe_receipt_provider_layer_digest=_PROBE_DEPLOYMENT_DIGEST,
        ),
        _effect_factory(tmp_path),
        (factory,),
    )
    runtime.initialize_storage()
    catalog = DbosAgentConfigurationCatalog(
        runtime.engine, runtime.agent_executor_registry
    )
    try:
        auth = AuthProfileRevision(
            "max", 1, ProviderId("anthropic"), AuthMode.SUBSCRIPTION
        )
        assert isinstance(
            catalog.publish_auth_profile_revision(auth), AuthProfileRevisionCreated
        )
        proven = AgentConfigurationRevision(
            "opus-survives-redeploy",
            auth.revision_hash,
            AgentExecutorRevision("claude-cli/v1"),
            AgentExecutionCapability.HEADLESS,
            AgentConfigurationRevisionFormatVersion.V2,
        )
        unproven = AgentConfigurationRevision(
            "opus-foreign-digest",
            auth.revision_hash,
            AgentExecutorRevision("claude-cli/v1"),
            AgentExecutionCapability.HEADLESS,
            AgentConfigurationRevisionFormatVersion.V2,
        )
        for configuration in (proven, unproven):
            assert isinstance(
                catalog.publish_agent_configuration_revision(configuration),
                AgentConfigurationRevisionCreated,
            )
        publish_checked_model_registry(
            runtime.engine, ProviderId("anthropic"), (proven, unproven)
        )
        (receipt_directory / "proven.json").write_bytes(
            _probe_receipt(
                configuration_hash=proven.revision_hash,
                source_commit=_PROBE_FOREIGN_SOURCE_COMMIT,
                provider_layer_digest=_PROBE_DEPLOYMENT_DIGEST,
                valid_until="2030-01-01T00:00:00Z",
            ).canonical_bytes()
        )
        (receipt_directory / "unproven.json").write_bytes(
            _probe_receipt(
                configuration_hash=unproven.revision_hash,
                provider_layer_digest=_PROBE_FOREIGN_DIGEST,
                valid_until="2030-01-01T00:00:00Z",
            ).canonical_bytes()
        )

        page = catalog.list_agent_configuration_revisions(None, 50)
        assert isinstance(page, AgentConfigurationRevisionPage)
        startable = {item.revision.revision_hash: item.startable for item in page.items}
        assert startable[proven.revision_hash] is True
        assert startable[unproven.revision_hash] is False
    finally:
        runtime.close()


def test_numbered_profile_and_configuration_revisions_are_immutable_and_restart(
    tmp_path: Path,
) -> None:
    root = tmp_path / "catalog-restart"
    factory = RecordingAgentExecutorFactoryV2(
        "anthropic", "claude-cli/v1", "catalog-before", b""
    )
    first = _runtime(root, (factory,))
    first.initialize_storage()
    client = _api_client(first)

    def publish_auth(revision_number: int, auth_mode: str = "subscription") -> Response:
        return client.post(
            API_PREFIX + "/auth-profile-revisions",
            json={
                "profile_id": "max",
                "revision_number": revision_number,
                "provider_id": "anthropic",
                "auth_mode": auth_mode,
            },
        )

    first_revision = publish_auth(1)
    exact_retry = publish_auth(1)
    second_revision = publish_auth(2)
    conflict = publish_auth(1, "api_key")
    configuration_request = {
        "model": "opus",
        "auth_profile_revision_hash": first_revision.json()[
            "auth_profile_revision_hash"
        ],
        "executor_revision": "claude-cli/v1",
    }
    configuration = client.post(
        API_PREFIX + "/agent-configuration-revisions", json=configuration_request
    )
    configuration_retry = client.post(
        API_PREFIX + "/agent-configuration-revisions", json=configuration_request
    )

    assert [
        first_revision.status_code,
        exact_retry.status_code,
        second_revision.status_code,
        conflict.status_code,
        configuration.status_code,
        configuration_retry.status_code,
    ] == [201, 200, 201, 409, 201, 200]
    assert conflict.json()["type"].endswith(":auth-profile-revision-conflict")
    first.close()

    restarted = _runtime(
        root,
        (
            RecordingAgentExecutorFactoryV2(
                "anthropic", "claude-cli/v1", "catalog-after", b""
            ),
        ),
    )
    try:
        restarted_client = _api_client(restarted)
        assert (
            restarted_client.post(
                API_PREFIX + "/auth-profile-revisions",
                json={
                    "profile_id": "max",
                    "revision_number": 1,
                    "provider_id": "anthropic",
                    "auth_mode": "subscription",
                },
            ).status_code
            == 200
        )
        assert (
            restarted_client.post(
                API_PREFIX + "/agent-configuration-revisions",
                json=configuration_request,
            ).status_code
            == 200
        )
        with restarted.engine.connect() as connection:
            assert (
                connection.scalar(
                    sa.text("SELECT COUNT(*) FROM auth_profile_revisions")
                )
                == 2
            )
            assert (
                connection.scalar(
                    sa.text("SELECT COUNT(*) FROM agent_configuration_revisions")
                )
                == 1
            )
    finally:
        restarted.close()


def test_start_refusals_precede_run_queue_event_and_rebind_mutation(
    tmp_path: Path,
) -> None:
    factory = RecordingAgentExecutorFactoryV2(
        "anthropic", "claude-cli/v1", "claude-refusal", b'"build"'
    )
    runtime = _runtime(tmp_path, (factory,))
    runtime.initialize_storage()
    _publish_output_schema(runtime)
    client = _api_client(runtime)
    auth_profile = AuthProfileRevision(
        "max", 1, ProviderId("anthropic"), AuthMode.SUBSCRIPTION
    )
    auth = client.post(
        API_PREFIX + "/auth-profile-revisions",
        json={
            "profile_id": auth_profile.profile_id,
            "revision_number": auth_profile.revision_number,
            "provider_id": auth_profile.provider_id.value,
            "auth_mode": auth_profile.auth_mode.value,
        },
    ).json()
    catalogued = tuple(
        AgentConfigurationRevision(
            model,
            auth_profile.revision_hash,
            AgentExecutorRevision("claude-cli/v1"),
            AgentExecutionCapability.HEADLESS,
            AgentConfigurationRevisionFormatVersion.V2,
        )
        for model in ("opus", "sonnet")
    )
    configuration_hashes = []
    for configuration in catalogued:
        response = client.post(
            API_PREFIX + "/agent-configuration-revisions",
            json={
                "model": configuration.model,
                "auth_profile_revision_hash": auth["auth_profile_revision_hash"],
                "executor_revision": configuration.executor_revision.value,
            },
        )
        assert response.status_code == 201
        configuration_hashes.append(
            response.json()["agent_configuration_revision_hash"]
        )
    publish_checked_model_registry(runtime.engine, auth_profile.provider_id, catalogued)
    workflow = client.post(
        API_PREFIX + "/workflow-revisions",
        content=_V3_DOCUMENT,
        headers={"content-type": "application/yaml"},
    ).json()
    revision_hash = workflow["workflow_revision_hash"]

    def start(run_id: str, role: str, configuration_hash: str) -> Response:
        return client.post(
            API_PREFIX + "/runs",
            json={
                "workflow_format_version": 3,
                "run_id": run_id,
                "workflow_revision_hash": revision_hash,
                "agent_bindings": [
                    {
                        "role": role,
                        "agent_configuration_revision_hash": configuration_hash,
                    }
                ],
                "orders": [],
            },
        )

    try:
        unregistered = start("unregistered", "builder", "f" * 64)
        wrong_role = start("wrong-role", "reviewer", configuration_hashes[0])
        valid = start("identity", "builder", configuration_hashes[0])
        rebound = start("identity", "builder", configuration_hashes[1])
        rebound_to_unregistered = start("identity", "builder", "f" * 64)
        unbound = client.post(
            API_PREFIX + "/runs",
            json={
                "run_id": "no-binding-for-a-declared-role",
                "workflow_revision_hash": revision_hash,
            },
        )

        assert unregistered.status_code == 422
        assert unregistered.json()["type"].endswith(":uncast-agent-roles")
        # One problem type now answers what three did, so the failing thing is
        # named in the body rather than in the URN. Asserting the reason is what
        # keeps "this override is not registered" distinguishable from "this role
        # has no default at all" -- the distinction the old 404 carried.
        assert unregistered.json()["uncast_roles"] == [
            {"role": "builder", "reason": "override-not-registered"}
        ]
        assert wrong_role.status_code == 422
        assert wrong_role.json()["type"].endswith(":invalid-agent-bindings")
        assert valid.status_code == 201
        assert rebound.status_code == 409
        assert rebound.json()["type"].endswith(":run-identity-conflict")
        # This one answered 409 run-identity-conflict before the API narrowed to
        # format 3: the identity check ran first, so a retry that changed its
        # configuration still learned the run id was taken. The model cast now
        # runs first and answers about the configuration instead. A retry sending
        # the same body it sent before still gets the conflict -- `rebound` above
        # proves that -- and the durable count below proves no second run is
        # written either way, which is the protection that matters.
        assert rebound_to_unregistered.status_code == 422
        assert rebound_to_unregistered.json()["type"].endswith(":uncast-agent-roles")
        assert rebound_to_unregistered.json()["uncast_roles"] == [
            {"role": "builder", "reason": "override-not-registered"}
        ]
        assert unbound.status_code == 422
        assert unbound.json()["type"].endswith(":uncast-agent-roles")
        assert unbound.json()["uncast_roles"] == [
            {"role": "builder", "reason": "no-project-default"}
        ]
        with runtime.engine.connect() as connection:
            assert connection.scalar(sa.select(sa.func.count()).select_from(runs)) == 1
            assert (
                connection.scalar(
                    sa.select(sa.func.count()).select_from(run_agent_bindings)
                )
                == 1
            )
            assert (
                connection.scalar(
                    sa.select(run_agent_bindings.c.agent_configuration_revision_hash)
                )
                == configuration_hashes[0]
            )
            assert (
                connection.scalar(sa.select(sa.func.count()).select_from(run_events))
                == 0
            )
    finally:
        runtime.close()

    empty_root = tmp_path / "production-empty"
    seeded = _runtime(
        empty_root,
        (
            RecordingAgentExecutorFactoryV2(
                "anthropic", "claude-cli/v1", "seed-only", b'"build"'
            ),
        ),
    )
    seeded.initialize_storage()
    _publish_output_schema(seeded)
    seeded_client = _api_client(seeded)
    seeded_auth = seeded_client.post(
        API_PREFIX + "/auth-profile-revisions",
        json={
            "profile_id": auth_profile.profile_id,
            "revision_number": auth_profile.revision_number,
            "provider_id": auth_profile.provider_id.value,
            "auth_mode": auth_profile.auth_mode.value,
        },
    ).json()
    seeded_configuration = seeded_client.post(
        API_PREFIX + "/agent-configuration-revisions",
        json={
            "model": catalogued[0].model,
            "auth_profile_revision_hash": seeded_auth["auth_profile_revision_hash"],
            "executor_revision": catalogued[0].executor_revision.value,
        },
    ).json()["agent_configuration_revision_hash"]
    publish_checked_model_registry(
        seeded.engine, auth_profile.provider_id, (catalogued[0],)
    )
    seeded_revision = seeded_client.post(
        API_PREFIX + "/workflow-revisions",
        content=_V3_DOCUMENT,
        headers={"content-type": "application/yaml"},
    ).json()["workflow_revision_hash"]
    seeded.close()

    production_empty = _runtime(empty_root, ())
    try:
        unavailable = _api_client(production_empty).post(
            API_PREFIX + "/runs",
            json={
                "workflow_format_version": 3,
                "run_id": "no-production-executor",
                "workflow_revision_hash": seeded_revision,
                "agent_bindings": [
                    {
                        "role": "builder",
                        "agent_configuration_revision_hash": seeded_configuration,
                    }
                ],
                "orders": [],
            },
        )
        assert unavailable.status_code == 409
        assert unavailable.json()["type"].endswith(
            ":agent-executor-binding-unavailable"
        )
        with production_empty.engine.connect() as connection:
            assert connection.scalar(sa.select(sa.func.count()).select_from(runs)) == 0
            assert (
                connection.scalar(sa.select(sa.func.count()).select_from(run_events))
                == 0
            )
    finally:
        production_empty.close()


@pytest.mark.parametrize(("output_size", "accepted"), [(49_152, True), (49_153, False)])
def test_the_output_schema_door_admits_the_route_bound_and_refuses_one_byte_past_it(
    tmp_path: Path, output_size: int, accepted: bool
) -> None:
    """The output door reads MAXIMUM_AGENT_OUTPUT_BYTES_V2, not the inline-order default.

    Route-owned bounds (schemas_v3.py): an agent output arrives through the
    provider frame, whose door is 49_152 bytes, not the 16_384-byte inline
    door a declared-output schema would otherwise inherit by default (#901
    slice 5 first exposed the collision; the door is fixed in
    agent_attempt_store.py). A byte over that bound is refused before any
    receipt is written, and the refusal itself is one committed attempt
    event -- unlike the pre-V3 receipt-construction guard this replaces, a
    refusal here is durable rather than a raised exception nothing recorded.
    """
    factories = (
        RecordingAgentExecutorFactoryV2(
            "anthropic", "claude-cli/v1", "claude-bound", b"build"
        ),
        RecordingAgentExecutorFactoryV2(
            "openai", "codex-cli/v1", "codex-bound", b"review"
        ),
    )
    runtime = _runtime(tmp_path, factories)
    runtime.initialize_storage()
    try:
        workflow, bindings = _publish_matrix(runtime)
        run_id = RunId(f"output/{output_size}")
        started = DbosDurableRunStarter(
            runtime.engine,
            runtime.settings,
            runtime.agent_executor_registry,
        ).start_published(
            StartPublishedRunRequestV2(run_id, workflow.revision_hash, bindings)
        )
        assert isinstance(started, DurableRunCreated)
        assert isinstance(started.run, RunV3)
        resolved = next(
            binding
            for binding in started.run.agent_bindings
            if binding.role.value == "builder"
        )
        request = AgentExecutionRequestV2(
            NodeExecutionId.for_node(run_id, workflow.revision_hash, "build"),
            run_id,
            workflow.revision_hash,
            "build",
            resolved,
            AgentExecutorOperationalIdentity("claude-bound"),
            b"build",
        )

        # A JSON string of the exact target byte length: the door measures raw
        # bytes before it ever decodes them, so an over-bound answer is refused
        # by size alone, but an admitted one still has to be the value the
        # declared schema (ANY_JSON_SCHEMA) accepts.
        output = b'"' + b"x" * (output_size - 2) + b'"'
        assert len(output) == output_size

        store = DbosAgentAttemptStore(
            runtime.engine, runtime.settings.application_version
        )
        store.prepare(agent_attempt_execution(request))
        store.claim(agent_attempt_execution(request))
        outcome = store.complete_success(
            agent_attempt_execution(request), AgentExecutionResult(output)
        )

        if accepted:
            assert isinstance(outcome, AgentAttemptSucceeded)
            retried = store.claim(agent_attempt_execution(request))
            assert outcome.attempt.state_version == 2
            assert retried == outcome
            expected_receipts = 1
        else:
            assert isinstance(outcome, AgentAttemptFailed)
            assert outcome.attempt.failure_code is (
                AgentAttemptFailureCode.OUTPUT_SCHEMA_REFUSED
            )
            expected_receipts = 0

        with runtime.engine.connect() as connection:
            assert (
                connection.scalar(
                    sa.select(sa.func.count()).select_from(agent_receipts_v2)
                )
                == expected_receipts
            )
            assert (
                connection.scalar(sa.select(sa.func.count()).select_from(run_events))
                == 1
            )
            record = connection.execute(
                sa.select(
                    runs.c.current_node_id,
                    runs.c.state_version,
                    runs.c.last_event_sequence,
                ).where(runs.c.run_id == run_id.value)
            ).one()
        assert tuple(record) == (("review", 1, 1) if accepted else ("build", 1, 1))
    finally:
        runtime.close()


MODE_MISMATCH_RUN = RunId("mode-mismatch/restart")
BOTH_UNATTENDED_CAPABILITIES = frozenset(
    {AgentExecutionCapability.HEADLESS, AgentExecutionCapability.HEADLESS_WITH_TOOLS}
)


def _mode_mismatch_prepared(
    seeded: DbosRuntime,
    factory: RecordingAgentExecutorFactoryV2,
    monkeypatch: pytest.MonkeyPatch,
) -> AgentExecutionRequestV2:
    """A run recorded before its mode was checked, stopped mid-node by a restart.

    Its first attempt is prepared and its node workflow left enqueued, as a
    serve that died while driving the node leaves both: the restarted runtime
    recovers that workflow, so the attempt has a live driver, and the node
    itself -- not the driverless sweep -- meets what the attempt reached.
    """
    seeded.initialize_storage()
    _publish_output_schema(seeded)
    workflow, bindings = _publish_single_capability(
        seeded, AgentExecutionCapability.HEADLESS_WITH_TOOLS, document=_V3_DOCUMENT
    )
    with monkeypatch.context() as unchecked:
        unchecked.setattr(
            "atelier2.adapters.dbos.starter.agent_mode_mismatch",
            lambda _graph, _bindings: None,
        )
        started = DbosDurableRunStarter(
            seeded.engine, seeded.settings, seeded.agent_executor_registry
        ).start_published(
            StartPublishedRunRequestV2(
                MODE_MISMATCH_RUN, workflow.revision_hash, bindings
            )
        )
    assert isinstance(started, DurableRunCreated)
    request = _replayed_request(
        dict(
            _node_binding(
                seeded.datasource,
                MODE_MISMATCH_RUN,
                workflow.revision_hash,
                "build",
                None,
            )
        ),
        MODE_MISMATCH_RUN,
        workflow.revision_hash,
        "build",
        factory.operational_identity,
        factory.declared_capabilities,
    )
    DbosAgentAttemptStore(seeded.engine, seeded.settings.application_version).prepare(
        agent_attempt_execution(request)
    )
    client = DBOSClient(system_database_engine=seeded.engine, use_listen_notify=False)
    try:
        client.enqueue(
            {
                "workflow_name": NODE_WORKFLOW_NAME,
                "queue_name": QUEUE_NAME,
                "workflow_id": node_workflow_id_for(request.node_execution_id),
                "app_version": seeded.settings.application_version,
            },
            MODE_MISMATCH_RUN.value,
            workflow.revision_hash.value,
            "build",
        )
    finally:
        client.destroy()
    return request


def _watchdog_bound_by_a_serve_that_died(
    store: DbosAgentAttemptStore, request: AgentExecutionRequestV2
) -> None:
    store.bind_watchdog(
        agent_attempt_execution(request),
        AgentProcessOwnerId("the-serve-before-the-restart"),
        WatchdogGenerationId("generation-before-the-restart"),
    )


def _claimed_before_the_restart(
    store: DbosAgentAttemptStore, request: AgentExecutionRequestV2
) -> None:
    _watchdog_bound_by_a_serve_that_died(store, request)
    assert isinstance(
        store.claim(agent_attempt_execution(request)), AgentAttemptClaimedByThisCall
    )


def _replaced_by_ordinal_two(
    store: DbosAgentAttemptStore, request: AgentExecutionRequestV2
) -> None:
    execution = agent_attempt_execution(request)
    command = CancelAgentAttemptRequest(
        request.run_id,
        execution.attempt_id,
        "replace-before-the-restart",
        store.load(execution.attempt_id).state_version,
        AgentAttemptReplacement.ONE,
    )
    assert isinstance(
        store.request_cancellation(command), AgentAttemptCancellationAccepted
    )
    store.attest_cancellation_cleanup(
        command, AgentAttemptCancellationDisposition.NEVER_LAUNCHED, None, None
    )


@pytest.mark.parametrize(
    "reached",
    [
        pytest.param(
            _watchdog_bound_by_a_serve_that_died, id="prepared-whose-watchdog-died"
        ),
        pytest.param(_claimed_before_the_restart, id="claimed"),
        pytest.param(_replaced_by_ordinal_two, id="replaced-by-ordinal-two"),
    ],
)
def test_a_restart_ends_a_node_bound_outside_its_mode_once_whatever_its_attempt_reached(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reached: Callable[[DbosAgentAttemptStore, AgentExecutionRequestV2], None],
) -> None:
    """One cleanup, one named ending, and no attempt left alive or launched."""
    seeded_factory = RecordingAgentExecutorFactoryV2(
        "anthropic",
        "claude-cli/v1",
        "seed",
        b"unused",
        capability_set=BOTH_UNATTENDED_CAPABILITIES,
    )
    seeded = _runtime(tmp_path, (seeded_factory,))
    try:
        request = _mode_mismatch_prepared(seeded, seeded_factory, monkeypatch)
        reached(
            DbosAgentAttemptStore(seeded.engine, seeded.settings.application_version),
            request,
        )
    finally:
        seeded.close()

    # Each refusal asks for its cleanup once, and the cancellation workflow that
    # acceptance enqueues may ask once more; a refusal that keeps re-cleaning a
    # finished attempt asks twice per turn and runs past the bound.
    bound = 6
    cleanups = 0
    requested = DbosAgentAttemptStore.request_cancellation

    def counted(
        self: DbosAgentAttemptStore, cleanup_request: CancelAgentAttemptRequest
    ) -> object:
        nonlocal cleanups
        cleanups += 1
        if cleanups > bound:
            raise AssertionError(f"cleanup requested {cleanups} times for one refusal")
        return requested(self, cleanup_request)

    monkeypatch.setattr(DbosAgentAttemptStore, "request_cancellation", counted)
    restarted_factory = RecordingAgentExecutorFactoryV2(
        "anthropic",
        "claude-cli/v1",
        "seed",
        b"must-not-run",
        capability_set=BOTH_UNATTENDED_CAPABILITIES,
    )
    restarted = _runtime(tmp_path, (restarted_factory,))
    try:
        restarted.launch()
        try:
            _wait_failed(restarted, MODE_MISMATCH_RUN)
        finally:
            assert cleanups <= 3
        with restarted.engine.connect() as connection:
            refusals = (
                connection.execute(
                    sa.select(run_events.c.payload).where(
                        run_events.c.event_kind == "AGENT_FAILED",
                        run_events.c.agent_attempt_id.is_(None),
                    )
                )
                .scalars()
                .all()
            )
            states = connection.execute(sa.select(agent_attempts.c.state)).scalars()
            attempt_states = {AgentAttemptState(str(state)) for state in states}
        assert refusals == [
            AgentExecutionRefusal.AGENT_MODE_MISMATCH.value.encode("ascii")
        ]
        assert attempt_states <= TERMINAL_AGENT_ATTEMPT_STATES
        assert restarted_factory.opened is not None
        assert restarted_factory.opened.requests == []
    finally:
        restarted.close()
