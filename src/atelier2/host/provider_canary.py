"""Run the configured live provider vectors and leave bounded proof behind.

The served agent-configuration list is the deployment's answer about which
exact provider/executor/configuration vectors are worth a live attempt: one
the deployment's own snapshot already calls `startable`, or one whose only
named problem is the receipt this very run would write -- no receipt on file
yet (`not_startable_reason: provider-probe-receipt-missing`) or a receipt
whose own last result was a failure (`provider-probe-failed`, #1103). Both
answers come from the same server-side judgment a start itself makes -- the
model registry pointer, the executor, and live evidence, all computed once
through `agent_catalog.py` -- so a superseded revision carries its own
distinct reason (`model-not-registered`) and is never offered as a vector.
This client reads those fields and derives nothing of its own: no local
registry fetch, no local cast, no parse of the repository's workflow bytes.
It then resolves the matching admitted workflow, starts one fresh run with
the listed configuration hash, and polls the public run resource to a
terminal state. It owns no provider process and opens no store.

Discovery is receipt-neutral: health, the bounded configuration list, and all
distinct admitted workflow names must resolve before any vector becomes an
attempt. Discovery refusal is visible through the process exit and journal
and leaves every last-known vector receipt byte-identical. Once one vector
enters execution, its outcome replaces that vector's secret-free
``provider-probe-receipt/v1`` beneath the operator's XDG state directory. The
durable run remains the evidence owner; a receipt carries only its identities
and terminal hash, or a bounded problem code. Replacement is a same-directory
temporary file plus ``os.replace`` so a reader sees either the previous
complete receipt or the new complete receipt.

The public start request has no separate ``idempotency_key``: its ``run_id`` is
the durable idempotency identity. Every trigger creates a timestamped identity,
so a deploy trigger may start another billed probe on the same day. This client
does not persist that identity before POST; a crash after the service accepts
the POST but before the receipt lands can therefore make the next trigger start
another billed run. Closing that gap requires persisting the planned ``run_id``
as the retry key before POST and replaying it until its outcome is receipted.
"""

from __future__ import annotations

import inspect
import json
import os
import tempfile
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from urllib.parse import urlencode, urlsplit

from pydantic import TypeAdapter, ValidationError

from atelier2.adapters import claude_subscription as claude_subscription_adapter
from atelier2.adapters import codex_subscription as codex_subscription_adapter
from atelier2.adapters import grok_subscription as grok_subscription_adapter
from atelier2.adapters.claude_subscription import (
    CLAUDE_ATELIER_DOORS_EXECUTOR_KEY,
    CLAUDE_SUBSCRIPTION_EXECUTOR_KEY,
    CLAUDE_WORKSPACE_TOOLS_EXECUTOR_KEY,
)
from atelier2.adapters.codex_subscription import CODEX_SUBSCRIPTION_EXECUTOR_KEY
from atelier2.adapters.grok_subscription import (
    GROK_SUBSCRIPTION_EXECUTOR_KEY,
    GROK_WORKSPACE_TOOLS_EXECUTOR_KEY,
)
from atelier2.api.problems import PROBLEM_TYPE_PREFIX
from atelier2.api.wire.resources import (
    AgentConfigurationRevisionListItemResource,
    AgentConfigurationRevisionPageResource,
    CatalogNameResolutionResource,
    HealthResource,
    NodeDetailResource,
    RunResourceV3,
)
from atelier2.contracts.agent_transcripts import TranscriptEventKind
from atelier2.contracts.agents import (
    AgentConfigurationNotStartableReason,
    AgentConfigurationRevisionHash,
)
from atelier2.contracts.hashing import Sha256Hash, frame
from atelier2.contracts.provider_probe_receipts import (
    _SOURCE_COMMIT as PROVIDER_PROBE_SOURCE_COMMIT_FORMAT,
)
from atelier2.contracts.provider_probe_receipts import (
    PROVIDER_CANARY_ATELIER_DOORS_WORKFLOW_NAME,
    PROVIDER_CANARY_HEADLESS_WORKFLOW_NAME,
    PROVIDER_CANARY_WORKSPACE_TOOLS_WORKFLOW_NAME,
    ProviderProbeProblemCode,
    ProviderProbeReceipt,
    ProviderProbeReceiptRefused,
    ProviderProbeResult,
    ProviderProbeVectorId,
    read_provider_probe_receipt,
)
from atelier2.contracts.revisions_v3 import RevisionKind
from atelier2.contracts.runs import RunId, WorkflowRevisionHash
from atelier2.contracts.when import recorded_instant
from atelier2.host.address import ADDRESSABLE_SCHEMES, DEFAULT_SERVICE_URL
from atelier2.host.atelier_api_client import (
    MAXIMUM_FAILURE_BODY_BYTES,
    AtelierApi,
    AtelierApiTransport,
    AtelierApiTransportFailure,
)
from atelier2.host.run_command import (
    AGENT_CONFIGURATION_PATH,
    JSON_MEDIA_TYPE,
    RUN_PATH,
    AgentRoleBinding,
    catalog_name_path,
    start_request_body,
)

