<script lang="ts">
  import { onDestroy } from "svelte";

  import type { CockpitApi, RunV3 } from "../api/client";
  import { decisionStatusCopy } from "../lib/decisionStatusCopy";
  import { wrapDisplayCopy } from "../lib/displayCopy";
  import { decodeUtf8Base64 } from "../lib/exactBytes";
  import { humanErrorMessage } from "../lib/humanRefusal";
  import {
    JournalUnreadableError,
    waitAnswerText,
    type MutationJournal,
    type WaitMutation
  } from "../lib/mutationJournal";
  import { runPageCopy } from "../lib/runPageCopy";
  import { runPath } from "../lib/route";
  import { runHasEnded } from "../lib/runState";
  import {
    deliverWaitAnswer,
    loadPendingWaitAnswer,
    prepareWaitAnswer,
    type PendingWaitLookup
  } from "../lib/waitAnswerDelivery";
  import { confirmedDecisionLabel, decisionLabel } from "../lib/waitDecision";
  import { workbenchPageCopy } from "../lib/workbenchPageCopy";
  import { workbenchQuestionAttribute, workbenchQuestions } from "../lib/workbenchQuestions";
  import LoadingState from "./LoadingState.svelte";

  /**
   * One open decision, pinned so it cannot scroll away in the Workbench stream
   * (issue #580, from the lived failure mode: a decision request once got lost
   * as the conversation grew). It is the decision-as-stage HEART names: the
   * question is the headline, the honest buttons stand under it, and one quiet
   * door leads to the whole run.
   *
   * The answer travels the one audited path `waitAnswerDelivery.ts` owns -- the
   * same path the run page's composer uses, so a decision made here carries the identical
   * durable pending/uncertain/retry journal and conflict handling rather than a
   * second implementation. A boolean or enum wait is answered by a click whose
   * exact JSON the click decides; every other shape (a free/written answer)
   * links to the run page, where the composer for prose already lives.
   */
  export let run: RunV3;
  export let workflowName: string;
  export let cockpitApi: CockpitApi;
  export let mutationJournal: MutationJournal;
  export let onRunRead: (run: RunV3) => void;
  export let navigate: (path: string) => void;
  export let compact = false;
  export let onExpand: () => void;
  /** Reports upward that the mutation journal itself could not be read
   * (#914, second half of #1131) -- the run page owns the one honest
   * sentence and its one door, mirroring the Workbench, rather than this
   * card writing that sentence into its own wait-failure slot. */
  export let onJournalPoisoned: () => void = () => {};

  /** One house beat (`--beat` in styles.css): long enough to read the landed sentence. */
  const ANSWER_LANDED_HOLD_MS = 1600;

  type QuestionLookup =
    | { state: "loading" }
    | { state: "present"; text: string }
    | { state: "missing" }
    | { state: "failed" };

  type GraphLookup =
    | { state: "loading" }
    | {
        state: "ready";
        kind: "boolean" | "enum" | "string" | "free";
        stringTyped: boolean;
        values: readonly string[];
        role: string | null;
      }
    | { state: "failed" };

  let question: QuestionLookup = { state: "loading" };
  let graph: GraphLookup = { state: "loading" };
  let pendingWait: WaitMutation | null = null;
  let waitAccepted = false;
  let waitBusy = false;
  let waitFailureMessage: string | null = null;
  let landedAnswer: string | null = null;
  let landedRun: RunV3 | null = null;
  let landedHold: number | null = null;
  let landedDelivered = false;
  let answeredNodeId: string | null = null;

  $: pendingAnswer = pendingWait === null ? null : waitAnswerText(pendingWait);
  $: confirmedDecision =
    pendingAnswer === null || graph.state !== "ready"
      ? null
      : confirmedDecisionLabel(
          graph.kind,
          graph.stringTyped,
          pendingAnswer,
          runPageCopy.answerYes,
          runPageCopy.answerNo
        );
  $: senderRole = graph.state === "ready" && graph.role !== null ? graph.role : run.current_node_id;
  $: senderItem = run.orders.length === 0 ? null : run.orders.map((order) => order.name).join(", ");

  // One load per waiting node, guarded by node identity the same way the run
  // page guards its own: a run update that leaves this node
  // unchanged never re-reads, and a move to another node reloads honestly.
  let loadedNodeKey = "";
  $: void loadForNode(
    run.public_run_reference,
    run.workflow_revision_hash,
    run.current_node_id,
    run.current_node_execution_id
  );

  async function loadForNode(
    publicRunReference: string,
    workflowRevisionHash: string,
    nodeId: string,
    nodeExecutionId: string | null
  ): Promise<void> {
    const key = `${publicRunReference}:${nodeExecutionId ?? "missing"}`;
    if (key === loadedNodeKey) return;
    loadedNodeKey = key;
    question = { state: "loading" };
    graph = { state: "loading" };
    pendingWait = null;
    waitAccepted = false;
    waitFailureMessage = null;
    if (nodeExecutionId === null) {
      waitFailureMessage = "The waiting turn does not name its exact execution.";
      return;
    }
    await Promise.all([
      loadQuestion(publicRunReference, nodeId, key),
      loadGraph(workflowRevisionHash, nodeId, key),
      loadPending(publicRunReference, workflowRevisionHash, nodeId, nodeExecutionId, key)
    ]);
  }

  async function loadQuestion(publicRunReference: string, nodeId: string, key: string): Promise<void> {
    try {
      const asked = await cockpitApi.getNodeDetail(publicRunReference, nodeId);
      if (key !== loadedNodeKey) return;
      if (asked.job_base64 === null || asked.job_base64.length === 0) {
        question = { state: "missing" };
        return;
      }
      const text = decodeUtf8Base64(asked.job_base64);
      question = text === null || text.length === 0 ? { state: "missing" } : { state: "present", text };
    } catch {
      if (key === loadedNodeKey) question = { state: "failed" };
    }
  }

  async function loadGraph(workflowRevisionHash: string, nodeId: string, key: string): Promise<void> {
    try {
      const revision = await cockpitApi.getWorkflowRevision(workflowRevisionHash);
      if (key !== loadedNodeKey) return;
      if (revision.workflow_revision_hash !== workflowRevisionHash) {
        graph = { state: "failed" };
        return;
      }
      const schema = revision.graph.wait_answer_schemas.find((entry) => entry.node_id === nodeId);
      const node = revision.graph.node_previews.find((entry) => entry.id === nodeId);
      graph = {
        state: "ready",
        kind: schema?.kind ?? "free",
        stringTyped: schema?.string_typed ?? false,
        values: schema?.values ?? [],
        role: node?.role ?? null
      };
    } catch {
      if (key === loadedNodeKey) graph = { state: "failed" };
    }
  }

  async function loadPending(
    publicRunReference: string,
    workflowRevisionHash: string,
    nodeId: string,
    nodeExecutionId: string,
    key: string
  ): Promise<void> {
    let lookup: PendingWaitLookup;
    try {
      lookup = await loadPendingWaitAnswer(
        mutationJournal,
        publicRunReference,
        workflowRevisionHash,
        nodeId,
        nodeExecutionId
      );
    } catch (error) {
      if (!(error instanceof JournalUnreadableError)) throw error;
      // The journal itself could not be read. This card has no door of its
      // own out of a poisoned journal -- that is the page's to show, as the
      // Workbench already does by never mounting this component while its
      // own journal check stays open -- so this reports upward instead of
      // writing the sentence into its own failure slot.
      onJournalPoisoned();
      return;
    }
    if (key !== loadedNodeKey) return;
    if (lookup.kind === "corrupt") {
      waitFailureMessage = lookup.message;
      return;
    }
    if (lookup.kind === "found") {
      pendingWait = lookup.pending;
      waitAccepted = false;
    }
  }

  /**
   * A boolean or enum decision button's own exact value: JSON-encoded text
   * for every ordinary schema, raw text for a `type: string` schema
   * (`graph.stringTyped`, #1091 PR #1108 finding 1) -- never a free-typed
   * answer, which the run page's own composer sends instead.
   */
  async function decide(answer: string): Promise<void> {
    waitFailureMessage = null;
    waitBusy = true;
    try {
      const nodeExecutionId = run.current_node_execution_id;
      const mutation = await prepareWaitAnswer(
        mutationJournal,
        run.public_run_reference,
        run.workflow_revision_hash,
        run.current_node_id,
        nodeExecutionId,
        answer,
        graph.state === "ready" && graph.stringTyped
      );
      pendingWait = mutation;
      waitAccepted = false;
      await settle(mutation);
    } catch (error) {
      waitFailureMessage = humanErrorMessage(error, runPageCopy.answerUnconfirmed);
    } finally {
      waitBusy = false;
    }
  }

  async function retry(): Promise<void> {
    if (pendingWait === null) return;
    waitFailureMessage = null;
    waitBusy = true;
    try {
      await settle(pendingWait);
    } finally {
      waitBusy = false;
    }
  }

  async function discard(): Promise<void> {
    if (pendingWait === null) return;
    await mutationJournal.discard(pendingWait.mutation_id);
    pendingWait = null;
    waitAccepted = false;
    waitFailureMessage = null;
  }

  function hasLeftWait(candidate: RunV3, nodeId: string): boolean {
    return runHasEnded(candidate.state) || candidate.current_node_id !== nodeId;
  }

  function clearLandedVisual(): void {
    landedDelivered = true;
    landedRun = null;
    landedAnswer = null;
    answeredNodeId = null;
    if (landedHold !== null) {
      window.clearTimeout(landedHold);
      landedHold = null;
    }
  }

  function deliverLandedRun(): void {
    if (landedDelivered || landedRun === null || answeredNodeId === null) return;
    // Live run already left this wait. Re-absorbing the frozen 202 would
    // restore the answered node over the stream's next wait.
    if (hasLeftWait(run, answeredNodeId)) {
      clearLandedVisual();
      return;
    }
    if (hasLeftWait(landedRun, answeredNodeId)) {
      const read = landedRun;
      clearLandedVisual();
      onRunRead(read);
      return;
    }
  }

  onDestroy(() => {
    if (landedHold !== null) {
      window.clearTimeout(landedHold);
      landedHold = null;
    }
    // Walking away is not an undo: the answer is already journaled.
    if (landedRun !== null) {
      deliverLandedRun();
    }
  });

  function holdLandedAnswer(answered: RunV3): void {
    // Capture the stamp before clearing the pending wait; confirmedDecision
    // reads that wait. The pin stays for one beat so the landed sentence can
    // be read; deliverLandedRun then retires it only when this wait has left.
    landedAnswer = confirmedDecision ?? pendingAnswer ?? "";
    pendingWait = null;
    waitAccepted = false;
    landedDelivered = false;
    landedRun = answered;
    answeredNodeId = run.current_node_id;
    landedHold = window.setTimeout(() => {
      landedHold = null;
      deliverLandedRun();
    }, ANSWER_LANDED_HOLD_MS);
  }

  // After the beat, a later leave only drops the sentence; the stream owns
  // the live run. Do not clear while the hold is still counting.
  $: if (
    landedHold === null &&
    landedAnswer !== null &&
    answeredNodeId !== null &&
    hasLeftWait(run, answeredNodeId)
  ) {
    clearLandedVisual();
  }

  async function settle(mutation: WaitMutation): Promise<void> {
    const outcome = await deliverWaitAnswer(
      cockpitApi,
      mutationJournal,
      mutation,
      runPageCopy.exactRetryUnconfirmed
    );
    // 200 is an idempotent replay (`confirmed`). The live first answer is 202
    // (`uncertain`). Both hold the landed sentence; the beat retires the pin
    // only when this wait has left.
    if (outcome.kind === "confirmed" || outcome.kind === "uncertain") {
      holdLandedAnswer(outcome.run);
      return;
    }
    pendingWait = outcome.pending;
    waitAccepted = false;
    waitFailureMessage = outcome.message;
    // A refusal the journal could not keep uncertain (the run already moved on)
    // must show that truth next, not keep offering an answer the run no longer
    // waits for.
    if (outcome.pending === null) await refreshCanonicalRun();
  }

  async function refreshCanonicalRun(): Promise<void> {
    try {
      onRunRead(await cockpitApi.getRun(run.public_run_reference));
    } catch {
      // The failure message already on screen names the problem; a second
      // failed refresh would only repeat it.
    }
  }

  function openRun(event: Event): void {
    event.preventDefault();
    navigate(runPath(run.public_run_reference));
  }
