"""The `atelier serve` deployments beyond one provider's basics.

`tests/host/test_local_host.py` owns every other `HostSettings`/
`compose_application` behavior; this module owns the Claude atelier-doors
arming (`#7`): the serve flag that arms and always attests the doors
executor, its refusal without the Claude deployment, its absence
leaving the doors unserved, and the doors executor's own registry entry
(capability, carrier, startability) once armed. It also owns
`_discover_grok_models`/`_discover_codex_models`'
credential isolation (`#1009`): no test elsewhere exercises those two
host-level probes, so their own private-directory discipline is proven here
rather than left unproven."""

from __future__ import annotations

import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa

from atelier2.adapters.claude_subscription import (
    CLAUDE_ATELIER_DOORS_EXECUTOR_KEY,
    CLAUDE_SUBSCRIPTION_EXECUTOR_KEY,
    ClaudeSubscriptionSettings,
)
from atelier2.adapters.codex_subscription import (
    CodexSandboxMode,
    CodexSubscriptionSettings,
)
from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.queue_projection_store import DbosQueueProjectionStore
from atelier2.adapters.dbos.run_store import load_run_orders
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import runs
from atelier2.adapters.grok_subscription import GrokSubscriptionSettings
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.contracts.agents import (
    AgentConfigurationRevision,
    AgentConfigurationRevisionFormatVersion,
    AgentExecutionCapability,
    AuthMode,
    AuthProfileRevision,
    ProviderId,
)
from atelier2.contracts.catalog_v3 import (
    CatalogActivatedAt,
    CatalogActor,
    CatalogLineageDisplayName,
)
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.host_configuration import ProjectId
from atelier2.contracts.pages import MAXIMUM_PAGE_ITEMS
from atelier2.contracts.provider_probe_receipts import (
    ProviderProbeReceipt,
    ProviderProbeResult,
    ProviderProbeVectorId,
)
from atelier2.contracts.queue_projection import (
    ConfirmQueueProposal,
    PlanQueueItem,
    QueueAdmissionRationale,
    QueueAutomationDisposition,
    QueueItemAdmitted,
    QueueItemProposed,
    QueueItemTrackerObservation,
    QueuePriorityRank,
    QueueProjectionRevision,
    QueueProjectPolicyRevision,
    QueueProposal,
    TrackerItemReference,
    WorkItemReference,
)
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.contracts.runs import (
    RunId,
    RunState,
    WorkflowRevision,
    WorkflowRevisionHash,
)
from atelier2.contracts.when import RecordedAt, recorded_instant
from atelier2.contracts.work_items import (
    WORK_ITEM_ORDER_SCHEMA_DOCUMENT,
    WORK_ITEM_ORDER_SCHEMA_REVISION,
    ObservedWorkItemRevision,
    WorkItemChangeMarker,
    WorkItemKind,
    read_work_item_order_document,
)
from atelier2.host import main
from atelier2.host.serving import (
    HostSettings,
    _discover_codex_models,
    _discover_grok_models,
    compose_application,
)
from atelier2.ports.agent_configurations import AgentConfigurationRevisionPage
from atelier2.ports.agent_executions import AgentExecutorCarrier
from atelier2.ports.issue_observation import (
    ObservedOpenTrackerItem,
    OpenTrackerItemsObserved,
    WorkItemRevisionObserved,
)
from atelier2.ports.published_revisions import CatalogLineageFounded
from atelier2.ports.queue_projection import QueueItemsPage, QueueItemsReconciled
from tests.integration.test_claude_atelier_doors import doors_deployment, doors_flags
from tests.integration.test_claude_subscription import (
    INTROSPECTING_CLAUDE,
    parsing_claude,
)
from tests.scenarios.agents import (
    agent_scratch_root,
    claude_subscription_deployment,
    publish_checked_model_registry,
)
from tests.scenarios.issue_observation import FakeTrackerItemSource
from tests.scenarios.runs import publish_revision
from tests.scenarios.workflows import graph_input_wait_line