PROVIDER_CANARY_ROLE = "provider-canary"
_RECEIPT_ALONE_NOT_STARTABLE_REASONS = frozenset(
    (
        AgentConfigurationNotStartableReason.PROVIDER_PROBE_RECEIPT_MISSING,
        AgentConfigurationNotStartableReason.PROVIDER_PROBE_FAILED,
    )
)
"""The two reasons discovery still offers as a vector (#1103): either one names
live evidence a fresh probe replaces, never a structural or registry problem."""
PROVIDER_CANARY_RECEIPT_VALIDITY = timedelta(hours=26)
PROVIDER_CANARY_TERMINAL_TIMEOUT_SECONDS = 300.0
PROVIDER_CANARY_POLL_INTERVAL_SECONDS = 2.0
PROVIDER_CANARY_HTTP_TIMEOUT_SECONDS = 30.0
PROVIDER_CANARY_CONFIGURATION_PAGE_SIZE = 50
PROVIDER_CANARY_MAXIMUM_CONFIGURATION_PAGES = 4
PROVIDER_CANARY_MAXIMUM_VECTORS = 50
PROVIDER_CANARY_MAXIMUM_CONCURRENT_VECTORS = 8
"""Bounds the thread pool, not the vector count: every discovered vector still
gets its own receipt, just never more than this many live billed runs in
flight together."""
PROVIDER_CANARY_DISCOVERY_TIMEOUT_SECONDS = 300.0
PROVIDER_CANARY_PROCESS_TIMEOUT_SECONDS = (
    PROVIDER_CANARY_DISCOVERY_TIMEOUT_SECONDS
    + PROVIDER_CANARY_MAXIMUM_VECTORS * PROVIDER_CANARY_TERMINAL_TIMEOUT_SECONDS
)
# The post-Serve-start drop-in fires this run the instant the Serve process
# begins, not once it can answer, so it races the ASGI startup rather than
# waiting behind it. 60 s comfortably outlasts every observed cold start while
# still failing loud long before an operator would suspect a hang.
PROVIDER_CANARY_HEALTH_WAIT_TIMEOUT_SECONDS = 60.0
PROVIDER_CANARY_HEALTH_WAIT_POLL_INTERVAL_SECONDS = 1.0
PROVIDER_CANARY_STATE_RELATIVE_PATH = Path("atelier2/provider-probes/live")

_health_resource = TypeAdapter(HealthResource)
_configuration_page_resource = TypeAdapter(AgentConfigurationRevisionPageResource)
_catalog_name_resolution_resource = TypeAdapter(CatalogNameResolutionResource)
_run_resource = TypeAdapter[RunResourceV3](RunResourceV3)
_node_detail_resource = TypeAdapter[NodeDetailResource](NodeDetailResource)

# These executor keys choose the matching probe workflow only. The served
# structurally-startable configuration list remains the sole owner of which
# vectors run, using the same executor constants that the Serve composition
# registers.
_WORKFLOW_BY_EXECUTOR = {
    (
        CLAUDE_SUBSCRIPTION_EXECUTOR_KEY.provider_id.value,
        CLAUDE_SUBSCRIPTION_EXECUTOR_KEY.executor_revision.value,
    ): PROVIDER_CANARY_HEADLESS_WORKFLOW_NAME,
    (
        CODEX_SUBSCRIPTION_EXECUTOR_KEY.provider_id.value,
        CODEX_SUBSCRIPTION_EXECUTOR_KEY.executor_revision.value,
    ): PROVIDER_CANARY_HEADLESS_WORKFLOW_NAME,
    (
        GROK_SUBSCRIPTION_EXECUTOR_KEY.provider_id.value,
        GROK_SUBSCRIPTION_EXECUTOR_KEY.executor_revision.value,
    ): PROVIDER_CANARY_HEADLESS_WORKFLOW_NAME,
    (
        CLAUDE_WORKSPACE_TOOLS_EXECUTOR_KEY.provider_id.value,
        CLAUDE_WORKSPACE_TOOLS_EXECUTOR_KEY.executor_revision.value,
    ): PROVIDER_CANARY_WORKSPACE_TOOLS_WORKFLOW_NAME,
    (
        GROK_WORKSPACE_TOOLS_EXECUTOR_KEY.provider_id.value,
        GROK_WORKSPACE_TOOLS_EXECUTOR_KEY.executor_revision.value,
    ): PROVIDER_CANARY_WORKSPACE_TOOLS_WORKFLOW_NAME,
    (
        CLAUDE_ATELIER_DOORS_EXECUTOR_KEY.provider_id.value,
        CLAUDE_ATELIER_DOORS_EXECUTOR_KEY.executor_revision.value,
    ): PROVIDER_CANARY_ATELIER_DOORS_WORKFLOW_NAME,
}


class ProviderCanaryServerUnavailable(RuntimeError):
    """The configured HTTP service could not answer at all."""


class ProviderCanaryHttpRefused(RuntimeError):
    """The service answered one typed refusal rather than the requested value."""

    def __init__(self, problem_code: str, detail: str = "") -> None:
        super().__init__(detail or problem_code)
        self.problem_code = problem_code


class ProviderCanaryAnswerUnreadable(RuntimeError):
    """An external answer did not satisfy the small shape this client reads."""


class ProviderCanaryRunTimedOut(RuntimeError):
    """A started provider canary did not reach terminal before its bound."""


class ProviderCanaryProcessTimedOut(RuntimeError):
    """The provider-canary process exhausted its total work budget."""


class ProviderCanaryDiscoveryFailed(RuntimeError):
    """No complete, nonempty startable provider-vector set was discovered."""


class ProviderCanaryWorkflowUnreadable(RuntimeError):
    """The deployed canary workflow bytes could not be read locally."""


class ProviderCanaryHttp(Protocol):
    def get(self, path: str, *, timeout_seconds: float) -> bytes: ...

    def post(
        self,
        path: str,
        body: bytes,
        *,
        timeout_seconds: float,
        media_type: str = JSON_MEDIA_TYPE,
    ) -> bytes: ...


class ProviderCanaryClock(Protocol):
    def now(self) -> datetime: ...

    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class SystemProviderCanaryClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


