"""The committed `code-review` workflow executes its object result contract."""

from __future__ import annotations

import json
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa

from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.artifact_store import DbosArtifactStore
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.runtime import DbosRuntime
from atelier2.adapters.dbos.schema import runs
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.contracts.agents import (
    AgentBinding,
    AgentBindingSet,
    AgentConfigurationRevision,
    AgentConfigurationRevisionFormatVersion,
    AgentExecutionCapability,
    AgentExecutorRevision,
    AgentRole,
    AuthMode,
    AuthProfileRevision,
    ProviderId,
)
from atelier2.contracts.artifacts import Artifact
from atelier2.contracts.orders import ArtifactOrderValue
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.contracts.run_projections import NodeState
from atelier2.contracts.runs import RunId, RunState, WorkflowRevision
from atelier2.contracts.schemas_v3 import (
    InstanceAccepted,
    InstanceRefused,
    SchemaAccepted,
    read_instance_document,
    read_schema_document,
)
from atelier2.ports.agent_configurations import (
    AgentConfigurationRevisionCreated,
    AuthProfileRevisionCreated,
)
from atelier2.ports.artifacts import ArtifactCreated, ArtifactExisting
from atelier2.ports.durable_runs import (
    AuthoredOrder,
    DurableRunCreated,
    DurableV3StartInputRefused,
    StartPublishedRunRequestV3,
    V3InputRefusal,
)
from atelier2.ports.published_revisions import (
    PublishedRevisionCreated,
    PublishedRevisionExisting,
)
from atelier2.ports.run_queries import NodeDetailFound
from tests.scenarios.agents import (
    RecordingAgentExecutorFactoryV2,
    publish_checked_model_registry,
)
from tests.scenarios.api import durable_queries
from tests.scenarios.durable_state import (
    canonical_loopback_effects,
    canonical_runtime_settings,
)

WORKFLOWS_DIRECTORY = Path(__file__).parents[2] / "workflows"
CODE_REVIEW_DOCUMENT = (WORKFLOWS_DIRECTORY / "code-review.yaml").read_bytes()
TEXT_SCHEMA = PublishedRevision(
    RevisionKind.SCHEMA,
    (WORKFLOWS_DIRECTORY / "schemas" / "nonempty_string.json").read_bytes(),
)
RESULT_SCHEMA = PublishedRevision(
    RevisionKind.SCHEMA,
    (WORKFLOWS_DIRECTORY / "schemas" / "code_review_result.json").read_bytes(),
)
DIFF_TEXT = "diff --git a/app.py b/app.py\n+print('reviewed')\n"
QUESTIONS_TEXT = "Does this change preserve the review contract?"
CONTEXT_TEXT = "The review contract is the only authority."
# `TEXT_SCHEMA` (`nonempty_string.json`) is a `type: string` schema: since
# #1091 an authored order's raw bytes ARE its text, with no JSON-quoting
# layer (`schemas_v3.read_authored_instance_document`).
DIFF = DIFF_TEXT.encode()
QUESTIONS = QUESTIONS_TEXT.encode()
CONTEXT = CONTEXT_TEXT.encode()
REVIEW = {
    "findings": [
        {
            "file": "app.py",
            "line": 1,
            "severity": "low",
            "text": "The change is safe to approve.",
        }
    ],
    "verdict": "approve",
}
ANSWER = json.dumps(REVIEW, ensure_ascii=False).encode()
REFUSED_REVIEWS = {
    "finding missing its file": {
        "findings": [
            {
                "line": 1,
                "severity": "low",
                "text": "A finding must name its file.",
            }
        ],
        "verdict": "revise",
    },
    "unknown verdict": {
        "findings": [],
        "verdict": "pass",
    },
}

RESULT_SCHEMA_CASES = {
    "revise-without-findings": ({"findings": [], "verdict": "revise"}, False),
    "revise-with-one-finding": (
        {"findings": REVIEW["findings"], "verdict": "revise"},
        True,
    ),
    "cannot-judge-without-reason": (
        {"findings": [], "verdict": "cannot-judge"},
        False,
    ),
    "cannot-judge-with-empty-reason": (
        {"findings": [], "verdict": "cannot-judge", "reason": ""},
        False,
    ),
    "cannot-judge-with-reason": (
        {
            "findings": [],
            "verdict": "cannot-judge",
            "reason": "Insufficient evidence to judge.",
        },
        True,
    ),
    "approve-without-findings": ({"findings": [], "verdict": "approve"}, True),
}


