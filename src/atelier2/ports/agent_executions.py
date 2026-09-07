from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from atelier2.contracts.agent_attempts import (
    MAXIMUM_RUNNER_STANDARD_ERROR_BYTES,
    AgentAttempt,
    AgentAttemptCancellationDisposition,
    AgentAttemptFailureCode,
    AgentAttemptId,
    AgentProcessOwnerId,
    WatchdogGenerationId,
)
from atelier2.contracts.agent_permissions import (
    PermissionDecision,
    PermissionRequest,
)
from atelier2.contracts.agent_transcripts import AttemptTranscript, TranscriptEvent
from atelier2.contracts.agents import (
    MAXIMUM_AGENT_PROCESS_INPUT_BYTES,
    MAXIMUM_AGENT_PROCESS_STANDARD_OUTPUT_BYTES,
    MAXIMUM_SIGNED_INT64,
    UNATTENDED_AGENT_EXECUTION_CAPABILITIES,
    AgentConfigurationRevisionHash,
    AgentExecutionCapability,
    AgentExecutionRequestV2,
    AgentExecutionResult,
    AgentExecutorOperationalIdentity,
    AgentExecutorRevision,
    ProviderId,
)
from atelier2.contracts.executions import AgentAttemptExecution
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.provider_probe_receipts import (
    ProviderProbeReceipt,
    ProviderProbeResult,
)
from atelier2.contracts.runs import WorkflowRevisionHash
from atelier2.contracts.when import RecordedAt
from atelier2.ports.provider_conversations import (
    ProviderCancellationCause,
    ProviderConversation,
    ProviderFilesystemAccess,
    ProviderTerminalOutcome,
)

MAXIMUM_AGENT_PROCESS_STANDARD_ERROR_BYTES = MAXIMUM_RUNNER_STANDARD_ERROR_BYTES


@dataclass(frozen=True)
class AgentExecutorKey:
    provider_id: ProviderId
    executor_revision: AgentExecutorRevision


class AgentExecutorCarrier(StrEnum):
    """Which authority starts one executor key's process (`#540` C-3.6).

    A registration's own fact, not the factory's: it is the composition root
    -- never the executor adapter itself -- that decides which authority a
    served key answers under. `LOCAL_PROCESS` is Serve's own
    `AgentProcessSupervisor`, the durable runtime's only carrier.
    """

    LOCAL_PROCESS = "local_process"


class WorkspaceFileTools(StrEnum):
    """Whether an executor's invocation may read and write the attempt's workspace.

    The narrower question `AgentExecutionCapability` deliberately never asks. A
    capability says a node asked for a tool-bearing call; this says whether the
    tools that call carries reach the files the attempt stands in. Two executors
    declaring `HEADLESS_WITH_TOOLS` can differ completely here -- one grants the
    workspace, another grants the product's own API doors and removes every
    built-in with `--tools=` -- and a run that pins a tool grant is asking for
    the first (`resolve_start_bindings`).

    The sentence belongs to the adapter that composes the invocation, and the
    composition root states it at registration until every factory declares it
    itself (#1166): a registration that says nothing keeps the answer every
    registration gave implicitly before this field existed.
    """

    GRANTED = "granted"
    WITHHELD = "withheld"


@dataclass(frozen=True)
class AgentExecutorManifestEntry:
    key: AgentExecutorKey
    operational_identity: AgentExecutorOperationalIdentity
    declared_capabilities: frozenset[AgentExecutionCapability]
    carrier: AgentExecutorCarrier = AgentExecutorCarrier.LOCAL_PROCESS
    workspace_file_tools: WorkspaceFileTools = WorkspaceFileTools.GRANTED


@dataclass(frozen=True)
class AgentExecutionFailure:
    """This process left no answer this executor could use, and what it did leave.

    The transcript is the executor's own reading of what the process wrote --
    the steps it got through, and whatever it printed instead of a usable
    answer. It travels with the failure rather than beside it because only the
    executor knows its provider's wire format, and only the failure it returns
    reaches the seam that can keep the reading durably.
    """

    code: AgentAttemptFailureCode
    transcript: AttemptTranscript | None = None


