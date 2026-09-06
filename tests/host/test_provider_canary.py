from __future__ import annotations

import json
import threading
import types
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from atelier2.api.problems import problem_resource
from atelier2.api.wire.resources import (
    CatalogNameResolutionResource,
    HealthResource,
    NodeDetailResource,
    NodeRailResource,
    RunCancellabilityResource,
    RunResourceV3,
)
from atelier2.contracts.agents import AgentConfigurationRevisionHash
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.provider_probe_receipts import (
    ProviderProbeProblemCode,
    ProviderProbeReceipt,
    ProviderProbeReceiptRefused,
    ProviderProbeResult,
    ProviderProbeVectorId,
    read_provider_probe_receipt,
)
from atelier2.contracts.revisions_v3 import RevisionKind
from atelier2.contracts.run_projections import NodeState
from atelier2.contracts.runs import RunId, WorkflowRevisionHash
from atelier2.contracts.when import RecordedAt
from atelier2.host import main
from atelier2.host.atelier_api_client import AtelierApi
from atelier2.host.provider_canary import (
    PROVIDER_CANARY_DISCOVERY_TIMEOUT_SECONDS,
    PROVIDER_CANARY_HEALTH_WAIT_POLL_INTERVAL_SECONDS,
    PROVIDER_CANARY_MAXIMUM_CONCURRENT_VECTORS,
    PROVIDER_CANARY_MAXIMUM_CONFIGURATION_PAGES,
    PROVIDER_CANARY_MAXIMUM_VECTORS,
    AtelierApiProviderCanaryHttp,
    ProviderCanaryDiscoveryFailed,
    ProviderCanaryHttpRefused,
    ProviderCanaryServerUnavailable,
    ProviderCanarySettings,
    ProviderLayerReceiptOutcome,
    execute_provider_canaries,
    provider_layer_digest,
    write_provider_canary_receipt_atomic,
)

CATALOG_NAME_PREFIX = f"/catalog-revisions/by-name/{RevisionKind.WORKFLOW.value}/"
SOURCE_COMMIT = "a" * 40
CONFIGURATION_HASH = "b" * 64
TERMINAL_HASH = "c" * 64
PUBLIC_RUN_REFERENCE = "run1.cHJvdmlkZXItY2FuYXJ5"
# The canary computes this from the checkout's own adapter files (#1124), the
# same value `execute_provider_canaries` embeds in every receipt it writes.
RUNNING_PROVIDER_LAYER_DIGEST = provider_layer_digest()
FOREIGN_PROVIDER_LAYER_DIGEST = Sha256Hash("9" * 64)
DEFAULT_SUCCESSFUL_RECEIPT_OBSERVED_AT = RecordedAt("2026-08-31T08:00:00Z")


@dataclass
class FakeClock:
    instant: datetime = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
    elapsed: float = 0.0

    def now(self) -> datetime:
        return self.instant + timedelta(seconds=self.elapsed)

    def monotonic(self) -> float:
        return self.elapsed

    def sleep(self, seconds: float) -> None:
        self.elapsed += seconds


@dataclass
class FakeHttp:
    configurations: tuple[dict[str, object], ...]
    workflow_directory: Path
    terminal_state: str = "COMPLETED"
    terminal_after_reads: int = 1
    refusal: str | None = None
    poll_refusal: str | None = None
    server_down: bool = False
    configuration_answer: bytes | None = None
    configuration_unavailable: bool = False
    configuration_refusal: str | None = None
    catalog_unavailable: bool = False
    health_unavailable: bool = False
    health_refusal: str | None = None
    health_answer: bytes | None = None
    health_answers_before_serving: list[bytes | Exception] = field(default_factory=list)
    health_source_commit: str = SOURCE_COMMIT
    configuration_pages: tuple[bytes, ...] | None = None
    node_detail_transcript_events: tuple[dict[str, object], ...] | None = None
    calls: list[tuple[str, str, bytes | None]] = field(default_factory=list)
    workflow_hashes: list[str] = field(default_factory=list)
    run_reads: int = 0
    configuration_page_reads: int = 0
    catalog_hashes: dict[str, str] = field(init=False)

    def __post_init__(self) -> None:
        self.catalog_hashes = {
            name: Sha256Hash.of(
                (self.workflow_directory / f"{name}.yaml").read_bytes()
            ).value
            for name in (
                "provider-canary-headless",
                "provider-canary-workspace-tools",
                "provider-canary-atelier-doors",
            )
        }

    def get(self, path: str, *, timeout_seconds: float) -> bytes:
        assert timeout_seconds > 0
        self.calls.append(("GET", path, None))
        if path == "/health":
            if self.health_answers_before_serving:
                answer = self.health_answers_before_serving.pop(0)
                if isinstance(answer, Exception):
                    raise answer
                return answer
            if self.health_unavailable:
                raise ProviderCanaryServerUnavailable("health timed out")
            if self.health_refusal is not None:
                raise ProviderCanaryHttpRefused(self.health_refusal, "health refused")
            if self.health_answer is not None:
                return self.health_answer
            return (
                HealthResource(
                    status="serving",
                    source_commit=self.health_source_commit,
                    source_tree="d" * 40,
                    serve_started_at="2026-08-31T08:00:00Z",
                )
                .model_dump_json()
                .encode()
            )
        if path.startswith("/agent-configuration-revisions"):
            if self.configuration_unavailable:
                raise ProviderCanaryServerUnavailable("configuration listing timed out")
            if self.configuration_refusal is not None:
                raise ProviderCanaryHttpRefused(
                    self.configuration_refusal, "configuration listing refused"
                )
            if self.configuration_answer is not None:
                return self.configuration_answer
            if self.configuration_pages is not None:
                page = self.configuration_pages[self.configuration_page_reads]
                self.configuration_page_reads += 1
                return page
            return encoded(items=self.configurations, next_after_revision_hash=None)
        if path.startswith(CATALOG_NAME_PREFIX):
            if self.catalog_unavailable:
                raise ProviderCanaryServerUnavailable("workflow catalog timed out")
            name = path.rsplit("/", 1)[-1]
            workflow_hash = self.catalog_hashes[name]
            self.workflow_hashes.append(workflow_hash)
            return (
                CatalogNameResolutionResource(
                    display_name=name,
                    lineage_id="4" * 64,
                    catalog_revision_hash=workflow_hash,
                    revision_number=1,
                )
                .model_dump_json()
                .encode()
            )
        if "/nodes/" in path:
            return node_detail_resource(
                node_id=path.rsplit("/", 1)[-1],
                transcript_events=self.node_detail_transcript_events,
            )
        if self.poll_refusal is not None:
            raise ProviderCanaryHttpRefused(self.poll_refusal, "poll refused")
        self.run_reads += 1
        state = (
            "STARTED"
            if self.run_reads < self.terminal_after_reads
            else self.terminal_state
        )
        return (
            run_resource("provider-canary-run", self.workflow_hashes[-1], state)
            .model_dump_json()
            .encode()
        )

    def post(
        self,
        path: str,
        body: bytes,
        *,
        timeout_seconds: float,
        media_type: str = "application/json",
    ) -> bytes:
        assert timeout_seconds > 0
        del media_type
        self.calls.append(("POST", path, body))
        if path == "/runs":
            if self.server_down:
                raise ProviderCanaryServerUnavailable("connection refused")
            if self.refusal is not None:
                raise ProviderCanaryHttpRefused(self.refusal, "start refused")
            request = json.loads(body)
            return (
                run_resource(
                    request["run_id"], request["workflow_revision_hash"], "STARTED"
                )
                .model_dump_json()
                .encode()
            )
        raise AssertionError(path)


