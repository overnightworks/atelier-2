from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from unittest.mock import patch
from urllib.parse import parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import sqlalchemy as sa
import uvicorn
from fastapi import FastAPI
from starlette.types import ASGIApp, Lifespan, Message, Receive, Scope, Send

from atelier2.adapters.dbos import workflow as dbos_workflow
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import runs
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.api.context import ApiContext, ApiPorts
from atelier2.api.limits import ApiLimits
from atelier2.api.references import decode_public_run_reference
from atelier2.api.stream import EventPollBackoff
from atelier2.application.model_configuration import (
    ModelRegistryPublished,
    ModelRegistryUnchanged,
    ProjectModelDefaultsMissing,
    ProjectModelDefaultsPublished,
    ProjectModelDefaultsRead,
    ProjectModelDefaultsUnchanged,
)
from atelier2.application.publish_agent_configurations import (
    AgentConfigurationRevisionPublished,
    AgentConfigurationRevisionUnchanged,
    AuthProfileRevisionPublished,
    AuthProfileRevisionUnchanged,
)
from atelier2.application.publish_schema_revision import (
    SchemaPublicationCreated,
    SchemaPublicationExisting,
)
from atelier2.application.publish_workflow_revision import (
    PublicationCreated,
    PublicationExisting,
)
from atelier2.application.read_run_events import RunEventsRead
from atelier2.application.read_runs import RunRead
from atelier2.contracts.agents import (
    AgentConfigurationRevision,
    AgentConfigurationRevisionHash,
    AgentExecutionCapability,
    AgentExecutionRequestV2,
    AgentExecutionResult,
    AuthProfileRevision,
    AuthProfileRevisionHash,
)
from atelier2.contracts.catalog_v3 import (
    CatalogActivatedAt,
    CatalogActor,
    CatalogAdmissionExisting,
    CatalogLineageFounded,
)
from atelier2.contracts.effects import (
    AdapterOperationName,
    AdapterRevision,
    EffectAdapterBinding,
    EffectDestination,
    EffectIntent,
    EffectReadback,
    EffectUnknownOutcome,
    PerformedEffect,
    ReadbackPhase,
)
from atelier2.contracts.executions import RunEventKind
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.host_configuration import ProjectId
from atelier2.contracts.provider_probe_receipts import (
    ProviderProbeReceipt,
    ProviderProbeResult,
    ProviderProbeVectorId,
)
from atelier2.contracts.queue_projection import (
    QueueItemTrackerObservation,
    TrackerItemReference,
    WorkItemReference,
)
from atelier2.contracts.revisions_v3 import (
    PublishedRevision,
    PublishedRevisionHash,
    RevisionKind,
)
from atelier2.contracts.runs import (
    RunId,
    RunState,
    WorkflowRevision,
    WorkflowRevisionHash,
)
from atelier2.contracts.when import RecordedAt, recorded_instant
from atelier2.contracts.work_items import (
    ObservedWorkItemRevision,
    WorkItemChangeMarker,
    WorkItemKind,
)
from atelier2.host import serving
from atelier2.host.provider_canary import (
    PROVIDER_CANARY_RECEIPT_VALIDITY,
    provider_layer_digest,
    write_provider_canary_receipt_atomic,
)
from atelier2.host.serving import HostSettings
from atelier2.ports.agent_configurations import (
    AgentConfigurationCatalog,
    AgentConfigurationRevisionCreated,
    AgentConfigurationRevisionExisting,
    ListAgentConfigurationRevisionsResult,
    ListAuthProfileRevisionsResult,
    PublishAgentConfigurationRevisionResult,
    PublishAuthProfileRevisionResult,
)
from atelier2.ports.agent_executions import (
    AgentExecutionFailure,
    AgentExecutorFactoryV2,
    AgentProcessCompletion,
    AgentProcessInvocation,
)
from atelier2.ports.effects import EffectAdapter, EffectAdapterFactory
from atelier2.ports.issue_observation import (
    ObservedOpenTrackerItem,
    OpenTrackerItemsObserved,
    TrackerItemSource,
    TrackerItemUnknown,
    WorkItemRevisionObserved,
)
from atelier2.ports.published_revisions import (
    PublishedRevisionCreated,
    PublishedRevisionExisting,
)
from atelier2.ports.queue_projection import QueueItemsReconciled
from tests.e2e.conductor_seed import (
    CONDUCTOR_MESSAGE_SCHEMA,
    CONDUCTOR_REPORT_SCHEMA,
    conductor_workflow_document,
)
from tests.scenarios.agents import (
    RecordingAgentExecutorFactoryV2,
    RecordingAgentExecutorV2,
)
from tests.scenarios.issue_observation import FakeTrackerItemSource
from tests.scenarios.runs import start_published_v3_run
from tests.scenarios.workflows import ANY_JSON_SCHEMA

WORKFLOW_NAME = "Prove one reconciliation"
OPEN_PR_OPERATION = PublishedRevision(
    RevisionKind.ADAPTER_OPERATION,
    json.dumps({"operation": AdapterOperationName.OPEN_PR.value}).encode("utf-8"),
)
BASELINE_AGENT_OUTPUT = b'"exact-request"'
"""What the seeded agent writes: one JSON value, judged by the any-JSON schema."""


def baseline_agent_executor_factory() -> RecordingAgentExecutorFactoryV2:
    """The executor key the two baseline runs stand bound to.

    Every runtime that opens over the seeded stores must register this key: the
    baseline runs are nonterminal on purpose, and the durable-binding guard
    refuses a registry that could not run what the store still owes.
    """

    return RecordingAgentExecutorFactoryV2(
        "exact", "exact/v1", "exact-operation", BASELINE_AGENT_OUTPUT
    )


