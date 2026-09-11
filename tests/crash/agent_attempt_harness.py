from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import sqlalchemy as sa
from dbos import SQLAlchemyDatasource

import atelier2.adapters.dbos.workflow as dbos_workflow
from atelier2.adapters.candidate_store import GitCandidateTreeStore
from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.run_transitions import load_run
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import runs
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.adapters.dbos.workflow import AgentExecutorMap, ReconstructedAgentAttempt
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.adapters.project_verification import declared_project
from atelier2.application.cancel_agent_attempt import (
    continue_agent_attempt_cancellation,
)
from atelier2.application.execute_agent_attempt import execute_agent_attempt
from atelier2.contracts.agent_attempts import (
    AgentAttempt,
    AgentAttemptId,
    AgentAttemptProcessPhase,
    AgentAttemptReplacement,
    AgentAttemptState,
    CancelAgentAttemptRequest,
)
from atelier2.contracts.agent_permissions import GRANTS_NOTHING
from atelier2.contracts.agents import (
    AgentBinding,
    AgentBindingSet,
    AgentConfigurationRevision,
    AgentConfigurationRevisionFormatVersion,
    AgentExecutionCapability,
    AgentExecutionRequestV2,
    AgentExecutorOperationalIdentity,
    AgentExecutorRevision,
    AgentRole,
    AuthMode,
    AuthProfileRevision,
    ProviderId,
    ResolvedAgentBinding,
)
from atelier2.contracts.candidate_reports import ReadPatch
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.executions import AgentAttemptExecution, NodeExecutionId
from atelier2.contracts.project_sources import CandidateTree, ProjectSourcePin
from atelier2.contracts.run_cancellations import CancelRunRequest
from atelier2.contracts.runs import RunId, RunState, WorkflowRevision
from atelier2.ports.agent_attempts import (
    AgentAttemptCancellationAccepted,
    RunCancellationAccepted,
    RunCancellationTerminalRetry,
)
from atelier2.ports.agent_executions import (
    AgentAttemptWorkspaceLease,
    AgentExecutionResult,
)
from atelier2.ports.candidate_store import CandidateTreeStore, LeasedWorkingTree
from atelier2.ports.durable_runs import StartPublishedRunRequestV2
from atelier2.ports.project_verification import DeclaredProject, PinnedProjectSource
from atelier2.ports.run_queries import (
    RunFound,
)
from tests.scenarios.agents import (
    NOTHING_IS_PERMITTED,
    RecordingAgentExecutorFactoryV2,
    RecordingAgentExecutorV2,
    agent_attempt_execution,
    agent_scratch_root,
    decoding_to,
    launching,
    process_invocation,
    publish_checked_model_registry,
    refusing,
    runtime_workspace_owner,
    workspace_files_nobody_opens,
)
from tests.scenarios.api import durable_queries
from tests.scenarios.projects import git_project
from tests.scenarios.workflows import ANY_JSON_SCHEMA

CRASHED = 86
DOCUMENT = f"""format_version: 3
name: Agent attempt crash harness document
nodes:
  - id: build
    type: agent
    role: builder
    mode: headless
    instruction: build
    outputs:
      - name: result
        schema: {{ref: result-schema, revision: {ANY_JSON_SCHEMA.revision_hash.value}}}
""".encode()


_APPENDS_TO_COUNTER = (
    "from pathlib import Path; Path(__import__('sys').argv[1]).open('ab').write(b'x')"
)


def inert_factory() -> RecordingAgentExecutorFactoryV2:
    """The registered executor, which this harness must never be served by."""

    refuse = refusing("runtime-owned executor is not the controlled test process")
    return RecordingAgentExecutorFactoryV2(
        "anthropic",
        "claude-cli/v1",
        "controlled-process",
        b"unused",
        command=refuse,
        decoder=refuse,
    )


def controlled_process_executor(counter: Path) -> RecordingAgentExecutorV2:
    """The executor this harness drives itself, counting every launch on disk."""

    return RecordingAgentExecutorV2(
        command=launching(sys.executable, "-c", _APPENDS_TO_COUNTER, str(counter)),
        # A JSON string, not bare text: the declared output schema is
        # schema-validated JSON now (#901 slice 5), so an answer this harness
        # never contends over still has to parse.
        decoder=decoding_to(b'"done"'),
    )


