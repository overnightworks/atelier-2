"""The Agent Client Protocol as one conversation, in its standard vocabulary only.

**Why a provider-neutral core.** Every duplex vector this product takes speaks
the same published protocol: a handshake, a session, one prompt, and a stream
of updates in which the agent asks its client for a permission or a file. What
differs per vendor is the vocabulary inside those messages, not the protocol
around them, so the lifecycle, the correlation and the bounds live here once
and a vendor's own spelling reaches them through `AcpVocabulary`, which answers
in typed values or refuses to name what it read. An unrepresentable request that
would have reached a file or a shell is refused closed: an extension cannot
widen what a provider may do by being unreadable.

**Why nothing here raises.** This runs inside the loop that owns the child
process, where a raised exception is a state nobody can answer for. Every
refused frame has an answer instead: a JSON-RPC error where one is owed,
bounded transcript evidence, a latched terminal reading, or an orderly close.
An exchange that stopped being one ends as `ProviderTerminalReason.PROTOCOL_FAULT`
and never in a provider's own word: only a `stopReason` the agent itself spelled
reaches `CANCELLED_BY_PROVIDER`, and which promise broke is kept as evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from atelier2.adapters.acp_vocabulary import (
    AcpToolCallStatus,
    AcpVocabulary,
    AssistantText,
    NothingToRecord,
    StandardAcpVocabulary,
    ToolCallAnnounced,
    Unrepresentable,
    cut_to_field,
    refused_permission_evidence,
)
from atelier2.adapters.newline_json_rpc import (
    INTERNAL_ERROR_CODE,
    INVALID_PARAMS_CODE,
    INVALID_REQUEST_CODE,
    METHOD_NOT_FOUND_CODE,
    NO_PARAMS,
    PARSE_ERROR_CODE,
    IncomingFrame,
    JsonObject,
    JsonRpcAnswer,
    JsonRpcError,
    JsonRpcFailure,
    JsonRpcFault,
    JsonRpcId,
    JsonRpcNotification,
    JsonRpcProtocolFault,
    JsonRpcRequest,
    JsonRpcResponse,
    NewlineJsonRpc,
    OutgoingMessage,
    UnsendableFrame,
    rendered,
)
from atelier2.contracts.agent_attempts import AgentAttemptId
from atelier2.contracts.agent_permissions import (
    MINIMUM_PERMISSION_CALL_ORDINAL,
    PermissionCorrelationId,
    PermissionDecision,
    PermissionRequest,
)
from atelier2.contracts.agent_transcripts import (
    MAXIMUM_TRANSCRIPT_STEP_CHARACTERS,
    AssistantTurn,
    ToolCalled,
    ToolReturned,
    UnrecognisedProviderOutput,
)
from atelier2.contracts.agents import MAXIMUM_AGENT_FIELD_CHARACTERS
from atelier2.ports.provider_conversations import (
    ProviderCancellationCause,
    ProviderCancellationFrame,
    ProviderCancellationRequest,
    ProviderConversationAction,
    ProviderConversationBounds,
    ProviderConversationClosing,
    ProviderConversationComplete,
    ProviderConversationEnding,
    ProviderFilesystemEffect,
    ProviderFilesystemRefusal,
    ProviderFilesystemReply,
    ProviderFilesystemRequest,
    ProviderFilesystemRequestId,
    ProviderSessionEvent,
    ProviderStandardInput,
    ProviderTerminalOutcome,
    ProviderTerminalReason,
)

type Actions = tuple[ProviderConversationAction, ...]
type Steps = tuple[ProviderSessionEvent, ...]

ACP_PROTOCOL_VERSION = 1
MAXIMUM_UNRECOGNISED_UPDATE_STEPS = 32
"""How much of a vocabulary this core cannot read it keeps as evidence.

