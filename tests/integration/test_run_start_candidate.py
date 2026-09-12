"""A publisher that continues an earlier one begins in that publication's work.

Standing on the earlier publication's base is only half of a fix round. The
other half is the work itself: a node declaring `starts_from` opens its lease on
the candidate that publication carried, adds to it, and pushes a commit that
replaces the first one on the same base -- so the branch keeps everything the
run has made, and a round that changes nothing is not a failure.

Which node a run may continue is decided before the run starts, and what it
begins in is read before its work item is claimed. Both are claims about real
trees and real receipts, so every scenario here drives the arrangement
`tests/scenarios/publishing_runs.py` owns: one repository, one bare remote, one
candidate store, one durable run.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from atelier2.adapters.candidate_store import GitCandidateTreeStore
from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.node_binding_codec import decode_node_binding
from atelier2.adapters.dbos.work_item_claims import refuse_unattested_pin
from atelier2.adapters.project_source import LocalGitProjectSource
from atelier2.application.bind_node import agent_execution_request_v2, pinned_project
from atelier2.application.evaluate_executability import (
    DocumentNotExecutable,
    ExecutableDocument,
    evaluate_executability,
)
from atelier2.application.execute_agent_attempt import execute_agent_attempt
from atelier2.contracts.agent_attempts import AgentAttemptId, AgentAttemptState
from atelier2.contracts.agent_permissions import GRANTS_NOTHING
from atelier2.contracts.agents import (
    AgentExecutionCapability,
    AgentExecutionRequestV2,
    AgentExecutionResult,
    AgentExecutorOperationalIdentity,
)
from atelier2.contracts.executions import AgentAttemptExecution, AgentExecutionRefusal
from atelier2.contracts.node_bindings import AgentNodeBindingV2
from atelier2.contracts.project_sources import CandidateTree
from atelier2.contracts.runs import RunState
from atelier2.ports.agent_attempts import (
    AgentAttemptExecutionOutcome,
    AgentAttemptSucceeded,
)
from atelier2.ports.agent_executions import (
    AgentProcessCommand,
    AgentProcessCompletion,
    AgentProcessInvocation,
    PrintModeExecutor,
)
from atelier2.ports.candidate_store import CandidateNotKept
from tests.scenarios.agents import (
    SCENARIO_PROVIDER_FRAME_BYTES,
    agent_attempt_execution,
    leased_directory_identity,
    runtime_workspace_owner,
    workspace_files_nobody_opens,
)
from tests.scenarios.projects import run_git
from tests.scenarios.publishing_runs import (
    BUILD,
    FIX,
    RUN,
    Publisher,
    PublishingRun,
    publishing_run,
    workflow_graph,
)
from tests.scenarios.runs import V3_OPERATIONAL_IDENTITY

WHAT_THE_BUILDER_MADE = {
    "kept.txt": "what the builder rewrote\n",
    "made.txt": "what the builder added\n",
}
WHAT_TRUNK_GAINED = {"trunk.txt": "what trunk gained beside the run\n"}
WHAT_THE_FIXER_ADDS = {"fixed.txt": "what the fixer added\n"}

WORK_AND_REPORT = (
    "import json, os, sys; "
    "found = sorted(os.listdir()); "
    "[open(name, 'w').write(body) for name, body in json.loads(sys.argv[1]).items()]; "
    "print(json.dumps(found), end='')"
)
"""What the provider does: name what it was started in, then do its work there."""


@dataclass
class WorkingProvider(PrintModeExecutor):
    """Writes these files where it was started, and names what it found there."""

    writes: Mapping[str, str] = field(default_factory=dict)
    found: list[frozenset[str]] = field(default_factory=list)

    def prepare_process(self, request: AgentExecutionRequestV2) -> AgentProcessCommand:
        del request
        return AgentProcessCommand(
            (sys.executable, "-c", WORK_AND_REPORT, json.dumps(dict(self.writes))),
            standard_output_frame_bytes=SCENARIO_PROVIDER_FRAME_BYTES,
        )

    def decode_process_completion(
        self, invocation: AgentProcessInvocation, completion: AgentProcessCompletion
    ) -> AgentExecutionResult:
        del invocation
        self.found.append(frozenset(json.loads(completion.standard_output)))
        return AgentExecutionResult(completion.standard_output)

    def release_credential_channel(self, command: AgentProcessCommand) -> None:
        del command

    def close(self) -> None:
        return None


def attempt_of(
    run: PublishingRun, publisher: Publisher
) -> tuple[AgentNodeBindingV2, AgentAttemptExecution]:
    """The binding this node's own step records, and the attempt it identifies."""

    binding = decode_node_binding(run.binding_of(publisher))
    assert isinstance(binding, AgentNodeBindingV2), binding
    return binding, agent_attempt_execution(
        agent_execution_request_v2(
            binding,
            RUN,
            run.revision.revision_hash,
            publisher.node_id,
            AgentExecutorOperationalIdentity(V3_OPERATIONAL_IDENTITY),
            frozenset({AgentExecutionCapability.HEADLESS}),
        )
    )


