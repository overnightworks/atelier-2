import {
  decodeStreamFrame,
  isRunProjectionCorrupt,
  isStreamFailure,
  type DefectiveRunRow,
  type Problem,
  type RunEvent
} from "../api/client";
import { withDefectiveRow } from "./runList";
import type { ConnectionState, ProtocolProblem } from "./runProjection";

/**
 * The workbench's hold of `GET /events`. Connection states are the same words
 * as a per-run stream; sequence and identity stay on the server's resume.
 */
type AttentionConnection = Exclude<ConnectionState, "complete">;

export interface AttentionHold {
  connection: AttentionConnection;
  protocol_problem: ProtocolProblem | null;
  stream_failure: Problem | null;
}

export function startAttentionHold(): AttentionHold {
  return { connection: "connecting", protocol_problem: null, stream_failure: null };
}

export function markAttentionLive(hold: AttentionHold): AttentionHold {
  if (attentionStopped(hold)) return hold;
  return { ...hold, connection: "live" };
}

export function markAttentionConnecting(
  hold: AttentionHold,
  reconnecting = false
): AttentionHold {
  if (attentionStopped(hold)) return hold;
  return { ...hold, connection: reconnecting ? "reconnecting" : "connecting" };
}

function markAttentionFailed(
  hold: AttentionHold,
  problem: Problem | null
): AttentionHold {
  return { ...hold, connection: "failed", stream_failure: problem };
}

export function attentionStopped(hold: AttentionHold): boolean {
  return hold.protocol_problem !== null || hold.connection === "failed";
}

/**
 * The kinds this feed carries, as the door itself declares them
 * (`ATTENTION_EVENT_KINDS` in `adapters/dbos/attention_events.py`). A shorter
 * list here is not caution: a kind the door sends and this list omits is read
 * as a broken contract, which stops the hold on the first reconciliation the
 * workshop raises. `RUN_PROJECTION_CORRUPT` is a feed frame, not a durable
 * kind; `applyAttentionFrame` names it without treating it as this list.
 */
function isAttentionEvent(event: RunEvent): boolean {
  return (
    event.event === "WAITING_INPUT" ||
    event.event === "AGENT_FAILED" ||
    event.event === "ACTION_RECONCILIATION_REQUIRED"
  );
}

export interface AppliedAttentionFrame {
  hold: AttentionHold;
  event: RunEvent | null;
  /**
   * The run this frame named unreadable, as the same defective row a run list
   * answers with (#1042): the feed goes on, and the run is shown for what it
   * is instead of vanishing from the room.
   */
  unreadable: DefectiveRunRow | null;
}

export function applyAttentionFrame(
  hold: AttentionHold,
  rawData: string
): AppliedAttentionFrame {
  if (attentionStopped(hold)) return { hold, event: null, unreadable: null };
  let frame;
  try {
    frame = decodeStreamFrame(JSON.parse(rawData));
  } catch {
    return decoderFailure(hold);
  }
  if (isStreamFailure(frame)) {
    return { hold: markAttentionFailed(hold, frame.problem), event: null, unreadable: null };
  }
  if (isRunProjectionCorrupt(frame)) {
    return {
      hold,
      event: null,
      unreadable: {
        kind: "defective",
        public_run_reference: frame.public_run_reference,
        problem_code: "durable-state-corrupt",
        detail: frame.problem.title
      }
    };
  }
  if (!isAttentionEvent(frame)) return decoderFailure(hold);
  return { hold, event: frame, unreadable: null };
}

function decoderFailure(hold: AttentionHold): AppliedAttentionFrame {
  return { hold: { ...hold, protocol_problem: { type: "decoder" } }, event: null, unreadable: null };
}

/**
 * The runs the feed has named unreadable, after one more frame.
 *
 * A readable event of a named run is the feed confirming that run reads
 * again, so its row leaves; the feed replays every run on reconnect, so a
 * repaired run is cleared by the next connect rather than never.
 */
export function feedDefectiveAfter(
  rows: readonly DefectiveRunRow[],
  applied: AppliedAttentionFrame
): DefectiveRunRow[] {
  if (applied.unreadable !== null) return withDefectiveRow(rows, applied.unreadable);
  const readable = applied.event?.public_run_reference;
  return rows.filter((row) => row.public_run_reference !== readable);
}