class AgentExecutionPreflightRefusal(Exception):
    """An executor refused a job before it could start a provider process."""

    code = AgentAttemptFailureCode.AGENT_REFUSED

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class AgentProcessCommand:
    """What one provider asks to be run, whose secrets remain reference-only.

    The executor declares `standard_output_frame_bytes`: the raw stdout frame
    this exact command may produce before supervision refuses it. The port
    owns the field and its validity; the value belongs to the provider whose
    wire format produces the frame, so no provider's number lives here. It is
    a different bound from the durable result bound a decoded execution result
    must satisfy.

    The command carries no working directory. Where a provider runs is an
    attempt's decision, not a provider's, so it arrives as a separate lease.

    Direct process adapters may durably retain this command while proving
    at-most-once launch. Its ordered environment may therefore contain only
    non-secret paths, references and toggles, and it is the child's complete
    environment rather than an overlay on the controller's environment.
    Credential material is handed off through a provider-owned path or OS
    credential channel, never as a value in this record.
    """

    arguments: tuple[str, ...]
    environment: tuple[tuple[str, str], ...] = ()
    standard_input: bytes = b""
    standard_output_frame_bytes: int = field(kw_only=True)

    def __post_init__(self) -> None:
        if not self.arguments or any(not value for value in self.arguments):
            raise ValueError("agent process arguments must be nonempty")
        names = tuple(name for name, _value in self.environment)
        if len(set(names)) != len(names) or any(not name for name in names):
            raise ValueError(
                "agent process environment names must be unique and nonempty"
            )
        if len(self.standard_input) > MAXIMUM_AGENT_PROCESS_INPUT_BYTES:
            raise ValueError(
                "agent process standard input exceeds "
                f"{MAXIMUM_AGENT_PROCESS_INPUT_BYTES} bytes"
            )
        if (
            type(self.standard_output_frame_bytes) is not int
            or self.standard_output_frame_bytes < 1
            or self.standard_output_frame_bytes
            > MAXIMUM_AGENT_PROCESS_STANDARD_OUTPUT_BYTES
        ):
            raise ValueError(
                "agent process standard output frame must fit the portable bound"
            )


# Linux refuses a path longer than PATH_MAX including its terminator, so a
# leased directory that could not be opened is not a directory this port may
# promise. The bound is the port's because durable records derive theirs from
# it: a record whose size bound is derived must know what it may hold.
MAXIMUM_AGENT_ATTEMPT_WORKSPACE_PATH_BYTES = 4_095


@dataclass(frozen=True)
class AgentAttemptWorkspaceLease:
    """One attempt's own scratch working directory, held only in live memory.

    The lease is bound to exactly one `AgentAttemptId`, so two attempts of the
    same node -- an ordinal-1 attempt and its deliberate ordinal-2 replacement
    -- never share a directory. It claims nothing about operating-system
    isolation: it is the directory this attempt owns, holding whatever its
    binding pinned, not a sandbox.

    It carries the directory's own identity, not only its path, because a launch
    happens later and elsewhere: between the attestation and the first process
    there is a window in which a peer of this user can replace the directory the
    path names. The launcher enters the identity -- open, `fstat`, compare, then
    enter through the descriptor it checked -- so the name is never resolved a
    second time.
    """

    attempt_id: AgentAttemptId
    working_directory: Path
    device: int
    inode: int

    def __post_init__(self) -> None:
        if not isinstance(self.attempt_id, AgentAttemptId):
            raise TypeError("agent attempt workspace lease identity must be typed")
        if type(self.device) is not int or type(self.inode) is not int:
            raise TypeError("agent attempt workspace identity must be typed")
        if self.device < 0 or self.inode < 0:
            raise ValueError("agent attempt workspace identity must be nonnegative")
        if not self.working_directory.is_absolute():
            raise ValueError("agent attempt workspace directory must be absolute")
        if (
            len(str(self.working_directory).encode("utf-8"))
            > MAXIMUM_AGENT_ATTEMPT_WORKSPACE_PATH_BYTES
        ):
            raise ValueError("agent attempt workspace directory exceeds the path bound")


