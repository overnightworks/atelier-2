from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum

from atelier2.contracts.agent_transcripts import AttemptTranscript
from atelier2.contracts.artifacts import MAXIMUM_ARTIFACT_BYTES
from atelier2.contracts.executions import NodeExecutionId
from atelier2.contracts.hashing import Sha256Hash, frame
from atelier2.contracts.runs import FIRST_ROUND_ORDINAL, RunId, WorkflowRevisionHash
from atelier2.contracts.when import RecordedAt

MAXIMUM_PROVIDER_PROBE_PROBLEM_CODE_BYTES = 128
PROVIDER_PROBE_TOKEN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
"""Owned here, one layer below `contracts/provider_probe_receipts.py`, which
already imports this module for `AgentConfigurationRevisionHash` -- importing
a receipt type back here would close that cycle. `provider_probe_receipts.py`
re-exports `ProviderProbeProblemCode` for its own `ProviderProbeVectorId`,
which shares the same bounded-token shape."""


@dataclass(frozen=True, slots=True)
class ProviderProbeProblemCode:
    """A bounded classification of a provider probe's failure.

    Never provider output or diagnostics -- moved here from
    `contracts/provider_probe_receipts.py` so `ProviderProbeFailure` below can
    name it without a cycle; that module still owns the receipt shape itself
    and re-exports this name for its own callers.
    """

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str):
            raise TypeError("a provider probe problem code must be text")
        if (
            PROVIDER_PROBE_TOKEN.fullmatch(self.value) is None
            or len(self.value.encode("ascii"))
            > MAXIMUM_PROVIDER_PROBE_PROBLEM_CODE_BYTES
        ):
            raise ValueError(
                "a provider probe problem code must be a bounded lowercase ASCII token"
            )


MAXIMUM_AGENT_FIELD_CHARACTERS = 1_024
MAXIMUM_AGENT_OUTPUT_BYTES_V2 = 49_152
# What a process accepts as stdin (or the job file Grok reads). Still a separate
# decision from the durable answer bound above, which it must not be derived
# from (#88) -- an agent that may be handed a large brief is not thereby allowed
# to answer with one.
#
# It is the artifact bound because a job is the instruction plus the material the
# node reads, and the largest single piece of material is an artifact: a smaller
# number here would admit an order at the start door and then make it
# unreachable by the agent it was written for, which is the wall a full
# pull-request diff hit. What a given provider accepts is a narrower, separate
# limit each invocation still declares at the process port.
MAXIMUM_AGENT_PROCESS_INPUT_BYTES = MAXIMUM_ARTIFACT_BYTES
# The ceiling every invocation's declared stdout frame must fit inside, and
# deliberately nobody's own frame: each provider states its exact number at the
# process port, derived from the wire format that produces it. That separation
# is the point. While this constant *was* one provider's measurement, raising it
# for a new operation would silently have widened what every other provider's
# process may write, and a frame nobody re-measured is a frame nobody knows.
#
# It stands above every frame the repository declares. The largest is the Claude
# subscription adapter's transcript-bearing stream, which carries a whole
# attempt's steps ahead of the same final envelope the envelope-only operation
# produced. This leaves room above that rather than tracking it exactly, so a
# provider measuring a slightly wider frame moves its own line and not the port's.
MAXIMUM_AGENT_PROCESS_STANDARD_OUTPUT_BYTES = 32 * MAXIMUM_AGENT_OUTPUT_BYTES_V2
MAXIMUM_SIGNED_INT64 = 2**63 - 1
MAXIMUM_PROVIDER_ID_CHARACTERS = 64
# The slug's own width, so the pattern, the store's CHECK and the wire cannot
# drift apart: one leading letter and the rest of the allowed characters.
PROVIDER_ID_PATTERN = rf"^[a-z][a-z0-9._-]{{0,{MAXIMUM_PROVIDER_ID_CHARACTERS - 1}}}$"
_PROVIDER_ID = re.compile(PROVIDER_ID_PATTERN)


def _require_bounded_text(value: str, owner: str) -> None:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= MAXIMUM_AGENT_FIELD_CHARACTERS
    ):
        raise ValueError(
            f"{owner} must contain 1..{MAXIMUM_AGENT_FIELD_CHARACTERS} exact characters"
        )


