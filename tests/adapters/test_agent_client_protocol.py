"""What one standard ACP conversation does, from its handshake to its ending.

The conversation is driven exactly as the process seam drives it -- bytes in,
actions out, an answer handed back for every question it published -- because
that is the only way its lifecycle, its correlation and its terminal reading
are the ones a live attempt would get. Nothing here starts a process, opens a
file or decides a permission: the sentences under test are what this
conversation asks for and what it concludes, never what someone did about it.
"""

from __future__ import annotations

import json
import tracemalloc
from collections.abc import Iterable
from pathlib import Path

import pytest

from atelier2.adapters.agent_client_protocol import (
    ACP_PROTOCOL_VERSION,
    MAXIMUM_UNRECOGNISED_UPDATE_STEPS,
    PROTOCOL_FAULT_EVIDENCE,
    AcpConversationFault,
    AcpMethod,
    Actions,
    AgentClientProtocolConversation,
)
from atelier2.adapters.newline_json_rpc import (
    INTERNAL_ERROR_CODE,
    INVALID_PARAMS_CODE,
    INVALID_REQUEST_CODE,
    JSON_RPC_VERSION,
    METHOD_NOT_FOUND_CODE,
    PARSE_ERROR_CODE,
    JsonObject,
    rendered,
)
from atelier2.contracts.agent_attempts import AgentAttemptId
from atelier2.contracts.agent_permissions import (
    GRANTS_NOTHING,
    PermissionDecision,
    PermissionEffect,
    PermissionPolicyRevision,
    PermissionRequest,
    PermissionScope,
    PermissionScopeKind,
    decide,
)
from atelier2.contracts.agent_transcripts import (
    MAXIMUM_TRANSCRIPT_STEP_CHARACTERS,
    AssistantTurn,
    ToolCalled,
    ToolReturned,
    TranscriptEvent,
    UnrecognisedProviderOutput,
)
from atelier2.contracts.agents import MAXIMUM_AGENT_FIELD_CHARACTERS
from atelier2.ports.provider_conversations import (
    ProviderCancellationCause,
    ProviderCancellationFrame,
    ProviderCancellationRequest,
    ProviderConversationBounds,
    ProviderConversationComplete,
    ProviderConversationEnding,
    ProviderFilesystemAnswer,
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

ATTEMPT = AgentAttemptId("a" * 64)
SESSION = "01a06f4c-7326-79d0-9bde-ed08ee7e716c"
PROMPT = "append one line to README.md"
WORKSPACE = Path("/attempts/one")
INITIALIZE_ID = 1
SESSION_NEW_ID = 2
PROMPT_ID = 3


def _bounds(
    reply: int = 8_192, cancel: int = 4_096, incomplete: int = 8_192
) -> ProviderConversationBounds:
    return ProviderConversationBounds(1_048_576, incomplete, reply, cancel, 16_384)


def _conversation(
    bounds: ProviderConversationBounds | None = None,
    maximum_tool_calls: int = 8,
    prompt: str = PROMPT,
) -> AgentClientProtocolConversation:
    return AgentClientProtocolConversation(
        ATTEMPT, prompt, WORKSPACE, bounds or _bounds(), maximum_tool_calls
    )


def _line(payload: JsonObject) -> bytes:
    return json.dumps({"jsonrpc": JSON_RPC_VERSION, **payload}).encode() + b"\n"


def _answer(identifier: int, result: JsonObject) -> bytes:
    return _line({"id": identifier, "result": result})


def _asks(identifier: int, method: str, params: JsonObject) -> bytes:
    return _line({"id": identifier, "method": method, "params": params})


def _notifies(method: str, params: JsonObject) -> bytes:
    return _line({"method": method, "params": params})


def _updates(update: JsonObject) -> bytes:
    return _notifies(AcpMethod.SESSION_UPDATE, {"sessionId": SESSION, "update": update})


def _written(actions: Iterable[object]) -> tuple[JsonObject, ...]:
    """Every frame this conversation asked to have written, decoded again."""

    return tuple(
        json.loads(action.data)
        for action in actions
        if isinstance(action, ProviderStandardInput)
    )


def _steps(actions: Iterable[object]) -> tuple[TranscriptEvent, ...]:
    return tuple(
        action.step for action in actions if isinstance(action, ProviderSessionEvent)
    )


def _prompting(conversation: AgentClientProtocolConversation) -> Actions:
    """Take this conversation to the point where its one prompt is in flight."""

    conversation.open()
    conversation.receive_output(
        _answer(INITIALIZE_ID, {"protocolVersion": ACP_PROTOCOL_VERSION})
    )
    return conversation.receive_output(_answer(SESSION_NEW_ID, {"sessionId": SESSION}))


def _ended(stop_reason: str = "end_turn") -> bytes:
    return _answer(PROMPT_ID, {"stopReason": stop_reason})


def _permission_asked(
    identifier: int = 0,
    kind: str = "edit",
    locations: tuple[JsonObject, ...] = ({"path": "README.md"},),
    options: tuple[JsonObject, ...] = (
        {"optionId": "allow-always", "kind": "allow_always"},
        {"optionId": "yes-once", "kind": "allow_once"},
        {"optionId": "no-once", "kind": "reject_once"},
    ),
) -> bytes:
    return _asks(
        identifier,
        AcpMethod.REQUEST_PERMISSION,
        {
            "sessionId": SESSION,
            "toolCall": {
                "toolCallId": "call-1",
                "kind": kind,
                "title": "Edit `README.md`",
                "locations": list(locations),
            },
            "options": list(options),
        },
    )


def _decision(request: PermissionRequest, granted: bool) -> PermissionDecision:
    policy = (
        PermissionPolicyRevision(frozenset({(request.effect, request.scope)}))
        if granted
        else GRANTS_NOTHING
    )
    return decide(policy, request)


def _broke(
    conversation: AgentClientProtocolConversation,
    fault: AcpConversationFault,
    ending: ProviderConversationEnding = ProviderConversationEnding.OUTPUT_ENDED,
) -> None:
    """A conversation that stopped being one ends as a protocol fault.

    The reading carries no provider word, because nothing the provider decided
    is what ended it; which of the protocol's promises broke is evidence.
    """

    closing = conversation.finish(ending)
    assert closing.outcome == ProviderTerminalOutcome(
        ProviderTerminalReason.PROTOCOL_FAULT
    )
    assert closing.steps == (
        ProviderSessionEvent(
            UnrecognisedProviderOutput(f"{PROTOCOL_FAULT_EVIDENCE}{fault.value}")
        ),
    )


def _only_request(actions: Actions) -> PermissionRequest:
    asked = [action for action in actions if isinstance(action, PermissionRequest)]
    assert len(asked) == 1
    return asked[0]


def test_a_conversation_opens_with_the_handshake_this_client_offers() -> None:
    conversation = _conversation()

    opened = conversation.open()

    assert _written(opened) == (
        {
            "jsonrpc": JSON_RPC_VERSION,
            "id": INITIALIZE_ID,
            "method": AcpMethod.INITIALIZE,
            "params": {
                "protocolVersion": ACP_PROTOCOL_VERSION,
                "clientCapabilities": {
                    "fs": {"readTextFile": True, "writeTextFile": True},
                    "terminal": False,
                },
            },
        },
    )


def test_the_answered_handshake_opens_a_session_in_the_attempt_workspace() -> None:
    conversation = _conversation()
    conversation.open()

    opened = conversation.receive_output(
        _answer(INITIALIZE_ID, {"protocolVersion": ACP_PROTOCOL_VERSION})
    )

    assert _written(opened) == (
        {
            "jsonrpc": JSON_RPC_VERSION,
            "id": SESSION_NEW_ID,
            "method": AcpMethod.SESSION_NEW,
            "params": {"cwd": str(WORKSPACE), "mcpServers": []},
        },
    )


def test_an_opened_session_publishes_its_stop_frame_before_it_prompts() -> None:
    """The frame is ready before the prompt that could need it: a cancellation
    costs no round trip through a conversation that may be mid-parse."""
    conversation = _conversation()

    prompting = _prompting(conversation)

    assert prompting[0] == ProviderCancellationFrame(
        json.dumps(
            {
                "jsonrpc": JSON_RPC_VERSION,
                "method": AcpMethod.SESSION_CANCEL.value,
                "params": {"sessionId": SESSION},
            },
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )
    assert _written(prompting) == (
        {
            "jsonrpc": JSON_RPC_VERSION,
            "id": PROMPT_ID,
            "method": AcpMethod.SESSION_PROMPT,
            "params": {
                "sessionId": SESSION,
                "prompt": [{"type": "text", "text": PROMPT}],
            },
        },
    )


def test_a_session_the_agent_never_named_ends_the_conversation() -> None:
    conversation = _conversation()
    conversation.open()
    conversation.receive_output(_answer(INITIALIZE_ID, {}))

    opened = conversation.receive_output(_answer(SESSION_NEW_ID, {"session": None}))

    assert opened == (ProviderConversationComplete(),)
    _broke(conversation, AcpConversationFault.NO_SESSION)


def test_a_refused_handshake_ends_the_conversation_as_a_protocol_fault() -> None:
    conversation = _conversation()
    conversation.open()

    refused = conversation.receive_output(
        _line({"id": INITIALIZE_ID, "error": {"code": -32603, "message": "no"}})
    )

    assert refused == (ProviderConversationComplete(),)
    _broke(conversation, AcpConversationFault.HANDSHAKE_REFUSED)


def test_what_the_agent_says_becomes_one_turn_rather_than_one_step_per_chunk() -> None:
    conversation = _conversation()
    _prompting(conversation)

    said = conversation.receive_output(
        _updates({"sessionUpdate": "agent_message_chunk", "content": _text("I will ")})
        + _updates({"sessionUpdate": "agent_message_chunk", "content": _text("do it.")})
    )
    ending = conversation.receive_output(_ended())

    assert _steps(said) == ()
    assert _steps(ending) == (AssistantTurn("I will do it."),)


def test_what_the_agent_thought_is_no_part_of_what_it_said() -> None:
    """A thought is the agent talking to itself: the transcript this version
    publishes carries what it said and did, and nothing reads a thought."""
    conversation = _conversation()
    _prompting(conversation)

    thought = conversation.receive_output(
        _updates({"sessionUpdate": "agent_thought_chunk", "content": _text("hmm.")})
    )
    ending = conversation.receive_output(_ended())

    assert thought == ()
    assert _steps(ending) == ()


def test_a_turn_wider_than_a_transcript_step_is_published_before_it_grows() -> None:
    conversation = _conversation(
        bounds=_bounds(incomplete=4 * MAXIMUM_TRANSCRIPT_STEP_CHARACTERS)
    )
    _prompting(conversation)

    said = conversation.receive_output(
        _updates(
            {
                "sessionUpdate": "agent_message_chunk",
                "content": _text("x" * MAXIMUM_TRANSCRIPT_STEP_CHARACTERS),
            }
        )
    )

    assert _steps(said) == (AssistantTurn("x" * MAXIMUM_TRANSCRIPT_STEP_CHARACTERS),)


def test_a_tool_call_and_its_outcome_are_one_pair_correlated_by_the_agents_id() -> None:
    conversation = _conversation()
    _prompting(conversation)

    called = conversation.receive_output(
        _updates(
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "call-7",
                "title": "read_file",
                "locations": [{"path": "README.md"}],
            }
        )
    )
    returned = conversation.receive_output(
        _updates(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "call-7",
                "status": "completed",
                "content": [{"type": "content", "content": _text("# Probe repo")}],
            }
        )
    )

    assert _steps(called) == (ToolCalled("read_file", "README.md"),)
    assert _steps(returned) == (ToolReturned("read_file", "completed: # Probe repo"),)


