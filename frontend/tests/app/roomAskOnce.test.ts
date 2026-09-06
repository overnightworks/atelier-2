import { cleanup, render, waitFor } from "@testing-library/svelte";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "../../src/App.svelte";
import {
  encodePublicRunReference,
  type CatalogLineageKind,
  type CockpitApi,
  type RunV3,
  type WorkflowRevisionSummary
} from "../../src/api/client";
import { MutationJournal } from "../../src/lib/mutationJournal";
import { WORKSHOP_DESTINATION } from "../../src/lib/workshop";
import { cockpitApiStub } from "../support/cockpitApi";
import { notCancellableBlock } from "../support/runV3";
import { revisionHash, runRow, startedRun, waitingInput } from "../support/runV3";

/**
 * REQ-UIQ-08: a surface that exceeds its interaction budget is a defect.
 * The tempo half of that budget, pinned here: a surface asks once for
 * what it shows — the number of Cockpit API requests when opening a
 * room does not grow with the number of its rows.
 *
 * A request is one CockpitApi method call. The later open puts more
 * rows on the same single list page, so a second page the list must
 * follow is not the variable. Counts, never milliseconds: a timing
 * assertion would lie under load.
 *
 * Workbench rows are STARTED living-shelf runs. Catalog rows are
 * admitted workflow tiles. History rows are finished V3 runs of one
 * workflow. A click into a row, a Workbench open-decision pin, the Run
 * page, Settings, a question sheet, and a createElement overlay are
 * outside what this open counts. The attention feed's own connect-time
 * replay is inside it (#1148): a fresh `GET /events` hands back one
 * durable event per non-terminal run before the operator has touched
 * anything, and this open pays for reading that burst off the run list.
 * A stream event delivered later, once the room has already settled, is
 * the outside case -- a decision that opens while the operator is
 * sitting here, not this open's own cost.
 *
 * History reads its workflow, work item and terminal result off the
 * listed run itself (#1045) -- no per-row node detail, no per-hash
 * revision read. Catalog still asks getRevisionByName once per
 * workflow tile; the sentence names that and does not prove it asks
 * once.
 */

const FEWER_ROWS = 2;
const MORE_ROWS = 5;

const NAMED_RESIDUAL_SURFACES = ["Catalog"] as const;

type Room = "Workbench" | "Catalog" | "History";

type OpenCount = {
  surface: Room;
  rows: number;
  requests: number;
};

function isThenable(value: unknown): value is Promise<unknown> {
  return (
    typeof value === "object" &&
    value !== null &&
    "then" in value &&
    typeof (value as { then?: unknown }).then === "function"
  );
}

function countingCockpitApi(overrides: Partial<CockpitApi> = {}): {
  api: CockpitApi;
  requestCount: () => number;
  idle: () => boolean;
} {
  const inner = cockpitApiStub(overrides);
  let requests = 0;
  let inFlight = 0;
  const api = new Proxy(inner, {
    get(target, property, receiver) {
      const value = Reflect.get(target, property, receiver) as unknown;
      if (typeof property !== "string" || typeof value !== "function") return value;
      return (...args: unknown[]) => {
        requests += 1;
        const result = (value as (...params: unknown[]) => unknown).apply(target, args);
        if (!isThenable(result)) return result;
        inFlight += 1;
        return Promise.resolve(result).finally(() => {
          inFlight -= 1;
        });
      };
    }
  }) as CockpitApi;
  return { api, requestCount: () => requests, idle: () => inFlight === 0 };
}

function growthLine(fewer: OpenCount, more: OpenCount): string | null {
  if (fewer.surface !== more.surface) {
    throw new Error(`compared ${fewer.surface} with ${more.surface}`);
  }
  if (more.rows <= fewer.rows) {
    throw new Error("the later open must have more rows");
  }
  if (more.requests <= fewer.requests) return null;
  return `${fewer.surface}: ${fewer.rows} rows → ${fewer.requests} requests; ${more.rows} rows → ${more.requests} requests`;
}

function surfaceOf(line: string): string {
  const cut = line.indexOf(":");
  return cut === -1 ? line : line.slice(0, cut);
}

