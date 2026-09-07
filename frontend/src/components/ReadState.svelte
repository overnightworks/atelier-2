<script lang="ts">
  import { connectionState } from "../lib/connectionState";
  import type { RetainedRead } from "../lib/readResource";
  import { readStateCopy, retryLabel } from "../lib/readStateCopy";
  import LoadingState from "./LoadingState.svelte";

  type ReadStateFailure =
    | { kind: "unavailable"; title: string }
    | { kind: "incomplete"; title: string };

  export let read: RetainedRead<unknown, ReadStateFailure>;
  export let label: string;
  export let onRetry: () => void;

  // The control exists only while the read is failed, so every fresh attempt
  // -- the operator's own retry and any background read of the same resource
  // alike -- destroys it and builds a new one, leaving the keyboard on
  // nothing at all. Taking focus back on a rebuild this control's own
  // keyboard held, and only while nothing else has claimed it since, keeps
  // the keyboard journey continuous across however many attempts run,
  // without ever pulling focus off a control the operator moved to or onto
  // one an unprompted first failure raised.
  let retryHoldsKeyboard = false;

  function keyboardArrived(): void {
    retryHoldsKeyboard = true;
  }

  function keepKeyboardOnRetry(node: HTMLButtonElement): void {
    const ownerDocument = node.ownerDocument;
    // A backgrounded tab reports the same empty focus as a rebuild that just
    // took this control away, so where the keyboard stands is unknowable
    // until the document holds it again: leave both the focus and the memory
    // of it untouched rather than guess.
    if (!ownerDocument.hasFocus()) return;
    const { activeElement, body } = ownerDocument;
    const keyboardIsNowhere = activeElement === null || activeElement === body;
    retryHoldsKeyboard = retryHoldsKeyboard && keyboardIsNowhere;
    if (retryHoldsKeyboard) node.focus();
  }

  // Confirmed truth ends the episode: the control leaves for good and the
  // keyboard lands on the document, so a later failure of this same
  // long-lived read must find nothing armed and raise its control as the
  // unprompted first failure it is.
  $: if (read.request.state === "idle") retryHoldsKeyboard = false;
</script>

<!--
  A confirmed read says nothing at all. The block used to hold its height on
  every surface whether or not it had anything to report, which put a band of
  empty space between a page's title and its content forever after the read
  landed (operator ruling 23.08.: no element that carries no statement).

  A failed read while the whole workshop reads unreachable says nothing of
  its own either: the central connection line above every room already names
  that fact once, and repeating it here as this read's own "unavailable" plus
  a Retry that could only ever fail the same way would be the same fact said
  twice in two different voices (#700). Once the connection returns, a page
  that re-asks on its own clears this on its own; one that does not still
  offers the Retry a returning operator can press.
-->
{#if read.request.state === "loading"}
  <div class="read-state">
    <div class="read-truth">
      <LoadingState
        label={read.confirmed === null ? readStateCopy.looking : readStateCopy.refreshing}
        compact
      />
    </div>
  </div>
{:else if read.request.state === "failed" && $connectionState !== "reconnecting"}
  <div class="read-state">
    <div class="read-truth">
      <span class="read-failure" role="alert">
        <span class="read-mark" aria-hidden="true">◇</span>
        <strong>{read.request.failure.title}</strong>
      </span>
    </div>
    <button
      class="quiet"
      type="button"
      aria-label={retryLabel(label)}
      onclick={onRetry}
      onfocus={keyboardArrived}
      use:keepKeyboardOnRetry
    >{readStateCopy.retry}</button>
  </div>
{/if}

<style>
  .read-state {
    display: flex;
    align-items: center;
    justify-content: flex-end;
    gap: var(--space-3);
    min-height: var(--tap);
  }

  .read-truth {
    flex: 1;
  }

  .read-failure {
    display: inline-flex;
    align-items: center;
    gap: var(--space-2);
    color: var(--signal-failure);
  }

  .read-mark {
    font-size: var(--text-lg);
  }

  @media (max-width: 32rem) {
    .read-state {
      align-items: flex-start;
    }
  }
</style>