def test_a_tool_call_the_agent_only_renames_is_not_announced_twice() -> None:
    conversation = _conversation()
    _prompting(conversation)
    conversation.receive_output(
        _updates(
            {"sessionUpdate": "tool_call", "toolCallId": "call-7", "title": "read_file"}
        )
    )

    renamed = conversation.receive_output(
        _updates(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "call-7",
                "title": "Read `README.md`",
            }
        )
    )

    assert _steps(renamed) == ()


@pytest.mark.parametrize(
    "update",
    [
        {"sessionUpdate": "session_info_update", "title": "a title"},
        {"sessionUpdate": "tool_call", "title": "no id at all"},
        {"sessionUpdate": "tool_call_update", "toolCallId": "call-7", "status": "??"},
    ],
)
def test_an_update_this_vocabulary_cannot_read_is_kept_as_evidence(
    update: JsonObject,
) -> None:
    conversation = _conversation()
    _prompting(conversation)

    unread = conversation.receive_output(_updates(update))

    assert _steps(unread) == (
        UnrecognisedProviderOutput(json.dumps(update, separators=(",", ":"))),
    )


def test_evidence_of_an_unreadable_stream_is_bounded() -> None:
    conversation = _conversation()
    _prompting(conversation)
    unreadable = _updates({"sessionUpdate": "unheard_of"})

    kept = conversation.receive_output(
        unreadable * (MAXIMUM_UNRECOGNISED_UPDATE_STEPS + 4)
    )

    assert len(_steps(kept)) == MAXIMUM_UNRECOGNISED_UPDATE_STEPS