@dataclass(frozen=True)
class AgentRole:
    value: str

    def __post_init__(self) -> None:
        _require_bounded_text(self.value, "agent role")


@dataclass(frozen=True)
class ProviderId:
    value: str

    def __post_init__(self) -> None:
        if _PROVIDER_ID.fullmatch(self.value) is None:
            raise ValueError("provider id must be a lowercase ASCII provider slug")


class AuthMode(StrEnum):
    SUBSCRIPTION = "subscription"
    API_KEY = "api_key"


MAXIMUM_AUTH_REFERENCE_BYTES = 128


@dataclass(frozen=True)
class AuthReference:
    """A provider's own non-secret pointer to a resolved authorization.

    Never the credential value and never a host path -- both sides of the
    wire check this exact typed form and refuse anything else. Each provider
    owns its own derivation into this shape.
    """

    value: str

    def __post_init__(self) -> None:
        encoded = self.value.encode("ascii")
        if not 1 <= len(encoded) <= MAXIMUM_AUTH_REFERENCE_BYTES:
            raise ValueError(
                f"auth reference must be 1..{MAXIMUM_AUTH_REFERENCE_BYTES} ASCII bytes"
            )


class AgentExecutionCapability(StrEnum):
    HEADLESS = "headless"
    HEADLESS_WITH_TOOLS = "headless_with_tools"
    INTERACTIVE = "interactive"


UNATTENDED_AGENT_EXECUTION_CAPABILITIES = frozenset(
    {
        AgentExecutionCapability.HEADLESS,
        AgentExecutionCapability.HEADLESS_WITH_TOOLS,
    }
)
"""Every capability an attempt can ask for with no operator at a terminal.

The durable runtime drives every attempt itself and stands at no terminal, so an
executor declaring only `INTERACTIVE` would name one no run could ever reach.
The two members differ in what the invocation may touch, not in who drives it:
`HEADLESS` is one text-in/text-out call, `HEADLESS_WITH_TOOLS` is a call whose
process may also use tools the bound executor grants it. Which tools those are
is the bound executor's declared contract, not this capability's: one executor
grants workspace tools where the attempt stands, another grants the product's
own API doors, and the capability only says that the node asked for a
tool-bearing call at all. The narrower question -- which executor, and therefore
which tools -- is answered by the binding's executor revision, which the
capability never selects.
"""


class AgentConfigurationRevisionFormatVersion(IntEnum):
    V1 = 1
    V2 = 2


class AuthProfileRevisionHash(Sha256Hash):
    """Identity of one public, secret-free authentication profile revision."""


class AgentConfigurationRevisionHash(Sha256Hash):
    """Identity of one immutable model/auth/executor selection."""


class AgentBindingSetHash(Sha256Hash):
    """Identity of the complete role matrix frozen into one V2 run."""


@dataclass(frozen=True)
class AuthProfileRevision:
    profile_id: str
    revision_number: int
    provider_id: ProviderId
    auth_mode: AuthMode
    revision_hash: AuthProfileRevisionHash = field(init=False)

    def __post_init__(self) -> None:
        _require_bounded_text(self.profile_id, "auth profile id")
        if (
            type(self.revision_number) is not int
            or not 1 <= self.revision_number <= MAXIMUM_SIGNED_INT64
        ):
            raise ValueError(
                "auth profile revision number must be a positive signed int64"
            )
        if not isinstance(self.provider_id, ProviderId) or not isinstance(
            self.auth_mode, AuthMode
        ):
            raise TypeError(
                "auth profile provider and mode must use their typed contracts"
            )
        object.__setattr__(
            self,
            "revision_hash",
            AuthProfileRevisionHash.of(
                frame(
                    "auth-profile-revision/v1",
                    self.profile_id.encode("utf-8"),
                    struct.pack(">Q", self.revision_number),
                    self.provider_id.value.encode("ascii"),
                    self.auth_mode.value.encode("ascii"),
                )
            ),
        )


