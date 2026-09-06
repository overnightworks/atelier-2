<script lang="ts">
  import { onMount, tick } from "svelte";

  import { wrapDisplayCopy } from "../lib/displayCopy";
  import { humanErrorMessage } from "../lib/humanRefusal";
  import { journalPoisonedCopy } from "../lib/journalPoisonedCopy";
  import { JournalUnreadableError, type MutationJournal } from "../lib/mutationJournal";
  import { exactLocal } from "../lib/when";
  import PoisonedJournalDiscardSheet from "./PoisonedJournalDiscardSheet.svelte";
  import ProblemNotice from "./ProblemNotice.svelte";

  /**
   * The one shared reaction a page shows a poisoned mutation journal (#914
   * second half, #1131): the one honest sentence, its one door, and the
   * confirm sheet behind it, owned once instead of copied per page (the
   * Workbench and the run page used to carry byte-identical copies of this
   * whole shape). A caller keeps only its own `{#if poisoned}` gating around
   * its own content and forwards the journal turning poisoned mid-session,
   * caught by its own read sites (`V3RunView`/`RunCancelCard`/
   * `PinnedDecision`'s own `onJournalPoisoned`), by setting the bound
   * `poisoned` flag directly -- the same flag this door owns and checks once
   * at its own mount.
   */
  export let mutationJournal: MutationJournal;
  export let poisoned = false;
  /** Runs once the journal has just been forgotten and the page is healthy
   * again -- e.g. the Workbench re-reads its conductor's own pending wait.
   * The run page needs nothing further: its own read sites already heal
   * themselves the same way they react to any other prop change. */
  export let onHealed: () => void = () => {};
  /** Where focus lands once the discard has landed; each caller names its
   * own room-level landing spot. */
  export let focusAfterHeal: { focus(): void } | null = null;
  export let doorAttributes: Record<string, string> = {};
  export let confirmAttributes: Record<string, string> = {};
  export let cancelAttributes: Record<string, string> = {};

  let discardConfirming = false;
  /** The exact bytes about to be forgotten, read once when the confirm sheet
   * opens (#914 line 12) -- the same reading the sheet's Technical reveal
   * shows and the post-discard receipt below measures. */
  let discardRaw: string | null = null;
  let discardSubmitting = false;
  let discardFailure: string | null = null;
  /** The receipt at display time: gone once this page is left, since no
   * second ledger survives the same poisoned storage (#914 line 12). */
  let discardReceipt: string | null = null;

  onMount(() => {
    void checkJournalHealth();
  });

  /**
   * One proactive read at mount, so a poisoned journal shows its one sentence
   * before any card tries and fails to read it -- rather than being
   * discovered piecemeal, once per card, once the page is already drawn.
   */
  async function checkJournalHealth(): Promise<void> {
    try {
      await mutationJournal.entries();
    } catch (error) {
      if (!(error instanceof JournalUnreadableError)) throw error;
      poisoned = true;
    }
  }

  function openDiscardConfirm(): void {
    discardRaw = mutationJournal.rawStored();
    discardFailure = null;
    discardConfirming = true;
  }

  function dismissDiscardConfirm(): void {
    discardConfirming = false;
  }

  /**
   * The one door out of a poisoned journal: remove it without ever reading
   * it, then let the page's own read sites run fresh against the
   * now-healthy journal -- the same healing every read site already does for
   * any other prop change, no page reload (#914 line 3).
   *
   * A throw from the browser's own storage stays inside the sheet as its
   * `failure` sentence instead of escaping after the sheet already closed
   * (#914 finding 6); only a successful discard closes it, heals the page,
   * and moves focus to `focusAfterHeal`.
   */
  async function confirmDiscardJournal(): Promise<void> {
    discardSubmitting = true;
    discardFailure = null;
    try {
      mutationJournal.discardPoisoned();
      discardReceipt = journalPoisonedCopy.forgottenReceipt(
        exactLocal(new Date().toISOString()),
        new globalThis.TextEncoder().encode(discardRaw ?? "").length
      );
      discardConfirming = false;
      poisoned = false;
      onHealed();
      await tick();
      focusAfterHeal?.focus();
    } catch (error) {
      discardFailure = humanErrorMessage(error, journalPoisonedCopy.discardFailure);
    } finally {
      discardSubmitting = false;
    }
  }
</script>

{#if poisoned}
  <!-- Every read the page's own content would make into the journal reads
       this same unreadable memory, so nothing below tries: one sentence,
       one door (mockup v8 `#v8-21-journal-poisoned`). -->
  <ProblemNotice
    title={wrapDisplayCopy(journalPoisonedCopy.sentence)}
    message=""
    actionLabel={wrapDisplayCopy(journalPoisonedCopy.door)}
    onAction={openDiscardConfirm}
    actionAttributes={doorAttributes}
  />
{:else if discardReceipt !== null}
  <p class="discard-receipt" role="status">{wrapDisplayCopy(discardReceipt)}</p>
{/if}

{#if discardConfirming}
  <PoisonedJournalDiscardSheet
    raw={discardRaw ?? ""}
    submitting={discardSubmitting}
    failure={discardFailure}
    {confirmAttributes}
    {cancelAttributes}
    onConfirm={() => { void confirmDiscardJournal(); }}
    onDismiss={dismissDiscardConfirm}
  />
{/if}

<style>
  .discard-receipt {
    margin: 0;
    color: var(--ink-dim);
    font-size: var(--text-xs);
  }
</style>
