import { describe, expect, it } from "vitest";

import type { RunV3 } from "../../src/api/client";
import {
  newestActivityFirst,
  newestReadOfEachRun,
  runActivityAt,
  runListRowReference,
  splitRunListRows,
  withDefectiveRow
} from "../../src/lib/runList";
import {
  defectiveRunRow,
  notCancellableBlock,
  publicReference,
  revisionHash,
  runRow,
  startedRun
} from "../support/runV3";

function v3Run(changes: Partial<RunV3> = {}): RunV3 {
  return {
    workflow_format_version: 3,
    run_id: "v3/two-agents",
    workflow_name: "Two agents in a line",
    public_run_reference: publicReference,
    workflow_revision_hash: revisionHash,
    agent_binding_set_hash: "b".repeat(64),
    run_configuration_revision_hash: "c".repeat(64),
    agent_bindings: [],
    orders: [],
    state_version: 1,
    state: "STARTED",
    current_node_id: "review",
    node_rail: [{ node_id: "review", state: "working", attempt: null }],
    cancellation: notCancellableBlock("between-nodes"),
    terminal_hash: null,
    latest_event_cursor: null,
    started_at: "2026-08-18T15:00:00Z",
    ended_at: null,
    ...changes,
    current_node_execution_id: changes.current_node_execution_id ?? revisionHash
  };
}

describe("the project run list ranks by last known activity", () => {
  it("puts a later start before an earlier one, even when the durable list answered oldest first", () => {
    const older = v3Run({
      run_id: "older",
      public_run_reference: "run1.b2xkZXI",
      started_at: "2026-08-18T14:00:00Z"
    });
    const newer = v3Run({
      run_id: "newer",
      public_run_reference: "run1.bmV3ZXI",
      started_at: "2026-08-18T16:00:00Z"
    });

    expect(newestActivityFirst([older, newer]).map((run) => run.run_id)).toEqual(["newer", "older"]);
  });

  it("ranks a finished run by when it ended, not when it started", () => {
    const long = v3Run({
      run_id: "long",
      public_run_reference: "run1.bG9uZw",
      state: "COMPLETED",
      terminal_hash: "d".repeat(64),
      started_at: "2026-08-18T10:00:00Z",
      ended_at: "2026-08-18T12:00:00Z"
    });
    const short = v3Run({
      run_id: "short",
      public_run_reference: "run1.c2hvcnQ",
      state: "COMPLETED",
      terminal_hash: "e".repeat(64),
      started_at: "2026-08-18T11:00:00Z",
      ended_at: "2026-08-18T13:00:00Z"
    });

    expect(runActivityAt(long)).toBe("2026-08-18T12:00:00Z");
    expect(newestActivityFirst([long, short]).map((run) => run.run_id)).toEqual(["short", "long"]);
  });

  it("ranks a run the operator cancelled by when it ended, same as any other terminal run", () => {
    const cancelled = v3Run({
      run_id: "cancelled",
      public_run_reference: "run1.Y2FuY2VsbGVk",
      state: "CANCELLED",
      terminal_hash: "f".repeat(64),
      started_at: "2026-08-18T10:00:00Z",
      ended_at: "2026-08-18T11:00:00Z"
    });
    const later = v3Run({
      run_id: "later",
      public_run_reference: "run1.bGF0ZXI",
      started_at: "2026-08-18T12:00:00Z"
    });

    expect(runActivityAt(cancelled)).toBe("2026-08-18T11:00:00Z");
    expect(newestActivityFirst([cancelled, later]).map((run) => run.run_id)).toEqual([
      "later",
      "cancelled"
    ]);
  });

  it("leaves rows without a stamp in the durable order, behind every dated row", () => {
    const dated = v3Run({ run_id: "dated", public_run_reference: "run1.ZGF0ZWQ" });
    const firstUntimed = startedRun({ run_id: "first", public_run_reference: "run1.Zmlyc3Q" });
    const secondUntimed = startedRun({ run_id: "second", public_run_reference: "run1.c2Vjb25k" });

    expect(
      newestActivityFirst([firstUntimed, dated, secondUntimed]).map((run) => run.run_id)
    ).toEqual(["dated", "first", "second"]);
  });
});

/**
 * A room that asks several state lists at one moment gets them answered
 * separately, so a run that moves between two answers comes back in both.
 */
describe("one entry per run, across separately answered reads", () => {
  it("keeps the higher state version when two reads answer with the same run", () => {
    const opened = runRow(v3Run({ state: "WAITING_INPUT", state_version: 2 }));
    const earlier = runRow(v3Run({ state: "STARTED", state_version: 1 }));

    expect(newestReadOfEachRun([earlier, opened])).toEqual([opened]);
    // Whichever read is drained first, the fresher truth is the one kept.
    expect(newestReadOfEachRun([opened, earlier])).toEqual([opened]);
  });

  it("keeps every distinct run, in the order it was first read", () => {
    const first = runRow(v3Run({ public_run_reference: "run1.YQ" }));
    const second = runRow(v3Run({ public_run_reference: "run1.Yg" }));

    expect(newestReadOfEachRun([first, second])).toEqual([first, second]);
  });

  it("answers an empty reading with an empty list rather than inventing a row", () => {
    expect(newestReadOfEachRun([])).toEqual([]);
  });

  it("prefers a run resource over a defective row for the same reference (#1042)", () => {
    const defective = defectiveRunRow({ public_run_reference: publicReference });
    const healthy = runRow(v3Run({ public_run_reference: publicReference }));

    expect(newestReadOfEachRun([defective, healthy])).toEqual([healthy]);
    // Whichever read is drained first, the more informative row is kept.
    expect(newestReadOfEachRun([healthy, defective])).toEqual([healthy]);
  });
});

describe("splitting a run list page into its two row shapes (#1042)", () => {
  it("separates a defective row from its healthy neighbours", () => {
    const first = v3Run({ run_id: "healthy-a", public_run_reference: "run1.YQ" });
    const second = v3Run({ run_id: "healthy-b", public_run_reference: "run1.Yg" });
    const defective = defectiveRunRow({ public_run_reference: "run1.Yw" });

    const split = splitRunListRows([runRow(first), defective, runRow(second)]);

    expect(split.runs).toEqual([first, second]);
    expect(split.defective).toEqual([defective]);
  });

  it("names the run either row shape refers to", () => {
    const healthy = runRow(v3Run({ public_run_reference: "run1.YQ" }));
    const defective = defectiveRunRow({ public_run_reference: "run1.Yg" });

    expect(runListRowReference(healthy)).toBe("run1.YQ");
    expect(runListRowReference(defective)).toBe("run1.Yg");
  });
});

describe("one defective row per run, however often it is named", () => {
  it("adds a run no reader has named yet", () => {
    const listed = defectiveRunRow({ public_run_reference: "run1.YQ" });
    const named = defectiveRunRow({ public_run_reference: "run1.Yg" });

    expect(withDefectiveRow([listed], named)).toEqual([listed, named]);
  });

  it("keeps the first row when the same run is named again", () => {
    const listed = defectiveRunRow({ public_run_reference: "run1.YQ" });
    const namedAgain = defectiveRunRow({
      public_run_reference: "run1.YQ",
      detail: "Durable state is corrupt"
    });

    expect(withDefectiveRow([listed], namedAgain)).toEqual([listed]);
  });
});