@dataclass(frozen=True)
class AgentConfigurationRevision:
    model: str
    auth_profile_revision_hash: AuthProfileRevisionHash
    executor_revision: AgentExecutorRevision
    requested_capability: AgentExecutionCapability
    revision_format_version: AgentConfigurationRevisionFormatVersion
    revision_hash: AgentConfigurationRevisionHash = field(init=False)

    def __post_init__(self) -> None:
        _require_bounded_text(self.model, "agent model")
        if not isinstance(self.auth_profile_revision_hash, AuthProfileRevisionHash):
            raise TypeError("agent configuration auth hash must be typed")
        if not isinstance(self.executor_revision, AgentExecutorRevision):
            raise TypeError("agent configuration executor revision must be typed")
        _require_bounded_text(self.executor_revision.value, "agent executor revision")
        if not isinstance(self.requested_capability, AgentExecutionCapability):
            raise TypeError("agent configuration capability must be typed")
        if not isinstance(
            self.revision_format_version, AgentConfigurationRevisionFormatVersion
        ):
            raise TypeError("agent configuration format version must be typed")
        if (
            self.revision_format_version is AgentConfigurationRevisionFormatVersion.V1
            and self.requested_capability is not AgentExecutionCapability.HEADLESS
        ):
            raise ValueError("legacy agent configurations require headless capability")
        if self.revision_format_version is AgentConfigurationRevisionFormatVersion.V1:
            framed = frame(
                "agent-configuration-revision/v1",
                self.model.encode("utf-8"),
                self.auth_profile_revision_hash.value.encode("ascii"),
                self.executor_revision.value.encode("utf-8"),
            )
        else:
            framed = frame(
                "agent-configuration-revision/v2",
                self.model.encode("utf-8"),
                self.auth_profile_revision_hash.value.encode("ascii"),
                self.executor_revision.value.encode("utf-8"),
                self.requested_capability.value.encode("ascii"),
            )
        object.__setattr__(
            self,
            "revision_hash",
            AgentConfigurationRevisionHash.of(framed),
        )


class AgentConfigurationNotStartableReason(StrEnum):
    """Why a listed configuration cannot start right now, named for the wire.

    One canonical vocabulary: `api/wire/resources.py`'s `not_startable_reason`
    Literal and this enum's values read identically, because a caller reads
    the reason from the wire and this is the one place it is decided.
    """

    AGENT_EXECUTOR_BINDING_UNAVAILABLE = "agent-executor-binding-unavailable"
    MODEL_NOT_REGISTERED = "model-not-registered"
    PROVIDER_PROBE_RECEIPT_MISSING = "provider-probe-receipt-missing"
    PROVIDER_PROBE_FAILED = "provider-probe-failed"


@dataclass(frozen=True)
class ProviderProbeFailure:
    """The latest provider probe's own recorded failure for one configuration.

    Carried only when a receipt for this exact configuration exists and its
    own result is a failure -- the honest evidence
    `provider-probe-failed` names, never invented from a merely missing or
    stale receipt (`provider-probe-receipt-missing` stays the answer there).
    """

    problem_code: ProviderProbeProblemCode
    observed_at: RecordedAt

    def __post_init__(self) -> None:
        if not isinstance(self.problem_code, ProviderProbeProblemCode):
            raise TypeError("a provider probe failure names a typed problem code")
        if not isinstance(self.observed_at, RecordedAt):
            raise TypeError("a provider probe failure names a typed recording instant")


