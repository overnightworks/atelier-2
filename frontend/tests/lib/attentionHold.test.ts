import { describe, expect, it } from "vitest";

import {
  applyAttentionFrame,
  feedDefectiveAfter,
  markAttentionConnecting,
  markAttentionLive,
  startAttentionHold
} from "../../src/lib/attentionHold";
import { eventCursor, publicReference, revisionHash, waitingInput } from "../support/runV3";

describe("the studio's hold of GET /events", () => {
  it("starts connecting and becomes live only after the door opens", () => {
    const hold = startAttentionHold();
    expect(hold.connection).toBe("connecting");
    expect(markAttentionLive(hold).connection).toBe("live");
    expect(markAttentionConnecting(markAttentionLive(hold), true).connection).toBe("reconnecting");
  });

  it("hands a WAITING_INPUT frame through without inventing a resume cursor", () => {
    const applied = applyAttentionFrame(
      markAttentionLive(startAttentionHold()),
      JSON.stringify(waitingInput(1))
    );

    expect(applied.hold.protocol_problem).toBeNull();
    expect(applied.event).toMatchObject({
      event: "WAITING_INPUT",
      public_run_reference: publicReference,
      cursor: eventCursor(1)
    });
  });

  it("hands an AGENT_FAILED frame through", () => {
    const applied = applyAttentionFrame(
      markAttentionLive(startAttentionHold()),
      JSON.stringify(agentFailed())
    );

    expect(applied.hold.protocol_problem).toBeNull();
    expect(applied.event).toMatchObject({
      event: "AGENT_FAILED",
      public_run_reference: publicReference
    });
  });

  it("names the unreadable run as a defective row and keeps the hold live", () => {
    const applied = applyAttentionFrame(
      markAttentionLive(startAttentionHold()),
      JSON.stringify({
        event: "RUN_PROJECTION_CORRUPT",
        public_run_reference: publicReference,
        problem: {
          type: "urn:atelier2:problem:v1:durable-state-corrupt",
          title: "Durable state is corrupt",
          status: 500,
          detail: "Stop mutation and inspect the durable store."
        }
      })
    );

    expect(applied.event).toBeNull();
    expect(applied.unreadable).toEqual({
      kind: "defective",
      public_run_reference: publicReference,
      problem_code: "durable-state-corrupt",
      detail: "Durable state is corrupt"
    });
    expect(applied.hold.connection).toBe("live");
    expect(applied.hold.protocol_problem).toBeNull();
    expect(applied.hold.stream_failure).toBeNull();
  });

  it("stops on STREAM_FAILED with the server's problem", () => {
    const applied = applyAttentionFrame(
      markAttentionLive(startAttentionHold()),
      JSON.stringify({
        event: "STREAM_FAILED",
        problem: {
          type: "urn:atelier2:problem:v1:durable-state-corrupt",
          title: "Durable state is corrupt",
          status: 500,
          detail: "Stop mutation and inspect the durable store."
        }
      })
    );

    expect(applied.event).toBeNull();
    expect(applied.hold.connection).toBe("failed");
    expect(applied.hold.stream_failure?.title).toBe("Durable state is corrupt");
  });

  // The door carries three kinds; a client list that knows two reads the third
  // as a broken contract and stops the hold on the first reconciliation the
  // workshop raises.
  it("takes every kind the attention door carries, reconciliation included", () => {
    const applied = applyAttentionFrame(
      markAttentionLive(startAttentionHold()),
      JSON.stringify({
        workflow_format_version: 3 as const,
        cursor: eventCursor(1),
        sequence: 1,
        public_run_reference: publicReference,
        workflow_revision_hash: revisionHash,
        node_id: "action",
        node_execution_id: revisionHash,
        event_hash: revisionHash,
        node_rail: [{ node_id: "action", state: "needs_you" as const, attempt: null }],
        event: "ACTION_RECONCILIATION_REQUIRED",
        request_base64: "YQ==",
        request_hash: revisionHash
      })
    );

    expect(applied.hold.protocol_problem).toBeNull();
    expect(applied.event?.event).toBe("ACTION_RECONCILIATION_REQUIRED");
  });

  it("names a non-attention durable event as a decoder failure", () => {
    const applied = applyAttentionFrame(
      markAttentionLive(startAttentionHold()),
      JSON.stringify({
        cursor: eventCursor(1),
        sequence: 1,
        public_run_reference: publicReference,
        workflow_revision_hash: revisionHash,
        node_id: "agent",
        node_execution_id: revisionHash,
        event_hash: revisionHash,
        event: "AGENT_COMPLETED",
        output: "done",
        payload_hash: revisionHash
      })
    );

    expect(applied.event).toBeNull();
    expect(applied.hold.protocol_problem).toEqual({ type: "decoder" });
  });

  it("names corrupt bytes as a decoder failure", () => {
    const applied = applyAttentionFrame(markAttentionLive(startAttentionHold()), "not-json");

    expect(applied.event).toBeNull();
    expect(applied.hold.protocol_problem).toEqual({ type: "decoder" });
  });
});

describe("the runs the feed names unreadable", () => {
  const live = markAttentionLive(startAttentionHold());
  const namedUnreadable = JSON.stringify({
    event: "RUN_PROJECTION_CORRUPT",
    public_run_reference: publicReference,
    problem: {
      type: "urn:atelier2:problem:v1:durable-state-corrupt",
      title: "Durable state is corrupt",
      status: 500,
      detail: "Stop mutation and inspect the durable store."
    }
  });

  it("keeps one row for a run the feed names again on reconnect", () => {
    const once = feedDefectiveAfter([], applyAttentionFrame(live, namedUnreadable));
    const again = feedDefectiveAfter(once, applyAttentionFrame(live, namedUnreadable));

    expect(again.map((row) => row.public_run_reference)).toEqual([publicReference]);
  });

  it("keeps the row when the feed delivers a readable event of that run", () => {
    const named = feedDefectiveAfter([], applyAttentionFrame(live, namedUnreadable));

    const afterReadableEvent = feedDefectiveAfter(
      named,
      applyAttentionFrame(live, JSON.stringify(agentFailed()))
    );

    expect(afterReadableEvent).toEqual(named);
  });
});

function agentFailed() {
  return {
    workflow_format_version: 3 as const,
    cursor: eventCursor(1),
    sequence: 1,
    public_run_reference: publicReference,
    workflow_revision_hash: revisionHash,
    node_id: "agent",
    node_execution_id: revisionHash,
    event_hash: revisionHash,
    node_rail: [
      {
        node_id: "agent",
        state: "failed" as const,
        attempt: { ordinal: 1 as const, state: "FAILED" as const }
      }
    ],
    event: "AGENT_FAILED" as const,
    failure_code: "PROCESS_EXITED_UNSUCCESSFULLY" as const,
    reason: null,
    attempt_id: revisionHash,
    attempt_ordinal: 1 as const
  };
}