def _frontend(tmp_path: Path) -> Path:
    frontend = tmp_path / "frontend"
    (frontend / "assets").mkdir(parents=True)
    (frontend / "index.html").write_text("index")
    return frontend


def _settings(tmp_path: Path) -> HostSettings:
    return HostSettings(
        database_path=tmp_path / "durable.sqlite",
        effect_store_path=tmp_path / "effects.sqlite",
        effect_adapter_revision="loopback-v1",
        effect_destination="local",
        application_version="composition-test",
        source_commit="c" * 40,
        source_tree="tree",
        frontend_dist=_frontend(tmp_path),
        # Isolated per test, never the operator's real XDG state directory:
        # a stray real receipt must never make an unrelated test's gate
        # answer depend on what happens to sit on the machine running it.
        provider_probe_receipt_directory=tmp_path / "provider-probes",
    )


def _serve_command(tmp_path: Path, *extra: str) -> list[str]:
    return [
        "serve",
        "--database",
        str(tmp_path / "durable.sqlite"),
        "--effect-store",
        str(tmp_path / "effects.sqlite"),
        "--effect-adapter-revision",
        "loopback-v1",
        "--effect-destination",
        "local",
        "--application-version",
        "serve-cli-test",
        "--source-commit",
        "c" * 40,
        "--source-tree",
        "tree",
        "--frontend-dist",
        str(_frontend(tmp_path)),
        *extra,
    ]


def _captured_serve_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, HostSettings]:
    captured: dict[str, HostSettings] = {}

    def fake_serve(settings: HostSettings) -> None:
        captured["settings"] = settings

    monkeypatch.setattr("atelier2.host.serve", fake_serve)
    return captured


def _doors_capable_claude(tmp_path: Path) -> ClaudeSubscriptionSettings:
    """A fake Claude whose parser reads the whole doors vector.

    The reference deployment exists only to ask the production composition
    which flags that vector carries, so the fake refuses exactly the flags a
    real release would not know -- the shape `attest_atelier_doors_invocation`
    insists on.
    """

    reference = doors_deployment(tmp_path, "doors-reference", INTROSPECTING_CLAUDE)
    directory = tmp_path / "claude-deployment"
    directory.mkdir()
    return claude_subscription_deployment(
        directory, parsing_claude(doors_flags(reference))
    )


def _claude_serve_flags(
    tmp_path: Path, claude: ClaudeSubscriptionSettings, *extra: str
) -> list[str]:
    return [
        "--agent-scratch-root",
        str(agent_scratch_root(tmp_path)),
        "--claude-executable",
        str(claude.executable),
        "--claude-credential-directory",
        str(claude.credential_directory),
        *extra,
    ]


def test_the_serve_flag_arms_and_attests_the_claude_atelier_doors_executor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One named flag beside the Claude deployment serves the doors executor.

    Arming always attests: the command launches the exact door-bearing vector
    once, so the composed settings carry no start refusal and the composition
    registers the doors executor beside its siblings.
    """

    claude = _doors_capable_claude(tmp_path)
    monkeypatch.setenv("PATH", claude.search_path)
    captured = _captured_serve_settings(monkeypatch)

    command = _serve_command(
        tmp_path, *_claude_serve_flags(tmp_path, claude, "--claude-atelier-doors")
    )
    assert main(command) == 0

    served = captured["settings"]
    assert served.claude_atelier_doors
    assert served.claude_atelier_doors_start_refusal is None
    _app, runtime = compose_application(served)
    try:
        assert CLAUDE_ATELIER_DOORS_EXECUTOR_KEY in runtime.agent_executor_registry.keys
    finally:
        runtime.close()


def test_an_unattestable_doors_vector_names_its_refusal_and_still_serves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An executable that cannot start the doors vector does not kill serve."""

    directory = tmp_path / "claude-deployment"
    directory.mkdir()
    claude = claude_subscription_deployment(directory, parsing_claude(()))
    monkeypatch.setenv("PATH", claude.search_path)
    captured = _captured_serve_settings(monkeypatch)

    command = _serve_command(
        tmp_path, *_claude_serve_flags(tmp_path, claude, "--claude-atelier-doors")
    )
    assert main(command) == 0

    served = captured["settings"]
    assert served.claude_atelier_doors
    assert served.claude_atelier_doors_start_refusal is not None
    assert "atelier-doors" in served.claude_atelier_doors_start_refusal


