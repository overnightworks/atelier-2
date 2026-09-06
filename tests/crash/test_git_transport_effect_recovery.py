"""Recovery after the remote accepted a push but the sender did not return."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path

import pytest
import sqlalchemy as sa

from atelier2.adapters.candidate_store import CANDIDATE_STORE_DIRECTORY_NAME
from atelier2.adapters.dbos.effect_store import intent_snapshot_from_record
from atelier2.adapters.dbos.names import RESOLVE_STEP_NAME
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import (
    effect_intents,
)
from atelier2.adapters.dbos.workflow_ids import reconcile_workflow_id_for
from atelier2.adapters.git_transport.effects import (
    GitCommandResult,
    GitRemote,
    GitTransportEffectAdapterFactory,
    SubprocessGitCommandRunner,
)
from atelier2.adapters.github.effects import GitHubEffectAdapterFactory
from atelier2.api.openapi import API_PREFIX
from atelier2.contracts.adapter_operations_v3 import AdapterOperationName
from atelier2.contracts.effects import (
    AdapterRevision,
    EffectDestination,
    EffectIntentStateVersion,
    EffectReceipt,
    EffectUnknownOutcome,
    OperatorAuthoritativeAbsence,
    PerformedEffect,
    ReadbackPhase,
    ReconcileActor,
    ReconcileCommand,
    ReconcileCommandId,
)
from atelier2.contracts.host_configuration import ProjectId
from atelier2.contracts.queue_projection import TrackerItemReference
from atelier2.contracts.runs import RunId, RunState
from atelier2.contracts.when import RecordedAt
from atelier2.contracts.work_items import (
    ObservedWorkItemRevision,
    WorkItemChangeMarker,
    WorkItemKind,
)
from atelier2.ports.effects import EffectAdapterRegistration, EffectAdapterRegistry
from atelier2.ports.issue_observation import WorkItemRevisionObserved
from tests.acceptance.test_v3_push_before_open_pr import (
    _WRITE_CANDIDATE,
    AGENT_OUTPUT,
    ORDER_NAME,
    _publish,
)
from tests.acceptance.test_v3_push_before_open_pr import (
    _repositories as _public_repositories,
)
from tests.integration.test_git_transport_push import _intent, _repositories
from tests.scenarios.agents import (
    RecordingAgentExecutorFactoryV2,
    agent_scratch_root,
    launching,
)
from tests.scenarios.api import durable_api_client
from tests.scenarios.head_branch_pull_requests import FakeHeadBranchPullRequests
from tests.scenarios.issue_observation import FakeTrackerItemSource
from tests.scenarios.run_waiting import wait_for_run_state
from tests.scenarios.runs import submit_reconcile_command
from tests.scenarios.work_item_claims import fake_agent_claim_executable

CRASHED = 86
APPLICATION_VERSION = "git-transport-crash-test"
PROJECT = ProjectId("git-transport-crash")
ITEM = TrackerItemReference("gh:642")
RUN = RunId("v3/git-transport-crash")
HARNESS = Path(__file__)


class CrashAfterAcceptedPush(SubprocessGitCommandRunner):
    def run(
        self,
        arguments: tuple[str, ...],
        *,
        working_directory: Path,
        environment: Mapping[str, str],
        standard_input: bytes | None = None,
    ) -> GitCommandResult:
        result = super().run(
            arguments,
            working_directory=working_directory,
            environment=environment,
            standard_input=standard_input,
        )
        if "push" in arguments and result.returncode == 0:
            raise RuntimeError("injected crash after accepted push")
        return result


class InconclusiveAfterAcceptedPush(SubprocessGitCommandRunner):
    def __init__(self, accepted_pushes: Path) -> None:
        self._accepted_pushes = accepted_pushes

    def run(
        self,
        arguments: tuple[str, ...],
        *,
        working_directory: Path,
        environment: Mapping[str, str],
        standard_input: bytes | None = None,
    ) -> GitCommandResult:
        if "ls-remote" in arguments and self._accepted_pushes.exists():
            return GitCommandResult(1, b"", b"remote read is inconclusive")
        result = super().run(
            arguments,
            working_directory=working_directory,
            environment=environment,
            standard_input=standard_input,
        )
        if "push" in arguments and result.returncode == 0:
            with self._accepted_pushes.open("a", encoding="utf-8") as pushes:
                pushes.write("accepted\n")
        return result


def _append_line(path: Path, line: str) -> None:
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, f"{line}\n".encode())
    finally:
        os.close(descriptor)


class CrashProcessAfterAcceptedPush(SubprocessGitCommandRunner):
    def __init__(self, push_attempts: Path, accepted_marker: Path) -> None:
        self._push_attempts = push_attempts
        self._accepted_marker = accepted_marker

    def run(
        self,
        arguments: tuple[str, ...],
        *,
        working_directory: Path,
        environment: Mapping[str, str],
        standard_input: bytes | None = None,
    ) -> GitCommandResult:
        if "push" in arguments:
            _append_line(self._push_attempts, "push")
        result = super().run(
            arguments,
            working_directory=working_directory,
            environment=environment,
            standard_input=standard_input,
        )
        if "push" in arguments and result.returncode == 0:
            self._accepted_marker.write_text("accepted\n", encoding="utf-8")
            os._exit(CRASHED)
        return result


class UnknownRemoteReads(SubprocessGitCommandRunner):
    def __init__(self, push_attempts: Path) -> None:
        self._push_attempts = push_attempts

    def run(
        self,
        arguments: tuple[str, ...],
        *,
        working_directory: Path,
        environment: Mapping[str, str],
        standard_input: bytes | None = None,
    ) -> GitCommandResult:
        if "ls-remote" in arguments:
            return GitCommandResult(1, b"", b"remote read is inconclusive")
        if "push" in arguments:
            _append_line(self._push_attempts, "push")
        return super().run(
            arguments,
            working_directory=working_directory,
            environment=environment,
            standard_input=standard_input,
        )


class PushAttemptRecordingRunner(SubprocessGitCommandRunner):
    def __init__(self, push_attempts: Path) -> None:
        self._push_attempts = push_attempts

    def run(
        self,
        arguments: tuple[str, ...],
        *,
        working_directory: Path,
        environment: Mapping[str, str],
        standard_input: bytes | None = None,
    ) -> GitCommandResult:
        if "push" in arguments:
            _append_line(self._push_attempts, "push")
        return super().run(
            arguments,
            working_directory=working_directory,
            environment=environment,
            standard_input=standard_input,
        )


def _runtime(root: Path, runner: SubprocessGitCommandRunner) -> DbosRuntime:
    github = GitHubEffectAdapterFactory(
        root / "github.sqlite",
        AdapterRevision("github-open-pr-v1"),
        EffectDestination("platform"),
    )
    push = GitTransportEffectAdapterFactory(
        root / CANDIDATE_STORE_DIRECTORY_NAME,
        GitRemote("local-crash-test", str(root / "remote.git")),
        AdapterRevision("git-push-v1"),
        EffectDestination("git"),
        FakeHeadBranchPullRequests(),
        runner,
    )
    registry = EffectAdapterRegistry(
        (
            EffectAdapterRegistration(AdapterOperationName.OPEN_PR, github),
            EffectAdapterRegistration(AdapterOperationName.PUSH_ATELIER_COMMIT, push),
        )
    )
    executor = RecordingAgentExecutorFactoryV2(
        "exact",
        "exact/v1",
        "exact-operation",
        AGENT_OUTPUT,
        command=launching(
            sys.executable,
            "-c",
            _WRITE_CANDIDATE,
            b"candidate exact bytes\n".hex(),
            AGENT_OUTPUT.hex(),
        ),
    )
    return DbosRuntime(
        DbosRuntimeSettings(
            root / "atelier.sqlite",
            APPLICATION_VERSION,
            agent_scratch_root=agent_scratch_root(root),
            project_id=PROJECT,
            bootstrap_project_root=root / "project",
            agent_claim_executable=fake_agent_claim_executable(root),
        ),
        registry,
        (executor,),
    )


def _seed_public_run(root: Path) -> None:
    runtime = _runtime(root, SubprocessGitCommandRunner())
    runtime.initialize_storage()
    try:
        workflow, bindings = _publish(runtime)
        item = ObservedWorkItemRevision(
            ITEM,
            WorkItemKind.ISSUE,
            b"Implement P3.\n\n## Dateien\n`one.txt`\n",
            WorkItemChangeMarker("issue-642-crash-v1"),
            RecordedAt("2026-08-27T12:00:00Z"),
        )
        binding = bindings.bindings[0]
        response = durable_api_client(
            runtime,
            served_project_id=PROJECT,
            tracker_item_source=FakeTrackerItemSource(
                snapshot_answer=WorkItemRevisionObserved(item),
                expected_snapshot_reference=item.item,
            ),
        ).post(
            API_PREFIX + "/runs",
            json={
                "workflow_format_version": 3,
                "run_id": RUN.value,
                "workflow_revision_hash": workflow.revision_hash.value,
                "agent_bindings": [
                    {
                        "role": binding.role.value,
                        "agent_configuration_revision_hash": (
                            binding.agent_configuration_revision_hash.value
                        ),
                    }
                ],
                "orders": [{"name": ORDER_NAME, "work_item": ITEM.value}],
            },
        )
        assert response.status_code == 201, response.text
    finally:
        runtime.close()


def _submit_absence(root: Path) -> None:
    runtime = _runtime(root, SubprocessGitCommandRunner())
    try:
        with runtime.engine.connect() as connection:
            push_record = (
                connection.execute(
                    sa.select(effect_intents).where(
                        effect_intents.c.operation_name
                        == AdapterOperationName.PUSH_ATELIER_COMMIT.value
                    )
                )
                .mappings()
                .one()
            )
        intent = intent_snapshot_from_record(push_record).intent
        submit_reconcile_command(
            runtime.engine,
            runtime.settings,
            ReconcileCommand(
                ReconcileCommandId("recover-accepted-push"),
                intent.reference,
                EffectIntentStateVersion(1),
                ReconcileActor("operator"),
                "authorized retry after the remote observation was inconclusive",
                OperatorAuthoritativeAbsence(),
            ),
        )
    finally:
        runtime.close()


def _child(root: Path, command: str, *, expected: int = 0) -> None:
    result = subprocess.run(
        (sys.executable, str(HARNESS), command, str(root)),
        capture_output=True,
        check=False,
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                (str(Path(__file__).parents[2]), str(Path(__file__).parents[2] / "src"))
            ),
        },
        text=True,
        timeout=30,
    )
    assert result.returncode == expected, result.stderr


def _launch_child(command: str, root: Path) -> None:
    push_attempts = root / "push-attempts"
    if command == "crash":
        runner: SubprocessGitCommandRunner = CrashProcessAfterAcceptedPush(
            push_attempts, root / "accepted"
        )
        expected = None
    elif command == "wait":
        runner = UnknownRemoteReads(push_attempts)
        expected = RunState.WAITING_RECONCILIATION
    elif command == "resolve":
        runner = PushAttemptRecordingRunner(push_attempts)
        expected = RunState.COMPLETED
    else:
        raise ValueError(f"unknown command: {command}")
    runtime = _runtime(root, runner)
    try:
        runtime.launch()
        if expected is None:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                time.sleep(0.025)
            raise AssertionError("runtime did not crash after the accepted push")
        wait_for_run_state(runtime.engine, RUN, expected)
    finally:
        runtime.close()


def test_runtime_resolve_retries_an_accepted_push_without_sending_again(
    tmp_path: Path,
) -> None:
    _project, remote, base = _public_repositories(tmp_path)
    _seed_public_run(tmp_path)

    _child(tmp_path, "wait")
    _submit_absence(tmp_path)
    _child(tmp_path, "crash", expected=CRASHED)

    assert (tmp_path / "accepted").read_text(encoding="utf-8") == "accepted\n"
    assert (tmp_path / "push-attempts").read_text(encoding="utf-8").splitlines() == [
        "push"
    ]
    with sqlite3.connect(tmp_path / "atelier.sqlite") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM effect_receipts WHERE operation_name=?",
            (AdapterOperationName.PUSH_ATELIER_COMMIT.value,),
        ).fetchone() == (0,)

    _child(tmp_path, "resolve")

    with sqlite3.connect(tmp_path / "atelier.sqlite") as connection:
        receipts = connection.execute(
            "SELECT logical_key,effect_id,result,confirmation_source,"
            "reconcile_command_id FROM effect_receipts WHERE operation_name=?",
            (AdapterOperationName.PUSH_ATELIER_COMMIT.value,),
        ).fetchall()
        assert len(receipts) == 1
        receipt = receipts[0]
        receipt_logical_key, effect_id, raw_result, confirmation_source, command_id = (
            receipt
        )
        linked_events = tuple(
            connection.execute(
                "SELECT event_kind FROM run_events WHERE receipt_logical_key=? "
                "ORDER BY event_sequence",
                (receipt_logical_key,),
            )
        )
        resolve_outputs = connection.execute(
            "SELECT COUNT(*) FROM operation_outputs "
            "WHERE workflow_uuid=? AND function_name=?",
            (
                reconcile_workflow_id_for(ReconcileCommandId(str(command_id))),
                RESOLVE_STEP_NAME,
            ),
        ).fetchone()
        run_state = connection.execute(
            "SELECT state FROM runs WHERE run_id=?", (RUN.value,)
        ).fetchone()

    result = json.loads(bytes(raw_result).decode("utf-8"))
    branch = str(result["branch"])
    commit = str(result["commit_oid"])
    assert effect_id == commit
    assert confirmation_source == "OPERATOR_AUTHORIZED_EXECUTION"
    assert command_id == "recover-accepted-push"
    assert linked_events == (
        ("ACTION_RECONCILIATION_RESOLVED",),
        ("ACTION_COMPLETED",),
    )
    assert resolve_outputs == (1,)
    assert run_state == (RunState.COMPLETED.value,)
    assert (
        subprocess.run(
            ("git", "-C", str(remote), "rev-list", "--count", f"{base}..{branch}"),
            capture_output=True,
            check=True,
            text=True,
        ).stdout.strip()
        == "1"
    )
    assert (
        subprocess.run(
            ("git", "-C", str(remote), "rev-parse", branch),
            capture_output=True,
            check=True,
            text=True,
        ).stdout.strip()
        == commit
    )
    assert (tmp_path / "push-attempts").read_text(encoding="utf-8").splitlines() == [
        "push"
    ]
    github = GitHubEffectAdapterFactory(
        tmp_path / "github.sqlite",
        AdapterRevision("github-open-pr-v1"),
        EffectDestination("platform"),
    )
    assert len(github.recorded_pull_requests()) == 1


def test_a_replayed_resolve_after_ref_removal_pushes_the_same_commit_once_more(
    tmp_path: Path,
) -> None:
    """The named residual gap, pinned at the workflow's own crash-recovery seam.

    `OBSERVE` finds the ref absent and its output is memoized durably before
    `RESOLVE` starts; the crash happens inside `RESOLVE`, after the push is
    accepted but before the step's own output is recorded, so DBOS replays
    `RESOLVE` on restart using that memoized absence rather than a fresh one.
    Removing the ref between the crash and the replay reproduces the residual
    gap named in ADR 0010 decision 5's 2026-09-05 amendment: the replay's own
    fresh `execute()` check finds the ref absent again and pushes once more.
    The zero-OID lease only ever admits the same deterministic commit, so the
    replay converges on the first push's exact object rather than a twin, and
    the run still ends `CONFIRMED`.
    """

    _project, remote, _base = _public_repositories(tmp_path)
    _seed_public_run(tmp_path)

    _child(tmp_path, "crash", expected=CRASHED)

    assert (tmp_path / "accepted").read_text(encoding="utf-8") == "accepted\n"
    assert (tmp_path / "push-attempts").read_text(encoding="utf-8").splitlines() == [
        "push"
    ]
    with sqlite3.connect(tmp_path / "atelier.sqlite") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM effect_receipts WHERE operation_name=?",
            (AdapterOperationName.PUSH_ATELIER_COMMIT.value,),
        ).fetchone() == (0,)

    pushed_branches = [
        name
        for name in subprocess.run(
            (
                "git",
                "-C",
                str(remote),
                "for-each-ref",
                "--format=%(refname:short)",
                "refs/heads/",
            ),
            capture_output=True,
            check=True,
            text=True,
        ).stdout.split()
        if name != "main"
    ]
    assert len(pushed_branches) == 1
    branch = pushed_branches[0]
    commit_before_replay = subprocess.run(
        ("git", "-C", str(remote), "rev-parse", branch),
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        ("git", "-C", str(remote), "update-ref", "-d", f"refs/heads/{branch}"),
        capture_output=True,
        check=True,
    )

    _child(tmp_path, "resolve")

    with sqlite3.connect(tmp_path / "atelier.sqlite") as connection:
        receipts = connection.execute(
            "SELECT effect_id,result FROM effect_receipts WHERE operation_name=?",
            (AdapterOperationName.PUSH_ATELIER_COMMIT.value,),
        ).fetchall()
        run_state = connection.execute(
            "SELECT state FROM runs WHERE run_id=?", (RUN.value,)
        ).fetchone()
    assert len(receipts) == 1
    effect_id, raw_result = receipts[0]
    result = json.loads(bytes(raw_result).decode("utf-8"))
    assert effect_id == commit_before_replay
    assert result["commit_oid"] == commit_before_replay
    assert result["branch"] == branch
    assert run_state == (RunState.COMPLETED.value,)
    assert (tmp_path / "push-attempts").read_text(encoding="utf-8").splitlines() == [
        "push",
        "push",
    ]
    assert (
        subprocess.run(
            ("git", "-C", str(remote), "rev-parse", branch),
            capture_output=True,
            check=True,
            text=True,
        ).stdout.strip()
        == commit_before_replay
    )


def test_restart_reads_the_exact_commit_without_pushing_a_twin(tmp_path: Path) -> None:
    store, remote, base, tree = _repositories(tmp_path)
    from tests.integration.test_git_transport_push import _factory

    factory = _factory(store, remote, CrashAfterAcceptedPush())
    intent, request = _intent(factory, base, tree)
    adapter = factory.open()
    try:
        with pytest.raises(RuntimeError, match="injected crash"):
            adapter.execute(intent)
    finally:
        adapter.close()

    recovered = _factory(store, remote).open()
    try:
        receipt = recovered.readback(intent, ReadbackPhase.AFTER_SEND)
    finally:
        recovered.close()
    assert isinstance(receipt, EffectReceipt)
    assert receipt.effect_id.value == request.expected_commit_oid(
        intent.request.request_hash.value
    )


def test_a_replay_after_a_lease_push_reads_the_same_commit_and_sends_nothing(
    tmp_path: Path,
) -> None:
    """The replaced branch was accepted, the answer was lost, the retry replays.

    A push that replaced an earlier attempt's commit under a lease leaves the
    branch naming this request's own deterministic commit, so the read after
    the crash resolves to that commit's receipt rather than to a second send --
    the same convergence a create-only push already has, now for the branch
    this operation replaced.
    """

    store, remote, base, tree = _repositories(tmp_path)
    from tests.integration.test_git_transport_push import (
        HEAD_BRANCH,
        _factory,
        _foreign_commit,
    )

    _foreign_commit(tmp_path, base, tree, remote, b"an earlier attempt\n")
    factory = _factory(store, remote, CrashAfterAcceptedPush())
    intent, request = _intent(factory, base, tree)
    adapter = factory.open()
    try:
        with pytest.raises(RuntimeError, match="injected crash"):
            adapter.execute(intent)
    finally:
        adapter.close()

    push_attempts = tmp_path / "push-attempts"
    recovered = _factory(
        store, remote, PushAttemptRecordingRunner(push_attempts)
    ).open()
    try:
        receipt = recovered.readback(intent, ReadbackPhase.AFTER_SEND)
    finally:
        recovered.close()

    expected = request.expected_commit_oid(intent.request.request_hash.value)
    assert isinstance(receipt, EffectReceipt)
    assert receipt.effect_id.value == expected
    assert not push_attempts.exists()
    assert (
        subprocess.run(
            ("git", "-C", str(remote), "rev-parse", HEAD_BRANCH.full_ref),
            capture_output=True,
            check=True,
            text=True,
        ).stdout.strip()
        == expected
    )


def test_a_lease_push_receipt_is_the_same_bytes_read_directly_or_after_a_crash(
    tmp_path: Path,
) -> None:
    """Which oid a lease replaced is evidence for an operator, not the effect's identity.

    A push that replaces a foreign commit under a lease and one that a crash
    interrupts right after the remote accepted it must agree on what the
    accepted send produced: the same request against the same store and
    remote is read back as the identical receipt bytes whether that receipt
    comes straight from the accepting `execute` or is reconstructed later by
    a readback that never itself observed the oid the branch stood at before.
    """

    store, remote, base, tree = _repositories(tmp_path)
    from tests.integration.test_git_transport_push import (
        HEAD_BRANCH,
        _factory,
        _foreign_commit,
    )

    foreign = _foreign_commit(tmp_path, base, tree, remote, b"an earlier attempt\n")
    factory = _factory(store, remote)
    intent, _request = _intent(factory, base, tree)
    adapter = factory.open()
    try:
        performed = adapter.execute(intent)
    finally:
        adapter.close()
    assert isinstance(performed, PerformedEffect)

    # Put the branch back where this attempt observed it, so a second send
    # against the same store and remote replays the identical lease push --
    # this time interrupted by a crash right after the remote accepts it.
    subprocess.run(
        ("git", "-C", str(remote), "update-ref", HEAD_BRANCH.full_ref, foreign),
        check=True,
    )
    crashing = _factory(store, remote, CrashAfterAcceptedPush()).open()
    try:
        with pytest.raises(RuntimeError, match="injected crash"):
            crashing.execute(intent)
    finally:
        crashing.close()

    recovered = _factory(store, remote).open()
    try:
        receipt = recovered.readback(intent, ReadbackPhase.AFTER_SEND)
    finally:
        recovered.close()

    assert isinstance(receipt, EffectReceipt)
    assert receipt.result.payload == performed.result.payload


def test_a_ref_removed_after_an_accepted_push_stays_unknown_and_pushes_nothing(
    tmp_path: Path,
) -> None:
    """The push landed, the answer did not, and then the ref was gone.

    A read taken after a send cannot tell a remote that never received the push
    from one that no longer holds it, so it reports the unknown outcome it has
    instead of the absence that would license a second push (ADR 0010 decision
    5, as amended 2026-09-05).
    """

    store, remote, base, tree = _repositories(tmp_path)
    from tests.integration.test_git_transport_push import HEAD_BRANCH, _factory

    factory = _factory(store, remote, CrashAfterAcceptedPush())
    intent, _request = _intent(factory, base, tree)
    adapter = factory.open()
    try:
        with pytest.raises(RuntimeError, match="injected crash"):
            adapter.execute(intent)
    finally:
        adapter.close()
    subprocess.run(
        ("git", "-C", str(remote), "update-ref", "-d", HEAD_BRANCH.full_ref),
        capture_output=True,
        check=True,
    )

    push_attempts = tmp_path / "push-attempts"
    recovered = _factory(
        store, remote, PushAttemptRecordingRunner(push_attempts)
    ).open()
    try:
        outcome = recovered.readback(intent, ReadbackPhase.AFTER_SEND)
    finally:
        recovered.close()

    assert isinstance(outcome, EffectUnknownOutcome)
    assert outcome.reason is not None
    assert "a send was already attempted" in outcome.reason.detail
    assert not push_attempts.exists()
    assert (
        subprocess.run(
            ("git", "-C", str(remote), "rev-parse", "--verify", HEAD_BRANCH.full_ref),
            capture_output=True,
            check=False,
        ).returncode
        != 0
    )


def test_restart_after_inconclusive_post_send_read_reconciles_without_second_push(
    tmp_path: Path,
) -> None:
    store, remote, base, tree = _repositories(tmp_path)
    from tests.integration.test_git_transport_push import _factory

    accepted_pushes = tmp_path / "accepted-pushes"
    factory = _factory(store, remote, InconclusiveAfterAcceptedPush(accepted_pushes))
    intent, _request = _intent(factory, base, tree)
    first = factory.open()
    try:
        outcome = first.execute(intent)
    finally:
        first.close()

    restarted = _factory(
        store, remote, InconclusiveAfterAcceptedPush(accepted_pushes)
    ).open()
    try:
        retried = restarted.execute(intent)
    finally:
        restarted.close()

    assert isinstance(outcome, EffectUnknownOutcome)
    assert isinstance(retried, EffectUnknownOutcome)
    assert accepted_pushes.read_text(encoding="utf-8").splitlines() == ["accepted"]


if __name__ == "__main__":
    raw_command, raw_root = sys.argv[1:]
    _launch_child(raw_command, Path(raw_root))
