import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../../src/App.svelte";
import PinnedDecision from "../../src/components/PinnedDecision.svelte";
import RunCancelCard from "../../src/components/RunCancelCard.svelte";
import V3RunView from "../../src/components/V3RunView.svelte";
import { prepareCancel } from "../../src/lib/cancelRunDelivery";
import { journalPoisonedCopy } from "../../src/lib/journalPoisonedCopy";
import { cancelMutation, MutationJournal } from "../../src/lib/mutationJournal";
import { runPageCopy } from "../../src/lib/runPageCopy";
import { MUTATION_JOURNAL_STORAGE_KEY } from "../../src/lib/storageKeys";
import { prepareWaitAnswer } from "../../src/lib/waitAnswerDelivery";
import { cockpitApiStub } from "../support/cockpitApi";
import {
  publicReference,
  startedRun,
  waitingInputRun,
  workflowName,
  workflowRevision
} from "../support/runV3";

/**
 * The run page's three journal read sites -- `V3RunView.loadPendingWait`,
 * `RunCancelCard`'s two reactive reads, and `PinnedDecision.loadForNode` --
 * react to a poisoned mutation journal the same way the Workbench already
 * does (#914, second half of #1131): one honest sentence, one door
 * (`journalPoisonedCopy`, `PoisonedJournalDiscardSheet.svelte`), and a
 * healed room once it is pressed, with no unhandled rejection along the way.
 */

/** jsdom has no modal dialog; the same seam `RunCancelCard`'s own staged
 * decision and the Workbench's poisoned-journal test already stub. */
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

/** Two entries sharing one mutation identity -- one of `entries()`'s own
 * rejection reasons (`mutationJournal.ts`), built from its own exported
 * `cancelMutation` rather than a hand-rolled envelope. */
function duplicateIdentityJournal(): string {
  const entry = {
    ...cancelMutation(publicReference, "d".repeat(64), "cancel-dup"),
    delivery: "prepared" as const
  };
  return JSON.stringify([entry, entry]);
}

/** A single otherwise-valid cancel entry whose `body_base64` was corrupted
 * after the fact -- one of `entries()`'s own rejection reasons distinct from
 * the top-level shape checks above: the byte/hash corruption a real browser
 * bug or a hand edit of `localStorage` could produce. */
function corruptedBodyJournal(): string {
  const entry = {
    ...cancelMutation(publicReference, "d".repeat(64), "cancel-corrupt"),
    delivery: "prepared" as const,
    body_base64: "not-canonical-base64!!"
  };
  return JSON.stringify([entry]);
}

/** A single otherwise-valid entry carrying one field `entries()` never
 * declared -- its own exact-keys refusal, distinct from an unknown delivery
 * state (a known field with an unrecognized value). */
function extraKeyJournal(): string {
  const entry = {
    ...cancelMutation(publicReference, "d".repeat(64), "cancel-extra"),
    delivery: "prepared" as const,
    unexpected_field: "x"
  };
  return JSON.stringify([entry]);
}

/** Every distinct way `entries()` (`mutationJournal.ts`) refuses to read the
 * stored journal -- corrupt JSON, a value that is not a list, an entry with
 * an unknown delivery state, a duplicate mutation identity, byte/hash
 * corruption inside one entry, and an entry carrying an undeclared key -- so
 * the run page proves it reacts the same way regardless of which one fired. */
const POISONED_JOURNAL_FIXTURES: readonly { name: string; stored: () => string }[] = [
  { name: "invalid JSON", stored: () => "{" },
  { name: "a value that is not a list", stored: () => JSON.stringify({}) },
  { name: "an entry with an unknown delivery state", stored: () => JSON.stringify([{ delivery: "sent" }]) },
  { name: "a duplicate mutation identity", stored: duplicateIdentityJournal },
  { name: "a cancel entry's body_base64 corrupted after the fact", stored: corruptedBodyJournal },
  { name: "an entry carrying an undeclared key", stored: extraKeyJournal }
];