def test_arming_the_doors_without_a_claude_deployment_is_refused_at_the_command_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as refusal:
        main(_serve_command(tmp_path, "--claude-atelier-doors"))

    assert refusal.value.code == 2
    assert "--claude-atelier-doors" in capsys.readouterr().err


def test_serve_without_the_doors_flag_leaves_the_doors_unarmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude = _doors_capable_claude(tmp_path)
    monkeypatch.setenv("PATH", claude.search_path)
    captured = _captured_serve_settings(monkeypatch)

    assert main(_serve_command(tmp_path, *_claude_serve_flags(tmp_path, claude))) == 0

    served = captured["settings"]
    assert not served.claude_atelier_doors
    assert served.claude_atelier_doors_start_refusal is None


INERT_CLAUDE = "raise SystemExit(0)\n"


def _claude_deployment(tmp_path: Path) -> ClaudeSubscriptionSettings:
    deployment = tmp_path / "claude-deployment"
    deployment.mkdir()
    return claude_subscription_deployment(deployment, INERT_CLAUDE)


def _doors_armed_settings(
    tmp_path: Path, claude_atelier_doors: bool = True
) -> HostSettings:
    frontend = tmp_path / "frontend"
    if not frontend.is_dir():
        (frontend / "assets").mkdir(parents=True)
        (frontend / "index.html").write_text("index")
    return HostSettings(
        database_path=tmp_path / "durable.sqlite",
        effect_store_path=tmp_path / "effects.sqlite",
        effect_adapter_revision="loopback-v1",
        effect_destination="local",
        application_version="composition-test",
        source_commit="c" * 40,
        source_tree="tree",
        frontend_dist=frontend,
        # Isolated per test, never the operator's real XDG state directory:
        # a stray real receipt must never make an unrelated test's gate
        # answer depend on what happens to sit on the machine running it.
        provider_probe_receipt_directory=tmp_path / "provider-probes",
        agent_scratch_root=agent_scratch_root(tmp_path),
        claude_subscription=_claude_deployment(tmp_path),
        claude_atelier_doors=claude_atelier_doors,
    )


def test_the_doors_executor_is_served_only_where_it_was_armed(
    tmp_path: Path,
) -> None:
    """Naming a Claude executable grants a tool-free call and nothing more.

    The doors executor lets a node's own process start real billed catalog
    runs, so it is a grant of its own: it appears in the registry only where
    the operator armed it, with the tool capability and the local carrier.
    """

    _app, runtime = compose_application(_doors_armed_settings(tmp_path))
    try:
        registry = runtime.agent_executor_registry
        assert CLAUDE_ATELIER_DOORS_EXECUTOR_KEY in registry.keys
        assert registry.declared_capabilities(
            CLAUDE_ATELIER_DOORS_EXECUTOR_KEY
        ) == frozenset({AgentExecutionCapability.HEADLESS_WITH_TOOLS})
        assert (
            registry.carrier(CLAUDE_ATELIER_DOORS_EXECUTOR_KEY)
            is AgentExecutorCarrier.LOCAL_PROCESS
        )
    finally:
        runtime.close()


def test_an_unarmed_claude_deployment_offers_no_doors_executor(
    tmp_path: Path,
) -> None:
    _app, runtime = compose_application(
        _doors_armed_settings(tmp_path, claude_atelier_doors=False)
    )
    try:
        assert runtime.agent_executor_registry.keys == frozenset(
            {CLAUDE_SUBSCRIPTION_EXECUTOR_KEY}
        )
    finally:
        runtime.close()