function pathOf(room: Room): string {
  if (room === "Workbench") return WORKSHOP_DESTINATION.workbench.path;
  if (room === "Catalog") return WORKSHOP_DESTINATION.catalog.path;
  return WORKSHOP_DESTINATION.history.path;
}

function visibleRowCount(room: Room): number {
  if (room === "History") return document.querySelectorAll(".history-row").length;
  if (room === "Catalog") return document.querySelectorAll("li.catalog-tile").length;
  return document.querySelectorAll("a.living-row").length;
}

function hexId(index: number, fill: string): string {
  return index.toString(16).padStart(64, fill);
}

/** The event cursor a run's own public reference must agree with (`validateEventCursor`). */
function eventCursorFor(publicRunReference: string, sequence: number): string {
  return `event1.${publicRunReference.slice("run1.".length)}.${sequence}`;
}

function minutesAgo(minutes: number): string {
  return new Date(Date.now() - minutes * 60_000).toISOString();
}

function historyRun(index: number): RunV3 {
  const runId = `history-${index}`;
  return {
    workflow_format_version: 3,
    run_id: runId,
    public_run_reference: encodePublicRunReference(runId),
    // Each row names its own revision hash (#1045 REVISE O1): a reintroduced
    // per-hash getWorkflowRevision would then scale with the row count and
    // this suite's own growth assertions would catch it again.
    workflow_revision_hash: hexId(index, "1"),
    workflow_name: "Two agents in a line",
    agent_binding_set_hash: "b".repeat(64),
    run_configuration_revision_hash: "c".repeat(64),
    agent_bindings: [],
    orders: [],
    work_item_reference: null,
    answer: { kind: "value", value_base64: btoa('{"answer":"ok"}'), value_hash: "f".repeat(64) },
    refusal_output: null,
    state_version: 1,
    state: "COMPLETED",
    current_node_id: "final",
    node_rail: [{ node_id: "final", state: "succeeded", attempt: null }],
    cancellation: notCancellableBlock("already-ended"),
    terminal_hash: revisionHash,
    latest_event_cursor: null,
    started_at: minutesAgo(38 + index),
    ended_at: minutesAgo(index),
    current_node_execution_id: revisionHash
  };
}

function catalogRevision(index: number): WorkflowRevisionSummary {
  return {
    workflow_revision_hash: hexId(index, "0"),
    workflow_format_version: 3,
    executable: true,
    not_executable_reason: null,
    name: `wf${index}`,
    description: "build",
    provenance: null
  };
}

function historyApi(rows: number): Partial<CockpitApi> {
  const runs = Array.from({ length: rows }, (_, index) => historyRun(index));
  return {
    listRuns: vi.fn(async (_after?: string, state?: RunV3["state"]) => ({
      items: state === "COMPLETED" ? runs.map(runRow) : [],
      next_after: null
    }))
  };
}

function catalogApi(rows: number): Partial<CockpitApi> {
  const items = Array.from({ length: rows }, (_, index) => catalogRevision(index));
  return {
    listWorkflowRevisions: vi.fn(async () => ({
      items,
      next_after_revision_hash: null
    })),
    getRevisionByName: vi.fn(async (_kind: CatalogLineageKind, name: string) => {
      const item = items.find((revision) => revision.name === name);
      if (item === undefined || item.name === null) throw new Error(`no catalog name ${name}`);
      return {
        display_name: item.name,
        lineage_id: hexId(items.indexOf(item), "e"),
        catalog_revision_hash: item.workflow_revision_hash,
        revision_number: 1
      };
    })
  };
}

