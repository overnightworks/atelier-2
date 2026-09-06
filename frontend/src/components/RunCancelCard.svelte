<script lang="ts">
  import { tick } from "svelte";

  import type { CockpitApi, RunV3 } from "../api/client";
  import {
    deliverCancel,
    loadPendingCancelForRun,
    prepareCancel,
    type PendingCancel
  } from "../lib/cancelRunDelivery";
  import { wrapDisplayCopy } from "../lib/displayCopy";
  import { humanErrorMessage } from "../lib/humanRefusal";
  import { JournalUnreadableError, MutationJournal, type CancelMutation } from "../lib/mutationJournal";
  import { cancelConsequence, cancelReasonSentence, runPageCopy } from "../lib/runPageCopy";
  import { runHasEnded } from "../lib/runState";

  /**
   * The one control that stops a running V3 agent (#439 P5).
   *
   * Whether it can stop at all is the server's word, read from
   * `run.cancellation`: a cancellable run names the exact node execution this
   * cancel fences on, and a run that cannot be cancelled names why in one closed
   * reason the cockpit spells as a sentence, never a grey disabled button. When
   * it can stop, cancelling is a staged decision (HEART "Decision as stage") --
   * a real question with two honest buttons, not a one-click irreversible fire.
   * On confirm the command travels the same audited pending/uncertain/retry path
   * the wait answer does, and every honest ending is named: stopping, cancelled,
   * finished-before-the-cancel, or a refusal that no retry can change.
   */
  export let run: RunV3;
  export let cockpitApi: CockpitApi;
  export let mutationJournal: MutationJournal;
  export let onRunRead: (run: RunV3) => void = () => {};
  /** Reports upward that the mutation journal itself could not be read
   * (#914, second half of #1131) -- the run page owns the one honest
   * sentence and its one door, mirroring the Workbench, rather than this
   * card failing silently. */
  export let onJournalPoisoned: () => void = () => {};

  const cancel = runPageCopy.cancel;

  /** `readPendingCancel`'s own result, so a poisoned journal is a case its
   * two callers must handle rather than a third, string-typed sentinel
   * squeezed alongside the real `PendingCancel | null`. */
  type PendingCancelLookup =
    | { kind: "found"; pending: PendingCancel | null }
    | { kind: "poisoned" };

  let pending: CancelMutation | null = null;
  let busy = false;
  let accepted = false;
  let uncertainMessage: string | null = null;
  let refusalMessage: string | null = null;
  let confirming = false;

  let dialogElement: globalThis.HTMLDialogElement;
  let openButton: HTMLButtonElement;
  let dismissButton: HTMLButtonElement;
  let confirmButton: HTMLButtonElement;
  let retryButton: HTMLButtonElement;

  $: terminal = runHasEnded(run.state);
  /**
   * The reason to spell out, or null to stay silent. `waiting-for-you` is
   * dropped because the run's standing word already reads "Waiting for you" and
   * the waiting card owns the move -- repeating it here is the clutter HEART
   * warns against.
   */
  $: reason =
    run.cancellation.cancellable || run.cancellation.reason === "waiting-for-you"
      ? null
      : run.cancellation.reason;

  let loadedFor = "";
  $: void loadPending(run.public_run_reference);
  $: void forgetCancelOnTerminal(run.state, run.public_run_reference);

  async function loadPending(publicRunReference: string): Promise<void> {
    if (publicRunReference === loadedFor) return;
    loadedFor = publicRunReference;
    if (terminal) {
      pending = null;
      return;
    }
    const lookup = await readPendingCancel(publicRunReference);
    if (lookup.kind === "poisoned") return;
    pending = lookup.pending;
    accepted = lookup.pending !== null && lookup.pending.delivery === "accepted";
  }

  /**
   * Once the stream carries the run to a terminal state, an accepted cancel has
   * done its work: the journal entry is spent, so it is discarded rather than
   * left to linger in storage forever (`loadPending` already hides it).
   */
  async function forgetCancelOnTerminal(
    state: RunV3["state"],
    publicRunReference: string
  ): Promise<void> {
    if (!runHasEnded(state)) return;
    const lookup = await readPendingCancel(publicRunReference);
    if (lookup.kind === "poisoned") return;
    if (lookup.pending !== null) {
      await mutationJournal.discard(lookup.pending.mutation_id);
    }
    pending = null;
    accepted = false;
  }

  /**
   * The one place both reactive reads above catch the journal itself
   * failing to read (#914, second half of #1131): the run page owns the
   * one honest sentence and its one door, mirroring the Workbench, rather
   * than either read failing silently.
   */
  async function readPendingCancel(
    publicRunReference: string
  ): Promise<PendingCancelLookup> {
    try {
      return { kind: "found", pending: await loadPendingCancelForRun(mutationJournal, publicRunReference) };
    } catch (error) {
      if (!(error instanceof JournalUnreadableError)) throw error;
      onJournalPoisoned();
      return { kind: "poisoned" };
    }
  }

  async function openDecision(): Promise<void> {
    refusalMessage = null;
    confirming = true;
    await tick();
    dialogElement.showModal();
    dismissButton.focus();
  }

  async function dismissDecision(): Promise<void> {
    dialogElement.close();
    confirming = false;
    await tick();
    openButton?.focus();
  }

  function handleDialogCancel(event: Event): void {
    event.preventDefault();
    void dismissDecision();
  }

  function containDialogFocus(event: KeyboardEvent): void {
    if (event.key !== "Tab") return;
    if (event.shiftKey && globalThis.document.activeElement === dismissButton) {
      event.preventDefault();
      confirmButton.focus();
    } else if (!event.shiftKey && globalThis.document.activeElement === confirmButton) {
      event.preventDefault();
      dismissButton.focus();
    }
  }

  async function confirmCancel(): Promise<void> {
    dialogElement.close();
    confirming = false;
    const target = run.cancellation.target_node_execution_id;
    if (target === null) return;
    busy = true;
    uncertainMessage = null;
    refusalMessage = null;
    try {
      const mutation = await prepareCancel(mutationJournal, run.public_run_reference, target);
      pending = mutation;
      accepted = false;
      await deliverAndSettle(mutation, runPageCopy.cancel.unconfirmed);
    } catch (error) {
      uncertainMessage = humanErrorMessage(error, runPageCopy.cancel.unconfirmed);
    } finally {
      busy = false;
    }
    await focusRetryOnUncertain();
  }

  async function retryCancel(): Promise<void> {
    if (pending === null) return;
    busy = true;
    uncertainMessage = null;
    try {
      await deliverAndSettle(pending, runPageCopy.exactRetryUnconfirmed);
    } finally {
      busy = false;
    }
    await focusRetryOnUncertain();
  }

  /**
   * When a delivery attempt leaves the cancel unconfirmed, the Retry control is
   * the one move left, so focus lands on it -- the same keyboard courtesy the
   * wait card gives its own Retry (`V3AnswerCard.focusRetry`). Focus moves only
   * after an attempt the operator made, never on the reload that merely surfaces
   * an already-open cancel.
   */
  async function focusRetryOnUncertain(): Promise<void> {
    await tick();
    if (pending !== null && !accepted && uncertainMessage !== null) {
      retryButton?.focus();
    }
  }

  /**
   * `discard` reads the whole journal before it writes, so a journal that
   * turned unreadable since this card last loaded refuses here exactly as it
   * would on a fresh read -- reported through the same `onJournalPoisoned`
   * door rather than left as an unhandled rejection.
   */
  async function discardCancel(): Promise<void> {
    if (pending === null) return;
    try {
      await mutationJournal.discard(pending.mutation_id);
    } catch (error) {
      if (!(error instanceof JournalUnreadableError)) throw error;
      onJournalPoisoned();
      return;
    }
    pending = null;
    accepted = false;
    uncertainMessage = null;
  }

  async function deliverAndSettle(
    mutation: CancelMutation,
    fallbackMessage: string
  ): Promise<void> {
    const outcome = await deliverCancel(cockpitApi, mutationJournal, mutation, fallbackMessage);
    if (outcome.kind === "cancelled") {
      pending = null;
      accepted = false;
      uncertainMessage = null;
      onRunRead(outcome.run);
      return;
    }
    if (outcome.kind === "cancelling") {
      pending = outcome.pending;
      accepted = true;
      uncertainMessage = null;
      onRunRead(outcome.run);
      return;
    }
    if (outcome.kind === "refused") {
      pending = null;
      accepted = false;
      uncertainMessage = null;
      refusalMessage = outcome.message;
      return;
    }
    pending = outcome.pending;
    accepted = false;
    uncertainMessage = outcome.message;
  }