def test_an_update_the_transcript_already_owns_is_not_recorded_again() -> None:
    conversation = _conversation()
    _prompting(conversation)

    echoed = conversation.receive_output(
        _updates({"sessionUpdate": "user_message_chunk", "content": _text(PROMPT)})
        + _updates(
            {"sessionUpdate": "available_commands_update", "availableCommands": []}
        )
    )

    assert echoed == ()


def test_a_notification_this_client_does_not_know_is_neither_answered_nor_kept() -> (
    None
):
    conversation = _conversation()
    _prompting(conversation)

    ignored = conversation.receive_output(
        _notifies("_x.ai/queue/changed", {"sessionId": SESSION, "entries": []})
    )

    assert ignored == ()


def test_a_request_this_client_does_not_serve_is_refused_with_its_own_id() -> None:
    conversation = _conversation()
    _prompting(conversation)

    unknown = conversation.receive_output(_asks(11, "terminal/create", {}))

    assert _written(unknown) == (
        {
            "jsonrpc": JSON_RPC_VERSION,
            "id": 11,
            "error": {
                "code": METHOD_NOT_FOUND_CODE,
                "message": "this client does not serve that method",
            },
        },
    )


@pytest.mark.parametrize(
    ("frame", "code"),
    [
        (b"{ not json }\n", PARSE_ERROR_CODE),
        (b'[{"jsonrpc":"2.0","method":"one"}]\n', INVALID_REQUEST_CODE),
    ],
)
def test_a_frame_that_is_not_a_message_is_answered_where_one_is_owed(
    frame: bytes, code: int
) -> None:
    conversation = _conversation()
    _prompting(conversation)

    refused = conversation.receive_output(frame)

    assert _written(refused) == (
        {
            "jsonrpc": JSON_RPC_VERSION,
            "id": None,
            "error": {"code": code, "message": "this client could not read that frame"},
        },
    )