def encoded(**document: object) -> bytes:
    return json.dumps(document).encode()


def run_resource(run_id: str, workflow_hash: str, state: str) -> RunResourceV3:
    terminal = state in {"COMPLETED", "FAILED", "CANCELLED"}
    rail_state = {
        "COMPLETED": NodeState.SUCCEEDED,
        "FAILED": NodeState.FAILED,
        "CANCELLED": NodeState.CANCELLED,
    }.get(state, NodeState.WORKING)
    return RunResourceV3.model_validate(
        {
            "workflow_format_version": 3,
            "run_id": run_id,
            "public_run_reference": PUBLIC_RUN_REFERENCE,
            "workflow_revision_hash": workflow_hash,
            "workflow_name": "provider canary probe",
            "agent_binding_set_hash": "1" * 64,
            "run_configuration_revision_hash": "2" * 64,
            "agent_bindings": (),
            "orders": (),
            "state_version": 1 if terminal else 0,
            "state": state,
            "current_node_id": "probe",
            "current_node_execution_id": "3" * 64,
            "node_rail": (
                NodeRailResource.model_validate(
                    {"node_id": "probe", "state": rail_state.value, "attempt": None}
                ),
            ),
            "cancellation": RunCancellabilityResource(
                cancellable=False,
                reason="already-ended" if terminal else "between-nodes",
                target_node_execution_id=None,
            ),
            "terminal_hash": TERMINAL_HASH if terminal else None,
            "latest_event_cursor": None,
        }
    )


def node_detail_resource(
    *, node_id: str, transcript_events: tuple[dict[str, object], ...] | None
) -> bytes:
    return (
        NodeDetailResource.model_validate(
            {
                "run_id": "5" * 64,
                "public_run_reference": PUBLIC_RUN_REFERENCE,
                "node_id": node_id,
                "state": NodeState.FAILED.value,
                "job_base64": None,
                "job_hash": None,
                "answer": None,
                "provenance": None,
                "refusal": "process-exited-unsuccessfully",
                "started_at": None,
                "ended_at": None,
                "transcript": (
                    None if transcript_events is None else {"events": transcript_events}
                ),
            }
        )
        .model_dump_json()
        .encode()
    )


def configuration(
    *,
    provider: str = "anthropic",
    executor: str = "claude-subscription/v1",
    capability: str = "headless",
    configuration_hash: str = CONFIGURATION_HASH,
    structurally_startable: bool = True,
    model_registered: bool = True,
    has_valid_receipt: bool = True,
    probe_failed: bool = False,
) -> dict[str, object]:
    """A listed item shaped exactly as the server's own precedence computes it.

    Mirrors `AgentConfigurationRevisionListItem`'s fixed order: an
    unavailable executor refuses first, then a registry the cast would
    refuse (a superseded revision), then only a missing or failed live
    receipt -- `probe_failed` distinguishes the two reasons a missing
    receipt can name (#1103); `startable` never needs its own knob, it is
    `not_startable_reason is None`.
    """

    if probe_failed and has_valid_receipt:
        raise ValueError("a proven configuration cannot also carry a failed probe")
    not_startable_reason = (
        "agent-executor-binding-unavailable"
        if not structurally_startable
        else "model-not-registered"
        if not model_registered
        else (
            "provider-probe-failed"
            if probe_failed
            else "provider-probe-receipt-missing"
        )
        if not has_valid_receipt
        else None
    )
    return {
        "model": "provider-model",
        "auth_profile_revision_hash": "e" * 64,
        "executor_revision": executor,
        "provider_id": provider,
        "auth_mode": "subscription",
        "requested_capability": capability,
        "agent_configuration_revision_hash": configuration_hash,
        "startable": not_startable_reason is None,
        "structurally_startable": structurally_startable,
        "not_startable_reason": not_startable_reason,
        "provider_probe_problem_code": "provider-overloaded" if probe_failed else None,
        "provider_probe_observed_at": "2026-01-01T00:00:00Z" if probe_failed else None,
    }


@pytest.fixture
def workflow_directory(tmp_path: Path) -> Path:
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    for name in ("headless", "workspace-tools", "atelier-doors"):
        (workflows / f"provider-canary-{name}.yaml").write_text(
            f"format_version: 3\nname: provider-canary-{name}\n",
            encoding="utf-8",
        )
    return workflows


def settings(workflow_directory: Path, state_directory: Path) -> ProviderCanarySettings:
    return ProviderCanarySettings(
        service_url="http://127.0.0.1:8422",
        workflow_directory=workflow_directory,
        state_directory=state_directory,
        terminal_timeout_seconds=5,
        poll_interval_seconds=1,
    )


def read_receipts(state_directory: Path) -> tuple[ProviderProbeReceipt, ...]:
    receipts: list[ProviderProbeReceipt] = []
    for path in sorted(state_directory.glob("*.json")):
        verdict = read_provider_probe_receipt(path.read_bytes())
        assert not isinstance(verdict, ProviderProbeReceiptRefused)
        receipts.append(verdict)
    return tuple(receipts)


def successful_receipt(
    *,
    provider_layer_digest: Sha256Hash = RUNNING_PROVIDER_LAYER_DIGEST,
    observed_at: RecordedAt = DEFAULT_SUCCESSFUL_RECEIPT_OBSERVED_AT,
) -> ProviderProbeReceipt:
    return ProviderProbeReceipt(
        vector=ProviderProbeVectorId(f"headless-{CONFIGURATION_HASH}"),
        configuration_hash=AgentConfigurationRevisionHash(CONFIGURATION_HASH),
        workflow_hash=WorkflowRevisionHash("d" * 64),
        provider_layer_digest=provider_layer_digest,
        source_commit=SOURCE_COMMIT,
        observed_at=observed_at,
        valid_until=RecordedAt("2026-09-01T10:00:00Z"),
        result=ProviderProbeResult.SUCCEEDED,
        run_reference=RunId("provider-canary/previous-run"),
        terminal_hash=Sha256Hash(TERMINAL_HASH),
    )


def write_live_success(state_directory: Path) -> Path:
    destination = state_directory / f"headless-{CONFIGURATION_HASH}.json"
    write_provider_canary_receipt_atomic(destination, successful_receipt())
    return destination


