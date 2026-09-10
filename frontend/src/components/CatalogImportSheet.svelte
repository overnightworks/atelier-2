<script lang="ts">
  import { onMount } from "svelte";

  import {
    CockpitRequestError,
    type CatalogIntakeKind,
    type LibraryRecognition,
    type Problem
  } from "../api/client";
  import {
    IMPORT_SHEET_KINDS,
    importKindLabel,
    importMistakeSentence,
    importSheetCanDeclare,
    importSheetReport,
    type ImportSheetKind
  } from "../lib/catalogImport";
  import { catalogPageCopy } from "../lib/catalogPageCopy";
  import { wrapDisplayCopy } from "../lib/displayCopy";
  import { humanErrorMessage } from "../lib/humanRefusal";
  import ProblemNotice from "./ProblemNotice.svelte";

  export let document: Uint8Array;
  export let fileName: string;
  export let recognition: LibraryRecognition | null;
  export let recognitionProblem: Problem | null;
  export let recognitionFailure: string | null;
  export let add: (document: Uint8Array, kind: CatalogIntakeKind) => Promise<void>;
  export let onClose: () => void;

  type DialogElement = HTMLElement & { open: boolean; showModal?: () => void; close?: () => void };

  let dialogElement: DialogElement;
  let closeButton: HTMLButtonElement;
  let kindButtons: HTMLButtonElement[] = [];
  let adding = false;
  let selectedKind: ImportSheetKind | null = null;
  let mistake: string | null = null;
  let failure: string | null = null;

  $: canDeclare = importSheetCanDeclare(recognition);
  $: report = recognition !== null && canDeclare ? importSheetReport(recognition, fileName) : null;
  $: addHeld = canDeclare && selectedKind === null && !adding;

  onMount(() => {
    if (typeof dialogElement.showModal === "function") dialogElement.showModal();
    else dialogElement.open = true;
    if (canDeclare) kindButtons[0]?.focus();
    else closeButton.focus();
  });

  function dismiss(): void {
    dialogElement.close?.();
    onClose();
  }

  async function addDeclaredDocument(): Promise<void> {
    if (!canDeclare || selectedKind === null) return;
    mistake = null;
    failure = null;
    adding = true;
    try {
      await add(document, selectedKind);
      dismiss();
    } catch (error) {
      if (error instanceof CockpitRequestError && error.transport_failure) {
        failure = humanErrorMessage(error, catalogPageCopy.importFailed);
      } else {
        mistake = importMistakeSentence(selectedKind);
      }
    } finally {
      adding = false;
    }
  }

  function chooseKind(kind: ImportSheetKind): void {
    if (adding) return;
    selectedKind = kind;
    mistake = null;
  }
</script>

