import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../../src/App.svelte";
import {
  CockpitRequestError,
  encodePublicRunReference,
  type CockpitApi,
  type RunV3,
  type WorkflowRevisionDetail
} from "../../src/api/client";
import { MutationJournal } from "../../src/lib/mutationJournal";
import { runPageCopy } from "../../src/lib/runPageCopy";
import { workbenchPageCopy } from "../../src/lib/workbenchPageCopy";
import { cockpitApiStub, FakeRunEventFeed } from "../support/cockpitApi";
import { cancellableBlock, runRow } from "../support/runV3";
import { waitingInput } from "../support/runV3";

/**
 * The Workbench pins every open decision in its own non-scrolling "Needs you"
 * region so a request can never be lost in the growing conversation -- the
 * lived failure mode that ruling names (#580). These tests drive that region
 * through the real surface: it holds a decision while the operator keeps
 * typing, survives leaving and returning, and answers it on the one audited
 * path the run page and the Board already share.
 */

const revisionHash = "a".repeat(64);
const publicReference = encodePublicRunReference("v3/decide");
const question = "Ship it, or hold it back?";

function waitingRun(overrides: Partial<RunV3> = {}): RunV3 {
  return {
    workflow_format_version: 3,
    run_id: "v3/decide",
    public_run_reference: publicReference,
    workflow_revision_hash: revisionHash,
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
    current_node_execution_id: overrides.current_node_execution_id ?? revisionHash
  };
}

function answeredRun(): RunV3 {
  return waitingRun({
    state: "COMPLETED",
    terminal_hash: "d".repeat(64),
    node_rail: [{ node_id: "approve", state: "succeeded", attempt: null }],
    ended_at: new Date().toISOString()
  });
}

function revision(
  kind: "boolean" | "enum" | "free",
  values: string[] | null = null,
  nodeIds: readonly string[] = ["approve"],
  stringTyped = false
): WorkflowRevisionDetail {
  return {
    workflow_revision_hash: revisionHash,
    document_base64: "YQ==",
    graph: {
      workflow_format_version: 3,
      executable: true,
      not_executable_reason: null,
      node_count: nodeIds.length,
      agent_roles: [],
      orders: [],
      wait_answer_schemas: nodeIds.map((nodeId) => ({
        node_id: nodeId,
        schema: { ref: "decision", revision: "e".repeat(64) },
        kind,
        string_typed: stringTyped,
        values
      })),
      node_previews: nodeIds.map((id) => ({
        id,
        kind: "wait",
        role: null,
        instruction_start: null,
        depends_on: []
      })),
      loops: [],
      name: "Approve once",
      description: null
    }
  } as WorkflowRevisionDetail;
}

function questionDetail(job: string | null = question, nodeId = "approve") {
  return {
    run_id: "v3/decide",
    public_run_reference: publicReference,
    node_id: nodeId,
    state: "needs_you",
    job_base64: job === null ? null : btoa(job),
    job_hash: job === null ? null : "e".repeat(64),
    answer: null,
    provenance: null,
    refusal: null
  };
}

function openWorkbench(runs: readonly RunV3[], overrides: Partial<CockpitApi> = {}) {
  window.history.replaceState(null, "", "/atelier/chat");
  const journal = new MutationJournal(sessionStorage);
  const view = render(App, {
    props: {
      cockpitApi: cockpitApiStub({
        listRuns: vi.fn(async (_after?: string, state?: string) => ({
          items: state === "WAITING_INPUT" ? runs.map(runRow) : [],
          next_after: null
        })),
        getNodeDetail: vi.fn(async () => questionDetail() as never),
        getWorkflowRevision: vi.fn(async () => revision("boolean")),
        ...overrides
      }),
      mutationJournal: journal
    }
  });
  return { ...view, journal };
}

beforeEach(() => {
  sessionStorage.clear();
});

afterEach(() => cleanup());