class DiesOnceTheWorkIsKept:
    """A store that really keeps the work, then loses the process that kept it.

    The crash has to land between two writes that no transaction can join: the
    candidate is a git ref and the attempt a durable row. Wrapping the real
    store rather than imitating one is what makes the proof worth anything --
    what stands afterwards was written by the same code a live run uses.
    """

    def __init__(self, kept: CandidateTreeStore) -> None:
        self._kept = kept

    def capture(
        self, pin: ProjectSourcePin, lease: AgentAttemptWorkspaceLease
    ) -> CandidateTree:
        self._kept.capture(pin, lease)
        os._exit(CRASHED)

    def read(self, attempt_id: AgentAttemptId) -> CandidateTree | None:
        return self._kept.read(attempt_id)

    def attest(self, candidate: CandidateTree) -> None:
        self._kept.attest(candidate)

    def materialize(
        self, candidate: CandidateTree, lease: AgentAttemptWorkspaceLease
    ) -> None:
        self._kept.materialize(candidate, lease)

    def written(
        self, pin: ProjectSourcePin, lease: AgentAttemptWorkspaceLease
    ) -> LeasedWorkingTree:
        return self._kept.written(pin, lease)

    def changes(self, written: LeasedWorkingTree) -> ReadPatch:
        return self._kept.changes(written)


def pinned_project_of(root: Path) -> PinnedProjectSource:
    """One real repository beside the store, pinned as an attempt would get it."""

    checkout = root / "project"
    pin = git_project(checkout, {"pyproject.toml": "", "src/tool.py": "print('x')\n"})
    return declared_project(checkout, root / "atelier.sqlite").pinned(pin, None, None)


def candidate_store_of(root: Path) -> GitCandidateTreeStore:
    return GitCandidateTreeStore(root / "project", root / "atelier.sqlite")


def run_controlled_process(counter: Path) -> None:
    subprocess.run(
        [sys.executable, "-c", _APPENDS_TO_COUNTER, str(counter)],
        check=True,
        timeout=10,
    )


def runtime(root: Path) -> DbosRuntime:
    return DbosRuntime(
        DbosRuntimeSettings(
            root / "atelier.sqlite",
            "agent-attempt-crash",
            agent_scratch_root=agent_scratch_root(root),
        ),
        LoopbackEffectAdapterFactory(
            root / "effects.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("agent-attempt-crash"),
        ),
        (inert_factory(),),
    )