Past this many, a provider whose whole stream is unreadable would spend the
transcript on repetitions of one finding instead of on the steps around it.
"""


class AcpMethod(StrEnum):
    """Every method this conversation sends or answers, and no other."""

    INITIALIZE = "initialize"
    SESSION_NEW = "session/new"
    SESSION_PROMPT = "session/prompt"
    SESSION_CANCEL = "session/cancel"
    SESSION_UPDATE = "session/update"
    REQUEST_PERMISSION = "session/request_permission"
    READ_TEXT_FILE = "fs/read_text_file"
    WRITE_TEXT_FILE = "fs/write_text_file"


class AcpStopReason(StrEnum):
    """The stop reasons that mean a turn simply ended.

    `cancelled` and `refusal` are deliberately absent: a provider that stopped
    itself is read as one, and its own word is data on the outcome.
    """

    END_TURN = "end_turn"
    MAX_TOKENS = "max_tokens"
    MAX_TURN_REQUESTS = "max_turn_requests"


class AcpSelectableOption(StrEnum):
    """The only two permission options this client will ever select.

    A persistent option answers questions nobody has asked yet, under a policy
    revision bound to this attempt alone.
    """

    ALLOW_ONCE = "allow_once"
    REJECT_ONCE = "reject_once"


class AcpConversationFault(StrEnum):
    """Which of the protocol's promises broke, where one did."""

    HANDSHAKE_REFUSED = "handshake-refused"
    NO_SESSION = "no-session"
    NO_STOP_REASON = "no-stop-reason"
    NO_TERMINAL_ANSWER = "no-terminal-answer"
    UNSENDABLE_FRAME = "unsendable-frame"
    LOST_FRAMING = "lost-framing"
    UNEXPECTED_ANSWER = "unexpected-answer"
    UNREADABLE_ANSWER = "unreadable-answer"
    FOREIGN_SESSION = "foreign-session"


_ANSWERED_FRAME_FAULTS = {
    JsonRpcFault.UNPARSEABLE: PARSE_ERROR_CODE,
    JsonRpcFault.NOT_A_MESSAGE: INVALID_REQUEST_CODE,
}
"""A frame that is still framed is answered where the protocol owes an answer."""

_FATAL_FRAME_FAULTS = {
    JsonRpcFault.OVERSIZE_FRAME: AcpConversationFault.LOST_FRAMING,
    JsonRpcFault.UNEXPECTED_RESPONSE: AcpConversationFault.UNEXPECTED_ANSWER,
    JsonRpcFault.MALFORMED_RESPONSE: AcpConversationFault.UNREADABLE_ANSWER,
}
"""A frame nobody can answer is the exchange itself having stopped being one."""

_EFFECT_OF_FILE_METHOD: dict[str, ProviderFilesystemEffect] = {
    AcpMethod.READ_TEXT_FILE: ProviderFilesystemEffect.READ,
    AcpMethod.WRITE_TEXT_FILE: ProviderFilesystemEffect.WRITE,
}

_STOP_REASONS_THAT_ONLY_END_A_TURN = frozenset(AcpStopReason)

_SESSION_BOUND_REQUESTS = frozenset(
    {
        AcpMethod.REQUEST_PERMISSION,
        AcpMethod.READ_TEXT_FILE,
        AcpMethod.WRITE_TEXT_FILE,
    }
)
"""Everything the agent may ask this client, each inside one named session."""

_REASON_OF_ENDING = {
    ProviderConversationEnding.CANCELLED_BY_OPERATOR: (
        ProviderTerminalReason.CANCELLED_BY_OPERATOR
    ),
    ProviderConversationEnding.CANCELLED_FOR_POLICY: (
        ProviderTerminalReason.POLICY_REFUSED
    ),
    ProviderConversationEnding.CANCELLED_FOR_BUDGET: (
        ProviderTerminalReason.BUDGET_EXHAUSTED
    ),
}

_CLIENT_HANDSHAKE: JsonObject = {
    "protocolVersion": ACP_PROTOCOL_VERSION,
    "clientCapabilities": {
        "fs": {"readTextFile": True, "writeTextFile": True},
        "terminal": False,
    },
}
_REFUSED_FILE_MESSAGE = "this client refused the file"