def runtime_over(
    root: Path, provider: RecordingAgentExecutorFactoryV2, scratch_root: Path
) -> DbosRuntime:
    return DbosRuntime(
        canonical_runtime_settings(root, "code-review-test", scratch_root),
        canonical_loopback_effects(root),
        (provider,),
    )


@pytest.fixture
def provider(request: pytest.FixtureRequest) -> RecordingAgentExecutorFactoryV2:
    return RecordingAgentExecutorFactoryV2(
        "exact", "exact/v1", "exact-op", getattr(request, "param", ANSWER)
    )


@pytest.fixture
def runtime(
    tmp_path: Path, provider: RecordingAgentExecutorFactoryV2
) -> Iterator[DbosRuntime]:
    with tempfile.TemporaryDirectory(
        prefix="atelier2-code-review-scratch-", dir="/var/tmp"
    ) as directory:
        started = runtime_over(tmp_path, provider, Path(directory))
        started.initialize_storage()
        try:
            yield started
        finally:
            started.close()


def publish(runtime: DbosRuntime) -> tuple[WorkflowRevision, AgentBindingSet]:
    store = DbosCatalogStore(runtime.engine)
    for revision in (TEXT_SCHEMA, RESULT_SCHEMA):
        result = store.publish_revision(revision)
        assert isinstance(
            result, (PublishedRevisionCreated, PublishedRevisionExisting)
        ), result
    catalog = DbosAgentConfigurationCatalog(
        runtime.engine, runtime.agent_executor_registry
    )
    auth = AuthProfileRevision("max", 1, ProviderId("exact"), AuthMode.SUBSCRIPTION)
    assert isinstance(
        catalog.publish_auth_profile_revision(auth), AuthProfileRevisionCreated
    )
    configuration = AgentConfigurationRevision(
        "opus",
        auth.revision_hash,
        AgentExecutorRevision("exact/v1"),
        AgentExecutionCapability.HEADLESS,
        AgentConfigurationRevisionFormatVersion.V2,
    )
    assert isinstance(
        catalog.publish_agent_configuration_revision(configuration),
        AgentConfigurationRevisionCreated,
    )
    publish_checked_model_registry(
        runtime.engine, ProviderId("exact"), (configuration,)
    )
    workflow = WorkflowRevision(CODE_REVIEW_DOCUMENT)
    DbosWorkflowRevisionPublisher(runtime.engine).publish(workflow)
    return workflow, AgentBindingSet(
        (AgentBinding(AgentRole("reviewer"), configuration.revision_hash),)
    )


def artifact_order(runtime: DbosRuntime, name: str, content: bytes) -> AuthoredOrder:
    published = DbosArtifactStore(runtime.engine).publish_artifact(Artifact(content))
    assert isinstance(published, (ArtifactCreated, ArtifactExisting)), published
    return AuthoredOrder(name, ArtifactOrderValue(published.artifact.artifact_hash))


def wait_for_completion(runtime: DbosRuntime, run_id: RunId) -> None:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        with runtime.engine.connect() as connection:
            state = connection.scalar(
                sa.select(runs.c.state).where(runs.c.run_id == run_id.value)
            )
        if state == RunState.COMPLETED.value:
            return
        time.sleep(0.025)
    raise AssertionError("code review run did not complete")


def test_a_code_review_without_context_is_refused_before_a_run_exists(
    runtime: DbosRuntime,
) -> None:
    workflow, bindings = publish(runtime)
    refused = DbosDurableRunStarter(
        runtime.engine, runtime.settings, runtime.agent_executor_registry
    ).start_published(
        StartPublishedRunRequestV3(
            RunId("v3/code-review-without-context"),
            workflow.revision_hash,
            bindings,
            orders=(
                artifact_order(runtime, "diff", DIFF),
                artifact_order(runtime, "review_questions", QUESTIONS),
            ),
        )
    )
    assert isinstance(refused, DurableV3StartInputRefused), refused
    assert refused.name == "context"
    assert refused.refusal is V3InputRefusal.MISSING
    with runtime.engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(runs)) == 0