@dataclass(frozen=True, slots=True)
class ProviderCanarySettings:
    service_url: str
    workflow_directory: Path
    state_directory: Path
    terminal_timeout_seconds: float = PROVIDER_CANARY_TERMINAL_TIMEOUT_SECONDS
    poll_interval_seconds: float = PROVIDER_CANARY_POLL_INTERVAL_SECONDS
    process_timeout_seconds: float = PROVIDER_CANARY_PROCESS_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        address = urlsplit(self.service_url)
        if address.scheme not in ADDRESSABLE_SCHEMES or not address.netloc:
            raise ValueError(
                f"{self.service_url!r} is not the address of a served Atelier API"
            )
        if self.terminal_timeout_seconds <= 0:
            raise ValueError("provider canary terminal timeout must be positive")
        if self.poll_interval_seconds <= 0:
            raise ValueError("provider canary poll interval must be positive")
        if self.process_timeout_seconds <= 0:
            raise ValueError("provider canary process timeout must be positive")


@dataclass(frozen=True, slots=True)
class ProviderCanaryFailure:
    vector: ProviderProbeVectorId
    problem_code: ProviderProbeProblemCode
    detail: str


class ProviderLayerReceiptOutcome(StrEnum):
    """What the newest readable prior receipt says about this run's provider layer.

    The state directory is never pruned, so a superseded configuration's
    receipts can still sit beside current ones (#1124): the newest readable
    receipt by `observed_at` is the last thing this deployment wrote, so it
    alone answers whether this redeploy turned the evidence over. Either no
    readable receipt exists yet (nothing to compare, most plausibly the first
    run after this very deploy), or the newest one's digest still matches, or
    it does not.
    """

    NO_READABLE_PRIOR_RECEIPT = "no-readable-prior-receipt"
    RECEIPTS_KEPT = "receipts-kept"
    RECEIPTS_INVALIDATED = "receipts-invalidated"


@dataclass(frozen=True, slots=True)
class ProviderLayerReceiptStatus:
    """The typed answer `_provider_layer_status` computes for one run (#1124)."""

    outcome: ProviderLayerReceiptOutcome
    current_digest: Sha256Hash
    previous_digest: Sha256Hash | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, ProviderLayerReceiptOutcome):
            raise TypeError("a provider layer receipt status names a typed outcome")
        if not isinstance(self.current_digest, Sha256Hash):
            raise TypeError("a provider layer receipt status names a typed digest")
        carries_previous_digest = self.previous_digest is not None
        names_a_change = (
            self.outcome is ProviderLayerReceiptOutcome.RECEIPTS_INVALIDATED
        )
        if carries_previous_digest != names_a_change:
            raise ValueError(
                "a provider layer receipt status names a previous digest only "
                "when receipts were invalidated"
            )


@dataclass(frozen=True, slots=True)
class ProviderCanaryReport:
    attempted: int
    failures: tuple[ProviderCanaryFailure, ...]
    provider_layer_status: ProviderLayerReceiptStatus
    """Whether existing receipts still apply to this run (#1124)."""

    @property
    def failed(self) -> int:
        return len(self.failures)


@dataclass(frozen=True, slots=True)
class _CanaryVector:
    vector_id: ProviderProbeVectorId
    configuration_hash: AgentConfigurationRevisionHash
    workflow_name: str


class AtelierApiProviderCanaryHttp:
    """The narrow HTTP boundary used by the live command.

    `transport` is a test seam only: production composition leaves it unset,
    so it reaches the real network.
    """

    def __init__(
        self,
        service_url: str = DEFAULT_SERVICE_URL,
        *,
        transport: AtelierApiTransport | None = None,
    ) -> None:
        self._api = AtelierApi(service_url, transport=transport)

    def close(self) -> None:
        self._api.close()

    def get(self, path: str, *, timeout_seconds: float) -> bytes:
        return self._called(
            timeout_seconds, lambda timeout: self._api.get(path, timeout=timeout)
        )

    def post(
        self,
        path: str,
        body: bytes,
        *,
        timeout_seconds: float,
        media_type: str = JSON_MEDIA_TYPE,
    ) -> bytes:
        return self._called(
            timeout_seconds,
            lambda timeout: self._api.post(
                path, body, media_type=media_type, timeout=timeout
            ),
        )

    def _called(
        self, timeout_seconds: float, request: Callable[[float], bytes]
    ) -> bytes:
        if timeout_seconds <= 0:
            raise ValueError("provider canary HTTP timeout must be positive")
        try:
            return request(min(PROVIDER_CANARY_HTTP_TIMEOUT_SECONDS, timeout_seconds))
        except AtelierApiTransportFailure as failure:
            if failure.status is None:
                raise ProviderCanaryServerUnavailable(failure.reason) from failure
            problem_code, detail = _problem_answer(failure.body, str(failure))
            raise ProviderCanaryHttpRefused(problem_code, detail) from failure


def default_provider_canary_state_directory(
    environment: Mapping[str, str] = os.environ,
) -> Path:
    state_home = environment.get("XDG_STATE_HOME")
    root = Path(state_home) if state_home else Path.home() / ".local/state"
    return root / PROVIDER_CANARY_STATE_RELATIVE_PATH


_PROVIDER_LAYER_FRAME_DOMAIN = "provider-layer/v1"
_PROVIDER_LAYER_ADAPTER_MODULES = (
    claude_subscription_adapter,
    codex_subscription_adapter,
    grok_subscription_adapter,
)
"""Every module that decides how this deployment talks to a provider, named
through the same modules `_WORKFLOW_BY_EXECUTOR` above draws its executor keys
from -- not a second, separately maintained file list -- so a fourth provider
adapter joins the digest the moment its module is imported here for its own
executor keys. Narrower than the full provider surface on purpose: the
pinned CLI executable path (`serve-live.sh`'s `--claude-executable`), the
probe workflow bytes (`workflows/provider-canary-*.yaml`), and the executor
start-binding wiring stay out -- OPERATIONS.md names that residual, backstopped
by the 26-hour receipt validity."""