_UNANSWERABLE_FILE_MESSAGE = "this client cannot answer the file as text"
_OVERSIZE_FILE_MESSAGE = "this client cannot answer a file that wide"
_UNKNOWN_METHOD_MESSAGE = "this client does not serve that method"
_UNREADABLE_FRAME_MESSAGE = "this client could not read that frame"
_UNREADABLE_PARAMS_MESSAGE = "this client could not read that request"
_WRITE_ACKNOWLEDGED: JsonObject = {}
PROTOCOL_FAULT_EVIDENCE = "acp protocol fault: "
"""How a broken promise is named in the one step that keeps it."""


def _refused_file_message(refusal: ProviderFilesystemRefusal, detail: str) -> str:
    """The refusal in the provider's own error frame, named but never located.

    The agent is told why -- its path left the lease, the policy said no -- so
    it can choose another path or stop asking, and an unclassified fault names
    its errno beside the word. Neither says where the lease stands.
    """

    named = f"{_REFUSED_FILE_MESSAGE}: {refusal.value}"
    return f"{named} ({detail})" if detail else named


@dataclass(frozen=True, slots=True)
class _PendingPermission:
    """One question in flight: who asked it, and which options it may be answered with."""

    identifier: JsonRpcId
    allowed: str
    refused: str


@dataclass(frozen=True, slots=True)
class _PendingFile:
    """One file request in flight: who asked it, and what it asked for."""

    identifier: JsonRpcId
    effect: ProviderFilesystemEffect