@dataclass(frozen=True, slots=True)
class ProviderConversationBinding:
    """One live conversation, bound to the executor revision that opened it.

    The revision is the comparable half and neither the driver nor its file
    access is: at-most-once launch compares what an invocation *is*, and live
    objects are never that. A second launch of one armed session carrying
    another revision's conversation is therefore refused instead of quietly
    sharing the first one's ending.
    """

    executor_revision: AgentExecutorRevision
    driver: ProviderConversation = field(compare=False, repr=False)
    files: ProviderFilesystemAccess = field(compare=False, repr=False)


@dataclass(frozen=True)
class AgentProcessInvocation:
    """One provider process invocation: a provider's command in one lease.

    `conversation` is absent for every provider that answers in print mode --
    a payload in, a frame out, nothing to ask. Where one is present the child's
    standard input stays open and supervision relays between it and this
    attempt's own permission authority.
    """

    command: AgentProcessCommand
    lease: AgentAttemptWorkspaceLease
    conversation: ProviderConversationBinding | None = None


class AgentAttemptWorkspaceOwner(Protocol):
    """The provider-neutral owner of every attempt's scratch directory."""

    def preflight(self) -> None:
        """Refuse an unusable scratch root without mutating anything."""
        ...

    def acquire(self, attempt_id: AgentAttemptId) -> AgentAttemptWorkspaceLease:
        """Create this attempt's own directory. Invoke only after its claim won."""
        ...

    def release(self, attempt_id: AgentAttemptId) -> None:
        """Remove this attempt's directory and its contents, idempotently."""
        ...


@dataclass(frozen=True)
class AgentProcessCompletion:
    """Everything supervision saw of one ended process.

    `session_events` are the steps a conversation published while the process
    ran, in the order it published them, and `terminal_outcome` is what that
    conversation concluded about its own ending. A print-mode process leaves
    neither: its whole story is the frame it printed, which its executor reads
    afterwards, and an exit code is all it ever said about stopping.
    """

    return_code: int
    standard_output: bytes
    standard_error: bytes
    session_events: tuple[TranscriptEvent, ...] = ()
    terminal_outcome: ProviderTerminalOutcome | None = None

    def __post_init__(self) -> None:
        if type(self.return_code) is not int:
            raise TypeError("agent process return code must be an integer")
        if type(self.session_events) is not tuple:
            raise TypeError("agent process session events are an exact ordered tuple")
        if not -MAXIMUM_SIGNED_INT64 - 1 <= self.return_code <= MAXIMUM_SIGNED_INT64:
            raise ValueError("agent process return code must fit signed int64")


class AgentExecutorV2(Protocol):
    def prepare_process(self, request: AgentExecutionRequestV2) -> AgentProcessCommand:
        """Prepare a live-only command without starting a child."""
        ...

    def open_conversation(
        self,
        request: AgentExecutionRequestV2,
        command: AgentProcessCommand,
        lease: AgentAttemptWorkspaceLease,
    ) -> ProviderConversationBinding | None:
        """Open this invocation's conversation, or answer `None` for print mode.

        Asked after the attempt's claim is won and its workspace is leased, and
        before anything is launched: a conversation is live state, so a call
        that never reaches a process must never have made one. It sees the
        lease because a provider that speaks a protocol has to be told where it
        stands before its first frame, and the command because the same
        executor revision can prepare more than one shape of call.
        """
        ...

    def decode_process_completion(
        self, invocation: AgentProcessInvocation, completion: AgentProcessCompletion
    ) -> AgentExecutionResult | AgentExecutionFailure:
        """Decode the answer of exactly this invocation.

        The invocation travels back with the process result because a provider
        may deliver its answer beside the process rather than inside it, and a
        completion carries no identity. Without it such an executor could only
        correlate through its own mutable state -- and one executor object
        serves every attempt on its key, so overlapping attempts would decode
        each other's answers into durable results.

        Decoding is therefore not purely a reading of bytes: the invocation
        carries the lease, and an executor may here take back what its own CLI
        left standing in that workspace beside the model's work -- the one
        moment between the process ending and anything reading the tree, and
        the only seam that offers it. What such a take-back must never touch is
        the attempt's own work; an executor that cannot tell the two apart
        leaves the entry standing rather than guessing.
        """
        ...

    def release_credential_channel(self, command: AgentProcessCommand) -> None:
        """Take back the secret channel this invocation handed its provider.

        A provider that reads its credentials from a directory gets one made for
        this command alone, and it is taken back on every path -- success,
        refusal, a claim this call lost, or an exception. It takes the command
        rather than the invocation because the channel is made while the command
        is prepared, which is before the attempt is claimed and therefore before
        any workspace is leased. That is deliberately not the discipline of the
        attempt's workspace: the workspace falls only once the attempt is
        durably terminal, because what a provider left behind is evidence. A
        copy of the operator's credentials is not evidence, and the shortest
        life it can have is the one it gets.
        """
        ...

    def close(self) -> None: ...