describe("the Workbench pins open decisions (#580)", () => {
  // This test drives more real interactions (two navigations and an answer)
  // than its neighbours, so v8 coverage instrumentation's overhead pushes it
  // past the default 5s timeout; the raised timeout accommodates the
  // instrumented run without hiding a real regression.
  it("proves(the-workbench-pins-an-open-decision-until-it-is-answered): holds the decision through a walk away and back, then retires it once answered on the shared audited path", async () => {
    const waiting = waitingRun();
    const answer = vi.fn(async (mutation: { body_base64: string }) => {
      void mutation;
      return { status: 202 as const, value: answeredRun() };
    });
    openWorkbench([waiting], { answer });

    const needsYou = await screen.findByRole("region", { name: question });
    expect((await within(needsYou).findByRole("heading", { name: question })).isConnected).toBe(true);
    await within(needsYou).findByRole("button", { name: runPageCopy.answerYes });

    // The whole point: the decision lives in its own pinned region, and
    // stands there until it is answered.
    expect(within(needsYou).getByRole("heading", { name: question }).isConnected).toBe(true);

    // Leaving for another room and coming back is not answering: the pin is
    // read again and stands.
    const rail = screen.getByRole("navigation", { name: "Workshop" });
    await fireEvent.click(within(rail).getByRole("link", { name: "Catalog" }));
    await screen.findByRole("heading", { level: 1, name: "Catalog" });
    await fireEvent.click(
      within(screen.getByRole("navigation", { name: "Workshop" })).getByRole("link", {
        name: new RegExp(workbenchPageCopy.title)
      })
    );
    const returned = await screen.findByRole("region", { name: question });
    await within(returned).findByRole("button", { name: runPageCopy.answerYes });

    // Answering sends the exact JSON the click decides, through the one audited
    // POST -- the same body the run page sends.
    await fireEvent.click(within(returned).getByRole("button", { name: runPageCopy.answerYes }));
    await waitFor(() => expect(answer).toHaveBeenCalledTimes(1));
    const body = JSON.parse(globalThis.atob((answer.mock.calls[0]?.[0] as { body_base64: string }).body_base64));
    expect(body).toEqual({
      workflow_revision_hash: revisionHash,
      node_id: "approve",
      expected_node_execution_id: waiting.current_node_execution_id,
      actor: "operator",
      answer_base64: btoa("true")
    });

    // Ruled line 3: the operator first sees that the answer landed, with the
    // pin still there and no take-back. After one house beat the pin retires.
    const landed = await screen.findByRole("status", { name: workbenchPageCopy.answerLanded });
    expect(landed.isConnected).toBe(true);
    expect(returned.isConnected).toBe(true);
    expect(screen.queryByRole("button", { name: runPageCopy.answerYes })).toBeNull();
    expect(screen.queryByRole("button", { name: runPageCopy.discard })).toBeNull();
    expect(screen.queryByRole("button", { name: runPageCopy.retry })).toBeNull();

    await waitFor(
      () => {
        expect(screen.queryByRole("status", { name: workbenchPageCopy.answerLanded })).toBeNull();
        expect(screen.queryByRole("heading", { name: question })).toBeNull();
      },
      { timeout: 3_000 }
    );
  }, 15_000);

  it("proves(the-workbench-pins-an-open-decision-until-it-is-answered): renders a string-typed enum's own raw text and sends it verbatim, never JSON-quoted (#1091 PR #1108 finding 1)", async () => {
    const waiting = waitingRun();
    const answer = vi.fn(async (mutation: { body_base64: string }) => {
      void mutation;
      return { status: 202 as const, value: answeredRun() };
    });
    openWorkbench([waiting], {
      answer,
      getWorkflowRevision: vi.fn(async () => revision("enum", ["ja", "nein"], ["approve"], true))
    });

    const needsYou = await screen.findByRole("region", { name: question });
    await fireEvent.click(within(needsYou).getByRole("button", { name: "nein" }));

    await waitFor(() => expect(answer).toHaveBeenCalledTimes(1));
    const body = JSON.parse(globalThis.atob((answer.mock.calls[0]?.[0] as { body_base64: string }).body_base64));
    expect(body.answer_base64).toBe(btoa("nein"));
  });

  it("greets an empty workshop instead of pinning a decision that is not there", async () => {
    openWorkbench([]);

    await screen.findByRole("heading", { level: 1, name: workbenchPageCopy.title });
    // Nothing waits, so nothing is said about it: the absence is the shape of
    // the room, not a sentence (ADR 0019 §3).
    expect(screen.queryByRole("heading", { name: question })).toBeNull();
  });

  it("leads a written decision to the run page rather than offering buttons the pin cannot answer here", async () => {
    openWorkbench([waitingRun()], { getWorkflowRevision: vi.fn(async () => revision("free")) });

    const needsYou = await screen.findByRole("region", { name: question });
    await within(needsYou).findByRole("heading", { name: question });
    expect(within(needsYou).queryByRole("button", { name: runPageCopy.answerYes })).toBeNull();
    const open = within(needsYou).getByRole("link", { name: workbenchPageCopy.openTheRun });

    await fireEvent.click(open);

    await waitFor(() => expect(window.location.pathname).toBe(`/atelier/runs/${publicReference}`));
  });

  it("keeps six decisions bounded to one expanded stage and promotes the compact control", async () => {
    const runs = Array.from({ length: 6 }, (_, index) =>
      waitingRun({
        run_id: `v3/decide-${index + 1}`,
        public_run_reference: encodePublicRunReference(`v3/decide-${index + 1}`)
      })
    );
    openWorkbench(runs);

    const decisions = await screen.findAllByRole("region", { name: question });
    expect(decisions).toHaveLength(6);
    const expanded = decisions[0];
    const compact = decisions[5];
    if (expanded === undefined || compact === undefined) {
      throw new Error("The decision stack did not render.");
    }

    expect(within(expanded).getByRole("link", { name: workbenchPageCopy.openTheRun }).isConnected).toBe(true);
    expect(
      within(compact).getByRole("button", {
        name: new RegExp(workbenchPageCopy.answerDecision)
      }).isConnected
    ).toBe(true);
    expect(
      within(compact).queryByRole("link", {
        name: workbenchPageCopy.openTheRun
      })
    ).toBeNull();

    await fireEvent.click(
      within(compact).getByRole("button", {
        name: new RegExp(workbenchPageCopy.answerDecision)
      })
    );

    await waitFor(() => {
      expect(
        within(compact).getByRole("link", {
          name: workbenchPageCopy.openTheRun
        }).isConnected
      ).toBe(true);
      expect(
        within(expanded).queryByRole("link", {
          name: workbenchPageCopy.openTheRun
        })
      ).toBeNull();
      expect(
        within(expanded).getByRole("button", {
          name: new RegExp(workbenchPageCopy.answerDecision)
        }).isConnected
      ).toBe(true);
    });
  });

  it("keeps an uncertain answer expanded when another decision is promoted", async () => {
    const answer = vi.fn(async () => {
      throw new CockpitRequestError("The connection ended without a response.");
    });
    openWorkbench(
      [
        waitingRun(),
        waitingRun({
          run_id: "v3/decide-second",
          public_run_reference: encodePublicRunReference("v3/decide-second")
        })
      ],
      { answer }
    );

    const decisions = await screen.findAllByRole("region", { name: question });
    const answeredDecision = decisions[0];
    const otherDecision = decisions[1];
    if (answeredDecision === undefined || otherDecision === undefined) {
      throw new Error("The decision stack did not render.");
    }

    await fireEvent.click(
      within(answeredDecision).getByRole("button", {
        name: runPageCopy.answerYes
      })
    );
    await within(answeredDecision).findByRole("heading", {
      name: "Answer uncertain"
    });
    await within(answeredDecision).findByRole("button", { name: runPageCopy.retry });

    await fireEvent.click(
      within(otherDecision).getByRole("button", {
        name: new RegExp(workbenchPageCopy.answerDecision)
      })
    );

    await within(otherDecision).findByRole("link", {
      name: workbenchPageCopy.openTheRun
    });
    expect(
      within(answeredDecision).getByRole("heading", {
        name: "Answer uncertain"
      }).isConnected
    ).toBe(true);
    expect(within(answeredDecision).getByRole("button", { name: runPageCopy.retry }).isConnected).toBe(true);
    expect(
      within(answeredDecision).queryByRole("button", {
        name: new RegExp(workbenchPageCopy.answerDecision)
      })
    ).toBeNull();
    expect(screen.queryByRole("status", { name: workbenchPageCopy.answerLanded })).toBeNull();
  });

  it("shows a landed sentence on the live 202 same-node payload and keeps it after the beat", async () => {
    const waiting = waitingRun();
    const answer = vi.fn(async (mutation: { body_base64: string }) => {
      void mutation;
      return { status: 202 as const, value: waitingRun() };
    });
    openWorkbench([waiting], { answer });

    const needsYou = await screen.findByRole("region", { name: question });
    await fireEvent.click(within(needsYou).getByRole("button", { name: runPageCopy.answerYes }));
    await waitFor(() => expect(answer).toHaveBeenCalledTimes(1));

    const landed = await screen.findByRole("status", { name: workbenchPageCopy.answerLanded });
    expect(landed.isConnected).toBe(true);
    expect(screen.queryByRole("heading", { name: question })).toBeNull();
    expect(screen.queryByRole("heading", { name: "Answer pending" })).toBeNull();
    expect(screen.queryByRole("button", { name: runPageCopy.answerYes })).toBeNull();
    expect(screen.queryByRole("button", { name: runPageCopy.discard })).toBeNull();
    expect(screen.queryByRole("button", { name: runPageCopy.retry })).toBeNull();

    await new Promise((resolve) => {
      setTimeout(resolve, 3_000);
    });
    expect(screen.getByRole("status", { name: workbenchPageCopy.answerLanded }).isConnected).toBe(
      true
    );
    expect(screen.queryByRole("heading", { name: question })).toBeNull();
    expect(screen.queryByRole("heading", { name: "Answer pending" })).toBeNull();
    expect(screen.queryByRole("button", { name: runPageCopy.discard })).toBeNull();
    expect(screen.queryByRole("button", { name: runPageCopy.retry })).toBeNull();
    expect(screen.queryByRole("button", { name: runPageCopy.answerYes })).toBeNull();
  });

  it("shows a confirmed answer as a landed sentence before the pin retires, and never on an uncertain send", async () => {
    const answer = vi.fn(async (mutation: { body_base64: string }) => {
      void mutation;
      return { status: 202 as const, value: answeredRun() };
    });
    openWorkbench([waitingRun()], { answer });

    const needsYou = await screen.findByRole("region", { name: question });
    await fireEvent.click(within(needsYou).getByRole("button", { name: runPageCopy.answerYes }));
    await waitFor(() => expect(answer).toHaveBeenCalledTimes(1));

    const landed = await screen.findByRole("status", { name: workbenchPageCopy.answerLanded });
    expect(landed.isConnected).toBe(true);
    expect(
      screen.getByRole("region", { name: workbenchPageCopy.pinnedDecisionsLabel }).isConnected
    ).toBe(true);
    expect(screen.queryByRole("button", { name: runPageCopy.answerYes })).toBeNull();
    expect(screen.queryByRole("button", { name: runPageCopy.discard })).toBeNull();
    expect(screen.queryByRole("button", { name: runPageCopy.retry })).toBeNull();
    expect(
      screen.queryByRole("button", { name: new RegExp(workbenchPageCopy.answerDecision) })
    ).toBeNull();

    await waitFor(
      () => {
        expect(screen.queryByRole("status", { name: workbenchPageCopy.answerLanded })).toBeNull();
        expect(screen.queryByRole("region", { name: workbenchPageCopy.pinnedDecisionsLabel })).toBeNull();
      },
      { timeout: 3_000 }
    );

    cleanup();
    const uncertain = vi.fn(async () => {
      throw new CockpitRequestError("The connection ended without a response.");
    });
    openWorkbench([waitingRun()], { answer: uncertain });
    const stillOpen = await screen.findByRole("region", { name: question });
    await fireEvent.click(within(stillOpen).getByRole("button", { name: runPageCopy.answerYes }));
    await within(stillOpen).findByRole("heading", { name: "Answer uncertain" });
    expect(screen.queryByRole("status", { name: workbenchPageCopy.answerLanded })).toBeNull();
    expect(within(stillOpen).getByRole("button", { name: runPageCopy.retry }).isConnected).toBe(true);
    expect(within(stillOpen).getByRole("button", { name: runPageCopy.discard }).isConnected).toBe(
      true
    );
  });

  it("shows the next wait on the same run after the landed beat", async () => {
    const nextQuestion = "Who reviews round 4?";
    const nextWait = waitingRun({
      current_node_id: "next-gate",
      current_node_execution_id: "f".repeat(64),
      node_rail: [
        { node_id: "approve", state: "succeeded", attempt: null },
        { node_id: "next-gate", state: "needs_you", attempt: null }
      ]
    });
    const answer = vi.fn(async (mutation: { body_base64: string }) => {
      void mutation;
      return { status: 202 as const, value: nextWait };
    });
    openWorkbench([waitingRun()], {
      answer,
      getNodeDetail: vi.fn(async (_publicReference: string, nodeId: string) =>
        nodeId === "next-gate"
          ? (questionDetail(nextQuestion, "next-gate") as never)
          : (questionDetail() as never)
      ),
      getWorkflowRevision: vi.fn(async () => revision("boolean", null, ["approve", "next-gate"]))
    });

    const needsYou = await screen.findByRole("region", { name: question });
    await fireEvent.click(within(needsYou).getByRole("button", { name: runPageCopy.answerYes }));
    await waitFor(() => expect(answer).toHaveBeenCalledTimes(1));

    const landed = await screen.findByRole("status", { name: workbenchPageCopy.answerLanded });
    expect(landed.isConnected).toBe(true);
    expect(screen.queryByRole("heading", { name: question })).toBeNull();
    expect(screen.queryByRole("button", { name: runPageCopy.discard })).toBeNull();

    await waitFor(
      () => {
        expect(screen.queryByRole("status", { name: workbenchPageCopy.answerLanded })).toBeNull();
        expect(screen.getByRole("heading", { name: nextQuestion }).isConnected).toBe(true);
        expect(screen.getByRole("button", { name: runPageCopy.answerYes }).isConnected).toBe(true);
        expect(screen.getByRole("button", { name: runPageCopy.answerNo }).isConnected).toBe(true);
        expect(screen.queryByRole("heading", { name: question })).toBeNull();
      },
      { timeout: 3_000 }
    );
  });

  it("does not re-absorb a stale same-node 202 when the stream takes the next wait", async () => {
    const nextQuestion = "Who reviews round 4?";
    const nextWait = waitingRun({
      current_node_id: "next-gate",
      current_node_execution_id: "f".repeat(64),
      node_rail: [
        { node_id: "approve", state: "succeeded", attempt: null },
        { node_id: "next-gate", state: "needs_you", attempt: null }
      ]
    });
    const feed = new FakeRunEventFeed();
    const answer = vi.fn(async (mutation: { body_base64: string }) => {
      void mutation;
      return { status: 202 as const, value: waitingRun() };
    });
    // The nudge names which run changed; what it now looks like comes off a
    // fresh read of the run list every open already reads (#1148), not a
    // read of that one run alone -- so the double this room re-asks after
    // the event is the same one it always asks, pointed at the newer set.
    let runs: RunV3[] = [waitingRun()];
    const listRuns = vi.fn(async (_after?: string, state?: string) => ({
      items: state === "WAITING_INPUT" ? runs.map(runRow) : [],
      next_after: null
    }));
    openWorkbench([], {
      answer,
      listRuns,
      openAttentionEvents: feed.openAttention,
      getNodeDetail: vi.fn(async (_publicReference: string, nodeId: string) =>
        nodeId === "next-gate"
          ? (questionDetail(nextQuestion, "next-gate") as never)
          : (questionDetail() as never)
      ),
      getWorkflowRevision: vi.fn(async () => revision("boolean", null, ["approve", "next-gate"]))
    });

    const needsYou = await screen.findByRole("region", { name: question });
    await fireEvent.click(within(needsYou).getByRole("button", { name: runPageCopy.answerYes }));
    await waitFor(() => expect(answer).toHaveBeenCalledTimes(1));

    const landed = await screen.findByRole("status", { name: workbenchPageCopy.answerLanded });
    expect(landed.isConnected).toBe(true);

    const listRunsCallsBeforeEvent = listRuns.mock.calls.length;
    feed.handlers?.opened();
    runs = [nextWait];
    feed.handlers?.event(
      JSON.stringify(
        waitingInput(1, {
          public_run_reference: publicReference,
          cursor: `event1.${publicReference.slice("run1.".length)}.1`,
          node_id: "next-gate"
        })
      )
    );

    // Anchored on the reload the nudge itself triggers, not on the eventual
    // settle below: the 202's own frozen "approve" snapshot is still the
    // component's `landedRun` right here, mid-flight, so a broken
    // `hasLeftWait` guard that re-absorbs it resurrects the answered
    // question in this exact window -- a flash the final `waitFor` alone
    // would never sample, since by the time it next polls the fresher read
    // has already overwritten it.
    await waitFor(() => {
      expect(listRuns.mock.calls.length).toBeGreaterThan(listRunsCallsBeforeEvent);
      expect(screen.queryByRole("heading", { name: question })).toBeNull();
      expect(screen.getByRole("status", { name: workbenchPageCopy.answerLanded }).isConnected).toBe(true);
    });

    await waitFor(
      () => {
        expect(screen.queryByRole("status", { name: workbenchPageCopy.answerLanded })).toBeNull();
        expect(screen.getByRole("heading", { name: nextQuestion }).isConnected).toBe(true);
        expect(screen.getByRole("button", { name: runPageCopy.answerYes }).isConnected).toBe(true);
        expect(screen.getByRole("button", { name: runPageCopy.answerNo }).isConnected).toBe(true);
        expect(screen.queryByRole("heading", { name: question })).toBeNull();
      },
      { timeout: 3_000 }
    );
  });
});