<div class="sheet-positioner">
  <dialog bind:this={dialogElement} class="sheet" aria-labelledby="catalog-import-title" oncancel={dismiss}>
    <header>
      <h2 id="catalog-import-title">{wrapDisplayCopy(catalogPageCopy.import)}</h2>
    </header>

    {#if report !== null}
      <div class="found">
        <span class="glyph" aria-hidden="true">{report.glyph}</span>
        <span class="count">{wrapDisplayCopy(report.count)}</span>
        <strong>{report.name}</strong>
      </div>
      <div class="kind-field">
        <span id="catalog-import-kind">{wrapDisplayCopy(catalogPageCopy.kind)}</span>
        <div class="kind-chips" role="group" aria-labelledby="catalog-import-kind">
          {#each IMPORT_SHEET_KINDS as kind, index (kind)}
            <button
              bind:this={kindButtons[index]}
              class="kind-chip"
              class:selected={selectedKind === kind}
              type="button"
              aria-pressed={selectedKind === kind}
              disabled={adding}
              onclick={() => { chooseKind(kind); }}
            >{wrapDisplayCopy(importKindLabel(kind))}</button>
          {/each}
        </div>
      </div>
    {:else if recognition?.outcome === "not_held"}
      <p class="failure" role="alert">{recognition.reason}</p>
    {/if}

    {#if recognitionProblem !== null}
      <ProblemNotice title={catalogPageCopy.importFailed} problem={recognitionProblem} />
    {/if}
    {#if recognitionFailure !== null}
      <p class="failure" role="alert">{recognitionFailure}</p>
    {/if}
    {#if mistake !== null}
      <p class="brick" role="alert">{wrapDisplayCopy(mistake)}</p>
    {/if}
    {#if failure !== null}
      <p class="failure" role="alert">{failure}</p>
    {/if}
    {#if addHeld}
      <p class="hint">{wrapDisplayCopy(catalogPageCopy.noKindDeclared)}</p>
    {/if}

    <footer>
      {#if canDeclare}
        <button
          class="primary"
          class:held={addHeld}
          type="button"
          disabled={adding || selectedKind === null}
          title={addHeld ? wrapDisplayCopy(catalogPageCopy.noKindDeclared) : undefined}
          onclick={() => { void addDeclaredDocument(); }}
        >
          {wrapDisplayCopy(adding ? catalogPageCopy.addingToCatalog : catalogPageCopy.addToCatalog)}
        </button>
      {/if}
      <button bind:this={closeButton} class="quiet" type="button" disabled={adding} onclick={dismiss}>
        {wrapDisplayCopy(canDeclare ? catalogPageCopy.cancel : catalogPageCopy.close)}
      </button>
    </footer>
  </dialog>
</div>

<style>
  .sheet-positioner { position: fixed; inset: 0; z-index: 10; pointer-events: none; }
  .sheet { box-sizing: border-box; position: fixed; inset: var(--space-5) var(--space-5) auto auto; width: min(var(--dialog-width), calc(100% - (var(--space-5) * 2))); max-height: calc(100vh - (var(--space-5) * 2)); margin: 0; padding: var(--space-5); background: var(--panel2); border: var(--edge) solid var(--ink); border-radius: var(--r-lg); box-shadow: var(--shadow-lift); pointer-events: auto; }
  .sheet::backdrop { background: color-mix(in srgb, var(--ground) 80%, transparent); }
  header, footer { display: flex; align-items: center; gap: var(--space-3); }
  h2 { margin: 0; font-family: var(--serif); }
  .found { display: grid; grid-template-columns: auto auto minmax(0, 1fr); align-items: baseline; gap: var(--space-2); margin-bottom: var(--space-4); }
  .glyph { color: var(--ink-dim); }
  .count { color: var(--ink-dim); font-size: var(--text-2xs); font-variant-numeric: tabular-nums; }
  .kind-field { display: grid; gap: var(--space-1); margin-bottom: var(--space-4); }
  .kind-field > span { font-weight: var(--weight-strong); }
  .kind-chips { display: flex; flex-wrap: wrap; gap: var(--space-1) var(--space-3); }
  .kind-chip { min-height: var(--tap); min-width: var(--tap); padding: var(--space-2) var(--space-1); border: 0; border-bottom: var(--edge-strong) solid transparent; border-radius: 0; background: transparent; color: var(--ink-dim); font-size: var(--text-2xs); font-weight: var(--weight-heavy); letter-spacing: var(--tracking-label); text-transform: uppercase; }
  /*
   * Filled, not just underlined (#1500): a focused-but-unchosen chip keeps
   * the workshop's ordinary 3px outline ring, which sits well outside the
   * button; a chosen chip fills the button itself, so the two signals never
   * read as the same "this one is picked" shape.
   */
  .kind-chip.selected { color: var(--panel2); background: var(--ink); border-bottom-color: transparent; border-radius: var(--r-pill); }
  .brick { margin: 0 0 var(--space-4); padding: var(--space-2) var(--space-3); border-left: var(--edge-strong) solid var(--signal-failure); color: var(--signal-failure); font-size: var(--text-xs); }
  .failure { color: var(--signal-failure); }
  .hint { margin: 0 0 var(--space-4); color: var(--ink-dim); font-size: var(--text-xs); }
  button.held { cursor: not-allowed; }
  @media (max-width: 480px) { .count { display: none; } }
  @media (max-width: 480px) { .sheet { inset: auto 0 0 0; width: 100%; max-height: 85vh; border-radius: var(--r-lg) var(--r-lg) 0 0; } }
</style>
