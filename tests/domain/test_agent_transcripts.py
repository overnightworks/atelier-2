"""What an attempt's kept steps promise: readable, credential-free, bounded."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any

import pytest

from atelier2.contracts.agent_transcripts import (
    MAXIMUM_ATTEMPT_TRANSCRIPT_BYTES,
    MAXIMUM_TRANSCRIPT_STEP_CHARACTERS,
    TRANSCRIPT_STEP_CUT_MARKER,
    AssistantTurn,
    AttemptTranscript,
    ProviderTerminalRefusal,
    ToolCalled,
    ToolReturned,
    TranscriptEvent,
    TranscriptEventKind,
    TranscriptTruncated,
    UnrecognisedProviderOutput,
    Usage,
)
from atelier2.contracts.secret_redaction import REDACTION_MARKER


def kept_events(transcript: AttemptTranscript) -> list[dict[str, Any]]:
    document = json.loads(transcript.document)
    events: list[dict[str, Any]] = document["events"]
    return events


def widest_step(filler: str) -> str:
    return filler * MAXIMUM_TRANSCRIPT_STEP_CHARACTERS


def planted_credential(issuer: str, body: str) -> str:
    """A credential-shaped canary, assembled here instead of spelled out.

    This repository's own history is secret-scanned, so a test that plants the
    shape of a credential builds it from parts rather than writing a
    credential-shaped literal into the history that scan reads.
    """

    return f"{issuer}{body}"


@pytest.mark.parametrize(
    ("canary", "printed"),
    [
        pytest.param(
            planted_credential("sk-ant", "-plantedcanarysecret0123456789"),
            "ANTHROPIC_API_KEY={canary}",
            id="an issuer-prefixed token",
        ),
        pytest.param(
            planted_credential("wJalrXUtnFEMI", "K7MDENGbPxRfiCYEXAMPLEKEY"),
            "AWS_SECRET_ACCESS_KEY={canary}",
            id="a provider credential named by its whole identifier",
        ),
        pytest.param(
            planted_credential("AKIA", "7QF3NOTAREALKEY0"),
            "aws --profile {canary} s3 ls",
            id="a provider key id no field names",
        ),
    ],
)
@pytest.mark.parametrize(
    "build",
    [
        pytest.param(AttemptTranscript.of, id="through the adapter's own door"),
        pytest.param(
            lambda events: AttemptTranscript(tuple(events)),
            id="through the constructor beside it",
        ),
    ],
)
def test_no_way_of_building_a_transcript_lets_a_credential_through(
    canary: str,
    printed: str,
    build: Callable[[Sequence[TranscriptEvent]], AttemptTranscript],
) -> None:
    """Both doors are asked, because a redactor with a way past it is none.

    An earlier revision made redaction the classmethod's job and left the
    constructor open beside it, so a caller that built one directly published
    the provider's raw bytes. The canaries are three shapes a real agent
    prints: one an issuer declares, one a whole identifier names, and one
    neither does.
    """

    leaked = printed.format(canary=canary)
    transcript = build(
        (
            ToolReturned("Bash", f"{leaked}\n"),
            AssistantTurn(f"I ran `{leaked}` and stopped."),
            UnrecognisedProviderOutput(f"fatal: {leaked} was rejected"),
            ToolCalled("Bash", f'{{"command":"{leaked}"}}'),
        )
    )

    assert canary not in transcript.document.decode("utf-8")
    assert all(step["redacted"] for step in kept_events(transcript))


def test_normalising_an_already_normalised_step_still_says_it_was_redacted() -> None:
    """The flag survives a second pass, or a rebuild would un-say the redaction.

    Nothing matches a credential shape once the marker stands where one stood,
    so a flag recomputed from the kept text alone would come back False and the
    reader would be told that nothing had been taken out.
    """

    canary = planted_credential("sk-ant", "-plantedcanarysecret0123456789")
    once = AttemptTranscript.of([ToolReturned("Bash", f"API_KEY={canary}")])

    assert AttemptTranscript(once.events) == once
    assert kept_events(once)[0]["redacted"]


def test_a_credential_a_tool_printed_is_replaced_and_the_step_says_so() -> None:
    canary = planted_credential("sk-ant", "-plantedcanarysecret0123456789")

    transcript = AttemptTranscript.of(
        [ToolReturned("Bash", f"ANTHROPIC_API_KEY={canary}\n")]
    )

    assert canary not in transcript.document.decode("utf-8")
    assert kept_events(transcript) == [
        {
            "event": TranscriptEventKind.TOOL_RETURNED.value,
            "name": "Bash",
            "result": f"ANTHROPIC_API_KEY={REDACTION_MARKER}\n",
            "redacted": True,
        }
    ]


def test_an_operator_home_path_in_a_provider_refusal_is_replaced_too() -> None:
    """An operator's own filesystem path is scrubbed like a credential shape.

    A provider refusal is otherwise kept verbatim (#1029); a path naming who
    ran the call and how their machine is laid out must not survive into an
    artifact nobody can delete.
    """

    transcript = AttemptTranscript.of(
        [
            ProviderTerminalRefusal(
                "api_error",
                "",
                "reading /home/operator/git/atelier-2/AGENTS.md failed",
            )
        ]
    )

    assert "operator" not in transcript.document.decode("utf-8")
    assert kept_events(transcript) == [
        {
            "event": "provider-terminal-refusal",
            "terminal_reason": "api_error",
            "api_error_status": "",
            "text": f"reading {REDACTION_MARKER} failed",
            "redacted": True,
        }
    ]


def test_a_credential_inside_a_step_too_wide_to_keep_is_still_replaced() -> None:
    canary = planted_credential("AKIA", "7QF3NOTAREALKEY0")
    result = f"{canary} " + widest_step("x")

    transcript = AttemptTranscript.of([ToolReturned("Read", result)])

    assert canary not in transcript.document.decode("utf-8")
    assert kept_events(transcript)[0]["redacted"]


def test_a_credential_in_output_no_step_describes_is_replaced_too() -> None:
    """What the vocabulary could not read is bounded and redacted like a step.

    This is the material with the least structure and the most risk -- whatever
    a failing call printed instead of a stream -- so a redactor that ran only
    over recognised steps would leave exactly the wrong text untouched.
    """

    canary = planted_credential("sk-ant", "-plantedcanarysecret0123456789")

    transcript = AttemptTranscript.of(
        [UnrecognisedProviderOutput(f"fatal: token {canary} rejected")]
    )

    assert canary not in transcript.document.decode("utf-8")
    assert kept_events(transcript) == [
        {
            "event": TranscriptEventKind.UNRECOGNISED_PROVIDER_OUTPUT.value,
            "text": f"fatal: token {REDACTION_MARKER} rejected",
            "redacted": True,
        }
    ]


def test_a_step_wider_than_a_reader_can_use_is_cut_rather_than_dropped() -> None:
    transcript = AttemptTranscript.of([AssistantTurn(widest_step("ab"))])

    (step,) = kept_events(transcript)
    assert step["text"].startswith("abab")
    assert step["text"].endswith(TRANSCRIPT_STEP_CUT_MARKER)
    assert len(step["text"]) == MAXIMUM_TRANSCRIPT_STEP_CHARACTERS


def test_a_step_the_reader_can_use_is_kept_exactly() -> None:
    transcript = AttemptTranscript.of([AssistantTurn("I read the file and stopped.")])

    assert kept_events(transcript) == [
        {
            "event": TranscriptEventKind.ASSISTANT_TURN.value,
            "text": "I read the file and stopped.",
            "redacted": False,
        }
    ]


def test_more_steps_than_the_document_holds_lose_the_oldest_and_count_them() -> None:
    steps = [ToolReturned("Read", widest_step(str(index % 10))) for index in range(200)]

    transcript = AttemptTranscript.of(steps)

    assert len(transcript.document) <= MAXIMUM_ATTEMPT_TRANSCRIPT_BYTES
    events = kept_events(transcript)
    dropped = events[0]["dropped_events"]
    assert events[0]["event"] == TranscriptEventKind.TRANSCRIPT_TRUNCATED.value
    assert dropped > 0
    assert (
        events[-1]["result"]
        == kept_events(AttemptTranscript.of(steps[-1:]))[0]["result"]
    )
    assert len(events) == len(steps) - dropped + 1


def test_a_transcript_that_fits_loses_nothing_and_keeps_its_order() -> None:
    transcript = AttemptTranscript.of(
        [
            ToolCalled("Read", '{"file_path":"/etc/hosts"}'),
            ToolReturned("Read", "127.0.0.1 localhost"),
            AssistantTurn("The host file names localhost."),
            Usage(1_200, 48),
        ]
    )

    assert [event["event"] for event in kept_events(transcript)] == [
        TranscriptEventKind.TOOL_CALLED.value,
        TranscriptEventKind.TOOL_RETURNED.value,
        TranscriptEventKind.ASSISTANT_TURN.value,
        TranscriptEventKind.USAGE.value,
    ]


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        pytest.param(
            ToolCalled("Bash", '{"command":"ls"}'),
            {
                "event": "tool-called",
                "name": "Bash",
                "arguments": '{"command":"ls"}',
                "redacted": False,
            },
            id="tool called",
        ),
        pytest.param(
            ToolReturned("Bash", "AGENTS.md\n"),
            {
                "event": "tool-returned",
                "name": "Bash",
                "result": "AGENTS.md\n",
                "redacted": False,
            },
            id="tool returned",
        ),
        pytest.param(
            AssistantTurn("Listing the repository."),
            {
                "event": "assistant-turn",
                "text": "Listing the repository.",
                "redacted": False,
            },
            id="assistant turn",
        ),
        pytest.param(
            Usage(2_048, 96, 512, 128),
            {
                "event": "usage",
                "input_tokens": 2_048,
                "output_tokens": 96,
                "cache_read_input_tokens": 512,
                "cache_creation_input_tokens": 128,
            },
            id="usage",
        ),
        pytest.param(
            ProviderTerminalRefusal(
                "rate_limit_error", "429", "Not logged in · Please run /login"
            ),
            {
                "event": "provider-terminal-refusal",
                "terminal_reason": "rate_limit_error",
                "api_error_status": "429",
                "text": "Not logged in · Please run /login",
                "redacted": False,
            },
            id="provider terminal refusal",
        ),
        pytest.param(
            UnrecognisedProviderOutput("Error: connection reset"),
            {
                "event": "unrecognised-provider-output",
                "text": "Error: connection reset",
                "redacted": False,
            },
            id="unrecognised provider output",
        ),
        pytest.param(
            TranscriptTruncated(7),
            {"event": "transcript-truncated", "dropped_events": 7},
            id="truncation marker",
        ),
    ],
)
def test_every_kind_of_step_is_kept_under_its_persisted_name(
    event: TranscriptEvent, expected: dict[str, Any]
) -> None:
    assert kept_events(AttemptTranscript.of([event])) == [expected]


def test_an_attempt_with_no_steps_keeps_no_transcript() -> None:
    with pytest.raises(ValueError, match="no steps"):
        AttemptTranscript.of([])


@pytest.mark.parametrize(
    "usage",
    [
        pytest.param({"input_tokens": -1, "output_tokens": 0}, id="negative input"),
        pytest.param({"input_tokens": 0, "output_tokens": -1}, id="negative output"),
    ],
)
def test_usage_no_provider_could_have_counted_is_refused(usage: dict[str, int]) -> None:
    with pytest.raises(ValueError, match="fewer than no tokens"):
        Usage(
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
        )


def test_a_truncation_marker_stands_for_a_real_loss() -> None:
    with pytest.raises(ValueError, match="at least one lost step"):
        TranscriptTruncated(0)


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        pytest.param(
            ToolCalled("Bash", '{"command":"ls"}'),
            {
                "event": "tool-called",
                "name": "Bash",
                "arguments": '{"command":"ls"}',
                "redacted": False,
            },
            id="tool called",
        ),
        pytest.param(
            ToolReturned("Bash", "AGENTS.md\n"),
            {
                "event": "tool-returned",
                "name": "Bash",
                "result": "AGENTS.md\n",
                "redacted": False,
            },
            id="tool returned",
        ),
        pytest.param(
            AssistantTurn("Listing the repository."),
            {
                "event": "assistant-turn",
                "text": "Listing the repository.",
                "redacted": False,
            },
            id="assistant turn",
        ),
        pytest.param(
            Usage(2_048, 96, 512, 128),
            {
                "event": "usage",
                "input_tokens": 2_048,
                "output_tokens": 96,
                "cache_read_input_tokens": 512,
                "cache_creation_input_tokens": 128,
            },
            id="usage",
        ),
        pytest.param(
            ProviderTerminalRefusal(
                "rate_limit_error", "429", "Not logged in · Please run /login"
            ),
            {
                "event": "provider-terminal-refusal",
                "terminal_reason": "rate_limit_error",
                "api_error_status": "429",
                "text": "Not logged in · Please run /login",
                "redacted": False,
            },
            id="provider terminal refusal",
        ),
        pytest.param(
            UnrecognisedProviderOutput("Error: connection reset"),
            {
                "event": "unrecognised-provider-output",
                "text": "Error: connection reset",
                "redacted": False,
            },
            id="unrecognised provider output",
        ),
        pytest.param(
            TranscriptTruncated(7),
            {"event": "transcript-truncated", "dropped_events": 7},
            id="truncation marker",
        ),
    ],
)
def test_every_kind_of_step_round_trips_through_from_document(
    event: TranscriptEvent, expected: dict[str, Any]
) -> None:
    stored = AttemptTranscript.of([event])

    loaded = AttemptTranscript.from_document(stored.document)

    assert loaded == stored
    assert loaded.document == stored.document
    assert kept_events(loaded) == [expected]


def test_from_document_reconstructs_the_canonical_stored_bytes() -> None:
    stored = AttemptTranscript.of(
        [
            ToolCalled("Read", '{"file_path":"/etc/hosts"}'),
            ToolReturned("Read", "127.0.0.1 localhost"),
            AssistantTurn("The host file names localhost."),
            Usage(1_200, 48),
            UnrecognisedProviderOutput("Error: connection reset"),
            TranscriptTruncated(7),
        ]
    )

    loaded = AttemptTranscript.from_document(stored.document)

    assert loaded.document == stored.document
    assert loaded == stored


def _canonical_payload() -> dict[str, Any]:
    return json.loads(
        AttemptTranscript.of([AssistantTurn("The host file names localhost.")]).document
    )


def _encode(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()


@pytest.mark.parametrize(
    "document",
    [
        pytest.param(b"", id="empty bytes"),
        pytest.param(
            b'{"kind":"attempt-transcript/v1","events":[]}',
            id="empty events",
        ),
        pytest.param(
            _encode(_canonical_payload() | {"kind": "attempt-transcript/v0"}),
            id="unknown kind",
        ),
        pytest.param(
            _encode(_canonical_payload() | {"extra": 1}),
            id="extra document key",
        ),
        pytest.param(
            _encode(
                _canonical_payload()
                | {
                    "events": [
                        {
                            "event": "mystery",
                            "text": "The host file names localhost.",
                            "redacted": False,
                        }
                    ]
                }
            ),
            id="unknown event",
        ),
        pytest.param(
            _encode(
                _canonical_payload()
                | {
                    "events": [
                        {
                            "event": "assistant-turn",
                            "text": "The host file names localhost.",
                            "redacted": False,
                            "extra": 1,
                        }
                    ]
                }
            ),
            id="extra event key",
        ),
        pytest.param(
            json.dumps(_canonical_payload(), indent=2).encode(),
            id="non-canonical encoding",
        ),
    ],
)
def test_from_document_refuses_a_document_that_is_not_the_canonical_transcript(
    document: bytes,
) -> None:
    with pytest.raises(ValueError):
        AttemptTranscript.from_document(document)


def test_from_document_refuses_bytes_the_constructor_would_have_to_rewrite() -> None:
    """Reading cannot skip the redactor: rewritten bytes are not the stored ones."""

    canary = planted_credential("sk-ant", "-plantedcanarysecret0123456789")
    stored = _encode(
        {
            "kind": "attempt-transcript/v1",
            "events": [
                {
                    "event": "tool-returned",
                    "name": "Bash",
                    "result": f"ANTHROPIC_API_KEY={canary}\n",
                    "redacted": False,
                }
            ],
        }
    )

    with pytest.raises(ValueError):
        AttemptTranscript.from_document(stored)