WORKFLOW = f"""format_version: 3
name: {WORKFLOW_NAME}
nodes:
  - id: agent
    type: agent
    role: builder
    mode: headless
    instruction: prove-reconciliation
    outputs:
      - name: request
        schema: {{ref: request-schema, revision: {ANY_JSON_SCHEMA.revision_hash.value}}}
  - id: action
    type: action
    operation: {{ref: open-pr, revision: {OPEN_PR_OPERATION.revision_hash.value}}}
    depends_on: [agent]
    inputs:
      - name: body
        from: {{node: agent, output: request}}
  - id: wait
    type: wait
    prompt: Accept the executed effect, or name what is wrong with it.
    depends_on: [action]
    outputs:
      - name: answer
        schema: {{ref: answer-schema, revision: {ANY_JSON_SCHEMA.revision_hash.value}}}
""".encode()
RUN_IDS = ("found-run", "absent-run")
# Deadlock brake for in-process Event and thread waits. Not a latency
# contract: the work is a local thread rendezvous, a fake decode, or a
# short DBOS seed. pytest -n auto still advances the wall clock while
# those threads are unscheduled, so a short bound flakes under CI CPU
# pressure (issue #747) while the same tests pass alone. Generation
# drain of live git capture owns GENERATION_DRAIN_SECONDS separately.
TIMEOUT_SECONDS = 60.0
E2E_OBSERVED_WORK_ITEM_BODY = "e2e observed work item gh:450 — Grüße 東京"
_E2E_TRACKER_ITEM_TITLE = "e2e observed work item gh:450"
_E2E_TRACKER_ITEM = TrackerItemReference("gh:450")
_E2E_WORK_ITEM = WorkItemReference(ProjectId("e2e-workshop"), _E2E_TRACKER_ITEM)
_E2E_QUEUE_SEED_READ = RecordedAt("2026-08-26T09:15:00Z")
_E2E_OBSERVED_REVISION = ObservedWorkItemRevision(
    _E2E_TRACKER_ITEM,
    WorkItemKind.ISSUE,
    E2E_OBSERVED_WORK_ITEM_BODY.encode("utf-8"),
    WorkItemChangeMarker("e2e-etag-gh-450"),
    RecordedAt("2026-08-26T09:15:00Z"),
)


# Deadlock brake for generation drain. In-process Event waits own
# TIMEOUT_SECONDS; git capture of the pinned project under CI CPU
# pressure owns this bound (issue #747 c2).
GENERATION_DRAIN_SECONDS = 60.0
# A restart stands in for a redeploy's process kill, which takes its open
# sockets with it. Uvicorn instead waits out every live connection, and a
# server-sent stream never ends on its own -- so a bare `should_exit` parked
# the whole harness on "Waiting for connections to close" for as long as any
# page held `GET /events` open (issue #1114). This is the grace an ordinary
# in-flight request gets before the restart drops the sockets anyway; uvicorn
# reads it as whole seconds.
RESTART_CONNECTION_GRACE_SECONDS = 1
# The fake conductor's fixed round report: valid against the production
# `CONDUCTOR_REPORT_SCHEMA`, so the browser proof sees exactly the reply a real
# doors-armed conductor would return -- same vector, unbilled.
CONDUCTOR_FAKE_ANSWER = "Nothing started: the workbench probe only asked for an answer."
CONDUCTOR_FAKE_REPORT = json.dumps(
    {
        "answer": CONDUCTOR_FAKE_ANSWER,
        "started_run_ids": [],
        "carried_context": "The workbench probe asked only for an answer.",
        "carried_context_truncated": False,
    }
).encode()
CONDUCTOR_FAKE_PROVIDER = "e2e-conductor"
CONDUCTOR_FAKE_REVISION = "conductor-fake/v1"
# A held V3 attempt parks in `working` this long so the browser has ample margin
# to reach and confirm the cancel by keyboard; the operator's cancel ends it far
# sooner, so this only bounds a run nobody stops.
HELD_ATTEMPT_SECONDS = 30.0
# Long enough for the graph drawing to be photographed live, and interruptible
# by the generation that opened it so a recompose does not wait this bound out.
DELAYED_ATTEMPT_SECONDS = 3.0
MODEL_VALIDATION_RUN_ID = "provider-model-validation"


def wait_for(
    event: threading.Event,
    waiting_for: str,
    *,
    timeout: float = TIMEOUT_SECONDS,
    thread: threading.Thread | None = None,
) -> None:
    """Wait for an Event, or fail naming what was waited for.

    A wall-clock bound is a deadlock brake, not a speed assertion. If a
    worker thread is still alive, the wait keeps going until the bound
    because pytest-xdist can leave a ready thread unscheduled while the
    clock still advances (issue #747). A dead worker fails immediately so
    a crashed decode is not reported as a timeout.
    """
    deadline = time.monotonic() + timeout
    while not event.is_set():
        if thread is not None and not thread.is_alive():
            raise RuntimeError(f"thread died before {waiting_for}")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                f"timed out after {timeout:g}s waiting for {waiting_for}"
            )
        event.wait(timeout=min(0.05, remaining))


def join_thread(
    thread: threading.Thread,
    waiting_for: str,
    *,
    timeout: float = TIMEOUT_SECONDS,
) -> None:
    thread.join(timeout=timeout)
    if thread.is_alive():
        raise TimeoutError(f"timed out after {timeout:g}s waiting for {waiting_for}")


def wait_until(
    ready: Callable[[], bool],
    waiting_for: str,
    *,
    timeout: float = TIMEOUT_SECONDS,
) -> None:
    deadline = time.monotonic() + timeout
    while True:
        if ready():
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                f"timed out after {timeout:g}s waiting for {waiting_for}"
            )
        time.sleep(min(0.025, remaining))


class RuntimeCloser(Protocol):
    def close(self) -> None: ...


@dataclass(frozen=True)
class BrowserScratchRoot:
    path: Path
    created_by_harness: bool

    @classmethod
    def create(cls) -> BrowserScratchRoot:
        return cls(Path(tempfile.mkdtemp(prefix="atelier2-e2e-scratch-")), True)

    @classmethod
    def borrow(cls, path: Path) -> BrowserScratchRoot:
        return cls(path, False)

    def close(self) -> None:
        if self.created_by_harness:
            shutil.rmtree(self.path)


def replace_closed_generation_scratch_root(
    previous: BrowserScratchRoot,
) -> BrowserScratchRoot:
    """Blank root for the next generation; the previous one is removed.

    Call only after that generation's runtime has closed. Reusing the same
    root would hand leftover attempt directories to the next workspace owner,
    which reconciles them against a store that no longer has those attempts.
    """

    next_root = BrowserScratchRoot.create()
    try:
        previous.close()
    except BaseException:
        next_root.close()
        raise
    return next_root


def close_runtime_and_scratch_root(
    runtime: RuntimeCloser | None, scratch_root: BrowserScratchRoot
) -> None:
    if runtime is None:
        scratch_root.close()
        return
    try:
        runtime.close()
    except BaseException:
        # A failed shutdown may still have a live generation writing this
        # workspace; deleting it would drop that work on the floor.
        print(
            f"preserving scratch root {scratch_root.path}: runtime shutdown failed",
            file=sys.stderr,
        )
        raise
    scratch_root.close()