def test_arming_the_doors_without_a_claude_deployment_is_refused(
    tmp_path: Path,
) -> None:
    frontend = tmp_path / "frontend"
    (frontend / "assets").mkdir(parents=True)
    (frontend / "index.html").write_text("index")

    with pytest.raises(ValueError, match="third executor"):
        HostSettings(
            database_path=tmp_path / "durable.sqlite",
            effect_store_path=tmp_path / "effects.sqlite",
            effect_adapter_revision="loopback-v1",
            effect_destination="local",
            application_version="composition-test",
            source_commit="commit",
            source_tree="tree",
            frontend_dist=frontend,
            claude_atelier_doors=True,
        )


def test_the_published_conductor_configuration_is_startable_where_doors_are_armed(
    tmp_path: Path,
) -> None:
    """The binding half of phase B: a config naming the doors revision starts.

    The catalog judges startability against the composed registry, so this is
    the production answer to "can a conductor node be bound": yes where the
    doors executor is armed, and the same configuration would be unstartable in
    a composition without it.
    """

    settings = _doors_armed_settings(tmp_path)
    _app, runtime = compose_application(settings)
    runtime.initialize_storage()
    try:
        catalog = DbosAgentConfigurationCatalog(
            runtime.engine, runtime.agent_executor_registry
        )
        auth = AuthProfileRevision(
            "max", 1, ProviderId("anthropic"), AuthMode.SUBSCRIPTION
        )
        catalog.publish_auth_profile_revision(auth)
        configuration = AgentConfigurationRevision(
            "claude-opus-4-6",
            auth.revision_hash,
            CLAUDE_ATELIER_DOORS_EXECUTOR_KEY.executor_revision,
            AgentExecutionCapability.HEADLESS_WITH_TOOLS,
            AgentConfigurationRevisionFormatVersion.V2,
        )
        catalog.publish_agent_configuration_revision(configuration)
        publish_checked_model_registry(
            runtime.engine, ProviderId("anthropic"), (configuration,)
        )

        assert settings.provider_probe_receipt_directory is not None
        settings.provider_probe_receipt_directory.mkdir(parents=True, exist_ok=True)
        now = datetime.now(UTC)
        assert runtime.settings.provider_probe_receipt_provider_layer_digest is not None
        receipt = ProviderProbeReceipt(
            ProviderProbeVectorId("atelier-doors-claude-opus-4-6"),
            configuration.revision_hash,
            WorkflowRevisionHash("b" * 64),
            runtime.settings.provider_probe_receipt_provider_layer_digest,
            settings.source_commit,
            recorded_instant(now - timedelta(minutes=1)),
            recorded_instant(now + timedelta(hours=1)),
            ProviderProbeResult.SUCCEEDED,
            RunId("provider-canary/atelier-doors-fixture"),
            terminal_hash=Sha256Hash("d" * 64),
        )
        (
            settings.provider_probe_receipt_directory / "claude-opus-4-6.json"
        ).write_bytes(receipt.canonical_bytes())

        page = catalog.list_agent_configuration_revisions(None, 10)

        assert isinstance(page, AgentConfigurationRevisionPage)
        listed = {item.revision.revision_hash: item.startable for item in page.items}
        assert listed[configuration.revision_hash] is True
    finally:
        runtime.close()


def _write_executable(path: Path, source: str) -> Path:
    path.write_text("#!/usr/bin/env python3\n" + source)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


# Stands in for grok's `models` subcommand: rather than listing real models,
# it reports the private home this probe was launched with and the auth.json
# bytes it can read there -- exactly what `_discover_grok_models`'s own
# isolation depends on, without a billed CLI.
DISCOVERING_GROK = """
import os, sys
from pathlib import Path

home = os.environ.get("GROK_HOME", "")
auth = (Path(home) / "auth.json").read_text() if home else ""
print("Available models:")
print(f"* HOME={home}")
print(f"* AUTH={auth}")
"""