def worked(
    run: PublishingRun, publisher: Publisher, provider: WorkingProvider
) -> tuple[AgentAttemptExecutionOutcome, CandidateTree | None]:
    """Run this node's attempt exactly as its durable binding composes it.

    Everything below the provider is production: the binding step records what
    the node begins in, the use case opens the lease and unpacks it there, and
    the store keeps whatever was left behind.
    """

    binding, execution = attempt_of(run, publisher)
    outcome = execute_agent_attempt(
        execution,
        provider,
        DbosAgentAttemptStore(run.runtime.engine),
        run.runtime.agent_process_supervisor,
        runtime_workspace_owner(run.runtime),
        pinned_project(binding, run.declared_project()),
        permissions=GRANTS_NOTHING,
        workspace_files=workspace_files_nobody_opens,
    )
    return outcome, run.declared_project().candidates.read(execution.attempt_id)


def published_build(run: PublishingRun) -> CandidateTree:
    """What `build` made, kept and pushed, with trunk moving on afterwards."""

    started_on = LocalGitProjectSource(run.project).head()
    built = run.keep(BUILD, WHAT_THE_BUILDER_MADE)
    run.forget_candidates()
    run.publish(BUILD, built, started_on.commit)
    run.move_trunk(WHAT_TRUNK_GAINED)
    run.advance_past(BUILD)
    return built


def unanchored(run: PublishingRun, candidate: CandidateTree) -> None:
    """Take this attempt's anchor away, as a store that lost the work would have."""

    run_git(
        run.candidates,
        "update-ref",
        "-d",
        f"refs/atelier/candidates/{candidate.attempt_id.value}",
    )


