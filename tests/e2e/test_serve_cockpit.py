from __future__ import annotations

import importlib.util
import sqlite3
import sys
import threading
import time
from contextlib import closing
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Protocol
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from atelier2.adapters.dbos.run_transitions import RunTransitionConflict

SCRIPT_PATH = Path(__file__).with_name("serve_cockpit.py")


def load_harness() -> ModuleType:
    specification = importlib.util.spec_from_file_location("serve_cockpit", SCRIPT_PATH)
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


harness = load_harness()


class ClosingRuntime:
    def __init__(self, scratch_root: Path) -> None:
        self.scratch_root = scratch_root
        self.closed = False

    def close(self) -> None:
        assert self.scratch_root.is_dir()
        self.closed = True


class FailingRuntime:
    def __init__(self, scratch_root: Path) -> None:
        self.scratch_root = scratch_root

    def close(self) -> None:
        assert self.scratch_root.is_dir()
        raise RuntimeError("runtime close failed")


def test_a_harness_created_scratch_root_is_removed_after_runtime_close(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    created_root = tmp_path / "created-root"
    monkeypatch.setattr(
        harness.tempfile,
        "mkdtemp",
        lambda prefix: str(created_root.mkdir() or created_root),
    )
    scratch_root = harness.BrowserScratchRoot.create()
    runtime = ClosingRuntime(scratch_root.path)

    harness.close_runtime_and_scratch_root(runtime, scratch_root)

    assert runtime.closed
    assert not created_root.exists()


def test_a_caller_supplied_scratch_root_is_never_removed(tmp_path: Path) -> None:
    caller_root = tmp_path / "caller-root"
    caller_root.mkdir()
    scratch_root = harness.BrowserScratchRoot.borrow(caller_root)
    runtime = ClosingRuntime(scratch_root.path)

    harness.close_runtime_and_scratch_root(runtime, scratch_root)

    assert runtime.closed
    assert caller_root.is_dir()


def test_a_harness_created_scratch_root_is_removed_when_the_runtime_never_started(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    created_root = tmp_path / "start-error-root"
    monkeypatch.setattr(
        harness.tempfile,
        "mkdtemp",
        lambda prefix: str(created_root.mkdir() or created_root),
    )
    scratch_root = harness.BrowserScratchRoot.create()

    harness.close_runtime_and_scratch_root(None, scratch_root)

    assert not created_root.exists()


def test_a_failed_runtime_close_preserves_the_scratch_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    failing_root = tmp_path / "close-error-root"
    monkeypatch.setattr(
        harness.tempfile,
        "mkdtemp",
        lambda prefix: str(failing_root.mkdir() or failing_root),
    )
    scratch_root = harness.BrowserScratchRoot.create()
    with pytest.raises(RuntimeError, match="runtime close failed"):
        harness.close_runtime_and_scratch_root(
            FailingRuntime(scratch_root.path), scratch_root
        )

    assert failing_root.is_dir()
    assert (
        f"preserving scratch root {failing_root}: runtime shutdown failed"
        in capsys.readouterr().err
    )


def test_a_harness_created_scratch_root_is_removed_after_test_interruption(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    interrupted_root = tmp_path / "interrupted-root"
    monkeypatch.setattr(
        harness.tempfile,
        "mkdtemp",
        lambda prefix: str(interrupted_root.mkdir() or interrupted_root),
    )
    scratch_root = harness.BrowserScratchRoot.create()
    runtime = ClosingRuntime(scratch_root.path)

    with pytest.raises(KeyboardInterrupt):
        try:
            raise KeyboardInterrupt
        finally:
            harness.close_runtime_and_scratch_root(runtime, scratch_root)

    assert runtime.closed
    assert not interrupted_root.exists()


def _run_states(database: Path) -> dict[str, str]:
    engine = harness.sa.create_engine(f"sqlite:///{database}")
    try:
        with engine.connect() as connection:
            return {
                str(row.run_id): str(row.state)
                for row in connection.execute(
                    harness.sa.select(harness.runs.c.run_id, harness.runs.c.state)
                )
            }
    finally:
        engine.dispose()


def _effect_call_count(effects: Path) -> int:
    with closing(sqlite3.connect(effects)) as connection:
        return connection.execute(
            "SELECT COUNT(*) FROM loopback_effect_calls"
        ).fetchone()[0]


def _served_settings(
    tmp_path: Path, database: Path, effects: Path, application_version: str
) -> object:
    frontend_dist = tmp_path / "frontend"
    (frontend_dist / "assets").mkdir(parents=True)
    (frontend_dist / "index.html").write_text("index")
    return harness.HostSettings(
        database_path=database,
        effect_store_path=effects,
        effect_adapter_revision="loopback-v1",
        effect_destination="r3-phase5-e2e",
        application_version=application_version,
        source_commit="reset-test",
        source_tree="reset-test",
        frontend_dist=frontend_dist,
        project_id=harness.ProjectId("e2e-workshop"),
        project_root=SCRIPT_PATH.resolve().parents[2],
        # Isolated per test, never the operator's real XDG state directory.
        # No test in this file starts a fresh run (only reconciles an
        # already-seeded baseline one, which never reaches the receipt gate),
        # so no matching receipt is minted here -- only the isolation.
        provider_probe_receipt_directory=tmp_path / "provider-probes",
    )


def test_a_reset_recompose_restores_the_exact_cold_boot_baseline(
    tmp_path: Path,
) -> None:
    """#742: `/__e2e/recompose?reset=true` must not merely survive a restart --
    it must wipe every durable trace beyond the cold-boot baseline in BOTH
    physical stores (the DBOS database and the loopback effect store) and
    reseed exactly what a fresh boot gives, proven rather than merely claimed.

    Drives the harness's own `/__e2e/recompose`/`/__e2e/generation` doors
    through a `TestClient`, the same observable surface a spec's browser
    uses, but calls `recompose_after_server_stop` directly instead of
    stopping a real uvicorn process -- that real process-restart shape is
    already proven end-to-end by `connection-restart.spec.ts`. What this
    test owns is that the state left behind is the exact cold-boot baseline,
    not merely "smaller".
    """
    database = tmp_path / "atelier.sqlite"
    effects = tmp_path / "effects.sqlite"
    application_version = "e2e-reset-test"
    harness.seed_boot_baseline(database, effects, application_version)
    baseline_runs = _run_states(database)
    baseline_effect_calls = _effect_call_count(effects)

    settings = _served_settings(tmp_path, database, effects, application_version)
    scratch_root = harness.BrowserScratchRoot.create()

    def compose() -> tuple:
        return _compose_with_scratch(settings, scratch_root)

    app, runtime = compose()
    # `harness` is loaded dynamically (`load_harness`), so pyright cannot know
    # `BrowserProofHarness`'s real type here -- `Any` names that honestly.
    proof: Any = None
    try:
        # Mutates BOTH stores through the one door the browser's own Absent
        # flow drives: resolving a baseline reconciliation executes the exact
        # request once (the `loopback_effect_calls` table, the effect store)
        # and advances the run off WAITING_RECONCILIATION (the `runs` table,
        # the DBOS database) -- the same two files `seed_boot_baseline` owns.
        with TestClient(app) as mutate_client:
            resolved = mutate_client.post(
                "/atelier/api/v1/runs/run1.Zm91bmQtcnVu/reconciliations",
                json={
                    "command_id": "reset-test-mutation",
                    "expected_intent_state_version": 1,
                    "actor": "reset-test",
                    "evidence": "mutating past the cold-boot baseline",
                    "determination": {"type": "operator_authoritative_absence"},
                },
            )
            assert resolved.status_code in (200, 202), resolved.text
        harness.wait_until(
            lambda: _effect_call_count(effects) == baseline_effect_calls + 1,
            "the absent resolution to write one loopback effect",
        )
        harness.wait_until(
            lambda: _run_states(database) != baseline_runs,
            "the resolved run to advance off the baseline state",
        )
        assert _effect_call_count(effects) == baseline_effect_calls + 1

        factory = harness.BlockingAgentExecutorFactory(
            "e2e", "blocking/v1", "e2e-blocking-process", b"reset-test-fixture"
        )
        proof_holder: list[Any] = []
        # The real process restarts on its own plain thread, outside uvicorn's
        # event loop, by the time `recompose_after_server_stop` runs (`main()`'s
        # own `while True` loop, after `server.run()` returns). A `TestClient`
        # request keeps an event loop live for the whole call, and DBOS's own
        # synchronous launch path refuses to run inside one -- so this restart
        # happens on its own thread too, joined before the next request reads
        # what it left behind, the same ordering the real restart guarantees.
        restart_threads: list[threading.Thread] = []

        def request_restart(reset: bool) -> None:
            thread = threading.Thread(
                target=proof_holder[0].recompose_after_server_stop, args=(reset,)
            )
            thread.start()
            restart_threads.append(thread)

        proof = harness.BrowserProofHarness(
            app,
            runtime,
            factory,
            compose,
            request_restart,
            lambda: harness.reset_to_boot_baseline(
                database, effects, application_version
            ),
        )
        proof_holder.append(proof)

        with TestClient(proof) as client:
            restarted = client.post("/__e2e/recompose?reset=true")
            assert restarted.status_code == 202
            expected_generation = restarted.text

            harness.join_thread(restart_threads[0], "the reset recompose to finish")

            observed_generation = client.get("/__e2e/generation")
            assert observed_generation.text == expected_generation

        assert _run_states(database) == baseline_runs
        assert _effect_call_count(effects) == baseline_effect_calls
    finally:
        if proof is not None:
            proof.runtime.close()
        else:
            runtime.close()
        scratch_root.close()


def _leftover_attempt_directory(scratch_root: Path) -> Path:
    leftover = scratch_root / ("ab" * 32)
    leftover.mkdir()
    return leftover


class ScratchRootWithPath(Protocol):
    path: Path


def _compose_with_scratch(settings: object, scratch_root: ScratchRootWithPath):
    factory = harness.RecordingAgentExecutorFactoryV2(
        "e2e-v3", "immediate/v1", "e2e-immediate-process", b'"V3 provider bytes"'
    )

    def build_runtime(
        runtime_settings: object,
        effect_factory: object,
        agent_factories_v2: tuple,
        *,
        tracker_item_source: object = None,
    ) -> object:
        return harness.DbosRuntime(
            harness.replace(runtime_settings, agent_scratch_root=scratch_root.path),
            effect_factory,
            (*agent_factories_v2, factory, harness.baseline_agent_executor_factory()),
            tracker_item_source=tracker_item_source,
        )

    with patch.object(harness.serving, "DbosRuntime", side_effect=build_runtime):
        return harness.serving.compose_application(settings)


def test_a_replaced_generation_scratch_root_drops_leftover_attempt_directories(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    roots = iter((tmp_path / "generation-1", tmp_path / "generation-2"))

    def fake_mkdtemp(prefix: str) -> str:
        path = next(roots)
        path.mkdir()
        return str(path)

    monkeypatch.setattr(harness.tempfile, "mkdtemp", fake_mkdtemp)
    previous = harness.BrowserScratchRoot.create()
    leftover = _leftover_attempt_directory(previous.path)

    next_root = harness.replace_closed_generation_scratch_root(previous)

    assert not leftover.exists()
    assert not previous.path.exists()
    assert next_root.path.is_dir()
    assert list(next_root.path.iterdir()) == []
    next_root.close()


def test_reusing_a_scratch_root_that_still_holds_attempt_directories_refuses_the_next_runtime(
    tmp_path: Path,
) -> None:
    """#747 (c): leftover attempt directories after a close are not in the next
    store. Reusing the scratch root makes the next workspace owner refuse
    during reconcile -- the fixture-host crash under a later `/__e2e/recompose`.
    """
    database = tmp_path / "atelier.sqlite"
    effects = tmp_path / "effects.sqlite"
    application_version = "e2e-scratch-reuse"
    harness.seed_boot_baseline(database, effects, application_version)
    settings = _served_settings(tmp_path, database, effects, application_version)
    scratch_root = harness.BrowserScratchRoot.create()
    _, runtime = _compose_with_scratch(settings, scratch_root)
    runtime.close()
    leftover = _leftover_attempt_directory(scratch_root.path)

    with pytest.raises(RunTransitionConflict, match="agent attempt is missing"):
        _compose_with_scratch(settings, scratch_root)

    leftover.rmdir()
    scratch_root.close()


def test_a_harness_wait_names_what_it_was_waiting_for() -> None:
    pending = threading.Event()
    with pytest.raises(TimeoutError, match="waiting for the held decode to start"):
        harness.wait_for(pending, "the held decode to start", timeout=0)


def test_a_harness_wait_fails_before_the_deadline_when_the_worker_dies() -> None:
    pending = threading.Event()

    def die() -> None:
        return

    thread = threading.Thread(target=die)
    thread.start()
    thread.join()
    started = time.monotonic()
    with pytest.raises(
        RuntimeError, match="thread died before the held decode to start"
    ):
        harness.wait_for(
            pending,
            "the held decode to start",
            thread=thread,
            timeout=harness.TIMEOUT_SECONDS,
        )
    assert time.monotonic() - started < 1.0


def test_a_harness_wait_returns_when_the_signal_already_fired() -> None:
    ready = threading.Event()
    ready.set()
    harness.wait_for(ready, "an already-set signal", timeout=0)


def test_a_harness_join_names_what_it_was_waiting_for() -> None:
    hold = threading.Event()

    def hang() -> None:
        hold.wait()

    thread = threading.Thread(target=hang)
    thread.start()
    try:
        with pytest.raises(
            TimeoutError, match="waiting for the recompose thread to finish"
        ):
            harness.join_thread(thread, "the recompose thread to finish", timeout=0)
    finally:
        hold.set()
        thread.join()


def test_a_harness_wait_until_names_what_it_was_waiting_for() -> None:
    with pytest.raises(TimeoutError, match="waiting for the mutation effect"):
        harness.wait_until(lambda: False, "the mutation effect", timeout=0)


def test_releasing_fake_provider_holds_unblocks_a_held_decode_before_the_hold_bound() -> (
    None
):
    holds = harness.FakeProviderHolds()
    factory = harness.HeldAgentExecutorFactory(
        "e2e-v3-held", "held/v1", "e2e-held-process", b'"V3 provider bytes"', holds
    )
    executor = factory.open()
    executor.requests.append(SimpleNamespace(run_id=SimpleNamespace(value="held-run")))
    finished = threading.Event()

    def run() -> None:
        executor.decode_process_completion(
            SimpleNamespace(),
            harness.AgentProcessCompletion(0, b'"V3 provider bytes"', b""),
        )
        finished.set()

    thread = threading.Thread(target=run)
    thread.start()
    harness.wait_for(executor.holding, "the held decode to start", thread=thread)
    holds.release_all()
    harness.join_thread(thread, "the held decode to finish after release")
    assert executor.released_before_bound
    assert finished.is_set()


def test_scratch_root_removal_cannot_precede_an_in_flight_decode_that_outlives_release(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    created_root = tmp_path / "in-flight-root"
    monkeypatch.setattr(
        harness.tempfile,
        "mkdtemp",
        lambda prefix: str(created_root.mkdir() or created_root),
    )
    scratch_root = harness.BrowserScratchRoot.create()
    runtime = ClosingRuntime(scratch_root.path)
    holds = harness.FakeProviderHolds()
    factory = harness.HeldAgentExecutorFactory(
        "e2e-v3-held", "held/v1", "e2e-held-process", b'"V3 provider bytes"', holds
    )
    executor = factory.open()
    executor.requests.append(SimpleNamespace(run_id=SimpleNamespace(value="held-run")))
    past_release = threading.Event()
    stall = threading.Event()
    decode_body = executor.decoder
    order: list[str] = []

    def stall_after_release(completion: object) -> object:
        past_release.set()
        harness.wait_for(stall, "the stalled decode to be resumed")
        result = decode_body(completion)
        order.append("decode-finished")
        return result

    executor.decoder = stall_after_release

    def run_decode() -> None:
        executor.decode_process_completion(
            SimpleNamespace(),
            harness.AgentProcessCompletion(0, b'"V3 provider bytes"', b""),
        )

    def drain_then_remove() -> None:
        harness.drain_inflight_fake_decodes(holds)
        order.append("idle")
        harness.close_runtime_and_scratch_root(runtime, scratch_root)
        order.append("removed")

    decoder_thread = threading.Thread(target=run_decode)
    closer = threading.Thread(target=drain_then_remove)
    decoder_thread.start()
    try:
        harness.wait_for(
            executor.holding,
            "the in-flight decode to start holding",
            thread=decoder_thread,
        )
        closer.start()
        harness.wait_for(
            past_release,
            "the in-flight decode to pass release",
            thread=decoder_thread,
        )
        assert created_root.is_dir()
        assert "idle" not in order
        assert "removed" not in order
        assert not runtime.closed
        stall.set()
        harness.join_thread(closer, "drain to remove the scratch root after the decode")
        harness.join_thread(decoder_thread, "the in-flight decode to finish")
        assert order == ["decode-finished", "idle", "removed"]
        assert runtime.closed
        assert not created_root.exists()
    finally:
        stall.set()
        holds.release_all()


def test_a_decode_that_starts_after_drain_observes_idle_is_rejected_before_scratch_root_removal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    created_root = tmp_path / "late-admit-root"
    monkeypatch.setattr(
        harness.tempfile,
        "mkdtemp",
        lambda prefix: str(created_root.mkdir() or created_root),
    )
    scratch_root = harness.BrowserScratchRoot.create()
    runtime = ClosingRuntime(scratch_root.path)
    holds = harness.FakeProviderHolds()
    factory = harness.HeldAgentExecutorFactory(
        "e2e-v3-held", "held/v1", "e2e-held-process", b'"V3 provider bytes"', holds
    )
    executor = factory.open()
    executor.requests.append(SimpleNamespace(run_id=SimpleNamespace(value="late-run")))

    harness.drain_inflight_fake_decodes(holds)

    with pytest.raises(RuntimeError, match="generation was sealed"):
        executor.decode_process_completion(
            SimpleNamespace(),
            harness.AgentProcessCompletion(0, b'"V3 provider bytes"', b""),
        )

    harness.close_runtime_and_scratch_root(runtime, scratch_root)
    assert runtime.closed
    assert not created_root.exists()


def test_a_stale_generation_decode_admitted_late_must_not_touch_the_removed_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gen1 = tmp_path / "generation-1"
    gen2 = tmp_path / "generation-2"
    roots = iter((gen1, gen2))

    def fake_mkdtemp(prefix: str) -> str:
        path = next(roots)
        path.mkdir()
        return str(path)

    monkeypatch.setattr(harness.tempfile, "mkdtemp", fake_mkdtemp)
    scratch_root = harness.BrowserScratchRoot.create()
    runtime = ClosingRuntime(scratch_root.path)
    holds = harness.FakeProviderHolds()
    factory = harness.HeldAgentExecutorFactory(
        "e2e-v3-held", "held/v1", "e2e-held-process", b'"V3 provider bytes"', holds
    )
    executor = factory.open()
    executor.requests.append(SimpleNamespace(run_id=SimpleNamespace(value="stale-run")))
    decode_body = executor.decoder
    touched: list[str] = []

    def touch_removed_root(completion: object) -> object:
        gen1.mkdir(exist_ok=True)
        (gen1 / "stale-decode").write_text("touched")
        touched.append("decoded")
        return decode_body(completion)

    executor.decoder = touch_removed_root
    entered = threading.Event()
    proceed = threading.Event()
    original_decode = executor.decode_process_completion

    def delay_before_admission(invocation: object, completion: object) -> object:
        entered.set()
        harness.wait_for(proceed, "the stale decode to be resumed")
        return original_decode(invocation, completion)

    executor.decode_process_completion = delay_before_admission
    outcome: list[RuntimeError | None] = []

    def run_decode() -> None:
        try:
            executor.decode_process_completion(
                SimpleNamespace(),
                harness.AgentProcessCompletion(0, b'"V3 provider bytes"', b""),
            )
        except RuntimeError as error:
            outcome.append(error)
            return
        outcome.append(None)

    decoder_thread = threading.Thread(target=run_decode)
    next_root = None
    decoder_thread.start()
    try:
        harness.wait_for(
            entered, "the stale decode to reach admission", thread=decoder_thread
        )
        harness.drain_inflight_fake_decodes(holds)
        runtime.close()
        holds.start_generation()
        next_root = harness.replace_closed_generation_scratch_root(scratch_root)
        assert not gen1.exists()
        proceed.set()
        harness.join_thread(decoder_thread, "the stale decode to be rejected")
        assert len(outcome) == 1
        assert isinstance(outcome[0], RuntimeError)
        assert "stale generation" in str(outcome[0])
        assert touched == []
        assert not gen1.exists()
        assert gen2.is_dir()
        assert list(gen2.iterdir()) == []
    finally:
        proceed.set()
        holds.release_all()
        if decoder_thread.is_alive():
            harness.join_thread(decoder_thread, "the leftover stale decode to stop")
        if next_root is not None:
            next_root.close()


def test_a_reset_recompose_opens_the_next_runtime_on_a_fresh_scratch_root(
    tmp_path: Path,
) -> None:
    """#747 (c): each served generation owns its scratch root. A reset
    recompose must not reopen DBOS against leftover attempt directories of the
    generation it just closed.
    """
    database = tmp_path / "atelier.sqlite"
    effects = tmp_path / "effects.sqlite"
    application_version = "e2e-scratch-rotate"
    harness.seed_boot_baseline(database, effects, application_version)
    settings = _served_settings(tmp_path, database, effects, application_version)
    holds = harness.FakeProviderHolds()
    blocking = harness.BlockingAgentExecutorFactory(
        "e2e", "blocking/v1", "e2e-blocking-process", b"reset-scratch-fixture"
    )
    v2 = harness.RecordingAgentExecutorFactoryV2(
        "e2e-v3", "immediate/v1", "e2e-immediate-process", b'"V3 provider bytes"'
    )
    scratch = {"root": harness.BrowserScratchRoot.create()}
    first_root = scratch["root"].path

    def build_runtime(
        runtime_settings: object,
        effect_factory: object,
        agent_factories_v2: tuple,
        *,
        tracker_item_source: object = None,
    ) -> object:
        return harness.DbosRuntime(
            harness.replace(runtime_settings, agent_scratch_root=scratch["root"].path),
            effect_factory,
            (
                *agent_factories_v2,
                blocking,
                v2,
                harness.baseline_agent_executor_factory(),
            ),
            tracker_item_source=tracker_item_source,
        )

    def compose() -> tuple:
        with patch.object(harness.serving, "DbosRuntime", side_effect=build_runtime):
            return harness.serving.compose_application(settings)

    def compose_next_generation() -> tuple:
        holds.start_generation()
        scratch["root"] = harness.replace_closed_generation_scratch_root(
            scratch["root"]
        )
        return compose()

    def drain_inflight() -> None:
        harness.drain_inflight_fake_decodes(holds, blocking)

    app, runtime = compose()
    leftover = _leftover_attempt_directory(first_root)
    proof: Any = None
    restart_threads: list[threading.Thread] = []
    proof_holder: list[Any] = []

    def request_restart(reset: bool) -> None:
        thread = threading.Thread(
            target=proof_holder[0].recompose_after_server_stop, args=(reset,)
        )
        thread.start()
        restart_threads.append(thread)

    try:
        proof = harness.BrowserProofHarness(
            app,
            runtime,
            blocking,
            compose_next_generation,
            request_restart,
            lambda: harness.reset_to_boot_baseline(
                database, effects, application_version
            ),
            drain_inflight,
        )
        proof_holder.append(proof)

        with TestClient(proof) as client:
            restarted = client.post("/__e2e/recompose?reset=true")
            assert restarted.status_code == 202
            expected_generation = restarted.text
            harness.join_thread(
                restart_threads[0], "the scratch-root reset recompose to finish"
            )
            observed_generation = client.get("/__e2e/generation")
            assert observed_generation.text == expected_generation

        assert not leftover.exists()
        assert not first_root.exists()
        assert scratch["root"].path.is_dir()
        assert scratch["root"].path != first_root
    finally:
        drain_inflight()
        if proof is not None:
            proof.runtime.close()
        else:
            runtime.close()
        scratch["root"].close()


def test_scratch_root_removal_cannot_precede_capture_after_decode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#747 (c2): drain waits for the rest of execute_agent_attempt, not only decode."""
    created_root = tmp_path / "capture-root"
    monkeypatch.setattr(
        harness.tempfile,
        "mkdtemp",
        lambda prefix: str(created_root.mkdir() or created_root),
    )
    scratch_root = harness.BrowserScratchRoot.create()
    runtime = ClosingRuntime(scratch_root.path)
    holds = harness.FakeProviderHolds()
    factory = harness.RecordingAgentExecutorFactoryV2(
        "e2e-v3", "immediate/v1", "e2e-immediate-process", b'"V3 provider bytes"'
    )
    executor = factory.open()
    executor.requests.append(
        SimpleNamespace(run_id=SimpleNamespace(value="immediate-run"))
    )
    past_decode = threading.Event()
    stall = threading.Event()
    entered_idle_wait = threading.Event()
    order: list[str] = []
    original_wait = harness.FakeProviderHolds.wait_until_idle

    def wait_and_signal(self: object, timeout: float = harness.TIMEOUT_SECONDS) -> None:
        entered_idle_wait.set()
        original_wait(self, timeout)

    monkeypatch.setattr(harness.FakeProviderHolds, "wait_until_idle", wait_and_signal)

    def capture_after_decode(*_args: object, **_kwargs: object) -> None:
        executor.decode_process_completion(
            SimpleNamespace(),
            harness.AgentProcessCompletion(0, b'"V3 provider bytes"', b""),
        )
        past_decode.set()
        harness.wait_for(stall, "the stalled capture to be resumed")
        order.append("capture-finished")

    tracked = harness.track_execute_agent_attempt(holds, capture_after_decode)

    def run_attempt() -> None:
        tracked()

    def drain_then_remove() -> None:
        harness.drain_inflight_fake_decodes(holds)
        order.append("idle")
        harness.close_runtime_and_scratch_root(runtime, scratch_root)
        order.append("removed")

    attempt_thread = threading.Thread(target=run_attempt)
    closer = threading.Thread(target=drain_then_remove)
    attempt_thread.start()
    try:
        harness.wait_for(
            past_decode,
            "the tracked attempt to finish decode",
            thread=attempt_thread,
        )
        closer.start()
        harness.wait_for(
            entered_idle_wait,
            "drain to wait for capture after decode",
            thread=closer,
        )
        assert created_root.is_dir()
        assert "idle" not in order
        assert "removed" not in order
        assert not runtime.closed
        stall.set()
        harness.join_thread(closer, "drain to remove the scratch root after capture")
        harness.join_thread(attempt_thread, "the tracked attempt to finish")
        assert order == ["capture-finished", "idle", "removed"]
        assert runtime.closed
        assert not created_root.exists()
    finally:
        stall.set()
        holds.release_all()


def test_scratch_root_removal_cannot_precede_an_active_dbos_workflow(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#747 (c2): drain waits for in-process DBOS workflows before rmtree."""
    created_root = tmp_path / "dbos-active-root"
    monkeypatch.setattr(
        harness.tempfile,
        "mkdtemp",
        lambda prefix: str(created_root.mkdir() or created_root),
    )
    scratch_root = harness.BrowserScratchRoot.create()
    runtime = ClosingRuntime(scratch_root.path)
    holds = harness.FakeProviderHolds()
    workflow_id = "atelier2-node-still-capturing"
    workflows = harness.NotifyingActiveWorkflows()
    workflows.acquire(workflow_id)
    seen_active = threading.Event()
    order: list[str] = []

    def fake_notifying_active_workflows() -> object:
        seen_active.set()
        return workflows

    monkeypatch.setattr(
        harness, "notifying_active_workflows", fake_notifying_active_workflows
    )

    def drain_then_remove() -> None:
        harness.drain_inflight_fake_decodes(holds)
        order.append("idle")
        harness.close_runtime_and_scratch_root(runtime, scratch_root)
        order.append("removed")

    closer = threading.Thread(target=drain_then_remove)
    closer.start()
    try:
        harness.wait_for(
            seen_active, "drain to see the active DBOS workflow", thread=closer
        )
        assert created_root.is_dir()
        assert "idle" not in order
        assert "removed" not in order
        assert not runtime.closed
        workflows.release(workflow_id)
        harness.join_thread(
            closer, "drain to remove the scratch root after workflows idle"
        )
        assert order == ["idle", "removed"]
        assert runtime.closed
        assert not created_root.exists()
    finally:
        if workflow_id in workflows.activeList():
            workflows.release(workflow_id)
        holds.release_all()


def test_wait_until_dbos_workflows_idle_fails_loud_when_a_workflow_does_not_finish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#747 (c2): drain fails loud when an in-process workflow never finishes."""
    workflows = harness.NotifyingActiveWorkflows()
    workflows.acquire("atelier2-node-still-capturing")
    monkeypatch.setattr(harness, "notifying_active_workflows", lambda: workflows)
    with pytest.raises(TimeoutError, match="waiting for 1 DBOS workflow"):
        harness.wait_until_dbos_workflows_idle(timeout=0)
