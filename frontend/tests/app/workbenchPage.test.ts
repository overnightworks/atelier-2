import type * as SvelteTestingLibrary from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { CockpitApi, RunV3, WorkflowRevisionDetail } from "../../src/api/client";
import { railCopy } from "../../src/lib/railCopy";
import { retryLabel } from "../../src/lib/readStateCopy";
import { seatCopy } from "../../src/lib/seatCopy";
import { workbenchPageCopy } from "../../src/lib/workbenchPageCopy";
import { workbenchQuestions } from "../../src/lib/workbenchQuestions";
import {
  describeWorkbenchControl,
  questionForWorkbenchControl,
  unansweredWorkbenchControls,
  workbenchInteractiveSelector,
  workbenchStageSelector
} from "../support/workbenchControls";
import { FakeRunEventFeed, PAGE_CURSORS, seatResource } from "../support/cockpitApi";
import { cancellableBlock } from "../support/runV3";
import {
  defectiveRunRow,
  runRow,
  startedRun,
  waitingInput,
  waitingInputRun,
  waitingReconciliationRun
} from "../support/runV3";

/**
 * Every test gets a freshly reset module graph -- exactly what a real reload
 * gives the operator, and what keeps a store another test wrote out of this
 * one. `vi.resetModules()` plus a fresh dynamic import of testing-library
 * alongside the app keeps every piece bound to the same reloaded Svelte
 * runtime; mixing a freshly reset component with a stale `render` from a
 * different runtime instance fails.
 */
let testingLibrary: typeof SvelteTestingLibrary;
let openWorkbenchApp: (overrides?: Partial<CockpitApi>) => void;

async function bootApp(): Promise<{
  testingLibrary: typeof SvelteTestingLibrary;
  openWorkbenchApp: (overrides?: Partial<CockpitApi>) => void;
}> {
  vi.resetModules();
  const library = await import("@testing-library/svelte");
  const { default: App } = await import("../../src/App.svelte");
  const { MutationJournal } = await import("../../src/lib/mutationJournal");
  const { cockpitApiStub } = await import("../support/cockpitApi");

  return {
    testingLibrary: library,
    openWorkbenchApp: (overrides: Partial<CockpitApi> = {}) =>
      library.render(App, {
        props: {
          cockpitApi: cockpitApiStub(overrides),
          mutationJournal: new MutationJournal(sessionStorage)
        }
      })
  };
}

beforeEach(async () => {
  sessionStorage.clear();
  window.history.replaceState(null, "", "/atelier/chat");

  ({ testingLibrary, openWorkbenchApp } = await bootApp());
});

afterEach(() => testingLibrary.cleanup());

describe("the workbench seats the operator at a terminal (#1099)", () => {
  const SEAT_URL = "http://127.0.0.1:7681/seat-9Kx2/";

  function openSeatRoom(overrides: Partial<CockpitApi> = {}): void {
    window.history.replaceState(null, "", "/atelier/chat");
    openWorkbenchApp(overrides);
  }

  it("frames the terminal at the address the serve named, under the project it belongs to", async () => {
    openSeatRoom({
      getSeat: vi.fn(async () =>
        seatResource({ state: "ALIVE", url: SEAT_URL, project_id: "atelier-2" })
      )
    });
    const { screen } = testingLibrary;

    const seat = await screen.findByRole("region", { name: seatCopy.regionLabel });
    const terminal = await screen.findByTitle(seatCopy.terminalTitle);
    expect(terminal.getAttribute("src")).toBe(SEAT_URL);
    expect(seat.textContent).toContain("atelier-2");
    expect(seat.textContent).toContain(seatCopy.trustBoundary);
  });

  it("says it is connecting rather than framing an empty surface while the read is still out", async () => {
    openSeatRoom({ getSeat: vi.fn(() => new Promise<never>(() => {})) });
    const { screen } = testingLibrary;

    expect((await screen.findByText(seatCopy.connecting)).isConnected).toBe(true);
    expect(screen.queryByTitle(seatCopy.terminalTitle)).toBeNull();
  });

  it("refuses in one sentence with the way out when no terminal answers, and leaves the room beside it working", async () => {
    const moving = startedRun({ public_run_reference: "run1.YQ", run_id: "still moving" });
    openSeatRoom({
      listRuns: vi.fn(async (_after?: string, state?: string) => ({
        items: state === "STARTED" ? [moving].map(runRow) : [],
        next_after: null
      })),
      getSeat: vi.fn(async () => seatResource({ state: "FAILED" }))
    });
    const { screen } = testingLibrary;

    expect((await screen.findByText(seatCopy.unreachableTitle)).isConnected).toBe(true);
    expect(screen.getByText(seatCopy.unreachableDetail).isConnected).toBe(true);
    expect(screen.queryByTitle(seatCopy.terminalTitle)).toBeNull();
    // The room does not fall over with the seat: what is moving still stands
    // on the shelf, one click from its graph.
    expect((await screen.findByRole("link", { name: /still moving/ })).isConnected).toBe(true);
  });

  it("keeps no trace of the web chat: no composer, no transcript, no way back to one", async () => {
    openSeatRoom({
      getSeat: vi.fn(async () =>
        seatResource({ state: "ALIVE", url: SEAT_URL, project_id: "atelier-2" })
      )
    });
    const { screen } = testingLibrary;

    await screen.findByTitle(seatCopy.terminalTitle);
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.queryByRole("button", { name: "Send" })).toBeNull();
    expect(screen.queryByRole("list", { name: "Conversation" })).toBeNull();
  });
});