class PrintModeExecutor:
    """The half of `AgentExecutorV2` a provider that only prints has no use for.

    A print-mode vector is handed its whole job at once and answers with one
    frame; there is no channel on which it could be asked anything, and every
    executor this product runs today is one. The sentence stands here once and
    each such executor names it, rather than six identical refusals -- and an
    executor that grows a real channel stops naming it and opens its own.
    """

    def open_conversation(
        self,
        request: AgentExecutionRequestV2,
        command: AgentProcessCommand,
        lease: AgentAttemptWorkspaceLease,
    ) -> None:
        del request, command, lease


class PermissionDecider(Protocol):
    """Who answers a running provider's permission question.

    One seam so that the session driver transports a question and an answer and
    decides neither. The only implementation is the policy revision the dispatch
    bound to this execution (`contracts.agent_permissions`).
    """

    def decide(self, request: PermissionRequest) -> PermissionDecision:
        """Answer exactly this question, under the authority the answer names."""
        ...


class AgentSession(Protocol):
    """One attempt's provider process, from armed to given up, behind one seam.

    Everything the application layer asks of a running provider: arm a session
    for an attempt before its launch boundary is decided, run exactly one
    `AgentProcessInvocation` in that attempt's own lease and wait for the
    process to end, and give up what supervision holds once the attempt is
    durably terminal. Stopping a live attempt belongs to the same seam rather
    than to a second authority, because only whoever holds the process can
    signal it, reap it, and say how it went out -- and an attempt whose
    supervision died with its host is attested here too.

    The first implementation is `AgentProcessSupervisor`, which runs the
    provider as a child of its own watchdog process; it is what every live
    attempt this product has run went through.
    """

    def prepare(self, execution: AgentAttemptExecution) -> AgentAttempt:
        """Arm a session for this attempt and durably bind who supervises it.

        Invoked before the launch boundary is decided, so a call that goes on
        to lose the claim has an armed session to give up rather than a
        process to kill. The attempt comes back as the store now holds it, and
        one that is no longer `PREPARED` comes back untouched: a session is
        armed exactly once.
        """
        ...

    def launch_and_wait(
        self,
        execution: AgentAttemptExecution,
        invocation: AgentProcessInvocation,
        permissions: PermissionDecider,
    ) -> AgentProcessCompletion:
        """Start exactly this invocation in its lease and wait for its ending.

        `permissions` is the authority this run's questions are answered under,
        handed in rather than looked up so that no session can answer under one
        the dispatch did not bind. An invocation carrying no conversation
        cannot ask at all -- print mode, a payload in and a frame out -- and
        no question is ever put to that authority.

        The completion is the terminal evidence of that process: the code it
        exited with, and the bounded output and error frames supervision
        collected -- what the caller composes a durable `ProcessExitSignature`
        from. One armed session launches one invocation: a second call
        carrying the same one waits for that same ending, and one carrying a
        different command is refused. An ending that is not the process's own
        -- an output frame outgrown, a cancellation, supervision this process
        no longer holds -- is raised rather than answered, because there is no
        exit code that would be true of it.
        """
        ...

    def cancel(
        self,
        attempt: AgentAttempt,
        cause: ProviderCancellationCause = ProviderCancellationCause.OPERATOR,
    ) -> tuple[
        AgentAttemptCancellationDisposition,
        AgentProcessOwnerId,
        WatchdogGenerationId,
    ]:
        """Stop this attempt's process now, and say how it went out.

        The disposition travels with the supervision identity that carried it
        out, because that triple is what the store attests the cleanup from.
        A session this process does not hold refuses with
        `AgentProcessOwnerNotLocal`; `recover` is that attempt's question.

        `cause` is why this attempt is being stopped, and it reaches the
        conversation as its ending: a signal cannot say afterwards whether a
        budget, a policy or an operator ended the run. It defaults to the
        operator's own request because that is the only stop this product has
        a caller for today.
        """
        ...

    def recover(
        self, attempt: AgentAttempt
    ) -> tuple[
        AgentAttemptCancellationDisposition,
        AgentProcessOwnerId,
        WatchdogGenerationId,
    ]:
        """Attest what is left of an attempt whose session this process never held.

        What a restart needs: the supervision that drove the attempt died with
        the host that ran it, so nothing here can signal it. Whatever it left
        is killed, and the attestation names the owner and generation the
        durable attempt already carries.
        """
        ...

    def release(self, attempt: AgentAttempt) -> None:
        """Give up what supervision holds for a durably attested cleanup."""
        ...

    def finalize(self, execution: AgentAttemptExecution) -> None:
        """Give up this attempt's session once the attempt is durably terminal."""
        ...