def provider_layer_digest() -> Sha256Hash:
    """Hash the exact bytes that decide how this deployment talks to a provider.

    Every module in `_PROVIDER_LAYER_ADAPTER_MODULES`, plus this canary client
    and the receipt contract it writes -- together the places provider
    behaviour is actually decided. Computed from files on disk alone,
    identically by the Serve process
    (`adapters/dbos/runtime.py`'s receipt gate, wired through
    `host/serving.py`) and by this canary client running beside it on the same
    checkout, so neither has to learn the other's live state to agree. A
    receipt embeds this digest instead of the whole `source_commit`: a
    redeploy that leaves every one of these files unchanged leaves every
    receipt valid, and touching even one file this deployment never arms
    still turns every receipt over -- the conservative side of "provider
    behaviour might have changed."
    """

    paths = sorted(
        {Path(inspect.getfile(module)) for module in _PROVIDER_LAYER_ADAPTER_MODULES}
        | {Path(__file__), Path(inspect.getfile(ProviderProbeReceipt))}
    )
    return Sha256Hash.of(
        frame(_PROVIDER_LAYER_FRAME_DOMAIN, *(path.read_bytes() for path in paths))
    )


def _discovery_timeout() -> ProviderCanaryDiscoveryFailed:
    return ProviderCanaryDiscoveryFailed(
        "discovery-timeout: provider discovery exceeded its bounded deadline"
    )


def _process_timeout() -> ProviderCanaryProcessTimedOut:
    return ProviderCanaryProcessTimedOut(
        "provider canary exceeded its total process deadline"
    )


def _vector_timeout(
    vector: _CanaryVector, *, process_deadline: float, terminal_deadline: float
) -> Callable[[], RuntimeError]:
    if process_deadline <= terminal_deadline:
        return _process_timeout

    def timed_out() -> ProviderCanaryRunTimedOut:
        return ProviderCanaryRunTimedOut(
            f"provider vector {vector.vector_id.value} did not reach terminal "
            "within its configured deadline"
        )

    return timed_out


def _remaining_before_deadline(
    clock: ProviderCanaryClock,
    deadline: float,
    timeout_error: Callable[[], RuntimeError],
) -> float:
    remaining = deadline - clock.monotonic()
    if remaining <= 0:
        raise timeout_error()
    return remaining


def _raise_if_deadline_reached(
    clock: ProviderCanaryClock,
    deadline: float,
    timeout_error: Callable[[], RuntimeError],
) -> None:
    _remaining_before_deadline(clock, deadline, timeout_error)


def _get_before_deadline(
    client: ProviderCanaryHttp,
    path: str,
    *,
    clock: ProviderCanaryClock,
    deadline: float,
    timeout_error: Callable[[], RuntimeError],
) -> bytes:
    try:
        answer = client.get(
            path,
            timeout_seconds=_remaining_before_deadline(clock, deadline, timeout_error),
        )
    except (ProviderCanaryHttpRefused, ProviderCanaryServerUnavailable) as failure:
        if clock.monotonic() >= deadline:
            raise timeout_error() from failure
        raise
    _raise_if_deadline_reached(clock, deadline, timeout_error)
    return answer


def _post_before_deadline(
    client: ProviderCanaryHttp,
    path: str,
    body: bytes,
    *,
    clock: ProviderCanaryClock,
    deadline: float,
    timeout_error: Callable[[], RuntimeError],
) -> bytes:
    try:
        answer = client.post(
            path,
            body,
            timeout_seconds=_remaining_before_deadline(clock, deadline, timeout_error),
        )
    except (ProviderCanaryHttpRefused, ProviderCanaryServerUnavailable) as failure:
        if clock.monotonic() >= deadline:
            raise timeout_error() from failure
        raise
    _raise_if_deadline_reached(clock, deadline, timeout_error)
    return answer