@dataclass
class AgentClientProtocolConversation:
    """One attempt's ACP session, from its handshake to its terminal reading.

    It opens with `initialize`, takes the session it is given, prompts once and
    reads what comes back until that prompt is answered. Everything it wants
    done it publishes as an action; nothing here writes, decides or opens.
    """

    attempt_id: AgentAttemptId
    prompt: str
    working_directory: Path
    bounds: ProviderConversationBounds
    maximum_tool_calls: int
    vocabulary: AcpVocabulary = field(default_factory=StandardAcpVocabulary)
    _codec: NewlineJsonRpc = field(init=False)
    _said: str = field(default="", init=False)
    _questions: dict[PermissionCorrelationId, _PendingPermission] = field(
        default_factory=dict, init=False
    )
    _files: dict[ProviderFilesystemRequestId, _PendingFile] = field(
        default_factory=dict, init=False
    )
    _session: str = field(default="", init=False)
    _claimed_session: str = field(default="", init=False)
    _tool_calls: dict[str, str] = field(default_factory=dict, init=False)
    _asked_questions: int = field(default=0, init=False)
    _asked_files: int = field(default=0, init=False)
    _unrecognised: int = field(default=0, init=False)
    _local_cause: ProviderTerminalReason | None = field(default=None, init=False)
    _fault: AcpConversationFault | None = field(default=None, init=False)
    _stop_reason: str = field(default="", init=False)
    _answered_prompt: bool = field(default=False, init=False)
    _ended: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if type(self.maximum_tool_calls) is not int or self.maximum_tool_calls < 1:
            raise ValueError("a conversation admits at least one tool call")
        self._codec = NewlineJsonRpc(self.bounds.maximum_incomplete_frame_bytes)

    @property
    def incomplete_frame_bytes(self) -> int:
        """What stands after the last newline, which no frame has completed yet."""

        return len(self._codec.incomplete_frame())

    def open(self) -> Actions:
        """Say the handshake, before this process has said anything."""

        return self._asking(AcpMethod.INITIALIZE, _CLIENT_HANDSHAKE)

    def receive_output(self, chunk: bytes) -> Actions:
        actions: list[ProviderConversationAction] = []
        for frame in self._codec.receive(chunk):
            if self._ended:
                break
            actions.extend(self._read(frame))
        return tuple(actions)

    def input_written(self, written_bytes: int) -> Actions:
        """Nothing this conversation says depends on what the child already has."""

        return ()

    def answer_permission(self, decision: PermissionDecision) -> ProviderStandardInput:
        pending = self._questions.pop(decision.correlation_id)
        chosen = pending.allowed if decision.granted else pending.refused
        if not decision.granted or not chosen:
            self._latch(ProviderTerminalReason.POLICY_REFUSED)
        return self._answering(pending.identifier, _permission_outcome(chosen))

    def answer_filesystem(
        self, reply: ProviderFilesystemReply
    ) -> ProviderStandardInput:
        pending = self._files.pop(reply.request_id)
        if reply.refusal is not None:
            return self._file_refused(
                pending.identifier, _refused_file_message(reply.refusal, reply.detail)
            )
        if pending.effect is ProviderFilesystemEffect.WRITE:
            return self._answering(pending.identifier, _WRITE_ACKNOWLEDGED)
        if len(reply.content) > self.bounds.maximum_reply_bytes:
            return self._file_refused(pending.identifier, _OVERSIZE_FILE_MESSAGE)
        try:
            content = reply.content.decode("utf-8")
        except UnicodeDecodeError:
            return self._file_refused(pending.identifier, _UNANSWERABLE_FILE_MESSAGE)
        return self._answering(pending.identifier, {"content": content})

    def _file_refused(
        self, identifier: JsonRpcId, message: str
    ) -> ProviderStandardInput:
        return self._delivered(JsonRpcError(identifier, INTERNAL_ERROR_CODE, message))

    def finish(self, ending: ProviderConversationEnding) -> ProviderConversationClosing:
        if not self._answered_prompt:
            self._stop_talking(AcpConversationFault.NO_TERMINAL_ANSWER)
        return ProviderConversationClosing(
            self._outcome(ending),
            self._flushed() + self._broken_promise() + self._half_frame(),
        )

    def _read(self, frame: IncomingFrame) -> Actions:
        match frame:
            case JsonRpcNotification(method, params):
                return self._notified(method, params)
            case JsonRpcRequest() as asked:
                return self._questioned(asked)
            case JsonRpcAnswer(method, result):
                return self._answered(method, result)
            case JsonRpcFailure():
                return self._faulted(AcpConversationFault.HANDSHAKE_REFUSED)
            case JsonRpcProtocolFault() as refused:
                return self._refused_frame(refused)

    def _refused_frame(self, refused: JsonRpcProtocolFault) -> Actions:
        code = _ANSWERED_FRAME_FAULTS.get(refused.fault)
        if code is None:
            return self._faulted(_FATAL_FRAME_FAULTS[refused.fault])
        return self._sending(JsonRpcError(refused.id, code, _UNREADABLE_FRAME_MESSAGE))

    def _notified(self, method: str, params: JsonObject) -> Actions:
        if method != AcpMethod.SESSION_UPDATE:
            return ()
        if not self._session:
            return self._claimed(params)
        if self._foreign(params):
            return self._faulted(AcpConversationFault.FOREIGN_SESSION)
        update = params.get("update")
        if not isinstance(update, dict):
            return self._flushed() + self._evidence(rendered(params))
        return self._updated(update)

    def _claimed(self, params: JsonObject) -> Actions:
        """Note which session an update claims while there is still none, and stop.

        A released agent narrates the session it is opening before `session/new`
        is answered, so an early update is not a fault -- but neither is it this
        conversation's story yet: nothing said under a session id nobody has
        confirmed may spend the tool ceiling, name a tool call or become a step.
        The first id one claims is kept and held against the answer.
        """

        claimed_session_id = params.get("sessionId")
        if (
            not self._claimed_session
            and isinstance(claimed_session_id, str)
            and claimed_session_id
        ):
            self._claimed_session = claimed_session_id
        return ()

    def _updated(self, update: JsonObject) -> Actions:
        identifier = update.get("toolCallId")
        named = identifier if isinstance(identifier, str) else ""
        classified = self.vocabulary.classify_update(update)
        if isinstance(classified, AssistantText):
            return self._spoken(classified.text)
        if isinstance(classified, NothingToRecord):
            return ()
        if isinstance(classified, Unrepresentable) or not named:
            return self._flushed() + self._evidence(rendered(update))
        recorded = self._charged(named) + self._flushed()
        if isinstance(classified, ToolCallAnnounced):
            return recorded + self._announced(
                named, classified.title, classified.locations
            )
        return recorded + self._settled(named, classified.status, classified.content)

    def _questioned(self, asked: JsonRpcRequest) -> Actions:
        if asked.method not in _SESSION_BOUND_REQUESTS:
            return self._sending(
                JsonRpcError(asked.id, METHOD_NOT_FOUND_CODE, _UNKNOWN_METHOD_MESSAGE)
            )
        if self._foreign(asked.params):
            return self._faulted(AcpConversationFault.FOREIGN_SESSION)
        if asked.method == AcpMethod.REQUEST_PERMISSION:
            return self._permission_asked(asked)
        return self._file_asked(asked, _EFFECT_OF_FILE_METHOD[asked.method])

    def _foreign(self, params: JsonObject) -> bool:
        """Whether this message belongs to a session other than this client's.

        A request is held to it always: nothing may reach this attempt's
        authority or its workspace under a session nobody here opened, and
        before `session/new` is answered there is no session to ask under at
        all. An update that arrives before that answer is held to the id it
        claimed instead, once the answer names one.
        """

        return params.get("sessionId") != self._session

    def _answered(self, method: str, result: JsonObject) -> Actions:
        match method:
            case AcpMethod.INITIALIZE:
                return self._asking(
                    AcpMethod.SESSION_NEW,
                    {"cwd": str(self.working_directory), "mcpServers": []},
                )
            case AcpMethod.SESSION_NEW:
                return self._session_opened(result)
            case AcpMethod.SESSION_PROMPT:
                return self._turn_ended(result)
            case _:
                return self._faulted(AcpConversationFault.NO_TERMINAL_ANSWER)

    def _session_opened(self, result: JsonObject) -> Actions:
        session = result.get("sessionId")
        if not isinstance(session, str) or not session:
            return self._faulted(AcpConversationFault.NO_SESSION)
        if self._claimed_session and self._claimed_session != session:
            return self._faulted(AcpConversationFault.FOREIGN_SESSION)
        self._session = session
        cancellation = self._codec.encode(
            JsonRpcNotification(AcpMethod.SESSION_CANCEL, {"sessionId": session}),
            self.bounds.maximum_cancel_bytes,
        )
        if isinstance(cancellation, UnsendableFrame):
            return self._faulted(AcpConversationFault.UNSENDABLE_FRAME)
        return (ProviderCancellationFrame(cancellation.data),) + self._asking(
            AcpMethod.SESSION_PROMPT,
            {"sessionId": session, "prompt": [{"type": "text", "text": self.prompt}]},
        )

    def _turn_ended(self, result: JsonObject) -> Actions:
        self._answered_prompt = True
        self._ended = True
        stopped = result.get("stopReason")
        spoken = stopped if isinstance(stopped, str) else ""
        if not spoken:
            self._stop_talking(AcpConversationFault.NO_STOP_REASON)
        elif spoken not in _STOP_REASONS_THAT_ONLY_END_A_TURN:
            self._stop_reason = cut_to_field(spoken)
        return self._flushed() + (ProviderConversationComplete(),)

    def _permission_asked(self, asked: JsonRpcRequest) -> Actions:
        offered = _PendingPermission(
            asked.id,
            _option_of(asked.params, AcpSelectableOption.ALLOW_ONCE),
            _option_of(asked.params, AcpSelectableOption.REJECT_ONCE),
        )
        tool_call = asked.params.get("toolCall")
        if not isinstance(tool_call, dict):
            return self._closed_refusal(NO_PARAMS, offered)
        named = tool_call.get("toolCallId")
        charged = self._charged(named) if isinstance(named, str) and named else ()
        classified = self.vocabulary.classify_permission(tool_call)
        if isinstance(classified, Unrepresentable):
            return charged + self._closed_refusal(tool_call, offered)
        self._asked_questions += 1
        correlation = PermissionCorrelationId.for_call(
            self.attempt_id, MINIMUM_PERMISSION_CALL_ORDINAL + self._asked_questions - 1
        )
        self._questions[correlation] = offered
        return charged + (
            PermissionRequest(classified.effect, classified.scope, correlation),
        )

    def _closed_refusal(
        self, tool_call: JsonObject, offered: _PendingPermission
    ) -> Actions:
        self._latch(ProviderTerminalReason.POLICY_REFUSED)
        return (
            self._flushed()
            + self._evidence(refused_permission_evidence(tool_call))
            + self._sending(
                JsonRpcResponse(
                    offered.identifier, _permission_outcome(offered.refused)
                )
            )
        )

    def _file_asked(
        self, asked: JsonRpcRequest, effect: ProviderFilesystemEffect
    ) -> Actions:
        path = asked.params.get("path")
        written = (
            _written_content(asked.params)
            if effect is ProviderFilesystemEffect.WRITE
            else b""
        )
        if not isinstance(path, str) or written is None:
            return self._sending(
                JsonRpcError(asked.id, INVALID_PARAMS_CODE, _UNREADABLE_PARAMS_MESSAGE)
            )
        self._asked_files += 1
        request_id = ProviderFilesystemRequestId(self._asked_files)
        self._files[request_id] = _PendingFile(asked.id, effect)
        return (ProviderFilesystemRequest(effect, Path(path), request_id, written),)

    def _asking(self, method: AcpMethod, params: JsonObject) -> Actions:
        return self._sending(self._codec.ask(method, params))

    def _written(self, message: OutgoingMessage) -> ProviderStandardInput | None:
        """The bytes this message costs, or nothing where it will not fit."""

        encoded = self._codec.encode(message, self.bounds.maximum_reply_bytes)
        if isinstance(encoded, UnsendableFrame):
            return None
        return ProviderStandardInput(encoded.data)

    def _sending(self, message: OutgoingMessage) -> Actions:
        written = self._written(message)
        if written is None:
            return self._faulted(AcpConversationFault.UNSENDABLE_FRAME)
        return (written,)

    def _answering(
        self, identifier: JsonRpcId, result: JsonObject
    ) -> ProviderStandardInput:
        return self._delivered(JsonRpcResponse(identifier, result))

    def _delivered(self, message: OutgoingMessage) -> ProviderStandardInput:
        written = self._written(message)
        if written is not None:
            return written
        self._stop_talking(AcpConversationFault.UNSENDABLE_FRAME)
        return ProviderStandardInput(b"")

    def _faulted(self, fault: AcpConversationFault) -> Actions:
        self._stop_talking(fault)
        return self._flushed() + (ProviderConversationComplete(),)

    def _stop_talking(self, fault: AcpConversationFault) -> None:
        self._ended = True
        self._fault = self._fault or fault

    def _latch(self, reason: ProviderTerminalReason) -> None:
        if self._local_cause is None:
            self._local_cause = reason

    def _charged(self, identifier: str) -> Actions:
        if identifier in self._tool_calls or self._local_cause is not None:
            return ()
        self._tool_calls[identifier] = ""
        if len(self._tool_calls) <= self.maximum_tool_calls:
            return ()
        self._latch(ProviderTerminalReason.BUDGET_EXHAUSTED)
        return (ProviderCancellationRequest(ProviderCancellationCause.BUDGET),)

    def _announced(
        self, identifier: str, title: str, locations: tuple[str, ...]
    ) -> Steps:
        if identifier not in self._tool_calls:
            return ()
        already = bool(self._tool_calls[identifier])
        self._tool_calls[identifier] = title
        if already:
            return ()
        return (ProviderSessionEvent(ToolCalled(title, ", ".join(locations))),)

    def _settled(
        self, identifier: str, status: AcpToolCallStatus, content: str
    ) -> Steps:
        """The outcome of one call this conversation took on, and of no other.

        A call it never took on -- one the ceiling already refused, one narrated
        before the session was named -- has no step to be the outcome of, and an
        outcome without its call reads as a tool nobody called.
        """

        if identifier not in self._tool_calls:
            return ()
        answered = f"{status.value}: {content}" if content else status.value
        return (
            ProviderSessionEvent(ToolReturned(self._tool_calls[identifier], answered)),
        )

    def _spoken(self, text: str) -> Actions:
        self._said += text
        if len(self._said) < MAXIMUM_TRANSCRIPT_STEP_CHARACTERS:
            return ()
        return self._flushed()

    def _flushed(self) -> Steps:
        if not self._said:
            return ()
        said, self._said = self._said, ""
        return (ProviderSessionEvent(AssistantTurn(said)),)

    def _evidence(self, text: str) -> Steps:
        if self._unrecognised >= MAXIMUM_UNRECOGNISED_UPDATE_STEPS:
            return ()
        self._unrecognised += 1
        return _kept(text)

    def _broken_promise(self) -> Steps:
        """Which of the protocol's promises broke, where one did.

        The reading itself is `PROTOCOL_FAULT`, and the seam's one word belongs
        to a provider that stopped itself -- so what broke is evidence, kept in
        the transcript that owns its width.
        """

        if self._fault is None:
            return ()
        return _kept(PROTOCOL_FAULT_EVIDENCE + self._fault.value)

    def _half_frame(self) -> Steps:
        rest = self._codec.incomplete_frame()
        return _kept(rest.decode("utf-8", "replace")) if rest else ()

    def _outcome(self, ending: ProviderConversationEnding) -> ProviderTerminalOutcome:
        if self._local_cause is not None:
            return ProviderTerminalOutcome(self._local_cause)
        cancelled = _REASON_OF_ENDING.get(ending)
        if cancelled is not None:
            return ProviderTerminalOutcome(cancelled)
        if self._fault is not None:
            return ProviderTerminalOutcome(ProviderTerminalReason.PROTOCOL_FAULT)
        if self._stop_reason:
            return ProviderTerminalOutcome(
                ProviderTerminalReason.CANCELLED_BY_PROVIDER, self._stop_reason
            )
        return ProviderTerminalOutcome(ProviderTerminalReason.ENDED)


