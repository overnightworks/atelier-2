import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../../src/App.svelte";
import {
  CockpitRequestError,
  RUN_NOT_CANCELLABLE_REASONS,
  type CockpitApi,
  type RunV3
} from "../../src/api/client";
import RunCancelCard from "../../src/components/RunCancelCard.svelte";
import { prepareCancel } from "../../src/lib/cancelRunDelivery";
import { shortFingerprint } from "../../src/lib/fingerprint";
import { cancelMutationId, MutationJournal } from "../../src/lib/mutationJournal";
import { backLinkCopy } from "../../src/lib/backLinkCopy";
import { runHeaderCopy } from "../../src/lib/runPages";
import { cancelReasonSentence, runPageCopy } from "../../src/lib/runPageCopy";
import { runPath } from "../../src/lib/route";
import { nodeAriaName, stateLabels } from "../../src/lib/stateMarkCopy";
import { workflowGraphCopy } from "../../src/lib/workflowGraphCopy";
import { cockpitApiStub, FakeRunEventFeed } from "../support/cockpitApi";
import {
  cancellableBlock,
  eventCursor,
  notCancellableBlock,
  publicReference,
  revisionHash as digest
} from "../support/runV3";

const configurationHash = "c".repeat(64);
const terminalHash = "d".repeat(64);

function v3Revision() {
  return {
    workflow_revision_hash: digest,
    document_base64: "YQ==",
    provenance: null,
    graph: {
      workflow_format_version: 3 as const,
      executable: true as const,
      not_executable_reason: null,
      node_count: 2,
      agent_roles: ["builder"],
      orders: [],
      wait_answer_schemas: [],
      node_previews: [
        {
          id: "implement",
          kind: "agent" as const,
          role: "builder",
          instruction_start: "Do the one thing this chain is for.",
          depends_on: []
        },
        {
          id: "review",
          kind: "agent" as const,
          role: "builder",
          instruction_start: "Check what the node before you did.",
          depends_on: ["implement"]
        }
      ],
      loops: [],
      name: "Two agents in a line",
      description: null
    }
  };
}

function v3Run(overrides: Partial<RunV3> = {}): RunV3 {
  return {
    workflow_format_version: 3,
    run_id: "v3/two-agents",
    public_run_reference: publicReference,
    workflow_revision_hash: digest,
    workflow_name: "Two agents in a line",
    agent_binding_set_hash: "b".repeat(64),
    run_configuration_revision_hash: configurationHash,
    agent_bindings: [],
    orders: [],
    state_version: 1,
    state: "STARTED",
    current_node_id: "review",
    node_rail: [
      { node_id: "implement", state: "succeeded", attempt: null },
      { node_id: "review", state: "working", attempt: null }
    ],
    cancellation: cancellableBlock(),
    terminal_hash: null,
    latest_event_cursor: null,
    started_at: "2026-08-18T15:00:00Z",
    ended_at: null,
    ...overrides,
    current_node_execution_id: overrides.current_node_execution_id ?? digest
  };
}

function api(run: RunV3, overrides: Partial<CockpitApi> = {}): CockpitApi {
  return cockpitApiStub({
    getRun: vi.fn(async () => run),
    getWorkflowRevision: vi.fn(async () => v3Revision()),
    ...overrides
  });
}

/** Opens a node and moves to one of its tabs, the way a reader does. */
async function openNodeTab(nodeName: RegExp | string, tab: string): Promise<void> {
  await fireEvent.click(await screen.findByRole("button", { name: nodeName }));
  await fireEvent.click(await screen.findByRole("tab", { name: tab }));
}

beforeEach(() => {
  sessionStorage.clear();
  window.history.replaceState(null, "", `/atelier/runs/${publicReference}`);
});

afterEach(() => cleanup());

/** jsdom has no modal dialog; the same seam RunCancelCard's staged decision uses. */
function stubDialogMethods(): void {
  Object.defineProperties(HTMLDialogElement.prototype, {
    showModal: {
      configurable: true,
      value(this: HTMLDialogElement): void {
        this.open = true;
      }
    },
    close: {
      configurable: true,
      value(this: HTMLDialogElement): void {
        this.open = false;
      }
    }
  });
}