class FakeProviderHolds:
    """Tracks in-flight fake provider work bound to one served generation.

    Delayed, held, and blocking executors wait on a release signal instead of
    sleeping. Drain sets that signal, waits until in-process DBOS workflows
    have finished, seals the live generation under the same lock that tracks
    in-flight count, then blocks until every already-admitted decode or
    tracked attempt has returned. Immediate and conductor executors never
    enter those holds; `track_execute_agent_attempt` counts their whole
    attempt, including candidate capture after decode. Each executor captures
    the live generation token when it is created; admission refuses when that
    token is not the live generation or when that generation is already
    sealed. start_generation mints a new token and does not unseal the
    previous one, so a decode delayed before the admission lock cannot enter
    after the old scratch root is removed.
    DBOS shutdown only waits one second for workflows and then
    ThreadPoolExecutor.shutdown(wait=False), so closing the runtime or
    removing the scratch root before those attempts finish still races a live
    generation.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._idle = threading.Condition(self._lock)
        self._released = threading.Event()
        self._inflight = 0
        self._generation: object = object()
        self._sealed_generation: object | None = None

    def bind_decode(self) -> tuple[object, threading.Event]:
        with self._lock:
            return self._generation, self._released

    @contextmanager
    def in_flight(self, generation: object) -> Iterator[None]:
        with self._lock:
            if generation is not self._generation:
                raise RuntimeError("cannot admit a fake decode from a stale generation")
            if generation is self._sealed_generation:
                raise RuntimeError(
                    "cannot admit a fake decode after this generation was sealed"
                )
            self._inflight += 1
        try:
            yield
        finally:
            with self._lock:
                self._inflight -= 1
                self._idle.notify_all()

    def release_all(self) -> None:
        self._released.set()

    def wait_until_idle(self, timeout: float = TIMEOUT_SECONDS) -> None:
        deadline = time.monotonic() + timeout
        with self._lock:
            self._sealed_generation = self._generation
            while self._inflight > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"timed out after {timeout:g}s waiting for "
                        f"{self._inflight} in-flight fake decode(s) to finish"
                    )
                self._idle.wait(remaining)

    def start_generation(self) -> None:
        with self._lock:
            if self._inflight:
                raise RuntimeError(
                    "cannot start a generation while "
                    f"{self._inflight} fake decode(s) are still in flight"
                )
            self._generation = object()
            self._released = threading.Event()


class UnknownReadbackAdapter:
    def __init__(self, delegate: EffectAdapter) -> None:
        self._delegate = delegate

    def readback(self, intent: EffectIntent, phase: ReadbackPhase) -> EffectReadback:
        return EffectUnknownOutcome(intent.reference)

    def execute(self, intent: EffectIntent) -> PerformedEffect | EffectUnknownOutcome:
        return self._delegate.execute(intent)

    def close(self) -> None:
        self._delegate.close()


class UnknownReadbackFactory:
    def __init__(self, delegate: LoopbackEffectAdapterFactory) -> None:
        self._delegate = delegate

    @property
    def binding(self) -> EffectAdapterBinding:
        return self._delegate.binding

    @property
    def proves_absence(self) -> bool:
        return self._delegate.proves_absence

    def open(self) -> UnknownReadbackAdapter:
        return UnknownReadbackAdapter(self._delegate.open())


class BlockingAgentExecutor(RecordingAgentExecutorV2):
    def __init__(
        self,
        output: bytes,
        requests: list[AgentExecutionRequestV2],
        lifecycle: list[str],
        name: str,
        release: threading.Event,
        owner: BlockingAgentExecutorFactory,
        generation: object | None,
    ) -> None:
        super().__init__(output, requests, lifecycle, name)
        self.observed = threading.Event()
        self.release = release
        self.owner = owner
        self._generation = generation

    def decode_process_completion(
        self, invocation: AgentProcessInvocation, completion: AgentProcessCompletion
    ) -> AgentExecutionResult | AgentExecutionFailure:
        if self.requests[-1].run_id.value == MODEL_VALIDATION_RUN_ID:
            return super().decode_process_completion(invocation, completion)
        self.owner.observed_executor = self
        tracking = (
            self.owner.holds.in_flight(self._generation)
            if self.owner.holds is not None
            else nullcontext()
        )
        with tracking:
            self.observed.set()
            wait_for(self.release, "the browser to observe the working attempt")
            return super().decode_process_completion(invocation, completion)

    def close(self) -> None:
        super().close()
        if (
            self.requests
            and self.requests[-1].run_id.value == MODEL_VALIDATION_RUN_ID
            and self.owner.opened is self
        ):
            self.owner.opened = None


class BlockingAgentExecutorFactory(RecordingAgentExecutorFactoryV2):
    observed_executor: BlockingAgentExecutor | None = None

    def __init__(
        self,
        provider: str,
        revision: str,
        operational_identity_value: str,
        output: bytes,
        holds: FakeProviderHolds | None = None,
    ) -> None:
        super().__init__(provider, revision, operational_identity_value, output)
        self.holds = holds

    def open(self) -> RecordingAgentExecutorV2:
        self.opens += 1
        self.lifecycle.append(f"open:{self.provider}")
        generation: object | None = None
        if self.holds is not None:
            generation, _ = self.holds.bind_decode()
        self.opened = BlockingAgentExecutor(
            self.output,
            [],
            self.lifecycle,
            self.provider,
            threading.Event(),
            self,
            generation,
        )
        return self.opened

    def release_in_flight(self) -> None:
        for executor in (self.observed_executor, self.opened):
            if isinstance(executor, BlockingAgentExecutor):
                executor.release.set()
        self.observed_executor = None
        type(self).observed_executor = None


class _ActiveWorkflows(Protocol):
    def acquire(
        self,
        key: str,
        queue_name: str | None = None,
        queue_partition_key: str | None = None,
    ) -> bool: ...

    def release(self, key: str) -> None: ...

    def activeList(self) -> list[str]: ...

    def count_for_queue(
        self, queue_name: str, queue_partition_key: str | None = None
    ) -> int: ...


class NotifyingActiveWorkflows:
    """Waitable in-process DBOS active-workflow set.

    Wraps the live set or stands alone. The wrapper lock is acquired before
    the inner set lock so wait_until_empty and release share one condition.
    """

    def __init__(self, inner: _ActiveWorkflows | None = None) -> None:
        self._lock = threading.Lock()
        self._idle = threading.Condition(self._lock)
        self._inner = inner
        self._standalone: dict[str, tuple[str, str | None] | None] = {}

    def acquire(
        self,
        key: str,
        queue_name: str | None = None,
        queue_partition_key: str | None = None,
    ) -> bool:
        with self._lock:
            if self._inner is not None:
                return self._inner.acquire(key, queue_name, queue_partition_key)
            if key in self._standalone:
                return False
            self._standalone[key] = (
                (queue_name, queue_partition_key) if queue_name is not None else None
            )
            return True

    def release(self, key: str) -> None:
        with self._lock:
            if self._inner is not None:
                self._inner.release(key)
                empty = not self._inner.activeList()
            else:
                del self._standalone[key]
                empty = not self._standalone
            if empty:
                self._idle.notify_all()

    def activeList(self) -> list[str]:
        with self._lock:
            return list(self._active_ids())

    def count_for_queue(
        self, queue_name: str, queue_partition_key: str | None = None
    ) -> int:
        with self._lock:
            if self._inner is not None:
                return self._inner.count_for_queue(queue_name, queue_partition_key)
            target = (queue_name, queue_partition_key)
            return sum(1 for bucket in self._standalone.values() if bucket == target)

    def wait_until_empty(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        with self._lock:
            while self._active_ids():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    remaining_ids = self._active_ids()
                    raise TimeoutError(
                        f"timed out after {timeout:g}s waiting for "
                        f"{len(remaining_ids)} DBOS workflow(s) to finish: "
                        f"{remaining_ids!r}"
                    )
                self._idle.wait(remaining)

    def _active_ids(self) -> tuple[str, ...]:
        if self._inner is not None:
            return tuple(self._inner.activeList())
        return tuple(self._standalone)


_notifying_active_workflows_lock = threading.Lock()


def notifying_active_workflows() -> NotifyingActiveWorkflows | None:
    """The live in-process DBOS set, wrapped once so drain can wait on empty.

    DBOS looks up `_active_workflows_set` at release time, so in-flight
    workflows notify this wrapper after the first drain wait.
    """

    import dbos._dbos as dbos_runtime

    instance: object | None = dbos_runtime._dbos_global_instance
    if instance is None:
        return None
    active_set_name = "_active_workflows_set"
    with _notifying_active_workflows_lock:
        current = getattr(instance, active_set_name)
        if isinstance(current, NotifyingActiveWorkflows):
            return current
        wrapped = NotifyingActiveWorkflows(current)
        setattr(instance, active_set_name, wrapped)
        return wrapped


def active_dbos_workflow_ids() -> tuple[str, ...]:
    """In-process DBOS workflows still running in this generation.

    `DbosRuntime.close` destroys DBOS after one second, then shuts the worker
    pool without joining. A node workflow still capturing then keeps the old
    lease path, and the next generation recovers it.
    """

    workflows = notifying_active_workflows()
    if workflows is None:
        return ()
    return tuple(workflows.activeList())


def wait_until_dbos_workflows_idle(
    timeout: float = GENERATION_DRAIN_SECONDS,
) -> None:
    workflows = notifying_active_workflows()
    if workflows is None:
        return
    workflows.wait_until_empty(timeout)


def track_execute_agent_attempt(
    holds: FakeProviderHolds,
    execute: Callable[..., object],
) -> Callable[..., object]:
    """Count the whole attempt, including capture after decode, in the drain."""

    def tracked(*args: object, **kwargs: object) -> object:
        generation, _ = holds.bind_decode()
        with holds.in_flight(generation):
            return execute(*args, **kwargs)

    return tracked


def drain_inflight_fake_decodes(
    holds: FakeProviderHolds,
    blocking: BlockingAgentExecutorFactory | None = None,
) -> None:
    holds.release_all()
    if blocking is not None:
        blocking.release_in_flight()
    deadline = time.monotonic() + GENERATION_DRAIN_SECONDS
    wait_until_dbos_workflows_idle(timeout=max(0.0, deadline - time.monotonic()))
    holds.wait_until_idle(timeout=max(0.0, deadline - time.monotonic()))
    wait_until_dbos_workflows_idle(timeout=max(0.0, deadline - time.monotonic()))


class DelayedAgentExecutor(RecordingAgentExecutorV2):
    """Holds a V3 node in `working` long enough for the browser to draw it live."""

    def __init__(
        self,
        output: bytes,
        requests: list[AgentExecutionRequestV2],
        lifecycle: list[str],
        name: str,
        holds: FakeProviderHolds,
    ) -> None:
        super().__init__(output, requests, lifecycle, name)
        self._holds = holds
        self._generation, self._released = holds.bind_decode()
        self.holding = threading.Event()

    def decode_process_completion(
        self, invocation: AgentProcessInvocation, completion: AgentProcessCompletion
    ) -> AgentExecutionResult | AgentExecutionFailure:
        if self.requests[-1].run_id.value == MODEL_VALIDATION_RUN_ID:
            return super().decode_process_completion(invocation, completion)
        with self._holds.in_flight(self._generation):
            self.holding.set()
            self._released.wait(DELAYED_ATTEMPT_SECONDS)
            return super().decode_process_completion(invocation, completion)


class DelayedAgentExecutorFactory(RecordingAgentExecutorFactoryV2):
    def __init__(
        self,
        provider: str,
        revision: str,
        operational_identity_value: str,
        output: bytes,
        holds: FakeProviderHolds,
    ) -> None:
        super().__init__(provider, revision, operational_identity_value, output)
        self._holds = holds

    def open(self) -> RecordingAgentExecutorV2:
        self.opens += 1
        self.lifecycle.append(f"open:{self.provider}")
        self.opened = DelayedAgentExecutor(
            self.output, [], self.lifecycle, self.provider, self._holds
        )
        return self.opened


class HeldAgentExecutor(RecordingAgentExecutorV2):
    """Holds a V3 node in `working` until an operator's cancel stops it.

    The browser needs a genuinely live V3 attempt to reach and confirm the cancel
    decision by keyboard (#439 P6). A short delay races the journey, so this one
    parks the node long enough for a person to open, confirm and watch the run
    stop -- bounded, so a forgotten run never hangs the server.
    """

    def __init__(
        self,
        output: bytes,
        requests: list[AgentExecutionRequestV2],
        lifecycle: list[str],
        name: str,
        holds: FakeProviderHolds,
    ) -> None:
        super().__init__(output, requests, lifecycle, name)
        self._holds = holds
        self._generation, self._released = holds.bind_decode()
        self.holding = threading.Event()
        self.released_before_bound = False

    def decode_process_completion(
        self, invocation: AgentProcessInvocation, completion: AgentProcessCompletion
    ) -> AgentExecutionResult | AgentExecutionFailure:
        if self.requests[-1].run_id.value == MODEL_VALIDATION_RUN_ID:
            return super().decode_process_completion(invocation, completion)
        with self._holds.in_flight(self._generation):
            self.holding.set()
            self.released_before_bound = self._released.wait(HELD_ATTEMPT_SECONDS)
            return super().decode_process_completion(invocation, completion)


class HeldAgentExecutorFactory(RecordingAgentExecutorFactoryV2):
    def __init__(
        self,
        provider: str,
        revision: str,
        operational_identity_value: str,
        output: bytes,
        holds: FakeProviderHolds,
    ) -> None:
        super().__init__(provider, revision, operational_identity_value, output)
        self._holds = holds

    def open(self) -> RecordingAgentExecutorV2:
        self.opens += 1
        self.lifecycle.append(f"open:{self.provider}")
        self.opened = HeldAgentExecutor(
            self.output, [], self.lifecycle, self.provider, self._holds
        )
        return self.opened


def _published_schema_hash(result: object) -> str:
    match result:
        case SchemaPublicationCreated(revision) | SchemaPublicationExisting(revision):
            return revision.revision_hash.value
        case refused:
            raise RuntimeError(f"schema publication failed: {refused!r}")


class BrowserProofHarness:
    def __init__(
        self,
        app: ASGIApp,
        runtime: DbosRuntime,
        factory: BlockingAgentExecutorFactory,
        recompose: Callable[[], tuple[ASGIApp, DbosRuntime]],
        request_restart: Callable[[bool], None],
        reset_state: Callable[[], None],
        drain_inflight: Callable[[], None] | None = None,
    ) -> None:
        self.app, self.runtime, self.factory = app, runtime, factory
        self.recompose = recompose
        self.request_restart = request_restart
        self.reset_state = reset_state
        self.drain_inflight = drain_inflight or (lambda: None)
        self.generation = 1
        self.expected_hash = hashlib.sha256(factory.output).hexdigest().encode("ascii")
        self.stream_counts: dict[str, int] = {}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if (
            scope["type"] == "http"
            and scope.get("method") == "GET"
            and path == "/__e2e/generation"
        ):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send(
                {
                    "type": "http.response.body",
                    "body": str(self.generation).encode("ascii"),
                }
            )
            return
        if (
            scope["type"] == "http"
            and scope.get("method") == "GET"
            and path == "/__e2e/current-wait-execution"
        ):
            references = parse_qs(scope.get("query_string", b"").decode()).get(
                "public_run_reference", []
            )
            body = (
                None
                if len(references) != 1
                else await asyncio.to_thread(self.current_wait_execution, references[0])
            )
            await send(
                {
                    "type": "http.response.start",
                    "status": 200 if body is not None else 409,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": b"{}" if body is None else body,
                }
            )
            return
        if (
            scope["type"] == "http"
            and scope.get("method") == "POST"
            and path == "/__e2e/release-blocking-attempt"
        ):
            released = await asyncio.to_thread(self.release_blocking_attempt)
            await send(
                {
                    "type": "http.response.start",
                    "status": 204 if released else 409,
                    "headers": [],
                }
            )
            await send({"type": "http.response.body", "body": b""})
            return
        if (
            scope["type"] == "http"
            and scope.get("method") == "POST"
            and path == "/__e2e/seed-conductor"
        ):
            body = await asyncio.to_thread(self.seed_conductor)
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        if (
            scope["type"] == "http"
            and scope.get("method") == "POST"
            and path == "/__e2e/recompose"
        ):
            # `?reset=true` additionally wipes and re-seeds durable state back to
            # the exact cold-boot baseline (#742): a spec that needs a guaranteed
            # unseeded server calls this itself instead of depending on file
            # listing order. Bare `/__e2e/recompose` keeps its original meaning --
            # a restart that a real redeploy's data survives -- unchanged, since
            # `cockpit.spec.ts` and `connection-restart.spec.ts` prove exactly
            # that.
            reset = (
                parse_qs(scope.get("query_string", b"").decode()).get(
                    "reset", ["false"]
                )[0]
                == "true"
            )
            self.request_restart(reset)
            await send(
                {
                    "type": "http.response.start",
                    "status": 202,
                    "headers": [(b"cache-control", b"no-store")],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": str(self.generation + 1).encode("ascii"),
                }
            )
            return
        stream_number = 0
        if path.endswith("/events"):
            stream_number = self.stream_counts.get(path, 0) + 1
            self.stream_counts[path] = stream_number

        async def proof_send(message: Message) -> None:
            if message["type"] == "http.response.body" and stream_number >= 2:
                body = message.get("body", b"").replace(self.expected_hash, b"0" * 64)
                message = {**message, "body": body}
            await send(message)

        await self.app(scope, receive, proof_send)

    def current_wait_execution(self, public_run_reference: str) -> bytes | None:
        if not isinstance(self.app, FastAPI):
            raise TypeError("the e2e wait fence requires the composed FastAPI app")
        context: ApiContext = self.app.state.api_context
        try:
            run_id = decode_public_run_reference(public_run_reference)
        except ValueError:
            return None
        result = context.use_cases.get_run(run_id)
        if not isinstance(result, RunRead):
            return None
        run = result.projection.run
        if run.state is not RunState.WAITING_INPUT or run.last_event_sequence < 1:
            return None
        head = context.use_cases.read_run_events(
            run.run_id, run.last_event_sequence - 1, 1
        )
        if not isinstance(head, RunEventsRead) or len(head.events) != 1:
            return None
        event = head.events[0].event
        if (
            event.event_kind is not RunEventKind.WAITING_INPUT
            or event.event_sequence != run.last_event_sequence
            or event.revision_hash != run.revision_hash
            or event.node_id != run.current_node_id
            or event.round_ordinal != run.current_round_ordinal
        ):
            return None
        return json.dumps(
            {"expected_node_execution_id": event.node_execution_id.value}
        ).encode()

    def release_blocking_attempt(self) -> bool:
        deadline = time.monotonic() + TIMEOUT_SECONDS
        executor = self.factory.observed_executor
        while executor is None:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)
            executor = self.factory.observed_executor
        if not executor.observed.wait(max(0, deadline - time.monotonic())):
            return False
        executor.release.set()
        return True

    def close(self) -> None:
        self.runtime.close()

    def recompose_after_server_stop(self, reset: bool) -> None:
        self.drain_inflight()
        self.runtime.close()
        if reset:
            self.reset_state()
        self.app, self.runtime = self.recompose()
        self.generation += 1

    def seed_conductor(self) -> bytes:
        """Publish the whole conductor catalog through the production doors.

        Everything the workbench needs to see a connected conductor: the
        message and report schemas, the production conductor document (built
        by its own owner, `atelier2.host.conductor_workflow`), its catalog lineage, an
        auth profile plus agent configuration bound to the fake conductor
        executor, and the project level-2 model default selecting that exact
        model. On demand rather than at startup, so one served
        instance proves BOTH workbench states: the honest refusal before this
        endpoint is called, the real conversation after.
        """

        context: ApiContext = self.app.state.api_context  # type: ignore[attr-defined]
        use_cases = context.use_cases
        message_hash = _published_schema_hash(
            use_cases.publish_schema_revision(CONDUCTOR_MESSAGE_SCHEMA)
        )
        report_hash = _published_schema_hash(
            use_cases.publish_schema_revision(CONDUCTOR_REPORT_SCHEMA)
        )
        document = conductor_workflow_document(message_hash, report_hash)
        match use_cases.publish_workflow_revision(document):
            case PublicationCreated(read) | PublicationExisting(read):
                revision = read.projection.revision
            case refused_publication:
                raise RuntimeError(
                    f"conductor publication failed: {refused_publication!r}"
                )
        match use_cases.found_catalog_lineage(
            RevisionKind.WORKFLOW,
            PublishedRevisionHash(revision.revision_hash.value),
            None,
            CatalogActor("e2e-harness"),
            CatalogActivatedAt("2026-01-01T00:00:00Z"),
        ):
            case CatalogLineageFounded(lineage) | CatalogAdmissionExisting(lineage):
                lineage_id = lineage.lineage_id.value
            case refused_admission:
                raise RuntimeError(f"conductor admission failed: {refused_admission!r}")

        match use_cases.publish_auth_profile_revision(
            "e2e-conductor-profile", 1, CONDUCTOR_FAKE_PROVIDER, "subscription"
        ):
            case AuthProfileRevisionPublished(profile) | AuthProfileRevisionUnchanged(
                profile
            ):
                auth_profile_hash = profile.revision_hash.value
            case refused_profile:
                raise RuntimeError(
                    f"conductor auth profile failed: {refused_profile!r}"
                )
        match use_cases.publish_agent_configuration_revision(
            "conductor-fake-model",
            auth_profile_hash,
            CONDUCTOR_FAKE_REVISION,
            AgentExecutionCapability.HEADLESS_WITH_TOOLS.value,
        ):
            case AgentConfigurationRevisionPublished(
                bound
            ) | AgentConfigurationRevisionUnchanged(bound):
                configuration_hash = bound.revision_hash.value
            case refused_configuration:
                raise RuntimeError(
                    f"conductor configuration failed: {refused_configuration!r}"
                )

        match use_cases.publish_model_registry(
            CONDUCTOR_FAKE_PROVIDER,
            1,
            (("conductor-fake-model", configuration_hash),),
        ):
            case ModelRegistryPublished(registry) | ModelRegistryUnchanged(registry):
                registry_hash = registry.revision_hash.value
            case refused_registry:
                raise RuntimeError(
                    f"conductor model registry failed: {refused_registry!r}"
                )
        match use_cases.validate_model_registry_entry(
            CONDUCTOR_FAKE_PROVIDER, configuration_hash
        ):
            case ModelRegistryPublished(registry) | ModelRegistryUnchanged(registry):
                registry_hash = registry.revision_hash.value
            case refused_validation:
                raise RuntimeError(
                    f"conductor model validation failed: {refused_validation!r}"
                )
        match use_cases.get_project_model_defaults("e2e-workshop"):
            case ProjectModelDefaultsRead(current_defaults):
                defaults_revision_number = current_defaults.revision_number + 1
                retained_defaults = tuple(
                    (
                        default.difficulty,
                        default.model_registry_revision_hash.value,
                        default.provider_id.value,
                        default.model_id,
                        default.agent_configuration_revision_hash.value,
                    )
                    for default in current_defaults.defaults
                    if default.difficulty != 2
                )
            case ProjectModelDefaultsMissing():
                defaults_revision_number = 1
                retained_defaults = ()
            case refused_defaults_read:
                raise RuntimeError(
                    f"conductor model defaults read failed: {refused_defaults_read!r}"
                )
        match use_cases.publish_project_model_defaults(
            "e2e-workshop",
            defaults_revision_number,
            retained_defaults
            + (
                (
                    2,
                    registry_hash,
                    CONDUCTOR_FAKE_PROVIDER,
                    "conductor-fake-model",
                    configuration_hash,
                ),
            ),
        ):
            case ProjectModelDefaultsPublished() | ProjectModelDefaultsUnchanged():
                pass
            case refused_defaults:
                raise RuntimeError(
                    f"conductor model defaults failed: {refused_defaults!r}"
                )
        return json.dumps(
            {
                "lineage_id": lineage_id,
                "workflow_revision_hash": revision.revision_hash.value,
                "configuration_hash": configuration_hash,
            }
        ).encode()


def seed_boot_baseline(database: Path, effects: Path, application_version: str) -> None:
    """(Re)creates the harness's cold-boot baseline against fresh database and
    effect-store files: the schema, and the two `RUN_IDS` runs already parked
    in `WAITING_RECONCILIATION` that `wait_for_reconciliation` and the Board's
    own "never empty" suite depend on. `main()` calls this once at process
    start; an `/__e2e/recompose?reset=true` (#742) calls it again after wiping
    both files, so a spec that needs a guaranteed-unseeded server reaches the
    exact same baseline a cold boot would give it, not an empty schema neither
    caller actually wants.
    """
    binding = LoopbackEffectAdapterFactory(
        effects,
        AdapterRevision("loopback-v1"),
        EffectDestination("r3-phase5-e2e"),
    )
    # A registered LOCAL_PROCESS executor demands a scratch root even though
    # the recording executor runs in-process. It must lie outside every git
    # worktree, so the seed borrows the same out-of-tree temporary shape the
    # served generations use, and removes it once the baseline stands.
    seed_scratch = BrowserScratchRoot.create()
    prepare = DbosRuntime(
        DbosRuntimeSettings(
            database, application_version, agent_scratch_root=seed_scratch.path
        ),
        UnknownReadbackFactory(binding),
        (baseline_agent_executor_factory(),),
    )
    try:
        prepare.initialize_storage()
        catalog_store = DbosCatalogStore(prepare.engine)
        for published_revision in (ANY_JSON_SCHEMA, OPEN_PR_OPERATION):
            published = catalog_store.publish_revision(published_revision)
            assert isinstance(
                published, (PublishedRevisionCreated, PublishedRevisionExisting)
            ), published
        revision = WorkflowRevision(WORKFLOW)
        for run_id in RUN_IDS:
            start_published_v3_run(
                prepare.engine,
                prepare.settings,
                RunId(run_id),
                revision,
                prepare.agent_executor_registry,
            )
        prepare.launch()
        wait_for_reconciliation(prepare)
    finally:
        prepare.close()
        seed_scratch.close()


def reset_to_boot_baseline(
    database: Path, effects: Path, application_version: str
) -> None:
    """Wipes both durable files (and their WAL/SHM sidecars) and reseeds them
    back to the exact cold-boot baseline (#742). A module-level function, not
    a closure inside `main()`, so it is independently callable and testable --
    `tests/e2e/test_serve_cockpit.py` drives it directly against a live,
    already-mutated harness rather than only through a real process restart.
    """
    for sqlite_path in (database, effects):
        sqlite_path.unlink(missing_ok=True)
        for sidecar_suffix in ("-wal", "-shm"):
            sqlite_path.with_name(sqlite_path.name + sidecar_suffix).unlink(
                missing_ok=True
            )
    seed_boot_baseline(database, effects, application_version)


def e2e_source_commit() -> str:
    """This checkout's own HEAD commit, in the one place the harness can
    truthfully learn it: the repository it is running from. Mirrors
    `scripts/container_snapshot.sh`'s own `git rev-parse --verify
    HEAD^{commit}` -- a real deploy's `--source-commit` flag is required and
    has no default (`host/__init__.py`'s `serve` parser), always filled from
    that same git identity. A provider probe receipt's own `source_commit` is
    validated as 40 lowercase hex, so the harness has to report an identity a
    receipt it mints could ever equal; a made-up label could not.
    """

    repository_root = Path(__file__).resolve().parents[2]
    resolved = subprocess.run(
        ["git", "-C", str(repository_root), "rev-parse", "--verify", "HEAD^{commit}"],
        check=True,
        capture_output=True,
        text=True,
    )
    return resolved.stdout.strip()


def _write_e2e_provider_probe_receipt(
    receipt_directory: Path,
    configuration_hash: AgentConfigurationRevisionHash,
    source_commit: str,
) -> None:
    """Proves a configuration this deployment just accepted, immediately.

    Every executor this harness serves is a fake that always answers the same
    way a live canary would find it: successfully. So the instant this
    deployment accepts a configuration bound to one of them, it already knows
    what a canary run would prove -- and mints the receipt itself, the same
    atomic write `host/provider_canary.py`'s own live canary uses, rather than
    leaving a spec or a fixture to ask for one. A fresh spec publishing a new
    model needs no Python change here: publication itself is what triggers
    proof, for every configuration this harness will ever accept.
    """

    observed = datetime.now(UTC)
    receipt = ProviderProbeReceipt(
        ProviderProbeVectorId(f"e2e-harness-{configuration_hash.value}"),
        configuration_hash,
        WorkflowRevisionHash.of(b"e2e-harness-proof-of-vector"),
        provider_layer_digest(),
        source_commit,
        recorded_instant(observed),
        recorded_instant(observed + PROVIDER_CANARY_RECEIPT_VALIDITY),
        ProviderProbeResult.SUCCEEDED,
        RunId(f"e2e-harness-proof/{configuration_hash.value}"),
        terminal_hash=Sha256Hash.of(b"e2e-harness-proof-of-vector"),
    )
    write_provider_canary_receipt_atomic(
        receipt_directory / f"{configuration_hash.value}.json", receipt
    )


@dataclass(frozen=True)
class ReceiptMintingAgentConfigurationCatalog:
    """The real catalog, plus the one thing this deployment does that a
    production one cannot: it already knows every configuration it accepts
    is provably startable, because it is the deployment that decides what
    "successfully" means for its own fake executors. Every other read and
    write passes straight through.
    """

    inner: AgentConfigurationCatalog
    receipt_directory: Path
    source_commit: str

    def publish_auth_profile_revision(
        self, revision: AuthProfileRevision
    ) -> PublishAuthProfileRevisionResult:
        return self.inner.publish_auth_profile_revision(revision)

    def publish_agent_configuration_revision(
        self, revision: AgentConfigurationRevision
    ) -> PublishAgentConfigurationRevisionResult:
        published = self.inner.publish_agent_configuration_revision(revision)
        if isinstance(
            published,
            AgentConfigurationRevisionCreated | AgentConfigurationRevisionExisting,
        ):
            _write_e2e_provider_probe_receipt(
                self.receipt_directory,
                published.revision.revision_hash,
                self.source_commit,
            )
        return published

    def agent_configuration_revision(
        self, revision_hash: AgentConfigurationRevisionHash
    ) -> tuple[AgentConfigurationRevision, AuthProfileRevision] | None:
        return self.inner.agent_configuration_revision(revision_hash)

    def list_agent_configuration_revisions(
        self, after: AgentConfigurationRevisionHash | None, limit: int
    ) -> ListAgentConfigurationRevisionsResult:
        return self.inner.list_agent_configuration_revisions(after, limit)

    def list_auth_profile_revisions(
        self, after: AuthProfileRevisionHash | None, limit: int
    ) -> ListAuthProfileRevisionsResult:
        return self.inner.list_auth_profile_revisions(after, limit)


def main() -> None:
    root = Path(os.environ["ATELIER2_E2E_ROOT"]).resolve()
    if root.name != ".playwright-runtime":
        raise RuntimeError("refusing to clear an unexpected e2e runtime path")
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    port = int(os.environ["ATELIER2_E2E_PORT"])
    database = root / "atelier.sqlite"
    effects = root / "effects.sqlite"
    # Isolated under this harness's own runtime root, wiped with it above:
    # never the operator's real XDG state directory, and never read by
    # anything but this deployment's own registry.
    receipt_directory = root / "provider-probes"
    harness_source_commit = e2e_source_commit()
    application_version = "r3-phase5-e2e"
    seed_boot_baseline(database, effects, application_version)

    holds = FakeProviderHolds()
    dbos_workflow.execute_agent_attempt = track_execute_agent_attempt(
        holds, dbos_workflow.execute_agent_attempt
    )
    factory = BlockingAgentExecutorFactory(
        "e2e",
        "blocking/v1",
        "e2e-blocking-process",
        (
            "Provider terminal evidence:\n"
            + "Grüße 東京 — durable agent output remains readable after completion.\n"
            * 20
        ).encode(),
        holds,
    )
    # The blocking provider exists so the browser can catch a V2 attempt
    # mid-flight. The immediate one finishes a V3 line without a hold. The
    # delayed one keeps a V3 node in `working` long enough for the graph
    # drawing to be photographed live.
    immediate = RecordingAgentExecutorFactoryV2(
        "e2e-v3", "immediate/v1", "e2e-immediate-process", b'"V3 provider bytes"'
    )
    delayed = DelayedAgentExecutorFactory(
        "e2e-v3-slow",
        "delayed/v1",
        "e2e-delayed-process",
        b"V3 provider bytes",
        holds,
    )
    # Held long enough for the browser to stop it by hand (#439 P6 cancel proof).
    held = HeldAgentExecutorFactory(
        "e2e-v3-held", "held/v1", "e2e-held-process", b'"V3 provider bytes"', holds
    )
    # The workbench chat proof (#7): a doors-shaped executor answering with the
    # production report shape, so a browser can send a message and read the
    # conductor's reply without a billed call.
    conductor = RecordingAgentExecutorFactoryV2(
        CONDUCTOR_FAKE_PROVIDER,
        CONDUCTOR_FAKE_REVISION,
        "e2e-conductor-process",
        CONDUCTOR_FAKE_REPORT,
        capability_set=frozenset({AgentExecutionCapability.HEADLESS_WITH_TOOLS}),
    )

    baseline = baseline_agent_executor_factory()

    # One tracker for the whole harness process. This deployment connects no
    # project source, so production hands the runtime no tracker at all; the
    # import door and the runtime's own queue sweep both read this fixture
    # instead, so they cannot disagree about the item a started run is about.
    fixture_tracker = FakeTrackerItemSource(
        open_items_answer=OpenTrackerItemsObserved(
            (ObservedOpenTrackerItem(_E2E_TRACKER_ITEM, _E2E_TRACKER_ITEM_TITLE, ()),)
        ),
        snapshot_answer=WorkItemRevisionObserved(_E2E_OBSERVED_REVISION),
        expected_snapshot_reference=_E2E_TRACKER_ITEM,
        unexpected_snapshot_answer=TrackerItemUnknown,
    )

    def runtime(
        settings: DbosRuntimeSettings,
        effect_factory: EffectAdapterFactory,
        agent_factories_v2: tuple[AgentExecutorFactoryV2, ...],
        *,
        tracker_item_source: TrackerItemSource | None,
    ) -> DbosRuntime:
        if tracker_item_source is not None:
            raise RuntimeError(
                "the e2e harness connects no project source, so it substitutes "
                "its own fixture tracker; a live one would reach the network"
            )
        factories = (
            *agent_factories_v2,
            baseline,
            factory,
            immediate,
            delayed,
            held,
            conductor,
        )
        # The e2e runtime root lives inside the repository checkout, which no
        # scratch root may, so the leased workspaces stand outside it.
        return DbosRuntime(
            replace(
                settings,
                agent_scratch_root=scratch_root.path,
            ),
            effect_factory,
            factories,
            tracker_item_source=fixture_tracker,
        )

    settings = HostSettings(
        database_path=database,
        effect_store_path=effects,
        effect_adapter_revision="loopback-v1",
        effect_destination="r3-phase5-e2e",
        application_version=application_version,
        source_commit=harness_source_commit,
        source_tree="r3-phase5-e2e",
        frontend_dist=Path(os.environ["ATELIER2_E2E_FRONTEND_DIST"]),
        port=port,
        project_id=ProjectId("e2e-workshop"),
        project_root=Path(__file__).resolve().parents[2],
        provider_probe_receipt_directory=receipt_directory,
    )

    original_create_app = serving.create_app

    def create_app_with_fixture_tracker(
        *,
        source_commit: str,
        source_tree: str,
        ports: ApiPorts,
        limits: ApiLimits,
        event_poll_backoff: EventPollBackoff,
        frontend_dist: Path | None = None,
        served_project_id: ProjectId | None = None,
        lifespan: Lifespan[FastAPI] | None = None,
    ) -> FastAPI:
        seeded = replace(
            ports,
            tracker_item_source=fixture_tracker,
            agent_configuration_catalog=ReceiptMintingAgentConfigurationCatalog(
                ports.agent_configuration_catalog, receipt_directory, source_commit
            ),
        )
        app = original_create_app(
            source_commit=source_commit,
            source_tree=source_tree,
            ports=seeded,
            limits=limits,
            event_poll_backoff=event_poll_backoff,
            frontend_dist=frontend_dist,
            served_project_id=served_project_id,
            lifespan=lifespan,
        )
        observed = seeded.queue_projection.reconcile_open_items(
            _E2E_WORK_ITEM.project,
            (
                (
                    _E2E_WORK_ITEM,
                    QueueItemTrackerObservation(
                        _E2E_TRACKER_ITEM_TITLE, _E2E_QUEUE_SEED_READ
                    ),
                ),
            ),
            _E2E_QUEUE_SEED_READ,
        )
        if not isinstance(observed, QueueItemsReconciled):
            raise TypeError(
                f"e2e work-item fixture did not land on the queue: {observed!r}"
            )
        return app

    def compose() -> tuple[ASGIApp, DbosRuntime]:
        with (
            patch.object(serving, "DbosRuntime", side_effect=runtime),
            patch.object(
                serving, "create_app", side_effect=create_app_with_fixture_tracker
            ),
        ):
            return serving.compose_application(settings)

    restart_requested = threading.Event()
    reset_requested = threading.Event()
    server: uvicorn.Server | None = None

    def request_restart(reset: bool) -> None:
        if reset:
            reset_requested.set()
        restart_requested.set()
        if server is None:
            raise RuntimeError("the e2e server is not running")
        server.should_exit = True

    def drain_inflight() -> None:
        drain_inflight_fake_decodes(holds, factory)

    scratch_root = BrowserScratchRoot.create()

    def compose_next_generation() -> tuple[ASGIApp, DbosRuntime]:
        nonlocal scratch_root
        holds.start_generation()
        scratch_root = replace_closed_generation_scratch_root(scratch_root)
        return compose()

    runtime_to_close: RuntimeCloser | None = None
    try:
        app, live_runtime = compose()
        runtime_to_close = live_runtime

        def reset_state() -> None:
            # The drain that precedes a reset seals the served generation, and
            # the reseed's own baseline attempts run through the tracked
            # execute path -- they belong to the incoming generation, so it is
            # minted before the seed writes. `compose_next_generation` minting
            # again afterwards is harmless: the seed's decodes have returned.
            holds.start_generation()
            reset_to_boot_baseline(database, effects, application_version)

        harness = BrowserProofHarness(
            app,
            live_runtime,
            factory,
            compose_next_generation,
            request_restart,
            reset_state,
            drain_inflight,
        )
        runtime_to_close = harness
        while True:
            server = uvicorn.Server(
                uvicorn.Config(
                    harness,
                    host=settings.host,
                    port=settings.port,
                    timeout_graceful_shutdown=RESTART_CONNECTION_GRACE_SECONDS,
                )
            )
            server.run()
            if not restart_requested.is_set():
                break
            restart_requested.clear()
            harness.recompose_after_server_stop(reset_requested.is_set())
            reset_requested.clear()
    finally:
        drain_inflight()
        close_runtime_and_scratch_root(runtime_to_close, scratch_root)


def wait_for_reconciliation(
    runtime: DbosRuntime, run_ids: tuple[str, ...] = RUN_IDS
) -> None:
    observed: dict[str, str] = {}

    def reached() -> bool:
        nonlocal observed
        with runtime.engine.connect() as connection:
            observed = {
                str(row.run_id): str(row.state)
                for row in connection.execute(sa.select(runs.c.run_id, runs.c.state))
            }
        return all(
            observed.get(run_id) == RunState.WAITING_RECONCILIATION.value
            for run_id in run_ids
        )

    try:
        wait_until(reached, "e2e runs to reach reconciliation")
    except TimeoutError as error:
        raise RuntimeError(
            f"e2e runs did not reach reconciliation: {observed!r}"
        ) from error


if __name__ == "__main__":
    main()