def _written_content(params: JsonObject) -> bytes | None:
    """The text a write request carries, or nothing where it carried none.

    A write is what its content says: a request that names none is one nobody
    can answer, never a file this client empties on the agent's behalf.
    """

    content = params.get("content")
    return content.encode("utf-8") if isinstance(content, str) else None


def _kept(text: str) -> Steps:
    return (ProviderSessionEvent(UnrecognisedProviderOutput(text)),)


def _permission_outcome(chosen: str) -> JsonObject:
    """The standard answer to one permission question, refusal included."""

    if not chosen:
        return {"outcome": {"outcome": "cancelled"}}
    return {"outcome": {"outcome": "selected", "optionId": chosen}}


def _option_of(params: JsonObject, wanted: AcpSelectableOption) -> str:
    """The provider's own opaque id for the one option of this kind, if it offered one.

    The id travels back as it arrived and is never read: what an option means
    is its `kind`. Above the field bound it is refused rather than cut, because
    a cut id addresses nothing.
    """

    options = params.get("options")
    if not isinstance(options, list):
        return ""
    for option in options:
        if not isinstance(option, dict) or option.get("kind") != wanted:
            continue
        offered = option.get("optionId")
        if (
            isinstance(offered, str)
            and 0 < len(offered) <= MAXIMUM_AGENT_FIELD_CHARACTERS
        ):
            return offered
    return ""