def test_a_code_review_round_trips_its_orders_to_an_object_result(
    runtime: DbosRuntime, provider: RecordingAgentExecutorFactoryV2
) -> None:
    schema = read_schema_document(RESULT_SCHEMA.document)
    assert isinstance(schema, SchemaAccepted), schema
    assert isinstance(read_instance_document(ANSWER, schema), InstanceAccepted)

    workflow, bindings = publish(runtime)
    run_id = RunId("v3/code-review-object")
    created = DbosDurableRunStarter(
        runtime.engine, runtime.settings, runtime.agent_executor_registry
    ).start_published(
        StartPublishedRunRequestV3(
            run_id,
            workflow.revision_hash,
            bindings,
            orders=(
                artifact_order(runtime, "diff", DIFF),
                artifact_order(runtime, "review_questions", QUESTIONS),
                artifact_order(runtime, "context", CONTEXT),
            ),
        )
    )
    assert isinstance(created, DurableRunCreated), created

    runtime.launch()
    wait_for_completion(runtime, run_id)

    assert provider.opened is not None
    handed = provider.opened.requests[0].job_bytes
    assert DIFF_TEXT.encode() in handed
    assert QUESTIONS_TEXT.encode() in handed
    assert CONTEXT_TEXT.encode() in handed
    detail = durable_queries(runtime.engine).get_node_detail(run_id, "review")
    assert isinstance(detail, NodeDetailFound), detail
    assert detail.detail.state is NodeState.SUCCEEDED
    assert detail.detail.answer is not None
    assert detail.detail.answer.value == ANSWER


@pytest.mark.parametrize(
    ("payload", "admitted"),
    RESULT_SCHEMA_CASES.values(),
    ids=RESULT_SCHEMA_CASES.keys(),
)
def test_the_code_review_result_schema_admits_only_honest_verdicts(
    payload: dict[str, object], admitted: bool
) -> None:
    schema = read_schema_document(RESULT_SCHEMA.document)
    assert isinstance(schema, SchemaAccepted), schema
    verdict = read_instance_document(json.dumps(payload).encode(), schema)
    if admitted:
        assert isinstance(verdict, InstanceAccepted), verdict
    else:
        assert isinstance(verdict, InstanceRefused), verdict


@pytest.mark.parametrize(
    "provider",
    [
        pytest.param(json.dumps(review).encode(), id=case)
        for case, review in REFUSED_REVIEWS.items()
    ],
    indirect=True,
)
def test_a_code_review_object_the_schema_refuses_never_becomes_a_success(
    runtime: DbosRuntime, provider: RecordingAgentExecutorFactoryV2
) -> None:
    workflow, bindings = publish(runtime)
    run_id = RunId("v3/code-review-refused")
    created = DbosDurableRunStarter(
        runtime.engine, runtime.settings, runtime.agent_executor_registry
    ).start_published(
        StartPublishedRunRequestV3(
            run_id,
            workflow.revision_hash,
            bindings,
            orders=(
                artifact_order(runtime, "diff", DIFF),
                artifact_order(runtime, "review_questions", QUESTIONS),
                artifact_order(runtime, "context", CONTEXT),
            ),
        )
    )
    assert isinstance(created, DurableRunCreated), created

    runtime.launch()
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        with runtime.engine.connect() as connection:
            if (
                connection.scalar(
                    sa.select(runs.c.state).where(runs.c.run_id == run_id.value)
                )
                == RunState.FAILED.value
            ):
                break
        time.sleep(0.025)
    else:
        raise AssertionError("code review refusal did not fail the run")

    detail = durable_queries(runtime.engine).get_node_detail(run_id, "review")
    assert isinstance(detail, NodeDetailFound), detail
    assert detail.detail.state is NodeState.FAILED
    assert detail.detail.answer is None
    assert detail.detail.refusal is not None
    assert "output-schema-refused" in detail.detail.refusal