def _grok_discovery_deployment(
    tmp_path: Path, source: str = DISCOVERING_GROK
) -> GrokSubscriptionSettings:
    executable = _write_executable(tmp_path / "grok", source)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    credentials = tmp_path / "grok-home"
    credentials.mkdir()
    authentication = credentials / "auth.json"
    authentication.write_bytes(b"{}")
    authentication.chmod(0o600)
    return GrokSubscriptionSettings(
        executable, workspace, credentials, os.environ.get("PATH", "/usr/bin")
    )


def test_grok_model_discovery_never_runs_inside_the_operators_credential_directory(
    tmp_path: Path,
) -> None:
    """The live deployment served `--grok-credential-directory` here directly.

    `_discover_grok_models` used to name that operator directory as `HOME` and
    `GROK_HOME` for a spawned process; this proves it now spawns into a
    private, disposable copy instead, that the copy carries the material the
    probe needs, and that the copy is already gone once the probe returns.
    """

    settings = _grok_discovery_deployment(tmp_path)

    models = _discover_grok_models(settings, timeout_seconds=5.0)

    reported = dict(entry.split("=", 1) for entry in models)
    assert reported["AUTH"] == "{}"
    assert reported["HOME"] != str(settings.credential_directory)
    assert not Path(reported["HOME"]).exists()


# A `models` subcommand that refuses outright, after recording the private
# home it was launched with beside itself -- the one channel left once
# `_discover_grok_models` never returns a value to read the home back from.
DISCOVERING_GROK_FAILING = """
import os, sys
from pathlib import Path

home = os.environ.get("GROK_HOME", "")
(Path(sys.argv[0]).resolve().parent / "observed-home.txt").write_text(home)
sys.stderr.write("synthetic Grok refusal\\n")
raise SystemExit(1)
"""


def test_grok_model_discovery_removes_its_private_home_after_a_failing_run(
    tmp_path: Path,
) -> None:
    settings = _grok_discovery_deployment(tmp_path, DISCOVERING_GROK_FAILING)

    with pytest.raises(ValueError, match="Grok model discovery failed"):
        _discover_grok_models(settings, timeout_seconds=5.0)

    reported_home = (tmp_path / "observed-home.txt").read_text()
    assert reported_home != str(settings.credential_directory)
    assert not Path(reported_home).exists()


# Stands in for `codex app-server`'s JSON-RPC handshake: rather than listing
# real models, it reports the private home this probe was launched with and
# the auth.json bytes it can read there -- exactly what
# `_discover_codex_models`'s own isolation depends on, without a billed CLI.
DISCOVERING_CODEX = """
import json, sys, os
from pathlib import Path


def send(message):
    sys.stdout.write(json.dumps(message) + "\\n")
    sys.stdout.flush()


home = os.environ.get("CODEX_HOME", "")
auth = (Path(home) / "auth.json").read_text() if home else ""

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    message = json.loads(line)
    if message.get("method") == "initialize":
        send({"jsonrpc": "2.0", "id": message["id"], "result": {}})
    elif message.get("method") == "model/list":
        send(
            {
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {
                    "data": [
                        {"model": f"HOME={home}"},
                        {"model": f"AUTH={auth}"},
                    ]
                },
            }
        )
"""


def _codex_discovery_deployment(
    tmp_path: Path, source: str = DISCOVERING_CODEX
) -> CodexSubscriptionSettings:
    executable = _write_executable(tmp_path / "codex", source)
    credentials = tmp_path / "codex-home"
    credentials.mkdir()
    authentication = credentials / "auth.json"
    authentication.write_bytes(b"{}")
    authentication.chmod(0o600)
    return CodexSubscriptionSettings(
        executable,
        credentials,
        os.environ.get("PATH", "/usr/bin"),
        CodexSandboxMode.READ_ONLY,
    )