def test_an_unreadable_call_is_answered_under_the_id_it_arrived_with() -> None:
    """An error answers a request, and a request is addressed by its id: an
    answer carrying none leaves the agent waiting for one it can correlate."""
    conversation = _conversation()
    _prompting(conversation)

    refused = conversation.receive_output(
        b'{"jsonrpc":"2.0","id":12,"method":"fs/read_text_file","params":[1]}\n'
    )

    assert _written(refused) == (
        {
            "jsonrpc": JSON_RPC_VERSION,
            "id": 12,
            "error": {
                "code": INVALID_REQUEST_CODE,
                "message": "this client could not read that frame",
            },
        },
    )


def test_an_answer_this_client_cannot_read_is_a_protocol_fault() -> None:
    conversation = _conversation()
    _prompting(conversation)

    unreadable = conversation.receive_output(
        _line({"id": PROMPT_ID, "result": {}, "error": {"code": -1, "message": "no"}})
    )

    assert unreadable == (ProviderConversationComplete(),)
    _broke(conversation, AcpConversationFault.UNREADABLE_ANSWER)


def test_a_permission_this_vocabulary_can_read_is_asked_under_a_minted_id() -> None:
    conversation = _conversation()
    _prompting(conversation)

    asked = conversation.receive_output(_permission_asked())

    assert _only_request(asked) == PermissionRequest(
        PermissionEffect.WORKSPACE_WRITE,
        PermissionScope(PermissionScopeKind.PATH_PREFIX, "README.md"),
        _only_request(asked).correlation_id,
    )
    assert _written(asked) == ()


def test_a_granted_permission_selects_the_agents_own_once_option() -> None:
    conversation = _conversation()
    _prompting(conversation)
    asked = conversation.receive_output(_permission_asked())

    answered = conversation.answer_permission(
        _decision(_only_request(asked), granted=True)
    )

    assert json.loads(answered.data) == {
        "jsonrpc": JSON_RPC_VERSION,
        "id": 0,
        "result": {"outcome": {"outcome": "selected", "optionId": "yes-once"}},
    }


def test_a_refused_permission_is_the_ending_even_when_the_agent_says_cancelled() -> (
    None
):
    """A turn the provider ends after our own refusal is a ruled refusal, not a
    provider that stopped itself: the latched local cause outranks its word."""
    conversation = _conversation()
    _prompting(conversation)
    asked = conversation.receive_output(_permission_asked())

    answered = conversation.answer_permission(
        _decision(_only_request(asked), granted=False)
    )
    conversation.receive_output(_ended("cancelled"))

    assert json.loads(answered.data)["result"] == {
        "outcome": {"outcome": "selected", "optionId": "no-once"}
    }
    assert conversation.finish(ProviderConversationEnding.OUTPUT_ENDED).outcome == (
        ProviderTerminalOutcome(ProviderTerminalReason.POLICY_REFUSED)
    )


def test_a_permission_offering_no_once_option_is_refused_closed() -> None:
    """A persistent option answers questions nobody has asked yet, so a
    conversation that has only those cancels the question instead."""
    conversation = _conversation()
    _prompting(conversation)
    asked = conversation.receive_output(
        _permission_asked(
            options=({"optionId": "always", "kind": "allow_always"},),
        )
    )

    answered = conversation.answer_permission(
        _decision(_only_request(asked), granted=True)
    )

    assert json.loads(answered.data)["result"] == {"outcome": {"outcome": "cancelled"}}
    assert conversation.finish(ProviderConversationEnding.OUTPUT_ENDED).outcome == (
        ProviderTerminalOutcome(ProviderTerminalReason.POLICY_REFUSED)
    )


@pytest.mark.parametrize(
    ("kind", "locations"),
    [
        ("execute", ({"path": "README.md"},)),
        ("edit", ()),
        ("edit", ({"path": ""},)),
        ("edit", ({"path": "README.md"}, {"path": "LICENCE"})),
        ("edit", ({"path": "d" * (MAXIMUM_AGENT_FIELD_CHARACTERS + 1)},)),
    ],
)
def test_a_permission_this_vocabulary_cannot_scope_is_refused_without_being_asked(
    kind: str, locations: tuple[JsonObject, ...]
) -> None:
    """A shell has no standard scope and an edit that names no location has
    none either; a second location and a path too long to be held whole are the
    same problem once more -- the answer would authorise something other than
    the question, so none of them is put to a policy."""
    conversation = _conversation()
    _prompting(conversation)

    asked = conversation.receive_output(
        _permission_asked(kind=kind, locations=locations)
    )

    assert not [action for action in asked if isinstance(action, PermissionRequest)]
    assert _written(asked) == (
        {
            "jsonrpc": JSON_RPC_VERSION,
            "id": 0,
            "result": {"outcome": {"outcome": "selected", "optionId": "no-once"}},
        },
    )
    assert conversation.finish(ProviderConversationEnding.OUTPUT_ENDED).outcome == (
        ProviderTerminalOutcome(ProviderTerminalReason.POLICY_REFUSED)
    )