@dataclass(frozen=True)
class AgentConfigurationRevisionListItem:
    """A listed immutable configuration with the host's current startability.

    Three independent judgments feed one fixed precedence -- the same order a
    start itself would meet the same three refusals in.
    `structurally_startable` asks only whether a factory is registered,
    available, and declares the capability, with no live evidence asked at
    all. `model_registered` asks the same registry lookup a start's cast
    makes for an explicit override (`cast_unbound_roles`): does the model
    registry still point at this exact configuration hash for its provider
    and model, or has a newer revision superseded it. `has_valid_receipt` is
    the live evidence one provider probe leaves behind. `startable` and
    `not_startable_reason` are computed from these three, never stored
    independently, so the two can never disagree. `probe_failure` is the raw
    receipt evidence, kept even when an earlier judgment in the precedence
    (a superseded model, say) already names the reason before the receipt is
    ever asked about. `probe_failure_evidence` is what the wire is allowed to
    carry: the failure only when `not_startable_reason` itself names
    `provider-probe-failed`, `None` otherwise, so the projection never has to
    re-decide what the reason already decided.
    """

    revision: AgentConfigurationRevision
    auth_profile: AuthProfileRevision
    structurally_startable: bool
    model_registered: bool
    has_valid_receipt: bool
    probe_failure: ProviderProbeFailure | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("structurally_startable", self.structurally_startable),
            ("model_registered", self.model_registered),
            ("has_valid_receipt", self.has_valid_receipt),
        ):
            if type(value) is not bool:
                raise TypeError(f"agent configuration {name} must be a bool")
        if self.probe_failure is not None and not isinstance(
            self.probe_failure, ProviderProbeFailure
        ):
            raise TypeError("agent configuration probe failure must be typed")
        if self.has_valid_receipt and self.probe_failure is not None:
            raise ValueError(
                "agent configuration cannot carry a probe failure beside a "
                "valid receipt"
            )

    @property
    def not_startable_reason(self) -> AgentConfigurationNotStartableReason | None:
        if not self.structurally_startable:
            return (
                AgentConfigurationNotStartableReason.AGENT_EXECUTOR_BINDING_UNAVAILABLE
            )
        if not self.model_registered:
            return AgentConfigurationNotStartableReason.MODEL_NOT_REGISTERED
        if not self.has_valid_receipt:
            return (
                AgentConfigurationNotStartableReason.PROVIDER_PROBE_FAILED
                if self.probe_failure is not None
                else AgentConfigurationNotStartableReason.PROVIDER_PROBE_RECEIPT_MISSING
            )
        return None

    @property
    def startable(self) -> bool:
        return self.not_startable_reason is None

    @property
    def probe_failure_evidence(self) -> ProviderProbeFailure | None:
        if (
            self.not_startable_reason
            is not AgentConfigurationNotStartableReason.PROVIDER_PROBE_FAILED
        ):
            return None
        return self.probe_failure


@dataclass(frozen=True)
class AgentBinding:
    role: AgentRole
    agent_configuration_revision_hash: AgentConfigurationRevisionHash


@dataclass(frozen=True)
class AgentBindingSet:
    bindings: tuple[AgentBinding, ...]
    binding_set_hash: AgentBindingSetHash = field(init=False)

    def __post_init__(self) -> None:
        ordered = tuple(
            sorted(
                self.bindings, key=lambda binding: binding.role.value.encode("utf-8")
            )
        )
        if len({binding.role for binding in ordered}) != len(ordered):
            raise ValueError("agent binding roles must be unique")
        object.__setattr__(self, "bindings", ordered)
        object.__setattr__(
            self,
            "binding_set_hash",
            AgentBindingSetHash.of(
                frame(
                    "run-agent-bindings/v1",
                    *(
                        frame(
                            "run-agent-binding/v1",
                            binding.role.value.encode("utf-8"),
                            binding.agent_configuration_revision_hash.value.encode(
                                "ascii"
                            ),
                        )
                        for binding in ordered
                    ),
                )
            ),
        )


@dataclass(frozen=True)
class ResolvedAgentBinding:
    role: AgentRole
    configuration: AgentConfigurationRevision
    auth_profile: AuthProfileRevision

    def __post_init__(self) -> None:
        if (
            self.configuration.auth_profile_revision_hash
            != self.auth_profile.revision_hash
        ):
            raise ValueError(
                "resolved agent binding auth revision differs from configuration"
            )


class AgentExecutionRequestHash(Sha256Hash):
    """The immutable fingerprint of one exact logical agent invocation."""


class AgentOutputHash(Sha256Hash):
    """The immutable fingerprint of the exact bytes one agent returned."""


class AgentReceiptHash(Sha256Hash):
    """The immutable fingerprint of one successful agent execution receipt."""


@dataclass(frozen=True)
class AgentExecutorIdentifier:
    value: str

    def __post_init__(self) -> None:
        if self.value == "":
            raise ValueError(f"{type(self).__name__} must be a nonempty string")


class AgentExecutorRevision(AgentExecutorIdentifier):
    """The immutable revision of the executor adapter."""


class AgentExecutorOperationalIdentity(AgentExecutorIdentifier):
    """The stable, non-secret identity of one executor operation."""