def output_schema_runtime(
    root: Path, executor_factory: RecordingAgentExecutorFactoryV2
) -> DbosRuntime:
    started = DbosRuntime(
        DbosRuntimeSettings(
            root / "atelier.sqlite",
            "v3-output-contract-test",
            agent_scratch_root=agent_scratch_root(root),
        ),
        LoopbackEffectAdapterFactory(
            root / "external.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        ),
        (executor_factory,),
    )
    started.initialize_storage()
    return started


def repair_evidence(execution: AgentAttemptExecution) -> dict[str, object]:
    request = execution.request
    return {
        "attempt_id": execution.attempt_id.value,
        "attempt_ordinal": execution.ordinal,
        "round_ordinal": request.round_ordinal,
        "job_bytes_base64": base64.b64encode(request.job_bytes).decode("ascii"),
        "request_hash": request.request_hash.value,
    }


def output_schema_repair_process(root: Path, mode: str) -> None:
    from tests.integration.test_v3_output_enforcement import (
        THE_ANSWER_THE_SCHEMA_ADMITS,
        THE_ANSWER_THE_SCHEMA_REFUSES,
        armed_attempt,
    )

    executor_factory = RecordingAgentExecutorFactoryV2(
        "exact",
        "exact/v1",
        "exact-operation",
        THE_ANSWER_THE_SCHEMA_ADMITS,
    )
    lease = output_schema_runtime(root, executor_factory)
    try:
        if mode == "crash-output-schema-repair-before-adapter":
            first = armed_attempt(lease)
            DbosAgentAttemptStore(
                lease.engine, lease.settings.application_version
            ).complete_success(
                first, AgentExecutionResult(THE_ANSWER_THE_SCHEMA_REFUSES)
            )

            reconstruct = dbos_workflow.reconstruct_agent_attempt

            def reconstruct_then_crash(
                datasource: SQLAlchemyDatasource,
                agent_executors_v2: AgentExecutorMap,
                project: DeclaredProject | None,
                attempt: AgentAttempt,
            ) -> ReconstructedAgentAttempt:
                reconstructed = reconstruct(
                    datasource, agent_executors_v2, project, attempt
                )
                execution = reconstructed.execution
                if execution.ordinal != 2:
                    raise AssertionError("the controlled crash did not reach repair")
                evidence = repair_evidence(execution)
                opened = executor_factory.opened
                evidence["adapter_requests"] = (
                    0 if opened is None else len(opened.requests)
                )
                (root / "precrash-repair.json").write_text(
                    json.dumps(evidence), encoding="utf-8"
                )
                os._exit(CRASHED)

            dbos_workflow.reconstruct_agent_attempt = reconstruct_then_crash
            lease.launch()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                time.sleep(0.01)
            raise AssertionError("the replacement workflow never reached its crash")
        if mode == "recover-output-schema-repair":
            lease.launch()
            deadline = time.monotonic() + 10
            state: object = None
            while time.monotonic() < deadline:
                with lease.engine.connect() as connection:
                    state = connection.scalar(sa.select(runs.c.state))
                if state == RunState.COMPLETED.value:
                    opened = executor_factory.opened
                    if opened is None or len(opened.requests) != 1:
                        raise AssertionError(
                            "repair did not invoke exactly one adapter"
                        )
                    evidence = repair_evidence(
                        AgentAttemptExecution(
                            opened.requests[0],
                            AgentAttemptId.for_execution(
                                opened.requests[0].node_execution_id,
                                opened.requests[0].request_hash,
                                2,
                            ),
                            2,
                        )
                    )
                    evidence["adapter_requests"] = len(opened.requests)
                    (root / "recovered-repair.json").write_text(
                        json.dumps(evidence), encoding="utf-8"
                    )
                    return
                time.sleep(0.01)
            raise AssertionError(f"repair recovery left run in {state!r}")
        raise ValueError(f"unknown output-schema mode {mode!r}")
    finally:
        lease.close()


def request(lease: DbosRuntime) -> AgentExecutionRequestV2:
    lease.initialize_storage()
    auth = AuthProfileRevision("max", 1, ProviderId("anthropic"), AuthMode.SUBSCRIPTION)
    configuration = AgentConfigurationRevision(
        "opus",
        auth.revision_hash,
        AgentExecutorRevision("claude-cli/v1"),
        AgentExecutionCapability.HEADLESS,
        AgentConfigurationRevisionFormatVersion.V2,
    )
    catalog = DbosAgentConfigurationCatalog(lease.engine, lease.agent_executor_registry)
    catalog.publish_auth_profile_revision(auth)
    catalog.publish_agent_configuration_revision(configuration)
    publish_checked_model_registry(
        lease.engine, ProviderId("anthropic"), (configuration,)
    )
    DbosCatalogStore(lease.engine).publish_revision(ANY_JSON_SCHEMA)
    workflow = WorkflowRevision(DOCUMENT)
    DbosWorkflowRevisionPublisher(lease.engine).publish(workflow)
    binding_set = AgentBindingSet(
        (AgentBinding(AgentRole("builder"), configuration.revision_hash),)
    )
    run_id = RunId("agent-attempt/crash")
    DbosDurableRunStarter(
        lease.engine,
        lease.settings,
        lease.agent_executor_registry,
    ).start_published(
        StartPublishedRunRequestV2(run_id, workflow.revision_hash, binding_set)
    )
    resolved = ResolvedAgentBinding(AgentRole("builder"), configuration, auth)
    return AgentExecutionRequestV2(
        NodeExecutionId.for_node(run_id, workflow.revision_hash, "build"),
        run_id,
        workflow.revision_hash,
        "build",
        resolved,
        AgentExecutorOperationalIdentity("controlled-process"),
        b"build",
    )


def main(root: Path, mode: str) -> None:
    if mode in {
        "crash-output-schema-repair-before-adapter",
        "recover-output-schema-repair",
    }:
        output_schema_repair_process(root, mode)
        return
    lease = runtime(root)
    try:
        exact_request = request(lease)
        store = DbosAgentAttemptStore(lease.engine, lease.settings.application_version)
        if mode == "crash-prepared":
            store.prepare(agent_attempt_execution(exact_request))
            os._exit(CRASHED)
        if mode == "crash-armed":
            store.prepare(agent_attempt_execution(exact_request))
            store.claim(agent_attempt_execution(exact_request))
            run_controlled_process(root / "counter")
            os._exit(CRASHED)
        if mode == "crash-running":
            ready = root / "running-pid"
            launch_attempt(lease, store, exact_request, ready)
            os._exit(CRASHED)
        if mode == "close-running":
            ready = root / "closed-running-pid"
            execution = launch_attempt(lease, store, exact_request, ready)
            attempt = store.load(execution.attempt_id)
            (root / "closed-cgroup").write_text(
                str(attempt_cgroup(lease, attempt)), encoding="utf-8"
            )
            (root / "closed-endpoint").write_text(
                str(attempt_endpoint(lease, attempt)), encoding="utf-8"
            )
            lease.close()
            return
        if mode == "crash-watchdog-and-running-descendant":
            descendant_pid = root / "descendant-pid"
            provider = (
                "from pathlib import Path; import subprocess,sys,time; "
                "subprocess.Popen((sys.executable,'-c',"
                "'from pathlib import Path; import os,signal,sys,time; '"
                "+'signal.signal(signal.SIGTERM,signal.SIG_IGN); '"
                "+'Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)',"
                "sys.argv[1]), start_new_session=True, stdout=subprocess.DEVNULL, "
                "stderr=subprocess.DEVNULL); time.sleep(60)"
            )
            launch_attempt(
                lease,
                store,
                exact_request,
                descendant_pid,
                (sys.executable, "-c", provider, str(descendant_pid)),
            )
            watchdog_pid = only_watchdog_child_pid()
            os.kill(watchdog_pid, 9)
            os._exit(CRASHED)
        if mode == "crash-after-cleanup-before-attestation":
            ready = root / "cleanup-before-attestation-pid"
            execution = launch_attempt(lease, store, exact_request, ready)
            attempt = store.load(execution.attempt_id)
            command = CancelAgentAttemptRequest(
                attempt.run_id,
                attempt.attempt_id,
                "cleanup-before-attestation",
                attempt.state_version,
                AgentAttemptReplacement.NONE,
            )
            cancellation = DbosAgentAttemptStore(
                lease.engine, lease.settings.application_version
            ).request_cancellation(command)
            if not isinstance(cancellation, AgentAttemptCancellationAccepted):
                raise AssertionError("controlled cancellation was not accepted")
            disposition, _owner, _generation = lease.agent_process_supervisor.cancel(
                store.load(attempt.attempt_id)
            )
            cgroup = attempt_cgroup(lease, store.load(attempt.attempt_id))
            endpoint = attempt_endpoint(lease, store.load(attempt.attempt_id))
            (root / "cleanup-disposition").write_text(
                disposition.value, encoding="ascii"
            )
            (root / "cleanup-cgroup").write_text(str(cgroup), encoding="utf-8")
            (root / "cleanup-endpoint").write_text(str(endpoint), encoding="utf-8")
            os._exit(CRASHED)
        if mode == "crash-after-attestation-before-release":
            ready = root / "attestation-before-release-pid"
            execution = launch_attempt(lease, store, exact_request, ready)
            attempt = store.load(execution.attempt_id)
            command = CancelAgentAttemptRequest(
                attempt.run_id,
                attempt.attempt_id,
                "attestation-before-release",
                attempt.state_version,
                AgentAttemptReplacement.NONE,
            )
            submitting_store = DbosAgentAttemptStore(
                lease.engine, lease.settings.application_version
            )
            cancellation = submitting_store.request_cancellation(command)
            if not isinstance(cancellation, AgentAttemptCancellationAccepted):
                raise AssertionError("controlled cancellation was not accepted")
            attempt = store.load(attempt.attempt_id)
            disposition, owner, generation = lease.agent_process_supervisor.cancel(
                attempt
            )
            terminal = store.attest_cancellation_cleanup(
                command, disposition, owner, generation
            )
            cgroup = attempt_cgroup(lease, terminal.attempt)
            endpoint = attempt_endpoint(lease, terminal.attempt)
            (root / "attested-cgroup").write_text(str(cgroup), encoding="utf-8")
            (root / "attested-endpoint").write_text(str(endpoint), encoding="utf-8")
            (root / "attested-state-version").write_text(
                str(terminal.attempt.state_version), encoding="ascii"
            )
            os._exit(CRASHED)
        if mode == "recover-without-witness":
            execution = agent_attempt_execution(exact_request)
            attempt = store.load(execution.attempt_id)
            cgroup = attempt_cgroup(lease, attempt)
            wait_for_empty_cgroup(cgroup)
            cgroup.rmdir()
            command = exact_cancellation_command(attempt)
            DbosAgentAttemptStore(
                lease.engine, lease.settings.application_version
            ).request_cancellation(command)
            result = continue_agent_attempt_cancellation(
                command,
                store,
                lease.agent_process_supervisor,
                runtime_workspace_owner(lease),
            )
            if result is None:
                raise AssertionError("an absent witness left the attempt unattested")
            (root / "witnessless-recovery-state").write_text(
                result.attempt.state.value, encoding="ascii"
            )
            return
        if mode.startswith("converge-driverless"):
            execution = agent_attempt_execution(exact_request)
            attempt = store.load(execution.attempt_id)
            cgroup = attempt_cgroup(lease, attempt)
            endpoint = attempt_endpoint(lease, attempt)
            (root / "driverless-cgroup").write_text(str(cgroup), encoding="utf-8")
            (root / "driverless-endpoint").write_text(str(endpoint), encoding="utf-8")
            if mode == "converge-driverless-without-witness":
                # What a restarting service unit does to its own subtree.
                wait_for_empty_cgroup(cgroup)
                cgroup.rmdir()
            lease.launch()
            (root / "converged-state").write_text(
                store.load(execution.attempt_id).state.value, encoding="ascii"
            )
            return
        if mode == "recover-cancellation":
            execution = agent_attempt_execution(exact_request)
            attempt = store.load(execution.attempt_id)
            witness = (
                None
                if attempt.watchdog_generation_id is None
                else attempt_cgroup(lease, attempt)
            )
            command = exact_cancellation_command(attempt)
            store_with_submission = DbosAgentAttemptStore(
                lease.engine, lease.settings.application_version
            )
            store_with_submission.request_cancellation(command)
            lease.launch()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                terminal = store.load(execution.attempt_id)
                if terminal.state in {
                    AgentAttemptState.CANCELLED,
                    AgentAttemptState.INTERRUPTED,
                } and (witness is None or not witness.exists()):
                    (root / "recovered-cancellation-state").write_text(
                        terminal.state.value, encoding="ascii"
                    )
                    return
                time.sleep(0.01)
            raise AssertionError("parent-death cancellation was not recovered")
        if mode == "recover-operator-cancellation":
            # #439 P3 Fenster (i): the operator's own run-cancel command is
            # durably accepted (CAS + enqueue committed) while nothing owns
            # the process any more, then this restart is the first runtime
            # that ever drives the enqueued cleanup workflow -- the same
            # crash-before-attestation shape `recover-cancellation` proves
            # above, but under a command `is_operator_run_cancel` recognizes,
            # so the run itself must lift to CANCELLED, not stay STARTED.
            execution = agent_attempt_execution(exact_request)
            attempt = store.load(execution.attempt_id)
            witness = (
                None
                if attempt.watchdog_generation_id is None
                else attempt_cgroup(lease, attempt)
            )
            submitting_store = DbosAgentAttemptStore(
                lease.engine, lease.settings.application_version
            )
            accepted = submitting_store.request_run_cancellation(
                CancelRunRequest(
                    attempt.run_id,
                    "operator-crash-restart-1",
                    exact_request.node_execution_id,
                )
            )
            if not isinstance(
                accepted, RunCancellationAccepted | RunCancellationTerminalRetry
            ):
                raise AssertionError("controlled operator run-cancel was not accepted")
            lease.launch()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                terminal = store.load(execution.attempt_id)
                with lease.engine.connect() as connection:
                    run = load_run(connection, attempt.run_id)
                if (
                    terminal.state
                    in {AgentAttemptState.CANCELLED, AgentAttemptState.INTERRUPTED}
                    and run.state is RunState.CANCELLED
                    and (witness is None or not witness.exists())
                ):
                    return
                time.sleep(0.01)
            raise AssertionError(
                "operator run-cancel during parent death was not recovered"
            )
        if mode == "crash-once-the-candidate-is-kept":
            project = pinned_project_of(root)
            (root / "pinned-tree").write_text(project.pin.tree, encoding="utf-8")
            candidates: CandidateTreeStore = DiesOnceTheWorkIsKept(project.candidates)
            execute_agent_attempt(
                agent_attempt_execution(exact_request),
                controlled_process_executor(root / "counter"),
                store,
                lease.agent_process_supervisor,
                runtime_workspace_owner(lease),
                replace(project, candidates=candidates),
                permissions=GRANTS_NOTHING,
                workspace_files=workspace_files_nobody_opens,
            )
            raise AssertionError("the kept candidate was supposed to end this process")
        if mode == "read-candidate":
            kept = candidate_store_of(root).read(
                agent_attempt_execution(exact_request).attempt_id
            )
            (root / "kept-tree").write_text(
                "" if kept is None else kept.tree, encoding="utf-8"
            )
            return
        if mode == "recover":
            execute_agent_attempt(
                agent_attempt_execution(exact_request),
                controlled_process_executor(root / "counter"),
                store,
                lease.agent_process_supervisor,
                runtime_workspace_owner(lease),
                permissions=GRANTS_NOTHING,
                workspace_files=workspace_files_nobody_opens,
            )
            found = durable_queries(lease.engine).get_run(exact_request.run_id)
            if isinstance(found, RunFound):
                attempt = found.projection.current_agent_attempt
                if attempt is not None:
                    (root / "projected-attempt-state").write_text(
                        attempt.state, encoding="utf-8"
                    )
            return
        raise ValueError(f"unknown mode {mode!r}")
    finally:
        lease.close()


def launch_attempt(
    lease: DbosRuntime,
    store: DbosAgentAttemptStore,
    exact_request: AgentExecutionRequestV2,
    ready: Path,
    arguments: tuple[str, ...] | None = None,
) -> AgentAttemptExecution:
    execution = agent_attempt_execution(exact_request)
    store.prepare(execution)
    lease.agent_process_supervisor.prepare(execution)
    store.claim(execution)
    invocation = process_invocation(
        execution.attempt_id,
        arguments
        or (
            sys.executable,
            "-c",
            "from pathlib import Path; import os,sys,time; Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)",
            str(ready),
        ),
        Path.cwd(),
    )
    threading.Thread(
        target=lease.agent_process_supervisor.launch_and_wait,
        args=(execution, invocation, NOTHING_IS_PERMITTED),
        daemon=True,
    ).start()
    wait_for_file(ready)
    wait_for_process_observation(store, execution)
    return execution


def wait_for_file(path: Path) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.01)
    raise AssertionError("controlled process did not become ready")