describe("the workbench is the room the workshop opens on", () => {
  /**
   * A source is either the fixed set an ordinary open reads, or a getter a
   * test can point at a variable it reassigns after an attention event
   * (#1148): the room's own reload always asks this same double again, so
   * a test names what the *next* ask answers rather than mocking a read of
   * one run apart from the list.
   */
  function listRunsByState(source: readonly RunV3[] | (() => readonly RunV3[])) {
    return vi.fn(async (_after?: string, state?: string) => {
      const runs = typeof source === "function" ? source() : source;
      return {
        items: (state === undefined ? runs : runs.filter((run) => run.state === state)).map(
          runRow
        ),
        next_after: null
      };
    });
  }

  function openRoom(runs: readonly RunV3[] = [], overrides: Partial<CockpitApi> = {}): void {
    window.history.replaceState(null, "", "/atelier");
    openWorkbenchApp({ listRuns: listRunsByState(runs), ...overrides });
  }

  // The identifier stays "the-workshop-opens-in-the-studio" (acceptance/131):
  // the room it names is the Workbench since ADR 0019 retired the Board.
  it("proves(the-workshop-opens-in-the-studio): opens the bare atelier path in the Workbench instead of a list of runs", async () => {
    openRoom();
    const { screen } = testingLibrary;

    expect((await screen.findByRole("heading", { name: "Workbench" })).isConnected).toBe(true);
    expect(screen.queryByRole("heading", { name: "Board" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "Runs" })).toBeNull();
    expect(window.location.pathname).toBe("/atelier");
  });

  // Same identifier note (acceptance/131): the inbox is the Workbench's pinned
  // stage and the rows beneath it.
  it("proves(the-inbox-names-every-run-that-waits-for-a-human): names every run in a durable waiting state, across every page the list holds, and leads to each in one click", async () => {
    // "Across everything" is only true while the reading spans the durable
    // pages: a run that waits on the second page is exactly the one a
    // single-page read would lose.
    openRoom([], {
      listRuns: vi.fn(async (after?: string, state?: string) => {
        if (state === "WAITING_INPUT") {
          return after === undefined
            ? {
                items: [runRow(waitingInputRun({ public_run_reference: "run1.Yg" }))],
                next_after: PAGE_CURSORS[0] ?? null
              }
            : {
                items: [runRow(waitingInputRun({ public_run_reference: "run1.YQ" }))],
                next_after: null
              };
        }
        if (state === "WAITING_RECONCILIATION") {
          return {
            items: [runRow(waitingReconciliationRun({ public_run_reference: "run1.Yw" }))],
            next_after: null
          };
        }
        return { items: [], next_after: null };
      })
    });
    const { fireEvent, screen, waitFor, within } = testingLibrary;

    // A waiting decision stands pinned in the Open-decisions region, one card
    // per run; a reconciliation this room cannot answer inline stays a row.
    const pinnedRegion = await screen.findByRole("region", { name: "Open decisions" });
    await waitFor(() => {
      expect(within(pinnedRegion).getAllByRole("listitem")).toHaveLength(2);
    });
    expect(screen.getByText(/Reconcile →/).isConnected).toBe(true);
    expect(screen.queryByText(/Running/)).toBeNull();

    await fireEvent.click(screen.getByText(/Reconcile →/));
    await waitFor(() => expect(window.location.pathname).toBe("/atelier/runs/run1.Yw"));
  });

  it("lays what is moving on the shelf beneath, one click from its graph", async () => {
    openRoom([startedRun({ public_run_reference: "run1.YQ", run_id: "rebuild the index" })]);
    const { fireEvent, screen, waitFor } = testingLibrary;

    const row = await screen.findByRole("link", { name: /rebuild the index/ });
    // The node at work is the row's own fact; no state word repeats the mark
    // beside it (ADR 0019 §3).
    expect(row.textContent).toContain("agent");
    expect(row.textContent).not.toContain("Running");

    await fireEvent.click(row);
    await waitFor(() => expect(window.location.pathname).toBe("/atelier/runs/run1.YQ"));
  });

  it("shows a run whose own projection failed as a defective row beside its healthy neighbours (#1042)", async () => {
    openRoom([], {
      listRuns: vi.fn(async (_after?: string, state?: string) => ({
        items:
          state === "STARTED"
            ? [
                runRow(startedRun({ public_run_reference: "run1.YQ", run_id: "rebuild the index" })),
                defectiveRunRow({
                  public_run_reference: "run1.Yg",
                  detail: "run current node is absent from its workflow graph"
                })
              ]
            : [],
        next_after: null
      }))
    });
    const { screen, within } = testingLibrary;

    // The healthy neighbour still opens its graph -- one row's own defect
    // never dims the ones beside it.
    expect((await screen.findByRole("link", { name: /rebuild the index/ })).isConnected).toBe(true);

    const defectiveList = await screen.findByRole("list", {
      name: workbenchPageCopy.defectiveRunsLabel
    });
    expect(within(defectiveList).getAllByRole("listitem")).toHaveLength(1);
    expect(within(defectiveList).getByText(workbenchPageCopy.defectiveRunTitle)).toBeTruthy();
    // Nothing here opens: there is no graph this room can show for a run it
    // could not read.
    expect(within(defectiveList).queryByRole("link")).toBeNull();
  });

  // The three state lists are asked at once and answered separately, so a run
  // that opens a wait while the started list is still on the wire comes back in
  // two of them.
  it("shows a run once when two of the three reads answer with it, and keeps the fresher truth", async () => {
    openRoom([], {
      listRuns: vi.fn(async (_after?: string, state?: string) => {
        if (state === "STARTED") {
          return {
            items: [
              runRow(startedRun({ public_run_reference: "run1.YQ", run_id: "moving run" }))
            ],
            next_after: null
          };
        }
        if (state === "WAITING_INPUT") {
          return {
            items: [
              runRow(
                waitingInputRun({
                  public_run_reference: "run1.YQ",
                  run_id: "moving run",
                  state_version: 2
                })
              )
            ],
            next_after: null
          };
        }
        return { items: [], next_after: null };
      })
    });
    const { screen, waitFor, within } = testingLibrary;

    // The fresher read waits, so the run stands once -- as a pinned decision,
    // never also as a moving row.
    const pinnedRegion = await screen.findByRole("region", { name: "Open decisions" });
    await waitFor(() => {
      expect(within(pinnedRegion).getAllByText(/moving run/)).toHaveLength(1);
    });
    expect(screen.queryByRole("link", { name: /moving run/ })).toBeNull();
  });

  /**
   * The room holds the attention stream the Board used to hold, so a decision
   * that opens while the operator is sitting here arrives where it belongs.
   * The frame is only a nudge: what the room shows is the canonical read.
   */
  const waitingDecisionQuestion = "Ship it, or hold it back?";
  const waitingDecisionRevisionHash = "a".repeat(64);

  function waitingV3Run(overrides: Partial<RunV3> = {}): RunV3 {
    return {
      workflow_format_version: 3,
      run_id: "v3/decide",
      public_run_reference: "run1.YQ",
      workflow_revision_hash: waitingDecisionRevisionHash,
      workflow_name: "decide",
      agent_binding_set_hash: "b".repeat(64),
      run_configuration_revision_hash: "c".repeat(64),
      agent_bindings: [],
      orders: [],
      state_version: 1,
      state: "WAITING_INPUT",
      current_node_id: "approve",
      node_rail: [{ node_id: "approve", state: "needs_you", attempt: null }],
      // A resting Wait is operator-cancellable (#668).
      cancellation: cancellableBlock(),
      terminal_hash: null,
      latest_event_cursor: null,
      started_at: "2026-08-18T15:00:00Z",
      ended_at: null,
      ...overrides,
      current_node_execution_id: overrides.current_node_execution_id ?? waitingDecisionRevisionHash
    };
  }

  function waitingV3Revision(): WorkflowRevisionDetail {
    return {
      workflow_revision_hash: waitingDecisionRevisionHash,
      document_base64: "YQ==",
      graph: {
        workflow_format_version: 3,
        executable: true,
        not_executable_reason: null,
        node_count: 1,
        agent_roles: [],
        orders: [],
        wait_answer_schemas: [
          {
            node_id: "approve",
            schema: { ref: "decision", revision: "e".repeat(64) },
            kind: "boolean",
            string_typed: false,
            values: null
          }
        ],
        node_previews: [
          { id: "approve", kind: "wait", role: null, instruction_start: null, depends_on: [] }
        ],
        loops: [],
        name: "Approve once",
        description: null
      }
    } as WorkflowRevisionDetail;
  }

  function waitingV3QuestionDetail() {
    return {
      run_id: "v3/decide",
      public_run_reference: "run1.YQ",
      node_id: "approve",
      state: "needs_you",
      job_base64: btoa(waitingDecisionQuestion),
      job_hash: "e".repeat(64),
      answer: null,
      provenance: null,
      refusal: null
    };
  }

  function openWaitingCard(runs: readonly RunV3[], overrides: Partial<CockpitApi> = {}): void {
    openRoom(runs, {
      getNodeDetail: vi.fn(async () => waitingV3QuestionDetail() as never),
      getWorkflowRevision: vi.fn(async () => waitingV3Revision()),
      ...overrides
    });
  }

  it("shows a decision that opens while the operator is looking, without a reload", async () => {
    const feed = new FakeRunEventFeed();
    const opened = waitingInputRun({ public_run_reference: "run1.YQ", run_id: "opened while here" });
    // The event names which run changed; what the room shows for it comes
    // off a fresh read of the same run list every open reads (#1148), not a
    // read of that one run alone.
    let runs: RunV3[] = [];
    const listRuns = listRunsByState(() => runs);
    openRoom([], { listRuns, openAttentionEvents: feed.openAttention });
    const { screen } = testingLibrary;
    await screen.findByRole("heading", { name: "Workbench" });
    feed.handlers?.opened();

    runs = [opened];
    feed.handlers?.event(
      JSON.stringify(waitingInput(1, { public_run_reference: "run1.YQ", cursor: "event1.YQ.1" }))
    );

    expect((await screen.findByText(/opened while here/)).isConnected).toBe(true);
    // The rail's number counts the same truth, from the same read.
    expect((await screen.findByLabelText(`1 ${railCopy.needsYouCountSuffix}`)).isConnected).toBe(
      true
    );
    expect(window.location.pathname).toBe("/atelier");
  });

  it("keeps an open decision card through a stream drop and takes the next one after recover, without a reload", async () => {
    const feed = new FakeRunEventFeed();
    const first = waitingV3Run({
      public_run_reference: "run1.YQ",
      run_id: "still waiting"
    });
    const recovered = waitingV3Run({
      public_run_reference: "run1.Yg",
      run_id: "after recover"
    });
    let runs: RunV3[] = [first];
    const listRuns = listRunsByState(() => runs);
    openWaitingCard([], { listRuns, openAttentionEvents: feed.openAttention });
    const { screen, waitFor } = testingLibrary;

    expect(
      (await screen.findByRole("region", { name: waitingDecisionQuestion })).isConnected
    ).toBe(true);
    feed.handlers?.opened();

    const pathname = window.location.pathname;

    feed.handlers?.disconnected();
    expect(screen.getByRole("region", { name: waitingDecisionQuestion }).isConnected).toBe(true);
    expect(screen.queryByText("Reconnecting")).toBeNull();

    feed.handlers?.opened();
    runs = [first, recovered];
    feed.handlers?.event(
      JSON.stringify(
        waitingInput(1, {
          public_run_reference: recovered.public_run_reference,
          cursor: "event1.Yg.1"
        })
      )
    );

    await waitFor(() => {
      expect(screen.getByText(/still waiting/).isConnected).toBe(true);
      expect(screen.getByText(/after recover/).isConnected).toBe(true);
      expect(screen.getAllByRole("region", { name: waitingDecisionQuestion })).toHaveLength(2);
    });
    expect(window.location.pathname).toBe(pathname);
  });

  it("says plainly when a nudge's own reload could not be read, and offers one move", async () => {
    const feed = new FakeRunEventFeed();
    const opened = waitingInputRun({ public_run_reference: "run1.YQ", run_id: "read on the second ask" });
    let failNextReload = false;
    let runs: RunV3[] = [];
    const listRuns = vi.fn(async (_after?: string, state?: RunV3["state"]) => {
      if (failNextReload) throw new Error("runs missing");
      return {
        items: (state === undefined ? runs : runs.filter((run) => run.state === state)).map(runRow),
        next_after: null
      };
    });
    openRoom([], { listRuns, openAttentionEvents: feed.openAttention });
    const { fireEvent, screen } = testingLibrary;
    await screen.findByRole("heading", { name: "Workbench" });
    feed.handlers?.opened();

    failNextReload = true;
    feed.handlers?.event(
      JSON.stringify(waitingInput(1, { public_run_reference: "run1.YQ", cursor: "event1.YQ.1" }))
    );
    expect((await screen.findByText(workbenchPageCopy.runsUnavailable)).isConnected).toBe(true);

    // The one move repeats exactly the read that failed, and nothing else.
    failNextReload = false;
    runs = [opened];
    await fireEvent.click(screen.getByRole("button", { name: retryLabel(workbenchPageCopy.runsLabel) }));

    expect((await screen.findByText(/read on the second ask/)).isConnected).toBe(true);
    expect(screen.queryByText(workbenchPageCopy.runsUnavailable)).toBeNull();
  });

  it("names a row by the catalog's workflow name, and falls back to the run id when the catalog names nothing", async () => {
    openRoom(
      [
        startedRun({
          public_run_reference: "run1.YQ",
          run_id: "named",
          workflow_revision_hash: "b".repeat(64)
        }),
        startedRun({ public_run_reference: "run1.Yg", run_id: "unnamed" })
      ],
      {
        listWorkflowRevisions: vi.fn(async () => ({
          items: [
            {
              workflow_revision_hash: "b".repeat(64),
              workflow_format_version: 3 as const,
              executable: true,
              not_executable_reason: null,
              name: "Preview door",
              description: null
            }
          ],
          next_after_revision_hash: null
        }))
      }
    );
    const { screen } = testingLibrary;

    expect((await screen.findByText("Preview door")).isConnected).toBe(true);
    expect(screen.getByText("unnamed").isConnected).toBe(true);
  });

  it("carries the ochre count in the rail only while something waits, and never a fabricated zero", async () => {
    openRoom([startedRun({ public_run_reference: "run1.YQ" })]);
    const { screen, within } = testingLibrary;

    await screen.findByRole("link", { name: /run/ });
    const rail = screen.getByRole("navigation", { name: "Workshop" });
    expect(within(rail).queryByLabelText(/needs you/)).toBeNull();

    testingLibrary.cleanup();
    openRoom([waitingReconciliationRun({ public_run_reference: "run1.Yw" })]);

    const counted = await screen.findByLabelText(`1 ${railCopy.needsYouCountSuffix}`);
    expect(counted.textContent).toBe("1");
  });

  it("asks the durable list by every non-terminal state, and reads the workflow catalog beside it", async () => {
    const listRuns = listRunsByState([startedRun()]);
    const listWorkflowRevisions = vi.fn(async () => ({
      items: [],
      next_after_revision_hash: null
    }));
    window.history.replaceState(null, "", "/atelier");
    openWorkbenchApp({ listRuns, listWorkflowRevisions });
    const { screen } = testingLibrary;
    await screen.findByRole("link", { name: /run/ });

    expect(listRuns.mock.calls.map(([, state]) => state).sort()).toEqual([
      "STARTED",
      "WAITING_INPUT",
      "WAITING_RECONCILIATION"
    ]);
    expect(listWorkflowRevisions).toHaveBeenCalled();
  });

  // The identifier stays "an-empty-area-names-the-one-next-action"
  // (acceptance/131); the one action possible today is the Catalog.
  it("proves(an-empty-area-names-the-one-next-action): names the one next action possible today, and offers it once", async () => {
    openRoom();
    const { fireEvent, screen } = testingLibrary;

    await screen.findByRole("heading", { name: workbenchPageCopy.emptyTitle });
    expect(screen.getAllByRole("link", { name: workbenchPageCopy.emptyStart })).toHaveLength(1);

    await fireEvent.click(screen.getByRole("link", { name: workbenchPageCopy.emptyStart }));

    expect((await screen.findByRole("heading", { name: "Catalog" })).isConnected).toBe(true);
  });

  it("proves(every-rendered-workbench-control-is-inventoried): every rendered Workbench control is inventoried with a question-shaped entry, and a control without an entry fails", async () => {
    const ids = Object.values(workbenchQuestions).map((entry) => entry.id);
    expect(new Set(ids).size).toBe(ids.length);
    for (const entry of Object.values(workbenchQuestions)) {
      expect(entry.question.endsWith("?")).toBe(true);
    }
    const { screen } = testingLibrary;

    openRoom([startedRun({ public_run_reference: "run1.YQ" })]);
    await screen.findByRole("link", { name: /run/ });
    expectWorkbenchControlsAreInventoried([workbenchQuestions.openRun.id]);

    testingLibrary.cleanup();
    openRoom([], { listRuns: vi.fn().mockRejectedValue(new Error("wire detail")) });
    await screen.findByRole("button", {
      name: retryLabel(workbenchPageCopy.runsLabel)
    });
    // A read that failed names no empty room: `ReadState`'s own retry is the
    // only control the room offers until it lands.
    expectWorkbenchControlsAreInventoried([workbenchQuestions.reloadWorkbenchRuns.id]);

    const stage = document.querySelector(workbenchStageSelector);
    if (stage === null) {
      throw new Error("the Workbench stage is missing");
    }
    const stray = document.createElement("button");
    stray.setAttribute("aria-label", "Exact time");
    stage.append(stray);
    const unanswered = unansweredWorkbenchControls(stage).map(describeWorkbenchControl);
    expect(unanswered).toEqual(["button Exact time"]);
  });

  function expectWorkbenchControlsAreInventoried(expected: readonly string[]): void {
    const stage = document.querySelector(workbenchStageSelector);
    if (stage === null) {
      throw new Error("the Workbench stage is missing");
    }
    const unanswered = unansweredWorkbenchControls(stage);
    expect(
      unanswered.map(describeWorkbenchControl),
      unanswered.map(describeWorkbenchControl).join("; ")
    ).toEqual([]);
    const present = [...stage.querySelectorAll(workbenchInteractiveSelector)].map((element) => {
      const found = questionForWorkbenchControl(element);
      if (found === null) {
        throw new Error(`unmapped Workbench control: ${describeWorkbenchControl(element)}`);
      }
      return found.id;
    });
    expect(new Set(present)).toEqual(new Set(expected));
  }
});