def execute_provider_canaries(
    settings: ProviderCanarySettings,
    *,
    http: ProviderCanaryHttp | None = None,
    clock: ProviderCanaryClock | None = None,
    on_provider_layer_status: Callable[[ProviderLayerReceiptStatus], None]
    | None = None,
) -> ProviderCanaryReport:
    """Run each currently startable, known provider vector exactly once.

    `on_provider_layer_status`, when given, fires the instant the provider
    layer status is known -- discovery complete, no vector started yet -- so a
    caller can journal it at that moment rather than waiting for every vector
    to finish (#1124)."""

    owned_client: AtelierApiProviderCanaryHttp | None = None
    client: ProviderCanaryHttp
    if http is not None:
        client = http
    else:
        owned_client = AtelierApiProviderCanaryHttp(settings.service_url)
        client = owned_client
    try:
        canary_clock = clock or SystemProviderCanaryClock()
        started_at = canary_clock.monotonic()
        process_deadline = started_at + settings.process_timeout_seconds
        discovery_deadline = min(
            process_deadline, started_at + PROVIDER_CANARY_DISCOVERY_TIMEOUT_SECONDS
        )
        health_wait_deadline = min(
            discovery_deadline, started_at + PROVIDER_CANARY_HEALTH_WAIT_TIMEOUT_SECONDS
        )
        try:
            health = _wait_for_serving_health(
                client,
                clock=canary_clock,
                deadline=health_wait_deadline,
                poll_interval_seconds=PROVIDER_CANARY_HEALTH_WAIT_POLL_INTERVAL_SECONDS,
            )
            if (
                PROVIDER_PROBE_SOURCE_COMMIT_FORMAT.fullmatch(health.source_commit)
                is None
            ):
                raise ProviderCanaryAnswerUnreadable(
                    "the service health source commit cannot identify receipt provenance"
                )
            vectors = _configured_vectors(
                client, clock=canary_clock, deadline=discovery_deadline
            )
            if not vectors:
                raise ProviderCanaryDiscoveryFailed(
                    "no-startable-provider-vectors: "
                    "the service listed no startable provider vectors"
                )
            admitted_workflows = _resolve_admitted_workflows(
                vectors,
                client,
                clock=canary_clock,
                deadline=discovery_deadline,
            )
            _raise_if_deadline_reached(
                canary_clock, discovery_deadline, _discovery_timeout
            )
        except ProviderCanaryDiscoveryFailed:
            raise
        except (
            ProviderCanaryServerUnavailable,
            ProviderCanaryHttpRefused,
            ProviderCanaryAnswerUnreadable,
        ) as failure:
            raise ProviderCanaryDiscoveryFailed(
                _discovery_problem_text(failure)
            ) from failure
        running_digest = provider_layer_digest()
        provider_layer_status = _provider_layer_status(
            settings.state_directory, running_digest
        )
        if on_provider_layer_status is not None:
            on_provider_layer_status(provider_layer_status)
        _raise_if_deadline_reached(canary_clock, process_deadline, _process_timeout)

        # No more than `PROVIDER_CANARY_MAXIMUM_CONCURRENT_VECTORS` live billed
        # runs are ever in flight together, but each vector still gets its own
        # receipt the instant its own outcome is known: a run-timeout vector
        # bounds only its own receipt, never delaying or starving the vectors
        # beside it. Each `_execute_vector` call writes its receipt itself, so
        # a receipt lands as soon as its vector finishes regardless of how
        # long a sibling vector keeps running.
        def run_one(vector: _CanaryVector) -> ProviderCanaryFailure | None:
            return _execute_vector(
                settings,
                vector,
                admitted_workflows[vector.workflow_name],
                health,
                running_digest,
                client,
                canary_clock,
                process_deadline,
            )

        pool_workers = min(len(vectors), PROVIDER_CANARY_MAXIMUM_CONCURRENT_VECTORS)
        with ThreadPoolExecutor(max_workers=pool_workers) as pool:
            failures = tuple(
                failure for failure in pool.map(run_one, vectors) if failure is not None
            )
        return ProviderCanaryReport(len(vectors), failures, provider_layer_status)
    finally:
        if owned_client is not None:
            owned_client.close()


def _provider_layer_status(
    state_directory: Path, digest: Sha256Hash
) -> ProviderLayerReceiptStatus:
    """The typed answer naming whether existing receipts still apply (#1124).

    Read before this run overwrites anything: the newest readable prior
    receipt names what the deployment's evidence looked like a moment ago,
    and its own `provider_layer_digest` either still matches this run's or
    does not. The state directory is never pruned, so an older, superseded
    configuration's receipt can still sit beside newer ones written under the
    current configuration -- only the newest one is the last thing this
    deployment actually wrote, so only it can answer "did this redeploy turn
    my evidence over". A directory holding no receipt at all, or holding only
    receipts this runtime refuses to read (an old pre-#1124 shape, for one),
    names no prior evidence instead of falsely claiming it was kept -- the bug
    an earlier version of this function had on the very deploy that lands the
    digest gate.
    """

    try:
        entries = state_directory.glob("*.json")
    except OSError:
        entries = []
    readable_receipts: list[ProviderProbeReceipt] = []
    for entry in entries:
        try:
            document = entry.read_bytes()
        except OSError:
            continue
        receipt = read_provider_probe_receipt(document)
        if isinstance(receipt, ProviderProbeReceiptRefused):
            continue
        readable_receipts.append(receipt)
    if not readable_receipts:
        return ProviderLayerReceiptStatus(
            ProviderLayerReceiptOutcome.NO_READABLE_PRIOR_RECEIPT, current_digest=digest
        )
    newest_receipt = max(
        readable_receipts, key=lambda receipt: receipt.observed_at.value
    )
    if newest_receipt.provider_layer_digest != digest:
        return ProviderLayerReceiptStatus(
            ProviderLayerReceiptOutcome.RECEIPTS_INVALIDATED,
            current_digest=digest,
            previous_digest=newest_receipt.provider_layer_digest,
        )
    return ProviderLayerReceiptStatus(
        ProviderLayerReceiptOutcome.RECEIPTS_KEPT, current_digest=digest
    )


def _discovery_problem_text(
    failure: ProviderCanaryServerUnavailable
    | ProviderCanaryHttpRefused
    | ProviderCanaryAnswerUnreadable,
) -> str:
    """The same bounded classification prefix an immediate discovery failure
    uses, reused for the last health answer a bounded wait gives up on."""

    if isinstance(failure, ProviderCanaryServerUnavailable):
        return f"server-unavailable: {failure}"
    if isinstance(failure, ProviderCanaryHttpRefused):
        return f"{_bounded_problem_code(failure.problem_code).value}: {failure}"
    return f"server-answer-unreadable: {failure}"