def test_codex_model_discovery_never_runs_inside_the_operators_credential_directory(
    tmp_path: Path,
) -> None:
    """`_discover_codex_models` used to name the operator's own `CODEX_HOME`.

    Unreachable from the live serve command line today, but the same proof as
    Grok's above: the spawned `app-server` process gets a private, disposable
    copy instead, that copy carries the material the probe needs, and it is
    already gone once the probe returns.
    """

    settings = _codex_discovery_deployment(tmp_path)

    models = _discover_codex_models(
        settings, timeout_seconds=5.0, termination_grace_seconds=5.0
    )

    reported = dict(entry.split("=", 1) for entry in models)
    assert reported["AUTH"] == "{}"
    assert reported["HOME"] != str(settings.credential_directory)
    assert not Path(reported["HOME"]).exists()


# An `app-server` that answers the `initialize` handshake with garbage
# JSON-RPC (no `result` field), after recording the private home it was
# launched with beside itself -- the one channel left once
# `_discover_codex_models` never returns a value to read the home back from.
DISCOVERING_CODEX_FAILING = """
import json, sys, os
from pathlib import Path

home = os.environ.get("CODEX_HOME", "")
(Path(sys.argv[0]).resolve().parent / "observed-home.txt").write_text(home)

message = json.loads(sys.stdin.readline())
sys.stdout.write(
    json.dumps({"jsonrpc": "2.0", "id": message["id"], "malformed": True}) + "\\n"
)
sys.stdout.flush()
"""


def test_codex_model_discovery_removes_its_private_home_after_a_failing_run(
    tmp_path: Path,
) -> None:
    settings = _codex_discovery_deployment(tmp_path, DISCOVERING_CODEX_FAILING)

    with pytest.raises(TypeError, match="Codex model discovery returned no result"):
        _discover_codex_models(
            settings, timeout_seconds=5.0, termination_grace_seconds=5.0
        )

    reported_home = (tmp_path / "observed-home.txt").read_text()
    assert reported_home != str(settings.credential_directory)
    assert not Path(reported_home).exists()


_QUEUE_STARTED_PROJECT = ProjectId("studio")