</script>

{#if terminal}
  <!-- Done is quiet: the run standing already carries the ending. -->
{:else if pending !== null}
  <!-- A cancel is in flight, so this card is the news and lifts to the top of the
       room; only a genuinely-accepted (202) cancel says "Stopping this run", while
       one whose reply was never confirmed says so honestly and offers Retry/Discard,
       the same shape the wait card uses on reload. -->
  <section class="cancel cancel-working cancel-hoist" aria-labelledby="run-cancel-title">
    <p class="eyebrow">{wrapDisplayCopy(cancel.eyebrow)}</p>
    <h2 id="run-cancel-title">{busy ? wrapDisplayCopy(cancel.sending) : accepted ? wrapDisplayCopy(cancel.accepted) : wrapDisplayCopy(cancel.uncertain)}</h2>
    {#if accepted && !busy}
      <p class="cancel-note">{wrapDisplayCopy(cancel.acceptedNote)}</p>
    {/if}
    {#if uncertainMessage !== null}
      <div class="wait-alert" role="alert" aria-label={wrapDisplayCopy(cancel.uncertain)}>
        <span class="wait-alert-shape" aria-hidden="true">?</span>
        <span><strong>{wrapDisplayCopy(cancel.uncertain)}</strong><small>{uncertainMessage}</small></span>
      </div>
    {/if}
    {#if !accepted && !busy}
      <div class="actions">
        <button type="button" disabled={busy} bind:this={retryButton} onclick={() => { void retryCancel(); }}>{wrapDisplayCopy(cancel.retry)}</button>
        <button class="quiet" type="button" disabled={busy} onclick={() => { void discardCancel(); }}>{wrapDisplayCopy(cancel.discard)}</button>
      </div>
    {/if}
  </section>
{:else if refusalMessage !== null}
  <section class="cancel" aria-labelledby="run-cancel-title">
    <p class="eyebrow">{wrapDisplayCopy(cancel.eyebrow)}</p>
    <p id="run-cancel-title" class="cancel-reason" role="status">{refusalMessage}</p>
    {#if run.cancellation.cancellable}
      <button
        class="cancel-open"
        type="button"
        bind:this={openButton}
        onclick={() => { void openDecision(); }}
      >{wrapDisplayCopy(cancel.open)}</button>
    {/if}
  </section>
{:else if run.cancellation.cancellable}
  <section class="cancel" aria-labelledby="run-cancel-title">
    <p id="run-cancel-title" class="eyebrow">{wrapDisplayCopy(cancel.eyebrow)}</p>
    <button
      class="cancel-open"
      type="button"
      disabled={busy}
      bind:this={openButton}
      onclick={() => { void openDecision(); }}
    >{wrapDisplayCopy(cancel.open)}</button>
  </section>
{:else if reason !== null}
  <section class="cancel" aria-labelledby="run-cancel-title">
    <p class="eyebrow">{wrapDisplayCopy(cancel.eyebrow)}</p>
    <p id="run-cancel-title" class="cancel-reason">{cancelReasonSentence(reason, run.current_node_id)}</p>
  </section>
{/if}

{#if confirming}
  <dialog
    class="dialog"
    aria-labelledby="run-cancel-question"
    bind:this={dialogElement}
    oncancel={handleDialogCancel}
    onkeydown={containDialogFocus}
  >
    <h2 id="run-cancel-question">{wrapDisplayCopy(cancel.question)}</h2>
    <p>{wrapDisplayCopy(cancelConsequence(run.state))}</p>
    <div class="dialog-actions">
      <button
        class="quiet"
        type="button"
        bind:this={dismissButton}
        onclick={() => { void dismissDecision(); }}
      >{wrapDisplayCopy(cancel.dismiss)}</button>
      <button
        class="primary"
        type="button"
        bind:this={confirmButton}
        onclick={() => { void confirmCancel(); }}
      >{wrapDisplayCopy(cancel.confirm)}</button>
    </div>
  </dialog>
{/if}

<style>
  .cancel {
    display: grid;
    gap: var(--space-2);
    border: var(--edge) solid var(--line);
    border-radius: var(--r-lg);
    padding: var(--space-4);
    background: var(--panel2);
  }

  /* Hue only for state: a cancel in flight is working, so it borrows the live
     edge; the quiet non-action reason keeps the hairline ink. */
  .cancel-working {
    border-color: var(--signal-live);
  }

  /* A cancel in flight is the room's news, so it lifts above the run's own
     shapes; the idle brake stays where it sits, below the work. */
  .cancel-hoist {
    order: -1;
  }

  .eyebrow {
    margin: 0;
    color: var(--ink-dim);
    font-size: var(--text-2xs);
    font-weight: var(--weight-heavy);
    letter-spacing: var(--tracking-label);
    text-transform: uppercase;
  }

  .cancel-working .eyebrow {
    color: var(--signal-live);
  }

  h2 {
    margin: 0;
    font-size: var(--text-md);
    line-height: var(--leading-tight);
    overflow-wrap: anywhere;
  }

  .cancel-reason {
    margin: 0;
    max-width: var(--reading-width);
    color: var(--ink-dim);
  }

  .cancel-note {
    margin: 0;
    color: var(--ink-dim);
    font-size: var(--text-sm);
  }

  /* The opener names a destructive act, so it wears the failed hue as an
     outline -- present but not shouting until the staged decision confirms it. */
  .cancel-open {
    justify-self: start;
    border-color: var(--signal-failure);
    color: var(--signal-failure);
    background: transparent;
  }
</style>