def wait_for_process_observation(
    store: DbosAgentAttemptStore, execution: AgentAttemptExecution
) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        attempt = store.load(execution.attempt_id)
        if attempt.process_phase is AgentAttemptProcessPhase.PROCESS_OBSERVED:
            return
        time.sleep(0.01)
    raise AssertionError("controlled process was not durably observed")


def only_watchdog_child_pid() -> int:
    children = (
        Path(f"/proc/{os.getpid()}/task/{os.getpid()}/children")
        .read_text(encoding="ascii")
        .split()
    )
    matching = []
    for value in children:
        command = Path(f"/proc/{value}/cmdline").read_bytes()
        if b"atelier2.adapters.agent_process_watchdog" in command:
            matching.append(int(value))
    if len(matching) != 1:
        raise AssertionError("expected exactly one live watchdog child")
    return matching[0]


def exact_cancellation_command(attempt: AgentAttempt) -> CancelAgentAttemptRequest:
    cancellation = attempt.cancellation
    return CancelAgentAttemptRequest(
        attempt.run_id,
        attempt.attempt_id,
        (
            "cancel-after-parent-death"
            if cancellation is None
            else cancellation.command_id
        ),
        (
            attempt.state_version
            if cancellation is None
            else cancellation.expected_attempt_state_version
        ),
        (
            AgentAttemptReplacement.NONE
            if cancellation is None
            else cancellation.replacement
        ),
    )


def attempt_cgroup(lease: DbosRuntime, attempt: AgentAttempt) -> Path:
    generation = attempt.watchdog_generation_id
    if generation is None:
        raise AssertionError("attempt has no watchdog generation")
    return lease.settings.process_cgroup_root() / (
        "atelier2-" + attempt.attempt_id.value[:16] + "-" + generation.value[:8]
    )


def attempt_endpoint(lease: DbosRuntime, attempt: AgentAttempt) -> Path:
    return lease.settings.process_control_root() / f"{attempt.attempt_id.value}.sock"


def wait_for_empty_cgroup(cgroup: Path) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if (
            cgroup.is_dir()
            and "populated 0"
            in (cgroup / "cgroup.events").read_text(encoding="ascii").splitlines()
        ):
            return
        time.sleep(0.01)
    raise AssertionError("attempt cgroup did not become empty")


if __name__ == "__main__":
    main(Path(sys.argv[1]), sys.argv[2])