def _wait_for_serving_health(
    client: ProviderCanaryHttp,
    *,
    clock: ProviderCanaryClock,
    deadline: float,
    poll_interval_seconds: float,
) -> HealthResource:
    """Poll `/health` until it answers `serving`, before any vector is tried.

    The post-Serve-start drop-in fires this run at process start, not once the
    process can answer, so a refused connection or a not-yet-mounted route is
    the expected first answer after an auto-deploy, not a defect. Absorbing
    that race here -- the one place both the timer and a hand run share --
    keeps every vector from meeting a spurious `server-unavailable` and
    leaving a fail receipt for a deploy that was actually fine.
    """

    last_health_text = "no health answer was received"
    while True:
        remaining = deadline - clock.monotonic()
        if remaining <= 0:
            raise ProviderCanaryDiscoveryFailed(
                "health-wait-timeout: /health never answered serving within its "
                f"bounded wait; last health answer: {last_health_text}"
            )
        try:
            document = client.get("/health", timeout_seconds=remaining)
            return _decoded(_health_resource, document, "health")
        except (
            ProviderCanaryServerUnavailable,
            ProviderCanaryHttpRefused,
            ProviderCanaryAnswerUnreadable,
        ) as failure:
            last_health_text = _discovery_problem_text(failure)
        clock.sleep(min(poll_interval_seconds, max(deadline - clock.monotonic(), 0.0)))


def _configured_vectors(
    client: ProviderCanaryHttp,
    *,
    clock: ProviderCanaryClock,
    deadline: float,
) -> tuple[_CanaryVector, ...]:
    vectors: list[_CanaryVector] = []
    after: str | None = None
    for page_number in range(1, PROVIDER_CANARY_MAXIMUM_CONFIGURATION_PAGES + 1):
        query = urlencode(
            {
                "limit": PROVIDER_CANARY_CONFIGURATION_PAGE_SIZE,
                **({"after_revision_hash": after} if after is not None else {}),
            }
        )
        page = _decoded(
            _configuration_page_resource,
            _get_before_deadline(
                client,
                f"{AGENT_CONFIGURATION_PATH}?{query}",
                clock=clock,
                deadline=deadline,
                timeout_error=_discovery_timeout,
            ),
            "agent-configuration page",
        )
        # A vector is a configuration the deployment's own snapshot already
        # calls `startable`, or one whose only problem is the receipt this
        # very run would write -- either no receipt on file yet
        # (`provider-probe-receipt-missing`) or one whose last recorded
        # result was itself a failure (`provider-probe-failed`, #1103): both
        # name exactly the evidence a fresh probe replaces, the same two
        # tokens `AgentConfigurationNotStartableReason` owns. A superseded
        # configuration now carries its own distinct reason
        # (`model-not-registered`) computed by the same cast lookup a start
        # makes (`agent_catalog.py` -> `resolve_start_bindings.
        # configuration_registered`), so it is excluded honestly rather than
        # offered as though a fresh probe would fix it. Discovery derives
        # nothing of its own from either field -- no local registry fetch, no
        # local cast -- so it can never drift from what a real start would
        # decide, and a redeploy that invalidates every receipt still leaves
        # every genuinely registered configuration reprobable.
        vectors.extend(
            vector
            for item in page.items
            if (
                item.startable
                or item.not_startable_reason in _RECEIPT_ALONE_NOT_STARTABLE_REASONS
            )
            and (vector := _canary_vector(item)) is not None
        )
        if len(vectors) > PROVIDER_CANARY_MAXIMUM_VECTORS:
            raise ProviderCanaryDiscoveryFailed(
                "too-many-provider-vectors: the service listed more than "
                f"{PROVIDER_CANARY_MAXIMUM_VECTORS} known startable provider vectors"
            )
        after = page.next_after_revision_hash
        if after is None:
            return tuple(vectors)
        if page_number == PROVIDER_CANARY_MAXIMUM_CONFIGURATION_PAGES:
            raise ProviderCanaryDiscoveryFailed(
                "too-many-configuration-pages: the service requires more than "
                f"{PROVIDER_CANARY_MAXIMUM_CONFIGURATION_PAGES} configuration pages"
            )
    raise AssertionError("bounded configuration pagination did not terminate")


def _resolve_admitted_workflows(
    vectors: tuple[_CanaryVector, ...],
    client: ProviderCanaryHttp,
    *,
    clock: ProviderCanaryClock,
    deadline: float,
) -> dict[str, WorkflowRevisionHash]:
    admitted: dict[str, WorkflowRevisionHash] = {}
    for workflow_name in dict.fromkeys(vector.workflow_name for vector in vectors):
        resolved = _decoded(
            _catalog_name_resolution_resource,
            _get_before_deadline(
                client,
                catalog_name_path(RevisionKind.WORKFLOW, workflow_name),
                clock=clock,
                deadline=deadline,
                timeout_error=_discovery_timeout,
            ),
            "admitted workflow name",
        )
        admitted[workflow_name] = WorkflowRevisionHash(resolved.catalog_revision_hash)
    return admitted


def _canary_vector(
    configuration: AgentConfigurationRevisionListItemResource,
) -> _CanaryVector | None:
    workflow_name = _workflow_for(
        configuration.provider_id, configuration.executor_revision
    )
    if workflow_name is None:
        return None
    configuration_hash = AgentConfigurationRevisionHash(
        configuration.agent_configuration_revision_hash
    )
    kind = workflow_name.removeprefix("provider-canary-")
    return _CanaryVector(
        ProviderProbeVectorId(f"{kind}-{configuration_hash.value}"),
        configuration_hash,
        workflow_name,
    )


def _workflow_for(provider_id: str, executor_revision: str) -> str | None:
    return _WORKFLOW_BY_EXECUTOR.get((provider_id, executor_revision))


class _StartRequestRefused(RuntimeError):
    """The POST /runs answer itself was a typed or generic refusal.

    Raised only around the start request, never around the terminal poll
    that follows it, so `start-refused-*` names exactly what it says: the
    start's own answer, not an unrelated refusal met while merely watching
    the run it already accepted.
    """

    def __init__(self, problem_code: ProviderProbeProblemCode, detail: str) -> None:
        super().__init__(detail)
        self.problem_code = problem_code
        self.detail = detail