def test_a_refused_permission_keeps_only_the_fields_that_name_it() -> None:
    """The evidence of a refusal is which door was asked for, of what kind and
    where. What the agent chose to put through that door -- its arguments, its
    vendor envelope -- is not evidence and may carry a secret."""
    conversation = _conversation()
    _prompting(conversation)

    refused = conversation.receive_output(
        _asks(
            0,
            AcpMethod.REQUEST_PERMISSION,
            {
                "sessionId": SESSION,
                "toolCall": {
                    "toolCallId": "call-1",
                    "kind": "execute",
                    "title": "Run `psql`",
                    "locations": [{"path": "README.md"}],
                    "rawInput": {"command": "psql postgres://user:s3cret@host/db"},
                    "_meta": {"x.ai/tool.name": "shell"},
                },
                "options": [{"optionId": "no-once", "kind": "reject_once"}],
            },
        )
    )

    assert _steps(refused) == (
        UnrecognisedProviderOutput(
            rendered(
                {
                    "title": "Run `psql`",
                    "kind": "execute",
                    "locations": ["README.md"],
                }
            )
        ),
    )


def test_a_file_the_agent_wants_read_is_asked_of_the_side_that_owns_it() -> None:
    conversation = _conversation()
    _prompting(conversation)

    asked = conversation.receive_output(
        _asks(
            4, AcpMethod.READ_TEXT_FILE, {"sessionId": SESSION, "path": "/a/README.md"}
        )
    )
    answered = conversation.answer_filesystem(
        ProviderFilesystemReply(
            ProviderFilesystemRequestId(1),
            ProviderFilesystemAnswer.ANSWERED,
            b"# Probe repo\n",
        )
    )

    assert asked == (
        ProviderFilesystemRequest(
            ProviderFilesystemEffect.READ,
            Path("/a/README.md"),
            ProviderFilesystemRequestId(1),
        ),
    )
    assert json.loads(answered.data) == {
        "jsonrpc": JSON_RPC_VERSION,
        "id": 4,
        "result": {"content": "# Probe repo\n"},
    }


def test_a_file_the_agent_wants_written_carries_its_content_to_that_side() -> None:
    conversation = _conversation()
    _prompting(conversation)

    asked = conversation.receive_output(
        _asks(
            4,
            AcpMethod.WRITE_TEXT_FILE,
            {"sessionId": SESSION, "path": "/a/README.md", "content": "one line\n"},
        )
    )
    answered = conversation.answer_filesystem(
        ProviderFilesystemReply(
            ProviderFilesystemRequestId(1), ProviderFilesystemAnswer.ANSWERED
        )
    )

    assert asked == (
        ProviderFilesystemRequest(
            ProviderFilesystemEffect.WRITE,
            Path("/a/README.md"),
            ProviderFilesystemRequestId(1),
            b"one line\n",
        ),
    )
    assert json.loads(answered.data)["result"] == {}


@pytest.mark.parametrize(
    ("reply", "message"),
    [
        (
            ProviderFilesystemReply(
                ProviderFilesystemRequestId(1),
                ProviderFilesystemAnswer.REFUSED,
                refusal=ProviderFilesystemRefusal.PATH_LEFT_THE_LEASE,
            ),
            "this client refused the file: path-left-the-lease",
        ),
        (
            ProviderFilesystemReply(
                ProviderFilesystemRequestId(1),
                ProviderFilesystemAnswer.REFUSED,
                refusal=ProviderFilesystemRefusal.WORKSPACE_IO_FAILED,
                detail="ENOSPC",
            ),
            "this client refused the file: workspace-io-failed (ENOSPC)",
        ),
        (
            ProviderFilesystemReply(
                ProviderFilesystemRequestId(1),
                ProviderFilesystemAnswer.ANSWERED,
                b"\xff\xfe",
            ),
            "this client cannot answer the file as text",
        ),
    ],
)
def test_a_file_this_client_does_not_deliver_is_an_error_the_turn_survives(
    reply: ProviderFilesystemReply, message: str
) -> None:
    conversation = _conversation()
    _prompting(conversation)
    conversation.receive_output(
        _asks(4, AcpMethod.READ_TEXT_FILE, {"sessionId": SESSION, "path": "/a/x"})
    )

    answered = conversation.answer_filesystem(reply)
    ended = conversation.receive_output(_ended())

    assert json.loads(answered.data) == {
        "jsonrpc": JSON_RPC_VERSION,
        "id": 4,
        "error": {"code": INTERNAL_ERROR_CODE, "message": message},
    }
    assert ended == (ProviderConversationComplete(),)
    assert conversation.finish(ProviderConversationEnding.OUTPUT_ENDED).outcome == (
        ProviderTerminalOutcome(ProviderTerminalReason.ENDED)
    )