class AgentProcessOwnerNotLocal(Exception):
    pass


class AgentExecutorFactoryV2(Protocol):
    @property
    def key(self) -> AgentExecutorKey: ...

    @property
    def operational_identity(self) -> AgentExecutorOperationalIdentity: ...

    @property
    def declared_capabilities(self) -> frozenset[AgentExecutionCapability]: ...

    def open(self) -> AgentExecutorV2: ...


class ProviderProbeReceiptReads(Protocol):
    """The narrow read this registry needs from wherever receipts are filed.

    Kept apart from the receipt's own storage shape (a small `.json` file per
    vector, which `host/provider_canary.py` writes) so the registry never
    learns where or how evidence is kept -- only that it can be asked for, by
    the configuration it proves.
    """

    def receipt_for(
        self, configuration_hash: AgentConfigurationRevisionHash
    ) -> ProviderProbeReceipt | None: ...


@dataclass(frozen=True)
class ProviderProbeReceiptGate:
    """Whether one configuration's live evidence is still trustworthy, right now.

    Three independent facts must all hold: a receipt exists, it succeeded (a
    receipt carrying a problem code is evidence of the opposite), it was proven
    under the exact provider layer this deployment actually runs (a receipt
    whose `provider_layer_digest` differs proves nothing about this one), and
    the clock still sits inside its validity window. Any one absence answers
    `False` -- there is no partial credit for stale or foreign proof.

    The comparison is the provider layer's own content digest
    (`host.provider_canary.provider_layer_digest`), not the receipt's
    `source_commit` (#1124): a redeploy that never touches the adapter files
    behind a provider leaves every receipt proven, exactly as a redeploy that
    does touch them must not.
    """

    reads: ProviderProbeReceiptReads
    deployment_provider_layer_digest: Sha256Hash
    clock: Callable[[], RecordedAt]

    def __post_init__(self) -> None:
        if not isinstance(self.deployment_provider_layer_digest, Sha256Hash):
            raise TypeError(
                "a provider probe receipt gate needs this deployment's own "
                "provider layer digest to judge foreign evidence"
            )

    def is_proven(self, configuration_hash: AgentConfigurationRevisionHash) -> bool:
        receipt = self.reads.receipt_for(configuration_hash)
        if receipt is None or receipt.result is not ProviderProbeResult.SUCCEEDED:
            return False
        if receipt.provider_layer_digest != self.deployment_provider_layer_digest:
            return False
        return receipt.is_valid_at(self.clock())


@dataclass(frozen=True)
class AgentExecutorRegistryEntry:
    manifest_entry: AgentExecutorManifestEntry
    factory: AgentExecutorFactoryV2 | None

    @property
    def key(self) -> AgentExecutorKey:
        return self.manifest_entry.key