@dataclass(frozen=True)
class AgentExecutionResult:
    """What one execution answered, and what it did to get there where that is known.

    The transcript is optional because it is a fact about the provider's wire
    format, not about this contract: an executor whose CLI publishes a
    structured stream decodes one, and an executor whose CLI publishes only a
    final answer leaves it `None` rather than inventing a shape. It is already
    bounded and redacted when it arrives -- `AttemptTranscript` is the only way
    to make one -- so the terminal write keeps it without judging it again.
    """

    output_bytes: bytes
    transcript: AttemptTranscript | None = None


@dataclass(frozen=True)
class AgentExecutionRequestV2:
    node_execution_id: NodeExecutionId
    run_id: RunId
    workflow_revision_hash: WorkflowRevisionHash
    node_id: str
    resolved_binding: ResolvedAgentBinding
    executor_operational_identity: AgentExecutorOperationalIdentity
    job_bytes: bytes
    declared_output_schema_bytes: bytes | None = None
    round_ordinal: int = FIRST_ROUND_ORDINAL
    maximum_assistant_turns: int | None = None
    request_hash: AgentExecutionRequestHash = field(init=False)

    def __post_init__(self) -> None:
        _require_bounded_text(self.node_id, "agent request node id")
        if not self.job_bytes:
            raise ValueError("agent request job bytes must be nonempty")
        if len(self.job_bytes) > MAXIMUM_AGENT_PROCESS_INPUT_BYTES:
            raise ValueError(
                f"agent request job bytes exceed {MAXIMUM_AGENT_PROCESS_INPUT_BYTES} bytes"
            )
        if self.declared_output_schema_bytes is not None:
            if type(self.declared_output_schema_bytes) is not bytes:
                raise TypeError("declared output schema bytes must be bytes")
            if not self.declared_output_schema_bytes:
                raise ValueError("declared output schema bytes must be nonempty")
        if self.maximum_assistant_turns is not None:
            if type(self.maximum_assistant_turns) is not int:
                raise TypeError("maximum assistant turns must be an integer")
            if not 1 <= self.maximum_assistant_turns <= MAXIMUM_SIGNED_INT64:
                raise ValueError(
                    "maximum assistant turns must be a positive signed int64"
                )
        expected_execution = NodeExecutionId.for_node(
            self.run_id, self.workflow_revision_hash, self.node_id, self.round_ordinal
        )
        if self.node_execution_id != expected_execution:
            raise ValueError(
                "agent request execution identity differs from its binding"
            )
        if not isinstance(
            self.executor_operational_identity, AgentExecutorOperationalIdentity
        ):
            raise TypeError("executor operational identity must be typed")
        _require_bounded_text(
            self.executor_operational_identity.value,
            "executor operational identity",
        )
        binding = self.resolved_binding
        auth = binding.auth_profile
        configuration = binding.configuration
        object.__setattr__(
            self,
            "request_hash",
            AgentExecutionRequestHash.of(
                frame(
                    "agent-execution-request/v2",
                    self.node_execution_id.value.encode("ascii"),
                    self.run_id.value.encode("utf-8"),
                    self.workflow_revision_hash.value.encode("ascii"),
                    self.node_id.encode("utf-8"),
                    binding.role.value.encode("utf-8"),
                    configuration.revision_hash.value.encode("ascii"),
                    auth.revision_hash.value.encode("ascii"),
                    auth.profile_id.encode("utf-8"),
                    struct.pack(">Q", auth.revision_number),
                    auth.provider_id.value.encode("ascii"),
                    auth.auth_mode.value.encode("ascii"),
                    configuration.model.encode("utf-8"),
                    configuration.executor_revision.value.encode("utf-8"),
                    self.executor_operational_identity.value.encode("utf-8"),
                    self.job_bytes,
                )
            ),
        )


class AgentOutputLimitExceeded(ValueError):
    """A V2 executor returned bytes that cannot be durably projected."""