let unhandledRejections: unknown[] = [];

function onUnhandledRejection(reason: unknown): void {
  unhandledRejections.push(reason);
}

beforeEach(() => {
  stubDialogMethods();
  sessionStorage.clear();
  window.history.replaceState(null, "", `/atelier/runs/${publicReference}`);
  unhandledRejections = [];
  process.on("unhandledRejection", onUnhandledRejection);
});

afterEach(() => {
  process.off("unhandledRejection", onUnhandledRejection);
  cleanup();
});

/** Long enough for a promise Node has not yet reported as unhandled to be
 * reported, so an assertion right after this reads the true count. */
async function flushUnhandledRejectionQueue(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 10));
}

describe("a poisoned mutation journal on the run page (#914, second half of #1131)", () => {
  it.each(POISONED_JOURNAL_FIXTURES)(
    "shows the one sentence and door instead of the cancel card, for $name",
    async ({ stored }) => {
      sessionStorage.setItem(MUTATION_JOURNAL_STORAGE_KEY, stored());
      render(App, {
        props: {
          cockpitApi: cockpitApiStub({ getRun: async () => startedRun() }),
          mutationJournal: new MutationJournal(sessionStorage)
        }
      });

      expect(await screen.findByText(journalPoisonedCopy.sentence)).toBeTruthy();
      expect(screen.getByRole("button", { name: journalPoisonedCopy.door })).toBeTruthy();
      // RunCancelCard's own reactive reads hit the identical poisoned
      // journal; it never mounts while the page stays poisoned.
      expect(screen.queryByRole("button", { name: runPageCopy.cancel.open })).toBeNull();
      expect(screen.queryByRole("heading", { level: 1 })).toBeNull();

      await flushUnhandledRejectionQueue();
      expect(unhandledRejections).toEqual([]);
    }
  );

  it("a waiting run shows the same sentence and door instead of the wait card", async () => {
    sessionStorage.setItem(MUTATION_JOURNAL_STORAGE_KEY, "{");
    render(App, {
      props: {
        cockpitApi: cockpitApiStub({ getRun: async () => waitingInputRun() }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });

    expect(await screen.findByText(journalPoisonedCopy.sentence)).toBeTruthy();
    // V3RunView's own onMount read (`loadPendingWait`) hits the identical
    // poisoned journal; it never mounts either, so neither its title nor
    // its wait card appears.
    expect(screen.queryByRole("heading", { level: 1 })).toBeNull();

    await flushUnhandledRejectionQueue();
    expect(unhandledRejections).toEqual([]);
  });

  it("forgetting the journal heals the whole page in the same render tree", async () => {
    sessionStorage.setItem(MUTATION_JOURNAL_STORAGE_KEY, "{");
    render(App, {
      props: {
        cockpitApi: cockpitApiStub({
          getRun: async () => startedRun(),
          getWorkflowRevision: async () => workflowRevision()
        }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });
    await screen.findByText(journalPoisonedCopy.sentence);

    await fireEvent.click(screen.getByRole("button", { name: journalPoisonedCopy.door }));
    const dialog = await screen.findByRole("dialog", { name: journalPoisonedCopy.confirmLabel });
    await fireEvent.click(within(dialog).getByRole("button", { name: journalPoisonedCopy.confirm }));

    await waitFor(() => expect(screen.queryByText(journalPoisonedCopy.sentence)).toBeNull());
    expect(sessionStorage.getItem(MUTATION_JOURNAL_STORAGE_KEY)).toBeNull();
    // The run page, gated on the same journal it just healed, renders the
    // run and its cancel control fresh -- no navigation, no App remount.
    expect(await screen.findByRole("heading", { level: 1, name: workflowName })).toBeTruthy();
    expect(await screen.findByRole("button", { name: runPageCopy.cancel.open })).toBeTruthy();
    expect(await screen.findByText(/^Forgotten at .+ — 1 byte gone\.$/)).toBeTruthy();

    const pageRoot = document.querySelector('section[aria-labelledby="v3-run-title"]');
    await waitFor(() => expect(document.activeElement).toBe(pageRoot));

    await flushUnhandledRejectionQueue();
    expect(unhandledRejections).toEqual([]);
  });

  it("reports it through onJournalPoisoned instead of writing the sentence into its own wait-failure slot", async () => {
    sessionStorage.setItem(MUTATION_JOURNAL_STORAGE_KEY, "{");
    let reported = 0;
    render(PinnedDecision, {
      props: {
        run: waitingInputRun(),
        workflowName,
        cockpitApi: cockpitApiStub(),
        mutationJournal: new MutationJournal(sessionStorage),
        onRunRead: () => {},
        navigate: () => {},
        onExpand: () => {},
        onJournalPoisoned: () => { reported += 1; }
      }
    });

    await waitFor(() => expect(reported).toBe(1));
    // The card's own failure slot never carries the page's one sentence --
    // that slot is the page's to fill, mirroring the Workbench.
    expect(screen.queryByText(journalPoisonedCopy.sentence)).toBeNull();

    await flushUnhandledRejectionQueue();
    expect(unhandledRejections).toEqual([]);
  });

  it("reports it through onJournalPoisoned and renders nothing of its own, mounted directly with a poisoned journal", async () => {
    sessionStorage.setItem(MUTATION_JOURNAL_STORAGE_KEY, "{");
    let reported = 0;
    const { container } = render(RunCancelCard, {
      props: {
        run: waitingInputRun(),
        cockpitApi: cockpitApiStub(),
        mutationJournal: new MutationJournal(sessionStorage),
        onJournalPoisoned: () => { reported += 1; }
      }
    });

    await waitFor(() => expect(reported).toBe(1));
    // Svelte leaves only its own empty anchor comments for the untaken
    // `{#if}` branches -- no element of the card's own renders.
    expect(container.querySelector("*")).toBeNull();
    expect(screen.queryByText(journalPoisonedCopy.sentence)).toBeNull();

    await flushUnhandledRejectionQueue();
    expect(unhandledRejections).toEqual([]);
  });

  it("reports it through onJournalPoisoned and shows no poisoned notice of its own, mounted directly with a poisoned journal", async () => {
    sessionStorage.setItem(MUTATION_JOURNAL_STORAGE_KEY, "{");
    let reported = 0;
    render(V3RunView, {
      props: {
        run: waitingInputRun(),
        cockpitApi: cockpitApiStub({ getWorkflowRevision: async () => workflowRevision() }),
        mutationJournal: new MutationJournal(sessionStorage),
        onJournalPoisoned: () => { reported += 1; }
      }
    });

    // V3RunView mounts RunCancelCard as a child and passes it the same
    // onJournalPoisoned, so both V3RunView.loadPendingWait and
    // RunCancelCard.loadPending report the poisoned journal independently --
    // the settled count is 2, not 1.
    await waitFor(() => expect(reported).toBe(2));
    // Deferring entirely to the page: no local door, no local sentence, and
    // the raw throw never leaked into the wait card's own failure slot.
    expect(screen.queryByText(journalPoisonedCopy.sentence)).toBeNull();

    await flushUnhandledRejectionQueue();
    expect(unhandledRejections).toEqual([]);
  });
});

/**
 * The three write paths that read the journal before they write it --
 * `discard()` on both cards and `deliverWaitAnswer`'s own re-read on retry --
 * are blocked by the identical poisoned journal a read site already refuses
 * (#914's remaining slice). Each proves the one shared reaction
 * (`onJournalPoisoned`) for every one of `entries()`'s own rejection reasons,
 * never an unhandled rejection.
 */
describe("a mutation journal turning poisoned between a write path's own load and its own write", () => {
  it.each(POISONED_JOURNAL_FIXTURES)(
    "RunCancelCard.discardCancel reports it through onJournalPoisoned, for $name",
    async ({ stored }) => {
      const journal = new MutationJournal(sessionStorage);
      const run = startedRun();
      await prepareCancel(journal, run.public_run_reference, "d".repeat(64));
      let reported = 0;
      render(RunCancelCard, {
        props: {
          run,
          cockpitApi: cockpitApiStub(),
          mutationJournal: journal,
          onJournalPoisoned: () => { reported += 1; }
        }
      });
      const discardButton = await screen.findByRole("button", { name: runPageCopy.cancel.discard });

      sessionStorage.setItem(MUTATION_JOURNAL_STORAGE_KEY, stored());
      await fireEvent.click(discardButton);

      await waitFor(() => expect(reported).toBe(1));
      await flushUnhandledRejectionQueue();
      expect(unhandledRejections).toEqual([]);
    }
  );

  it.each(POISONED_JOURNAL_FIXTURES)(
    "V3RunView.discardWait reports it through onJournalPoisoned, for $name",
    async ({ stored }) => {
      const journal = new MutationJournal(sessionStorage);
      const run = waitingInputRun();
      await prepareWaitAnswer(
        journal,
        run.public_run_reference,
        run.workflow_revision_hash,
        run.current_node_id,
        run.current_node_execution_id,
        "an earlier answer",
        false
      );
      let reported = 0;
      render(V3RunView, {
        props: {
          run,
          cockpitApi: cockpitApiStub({ getWorkflowRevision: async () => workflowRevision() }),
          mutationJournal: journal,
          onJournalPoisoned: () => { reported += 1; }
        }
      });
      const discardButton = await screen.findByRole("button", { name: runPageCopy.discard });

      sessionStorage.setItem(MUTATION_JOURNAL_STORAGE_KEY, stored());
      await fireEvent.click(discardButton);

      await waitFor(() => expect(reported).toBe(1));
      await flushUnhandledRejectionQueue();
      expect(unhandledRejections).toEqual([]);
    }
  );

  it.each(POISONED_JOURNAL_FIXTURES)(
    "V3RunView.retryWait reports it through onJournalPoisoned, for $name",
    async ({ stored }) => {
      const journal = new MutationJournal(sessionStorage);
      const run = waitingInputRun();
      await prepareWaitAnswer(
        journal,
        run.public_run_reference,
        run.workflow_revision_hash,
        run.current_node_id,
        run.current_node_execution_id,
        "an earlier answer",
        false
      );
      let reported = 0;
      render(V3RunView, {
        props: {
          run,
          cockpitApi: cockpitApiStub({
            getWorkflowRevision: async () => workflowRevision(),
            answer: async () => ({ status: 200, value: waitingInputRun() })
          }),
          mutationJournal: journal,
          onJournalPoisoned: () => { reported += 1; }
        }
      });
      const retryButton = await screen.findByRole("button", { name: runPageCopy.retry });

      sessionStorage.setItem(MUTATION_JOURNAL_STORAGE_KEY, stored());
      await fireEvent.click(retryButton);

      await waitFor(() => expect(reported).toBe(1));
      await flushUnhandledRejectionQueue();
      expect(unhandledRejections).toEqual([]);
    }
  );

  it("does not swallow a failure that is not the journal itself: discardCancel rethrows it rather than reporting onJournalPoisoned", async () => {
    const journal = new MutationJournal(sessionStorage);
    const run = startedRun();
    await prepareCancel(journal, run.public_run_reference, "d".repeat(64));
    const notAJournalFailure = new Error("the browser's storage quota is exceeded");
    vi.spyOn(journal, "discard").mockRejectedValueOnce(notAJournalFailure);
    let reported = 0;
    render(RunCancelCard, {
      props: {
        run,
        cockpitApi: cockpitApiStub(),
        mutationJournal: journal,
        onJournalPoisoned: () => { reported += 1; }
      }
    });
    const discardButton = await screen.findByRole("button", { name: runPageCopy.cancel.discard });

    await fireEvent.click(discardButton);

    await flushUnhandledRejectionQueue();
    expect(unhandledRejections).toEqual([notAJournalFailure]);
    expect(reported).toBe(0);
  });
});