@dataclass(frozen=True)
class AgentExecutorRegistration:
    """One declared executor and the factory that can currently start it."""

    manifest_entry: AgentExecutorManifestEntry
    factory: AgentExecutorFactoryV2 | None

    @classmethod
    def startable(
        cls,
        factory: AgentExecutorFactoryV2,
        carrier: AgentExecutorCarrier = AgentExecutorCarrier.LOCAL_PROCESS,
        workspace_file_tools: WorkspaceFileTools = WorkspaceFileTools.GRANTED,
    ) -> AgentExecutorRegistration:
        return cls(
            AgentExecutorManifestEntry(
                factory.key,
                factory.operational_identity,
                frozenset(factory.declared_capabilities),
                carrier,
                workspace_file_tools,
            ),
            factory,
        )

    @classmethod
    def unavailable(
        cls,
        factory: AgentExecutorFactoryV2,
        carrier: AgentExecutorCarrier = AgentExecutorCarrier.LOCAL_PROCESS,
        workspace_file_tools: WorkspaceFileTools = WorkspaceFileTools.GRANTED,
    ) -> AgentExecutorRegistration:
        return cls(
            AgentExecutorManifestEntry(
                factory.key,
                factory.operational_identity,
                frozenset(factory.declared_capabilities),
                carrier,
                workspace_file_tools,
            ),
            None,
        )