def test_a_file_wider_than_this_conversation_may_write_is_never_spelled() -> None:
    """A file arrives from a disk and a reply bound is a buffer: a client that
    only discovers the width after spelling the answer has already held it."""
    conversation = _conversation()
    _prompting(conversation)
    conversation.receive_output(
        _asks(4, AcpMethod.READ_TEXT_FILE, {"sessionId": SESSION, "path": "/a/big"})
    )
    wider_than_any_reply = b"x" * 4_000_000

    tracemalloc.start()
    answered = conversation.answer_filesystem(
        ProviderFilesystemReply(
            ProviderFilesystemRequestId(1),
            ProviderFilesystemAnswer.ANSWERED,
            wider_than_any_reply,
        )
    )
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert json.loads(answered.data) == {
        "jsonrpc": JSON_RPC_VERSION,
        "id": 4,
        "error": {
            "code": INTERNAL_ERROR_CODE,
            "message": "this client cannot answer a file that wide",
        },
    }
    assert peak < len(wider_than_any_reply)


@pytest.mark.parametrize(
    ("method", "params"),
    [
        (AcpMethod.READ_TEXT_FILE, {"path": None}),
        (AcpMethod.WRITE_TEXT_FILE, {"path": "/a/README.md"}),
        (AcpMethod.WRITE_TEXT_FILE, {"path": "/a/README.md", "content": None}),
    ],
)
def test_a_file_request_this_client_cannot_read_is_refused_by_its_params(
    method: str, params: JsonObject
) -> None:
    """A write is what its content says: a request that names none is one
    nobody can answer, never a file this client empties on the agent's behalf.
    The turn survives it, as it survives every other refused file."""
    conversation = _conversation()
    _prompting(conversation)

    asked = conversation.receive_output(
        _asks(4, method, {"sessionId": SESSION, **params})
    )
    ended = conversation.receive_output(_ended())

    assert not [
        action for action in asked if isinstance(action, ProviderFilesystemRequest)
    ]
    assert _written(asked) == (
        {
            "jsonrpc": JSON_RPC_VERSION,
            "id": 4,
            "error": {
                "code": INVALID_PARAMS_CODE,
                "message": "this client could not read that request",
            },
        },
    )
    assert ended == (ProviderConversationComplete(),)


@pytest.mark.parametrize(
    "frame",
    [
        _notifies(
            AcpMethod.SESSION_UPDATE,
            {
                "sessionId": "another-session",
                "update": {"sessionUpdate": "agent_message_chunk"},
            },
        ),
        _asks(
            7,
            AcpMethod.READ_TEXT_FILE,
            {"sessionId": "another-session", "path": "/a/x"},
        ),
        _asks(7, AcpMethod.READ_TEXT_FILE, {"path": "/a/x"}),
        _asks(
            7,
            AcpMethod.REQUEST_PERMISSION,
            {"sessionId": "another-session", "toolCall": {}, "options": []},
        ),
    ],
)
def test_a_message_bound_to_another_session_is_a_protocol_fault(frame: bytes) -> None:
    """The session this client opened is the only one it is in: a message
    addressed to another, or to none, is not this conversation's -- answering
    it would let a second session's traffic reach this attempt's authority."""
    conversation = _conversation()
    _prompting(conversation)

    foreign = conversation.receive_output(frame)

    assert foreign == (ProviderConversationComplete(),)
    _broke(conversation, AcpConversationFault.FOREIGN_SESSION)


def test_what_the_agent_narrates_before_its_session_is_named_is_no_part_of_it() -> None:
    """A released agent talks while `session/new` is still in flight, and what
    it says then stands under a session nobody has confirmed: it neither spends
    this attempt's tool ceiling nor reaches its transcript."""
    conversation = _conversation(maximum_tool_calls=1)
    conversation.open()
    conversation.receive_output(
        _answer(INITIALIZE_ID, {"protocolVersion": ACP_PROTOCOL_VERSION})
    )

    early = conversation.receive_output(
        _updates({"sessionUpdate": "tool_call", "toolCallId": "early", "title": "a"})
        + _updates({"sessionUpdate": "agent_message_chunk", "content": _text("hi")})
    )
    conversation.receive_output(_answer(SESSION_NEW_ID, {"sessionId": SESSION}))
    taken = conversation.receive_output(
        _updates({"sessionUpdate": "tool_call", "toolCallId": "first", "title": "b"})
    )

    assert early == ()
    assert _steps(taken) == (ToolCalled("b", ""),)
    assert not [
        action for action in taken if isinstance(action, ProviderCancellationRequest)
    ]