</script>

<section
  class="pinned-decision"
  class:pinned-decision-sent={pendingWait !== null}
  class:pinned-decision-compact={compact && landedAnswer === null}
  class:pinned-decision-landed={landedAnswer !== null}
  aria-labelledby="pinned-decision-title-{run.public_run_reference}"
>
  {#if compact && pendingWait === null && landedAnswer === null}
    <button
      class="compact-answer"
      type="button"
      {...{ [workbenchQuestionAttribute]: workbenchQuestions.answerDecision.id }}
      onclick={onExpand}
    >
      <span class="from compact-from">
        <b>{senderRole}</b>
        <span> · {workflowName}</span>
        {#if senderItem !== null}
          <span> · {senderItem}</span>
        {/if}
      </span>
      <span id="pinned-decision-title-{run.public_run_reference}" class="compact-question">
        {#if question.state === "present"}
          {question.text}
        {:else if question.state === "missing"}
          {wrapDisplayCopy(runPageCopy.questionMissing)}
        {:else if question.state === "failed"}
          {wrapDisplayCopy(runPageCopy.needsYou)}
        {:else}
          <LoadingState label={wrapDisplayCopy(runPageCopy.questionLooking)} compact />
        {/if}
      </span>
      <span class="compact-action">{wrapDisplayCopy(workbenchPageCopy.answerDecision)}</span>
    </button>
  {:else}
    <p class="from">
      <b>{senderRole}</b>
      <span> · {workflowName}</span>
      {#if senderItem !== null}
        <span> · {senderItem}</span>
      {/if}
    </p>

  {#if landedAnswer !== null}
    <p
      id="pinned-decision-title-{run.public_run_reference}"
      class="landed-sentence"
      role="status"
      aria-label={wrapDisplayCopy(workbenchPageCopy.answerLanded)}
    >
      <span class="landed-stamp" aria-hidden="true">✓ {wrapDisplayCopy(landedAnswer)} —</span>
      {wrapDisplayCopy(workbenchPageCopy.answerLanded)}
    </p>
  {:else if pendingWait !== null}
    <h3 id="pinned-decision-title-{run.public_run_reference}" class="question">
      {waitBusy ? decisionStatusCopy.sending : waitAccepted ? decisionStatusCopy.pending : decisionStatusCopy.uncertain}
    </h3>
    {#if waitFailureMessage !== null}
      <div class="pinned-alert" role="alert" aria-label={decisionStatusCopy.sendUncertain}>
        <strong>{decisionStatusCopy.sendUncertain}</strong>
        <small>{waitFailureMessage}</small>
      </div>
    {/if}
    <output class="pinned-answer" aria-label={decisionStatusCopy.exactAnswer}
      >{confirmedDecision !== null
        ? `${wrapDisplayCopy(runPageCopy.answeredPrefix)} ${confirmedDecision}`
        : pendingAnswer}</output
    >
    {#if !waitAccepted && !waitBusy}
      <div class="pinned-actions">
        <button
          type="button"
          {...{ [workbenchQuestionAttribute]: workbenchQuestions.answerDecision.id }}
          onclick={() => { void retry(); }}
        >{runPageCopy.retry}</button>
        <button
          type="button"
          class="quiet"
          {...{ [workbenchQuestionAttribute]: workbenchQuestions.answerDecision.id }}
          onclick={() => { void discard(); }}
        >{runPageCopy.discard}</button>
      </div>
    {/if}
  {:else}
    {#if question.state === "present"}
      <h3 id="pinned-decision-title-{run.public_run_reference}" class="question">{question.text}</h3>
    {:else if question.state === "missing"}
      <h3 id="pinned-decision-title-{run.public_run_reference}" class="question">{wrapDisplayCopy(runPageCopy.questionMissing)}</h3>
    {:else if question.state === "failed"}
      <h3 id="pinned-decision-title-{run.public_run_reference}" class="question">{wrapDisplayCopy(runPageCopy.needsYou)}</h3>
    {:else}
      <h3 id="pinned-decision-title-{run.public_run_reference}" class="question">
        <LoadingState label={wrapDisplayCopy(runPageCopy.questionLooking)} compact />
      </h3>
    {/if}

    {#if waitFailureMessage !== null}
      <div class="pinned-alert" role="alert" aria-label={decisionStatusCopy.sendFailed}>
        <strong>{decisionStatusCopy.sendFailed}</strong>
        <small>{waitFailureMessage}</small>
      </div>
    {/if}
  {/if}

    {#if landedAnswer === null}
    <!-- The one quiet door to the whole run: the story behind the question,
         kept as the stage's aside rather than a second decision control. -->
    <div class="pinned-acts">
      {#if pendingWait === null && graph.state === "loading"}
        <p class="pinned-status">
          <LoadingState label={wrapDisplayCopy(runPageCopy.questionLooking)} compact />
        </p>
      {:else if pendingWait === null && graph.state === "ready" && graph.kind === "boolean"}
        <div class="pinned-buttons" role="group" aria-label={wrapDisplayCopy(runPageCopy.answerLabel)}>
          <button class="primary" type="button" disabled={waitBusy} {...{ [workbenchQuestionAttribute]: workbenchQuestions.answerDecision.id }} onclick={() => { void decide("true"); }}>{wrapDisplayCopy(runPageCopy.answerYes)}</button>
          <button class="primary" type="button" disabled={waitBusy} {...{ [workbenchQuestionAttribute]: workbenchQuestions.answerDecision.id }} onclick={() => { void decide("false"); }}>{wrapDisplayCopy(runPageCopy.answerNo)}</button>
        </div>
      {:else if pendingWait === null && graph.state === "ready" && graph.kind === "enum"}
        <div class="pinned-buttons" role="group" aria-label={wrapDisplayCopy(runPageCopy.answerLabel)}>
          {#each graph.values as value (value)}
            <button class="primary" type="button" disabled={waitBusy} {...{ [workbenchQuestionAttribute]: workbenchQuestions.answerDecision.id }} onclick={() => { void decide(value); }}>{decisionLabel(value, graph.stringTyped)}</button>
          {/each}
        </div>
      {/if}
      <a class="pinned-door" href={runPath(run.public_run_reference)} onclick={openRun}
        >{wrapDisplayCopy(workbenchPageCopy.openTheRun)}</a
      >
    </div>
    {/if}
  {/if}
</section>

<style>
  .pinned-decision {
    display: grid;
    gap: var(--space-2);
    border: var(--edge-strong) solid var(--signal-attention-mark);
    border-radius: var(--r-lg);
    padding: var(--space-3) var(--space-5);
    background: var(--panel2);
  }

  .pinned-decision-sent {
    border-color: var(--signal-live);
  }

  .pinned-decision-landed {
    border-color: var(--line);
  }

  .pinned-decision-compact {
    border-width: var(--edge);
    padding: 0;
  }

  .compact-answer {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    width: 100%;
    min-height: var(--tap);
    gap: var(--space-2);
    border: 0;
    padding: var(--space-3) var(--space-4);
    background: transparent;
    color: inherit;
    font: inherit;
    font-size: var(--text-sm);
    text-align: left;
  }

  .compact-question {
    flex: 1;
    min-width: var(--decision-question-min);
  }

  .compact-action {
    margin-left: auto;
    color: var(--signal-attention);
    font-size: var(--text-sm);
    font-weight: var(--weight-strong);
    white-space: nowrap;
  }

  .from {
    margin: 0;
    color: var(--ink-dim);
    font-size: var(--text-2xs);
    font-weight: var(--weight-heavy);
  }

  .compact-from {
    flex: 0 1 auto;
    white-space: nowrap;
  }

  .from b {
    color: var(--ink);
  }

  .question {
    margin: 0;
    font-size: var(--text-lg);
    line-height: var(--leading-tight);
    overflow-wrap: anywhere;
  }

  .landed-sentence {
    margin: 0;
    font-size: var(--text-lg);
    line-height: var(--leading-tight);
    overflow-wrap: anywhere;
  }

  .landed-stamp {
    font-weight: var(--weight-strong);
  }

  .pinned-status {
    margin: 0;
    color: var(--ink-dim);
    font-size: var(--text-sm);
  }

  .pinned-answer {
    font-size: var(--text-sm);
    overflow-wrap: anywhere;
  }

  .pinned-acts,
  .pinned-buttons,
  .pinned-actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-3);
  }

  .pinned-alert {
    display: grid;
    gap: var(--space-1);
    padding: var(--space-2) var(--space-3);
    border-left: var(--edge-mark) solid var(--signal-failure);
    border-radius: var(--r);
    background: color-mix(in srgb, var(--signal-failure) var(--wash), var(--panel2));
    font-size: var(--text-xs);
  }

  .pinned-door {
    margin-left: auto;
    justify-self: start;
    color: var(--ink-dim);
    font-size: var(--text-xs);
    font-weight: var(--weight-medium);
  }

  .pinned-door:hover {
    color: var(--ink);
  }

  @media (max-width: 48rem) {
    .compact-from {
      white-space: normal;
    }
  }
</style>