def _execute_vector(
    settings: ProviderCanarySettings,
    vector: _CanaryVector,
    admitted_workflow_hash: WorkflowRevisionHash,
    health: HealthResource,
    provider_layer_digest: Sha256Hash,
    client: ProviderCanaryHttp,
    clock: ProviderCanaryClock,
    process_deadline: float,
) -> ProviderCanaryFailure | None:
    vector_started_at = clock.monotonic()
    terminal_deadline = vector_started_at + settings.terminal_timeout_seconds
    deadline = min(process_deadline, terminal_deadline)
    timeout_error = _vector_timeout(
        vector, process_deadline=process_deadline, terminal_deadline=terminal_deadline
    )
    workflow_hash = admitted_workflow_hash
    run_id = _run_id(vector.vector_id, clock.now().astimezone(UTC))
    workflow_path = settings.workflow_directory / f"{vector.workflow_name}.yaml"
    try:
        _raise_if_deadline_reached(clock, deadline, timeout_error)
        try:
            workflow_document = workflow_path.read_bytes()
        except OSError as unreadable:
            raise ProviderCanaryWorkflowUnreadable(
                f"could not read {workflow_path}: {unreadable}"
            ) from unreadable
        _raise_if_deadline_reached(clock, deadline, timeout_error)
        workflow_hash = WorkflowRevisionHash.of(workflow_document)
        if admitted_workflow_hash != workflow_hash:
            raise ProviderCanaryAnswerUnreadable(
                f"the admitted {vector.workflow_name} revision does not identify "
                "the deployed workflow bytes"
            )
        binding = AgentRoleBinding(
            PROVIDER_CANARY_ROLE, vector.configuration_hash.value
        )
        try:
            posted = _post_before_deadline(
                client,
                RUN_PATH,
                # The shared public-run owner emits StartRunRequestResourceV2:
                # bindings are present and these workflows declare no orders.
                start_request_body(run_id.value, workflow_hash.value, (binding,)),
                clock=clock,
                deadline=deadline,
                timeout_error=timeout_error,
            )
        except ProviderCanaryHttpRefused as refused:
            raise _StartRequestRefused(
                _start_refused_problem_code(refused.problem_code), str(refused)
            ) from refused
        started = _decoded(_run_resource, posted, "started run")
        ended = _wait_for_terminal(
            client,
            started,
            clock,
            deadline,
            settings.poll_interval_seconds,
            timeout_error,
        )
        if ended.state == "COMPLETED":
            assert ended.terminal_hash is not None
            receipt = _receipt(
                vector,
                workflow_hash,
                provider_layer_digest,
                health.source_commit,
                run_id,
                clock,
                terminal_hash=Sha256Hash(ended.terminal_hash),
            )
            write_provider_canary_receipt_atomic(
                settings.state_directory / f"{vector.vector_id.value}.json", receipt
            )
            return None
        problem_code = (
            _failed_run_problem_code(
                client, started, ended, clock, deadline, timeout_error
            )
            if ended.state == "FAILED"
            else ProviderProbeProblemCode(f"run-{ended.state.lower()}")
        )
        detail = f"run {run_id.value} ended {ended.state}"
    except ProviderCanaryServerUnavailable as unavailable:
        problem_code = ProviderProbeProblemCode("server-unavailable")
        detail = str(unavailable)
    except ProviderCanaryRunTimedOut as timed_out:
        problem_code = ProviderProbeProblemCode("run-timeout")
        detail = str(timed_out)
    except ProviderCanaryProcessTimedOut as timed_out:
        problem_code = ProviderProbeProblemCode("process-timeout")
        detail = str(timed_out)
    except _StartRequestRefused as refused:
        problem_code = refused.problem_code
        detail = refused.detail
    except ProviderCanaryHttpRefused as refused:
        problem_code = _bounded_problem_code(refused.problem_code)
        detail = str(refused)
    except ProviderCanaryAnswerUnreadable as unreadable:
        problem_code = ProviderProbeProblemCode("server-answer-unreadable")
        detail = str(unreadable)
    except ProviderCanaryWorkflowUnreadable as unreadable:
        problem_code = ProviderProbeProblemCode("workflow-unreadable")
        detail = str(unreadable)
    receipt = _receipt(
        vector,
        workflow_hash,
        provider_layer_digest,
        health.source_commit,
        run_id,
        clock,
        problem_code=problem_code,
    )
    write_provider_canary_receipt_atomic(
        settings.state_directory / f"{vector.vector_id.value}.json", receipt
    )
    return ProviderCanaryFailure(vector.vector_id, problem_code, detail)


def _run_id(vector: ProviderProbeVectorId, observed: datetime) -> RunId:
    return RunId(
        "provider-canary/"
        f"{vector.value}/"
        f"{observed.astimezone(UTC).isoformat(timespec='microseconds')}"
    )


def _wait_for_terminal(
    client: ProviderCanaryHttp,
    started: RunResourceV3,
    clock: ProviderCanaryClock,
    deadline: float,
    poll_interval_seconds: float,
    timeout_error: Callable[[], RuntimeError],
) -> RunResourceV3:
    current = started
    while current.state not in {"COMPLETED", "FAILED", "CANCELLED"}:
        _raise_if_deadline_reached(clock, deadline, timeout_error)
        current = _decoded(
            _run_resource,
            _get_before_deadline(
                client,
                f"{RUN_PATH}/{started.public_run_reference}",
                clock=clock,
                deadline=deadline,
                timeout_error=timeout_error,
            ),
            "run",
        )
        if current.state not in {"COMPLETED", "FAILED", "CANCELLED"}:
            clock.sleep(
                min(
                    poll_interval_seconds,
                    _remaining_before_deadline(clock, deadline, timeout_error),
                )
            )
            _raise_if_deadline_reached(clock, deadline, timeout_error)
    if current.terminal_hash is None:
        raise ProviderCanaryAnswerUnreadable("a terminal run carries no terminal hash")
    return current