def test_a_session_other_than_the_one_an_early_update_claimed_is_a_protocol_fault() -> (
    None
):
    """The id an early update claimed is held against the answer when it comes:
    two session ids in one exchange means what this client read was another's."""
    conversation = _conversation()
    conversation.open()
    conversation.receive_output(
        _answer(INITIALIZE_ID, {"protocolVersion": ACP_PROTOCOL_VERSION})
    )
    conversation.receive_output(
        _notifies(
            AcpMethod.SESSION_UPDATE,
            {
                "sessionId": "another-session",
                "update": {"sessionUpdate": "agent_message_chunk"},
            },
        )
    )

    opened = conversation.receive_output(
        _answer(SESSION_NEW_ID, {"sessionId": SESSION})
    )

    assert opened == (ProviderConversationComplete(),)
    _broke(conversation, AcpConversationFault.FOREIGN_SESSION)


def test_the_agents_questions_are_answered_while_its_prompt_is_still_open() -> None:
    conversation = _conversation()
    _prompting(conversation)

    both = conversation.receive_output(
        _asks(0, AcpMethod.READ_TEXT_FILE, {"sessionId": SESSION, "path": "/a/x"})
        + _permission_asked(identifier=1)
    )
    ended = conversation.receive_output(_ended())

    assert [type(action) for action in both] == [
        ProviderFilesystemRequest,
        PermissionRequest,
    ]
    assert ended == (ProviderConversationComplete(),)


def test_a_tool_call_ceiling_stops_the_attempt_once_it_is_spent() -> None:
    conversation = _conversation(maximum_tool_calls=2)
    _prompting(conversation)

    within = conversation.receive_output(
        _updates({"sessionUpdate": "tool_call", "toolCallId": "one", "title": "a"})
        + _updates({"sessionUpdate": "tool_call", "toolCallId": "one", "title": "a"})
        + _updates({"sessionUpdate": "tool_call", "toolCallId": "two", "title": "b"})
    )
    past = conversation.receive_output(
        _updates({"sessionUpdate": "tool_call", "toolCallId": "three", "title": "c"})
    )

    assert not [
        action for action in within if isinstance(action, ProviderCancellationRequest)
    ]
    assert past[0] == ProviderCancellationRequest(ProviderCancellationCause.BUDGET)
    assert conversation.finish(
        ProviderConversationEnding.CANCELLED_FOR_BUDGET
    ).outcome == ProviderTerminalOutcome(ProviderTerminalReason.BUDGET_EXHAUSTED)


def test_no_tool_call_is_taken_on_after_the_ceiling_stopped_the_attempt() -> None:
    """A conversation that has asked for its own stop is not still collecting:
    what it kept counting could grow for as long as the provider talked."""
    conversation = _conversation(maximum_tool_calls=1)
    _prompting(conversation)
    conversation.receive_output(
        _updates({"sessionUpdate": "tool_call", "toolCallId": "one", "title": "a"})
        + _updates({"sessionUpdate": "tool_call", "toolCallId": "two", "title": "b"})
    )

    later = conversation.receive_output(
        _updates({"sessionUpdate": "tool_call", "toolCallId": "three", "title": "c"})
    )

    assert _steps(later) == ()


def test_an_outcome_for_a_call_this_conversation_never_took_on_is_no_step() -> None:
    """A ceiling that refused a call refused its outcome with it: a step naming
    no call at all reads as a tool nobody called."""
    conversation = _conversation(maximum_tool_calls=1)
    _prompting(conversation)
    conversation.receive_output(
        _updates({"sessionUpdate": "tool_call", "toolCallId": "one", "title": "a"})
        + _updates({"sessionUpdate": "tool_call", "toolCallId": "two", "title": "b"})
    )

    settled = conversation.receive_output(
        _updates(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "three",
                "status": "completed",
                "content": [{"type": "content", "content": _text("done")}],
            }
        )
    )

    assert _steps(settled) == ()


@pytest.mark.parametrize(
    ("stop_reason", "ending", "outcome"),
    [
        (
            "end_turn",
            ProviderConversationEnding.OUTPUT_ENDED,
            ProviderTerminalOutcome(ProviderTerminalReason.ENDED),
        ),
        (
            "max_tokens",
            ProviderConversationEnding.OUTPUT_ENDED,
            ProviderTerminalOutcome(ProviderTerminalReason.ENDED),
        ),
        (
            "cancelled",
            ProviderConversationEnding.OUTPUT_ENDED,
            ProviderTerminalOutcome(
                ProviderTerminalReason.CANCELLED_BY_PROVIDER, "cancelled"
            ),
        ),
        (
            "sudden_hail",
            ProviderConversationEnding.OUTPUT_ENDED,
            ProviderTerminalOutcome(
                ProviderTerminalReason.CANCELLED_BY_PROVIDER, "sudden_hail"
            ),
        ),
        (
            "cancelled",
            ProviderConversationEnding.CANCELLED_BY_OPERATOR,
            ProviderTerminalOutcome(ProviderTerminalReason.CANCELLED_BY_OPERATOR),
        ),
    ],
)
def test_the_ending_a_conversation_reads_is_its_own_cause_before_the_agents_word(
    stop_reason: str,
    ending: ProviderConversationEnding,
    outcome: ProviderTerminalOutcome,
) -> None:
    conversation = _conversation()
    _prompting(conversation)

    finished = conversation.receive_output(_ended(stop_reason))

    assert finished == (ProviderConversationComplete(),)
    assert conversation.finish(ending).outcome == outcome