@dataclass(frozen=True)
class AgentReceiptBody:
    """Every value one V2 receipt hash commits to, in the order it frames them.

    A receipt is this body sealed with its hash; a verifier holding only the
    presented fields folds the same fingerprint out of the body alone.
    """

    request_hash: AgentExecutionRequestHash
    node_execution_id: NodeExecutionId
    run_id: RunId
    workflow_revision_hash: WorkflowRevisionHash
    node_id: str
    role: AgentRole
    binding_set_hash: AgentBindingSetHash
    agent_configuration_revision_hash: AgentConfigurationRevisionHash
    auth_profile_revision_hash: AuthProfileRevisionHash
    profile_id: str
    revision_number: int
    provider_id: ProviderId
    auth_mode: AuthMode
    model: str
    executor_revision: AgentExecutorRevision
    executor_operational_identity: AgentExecutorOperationalIdentity
    output_bytes: bytes
    output_hash: AgentOutputHash

    def fingerprint(self) -> AgentReceiptHash:
        return AgentReceiptHash.of(
            frame(
                "agent-receipt/v2",
                self.request_hash.value.encode("ascii"),
                self.node_execution_id.value.encode("ascii"),
                self.run_id.value.encode("utf-8"),
                self.workflow_revision_hash.value.encode("ascii"),
                self.node_id.encode("utf-8"),
                self.role.value.encode("utf-8"),
                self.binding_set_hash.value.encode("ascii"),
                self.agent_configuration_revision_hash.value.encode("ascii"),
                self.auth_profile_revision_hash.value.encode("ascii"),
                self.profile_id.encode("utf-8"),
                struct.pack(">Q", self.revision_number),
                self.provider_id.value.encode("ascii"),
                self.auth_mode.value.encode("ascii"),
                self.model.encode("utf-8"),
                self.executor_revision.value.encode("utf-8"),
                self.executor_operational_identity.value.encode("utf-8"),
                self.output_bytes,
                self.output_hash.value.encode("ascii"),
            )
        )


@dataclass(frozen=True)
class AgentReceiptV2(AgentReceiptBody):
    receipt_hash: AgentReceiptHash
    round_ordinal: int = FIRST_ROUND_ORDINAL

    def __post_init__(self) -> None:
        _require_bounded_text(self.node_id, "agent receipt node id")
        _require_bounded_text(self.profile_id, "auth profile id")
        _require_bounded_text(self.model, "agent model")
        if self.node_execution_id != NodeExecutionId.for_node(
            self.run_id, self.workflow_revision_hash, self.node_id, self.round_ordinal
        ):
            raise ValueError(
                "agent receipt execution identity differs from its binding"
            )
        if len(self.output_bytes) > MAXIMUM_AGENT_OUTPUT_BYTES_V2:
            raise AgentOutputLimitExceeded(
                f"agent output exceeds {MAXIMUM_AGENT_OUTPUT_BYTES_V2} bytes"
            )
        if self.output_hash != AgentOutputHash.of(self.output_bytes):
            raise ValueError("agent receipt output hash differs from its bytes")
        if self.receipt_hash != self.fingerprint():
            raise ValueError("agent receipt hash differs from its exact binding")

    @classmethod
    def for_execution(
        cls,
        request: AgentExecutionRequestV2,
        binding_set_hash: AgentBindingSetHash,
        result: AgentExecutionResult,
    ) -> AgentReceiptV2:
        if len(result.output_bytes) > MAXIMUM_AGENT_OUTPUT_BYTES_V2:
            raise AgentOutputLimitExceeded(
                f"agent output exceeds {MAXIMUM_AGENT_OUTPUT_BYTES_V2} bytes"
            )
        binding = request.resolved_binding
        configuration = binding.configuration
        auth = binding.auth_profile
        body = AgentReceiptBody(
            request.request_hash,
            request.node_execution_id,
            request.run_id,
            request.workflow_revision_hash,
            request.node_id,
            binding.role,
            binding_set_hash,
            configuration.revision_hash,
            auth.revision_hash,
            auth.profile_id,
            auth.revision_number,
            auth.provider_id,
            auth.auth_mode,
            configuration.model,
            configuration.executor_revision,
            request.executor_operational_identity,
            result.output_bytes,
            AgentOutputHash.of(result.output_bytes),
        )
        return cls(
            body.request_hash,
            body.node_execution_id,
            body.run_id,
            body.workflow_revision_hash,
            body.node_id,
            body.role,
            body.binding_set_hash,
            body.agent_configuration_revision_hash,
            body.auth_profile_revision_hash,
            body.profile_id,
            body.revision_number,
            body.provider_id,
            body.auth_mode,
            body.model,
            body.executor_revision,
            body.executor_operational_identity,
            body.output_bytes,
            body.output_hash,
            body.fingerprint(),
            request.round_ordinal,
        )