function workbenchApi(rows: number): Partial<CockpitApi> {
  const runs = Array.from({ length: rows }, (_, index) => {
    const runId = `shelf-${index}`;
    return startedRun({
      run_id: runId,
      public_run_reference: encodePublicRunReference(runId),
      // Each row names its own revision hash (#1148 REVISE M3, same
      // rationale as `historyRun`): a per-hash read fan-out on open would
      // then scale with the row count and this suite's own growth assertions
      // would catch it -- a shared hash across every row would hide it.
      workflow_revision_hash: hexId(index, "2")
    });
  });
  return {
    listRuns: vi.fn(async (_after?: string, state?: RunV3["state"]) => ({
      items: state === "STARTED" ? runs.map(runRow) : [],
      next_after: null
    })),
    // GET /events replays every durable attention-kind event with no cursor
    // to resume from, so a fresh connect hands back one frame per run this
    // dataset ever named -- exactly what a real connect does today (#1148
    // Befund: 83 requests on an 83-run Workbench). This fake reproduces that
    // replay so a per-run read the burst would otherwise hide shows up here.
    openAttentionEvents: vi.fn((handlers) => {
      handlers.opened();
      runs.forEach((run, index) => {
        const sequence = index + 1;
        handlers.event(
          JSON.stringify(
            waitingInput(sequence, {
              public_run_reference: run.public_run_reference,
              cursor: eventCursorFor(run.public_run_reference, sequence)
            })
          )
        );
      });
      return { close: vi.fn() };
    }),
    getRun: vi.fn(async (reference: string) =>
      runs.find((run) => run.public_run_reference === reference) ??
      startedRun({ public_run_reference: reference })
    )
  };
}

function apiFor(room: Room, rows: number): Partial<CockpitApi> {
  if (room === "History") return historyApi(rows);
  if (room === "Catalog") return catalogApi(rows);
  return workbenchApi(rows);
}

async function measureOpen(room: Room, rows: number): Promise<OpenCount> {
  const counting = countingCockpitApi(apiFor(room, rows));
  window.history.replaceState(null, "", pathOf(room));
  render(App, {
    props: {
      cockpitApi: counting.api,
      mutationJournal: new MutationJournal(sessionStorage)
    }
  });
  try {
    // A room fed by an attention stream can drain a burst of nudges across
    // several sequential reads, each with its own idle gap between requests
    // in flight (#1148): the first quiet moment is not necessarily the last
    // one, so this waits for two consecutive idle reads to agree on the same
    // count rather than trusting the first.
    let previousCount = -1;
    await waitFor(() => {
      expect(visibleRowCount(room)).toBe(rows);
      expect(counting.idle()).toBe(true);
      const currentCount = counting.requestCount();
      const settled = currentCount === previousCount;
      previousCount = currentCount;
      expect(settled).toBe(true);
    });
    return { surface: room, rows, requests: counting.requestCount() };
  } finally {
    cleanup();
  }
}

afterEach(() => {
  vi.restoreAllMocks();
  cleanup();
  window.history.replaceState(null, "", "/atelier");
});

describe("a surface asks once for what it shows", () => {
  it("names the surface, row counts, and request counts when a later open asks more", () => {
    expect(
      growthLine(
        { surface: "History", rows: 2, requests: 8 },
        { surface: "History", rows: 5, requests: 11 }
      )
    ).toBe("History: 2 rows → 8 requests; 5 rows → 11 requests");
  });

  it("is silent when the request count stays put as the rows grow", () => {
    expect(
      growthLine(
        { surface: "Workbench", rows: 2, requests: 9 },
        { surface: "Workbench", rows: 5, requests: 9 }
      )
    ).toBeNull();
  });

  it("stays green on History rows, which read their workflow, work item and result off the row itself", async () => {
    const fewer = await measureOpen("History", FEWER_ROWS);
    const more = await measureOpen("History", MORE_ROWS);
    expect(more.requests).toBe(fewer.requests);
    expect(growthLine(fewer, more)).toBeNull();
  });

  it("stays green on Workbench living-shelf rows, which ask the same number twice", async () => {
    const fewer = await measureOpen("Workbench", FEWER_ROWS);
    const more = await measureOpen("Workbench", MORE_ROWS);
    expect(more.requests).toBe(fewer.requests);
    expect(growthLine(fewer, more)).toBeNull();
  });

  it("proves(a-surface-asks-once-for-what-it-shows): opening a room does not grow API requests with its row count", async () => {
    const rooms: Room[] = ["Workbench", "Catalog", "History"];
    const violations: string[] = [];
    for (const room of rooms) {
      const line = growthLine(
        await measureOpen(room, FEWER_ROWS),
        await measureOpen(room, MORE_ROWS)
      );
      if (line !== null) violations.push(line);
    }
    expect(violations.map(surfaceOf).sort(), violations.join("\n")).toEqual([
      ...NAMED_RESIDUAL_SURFACES
    ]);
  });
});
