import { describe, expect, it } from "vitest";

import type { RunEvent, RunV3 } from "../../src/api/client";
import {
  emptyConductorTranscript,
  reduceConductorEvent
} from "../../src/lib/conductorConversation";
import { orderedConductorCandidates } from "../../src/lib/conductorEpisode";

const conductorRevision = "c".repeat(64);

function conductorRun(
  publicRunReference: string,
  startedAt: string | undefined,
  state: RunV3["state"] = "WAITING_INPUT"
): RunV3 {
  return {
    public_run_reference: publicRunReference,
    workflow_revision_hash: conductorRevision,
    started_at: startedAt,
    state
  } as RunV3;
}

/**
 * The conductor's message wait is a `{type: "string"}` schema, so the
 * composer sends the operator's text raw, with no JSON-quoting layer
 * (`CONDUCTOR_MESSAGE_IS_STRING_SCHEMA`, #1091 PR #1108 finding 3) --
 * `answer_base64` here carries exactly the bytes that convention hands the
 * wire, never `JSON.stringify(text)`.
 */
function answeredMessage(cursor: string, text: string): RunEvent {
  return {
    event: "WAIT_ANSWERED",
    cursor,
    node_id: "next_message",
    answer_base64: btoa(text)
  } as RunEvent;
}

function completedAnswer(cursor: string, answer: string): RunEvent {
  return {
    event: "AGENT_COMPLETED",
    cursor,
    node_id: "conduct",
    public_run_reference: "run1.conversation",
    output_base64: btoa(JSON.stringify({ answer }))
  } as RunEvent;
}

function failedAnswer(cursor: string): RunEvent {
  return {
    event: "AGENT_FAILED",
    cursor,
    node_id: "conduct",
    public_run_reference: "run1.conversation"
  } as RunEvent;
}

describe("ordering conductor run candidates", () => {
  it("chooses the newest stamped non-terminal conductor run", () => {
    const [selected] = orderedConductorCandidates([
      conductorRun("run1.older", "2026-09-01T10:00:00Z"),
      conductorRun("run1.newer", "2026-09-01T10:01:00Z"),
      conductorRun("run1.finished", "2026-09-01T10:02:00Z", "COMPLETED")
    ]);

    expect(selected?.public_run_reference).toBe("run1.newer");
  });

  it("returns no candidate when no conductor run remains live", () => {
    expect(
      orderedConductorCandidates([conductorRun("run1.finished", "2026-09-01T10:00:00Z", "COMPLETED")])
    ).toEqual([]);
  });

  it("refuses an unstamped run rather than guessing its age", () => {
    expect(orderedConductorCandidates([conductorRun("run1.unknown", undefined)])).toEqual([]);
  });
});

describe("the durable conductor transcript", () => {
  it("replays a whole history idempotently when an EventSource delivers a frame twice", () => {
    const answered = answeredMessage("run1.conversation:1", "Start the canary");
    const completed = completedAnswer("run1.conversation:2", "Canary is ready.");
    const once = reduceConductorEvent(
      reduceConductorEvent(emptyConductorTranscript(), answered),
      completed
    );
    const replayed = reduceConductorEvent(once, completed);

    expect(replayed.messages.map((message) => [message.speaker, message.text])).toEqual([
      ["you", "Start the canary"],
      ["house", "Canary is ready."]
    ]);
  });

  it("shows the operator's own message verbatim, quotes and all, with no JSON layer to undo", () => {
    const transcript = reduceConductorEvent(
      emptyConductorTranscript(),
      answeredMessage("run1.conversation:1", 'say "yes" to the run')
    );

    expect(transcript.messages[0]?.text).toBe('say "yes" to the run');
  });

  it("keeps an answered round legible when its conductor fails", () => {
    const transcript = reduceConductorEvent(
      reduceConductorEvent(emptyConductorTranscript(), answeredMessage("run1.conversation:1", "Try it.")),
      failedAnswer("run1.conversation:2")
    );

    expect(transcript.messages.map((message) => message.speaker)).toEqual(["you", "house"]);
  });

  it("names a malformed report as unreadable instead of inventing an answer", () => {
    const transcript = reduceConductorEvent(
      emptyConductorTranscript(),
      {
        event: "AGENT_COMPLETED",
        cursor: "run1.conversation:2",
        node_id: "conduct",
        public_run_reference: "run1.conversation",
        output_base64: btoa("not a report")
      } as RunEvent
    );

    expect(transcript.messages[0]?.text).toMatch(/could not be read/);
  });
});