def test_a_turn_that_ended_without_saying_why_is_a_protocol_fault() -> None:
    """A `stopReason` is what a terminal answer is: an answer without one says
    the turn is over and refuses to say how, which is a broken promise rather
    than a provider stopping itself."""
    conversation = _conversation()
    _prompting(conversation)

    ended = conversation.receive_output(_answer(PROMPT_ID, {}))

    assert ended == (ProviderConversationComplete(),)
    _broke(conversation, AcpConversationFault.NO_STOP_REASON)


def test_a_prompt_that_was_never_answered_is_not_a_turn_that_ended() -> None:
    conversation = _conversation()
    _prompting(conversation)

    _broke(conversation, AcpConversationFault.NO_TERMINAL_ANSWER)


def test_a_second_terminal_answer_changes_nothing_the_first_one_settled() -> None:
    conversation = _conversation()
    _prompting(conversation)
    conversation.receive_output(_ended())

    again = conversation.receive_output(_ended("cancelled"))

    assert again == ()
    assert conversation.finish(ProviderConversationEnding.OUTPUT_ENDED).outcome == (
        ProviderTerminalOutcome(ProviderTerminalReason.ENDED)
    )


def test_what_stood_after_the_last_newline_is_kept_when_the_output_runs_out() -> None:
    conversation = _conversation()
    _prompting(conversation)
    conversation.receive_output(_ended())

    closing = conversation.finish(ProviderConversationEnding.OUTPUT_ENDED)

    assert closing.steps == ()

    unfinished = _conversation()
    _prompting(unfinished)
    unfinished.receive_output(b'{"jsonrpc":"2.0","method":"session/up')

    assert unfinished.finish(ProviderConversationEnding.TERMINATED).steps == (
        ProviderSessionEvent(
            UnrecognisedProviderOutput(
                f"{PROTOCOL_FAULT_EVIDENCE}"
                f"{AcpConversationFault.NO_TERMINAL_ANSWER.value}"
            )
        ),
        ProviderSessionEvent(
            UnrecognisedProviderOutput('{"jsonrpc":"2.0","method":"session/up')
        ),
    )


def test_an_answer_to_a_question_nobody_asked_ends_the_conversation() -> None:
    conversation = _conversation()
    _prompting(conversation)

    unexpected = conversation.receive_output(_answer(99, {"stopReason": "end_turn"}))

    assert unexpected == (ProviderConversationComplete(),)
    _broke(conversation, AcpConversationFault.UNEXPECTED_ANSWER)


def test_a_line_wider_than_this_conversation_may_hold_ends_it() -> None:
    conversation = _conversation(bounds=_bounds(incomplete=256))
    _prompting(conversation)

    flooded = conversation.receive_output(b"x" * 512 + b"\n")

    assert flooded == (ProviderConversationComplete(),)
    _broke(conversation, AcpConversationFault.LOST_FRAMING)


def test_a_prompt_this_client_cannot_spell_inside_its_reply_bound_ends_it() -> None:
    """The handshake fits and the prompt does not, so what the bound refuses is
    the one frame this attempt exists to send."""
    conversation = _conversation(bounds=_bounds(reply=1_024), prompt="p" * 2_048)

    conversation.open()
    conversation.receive_output(_answer(INITIALIZE_ID, {}))
    unsendable = conversation.receive_output(
        _answer(SESSION_NEW_ID, {"sessionId": SESSION})
    )

    assert _written(unsendable) == ()
    assert unsendable[-1] == ProviderConversationComplete()
    _broke(conversation, AcpConversationFault.UNSENDABLE_FRAME)


def test_a_stop_frame_this_client_cannot_spell_inside_its_cancel_bound_ends_it() -> (
    None
):
    conversation = _conversation(bounds=_bounds(cancel=16))

    conversation.open()
    conversation.receive_output(_answer(INITIALIZE_ID, {}))
    unsendable = conversation.receive_output(
        _answer(SESSION_NEW_ID, {"sessionId": SESSION})
    )

    assert unsendable == (ProviderConversationComplete(),)


def test_a_conversation_admits_at_least_one_tool_call() -> None:
    with pytest.raises(ValueError, match="at least one tool call"):
        _conversation(maximum_tool_calls=0)


def _text(said: str) -> JsonObject:
    return {"type": "text", "text": said}