def _failed_run_problem_code(
    client: ProviderCanaryHttp,
    started: RunResourceV3,
    ended: RunResourceV3,
    clock: ProviderCanaryClock,
    deadline: float,
    timeout_error: Callable[[], RuntimeError],
) -> ProviderProbeProblemCode:
    """`provider-refused` where the failed node's own transcript named one.

    A run's own terminal state carries no more than `FAILED`; telling a
    provider that read and refused a call apart from a genuinely broken vector
    needs the node's own transcript (#1029), so a rate limit does not read as a
    defect. Reading that transcript is itself a live call this classification
    alone should not fail over: any refusal to answer it just keeps the plain
    `run-failed` code the caller already had.
    """

    try:
        detail = _decoded(
            _node_detail_resource,
            _get_before_deadline(
                client,
                f"{RUN_PATH}/{started.public_run_reference}/nodes/"
                f"{ended.current_node_id}",
                clock=clock,
                deadline=deadline,
                timeout_error=timeout_error,
            ),
            "failed node detail",
        )
    except (
        ProviderCanaryHttpRefused,
        ProviderCanaryServerUnavailable,
        ProviderCanaryAnswerUnreadable,
    ):
        return ProviderProbeProblemCode("run-failed")
    if detail.transcript is not None and any(
        event.event == TranscriptEventKind.PROVIDER_TERMINAL_REFUSAL
        for event in detail.transcript.events
    ):
        return ProviderProbeProblemCode("provider-refused")
    return ProviderProbeProblemCode("run-failed")


def _receipt(
    vector: _CanaryVector,
    workflow_hash: WorkflowRevisionHash,
    provider_layer_digest: Sha256Hash,
    source_commit: str,
    run_id: RunId,
    clock: ProviderCanaryClock,
    *,
    terminal_hash: Sha256Hash | None = None,
    problem_code: ProviderProbeProblemCode | None = None,
) -> ProviderProbeReceipt:
    observed = clock.now().astimezone(UTC)
    return ProviderProbeReceipt(
        vector.vector_id,
        vector.configuration_hash,
        workflow_hash,
        provider_layer_digest,
        source_commit,
        recorded_instant(observed),
        recorded_instant(observed + PROVIDER_CANARY_RECEIPT_VALIDITY),
        (
            ProviderProbeResult.SUCCEEDED
            if terminal_hash is not None
            else ProviderProbeResult.FAILED
        ),
        run_id,
        terminal_hash,
        problem_code,
    )


def write_provider_canary_receipt_atomic(
    destination: Path, receipt: ProviderProbeReceipt
) -> None:
    """Replace one receipt only after its complete bytes reach a sibling file."""

    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(receipt.canonical_bytes())
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        directory_descriptor = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _decoded[AnswerT](
    answer_type: TypeAdapter[AnswerT], document: bytes, name: str
) -> AnswerT:
    try:
        return answer_type.validate_json(document)
    except ValidationError as unreadable:
        raise ProviderCanaryAnswerUnreadable(
            f"the service did not answer {name}: {unreadable}"
        ) from unreadable


def _problem_answer(document: bytes, fallback: str) -> tuple[str, str]:
    if len(document) > MAXIMUM_FAILURE_BODY_BYTES:
        return "http-refused", fallback
    try:
        decoded = json.loads(document)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return "http-refused", fallback
    if not isinstance(decoded, dict):
        return "http-refused", fallback
    raw_type = decoded.get("type")
    detail = decoded.get("detail")
    # The problem-vocabulary owner (`atelier2.api.problems`) frames every type
    # as `urn:atelier2:problem:v1:<code>` -- colon-separated, not `/`-separated
    # -- so the code is read by stripping that exact prefix, never guessed.
    problem_code = (
        raw_type.removeprefix(PROBLEM_TYPE_PREFIX)
        if isinstance(raw_type, str) and raw_type.startswith(PROBLEM_TYPE_PREFIX)
        else "http-refused"
    )
    return problem_code, detail if isinstance(detail, str) else fallback


def _bounded_problem_code(raw: str) -> ProviderProbeProblemCode:
    try:
        return ProviderProbeProblemCode(raw)
    except (TypeError, ValueError):
        return ProviderProbeProblemCode("http-refused")


def _start_refused_problem_code(raw: str) -> ProviderProbeProblemCode:
    """A typed refusal the start answered, named as its own honest token.

    `raw` already carries the correctly parsed problem code (`_problem_answer`
    reads the type from its owning vocabulary), so this only prefixes it into
    the canary's own namespace -- distinguishing "the start refused this
    binding with a named problem" from every other receipt code this module
    assigns. `ProviderProbeProblemCode` bounds its token to lowercase ASCII
    and hyphens, so the prefix joins with a hyphen rather than the colon the
    problem vocabulary itself uses. `raw` itself already reading `http-refused`
    means no type was ever recovered -- that ambient case stays exactly
    `http-refused`, not a hollow "start-refused" wrapper around nothing.
    """

    if raw == "http-refused":
        return ProviderProbeProblemCode("http-refused")
    try:
        return ProviderProbeProblemCode(f"start-refused-{raw}")
    except (TypeError, ValueError):
        return ProviderProbeProblemCode("http-refused")