def test_provider_canary_is_an_operator_cli_subcommand(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as ended:
        main(["provider-canary", "--help"])

    assert ended.value.code == 0
    assert "provider-probe-receipt/v1" in capsys.readouterr().out


def test_each_configured_vector_starts_exactly_one_matching_workflow_and_receipts_it(
    tmp_path: Path, workflow_directory: Path
) -> None:
    http = FakeHttp(
        (
            configuration(),
            configuration(
                provider="xai",
                executor="grok-subscription-tools/v1",
                capability="headless_with_tools",
                configuration_hash="f" * 64,
            ),
        ),
        workflow_directory,
    )
    state_directory = tmp_path / "state"

    report = execute_provider_canaries(
        settings(workflow_directory, state_directory), http=http, clock=FakeClock()
    )

    start_bodies = [
        body for _method, path, body in http.calls if path == "/runs" and body
    ]
    # Every vector starts together (#1124): two concurrent threads race to
    # append their own POST to the shared call log, so only the *set* of
    # starts is a stable assertion, never their order.
    starts_by_configuration = {
        start["agent_bindings"][0]["agent_configuration_revision_hash"]: start
        for start in (json.loads(body) for body in start_bodies)
    }
    resolved = {
        path
        for method, path, _body in http.calls
        if method == "GET" and path.startswith(CATALOG_NAME_PREFIX)
    }
    assert set(starts_by_configuration) == {CONFIGURATION_HASH, "f" * 64}
    assert starts_by_configuration[CONFIGURATION_HASH] == {
        "workflow_format_version": 2,
        "run_id": starts_by_configuration[CONFIGURATION_HASH]["run_id"],
        "workflow_revision_hash": http.workflow_hashes[0],
        "agent_bindings": [
            {
                "role": "provider-canary",
                "agent_configuration_revision_hash": CONFIGURATION_HASH,
            }
        ],
    }
    assert resolved == {
        f"{CATALOG_NAME_PREFIX}provider-canary-headless",
        f"{CATALOG_NAME_PREFIX}provider-canary-workspace-tools",
    }
    # Discovery (health, listing, workflow resolution) is a strictly earlier,
    # single-threaded phase: every workflow-resolution GET precedes every
    # vector's own POST, even though the two POSTs race each other.
    first_start_index = min(
        index
        for index, (method, path, _body) in enumerate(http.calls)
        if method == "POST" and path == "/runs"
    )
    assert all(
        index < first_start_index
        for index, (method, path, _body) in enumerate(http.calls)
        if method == "GET" and path.startswith(CATALOG_NAME_PREFIX)
    )
    assert report.failed == 0
    receipts = read_receipts(state_directory)
    assert len(receipts) == 2
    assert {receipt.result for receipt in receipts} == {ProviderProbeResult.SUCCEEDED}
    assert {
        receipt.terminal_hash.value for receipt in receipts if receipt.terminal_hash
    } == {TERMINAL_HASH}
    assert {receipt.source_commit for receipt in receipts} == {SOURCE_COMMIT}
    assert {receipt.provider_layer_digest for receipt in receipts} == {
        RUNNING_PROVIDER_LAYER_DIGEST
    }
    assert {receipt.valid_until.value for receipt in receipts} == {
        "2026-09-02T10:00:00Z"
    }


@pytest.mark.parametrize(
    "non_serving_answer",
    (
        pytest.param(
            ProviderCanaryServerUnavailable("connection refused"),
            id="connection-failure",
        ),
        pytest.param(
            encoded(
                status="starting", source_commit=SOURCE_COMMIT, source_tree="d" * 40
            ),
            id="non-serving-status",
        ),
        pytest.param(b"not-json", id="unreadable-body"),
    ),
)
def test_a_canary_retries_a_non_serving_health_answer_before_it_tries_any_vector(
    tmp_path: Path,
    workflow_directory: Path,
    non_serving_answer: bytes | Exception,
) -> None:
    state_directory = tmp_path / "state"
    http = FakeHttp((configuration(),), workflow_directory)
    http.health_answers_before_serving = [non_serving_answer]
    clock = FakeClock()

    report = execute_provider_canaries(
        settings(workflow_directory, state_directory), http=http, clock=clock
    )

    assert report.attempted == 1
    assert report.failed == 0
    health_call_indexes = [
        index
        for index, (_method, path, _body) in enumerate(http.calls)
        if path == "/health"
    ]
    start_call_index = next(
        index
        for index, (method, path, _body) in enumerate(http.calls)
        if method == "POST" and path == "/runs"
    )
    assert len(health_call_indexes) == 2
    assert max(health_call_indexes) < start_call_index
    assert clock.elapsed >= PROVIDER_CANARY_HEALTH_WAIT_POLL_INTERVAL_SECONDS


def test_health_that_never_turns_serving_fails_loud_once_with_no_vector_attempted(
    tmp_path: Path, workflow_directory: Path
) -> None:
    state_directory = tmp_path / "state"
    http = FakeHttp((configuration(),), workflow_directory)
    http.health_unavailable = True

    with pytest.raises(
        ProviderCanaryDiscoveryFailed,
        match="health-wait-timeout.*health timed out",
    ):
        execute_provider_canaries(
            settings(workflow_directory, state_directory), http=http, clock=FakeClock()
        )

    assert not any(method == "POST" for method, _path, _body in http.calls)
    assert all(path == "/health" for _method, path, _body in http.calls)


def test_discovery_reprobes_every_registered_vector_whose_receipt_is_foreign(
    tmp_path: Path, workflow_directory: Path
) -> None:
    """The self-healing property #942 claimed, restored under the amended rule.

    A redeploy invalidates every receipt by `source_commit`: the listing
    answers `startable: false` for every genuinely registered vector, exactly
    as it would the morning after -- but its reason is
    `provider-probe-receipt-missing`, not a superseded registry pointer, so
    discovery still finds it, still starts it through the exemption, and
    still writes it a fresh receipt.
    """

    http = FakeHttp(
        (
            configuration(has_valid_receipt=False),
            configuration(
                provider="xai",
                executor="grok-subscription-tools/v1",
                capability="headless_with_tools",
                configuration_hash="f" * 64,
                has_valid_receipt=False,
            ),
        ),
        workflow_directory,
    )
    state_directory = tmp_path / "state"

    report = execute_provider_canaries(
        settings(workflow_directory, state_directory), http=http, clock=FakeClock()
    )

    start_bodies = [
        body for _method, path, body in http.calls if path == "/runs" and body
    ]
    assert len(start_bodies) == 2
    assert report.attempted == 2
    assert report.failed == 0
    receipts = read_receipts(state_directory)
    assert len(receipts) == 2
    assert {receipt.result for receipt in receipts} == {ProviderProbeResult.SUCCEEDED}


def test_discovery_reprobes_a_vector_whose_last_probe_failed(
    tmp_path: Path, workflow_directory: Path
) -> None:
    """#1103: a real failed probe is offered exactly like a missing one.

    Splitting `not_startable_reason` into `provider-probe-receipt-missing`
    and `provider-probe-failed` on the wire must not narrow what a live
    canary retries -- both name the same live evidence this run itself
    would replace, so a configuration whose last probe genuinely failed
    still gets a fresh attempt.
    """

    http = FakeHttp(
        (configuration(has_valid_receipt=False, probe_failed=True),),
        workflow_directory,
    )
    state_directory = tmp_path / "state"

    report = execute_provider_canaries(
        settings(workflow_directory, state_directory), http=http, clock=FakeClock()
    )

    assert report.attempted == 1
    assert report.failed == 0
    (receipt,) = read_receipts(state_directory)
    assert receipt.result is ProviderProbeResult.SUCCEEDED


def test_discovery_excludes_a_superseded_configuration_by_its_own_reason(
    tmp_path: Path, workflow_directory: Path
) -> None:
    """A structurally startable but superseded revision is not a vector.

    The listing's own `not_startable_reason` names it `model-not-registered`
    -- computed by the same cast lookup a start makes -- not
    `provider-probe-receipt-missing`, so discovery excludes it honestly
    rather than offering it as though a fresh probe would fix it.
    """

    superseded_hash = "1" * 64
    http = FakeHttp(
        (
            configuration(configuration_hash=superseded_hash, model_registered=False),
            configuration(),
        ),
        workflow_directory,
    )
    state_directory = tmp_path / "state"

    report = execute_provider_canaries(
        settings(workflow_directory, state_directory), http=http, clock=FakeClock()
    )

    assert report.attempted == 1
    assert report.failed == 0
    (receipt,) = read_receipts(state_directory)
    assert receipt.configuration_hash == AgentConfigurationRevisionHash(
        CONFIGURATION_HASH
    )


def test_the_vector_cap_applies_after_filtering_not_before(
    tmp_path: Path, workflow_directory: Path
) -> None:
    """51 raw candidates, one superseded (`startable: false`): 50 attempts.

    The cap is checked against the filtered vector count, never the raw
    listing count -- a listing this size with exactly one superseded
    configuration must attempt every genuine vector, not fail loud over a
    limit it never actually reached.
    """

    first_page = tuple(
        configuration(configuration_hash=f"{number:064x}")
        for number in range(PROVIDER_CANARY_MAXIMUM_VECTORS)
    )
    superseded = configuration(configuration_hash="f" * 64, model_registered=False)
    http = FakeHttp((*first_page, superseded), workflow_directory)
    state_directory = tmp_path / "state"

    report = execute_provider_canaries(
        settings(workflow_directory, state_directory), http=http, clock=FakeClock()
    )

    assert report.attempted == PROVIDER_CANARY_MAXIMUM_VECTORS
    assert report.failed == 0


@pytest.mark.parametrize(
    ("failure_mode", "expected_problem"),
    (
        ("refused", "agent-executor-binding-unavailable"),
        ("malformed", "server-answer-unreadable"),
        ("unavailable", "server-unavailable"),
    ),
)
def test_a_failed_startability_read_fails_closed_and_reads_once(
    tmp_path: Path,
    workflow_directory: Path,
    failure_mode: str,
    expected_problem: str,
) -> None:
    """The listing's own `startable` field is the sole judgment source.

    A refusal, a malformed answer, or an unreachable service each abort the
    whole run loud before any vector is admitted -- no POST /runs, every
    live receipt untouched -- and the listing is read exactly once, never
    retried.
    """

    state_directory = tmp_path / "state"
    destination = write_live_success(state_directory)
    previous = destination.read_bytes()
    http = FakeHttp((configuration(),), workflow_directory)
    if failure_mode == "refused":
        http.configuration_refusal = "agent-executor-binding-unavailable"
    elif failure_mode == "malformed":
        http.configuration_answer = b"not-json"
    else:
        http.configuration_unavailable = True

    with pytest.raises(ProviderCanaryDiscoveryFailed, match=expected_problem):
        execute_provider_canaries(
            settings(workflow_directory, state_directory), http=http, clock=FakeClock()
        )

    assert destination.read_bytes() == previous
    assert not any(method == "POST" for method, _path, _body in http.calls)
    assert (
        sum(
            1
            for method, path, _body in http.calls
            if method == "GET" and path.startswith("/agent-configuration-revisions")
        )
        == 1
    )


def test_a_timed_out_startability_read_fails_closed_before_any_start(
    tmp_path: Path, workflow_directory: Path
) -> None:
    """A discovery deadline exceeded while reading the listing still fails
    the whole run loud, before any vector is admitted."""

    state_directory = tmp_path / "state"
    destination = write_live_success(state_directory)
    previous = destination.read_bytes()
    delegate = FakeHttp((configuration(),), workflow_directory)
    clock = FakeClock()

    class SlowConfigurationHttp:
        def get(self, path: str, *, timeout_seconds: float) -> bytes:
            answer = delegate.get(path, timeout_seconds=timeout_seconds)
            if path.startswith("/agent-configuration-revisions"):
                clock.elapsed += PROVIDER_CANARY_DISCOVERY_TIMEOUT_SECONDS + 1
            return answer

        def post(
            self,
            path: str,
            body: bytes,
            *,
            timeout_seconds: float,
            media_type: str = "application/json",
        ) -> bytes:
            raise AssertionError(
                "no start should be attempted after a timed-out startability read"
            )

    with pytest.raises(ProviderCanaryDiscoveryFailed, match="discovery-timeout"):
        execute_provider_canaries(
            settings(workflow_directory, state_directory),
            http=SlowConfigurationHttp(),
            clock=clock,
        )

    assert destination.read_bytes() == previous
    assert not any(method == "POST" for method, _path, _body in delegate.calls)


def test_discovery_finds_nothing_for_a_vector_the_service_marks_not_startable(
    tmp_path: Path, workflow_directory: Path
) -> None:
    """`startable: false` refuses discovery -- the canary asks the live
    service's own judgment and derives nothing of its own from it."""

    http = FakeHttp(
        (configuration(structurally_startable=False),),
        workflow_directory,
    )
    state_directory = tmp_path / "state"

    with pytest.raises(ProviderCanaryDiscoveryFailed, match="no-startable"):
        execute_provider_canaries(
            settings(workflow_directory, state_directory), http=http, clock=FakeClock()
        )

    assert read_receipts(state_directory) == ()


def test_each_trigger_starts_a_new_run_even_on_the_same_day(
    tmp_path: Path, workflow_directory: Path
) -> None:
    http = FakeHttp((configuration(),), workflow_directory)
    clock = FakeClock()
    canary_settings = settings(workflow_directory, tmp_path / "state")

    execute_provider_canaries(canary_settings, http=http, clock=clock)
    clock.instant += timedelta(minutes=1)
    execute_provider_canaries(canary_settings, http=http, clock=clock)

    starts = [
        json.loads(body)
        for _method, path, body in http.calls
        if path == "/runs" and body is not None
    ]
    assert len(starts) == 2
    assert starts[0]["run_id"] != starts[1]["run_id"]
    assert starts[0]["run_id"].startswith("provider-canary/")
    assert starts[1]["run_id"].startswith("provider-canary/")


@pytest.mark.parametrize(
    ("failure_mode", "expected_problem"),
    (
        ("server-down", "server-unavailable"),
        ("timeout", "run-timeout"),
        (
            "refusal",
            "start-refused-agent-executor-binding-unavailable",
        ),
    ),
)
def test_server_down_timeout_and_start_refusal_each_leave_a_loud_fail_receipt(
    tmp_path: Path,
    workflow_directory: Path,
    failure_mode: str,
    expected_problem: str,
) -> None:
    http = FakeHttp((configuration(),), workflow_directory)
    if failure_mode == "server-down":
        http.server_down = True
    elif failure_mode == "timeout":
        http.terminal_after_reads = 100
    else:
        http.refusal = "agent-executor-binding-unavailable"
    state_directory = tmp_path / "state"

    report = execute_provider_canaries(
        settings(workflow_directory, state_directory), http=http, clock=FakeClock()
    )

    assert report.failed == 1
    (receipt,) = read_receipts(state_directory)
    assert receipt.result is ProviderProbeResult.FAILED
    assert receipt.problem_code is not None
    assert receipt.problem_code.value == expected_problem
    assert receipt.terminal_hash is None


def test_a_typed_poll_refusal_is_named_by_its_own_code_not_start_refused(
    tmp_path: Path, workflow_directory: Path
) -> None:
    """`start-refused-*` belongs to the POST /runs answer alone.

    A refusal met while merely watching an already-accepted run (a GET
    poll) is a different problem and keeps its own bare code -- conflating
    the two would say a start was refused when it plainly was not.
    """

    http = FakeHttp((configuration(),), workflow_directory)
    http.poll_refusal = "invalid-public-run-reference"
    state_directory = tmp_path / "state"

    report = execute_provider_canaries(
        settings(workflow_directory, state_directory), http=http, clock=FakeClock()
    )

    assert report.failed == 1
    (receipt,) = read_receipts(state_directory)
    assert receipt.problem_code == ProviderProbeProblemCode(
        "invalid-public-run-reference"
    )


def test_a_failed_run_replaces_the_live_success_receipt(
    tmp_path: Path, workflow_directory: Path
) -> None:
    state_directory = tmp_path / "state"
    destination = write_live_success(state_directory)
    previous = destination.read_bytes()
    http = FakeHttp((configuration(),), workflow_directory, server_down=True)

    report = execute_provider_canaries(
        settings(workflow_directory, state_directory), http=http, clock=FakeClock()
    )

    assert report.failed == 1
    assert destination.read_bytes() != previous
    (receipt,) = read_receipts(state_directory)
    assert receipt.result is ProviderProbeResult.FAILED
    assert receipt.problem_code == ProviderProbeProblemCode("server-unavailable")


def test_a_failed_run_without_a_named_refusal_keeps_the_plain_problem_code(
    tmp_path: Path, workflow_directory: Path
) -> None:
    http = FakeHttp((configuration(),), workflow_directory, terminal_state="FAILED")
    state_directory = tmp_path / "state"

    report = execute_provider_canaries(
        settings(workflow_directory, state_directory), http=http, clock=FakeClock()
    )

    assert report.failed == 1
    (receipt,) = read_receipts(state_directory)
    assert receipt.result is ProviderProbeResult.FAILED
    assert receipt.problem_code == ProviderProbeProblemCode("run-failed")


def test_a_failed_run_whose_transcript_names_a_provider_refusal_gets_its_own_code(
    tmp_path: Path, workflow_directory: Path
) -> None:
    """A rate-limited provider is not a broken vector, and the receipt says so.

    The run's own terminal state carries no more than `FAILED`; telling the two
    apart needs the failed node's own transcript (#1029).
    """

    http = FakeHttp(
        (configuration(),),
        workflow_directory,
        terminal_state="FAILED",
        node_detail_transcript_events=(
            {
                "event": "provider-terminal-refusal",
                "terminal_reason": "rate_limit_error",
                "api_error_status": "429",
                "text": "Not logged in · Please run /login",
                "redacted": False,
                "moment": {"origin": "v1-before-moments"},
            },
        ),
    )
    state_directory = tmp_path / "state"

    report = execute_provider_canaries(
        settings(workflow_directory, state_directory), http=http, clock=FakeClock()
    )

    assert report.failed == 1
    (receipt,) = read_receipts(state_directory)
    assert receipt.result is ProviderProbeResult.FAILED
    assert receipt.problem_code == ProviderProbeProblemCode("provider-refused")


def test_an_unreadable_local_workflow_replaces_the_live_success_receipt(
    tmp_path: Path, workflow_directory: Path
) -> None:
    state_directory = tmp_path / "state"
    write_live_success(state_directory)
    http = FakeHttp((configuration(),), workflow_directory)
    (workflow_directory / "provider-canary-headless.yaml").unlink()

    report = execute_provider_canaries(
        settings(workflow_directory, state_directory),
        http=http,
        clock=FakeClock(),
    )

    assert report.failed == 1
    (receipt,) = read_receipts(state_directory)
    assert receipt.result is ProviderProbeResult.FAILED
    assert receipt.problem_code == ProviderProbeProblemCode("workflow-unreadable")


@pytest.mark.parametrize(
    ("failure_mode", "expected_problem"),
    (
        ("unavailable", "server-unavailable"),
        ("unreadable", "server-answer-unreadable"),
        ("empty", "no-startable-provider-vectors"),
        ("catalog", "server-unavailable"),
        ("health-unavailable", "server-unavailable"),
        ("health-refused", "health-refused"),
        ("health-unreadable", "server-answer-unreadable"),
        ("health-provenance", "server-answer-unreadable"),
    ),
)
def test_discovery_failure_preserves_every_live_success_byte_for_byte(
    tmp_path: Path,
    workflow_directory: Path,
    failure_mode: str,
    expected_problem: str,
) -> None:
    state_directory = tmp_path / "state"
    destination = write_live_success(state_directory)
    previous = destination.read_bytes()
    http = FakeHttp((configuration(),), workflow_directory)
    if failure_mode == "unavailable":
        http.configuration_unavailable = True
    elif failure_mode == "unreadable":
        http.configuration_answer = b"not-json"
    elif failure_mode == "catalog":
        http.catalog_unavailable = True
    elif failure_mode == "health-unavailable":
        http.health_unavailable = True
    elif failure_mode == "health-refused":
        http.health_refusal = "health-refused"
    elif failure_mode == "health-unreadable":
        http.health_answer = b"not-json"
    elif failure_mode == "health-provenance":
        http.health_source_commit = "not-a-commit"
    else:
        http.configurations = ()

    with pytest.raises(ProviderCanaryDiscoveryFailed, match=expected_problem):
        execute_provider_canaries(
            settings(workflow_directory, state_directory), http=http, clock=FakeClock()
        )

    assert destination.read_bytes() == previous
    assert not any(method == "POST" for method, _path, _body in http.calls)


def test_empty_discovery_without_previous_receipts_is_a_loud_cli_failure(
    tmp_path: Path,
    workflow_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    http = FakeHttp((), workflow_directory)
    monkeypatch.setattr(
        "atelier2.host.execute_provider_canaries",
        lambda canary_settings, **keywords: execute_provider_canaries(
            canary_settings, http=http, clock=FakeClock(), **keywords
        ),
    )

    exit_status = main(
        [
            "provider-canary",
            "--workflow-directory",
            str(workflow_directory),
            "--state-directory",
            str(tmp_path / "state"),
        ]
    )

    assert exit_status == 1
    assert "no startable provider vectors" in capsys.readouterr().err


def test_a_hung_http_start_uses_the_terminal_bound_and_leaves_a_fail_receipt(
    tmp_path: Path,
    workflow_directory: Path,
) -> None:
    timeouts: list[tuple[str, float]] = []
    workflow_name = "provider-canary-headless"
    workflow_hash = Sha256Hash.of(
        (workflow_directory / f"{workflow_name}.yaml").read_bytes()
    ).value

    def hung_start(request: httpx.Request) -> httpx.Response:
        full_url = str(request.url)
        timeouts.append((full_url, request.extensions["timeout"]["read"]))
        method = request.method
        if full_url.endswith("/health"):
            answer = HealthResource(
                status="serving",
                source_commit=SOURCE_COMMIT,
                source_tree="d" * 40,
                serve_started_at="2026-08-31T08:00:00Z",
            ).model_dump_json()
        elif "/agent-configuration-revisions?" in full_url:
            answer = encoded(
                items=(configuration(),), next_after_revision_hash=None
            ).decode()
        elif full_url.endswith(f"{CATALOG_NAME_PREFIX}{workflow_name}"):
            answer = CatalogNameResolutionResource(
                display_name=workflow_name,
                lineage_id="4" * 64,
                catalog_revision_hash=workflow_hash,
                revision_number=1,
            ).model_dump_json()
        elif full_url.endswith("/runs") and method == "POST":
            raise httpx.TimeoutException("HTTP start timed out")
        else:
            raise AssertionError((method, full_url))
        return httpx.Response(200, content=answer.encode())

    state_directory = tmp_path / "state"
    canary_settings = ProviderCanarySettings(
        service_url="http://127.0.0.1:8422",
        workflow_directory=workflow_directory,
        state_directory=state_directory,
        terminal_timeout_seconds=5,
        poll_interval_seconds=1,
    )
    http = AtelierApiProviderCanaryHttp(
        AtelierApi(
            canary_settings.service_url, transport=httpx.MockTransport(hung_start)
        )
    )

    report = execute_provider_canaries(canary_settings, http=http, clock=FakeClock())

    assert timeouts
    assert all(
        timeout <= canary_settings.terminal_timeout_seconds
        for url, timeout in timeouts
        if url.endswith("/runs")
    )
    assert all(timeout <= 30 for _url, timeout in timeouts)
    assert report.failed == 1
    (receipt,) = read_receipts(state_directory)
    assert receipt.result is ProviderProbeResult.FAILED
    assert receipt.problem_code == ProviderProbeProblemCode("server-unavailable")


@pytest.mark.parametrize(
    ("failure_status", "failure_body", "expected_problem_code"),
    (
        (
            422,
            lambda: problem_resource("uncast-agent-roles").model_dump_json().encode(),
            "start-refused-uncast-agent-roles",
        ),
        (502, lambda: b"upstream exploded", "http-refused"),
    ),
    ids=("typed-start-refusal", "unclassifiable-http-refusal"),
)
def test_a_real_http_start_refusal_is_classified_by_the_owning_vocabulary(
    tmp_path: Path,
    workflow_directory: Path,
    failure_status: int,
    failure_body: Callable[[], bytes],
    expected_problem_code: str,
) -> None:
    """The URN is parsed at its owner (`atelier2.api.problems`), never guessed.

    A typed start refusal (`uncast-agent-roles` among them) keeps its own
    honest token instead of degrading to `http-refused`; an answer this
    client genuinely cannot classify still keeps that same generic code,
    unchanged from before.
    """

    workflow_name = "provider-canary-headless"
    workflow_hash = Sha256Hash.of(
        (workflow_directory / f"{workflow_name}.yaml").read_bytes()
    ).value

    def answering(request: httpx.Request) -> httpx.Response:
        full_url = str(request.url)
        method = request.method
        if full_url.endswith("/health"):
            answer = (
                HealthResource(
                    status="serving",
                    source_commit=SOURCE_COMMIT,
                    source_tree="d" * 40,
                    serve_started_at="2026-08-31T08:00:00Z",
                )
                .model_dump_json()
                .encode()
            )
        elif "/agent-configuration-revisions?" in full_url:
            answer = encoded(items=(configuration(),), next_after_revision_hash=None)
        elif full_url.endswith(f"{CATALOG_NAME_PREFIX}{workflow_name}"):
            answer = (
                CatalogNameResolutionResource(
                    display_name=workflow_name,
                    lineage_id="4" * 64,
                    catalog_revision_hash=workflow_hash,
                    revision_number=1,
                )
                .model_dump_json()
                .encode()
            )
        elif full_url.endswith("/runs") and method == "POST":
            return httpx.Response(failure_status, content=failure_body())
        else:
            raise AssertionError((method, full_url))
        return httpx.Response(200, content=answer)

    state_directory = tmp_path / "state"
    canary_settings = ProviderCanarySettings(
        service_url="http://127.0.0.1:8422",
        workflow_directory=workflow_directory,
        state_directory=state_directory,
        terminal_timeout_seconds=5,
        poll_interval_seconds=1,
    )
    http = AtelierApiProviderCanaryHttp(
        AtelierApi(
            canary_settings.service_url, transport=httpx.MockTransport(answering)
        )
    )

    report = execute_provider_canaries(canary_settings, http=http, clock=FakeClock())

    assert report.failed == 1
    (receipt,) = read_receipts(state_directory)
    assert receipt.problem_code == ProviderProbeProblemCode(expected_problem_code)


@pytest.mark.parametrize("limit_kind", ("pages", "vectors"))
def test_discovery_limits_are_loud_and_receipt_neutral(
    tmp_path: Path, workflow_directory: Path, limit_kind: str
) -> None:
    state_directory = tmp_path / "state"
    destination = write_live_success(state_directory)
    previous = destination.read_bytes()
    http = FakeHttp((), workflow_directory)
    if limit_kind == "pages":
        http.configuration_pages = tuple(
            encoded(items=(), next_after_revision_hash=f"{page_number + 1:064x}")
            for page_number in range(PROVIDER_CANARY_MAXIMUM_CONFIGURATION_PAGES)
        )
        expected_problem = "too-many-configuration-pages"
    else:
        first_page = tuple(
            configuration(configuration_hash=f"{number:064x}")
            for number in range(PROVIDER_CANARY_MAXIMUM_VECTORS)
        )
        http.configuration_pages = (
            encoded(items=first_page, next_after_revision_hash="e" * 64),
            encoded(
                items=(configuration(configuration_hash="f" * 64),),
                next_after_revision_hash=None,
            ),
        )
        expected_problem = "too-many-provider-vectors"

    with pytest.raises(ProviderCanaryDiscoveryFailed, match=expected_problem):
        execute_provider_canaries(
            settings(workflow_directory, state_directory), http=http, clock=FakeClock()
        )

    assert destination.read_bytes() == previous
    if limit_kind == "pages":
        assert http.configuration_page_reads == (
            PROVIDER_CANARY_MAXIMUM_CONFIGURATION_PAGES
        )


def test_the_overall_deadline_bounds_a_hanging_vectors_own_cumulative_work(
    tmp_path: Path, workflow_directory: Path
) -> None:
    """A vector that keeps consuming time past the process budget ends in a
    loud, receipted `process-timeout` rather than hanging the whole run.

    #1124: this failure is now caught and receipted by the vector's own
    execution, exactly like a run-timeout or a server refusal -- it no longer
    aborts `execute_provider_canaries` itself, because concurrent vectors
    share no single "next vector" checkpoint left to raise from. A sibling
    vector's own receipt is unaffected by this one's exhausted budget.
    """

    clock = FakeClock()
    delegate = FakeHttp((configuration(),), workflow_directory)

    class ConsumingHttp:
        def get(self, path: str, *, timeout_seconds: float) -> bytes:
            clock.elapsed += min(1.0, timeout_seconds)
            return delegate.get(path, timeout_seconds=timeout_seconds)

        def post(
            self,
            path: str,
            body: bytes,
            *,
            timeout_seconds: float,
            media_type: str = "application/json",
        ) -> bytes:
            clock.elapsed += min(1.0, timeout_seconds)
            return delegate.post(
                path,
                body,
                timeout_seconds=timeout_seconds,
                media_type=media_type,
            )

    http = ConsumingHttp()
    state_directory = tmp_path / "state"
    canary_settings = ProviderCanarySettings(
        service_url="http://127.0.0.1:8422",
        workflow_directory=workflow_directory,
        state_directory=state_directory,
        terminal_timeout_seconds=5,
        poll_interval_seconds=1,
        process_timeout_seconds=4,
    )

    report = execute_provider_canaries(canary_settings, http=http, clock=clock)

    starts = [path for method, path, _body in delegate.calls if method == "POST"]
    assert starts == ["/runs"]
    assert clock.elapsed <= canary_settings.process_timeout_seconds
    assert report.failed == 1
    (first_receipt,) = read_receipts(state_directory)
    assert first_receipt.vector == ProviderProbeVectorId(
        f"headless-{CONFIGURATION_HASH}"
    )
    assert first_receipt.problem_code == ProviderProbeProblemCode("process-timeout")


def test_atomic_receipt_replacement_never_exposes_a_partly_written_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "vector.json"
    destination.write_bytes(b"previous-complete-receipt")
    receipt = ProviderProbeReceipt(
        vector=ProviderProbeVectorId("headless-vector"),
        configuration_hash=AgentConfigurationRevisionHash(CONFIGURATION_HASH),
        workflow_hash=WorkflowRevisionHash("d" * 64),
        provider_layer_digest=RUNNING_PROVIDER_LAYER_DIGEST,
        source_commit=SOURCE_COMMIT,
        observed_at=RecordedAt("2026-09-01T08:00:00Z"),
        valid_until=RecordedAt("2026-09-02T10:00:00Z"),
        result=ProviderProbeResult.SUCCEEDED,
        run_reference=RunId("provider-canary/vector/2026-09-01T08:00:00Z"),
        terminal_hash=Sha256Hash(TERMINAL_HASH),
    )
    monkeypatch.setattr(
        "atelier2.host.provider_canary.os.replace",
        lambda _source, _destination: (_ for _ in ()).throw(OSError("interrupted")),
    )

    with pytest.raises(OSError, match="interrupted"):
        write_provider_canary_receipt_atomic(destination, receipt)

    assert destination.read_bytes() == b"previous-complete-receipt"
    assert list(tmp_path.iterdir()) == [destination]


def test_a_hanging_vectors_own_receipt_never_delays_a_sibling_vectors(
    tmp_path: Path, workflow_directory: Path
) -> None:
    """#1124: vectors probe concurrently, proven by both starts being in
    flight at once.

    Sequential probing could never pass the barrier below: the second
    vector's own POST would not fire until the first vector's entire attempt
    -- including its full hang -- had already ended, so the barrier would
    time out rather than release both threads together.
    """

    slow_configuration_hash = "f" * 64
    http = FakeHttp(
        (
            configuration(),
            configuration(
                provider="xai",
                executor="grok-subscription-tools/v1",
                capability="headless_with_tools",
                configuration_hash=slow_configuration_hash,
            ),
        ),
        workflow_directory,
    )
    both_starts_in_flight = threading.Barrier(2, timeout=2.0)
    never_set = threading.Event()

    class ConcurrentStartHttp:
        def get(self, path: str, *, timeout_seconds: float) -> bytes:
            return http.get(path, timeout_seconds=timeout_seconds)

        def post(
            self,
            path: str,
            body: bytes,
            *,
            timeout_seconds: float,
            media_type: str = "application/json",
        ) -> bytes:
            both_starts_in_flight.wait()
            request = json.loads(body)
            binding = request["agent_bindings"][0]
            if binding["agent_configuration_revision_hash"] == slow_configuration_hash:
                # A real client's own timeout would give up after exactly this
                # long; the event never fires, so this always waits the full
                # bound rather than racing on when a test happens to run.
                never_set.wait(timeout=timeout_seconds)
                raise ProviderCanaryServerUnavailable("hung provider vector")
            return http.post(
                path, body, timeout_seconds=timeout_seconds, media_type=media_type
            )

    state_directory = tmp_path / "state"
    canary_settings = ProviderCanarySettings(
        service_url="http://127.0.0.1:8422",
        workflow_directory=workflow_directory,
        state_directory=state_directory,
        terminal_timeout_seconds=0.2,
    )

    report = execute_provider_canaries(canary_settings, http=ConcurrentStartHttp())

    assert report.attempted == 2
    assert report.failed == 1
    receipts = {
        receipt.configuration_hash.value: receipt
        for receipt in read_receipts(state_directory)
    }
    slow_receipt = receipts[slow_configuration_hash]
    assert receipts[CONFIGURATION_HASH].result is ProviderProbeResult.SUCCEEDED
    assert slow_receipt.result is ProviderProbeResult.FAILED
    assert slow_receipt.problem_code is not None
    assert slow_receipt.problem_code.value in {"server-unavailable", "run-timeout"}


def test_provider_layer_status_names_receipts_kept_when_unchanged(
    tmp_path: Path, workflow_directory: Path
) -> None:
    http = FakeHttp((configuration(),), workflow_directory)
    state_directory = tmp_path / "state"
    write_provider_canary_receipt_atomic(
        state_directory / f"headless-{CONFIGURATION_HASH}.json",
        successful_receipt(provider_layer_digest=RUNNING_PROVIDER_LAYER_DIGEST),
    )

    report = execute_provider_canaries(
        settings(workflow_directory, state_directory), http=http, clock=FakeClock()
    )

    assert report.provider_layer_status.outcome is (
        ProviderLayerReceiptOutcome.RECEIPTS_KEPT
    )
    assert report.provider_layer_status.current_digest == RUNNING_PROVIDER_LAYER_DIGEST
    assert report.provider_layer_status.previous_digest is None


def test_provider_layer_status_names_receipts_invalidated_when_changed(
    tmp_path: Path, workflow_directory: Path
) -> None:
    http = FakeHttp((configuration(),), workflow_directory)
    state_directory = tmp_path / "state"
    write_provider_canary_receipt_atomic(
        state_directory / f"headless-{CONFIGURATION_HASH}.json",
        successful_receipt(provider_layer_digest=FOREIGN_PROVIDER_LAYER_DIGEST),
    )

    report = execute_provider_canaries(
        settings(workflow_directory, state_directory), http=http, clock=FakeClock()
    )

    assert report.provider_layer_status.outcome is (
        ProviderLayerReceiptOutcome.RECEIPTS_INVALIDATED
    )
    assert report.provider_layer_status.current_digest == RUNNING_PROVIDER_LAYER_DIGEST
    assert report.provider_layer_status.previous_digest == FOREIGN_PROVIDER_LAYER_DIGEST


def test_provider_layer_status_ignores_a_stale_foreign_receipt_that_sorts_first(
    tmp_path: Path, workflow_directory: Path
) -> None:
    """#1124 review: the state directory is never pruned, so a superseded
    configuration's receipt can linger beside current ones. A lingering
    receipt from an old foreign digest whose filename happens to sort before
    the current, newer receipts must not override them -- the newest receipt
    by `observed_at` is the last thing this deployment actually wrote."""

    http = FakeHttp((configuration(),), workflow_directory)
    state_directory = tmp_path / "state"
    write_provider_canary_receipt_atomic(
        state_directory / "aaa-superseded.json",
        successful_receipt(
            provider_layer_digest=FOREIGN_PROVIDER_LAYER_DIGEST,
            observed_at=RecordedAt("2026-08-01T08:00:00Z"),
        ),
    )
    write_provider_canary_receipt_atomic(
        state_directory / "zzz-current.json",
        successful_receipt(
            provider_layer_digest=RUNNING_PROVIDER_LAYER_DIGEST,
            observed_at=RecordedAt("2026-08-31T08:00:00Z"),
        ),
    )

    report = execute_provider_canaries(
        settings(workflow_directory, state_directory), http=http, clock=FakeClock()
    )

    assert report.provider_layer_status.outcome is (
        ProviderLayerReceiptOutcome.RECEIPTS_KEPT
    )
    assert report.provider_layer_status.current_digest == RUNNING_PROVIDER_LAYER_DIGEST
    assert report.provider_layer_status.previous_digest is None


def test_provider_layer_status_answers_from_the_newest_receipt_even_when_it_sorts_last(
    tmp_path: Path, workflow_directory: Path
) -> None:
    """Mirror of the above: a newer, genuinely foreign receipt whose filename
    sorts after an older, still-matching one must invalidate -- the previous
    digest reported is the newest receipt's own, not the older receipt's."""

    http = FakeHttp((configuration(),), workflow_directory)
    state_directory = tmp_path / "state"
    write_provider_canary_receipt_atomic(
        state_directory / "aaa-superseded.json",
        successful_receipt(
            provider_layer_digest=RUNNING_PROVIDER_LAYER_DIGEST,
            observed_at=RecordedAt("2026-08-01T08:00:00Z"),
        ),
    )
    write_provider_canary_receipt_atomic(
        state_directory / "zzz-current.json",
        successful_receipt(
            provider_layer_digest=FOREIGN_PROVIDER_LAYER_DIGEST,
            observed_at=RecordedAt("2026-08-31T08:00:00Z"),
        ),
    )

    report = execute_provider_canaries(
        settings(workflow_directory, state_directory), http=http, clock=FakeClock()
    )

    assert report.provider_layer_status.outcome is (
        ProviderLayerReceiptOutcome.RECEIPTS_INVALIDATED
    )
    assert report.provider_layer_status.current_digest == RUNNING_PROVIDER_LAYER_DIGEST
    assert report.provider_layer_status.previous_digest == FOREIGN_PROVIDER_LAYER_DIGEST


def test_provider_layer_status_names_no_readable_prior_receipt_on_a_first_ever_run(
    tmp_path: Path, workflow_directory: Path
) -> None:
    http = FakeHttp((configuration(),), workflow_directory)
    state_directory = tmp_path / "never-created"

    report = execute_provider_canaries(
        settings(workflow_directory, state_directory), http=http, clock=FakeClock()
    )

    assert report.provider_layer_status.outcome is (
        ProviderLayerReceiptOutcome.NO_READABLE_PRIOR_RECEIPT
    )
    assert report.provider_layer_status.current_digest == RUNNING_PROVIDER_LAYER_DIGEST
    assert report.provider_layer_status.previous_digest is None


def test_provider_layer_status_names_no_readable_prior_receipt_for_an_old_format_document(
    tmp_path: Path, workflow_directory: Path
) -> None:
    """#1124 review: a pre-#1124 receipt (no `provider_layer_digest` field) is
    every live receipt on the very deploy that introduces the digest gate.
    Falling through to "receipts kept" there would tell the operator every
    receipt still applies while every one of them is in fact unreadable."""

    http = FakeHttp((configuration(),), workflow_directory)
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    old_format_document = json.loads(successful_receipt().canonical_bytes())
    del old_format_document["provider_layer_digest"]
    (state_directory / f"headless-{CONFIGURATION_HASH}.json").write_bytes(
        json.dumps(old_format_document, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    )

    report = execute_provider_canaries(
        settings(workflow_directory, state_directory), http=http, clock=FakeClock()
    )

    assert report.provider_layer_status.outcome is (
        ProviderLayerReceiptOutcome.NO_READABLE_PRIOR_RECEIPT
    )
    assert report.provider_layer_status.current_digest == RUNNING_PROVIDER_LAYER_DIGEST
    assert report.provider_layer_status.previous_digest is None


def test_provider_layer_digest_changes_with_its_source_bytes_and_stays_stable_otherwise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1124 review: the digest is a real content hash of its source modules,
    not an identity or a timestamp -- changing bytes changes it, and hashing
    the same bytes twice never does."""

    module_path = tmp_path / "fake_provider_layer_module.py"
    module_path.write_text("FIRST_VERSION = 1\n", encoding="utf-8")
    fake_module = types.ModuleType("fake_provider_layer_module")
    fake_module.__file__ = str(module_path)
    monkeypatch.setattr(
        "atelier2.host.provider_canary._PROVIDER_LAYER_ADAPTER_MODULES",
        (fake_module,),
    )

    first_digest = provider_layer_digest()
    stable_digest = provider_layer_digest()
    module_path.write_text("FIRST_VERSION = 2\n", encoding="utf-8")
    changed_digest = provider_layer_digest()

    assert stable_digest == first_digest
    assert changed_digest != first_digest


def test_the_thread_pool_never_exceeds_the_concurrency_cap(
    tmp_path: Path, workflow_directory: Path
) -> None:
    """#1124 review: discovering more startable vectors than
    `PROVIDER_CANARY_MAXIMUM_CONCURRENT_VECTORS` must never start more live
    billed runs together than that ceiling -- a redeploy that clears many
    vectors' receipts at once is exactly when this matters most. Proven by
    the observed peak of simultaneously in-flight `POST /runs` calls, not by
    pinning the pool's own constructor argument.

    A barrier with exactly `cap` parties forces the first `cap` starts to be
    genuinely in flight together before any of them may return, so the
    measured peak is deterministic rather than a timing accident; the
    remaining three starts skip the barrier; a barrier of `cap` parties can
    never be satisfied by only three later threads, so waiting on it would
    deadlock the run.
    """

    cap = PROVIDER_CANARY_MAXIMUM_CONCURRENT_VECTORS
    vector_count = cap + 3
    configurations = tuple(
        configuration(configuration_hash=f"{number:064x}")
        for number in range(vector_count)
    )
    http = FakeHttp(configurations, workflow_directory)
    state_directory = tmp_path / "state"
    first_wave_in_flight = threading.Barrier(cap, timeout=2.0)
    lock = threading.Lock()
    in_flight = 0
    peak = 0
    starts_seen = 0

    class PeakTrackingHttp:
        def get(self, path: str, *, timeout_seconds: float) -> bytes:
            return http.get(path, timeout_seconds=timeout_seconds)

        def post(
            self,
            path: str,
            body: bytes,
            *,
            timeout_seconds: float,
            media_type: str = "application/json",
        ) -> bytes:
            if path != "/runs":
                return http.post(
                    path, body, timeout_seconds=timeout_seconds, media_type=media_type
                )
            nonlocal in_flight, peak, starts_seen
            with lock:
                in_flight += 1
                peak = max(peak, in_flight)
                is_first_wave = starts_seen < cap
                starts_seen += 1
            try:
                if is_first_wave:
                    first_wave_in_flight.wait()
            finally:
                with lock:
                    in_flight -= 1
            return http.post(
                path, body, timeout_seconds=timeout_seconds, media_type=media_type
            )

    report = execute_provider_canaries(
        settings(workflow_directory, state_directory),
        http=PeakTrackingHttp(),
        clock=FakeClock(),
    )

    assert peak == cap
    assert report.attempted == vector_count