def test_a_queue_started_run_carries_the_admitted_items_tracker_reference(
    tmp_path: Path,
) -> None:
    """A2 (`#1145`): a run the queue sweep starts is pinned to the item it is about.

    Through `DbosRuntime` composed exactly as `compose_application` composes
    it -- `tracker_item_source` injected the same way `_effect_adapters` is --
    because a genuine `compose_application` connected to a live GitHub project
    would reach the real network for the sweep's own tracker read, and this
    slice's subject is the queue's order-filling decision, not the GitHub
    adapter's HTTP contract (`tests/integration/test_github_observation.py`
    owns that). The bound document declares one `graph_input` pinned to the
    work-item schema, the same shape `workflows/issue-to-pr.yaml` declares,
    without that file's agent role -- proving `advance_queue`'s own decision
    needs no agent-configuration or model-registry setup beside it.
    """

    tracker_reference = TrackerItemReference("gh:9001")
    observed_at = RecordedAt("2026-09-04T09:00:00Z")
    revision = ObservedWorkItemRevision(
        tracker_reference,
        WorkItemKind.ISSUE,
        b"push the candidate before the pull request opens",
        WorkItemChangeMarker('W/"9001"'),
        observed_at,
    )
    # A served project bound to a tracker is exactly the composition the queue
    # sweep now imports through on every tick (#1451), so the fake must answer
    # the same open-items read the real one would -- the run's own item, or
    # the sweep's import would retire the row this test seeds by hand below.
    tracker = FakeTrackerItemSource(
        open_items_answer=OpenTrackerItemsObserved(
            (ObservedOpenTrackerItem(tracker_reference, "push before the pr", ()),),
            observed_at,
        ),
        snapshot_answer=WorkItemRevisionObserved(revision),
    )
    project_root = tmp_path / "operator-project"
    project_root.mkdir()
    runtime = DbosRuntime(
        DbosRuntimeSettings(
            tmp_path / "durable.sqlite",
            "queue-carries-item-test",
            project_id=_QUEUE_STARTED_PROJECT,
            bootstrap_project_root=project_root,
        ),
        LoopbackEffectAdapterFactory(
            tmp_path / "external.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        ),
        tracker_item_source=tracker,
    )
    try:
        engine = runtime.engine
        catalog = DbosCatalogStore(engine)
        work_item_schema = PublishedRevision(
            RevisionKind.SCHEMA, WORK_ITEM_ORDER_SCHEMA_DOCUMENT
        )
        approval_schema = PublishedRevision(RevisionKind.SCHEMA, b"true")
        document = graph_input_wait_line(WORK_ITEM_ORDER_SCHEMA_REVISION.value)
        published = PublishedRevision(RevisionKind.WORKFLOW, document)
        for revision_to_publish in (work_item_schema, approval_schema, published):
            catalog.publish_revision(revision_to_publish)
        # The catalog registry (lineage, name resolution) and the durable
        # workflow-revision store the starter reads by hash are two owners of
        # the same bytes (ADR 0007); a start needs both published.
        publish_revision(engine, WorkflowRevision(document))
        founded = catalog.found_lineage(
            published,
            CatalogLineageDisplayName("queue-carries-item"),
            CatalogActor("operator"),
            CatalogActivatedAt("2026-09-04T09:00:00Z"),
        )
        assert isinstance(founded, CatalogLineageFounded)

        queue = DbosQueueProjectionStore(engine)
        item_reference = WorkItemReference(_QUEUE_STARTED_PROJECT, tracker_reference)
        queue.put_policy(
            QueueProjectPolicyRevision(_QUEUE_STARTED_PROJECT, 1, 1, None), 0
        )
        reconciled = queue.reconcile_open_items(
            _QUEUE_STARTED_PROJECT,
            (
                (
                    item_reference,
                    QueueItemTrackerObservation("push before the pr", observed_at),
                ),
            ),
            observed_at,
        )
        assert isinstance(reconciled, QueueItemsReconciled)
        proposed = queue.plan(
            PlanQueueItem(
                item_reference,
                QueueProposal(
                    QueuePriorityRank(1),
                    founded.lineage.lineage_id,
                    (),
                    QueueAutomationDisposition.AUTOMATION_AUTHORIZED,
                    1,
                ),
                QueueProjectionRevision(0),
            )
        )
        assert isinstance(proposed, QueueItemProposed)
        admitted = queue.confirm(
            ConfirmQueueProposal(
                item_reference,
                proposed.revision,
                QueueAdmissionRationale("operator approved the inspected proposal"),
            )
        )
        assert isinstance(admitted, QueueItemAdmitted)

        runtime.launch()

        page = queue.list_items(None, MAXIMUM_PAGE_ITEMS)
        assert isinstance(page, QueueItemsPage)
        (snapshot,) = [
            item for item in page.items if item.item_reference == item_reference
        ]
        assert snapshot.launch_binding is not None
        run_id = snapshot.launch_binding.run_id

        with engine.connect() as connection:
            orders_by_run = load_run_orders(connection, [run_id.value])
        (order,) = orders_by_run[run_id.value]
        assert order.schema_revision == WORK_ITEM_ORDER_SCHEMA_REVISION
        decoded = read_work_item_order_document(order.value)
        assert decoded is not None
        assert decoded.reference == tracker_reference

        with engine.connect() as connection:
            run_record = connection.execute(
                sa.select(runs.c.state).where(runs.c.run_id == run_id.value)
            ).scalar_one()
        assert run_record == RunState.STARTED.value
    finally:
        runtime.close()