def rotted(run: PublishingRun, candidate: CandidateTree, path: str) -> None:
    """Break one blob of this candidate where it lies, as a losing disk would.

    The anchor stands, the tree stands, and every object of it is still findable
    under its own name -- only what one of them inflates to is gone. That is the
    loss a walk which merely finds objects cannot see and a checkout would be
    the first to hit.
    """

    blob = run_git(run.candidates, "rev-parse", f"{candidate.tree}:{path}")
    stored = run.candidates / "objects" / blob[:2] / blob[2:]
    stored.chmod(0o644)
    bytes_of_it = bytearray(stored.read_bytes())
    bytes_of_it[len(bytes_of_it) // 2] ^= 0xFF
    stored.write_bytes(bytes(bytes_of_it))


def never_kept(run: PublishingRun, files: Mapping[str, str]) -> CandidateTree:
    """A tree this project really has and this store was never given."""

    for name, body in files.items():
        run.project.joinpath(name).write_text(body, encoding="utf-8")
    run_git(run.project, "add", "--all")
    tree = run_git(run.project, "write-tree")
    run.forget_candidates()
    return CandidateTree(AgentAttemptId(BUILD.attempt_id()), tree)


def test_a_continuing_publisher_opens_its_lease_on_the_published_candidate(
    tmp_path: Path,
) -> None:
    with publishing_run(tmp_path, "Build, then fix", (BUILD, FIX)) as run:
        published_build(run)
        provider = WorkingProvider(WHAT_THE_FIXER_ADDS)

        worked(run, FIX, provider)

        assert provider.found == [frozenset(WHAT_THE_BUILDER_MADE)]


def test_the_commit_a_continuing_publisher_pushes_carries_both_rounds_on_one_base(
    tmp_path: Path,
) -> None:
    """The point of the round: one commit, the first base, everything the run made."""

    with publishing_run(tmp_path, "Build, then fix", (BUILD, FIX)) as run:
        started_on = LocalGitProjectSource(run.project).head()
        published_build(run)
        _outcome, fixed = worked(run, FIX, WorkingProvider(WHAT_THE_FIXER_ADDS))
        assert fixed is not None

        second = run.publish(FIX, fixed, run.pin_of(FIX).commit)

        assert run.parent_of(second) == started_on.commit
        assert run.changed_between(started_on.commit, second) == frozenset(
            {*WHAT_THE_BUILDER_MADE, *WHAT_THE_FIXER_ADDS}
        )


def test_a_continuation_that_changes_nothing_keeps_the_candidate_it_began_in(
    tmp_path: Path,
) -> None:
    """An empty round is no failure, and a trip through the store is the same tree."""

    with publishing_run(tmp_path, "Build, then fix", (BUILD, FIX)) as run:
        built = published_build(run)

        outcome, kept = worked(run, FIX, WorkingProvider())

        assert isinstance(outcome, AgentAttemptSucceeded), outcome
        assert kept is not None
        assert kept.tree == built.tree


def test_a_first_publisher_declaring_no_continuation_begins_in_its_pin(
    tmp_path: Path,
) -> None:
    with publishing_run(tmp_path, "Build, then fix", (BUILD, FIX)) as run:
        assert "start_candidate_tree" not in run.binding_of(BUILD)


def test_a_replacement_attempt_begins_in_the_candidate_the_first_one_did(
    tmp_path: Path,
) -> None:
    """A replacement composes the binding again; what it begins in must not move."""

    with publishing_run(tmp_path, "Build, then fix", (BUILD, FIX)) as run:
        built = published_build(run)
        first = run.binding_of(FIX)
        run.move_trunk({"later.txt": "what trunk gained after that\n"})

        replacement = run.binding_of(FIX)

        assert first.get("start_candidate_tree") == built.tree
        assert replacement.get("start_candidate_tree") == built.tree


def test_a_start_candidate_the_store_lost_stops_the_attempt_before_it_is_claimed(
    tmp_path: Path,
) -> None:
    """Never a fall back on the pin: that push would take the first round away."""

    with publishing_run(tmp_path, "Build, then fix", (BUILD, FIX)) as run:
        built = published_build(run)
        _binding, execution = attempt_of(run, FIX)
        unanchored(run, built)
        provider = WorkingProvider(WHAT_THE_FIXER_ADDS)

        with pytest.raises(CandidateNotKept, match=built.tree):
            worked(run, FIX, provider)

        assert provider.found == []
        attempts = DbosAgentAttemptStore(run.runtime.engine)
        assert attempts.load(execution.attempt_id).state is AgentAttemptState.PREPARED


def test_a_start_candidate_that_no_longer_reads_back_stops_the_attempt_before_the_claim(
    tmp_path: Path,
) -> None:
    """Standing there is not readable: the check before the claim reads what it finds."""

    with publishing_run(tmp_path, "Build, then fix", (BUILD, FIX)) as run:
        built = published_build(run)
        _binding, execution = attempt_of(run, FIX)
        one_of_its_files = next(iter(WHAT_THE_BUILDER_MADE))
        rotted(run, built, one_of_its_files)
        provider = WorkingProvider(WHAT_THE_FIXER_ADDS)

        with pytest.raises(CandidateNotKept, match=built.tree):
            worked(run, FIX, provider)

        assert provider.found == []
        attempts = DbosAgentAttemptStore(run.runtime.engine)
        assert attempts.load(execution.attempt_id).state is AgentAttemptState.PREPARED


def test_a_node_whose_start_candidate_is_gone_ends_instead_of_holding_its_lane(
    tmp_path: Path,
) -> None:
    """The claim door reads the same loss, and answers it as this node's own end."""

    with publishing_run(tmp_path, "Build, then fix", (BUILD, FIX)) as run:
        built = published_build(run)
        binding, _execution = attempt_of(run, FIX)
        unanchored(run, built)

        ended = refuse_unattested_pin(
            run.runtime.datasource,
            binding,
            run.declared_project(),
            RUN,
            run.revision.revision_hash,
            FIX.node_id,
        )

        assert ended == RunState.FAILED.value
        assert run.claims_taken() == ()
        refusal = run.refusal_of(FIX)
        assert refusal is not None
        assert refusal.refusal is AgentExecutionRefusal.WORK_ITEM_CLAIM_REFUSED
        assert built.tree in refusal.detail


def test_a_candidate_this_store_never_kept_writes_nothing_into_a_lease(
    tmp_path: Path,
) -> None:
    """A checkout that cannot finish leaves no half tree an attempt could work in."""

    with publishing_run(tmp_path, "Build alone", (BUILD,)) as run:
        elsewhere = never_kept(run, WHAT_THE_BUILDER_MADE)
        store = GitCandidateTreeStore(run.project, run.runtime.settings.database_path)
        lease = leased_directory_identity(elsewhere.attempt_id, tmp_path / "lease")

        with pytest.raises(CandidateNotKept):
            store.materialize(elsewhere, lease)

        assert list(lease.working_directory.iterdir()) == []


UNGRANTED_CONTINUATION = Publisher(
    "fix", depends_on=("build",), publishes=False, starts_from="build"
)
UNDECLARED_SOURCE = Publisher("fix", depends_on=("build",), starts_from="nowhere")
UNORDERED_SOURCE = Publisher("build", starts_from="fix")
UNPUBLISHING_SOURCE = Publisher("fix", depends_on=("seed",), starts_from="seed")
SILENT_SECOND_PUBLISHER = Publisher("fix", depends_on=("build",))
QUIET_PREDECESSOR = Publisher("seed", publishes=False)


@pytest.mark.parametrize(
    ("line", "refused_for"),
    [
        pytest.param(
            (BUILD, UNGRANTED_CONTINUATION), "holds no push grant", id="no-grant"
        ),
        pytest.param(
            (BUILD, UNDECLARED_SOURCE), "this graph never declares", id="no-such-node"
        ),
        pytest.param(
            (UNORDERED_SOURCE, FIX), "does not order before it", id="not-ordered"
        ),
        pytest.param(
            (QUIET_PREDECESSOR, UNPUBLISHING_SOURCE),
            "publishes nothing",
            id="not-a-publisher",
        ),
        pytest.param(
            (BUILD, SILENT_SECOND_PUBLISHER),
            "declares no starts_from",
            id="silent-second-publisher",
        ),
    ],
)
def test_a_document_no_run_could_continue_is_refused_before_it_starts(
    tmp_path: Path, line: tuple[Publisher, ...], refused_for: str
) -> None:
    """Named at the start gate, where every pinned grant has just been resolved."""

    with publishing_run(tmp_path, "Build, then fix", (BUILD, FIX)) as run:
        judged = evaluate_executability(
            workflow_graph("Refused", line), DbosCatalogStore(run.runtime.engine)
        )

        assert isinstance(judged, DocumentNotExecutable), judged
        assert refused_for in judged.reason


def test_a_declared_continuation_of_an_earlier_publisher_is_executable(
    tmp_path: Path,
) -> None:
    with publishing_run(tmp_path, "Build, then fix", (BUILD, FIX)) as run:
        judged = evaluate_executability(
            workflow_graph("Build, then fix", (BUILD, FIX)),
            DbosCatalogStore(run.runtime.engine),
        )

        assert isinstance(judged, ExecutableDocument), judged