class AgentExecutorRegistry:
    """Immutable host registry for declared executors and current startability."""

    def __init__(
        self,
        registrations: tuple[
            AgentExecutorFactoryV2 | AgentExecutorRegistration, ...
        ] = (),
        *,
        receipt_gate: ProviderProbeReceiptGate | None = None,
        reprobe_exempt_workflow_revisions: Callable[
            [], frozenset[WorkflowRevisionHash]
        ] = lambda: frozenset(),
    ) -> None:
        """Build the registry, optionally armed with its receipt gate.

        `receipt_gate` is the registry's one safety switch: omitted, every
        caller keeps today's factory-and-capability answer exactly, which is
        what every registry outside a served deployment still wants. Wired
        (`adapters/dbos/runtime.py`'s single production construction site
        always wires it), `is_startable` additionally requires live proof for
        the exact configuration asked about. `reprobe_exempt_workflow_revisions`
        is asked fresh on every call rather than resolved once here, because
        the deployment's admitted canary revisions can change while this
        registry keeps serving; its structural default, an empty set, exempts
        nothing -- there is no flag to leave off by accident, only hashes to
        list.
        """

        self._receipt_gate = receipt_gate
        self._reprobe_exempt_workflow_revisions = reprobe_exempt_workflow_revisions
        factories = tuple(
            registration.factory
            if isinstance(registration, AgentExecutorRegistration)
            else registration
            for registration in registrations
        )
        object_identities = tuple(
            id(factory) for factory in factories if factory is not None
        )
        if len(set(object_identities)) != len(object_identities):
            raise ValueError("agent executor registry factory objects must be unique")
        captured_registrations = tuple(
            registration
            if isinstance(registration, AgentExecutorRegistration)
            else AgentExecutorRegistration.startable(registration)
            for registration in registrations
        )
        captured = tuple(
            AgentExecutorRegistryEntry(
                registration.manifest_entry,
                registration.factory,
            )
            for registration in captured_registrations
        )
        if any(
            not all(
                isinstance(capability, AgentExecutionCapability)
                for capability in entry.manifest_entry.declared_capabilities
            )
            for entry in captured
        ):
            raise TypeError("agent executor capabilities must use their typed contract")
        if any(
            not (
                entry.manifest_entry.declared_capabilities
                & UNATTENDED_AGENT_EXECUTION_CAPABILITIES
            )
            for entry in captured
        ):
            raise ValueError(
                "every agent executor must declare an unattended capability"
            )
        ordered = tuple(
            sorted(
                captured,
                key=lambda entry: (
                    entry.key.provider_id.value.encode("ascii"),
                    entry.key.executor_revision.value.encode("utf-8"),
                ),
            )
        )
        keys = tuple(entry.key for entry in ordered)
        if len(set(keys)) != len(keys):
            raise ValueError("agent executor registry keys must be unique")
        self._entries = ordered
        self._by_key = dict(zip(keys, ordered, strict=True))

    @property
    def entries(self) -> tuple[AgentExecutorRegistryEntry, ...]:
        return self._entries

    @property
    def manifest(self) -> tuple[AgentExecutorManifestEntry, ...]:
        return tuple(entry.manifest_entry for entry in self._entries)

    @property
    def keys(self) -> frozenset[AgentExecutorKey]:
        return frozenset(self._by_key)

    def contains(self, key: AgentExecutorKey) -> bool:
        return key in self._by_key

    def declared_capabilities(
        self, key: AgentExecutorKey
    ) -> frozenset[AgentExecutionCapability]:
        return self._by_key[key].manifest_entry.declared_capabilities

    def carrier(self, key: AgentExecutorKey) -> AgentExecutorCarrier:
        return self._by_key[key].manifest_entry.carrier

    def workspace_file_tools(self, key: AgentExecutorKey) -> WorkspaceFileTools:
        """Whether this executor's invocation reaches the attempt's own files."""

        return self._by_key[key].manifest_entry.workspace_file_tools

    def is_structurally_startable(
        self, key: AgentExecutorKey, capability: AgentExecutionCapability
    ) -> bool:
        """Whether a factory is registered, available, and declares this capability.

        Asks nothing about live evidence -- no configuration, no receipt, no
        clock. This is the *only* question a reprobe exemption may waive past
        (`resolve_start_bindings`) and the only one a canary discovery may
        ask: an executor the operator never registered, or marked
        `unavailable` (a declared version mismatch, say), is not something any
        run -- canary or ordinary -- could ever produce evidence for, so no
        exemption and no receipt changes this answer.
        """
        entry = self._by_key.get(key)
        return (
            entry is not None
            and entry.factory is not None
            and capability in entry.manifest_entry.declared_capabilities
        )

    def is_startable(
        self,
        key: AgentExecutorKey,
        capability: AgentExecutionCapability,
        configuration_hash: AgentConfigurationRevisionHash,
    ) -> bool:
        """Whether this exact configuration may start now.

        `configuration_hash` names the exact `AgentConfigurationRevision` this
        answer is about -- several configurations can share one executor
        revision, so a caller with proof state to consult (a provider probe
        receipt is keyed by configuration, not executor) needs the answer at
        this grain. `is_structurally_startable` always runs first and alone
        can already refuse; only once it holds does an armed registry go on to
        ask its receipt gate whether live evidence for this exact
        configuration is still trustworthy. A registry built without a
        receipt gate never asks that second question at all -- this is the
        full, ordinary-work answer; a caller that must waive only the
        evidence half (the reprobe exemption) asks `is_structurally_startable`
        directly instead.
        """
        return self.is_structurally_startable(
            key, capability
        ) and self.has_valid_receipt(configuration_hash)

    def has_valid_receipt(
        self, configuration_hash: AgentConfigurationRevisionHash
    ) -> bool:
        """Whether live evidence alone would let this exact configuration start.

        Asks only the receipt gate -- not structural availability, which
        `is_structurally_startable` already answers on its own. A registry
        built without a receipt gate always answers True, matching
        `is_startable`'s own fallback when live evidence is not required.
        """
        if self._receipt_gate is None:
            return True
        return self._receipt_gate.is_proven(configuration_hash)

    def latest_receipt(
        self, configuration_hash: AgentConfigurationRevisionHash
    ) -> ProviderProbeReceipt | None:
        """The most recent live evidence recorded for this exact configuration.

        Reads straight through the same receipt gate `has_valid_receipt`
        already consults -- no separate store, no new boundary. A registry
        built without a receipt gate has no evidence to read and answers
        `None`, matching `has_valid_receipt`'s own unarmed fallback.
        """
        if self._receipt_gate is None:
            return None
        return self._receipt_gate.reads.receipt_for(configuration_hash)

    def reprobe_exempt(self, workflow_hash: WorkflowRevisionHash) -> bool:
        """Whether starting this exact workflow needs no receipt evidence yet.

        The one reprobe exemption: a fresh canary run of a currently admitted
        `provider-canary-*` workflow is what produces the receipt a normal
        start would otherwise require, so refusing it too would strand the
        deployment with no way to ever prove itself again. Membership is
        judged by `WorkflowRevisionHash` alone, never by a name or a flag --
        the admitted set is asked fresh, and an empty or unresolved set
        exempts nothing, structurally, because there is no hash it could ever
        equal.
        """
        if not isinstance(workflow_hash, WorkflowRevisionHash):
            raise TypeError("a reprobe exemption is asked about a typed workflow hash")
        return workflow_hash in self._reprobe_exempt_workflow_revisions()