describe("a version 3 run in the cockpit", () => {
  it("proves(a-v3-run-is-visible-in-the-cockpit): shows the line, which node is running, and no proof plumbing on the way", async () => {
    const feed = new FakeRunEventFeed();
    const cockpitApi = api(v3Run(), { openRunEvents: feed.open });

    render(App, {
      props: { cockpitApi, mutationJournal: new MutationJournal(sessionStorage) }
    });

    expect(
      (await screen.findByRole("heading", { level: 1, name: "Two agents in a line" })).isConnected
    ).toBe(true);
    const graph = await screen.findByRole("region", { name: workflowGraphCopy.label });
    expect(within(graph).getByRole("button", { name: nodeAriaName("implement", "succeeded") }).isConnected).toBe(true);
    expect(within(graph).getByRole("button", { name: nodeAriaName("review", "working") }).isConnected).toBe(true);
    expect(screen.getByLabelText(runPageCopy.whereThisRunStands).textContent).toContain("Running");
    // The main surface carries no fingerprints and no "not yet" placeholder:
    // every proof lives one click away, in the node's Evidence tab (operator
    // ruling 23.08.).
    expect(screen.queryByText(/not yet/i)).toBeNull();
    expect(screen.queryByText(configurationHash)).toBeNull();
    expect(screen.queryByRole("group", { name: runPageCopy.runConfiguration })).toBeNull();
    expect(screen.queryByRole("group", { name: runPageCopy.terminalHash })).toBeNull();
    // A loaded run is not a failed one: the page must not offer to fetch it again
    // beneath the answer it already has.
    expect(screen.queryByRole("button", { name: runPageCopy.readAgain })).toBeNull();
  });

  it("proves(a-run-carries-when-it-started-and-ended): shows the run's exact facts in the same line as its state, honestly omitting a timestamp that has not arrived and a duration its own state sentence already carries, with no reveal to find them behind", async () => {
    render(App, {
      props: { cockpitApi: api(v3Run()), mutationJournal: new MutationJournal(sessionStorage) }
    });

    await screen.findByRole("heading", { level: 1, name: "Two agents in a line" });

    const standing = screen.getByLabelText(runPageCopy.whereThisRunStands);
    expect(standing.textContent).toContain("Running");
    expect(standing.textContent).toMatch(/for \d/);
    expect(standing.textContent).toMatch(/started \d/);
    // A run still going never repeats its own "for" reading as a second,
    // separately labelled duration fact (Zeiten-Hierarchie, operator ruling
    // 23.08.).
    expect(standing.textContent).not.toContain("duration");
    expect(standing.textContent).not.toContain("ended");
    expect(screen.queryByText("Exact time")).toBeNull();
  });

  it("proves(a-done-run-reads-the-time-since-it-landed): a finished run's relative reading is how long ago it ended, never how long it took to run", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-08-18T17:00:12Z"));
    try {
      render(App, {
        props: {
          cockpitApi: api(
            v3Run({
              state: "COMPLETED",
              node_rail: [
                { node_id: "implement", state: "succeeded", attempt: null },
                { node_id: "review", state: "succeeded", attempt: null }
              ],
              ended_at: "2026-08-18T15:00:12Z"
            })
          ),
          mutationJournal: new MutationJournal(sessionStorage)
        }
      });

      await screen.findByRole("heading", { level: 1, name: "Two agents in a line" });

      const standing = screen.getByLabelText(runPageCopy.whereThisRunStands);
      expect(standing.textContent).toContain("Done");
      // Two hours passed between the run ending and now -- not the twelve
      // seconds the run itself took to complete.
      expect(standing.textContent).toMatch(/2 h ago/);
      expect(standing.textContent).toMatch(/started .* · ended .* · duration 12 s/);
    } finally {
      vi.useRealTimers();
    }
  });

  it("proves(a-chain-run-is-watched-while-it-runs): moves the node that finished to Done on the one picture of the run", async () => {
    const feed = new FakeRunEventFeed();
    const cockpitApi = api(v3Run(), { openRunEvents: feed.open });

    render(App, {
      props: { cockpitApi, mutationJournal: new MutationJournal(sessionStorage) }
    });
    await screen.findByRole("heading", { level: 1, name: "Two agents in a line" });
    feed.handlers?.opened();
    feed.handlers?.event(
      JSON.stringify(await completedEvent("implement", "the draft", 1))
    );

    const graph = await screen.findByRole("region", { name: workflowGraphCopy.label });
    await waitFor(() =>
      expect(within(graph).getByRole("button", { name: nodeAriaName("implement", "succeeded") }).isConnected).toBe(true)
    );
    // The graph is the one truth about where the run stands; the finished
    // node's output is not pasted beside it.
    expect(document.body.textContent).not.toContain("the draft");
  });

  it("proves(a-v3-stream-closes-only-when-every-event-has-arrived): keeps the stream open until the applied events match the run cursor", async () => {
    const feed = new FakeRunEventFeed();
    const ended = v3Run({
      state: "COMPLETED",
      terminal_hash: terminalHash,
      current_node_id: "review",
      latest_event_cursor: eventCursor(2),
      node_rail: [
        { node_id: "implement", state: "succeeded", attempt: null },
        { node_id: "review", state: "succeeded", attempt: null }
      ]
    });
    const getRun = vi.fn().mockResolvedValueOnce(v3Run()).mockResolvedValue(ended);
    const cockpitApi = api(v3Run(), { getRun, openRunEvents: feed.open });

    render(App, {
      props: { cockpitApi, mutationJournal: new MutationJournal(sessionStorage) }
    });
    await screen.findByRole("heading", { level: 1, name: "Two agents in a line" });
    feed.handlers?.opened();
    feed.handlers?.event(JSON.stringify(await completedEvent("implement", "the draft", 1)));

    const graph = await screen.findByRole("region", { name: workflowGraphCopy.label });
    await waitFor(() =>
      expect(within(graph).getByRole("button", { name: nodeAriaName("implement", "succeeded") }).isConnected).toBe(true)
    );
    expect(feed.close).not.toHaveBeenCalled();

    feed.handlers?.event(JSON.stringify(await completedEvent("review", "looks good", 2)));

    await waitFor(() => expect(feed.close).toHaveBeenCalled());
    expect(screen.getByLabelText(runPageCopy.whereThisRunStands).textContent).toContain("Done");
  });

  it("closes the stream once a run watched live lands on CANCELLED, through the one terminality owner", async () => {
    const feed = new FakeRunEventFeed();
    const cancelled = v3Run({
      state: "CANCELLED",
      cancellation: notCancellableBlock("already-ended"),
      current_node_id: "review",
      latest_event_cursor: eventCursor(1),
      node_rail: [
        { node_id: "implement", state: "succeeded", attempt: null },
        { node_id: "review", state: "queued", attempt: null }
      ]
    });
    const getRun = vi.fn().mockResolvedValueOnce(v3Run()).mockResolvedValue(cancelled);
    const cockpitApi = api(v3Run(), { getRun, openRunEvents: feed.open });

    render(App, {
      props: { cockpitApi, mutationJournal: new MutationJournal(sessionStorage) }
    });
    await screen.findByRole("heading", { level: 1, name: "Two agents in a line" });
    feed.handlers?.opened();
    feed.handlers?.event(JSON.stringify(await completedEvent("implement", "the draft", 1)));

    await waitFor(() => expect(feed.close).toHaveBeenCalled());
    expect(screen.getByLabelText(runPageCopy.whereThisRunStands).textContent).toContain("Cancelled");
    // Closed, not reconnected: the browser's own EventSource retry never fires
    // because the app closed the connection itself.
    expect(feed.open).toHaveBeenCalledTimes(1);
  });

  it("keeps the terminal fingerprint out of the main surface and inside the node's evidence", async () => {
    const cockpitApi = api(
      v3Run({ state: "COMPLETED", terminal_hash: terminalHash, current_node_id: "review" }),
      { getNodeDetail: vi.fn(async () => finishedNodeDetail() as never) }
    );

    render(App, {
      props: { cockpitApi, mutationJournal: new MutationJournal(sessionStorage) }
    });
    await screen.findByRole("heading", { level: 1, name: "Two agents in a line" });

    expect(screen.getByLabelText(runPageCopy.whereThisRunStands).textContent).toContain("Done");
    expect(screen.queryByRole("group", { name: runPageCopy.terminalHash })).toBeNull();

    await openNodeTab(/implement/, runPageCopy.tabEvidence);

    const terminal = await screen.findByRole("group", { name: runPageCopy.terminalHash });
    expect(within(terminal).getByText(shortFingerprint(terminalHash)).isConnected).toBe(true);
  });

  it("asks for the published revision so it can draw the edges, and opens the stream it can now read", async () => {
    const feed = new FakeRunEventFeed();
    const cockpitApi = api(v3Run(), { openRunEvents: feed.open });

    render(App, {
      props: { cockpitApi, mutationJournal: new MutationJournal(sessionStorage) }
    });

    await screen.findByRole("heading", { level: 1, name: "Two agents in a line" });
    await screen.findByRole("region", { name: workflowGraphCopy.label });
    expect(cockpitApi.getWorkflowRevision).toHaveBeenCalledWith(digest);
    expect(feed.open).toHaveBeenCalledTimes(1);
  });

  it("says it is looking while the published graph is still arriving", async () => {
    render(App, {
      props: {
        cockpitApi: api(v3Run(), {
          getWorkflowRevision: vi.fn(() => new Promise<ReturnType<typeof v3Revision>>(() => undefined))
        }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });

    // A V3 graph always declares a name once read; while it is still arriving
    // the title says that honestly instead of falling back to the raw run id.
    // The title and the rail both carry their own loading region (the same
    // shared component, REQ-UIQ-13), so both say the identical sentence.
    await screen.findByRole("heading", { level: 1, name: runPageCopy.looking });
    const statuses = screen.getAllByRole("status");
    expect(statuses.length).toBe(2);
    for (const status of statuses) {
      // Insignificant whitespace from the surrounding `{#if}` block's own
      // indentation collapses visually in a browser; this normalizes it the
      // same way `historyPage.test.ts`'s `visibleResultText` does, so the
      // assertion pins the rendered sentence, not incidental DOM shape.
      expect((status.textContent ?? "").replace(/\s+/g, " ").trim()).toBe(runPageCopy.looking);
    }
    expect(screen.queryByRole("region", { name: workflowGraphCopy.label })).toBeNull();
  });

  it("names a graph that could not be read instead of inventing a line from the rail", async () => {
    render(App, {
      props: {
        cockpitApi: api(v3Run(), {
          getWorkflowRevision: vi.fn(async () => {
            throw new Error("store asleep");
          })
        }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });

    expect((await screen.findByText(runPageCopy.graphUnreadable)).isConnected).toBe(true);
    expect(screen.getByText("store asleep").isConnected).toBe(true);
    expect(screen.queryByRole("region", { name: workflowGraphCopy.label })).toBeNull();
    expect(screen.getByRole("button", { name: /implement/ }).isConnected).toBe(true);
    expect(screen.getByRole("button", { name: /review/ }).isConnected).toBe(true);
    // A graph that could not be read still has no name to show; the title
    // names that state rather than falling back to the raw run id.
    expect(
      screen.getByRole("heading", { level: 1, name: runPageCopy.workflowUnavailable }).isConnected
    ).toBe(true);
  });

  it("leads back to the Workbench without repeating the page's own title", async () => {
    render(App, {
      props: { cockpitApi: api(v3Run()), mutationJournal: new MutationJournal(sessionStorage) }
    });
    await screen.findByRole("heading", { level: 1, name: "Two agents in a line" });

    const back = screen.getByRole("navigation", { name: backLinkCopy.whereYouAre });
    expect(within(back).getAllByRole("link").map((step) => step.textContent?.trim())).toEqual([
      "←Workbench"
    ]);
    expect(within(back).queryByText("Two agents in a line")).toBeNull();
  });
});

describe("a run whose first read has already ended", () => {
  it("opens no event stream and never reconnects for a run already FAILED or CANCELLED", async () => {
    const terminalCases = [
      { state: "FAILED" as const, cancellation: cancellableBlock() },
      { state: "CANCELLED" as const, cancellation: notCancellableBlock("already-ended") }
    ];
    for (const { state, cancellation } of terminalCases) {
      cleanup();
      window.history.replaceState(null, "", `/atelier/runs/${publicReference}`);
      const feed = new FakeRunEventFeed();
      const cockpitApi = api(v3Run({ state, cancellation }), { openRunEvents: feed.open });

      render(App, {
        props: { cockpitApi, mutationJournal: new MutationJournal(sessionStorage) }
      });
      await screen.findByRole("heading", { level: 1, name: "Two agents in a line" });

      expect(feed.open).not.toHaveBeenCalled();
    }
  });

  it("still subscribes to the event stream for a run that is still running", async () => {
    const feed = new FakeRunEventFeed();
    const cockpitApi = api(v3Run({ state: "STARTED" }), { openRunEvents: feed.open });

    render(App, {
      props: { cockpitApi, mutationJournal: new MutationJournal(sessionStorage) }
    });
    await screen.findByRole("heading", { level: 1, name: "Two agents in a line" });

    expect(feed.open).toHaveBeenCalledTimes(1);
  });
});

describe("a started run shows the working node live", () => {
  it("proves(a-started-run-shows-the-working-node-live): the working node is live work, and the log that is not here is named where a log would live", async () => {
    const feed = new FakeRunEventFeed();
    const cockpitApi = api(v3Run(), {
      openRunEvents: feed.open,
      getNodeDetail: vi.fn(async () => workingNodeDetail() as never)
    });

    render(App, {
      props: { cockpitApi, mutationJournal: new MutationJournal(sessionStorage) }
    });

    await screen.findByRole("heading", { level: 1, name: "Two agents in a line" });
    const graph = await screen.findByRole("region", { name: workflowGraphCopy.label });
    const working = within(graph).getByRole("button", { name: nodeAriaName("review", "working") });
    expect(working.getAttribute("data-live")).toBe("true");
    expect(within(graph).getByRole("button", { name: nodeAriaName("implement", "succeeded") }).getAttribute("data-live")).toBeNull();
    expect(screen.queryByRole("progressbar")).toBeNull();
    // Connecting is ordinary loading, not a problem worth a line of its own.
    expect(screen.queryByText(runPageCopy.streamStale)).toBeNull();

    feed.handlers?.opened();
    feed.handlers?.event(JSON.stringify(await completedEvent("implement", "the draft", 1)));

    await openNodeTab(nodeAriaName("review", "working"), runPageCopy.tabLog);
    expect(screen.getByText(runPageCopy.processLogInLease).isConnected).toBe(true);
    expect(screen.getByText(runPageCopy.logAbsent).isConnected).toBe(true);
  });

  it("proves(a-started-run-shows-the-working-node-live): a failed stream says the page is not following, and reading again heals it", async () => {
    const feed = new FakeRunEventFeed();
    render(App, {
      props: {
        cockpitApi: api(v3Run(), { openRunEvents: feed.open }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });
    await screen.findByRole("heading", { level: 1, name: "Two agents in a line" });
    feed.handlers?.opened();
    feed.handlers?.event(
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

    const notice = await screen.findByRole("alert");
    expect(notice.textContent).toContain("Durable state is corrupt");
    expect(screen.getByText(runPageCopy.streamStale).isConnected).toBe(true);

    // A stream that stopped is never a dead end: the one act that can heal it
    // stands right beside the words that say it stopped (operator, 23.08.).
    const readAgain = screen.getByRole("button", { name: runPageCopy.readAgain });
    await fireEvent.click(readAgain);
    await waitFor(() => expect(feed.open).toHaveBeenCalledTimes(2));
    feed.handlers?.opened();
    await waitFor(() => expect(screen.queryByText(runPageCopy.streamStale)).toBeNull());
  });

  it("proves(a-started-run-shows-the-working-node-live): a corrupt event is named as itself", async () => {
    const feed = new FakeRunEventFeed();
    render(App, {
      props: {
        cockpitApi: api(v3Run(), { openRunEvents: feed.open }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });
    await screen.findByRole("heading", { level: 1, name: "Two agents in a line" });
    feed.handlers?.opened();
    feed.handlers?.event("not-json");

    expect((await screen.findByText(runPageCopy.eventInvalid)).isConnected).toBe(true);
    expect(screen.getByText(runPageCopy.streamStale).isConnected).toBe(true);
  });

  it("does not keep a live mark on a finished run", async () => {
    render(App, {
      props: {
        cockpitApi: api(
          v3Run({
            state: "COMPLETED",
            terminal_hash: terminalHash,
            current_node_id: "review",
            node_rail: [
              { node_id: "implement", state: "succeeded", attempt: null },
              { node_id: "review", state: "succeeded", attempt: null }
            ]
          })
        ),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });
    await screen.findByRole("heading", { level: 1, name: "Two agents in a line" });
    expect(document.querySelector("[data-live='true']")).toBeNull();
    expect(screen.getByLabelText(runPageCopy.whereThisRunStands).textContent).toContain("Done");
  });
});

describe("a version 3 run that stops for a person", () => {
  const answer = '"approved, with the second paragraph rewritten"';
  const question = "Approve this, or name the blocking defect.";
  const earlierResult = "Three German sentences about code review.";

  function waitRevision() {
    const revision = v3Revision();
    return {
      ...revision,
      graph: {
        ...revision.graph,
        name: "A person approves last",
        node_previews: [
          revision.graph.node_previews[0]!,
          {
            id: "approve",
            kind: "wait" as const,
            role: null,
            instruction_start: null,
            depends_on: ["implement"]
          }
        ]
      }
    };
  }

  function waitingRun(): RunV3 {
    return v3Run({
      run_id: "v3/a-person-approves",
      state: "WAITING_INPUT",
      current_node_id: "approve",
      // A resting Wait is operator-cancellable (#668).
      cancellation: cancellableBlock(),
      node_rail: [
        { node_id: "implement", state: "succeeded", attempt: null },
        { node_id: "approve", state: "needs_you", attempt: null }
      ]
    });
  }

  function answeredRun(): RunV3 {
    return v3Run({
      run_id: "v3/a-person-approves",
      state: "COMPLETED",
      current_node_id: "approve",
      cancellation: notCancellableBlock("already-ended"),
      node_rail: [
        { node_id: "implement", state: "succeeded", attempt: null },
        { node_id: "approve", state: "succeeded", attempt: null }
      ],
      terminal_hash: terminalHash
    });
  }

  async function waitAnsweredEvent(sequence: number) {
    return {
      workflow_format_version: 3,
      cursor: `event1.cnVu.${sequence}`,
      sequence,
      public_run_reference: publicReference,
      workflow_revision_hash: digest,
      node_id: "approve",
      node_execution_id: "b".repeat(64),
      event_hash: "c".repeat(64),
      node_rail: [{ node_id: "approve", state: "succeeded", attempt: null }],
      event: "WAIT_ANSWERED",
      actor: "operator",
      answer_base64: btoa(answer),
      answer_hash: [
        ...new Uint8Array(
          await crypto.subtle.digest("SHA-256", new TextEncoder().encode(answer))
        )
      ]
        .map((byte) => byte.toString(16).padStart(2, "0"))
        .join("")
    };
  }

  function waitNodeDetail(job: string | null = question) {
    return {
      run_id: "v3/a-person-approves",
      public_run_reference: publicReference,
      node_id: "approve",
      state: "needs_you",
      job_base64: job === null ? null : btoa(job),
      job_hash: job === null ? null : "e".repeat(64),
      answer: null,
      provenance: null,
      refusal: null
    };
  }

  function earlierNodeDetail() {
    return {
      run_id: "v3/a-person-approves",
      public_run_reference: publicReference,
      node_id: "implement",
      state: "succeeded",
      job_base64: btoa("Write three German sentences about code review."),
      job_hash: "e".repeat(64),
      answer: { value_base64: btoa(earlierResult), value_hash: "f".repeat(64) },
      provenance: null,
      refusal: null
    };
  }

  /** Answers the wait node with the question, and every other node with its result. */
  function waitingApi(overrides: Partial<CockpitApi> = {}, job: string | null = question) {
    return api(waitingRun(), {
      getWorkflowRevision: vi.fn(async () => waitRevision()),
      getNodeDetail: vi.fn(async (_reference: string, nodeId: string) =>
        (nodeId === "approve" ? waitNodeDetail(job) : earlierNodeDetail()) as never
      ),
      ...overrides
    });
  }

  it("proves(a-v3-line-stops-for-a-person-and-their-answer-carries-it-on): draws the node that owes a person a move as the one needing them", async () => {
    render(App, {
      props: { cockpitApi: waitingApi(), mutationJournal: new MutationJournal(sessionStorage) }
    });

    const graph = await screen.findByRole("region", { name: workflowGraphCopy.label });
    expect(within(graph).getByRole("button", { name: nodeAriaName("approve", "needs_you") }).isConnected).toBe(
      true
    );
    expect(within(graph).getByRole("button", { name: nodeAriaName("implement", "succeeded") }).isConnected).toBe(true);
    expect(screen.getByLabelText(runPageCopy.whereThisRunStands).textContent).toContain(
      "Waiting for you"
    );
  });

  it("proves(a-resting-wait-still-offers-its-own-cancel): a run resting on an unanswered wait offers the cancel control, not a silent gap where one used to explain itself", async () => {
    render(App, {
      props: { cockpitApi: waitingApi(), mutationJournal: new MutationJournal(sessionStorage) }
    });

    await screen.findByRole("heading", { name: question });
    expect(await screen.findByRole("button", { name: runPageCopy.cancel.open })).toBeTruthy();
  });

  it("proves(a-v3-line-stops-for-a-person-and-their-answer-carries-it-on): carries the page on when the answer arrives, without an answer of its own to settle", async () => {
    const feed = new FakeRunEventFeed();
    const journal = new MutationJournal(sessionStorage);
    const getRun = vi
      .fn()
      .mockResolvedValueOnce(waitingRun())
      .mockResolvedValue(answeredRun());
    const cockpitApi = waitingApi({ getRun, openRunEvents: feed.open });

    render(App, { props: { cockpitApi, mutationJournal: journal } });
    await screen.findByRole("button", { name: nodeAriaName("approve", "needs_you") });
    feed.handlers?.opened();
    feed.handlers?.event(JSON.stringify(await waitAnsweredEvent(1)));

    await waitFor(() =>
      expect(screen.getByRole("button", { name: nodeAriaName("approve", "succeeded") }).isConnected).toBe(true)
    );
    expect(screen.getByLabelText(runPageCopy.whereThisRunStands).textContent).toContain("Done");
    expect(await journal.entries()).toEqual([]);
    expect(screen.queryByText(runPageCopy.runUnavailable)).toBeNull();
  });

  it("proves(a-waiting-v3-run-is-answerable-on-its-run-page): leads with the question and the earlier result it is about", async () => {
    render(App, {
      props: { cockpitApi: waitingApi(), mutationJournal: new MutationJournal(sessionStorage) }
    });

    // The question is the title of the card. "WAIT approve" tells a person
    // nothing they can act on (operator, 23.08.).
    expect(await screen.findByRole("heading", { name: question })).toBeTruthy();
    expect(screen.queryByText("Wait approve")).toBeNull();

    const context = await screen.findByRole("region", { name: runPageCopy.answerContext });
    await waitFor(() =>
      expect(within(context).getByText(earlierResult).isConnected).toBe(true)
    );
    expect(within(context).getByRole("article", { name: "implement" }).isConnected).toBe(true);
  });

  it("proves(a-waiting-v3-run-is-answerable-on-its-run-page): sends plain words as the JSON string the schema expects", async () => {
    const journal = new MutationJournal(sessionStorage);
    const answerCall = vi.fn(async (mutation: { body_base64: string }) => {
      void mutation;
      return { status: 202, value: answeredRun() };
    });

    render(App, {
      props: {
        cockpitApi: waitingApi({ answer: answerCall }),
        mutationJournal: journal
      }
    });

    await screen.findByRole("heading", { name: question });
    await fireEvent.input(screen.getByLabelText(runPageCopy.answerLabel), {
      target: { value: "approved, with the second paragraph rewritten" }
    });
    await fireEvent.click(screen.getByRole("button", { name: runPageCopy.answerSubmit }));

    await waitFor(() => expect(answerCall).toHaveBeenCalledTimes(1));
    const body = JSON.parse(globalThis.atob(answerCall.mock.calls[0]?.[0]?.body_base64 ?? ""));
    expect(body).toEqual({
      workflow_revision_hash: digest,
      node_id: "approve",
      expected_node_execution_id: waitingRun().current_node_execution_id,
      actor: "operator",
      answer_base64: btoa(answer)
    });
  });

  it("proves(a-waiting-v3-run-is-answerable-on-its-run-page): passes an answer already written as JSON through untouched", async () => {
    const answerCall = vi.fn(async (mutation: { body_base64: string }) => {
      void mutation;
      return { status: 202, value: answeredRun() };
    });

    render(App, {
      props: {
        cockpitApi: waitingApi({ answer: answerCall }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });

    await screen.findByRole("heading", { name: question });
    await fireEvent.input(screen.getByLabelText(runPageCopy.answerLabel), {
      target: { value: '{"verdict":"green"}' }
    });
    await fireEvent.click(screen.getByRole("button", { name: runPageCopy.answerSubmit }));

    await waitFor(() => expect(answerCall).toHaveBeenCalledTimes(1));
    const body = JSON.parse(globalThis.atob(answerCall.mock.calls[0]?.[0]?.body_base64 ?? ""));
    expect(body.answer_base64).toBe(btoa('{"verdict":"green"}'));
  });

  /** The published answer schema of #553's decision-button graphs, everything but its classification. */
  function decisionSchema(
    kind: "boolean" | "enum" | "string" | "free",
    values: string[] | null = null,
    stringTyped = false
  ) {
    const revision = waitRevision();
    return {
      ...revision,
      graph: {
        ...revision.graph,
        wait_answer_schemas: [
          {
            node_id: "approve",
            schema: { ref: "decision", revision: "e".repeat(64) },
            kind,
            string_typed: stringTyped,
            values
          }
        ]
      }
    };
  }

  it("proves(a-waiting-v3-run-is-answerable-on-its-run-page): a boolean schema renders two decision buttons, sends the exact click, and confirms it", async () => {
    let resolveAnswer: (result: { status: 202; value: RunV3 }) => void = () => {};
    const answerCall = vi.fn(
      (mutation: { body_base64: string }) =>
        new Promise<{ status: 202; value: RunV3 }>((resolve) => {
          void mutation;
          resolveAnswer = resolve;
        })
    );
    render(App, {
      props: {
        cockpitApi: waitingApi({
          answer: answerCall,
          getWorkflowRevision: vi.fn(async () => decisionSchema("boolean"))
        }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });

    await screen.findByRole("heading", { name: question });
    expect(screen.queryByRole("textbox")).toBeNull();

    await fireEvent.click(await screen.findByRole("button", { name: runPageCopy.answerYes }));

    await waitFor(() => expect(answerCall).toHaveBeenCalledTimes(1));
    const body = JSON.parse(globalThis.atob(answerCall.mock.calls[0]?.[0]?.body_base64 ?? ""));
    expect(body.answer_base64).toBe(btoa("true"));
    await screen.findByText(`${runPageCopy.answeredPrefix} ${runPageCopy.answerYes}`);
    resolveAnswer({ status: 202, value: answeredRun() });
  });

  it("proves(a-waiting-v3-run-is-answerable-on-its-run-page): an enum schema renders one button per value and sends its exact JSON", async () => {
    let resolveAnswer: (result: { status: 202; value: RunV3 }) => void = () => {};
    const answerCall = vi.fn(
      (mutation: { body_base64: string }) =>
        new Promise<{ status: 202; value: RunV3 }>((resolve) => {
          void mutation;
          resolveAnswer = resolve;
        })
    );
    render(App, {
      props: {
        cockpitApi: waitingApi({
          answer: answerCall,
          getWorkflowRevision: vi.fn(async () =>
            decisionSchema("enum", ['"approve"', '"revise"'])
          )
        }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });

    await screen.findByRole("heading", { name: question });
    expect(screen.queryByRole("textbox")).toBeNull();

    await fireEvent.click(await screen.findByRole("button", { name: "revise" }));

    await waitFor(() => expect(answerCall).toHaveBeenCalledTimes(1));
    const body = JSON.parse(globalThis.atob(answerCall.mock.calls[0]?.[0]?.body_base64 ?? ""));
    expect(body.answer_base64).toBe(btoa('"revise"'));
    await screen.findByText(`${runPageCopy.answeredPrefix} revise`);
    resolveAnswer({ status: 202, value: answeredRun() });
  });

  it("proves(a-waiting-v3-run-is-answerable-on-its-run-page): a string-typed enum sends its raw text, never JSON-quoted (#1091 PR #1108 finding 1)", async () => {
    let resolveAnswer: (result: { status: 202; value: RunV3 }) => void = () => {};
    const answerCall = vi.fn(
      (mutation: { body_base64: string }) =>
        new Promise<{ status: 202; value: RunV3 }>((resolve) => {
          void mutation;
          resolveAnswer = resolve;
        })
    );
    render(App, {
      props: {
        cockpitApi: waitingApi({
          answer: answerCall,
          getWorkflowRevision: vi.fn(async () =>
            decisionSchema("enum", ["ja", "nein"], true)
          )
        }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });

    await screen.findByRole("heading", { name: question });
    expect(screen.queryByRole("textbox")).toBeNull();

    await fireEvent.click(await screen.findByRole("button", { name: "nein" }));

    await waitFor(() => expect(answerCall).toHaveBeenCalledTimes(1));
    const body = JSON.parse(globalThis.atob(answerCall.mock.calls[0]?.[0]?.body_base64 ?? ""));
    expect(body.answer_base64).toBe(btoa("nein"));
    await screen.findByText(`${runPageCopy.answeredPrefix} nein`);
    resolveAnswer({ status: 202, value: answeredRun() });
  });

  it("proves(a-waiting-v3-run-is-answerable-on-its-run-page): a string schema keeps the text field, never the decision buttons (#1091 PR #1108 regression)", async () => {
    render(App, {
      props: {
        cockpitApi: waitingApi({
          getWorkflowRevision: vi.fn(async () => decisionSchema("string", null, true))
        }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });

    await screen.findByRole("heading", { name: question });

    expect(screen.getByLabelText(runPageCopy.answerLabel).isConnected).toBe(true);
    expect(screen.queryByRole("button", { name: runPageCopy.answerYes })).toBeNull();
    expect(screen.queryByRole("button", { name: runPageCopy.answerNo })).toBeNull();
  });

  it("proves(a-waiting-v3-run-is-answerable-on-its-run-page): keeps the free-form textarea for a schema this build has not classified", async () => {
    render(App, {
      props: { cockpitApi: waitingApi(), mutationJournal: new MutationJournal(sessionStorage) }
    });

    await screen.findByRole("heading", { name: question });

    expect(screen.getByLabelText(runPageCopy.answerLabel).isConnected).toBe(true);
    expect(screen.queryByRole("button", { name: runPageCopy.answerYes })).toBeNull();
  });

  it("proves(a-waiting-v3-run-is-answerable-on-its-run-page): names a refused answer on the card", async () => {
    render(App, {
      props: {
        cockpitApi: waitingApi({
          answer: vi.fn(async () => {
            throw new CockpitRequestError(
              "The durable run is no longer waiting for this answer.",
              {
                type: "urn:atelier2:problem:v1:answer-state-conflict",
                title: "Answer state conflict",
                status: 409,
                detail: "The durable run is no longer waiting for this answer."
              },
              true
            );
          })
        }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });
    await screen.findByRole("heading", { name: question });
    await fireEvent.input(screen.getByLabelText(runPageCopy.answerLabel), {
      target: { value: "yes" }
    });
    await fireEvent.click(screen.getByRole("button", { name: runPageCopy.answerSubmit }));

    const alert = await screen.findByRole("alert", { name: "Send failed" });
    expect(alert.textContent).toContain("The durable run is no longer waiting for this answer.");
    expect(screen.getByLabelText(runPageCopy.answerLabel).isConnected).toBe(true);
  });

  it("proves(a-waiting-v3-run-is-answerable-on-its-run-page): names an absent question instead of the bare node id", async () => {
    render(App, {
      props: {
        cockpitApi: waitingApi({}, null),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });

    expect(await screen.findByText(runPageCopy.questionMissing)).toBeTruthy();
    expect(screen.queryByText(question)).toBeNull();
  });

  it("proves(a-waiting-v3-run-is-answerable-on-its-run-page): names a damaged question instead of an honest absence", async () => {
    const cockpitApi = api(waitingRun(), {
      getWorkflowRevision: vi.fn(async () => waitRevision()),
      getNodeDetail: vi.fn(async (_reference: string, nodeId: string) =>
        (nodeId === "approve"
          ? { ...waitNodeDetail(), job_base64: "////" }
          : earlierNodeDetail()) as never
      )
    });

    render(App, {
      props: { cockpitApi, mutationJournal: new MutationJournal(sessionStorage) }
    });

    expect(await screen.findByText(runPageCopy.waitQuestionUnreadable)).toBeTruthy();
    expect(screen.queryByText(runPageCopy.questionMissing)).toBeNull();
    expect(screen.queryByText(runPageCopy.questionLooking)).toBeNull();
  });
});

describe("cancelling a version 3 run from the cockpit", () => {
  const cancel = runPageCopy.cancel;
  const targetNodeExecutionId = "d".repeat(64);

  beforeEach(() => {
    stubDialogMethods();
    sessionStorage.clear();
    window.history.replaceState(null, "", `/atelier/runs/${publicReference}`);
  });

  afterEach(() => cleanup());

  function overtakenError(): CockpitRequestError {
    return new CockpitRequestError(
      "The agent finished before this cancel reached it; its result stands and the run moved on.",
      {
        type: "urn:atelier2:problem:v1:run-cancellation-overtaken-by-success",
        title: "Run cancellation overtaken by success",
        status: 409,
        detail: "The agent finished before this cancel reached it; its result stands and the run moved on."
      },
      true
    );
  }

  async function openStagedDecision(): Promise<void> {
    await fireEvent.click(await screen.findByRole("button", { name: cancel.open }));
    expect(await screen.findByRole("heading", { name: cancel.question })).toBeTruthy();
  }

  it("proves(the-cockpit-cancels-a-running-v3-run): stages the decision, then stops the run on the one audited path with an idempotency-keyed command", async () => {
    const journal = new MutationJournal(sessionStorage);
    const cancelRun = vi.fn<CockpitApi["cancelRun"]>().mockResolvedValue({
      status: 202,
      value: v3Run({ cancellation: notCancellableBlock("already-cancelling") })
    });
    render(App, { props: { cockpitApi: api(v3Run(), { cancelRun }), mutationJournal: journal } });

    await openStagedDecision();
    await fireEvent.click(screen.getByRole("button", { name: cancel.confirm }));

    await waitFor(() => expect(cancelRun).toHaveBeenCalledTimes(1));
    const sent = cancelRun.mock.calls[0]?.[0];
    expect(sent?.expected_node_execution_id).toBe(targetNodeExecutionId);
    expect((sent?.idempotency_key ?? "").length).toBeGreaterThan(0);

    expect(await screen.findByText(cancel.accepted)).toBeTruthy();
    const cancelEntries = (await journal.entries()).filter((entry) => entry.kind === "cancel");
    expect(cancelEntries).toHaveLength(1);
    // The staged control does not reappear while the run is stopping: no second cancel.
    expect(screen.queryByRole("button", { name: cancel.open })).toBeNull();
  });

  it("dismisses the staged decision without sending a cancel", async () => {
    const cancelRun = vi.fn();
    render(App, {
      props: { cockpitApi: api(v3Run(), { cancelRun }), mutationJournal: new MutationJournal(sessionStorage) }
    });

    await openStagedDecision();
    await fireEvent.click(screen.getByRole("button", { name: cancel.dismiss }));

    await waitFor(() => expect(screen.queryByRole("heading", { name: cancel.question })).toBeNull());
    expect(cancelRun).not.toHaveBeenCalled();
    expect(await screen.findByRole("button", { name: cancel.open })).toBeTruthy();
  });

  it("proves(a-run-that-finished-first-is-not-called-cancelled): tells the operator the run finished before the cancel, with no false cancelled and no retry", async () => {
    const journal = new MutationJournal(sessionStorage);
    const cancelRun = vi.fn(async () => {
      throw overtakenError();
    });
    render(App, { props: { cockpitApi: api(v3Run(), { cancelRun }), mutationJournal: journal } });

    await openStagedDecision();
    await fireEvent.click(screen.getByRole("button", { name: cancel.confirm }));

    expect(
      await screen.findByText(/finished before this cancel reached it/)
    ).toBeTruthy();
    // The run moved on, so its standing must not read as cancelled.
    expect(screen.getByLabelText(runPageCopy.whereThisRunStands).textContent).not.toContain("Cancelled");
    expect(screen.queryByText(cancel.accepted)).toBeNull();
    expect(screen.queryByRole("button", { name: cancel.retry })).toBeNull();
    expect((await journal.entries()).filter((entry) => entry.kind === "cancel")).toHaveLength(0);
  });

  it("keeps the exact command for retry when the reply is lost, then resends it unchanged", async () => {
    const journal = new MutationJournal(sessionStorage);
    const cancelRun = vi
      .fn()
      .mockRejectedValueOnce(new CockpitRequestError("The workshop could not be reached."))
      .mockResolvedValue({
        status: 202,
        value: v3Run({ cancellation: notCancellableBlock("already-cancelling") })
      });
    render(App, { props: { cockpitApi: api(v3Run(), { cancelRun }), mutationJournal: journal } });

    await openStagedDecision();
    await fireEvent.click(screen.getByRole("button", { name: cancel.confirm }));

    expect(await screen.findByText(cancel.uncertain)).toBeTruthy();
    // Entering the uncertain state hands the keyboard the one move left: Retry.
    const retryButton = await screen.findByRole("button", { name: cancel.retry });
    await waitFor(() => expect(document.activeElement).toBe(retryButton));
    const firstKey = cancelRun.mock.calls[0]?.[0]?.idempotency_key;
    await fireEvent.click(retryButton);

    await waitFor(() => expect(cancelRun).toHaveBeenCalledTimes(2));
    expect(cancelRun.mock.calls[1]?.[0]?.idempotency_key).toBe(firstKey);
    expect(await screen.findByText(cancel.accepted)).toBeTruthy();
  });

  it("proves(a-reload-during-an-unconfirmed-cancel-does-not-lie): offers Retry/Discard rather than claiming the run is stopping, and Retry resends the exact same command", async () => {
    const journal = () => new MutationJournal(sessionStorage);
    const cancelRun = vi
      .fn<CockpitApi["cancelRun"]>()
      .mockRejectedValueOnce(new CockpitRequestError("The workshop could not be reached."))
      .mockResolvedValue({
        status: 202,
        value: v3Run({ cancellation: notCancellableBlock("already-cancelling") })
      });

    render(App, { props: { cockpitApi: api(v3Run(), { cancelRun }), mutationJournal: journal() } });
    await openStagedDecision();
    await fireEvent.click(screen.getByRole("button", { name: cancel.confirm }));
    expect(await screen.findByText(cancel.uncertain)).toBeTruthy();
    const firstKey = cancelRun.mock.calls[0]?.[0]?.idempotency_key;

    // Reload: a fresh page reads the same durable journal for a cancel the server
    // never confirmed.
    cleanup();
    render(App, { props: { cockpitApi: api(v3Run(), { cancelRun }), mutationJournal: journal() } });

    expect(await screen.findByText(cancel.uncertain)).toBeTruthy();
    expect(screen.queryByText(cancel.accepted)).toBeNull();
    const retry = await screen.findByRole("button", { name: cancel.retry });
    expect(screen.getByRole("button", { name: cancel.discard })).toBeTruthy();

    await fireEvent.click(retry);
    await waitFor(() => expect(cancelRun).toHaveBeenCalledTimes(2));
    expect(cancelRun.mock.calls[1]?.[0]?.idempotency_key).toBe(firstKey);
    expect(await screen.findByText(cancel.accepted)).toBeTruthy();
  });

  it("proves(a-reload-during-an-accepted-cancel-still-reads-stopping): keeps 'Stopping this run' with no Retry for a cancel the server accepted", async () => {
    const journal = () => new MutationJournal(sessionStorage);
    const cancelRun = vi.fn<CockpitApi["cancelRun"]>().mockResolvedValue({
      status: 202,
      value: v3Run({ cancellation: notCancellableBlock("already-cancelling") })
    });

    render(App, { props: { cockpitApi: api(v3Run(), { cancelRun }), mutationJournal: journal() } });
    await openStagedDecision();
    await fireEvent.click(screen.getByRole("button", { name: cancel.confirm }));
    expect(await screen.findByText(cancel.accepted)).toBeTruthy();
    // Wait for the durable 202 acceptance before the reload, so the page reads a
    // settled cancel rather than one still on the wire.
    await waitFor(async () =>
      expect(
        (await journal().get(cancelMutationId(publicReference, targetNodeExecutionId)))?.delivery
      ).toBe("accepted")
    );

    cleanup();
    render(App, { props: { cockpitApi: api(v3Run(), { cancelRun }), mutationJournal: journal() } });

    expect(await screen.findByText(cancel.accepted)).toBeTruthy();
    expect(screen.queryByText(cancel.uncertain)).toBeNull();
    expect(screen.queryByRole("button", { name: cancel.retry })).toBeNull();
    expect(screen.queryByRole("button", { name: cancel.open })).toBeNull();
    expect(cancelRun).toHaveBeenCalledTimes(1);
  });

  it("discards a spent cancel from the journal once the run reaches its cancelled terminal", async () => {
    const journal = new MutationJournal(sessionStorage);
    const prepared = await prepareCancel(journal, publicReference, targetNodeExecutionId);
    await journal.markAccepted(prepared.mutation_id);

    render(RunCancelCard, {
      props: {
        run: v3Run({ state: "CANCELLED", cancellation: notCancellableBlock("already-ended") }),
        cockpitApi: api(v3Run()),
        mutationJournal: journal
      }
    });

    await waitFor(async () =>
      expect(await journal.get(cancelMutationId(publicReference, targetNodeExecutionId))).toBeNull()
    );
    expect(screen.queryByText(cancel.accepted)).toBeNull();
  });

  it("proves(a-run-that-cannot-be-cancelled-shows-why): shows the server's reason instead of a cancel button when the run cannot be cancelled", async () => {
    const run = v3Run({ cancellation: notCancellableBlock("between-nodes") });
    render(App, {
      props: { cockpitApi: api(run), mutationJournal: new MutationJournal(sessionStorage) }
    });

    await screen.findByRole("heading", { level: 1, name: "Two agents in a line" });
    expect(
      screen.getByText(cancelReasonSentence("between-nodes", run.current_node_id))
    ).toBeTruthy();
    expect(screen.queryByRole("button", { name: cancel.open })).toBeNull();
  });

  it.each([
    ["WAITING_INPUT", cancel.consequenceWaiting, cancel.consequenceWorking],
    ["STARTED", cancel.consequenceWorking, cancel.consequenceWaiting]
  ] as const)(
    "tells a %s run what cancelling really costs it, and not the other run's cost",
    async (state, said, unsaid) => {
      render(RunCancelCard, {
        props: {
          run: v3Run({ state, cancellation: cancellableBlock(targetNodeExecutionId) }),
          cockpitApi: api(v3Run()),
          mutationJournal: new MutationJournal(sessionStorage)
        }
      });

      await openStagedDecision();

      expect(screen.getByText(said)).toBeTruthy();
      expect(screen.queryByText(unsaid)).toBeNull();
    }
  );

  it.each([...RUN_NOT_CANCELLABLE_REASONS])(
    "reads %s to the operator as a sentence rather than the server's token",
    (reason) => {
      const sentence = cancelReasonSentence(reason, "implement");

      expect(sentence).not.toContain(reason);
      expect(sentence.endsWith(".")).toBe(true);
    }
  );
});

async function failedEvent(nodeId: string, reason: string, sequence: number) {
  return {
    workflow_format_version: 3,
    cursor: `event1.cnVu.${sequence}`,
    sequence,
    public_run_reference: publicReference,
    workflow_revision_hash: digest,
    node_id: nodeId,
    node_execution_id: "b".repeat(64),
    event_hash: "c".repeat(64),
    node_rail: [{ node_id: nodeId, state: "failed" as const, attempt: null }],
    event: "AGENT_FAILED",
    failure_code: "OUTPUT_SCHEMA_REFUSED",
    reason,
    attempt_id: "e".repeat(64),
    attempt_ordinal: 1
  };
}

async function completedEvent(nodeId: string, output: string, sequence: number) {
  const encoded = btoa(output);
  // Named apart from the imported revision digest on purpose: one shadowed the
  // other once, and the strict decoder refused the event rather than quietly
  // reading an ArrayBuffer as a hash.
  const outputDigest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(output)
  );
  return {
    workflow_format_version: 3,
    cursor: `event1.cnVu.${sequence}`,
    sequence,
    public_run_reference: publicReference,
    workflow_revision_hash: digest,
    node_id: nodeId,
    node_execution_id: "b".repeat(64),
    event_hash: "c".repeat(64),
    node_rail: [{ node_id: nodeId, state: "succeeded", attempt: null }],
    event: "AGENT_COMPLETED",
    output_base64: encoded,
    output_hash: [...new Uint8Array(outputDigest)]
      .map((byte) => byte.toString(16).padStart(2, "0"))
      .join(""),
    attempt_id: "e".repeat(64),
    attempt_ordinal: 1
  };
}

function finishedNodeDetail(overrides: Record<string, unknown> = {}) {
  return {
    run_id: "v3/two-agents",
    public_run_reference: publicReference,
    node_id: "implement",
    state: "succeeded",
    job_base64: btoa("Write three German sentences about code review."),
    job_hash: "e".repeat(64),
    answer: { value_base64: btoa("Ein gutes Code-Review."), value_hash: "f".repeat(64) },
    provenance: null,
    refusal: null,
    ...overrides
  };
}

function workingNodeDetail() {
  return {
    run_id: "v3/two-agents",
    public_run_reference: publicReference,
    node_id: "review",
    state: "working",
    job_base64: btoa("Check what the node before you did."),
    job_hash: "e".repeat(64),
    answer: null,
    provenance: null,
    refusal: null
  };
}

describe("a failed node on the run page", () => {
  it("proves(a-failed-run-page-does-not-pose-as-working): a dead run is Failed, not Working, and empty facts do not say yet", async () => {
    const getNodeDetail = vi.fn(async () =>
      ({
        run_id: "v3/two-agents",
        public_run_reference: publicReference,
        node_id: "implement",
        state: "failed",
        job_base64: btoa("Write three German sentences about code review."),
        job_hash: "e".repeat(64),
        answer: null,
        provenance: null,
        refusal: "output-schema-refused: instance-not-json: Expecting value",
        started_at: "2026-08-18T15:00:00Z",
        ended_at: "2026-08-18T15:00:12Z"
      }) as never
    );
    render(App, {
      props: {
        cockpitApi: api(
          v3Run({
            state: "FAILED",
            current_node_id: "implement",
            terminal_hash: terminalHash,
            ended_at: "2026-08-18T15:00:12Z",
            node_rail: [
              { node_id: "implement", state: "failed", attempt: { ordinal: 1, state: "FAILED" } },
              { node_id: "review", state: "queued", attempt: null }
            ]
          }),
          { getNodeDetail }
        ),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });
    await screen.findByRole("heading", { level: 1, name: "Two agents in a line" });

    expect(screen.getByLabelText(runPageCopy.whereThisRunStands).textContent).toContain("Failed");
    expect(screen.getByRole("button", { name: nodeAriaName("implement", "failed") }).isConnected).toBe(true);
    expect(screen.queryByRole("button", { name: new RegExp(stateLabels.working) })).toBeNull();

    // A failed run opens the node that stopped it, on the reason it stopped.
    expect((await screen.findByRole("tab", { name: runPageCopy.tabResult })).getAttribute("aria-selected")).toBe("true");
    await screen.findByText("Nothing written.");

    await fireEvent.click(screen.getByRole("tab", { name: runPageCopy.tabEvidence }));
    await screen.findByText("No receipt.");
    const who = await screen.findByRole("region", { name: "Who" });
    expect(within(who).getByText("Usage").closest("p")?.textContent).toMatch(/^Usage not recorded/);
    expect(within(who).getByText("Resolved model").closest("p")?.textContent).toMatch(
      /^Resolved model not recorded/
    );
    // The facts line under the node's title is the one place duration shows now
    // -- every tab, not only Evidence (the "Done" chip it replaces, 23.08.).
    const panel = screen.getByRole("complementary");
    await within(panel).findByText(/duration 12 s/);
    expect(within(panel).queryByText(/yet/)).toBeNull();
  });

  it("keeps the auto-opened failed node open when it is selected again", async () => {
    const getNodeDetail = vi.fn(async () =>
      finishedNodeDetail({
        state: "failed",
        answer: null,
        refusal: "output-schema-refused: instance-not-json: Expecting value",
        started_at: "2026-08-18T15:00:00Z",
        ended_at: "2026-08-18T15:00:12Z"
      }) as never
    );
    render(App, {
      props: {
        cockpitApi: api(
          v3Run({
            state: "FAILED",
            current_node_id: "implement",
            terminal_hash: terminalHash,
            ended_at: "2026-08-18T15:00:12Z",
            node_rail: [
              { node_id: "implement", state: "failed", attempt: { ordinal: 1, state: "FAILED" } },
              { node_id: "review", state: "queued", attempt: null }
            ]
          }),
          { getNodeDetail }
        ),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });
    await screen.findByRole("heading", { level: 1, name: "Two agents in a line" });

    const panel = await screen.findByRole("complementary");
    expect(within(panel).getByRole("heading", { name: "implement" }).isConnected).toBe(true);
    await within(panel).findByText("Nothing written.");

    await fireEvent.click(screen.getByRole("button", { name: nodeAriaName("implement", "failed") }));

    const stillOpen = screen.getByRole("complementary");
    expect(within(stillOpen).getByRole("heading", { name: "implement" }).isConnected).toBe(true);
    expect(within(stillOpen).getByRole("tabpanel").textContent).toContain("Nothing written.");
    expect(within(stillOpen).getByRole("tabpanel").textContent).not.toContain("yet");
  });

  it("proves(a-failed-node-shows-the-stored-reason-on-the-run-page): shows the stored reason beside the state that says the run failed", async () => {
    // The run is still STARTED on the first read -- the terminal FAILED
    // reading arrives only through the live event below, the same way an
    // operator watching a run actually sees it fail. A run already FAILED on
    // its first read opens no stream at all (#1044), so a stream event is not
    // how such a run's reason can reach this page.
    const feed = new FakeRunEventFeed();
    const reason = "output-schema-refused: instance-not-json: Expecting value";
    const failed = v3Run({
      state: "FAILED",
      current_node_id: "implement",
      terminal_hash: terminalHash,
      latest_event_cursor: eventCursor(1),
      node_rail: [
        { node_id: "implement", state: "failed", attempt: null },
        { node_id: "review", state: "queued", attempt: null }
      ]
    });
    const getRun = vi.fn().mockResolvedValueOnce(v3Run()).mockResolvedValue(failed);
    const cockpitApi = api(v3Run(), { getRun, openRunEvents: feed.open });

    render(App, {
      props: { cockpitApi, mutationJournal: new MutationJournal(sessionStorage) }
    });
    await screen.findByRole("heading", { level: 1, name: "Two agents in a line" });
    feed.handlers?.opened();
    feed.handlers?.event(JSON.stringify(await failedEvent("implement", reason, 1)));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain(reason);
    expect(alert.textContent).toContain("implement");
    // The graph stays quiet: it carries state, never a paragraph of prose.
    const graph = screen.getByRole("region", { name: workflowGraphCopy.label });
    expect(
      within(graph).getByRole("button", { name: nodeAriaName("implement", "failed") }).textContent
    ).not.toContain(reason);
  });
});

describe("the click into a node", () => {
  // Both values stand well over the 120 characters at which the timeline cuts
  // its preview, because the sentence these tests carry says the panel shows the
  // job and the answer WHOLE. Under a shorter value a truncating panel passes
  // every assertion here, so the value is the proof: shorten either one and the
  // clause stops being tested.
  const asked =
    "Judge the draft you were handed, sentence by sentence, and say plainly which of them you would send back to its author, and for what reason.";
  const wrote =
    "Ein gutes Code-Review schuetzt vor fehlerhaftem Code. Es liest zuerst die Absicht und danach die Zeilen. Wer nur die Zeilen liest, findet Tippfehler und keine Denkfehler.";

  function nodeDetail(overrides: Record<string, unknown> = {}) {
    return {
      run_id: "v3/two-agents",
      public_run_reference: publicReference,
      node_id: "implement",
      state: "succeeded",
      job_base64: btoa(asked),
      job_hash: "e".repeat(64),
      answer: { value_base64: btoa(wrote), value_hash: "f".repeat(64) },
      provenance: {
        role: "builder",
        provider_id: "anthropic",
        model: "sonnet",
        executor_revision: "headless-print-json/v1",
        executor_operational_identity: "headless-print-json/v1",
        auth_mode: "subscription",
        profile_id: "operator-subscription",
        agent_configuration_revision_hash: "a".repeat(64),
        request_hash: "b".repeat(64),
        receipt_hash: "c".repeat(64)
      },
      refusal: null,
      ...overrides
    };
  }

  it("proves(a-click-into-a-node-shows-what-it-was-asked-and-wrote): asks the server for that node and shows what it wrote, what it was asked and who ran it", async () => {
    const getNodeDetail = vi.fn(async () => nodeDetail() as never);
    render(App, {
      props: {
        cockpitApi: api(v3Run(), { getNodeDetail }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });
    await screen.findByText("implement");

    await fireEvent.click(screen.getByRole("button", { name: /implement/ }));

    expect(getNodeDetail).toHaveBeenCalledWith(publicReference, "implement");
    // A finished node opens on what it produced -- whole, not a preview.
    await screen.findByText(wrote);
    await fireEvent.click(screen.getByRole("tab", { name: runPageCopy.tabPrompt }));
    await screen.findByText(asked);

    await fireEvent.click(screen.getByRole("tab", { name: runPageCopy.tabEvidence }));
    const who = await screen.findByRole("region", { name: "Who" });
    expect(who.textContent).toMatch(/builder · anthropic/);
    expect(within(who).getByText("sonnet").isConnected).toBe(true);
    // Every fingerprint shows its value beside its name, shortened, never as a
    // label a click has to solve (operator ruling 23.08.).
    for (const label of [
      runPageCopy.promptHash,
      runPageCopy.outputHash,
      runPageCopy.receiptHash
    ]) {
      expect(screen.getByRole("group", { name: label }).isConnected).toBe(true);
    }
    expect(screen.getByText(shortFingerprint("e".repeat(64))).isConnected).toBe(true);
  });

  it("groups each evidence fact with its own explanation, never a neighbour's, and says \"Seals\" once for the whole list, not once per fact", async () => {
    render(App, {
      props: {
        cockpitApi: api(v3Run(), { getNodeDetail: vi.fn(async () => nodeDetail() as never) }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });
    await screen.findByText("implement");

    await openNodeTab(/implement/, runPageCopy.tabEvidence);

    const thisRun = await screen.findByRole("region", { name: runPageCopy.evidenceRun });
    // The list's one sentence about what a fingerprint seals stands once,
    // above the list, instead of a "Seals" wallpapered across every fact
    // beneath it (#579's befund 7.5).
    expect(within(thisRun).getByText(runPageCopy.evidenceRunIntro).isConnected).toBe(true);
    expect(within(thisRun).queryAllByText(/^Seals /)).toEqual([]);

    const groups = within(thisRun).getAllByRole("group");
    // The reading order a person meets these facts in, and the sentence each
    // one carries -- read together, inside the same accessible group, so a
    // sentence can never be mistaken for belonging to the field beside it.
    const fieldsInOrder: ReadonlyArray<{ label: string; seals: string }> = [
      { label: runPageCopy.receiptHash, seals: runPageCopy.sealsReceipt },
      { label: runPageCopy.promptHash, seals: runPageCopy.sealsPrompt },
      { label: runPageCopy.outputHash, seals: runPageCopy.sealsOutput },
      { label: runHeaderCopy.runIdLabel, seals: runHeaderCopy.sealsRunId },
      { label: runPageCopy.workflowRevision, seals: runPageCopy.sealsWorkflow },
      { label: runPageCopy.runConfiguration, seals: runPageCopy.sealsConfiguration }
    ];
    expect(groups.map((group) => group.getAttribute("aria-label"))).toEqual(
      fieldsInOrder.map((field) => field.label)
    );
    fieldsInOrder.forEach((field, index) => {
      const group = groups[index];
      if (group === undefined) throw new Error(`no evidence group rendered for ${field.label}`);
      const sentence = `${field.seals.charAt(0).toUpperCase()}${field.seals.slice(1)}.`;
      expect(within(group).getByText(sentence)).toBeTruthy();
      // No neighbour's label leaked into this group's own accessible content.
      const otherLabels = fieldsInOrder.filter((_, other) => other !== index).map((f) => f.label);
      for (const otherLabel of otherLabels) {
        expect(within(group).queryByText(otherLabel)).toBeNull();
      }
    });
  });

  it("proves(a-click-into-a-node-shows-what-it-was-asked-and-wrote): says usage is not recorded instead of leaving the question open", async () => {
    render(App, {
      props: {
        cockpitApi: api(v3Run(), { getNodeDetail: vi.fn(async () => nodeDetail() as never) }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });
    await screen.findByText("implement");

    await openNodeTab(/implement/, runPageCopy.tabEvidence);

    const who = await screen.findByRole("region", { name: "Who" });
    expect(within(who).getByText("Usage").closest("p")?.textContent).toMatch(/^Usage not recorded/);
    expect(screen.queryByText(/not recorded yet/)).toBeNull();
  });

  it("proves(a-node-carries-how-long-it-ran): shows the recorded duration on a node that ran", async () => {
    render(App, {
      props: {
        cockpitApi: api(v3Run(), {
          getNodeDetail: vi.fn(async () =>
            nodeDetail({
              started_at: "2026-08-18T15:00:00Z",
              ended_at: "2026-08-18T15:05:00Z"
            }) as never
          )
        }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });
    await screen.findByText("implement");

    await openNodeTab(/implement/, runPageCopy.tabEvidence);

    // The facts line under the node's title carries duration now, replacing
    // the "Done" chip -- one grammar for every tab, not an Evidence-only fact.
    const factsLine = await screen.findByText(/started .* · ended .* · duration 5 min/);
    expect(factsLine.isConnected).toBe(true);
  });

  it("a done wait node says it was answered instead of claiming nothing was written", async () => {
    const answeredGate = nodeDetail({
      node_id: "gate",
      state: "succeeded",
      job_base64: btoa("Merge this, or name the blocking defect."),
      job_hash: "e".repeat(64),
      answer: null,
      provenance: null
    });
    render(App, {
      props: {
        cockpitApi: api(v3Run(), { getNodeDetail: vi.fn(async () => answeredGate as never) }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });
    await screen.findByText("implement");

    await openNodeTab(/implement/, runPageCopy.tabResult);

    await screen.findByText(runPageCopy.waitAnswerNotReadable);
    expect(screen.queryByText(runPageCopy.outputEmptyEnded)).toBeNull();
  });

  it("a node with a written answer shows the value, not the wait explanation", async () => {
    render(App, {
      props: {
        cockpitApi: api(v3Run(), { getNodeDetail: vi.fn(async () => nodeDetail() as never) }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });
    await screen.findByText("implement");

    await openNodeTab(/implement/, runPageCopy.tabResult);

    await screen.findByText(wrote);
    expect(screen.queryByText(runPageCopy.waitAnswerNotReadable)).toBeNull();
  });

  it("proves(a-stopped-node-says-so-and-a-waiting-one-does-not): shows the refusal that stops the run, in the words of the owner that refused", async () => {
    const stopped = nodeDetail({
      node_id: "review",
      state: "working",
      job_base64: null,
      job_hash: null,
      answer: null,
      provenance: null,
      refusal:
        "node 'implement' produced an output its own schema refuses: instance-not-json: Expecting value"
    });
    render(App, {
      props: {
        cockpitApi: api(v3Run(), { getNodeDetail: vi.fn(async () => stopped as never) }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });
    await openNodeTab(/review/, runPageCopy.tabResult);

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain(runPageCopy.stoppedHere);
    expect(alert.textContent).toContain("instance-not-json");
    expect(alert.textContent).toContain("implement");
  });

  it("proves(a-stopped-node-says-so-and-a-waiting-one-does-not): shows a node whose work has not arrived as waiting, not as refused", async () => {
    const waiting = nodeDetail({
      node_id: "review",
      state: "queued",
      job_base64: null,
      job_hash: null,
      answer: null,
      provenance: null,
      refusal: null
    });
    render(App, {
      props: {
        cockpitApi: api(v3Run(), { getNodeDetail: vi.fn(async () => waiting as never) }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });
    await openNodeTab(/review/, runPageCopy.tabResult);

    await screen.findByText(/Waiting for the work before it/);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("proves(a-stopped-node-says-so-and-a-waiting-one-does-not): shows a store that disagrees with itself as a problem, not as a tidy refusal", async () => {
    const getNodeDetail = vi.fn(async () => {
      throw new Error("Durable state is corrupt");
    });
    render(App, {
      props: {
        cockpitApi: api(v3Run(), { getNodeDetail: getNodeDetail as never }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });
    await screen.findByText("implement");

    await fireEvent.click(screen.getByRole("button", { name: /implement/ }));

    await screen.findByText(runPageCopy.nodeUnreadable);
    expect(screen.queryByRole("alert")?.textContent ?? "").not.toContain(runPageCopy.stoppedHere);
  });

  it("proves(a-run-page-speaks-prompt-and-output): carries the node's whole history in named tabs", async () => {
    render(App, {
      props: {
        cockpitApi: api(v3Run(), { getNodeDetail: vi.fn(async () => nodeDetail() as never) }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });
    await screen.findByText("implement");

    await fireEvent.click(screen.getByRole("button", { name: /implement/ }));

    const tabs = await screen.findByRole("tablist", { name: runPageCopy.tabsLabel });
    expect(within(tabs).getAllByRole("tab").map((tab) => tab.textContent?.trim())).toEqual([
      runPageCopy.tabResult,
      runPageCopy.tabInput,
      runPageCopy.tabPrompt,
      runPageCopy.tabLog,
      runPageCopy.tabEvidence
    ]);
    // A finished node opens on what it produced.
    expect(
      within(tabs).getByRole("tab", { name: runPageCopy.tabResult }).getAttribute("aria-selected")
    ).toBe("true");
    expect(screen.queryByText("Asked")).toBeNull();
    expect(screen.queryByText("Answered")).toBeNull();
  });

  it("opens a waiting node on the question it asks, not on the tab that happens to be first", async () => {
    render(App, {
      props: {
        cockpitApi: api(
          v3Run({
            state: "WAITING_INPUT",
            current_node_id: "review",
            node_rail: [
              { node_id: "implement", state: "succeeded", attempt: null },
              { node_id: "review", state: "needs_you", attempt: null }
            ]
          }),
          {
            getNodeDetail: vi.fn(async () =>
              nodeDetail({ node_id: "review", state: "needs_you", answer: null }) as never
            )
          }
        ),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });

    await fireEvent.click(await screen.findByRole("button", { name: nodeAriaName("review", "needs_you") }));

    expect(
      screen.getByRole("tab", { name: runPageCopy.tabPrompt }).getAttribute("aria-selected")
    ).toBe("true");
  });

  it("names the earlier nodes a node reads under Input, and says so honestly when it reads none", async () => {
    render(App, {
      props: {
        cockpitApi: api(v3Run(), {
          getNodeDetail: vi.fn(async (_reference: string, nodeId: string) =>
            nodeDetail({ node_id: nodeId }) as never
          )
        }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });

    await openNodeTab(nodeAriaName("review", "working"), runPageCopy.tabInput);
    const reads = await screen.findByRole("tabpanel");
    expect(within(reads).getByText(runPageCopy.inputReads).isConnected).toBe(true);
    expect(within(reads).getByText("implement").isConnected).toBe(true);

    await fireEvent.click(screen.getByRole("button", { name: nodeAriaName("review", "working") }));
    await openNodeTab(nodeAriaName("implement", "succeeded"), runPageCopy.tabInput);
    expect(
      within(screen.getByRole("tabpanel")).getByText(runPageCopy.inputNone).isConnected
    ).toBe(true);
  });

  it("proves(a-run-page-labels-the-declared-model): labels the receipt model as the declared configuration model and says a resolved model is not recorded", async () => {
    render(App, {
      props: {
        cockpitApi: api(v3Run(), { getNodeDetail: vi.fn(async () => nodeDetail() as never) }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });
    await screen.findByText("implement");

    await openNodeTab(/implement/, runPageCopy.tabEvidence);

    const who = await screen.findByRole("region", { name: "Who" });
    expect(within(who).getByText("Declared model").isConnected).toBe(true);
    expect(within(who).getByText("sonnet").isConnected).toBe(true);
    const resolved = within(who).getByText("Resolved model").closest("p");
    expect(resolved?.textContent).toMatch(/^Resolved model not recorded/);
    expect(resolved?.textContent).not.toContain("sonnet");
    await fireEvent.click(within(who).getByRole("button", { name: "Why resolved model is missing" }));
    expect(
      within(who).getByText(/No receipt records a provider-resolved model/).isConnected
    ).toBe(true);
  });

  it("proves(a-run-page-labels-the-declared-model): a working node without a receipt does not invent a declared model and still names the unrecorded resolved one", async () => {
    render(App, {
      props: {
        cockpitApi: api(v3Run(), {
          getNodeDetail: vi.fn(async () =>
            nodeDetail({
              node_id: "review",
              state: "working",
              answer: null,
              provenance: null
            }) as never
          )
        }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });
    await openNodeTab(nodeAriaName("review", "working"), runPageCopy.tabEvidence);

    const who = await screen.findByRole("region", { name: "Who" });
    expect(within(who).getByText("No receipt yet.").isConnected).toBe(true);
    expect(within(who).queryByText("Declared model")).toBeNull();
    expect(within(who).queryByText("sonnet")).toBeNull();
    expect(within(who).getByText("Resolved model").closest("p")?.textContent).toMatch(
      /^Resolved model not recorded yet/
    );
  });
});

describe("the run page speaking the target words", () => {
  function nodeDetail() {
    return finishedNodeDetail();
  }

  it("proves(a-run-page-hash-is-a-named-proof-anchor): a fingerprint shows its value beside its name and copies in full", async () => {
    const writeText = vi.fn(async () => undefined);
    Object.assign(globalThis.navigator, { clipboard: { writeText } });
    render(App, {
      props: {
        cockpitApi: api(v3Run(), { getNodeDetail: vi.fn(async () => nodeDetail() as never) }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });
    await screen.findByRole("heading", { level: 1, name: "Two agents in a line" });

    // Nothing of the sort stands on the main surface.
    expect(screen.queryByText(configurationHash)).toBeNull();
    expect(screen.queryByRole("group", { name: runPageCopy.runConfiguration })).toBeNull();

    await openNodeTab(/implement/, runPageCopy.tabEvidence);

    const anchor = await screen.findByRole("group", { name: runPageCopy.runConfiguration });
    expect(within(anchor).getByText(shortFingerprint(configurationHash)).isConnected).toBe(true);
    await fireEvent.click(
      within(anchor).getByRole("button", { name: `Copy ${runPageCopy.runConfiguration}` })
    );
    expect(writeText).toHaveBeenCalledWith(configurationHash);
  });

  it("proves(a-run-page-does-not-repeat-node-outputs-as-a-timeline): never pastes a finished node's output onto the run surface", async () => {
    const feed = new FakeRunEventFeed();
    render(App, {
      props: {
        cockpitApi: api(v3Run(), { openRunEvents: feed.open }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });
    await screen.findByRole("heading", { level: 1, name: "Two agents in a line" });
    feed.handlers?.opened();
    feed.handlers?.event(JSON.stringify(await completedEvent("implement", "schreiben", 1)));

    const graph = await screen.findByRole("region", { name: workflowGraphCopy.label });
    await waitFor(() =>
      expect(within(graph).getByRole("button", { name: nodeAriaName("implement", "succeeded") }).isConnected).toBe(true)
    );
    expect(document.body.textContent).not.toContain("schreiben");
    expect(screen.queryByText("As it happened")).toBeNull();
    expect(screen.queryByRole("list", { name: "What finished" })).toBeNull();
  });

  it("proves(a-run-page-leads-with-the-workflow-name): the name is the title and the run id waits in the node's evidence", async () => {
    render(App, {
      props: {
        cockpitApi: api(v3Run(), { getNodeDetail: vi.fn(async () => nodeDetail() as never) }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });

    expect(
      (await screen.findByRole("heading", { level: 1, name: "Two agents in a line" })).isConnected
    ).toBe(true);
    expect(screen.queryByRole("heading", { level: 1, name: "Run v3/two-agents" })).toBeNull();
    // The run id names a thing; a chip that names it without showing it is a
    // riddle, so it does not stand on the main surface at all (operator, 23.08.).
    expect(screen.queryByText("v3/two-agents")).toBeNull();

    await openNodeTab(/implement/, runPageCopy.tabEvidence);

    const identity = await screen.findByRole("group", { name: "Run id" });
    expect(within(identity).getByText("v3/two-agents").isConnected).toBe(true);
  });
});

describe("retry from a node on a finished run", () => {
  const fork = runPageCopy.fork;
  const successorReference = "run1.Zm9yaw";
  const orderName = "work_item";

  beforeEach(() => {
    stubDialogMethods();
  });

  function finishedOrigin(overrides: Partial<RunV3> = {}): RunV3 {
    return v3Run({
      state: "COMPLETED",
      terminal_hash: terminalHash,
      ended_at: "2026-08-18T15:00:12Z",
      current_node_id: "review",
      orders: [{ name: orderName, bytes: 48, schema_revision_hash: digest }],
      node_rail: [
        { node_id: "implement", state: "succeeded", attempt: null },
        { node_id: "review", state: "succeeded", attempt: null }
      ],
      cancellation: notCancellableBlock("already-ended"),
      ...overrides
    });
  }

  function successorRun(): RunV3 {
    return v3Run({
      run_id: "v3/forked",
      public_run_reference: successorReference,
      state: "STARTED",
      current_node_id: "review",
      fork_origin: {
        public_run_reference: publicReference,
        terminal_hash: terminalHash,
        restart_from_node_id: "review",
        fork_hash: "e".repeat(64)
      },
      node_rail: [
        {
          node_id: "implement",
          state: "succeeded",
          attempt: null,
          reused_from_run_reference: publicReference,
          source_event_hash: "f".repeat(64),
          source_receipt_hash: "1".repeat(64),
          source_declared_context_package_hash: "2".repeat(64)
        },
        { node_id: "review", state: "working", attempt: null }
      ]
    });
  }

  const carriedSecret = "secret-token";

  function detailFor(nodeId: string) {
    return finishedNodeDetail({
      node_id: nodeId,
      state: "succeeded",
      job_base64: btoa(`Check what came before, and keep ${carriedSecret} out of the confirmation.`),
      answer: {
        value_base64: btoa(`Looks good; the ${carriedSecret} must not leave this panel.`),
        value_hash: "f".repeat(64)
      }
    });
  }

  async function openRetry(
    origin: RunV3,
    nodeName: RegExp | string,
    overrides: Partial<CockpitApi> = {}
  ) {
    const cockpitApi = api(origin, {
      getNodeDetail: vi.fn(async (_reference: string, nodeId: string) => detailFor(nodeId) as never),
      ...overrides
    });
    render(App, {
      props: { cockpitApi, mutationJournal: new MutationJournal(sessionStorage) }
    });
    await screen.findByRole("heading", { level: 1, name: "Two agents in a line" });
    await fireEvent.click(await screen.findByRole("button", { name: nodeName }));
    await fireEvent.click(await screen.findByRole("button", { name: fork.retryHere }));
    return cockpitApi;
  }

  it("offers the door on a completed, failed, or cancelled run", async () => {
    const origin = finishedOrigin();
    render(App, {
      props: {
        cockpitApi: api(origin, {
          getNodeDetail: vi.fn(async (_reference: string, nodeId: string) =>
            detailFor(nodeId) as never
          )
        }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });
    await screen.findByRole("heading", { level: 1, name: "Two agents in a line" });
    await fireEvent.click(
      await screen.findByRole("button", { name: nodeAriaName("review", "succeeded") })
    );
    expect(await screen.findByRole("button", { name: fork.retryHere })).toBeTruthy();
  });

  it("says why the door is absent on a running, waiting, or reconciling run", async () => {
    for (const state of ["STARTED", "WAITING_INPUT", "WAITING_RECONCILIATION"] as const) {
      cleanup();
      window.history.replaceState(null, "", `/atelier/runs/${publicReference}`);
      const live = v3Run({
        state,
        current_node_id: state === "STARTED" ? "review" : "implement",
        node_rail: [
          {
            node_id: "implement",
            state: state === "STARTED" ? "succeeded" : "needs_you",
            attempt: null
          },
          { node_id: "review", state: state === "STARTED" ? "working" : "queued", attempt: null }
        ]
      });
      render(App, {
        props: {
          cockpitApi: api(live, {
            getNodeDetail: vi.fn(async () => finishedNodeDetail() as never)
          }),
          mutationJournal: new MutationJournal(sessionStorage)
        }
      });
      await screen.findByRole("heading", { level: 1, name: "Two agents in a line" });
      await fireEvent.click(await screen.findByRole("button", { name: /implement/ }));
      expect(await screen.findByText(fork.unavailableRunning)).toBeTruthy();
      expect(screen.queryByRole("button", { name: fork.retryHere })).toBeNull();
    }
  });

  it("says the node is not on the run's line when a preview has no rail entry", async () => {
    const origin = finishedOrigin({
      node_rail: [{ node_id: "implement", state: "succeeded", attempt: null }]
    });
    render(App, {
      props: {
        cockpitApi: api(origin, {
          getNodeDetail: vi.fn(async (_reference: string, nodeId: string) =>
            (nodeId === "review" ? detailFor("review") : detailFor("implement")) as never
          )
        }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });
    await screen.findByRole("heading", { level: 1, name: "Two agents in a line" });
    const graph = await screen.findByRole("region", { name: workflowGraphCopy.label });
    await fireEvent.click(within(graph).getByRole("button", { name: "review" }));
    expect(await screen.findByText(fork.unavailableUnknownNode)).toBeTruthy();
    expect(screen.queryByRole("button", { name: fork.retryHere })).toBeNull();
  });

  it("says a step before this one did not succeed when the restart is not reusable", async () => {
    const origin = finishedOrigin({
      state: "FAILED",
      current_node_id: "review",
      node_rail: [
        { node_id: "implement", state: "failed", attempt: null },
        { node_id: "review", state: "queued", attempt: null }
      ]
    });
    render(App, {
      props: {
        cockpitApi: api(origin, {
          getNodeDetail: vi.fn(async () =>
            ({
              run_id: "v3/two-agents",
              public_run_reference: publicReference,
              node_id: "review",
              state: "queued",
              job_base64: null,
              job_hash: null,
              answer: null,
              provenance: null,
              refusal: null
            }) as never
          )
        }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });
    await screen.findByRole("heading", { level: 1, name: "Two agents in a line" });
    // A failed run opens the node that stopped it (`review`).
    const panel = await screen.findByRole("complementary");
    expect(within(panel).getByRole("heading", { name: "review" }).isConnected).toBe(true);
    expect(await screen.findByText(fork.unavailablePrefix)).toBeTruthy();
    expect(screen.queryByRole("button", { name: fork.retryHere })).toBeNull();
  });

  it("names what is carried over and what will run again, and keeps the carried node's bytes out of the sheet", async () => {
    await openRetry(finishedOrigin(), nodeAriaName("review", "succeeded"));

    expect(await screen.findByRole("heading", { name: fork.confirmTitle("review") })).toBeTruthy();
    expect(screen.getByText(fork.carriedOver).closest("p")?.textContent).toContain("implement");
    expect(screen.getByText(fork.carriedOver).closest("p")?.textContent).toContain(orderName);
    expect(screen.getByText(fork.runsAgain).closest("p")?.textContent).toContain("review");
    expect(screen.getByText(fork.deferralSentence)).toBeTruthy();

    const sheet = screen.getByRole("dialog", { name: fork.sheetLabel });
    expect(sheet.textContent).not.toContain(carriedSecret);
    expect(sheet.textContent).not.toContain("Check what came before");
    expect(sheet.textContent).not.toContain("Looks good");
    // The node panel behind the sheet still shows those exact bytes, so this
    // is a scoped proof rather than a body-wide sweep that could never see them.
    expect(screen.getByRole("complementary").textContent).toContain(carriedSecret);
  });

  it("posts a fork and opens the successor as a distinct run", async () => {
    const successor = successorRun();
    const origin = finishedOrigin({
      fork_successors: [
        {
          public_run_reference: successorReference,
          restart_from_node_id: "review",
          fork_hash: "e".repeat(64)
        }
      ]
    });
    const getRun = vi.fn(async (ref: string) => (ref === successorReference ? successor : origin));
    const forkRun = vi.fn<CockpitApi["forkRun"]>().mockResolvedValue({
      status: 201,
      value: successor
    });
    await openRetry(origin, nodeAriaName("review", "succeeded"), { forkRun, getRun });
    await fireEvent.click(screen.getByRole("button", { name: fork.startAgain }));

    await waitFor(() => expect(forkRun).toHaveBeenCalledTimes(1));
    const sent = forkRun.mock.calls[0]?.[0];
    expect(sent?.publicRunReference).toBe(publicReference);
    expect(sent?.restartFromNodeId).toBe("review");
    expect((sent?.idempotencyKey ?? "").length).toBeGreaterThan(0);
    await waitFor(() => expect(window.location.pathname).toBe(runPath(successorReference)));
    expect(await screen.findByText(/Fork of /)).toBeTruthy();
  });

  it("shows the successor on the origin as a separate line", async () => {
    render(App, {
      props: {
        cockpitApi: api(
          finishedOrigin({
            fork_successors: [
              {
                public_run_reference: successorReference,
                restart_from_node_id: "review",
                fork_hash: "e".repeat(64)
              }
            ]
          })
        ),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });
    await screen.findByRole("heading", { level: 1, name: "Two agents in a line" });
    const lineage = await screen.findByRole("link", {
      name: fork.originSuccessor("Two agents in a line", "review")
    });
    expect(lineage.getAttribute("href")).toBe(runPath(successorReference));
  });

  it("opens the failed node so the door is on the stopping step", async () => {
    render(App, {
      props: {
        cockpitApi: api(
          finishedOrigin({
            state: "FAILED",
            current_node_id: "review",
            node_rail: [
              { node_id: "implement", state: "succeeded", attempt: null },
              { node_id: "review", state: "failed", attempt: null }
            ]
          }),
          { getNodeDetail: vi.fn(async () => detailFor("review") as never) }
        ),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });
    await screen.findByRole("heading", { level: 1, name: "Two agents in a line" });
    const panel = await screen.findByRole("complementary");
    expect(within(panel).getByRole("heading", { name: "review" }).isConnected).toBe(true);
    expect(within(panel).getByRole("button", { name: fork.retryHere }).isConnected).toBe(true);
  });

  it("keeps the origin and the sheet when the fork is refused", async () => {
    const forkRun = vi.fn<CockpitApi["forkRun"]>(async () => {
      throw new CockpitRequestError(
        "This API version forks only a linear workflow without loop rounds.",
        {
          type: "urn:atelier2:problem:v1:run-fork-loop-unsupported",
          title: "Run fork loop is unsupported",
          status: 409,
          detail: "This API version forks only a linear workflow without loop rounds."
        },
        true
      );
    });
    await openRetry(finishedOrigin(), nodeAriaName("review", "succeeded"), { forkRun });
    await fireEvent.click(screen.getByRole("button", { name: fork.startAgain }));

    expect(await screen.findByText(/linear workflow without loop rounds/)).toBeTruthy();
    expect(screen.getByRole("heading", { name: fork.confirmTitle("review") }).isConnected).toBe(true);
    expect(window.location.pathname).toBe(runPath(publicReference));
    expect(forkRun).toHaveBeenCalledTimes(1);
    await fireEvent.click(screen.getByRole("button", { name: fork.startAgain }));
    await waitFor(() => expect(forkRun).toHaveBeenCalledTimes(2));
    const keys = forkRun.mock.calls.map((call) => call[0]?.idempotencyKey);
    expect(keys[0]).toBeTruthy();
    expect(keys[0]).toBe(keys[1]);
  });
});
