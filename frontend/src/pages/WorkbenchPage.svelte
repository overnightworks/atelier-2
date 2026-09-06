<script lang="ts">
  import { onMount } from "svelte";

  import {
    type CockpitApi,
    type DefectiveRunRow,
    type RunEventSubscription,
    type RunV3
  } from "../api/client";
  import DefectiveRunRowItem from "../components/DefectiveRunRow.svelte";
  import PinnedDecision from "../components/PinnedDecision.svelte";
  import PoisonedJournalDoor from "../components/PoisonedJournalDoor.svelte";
  import ProblemNotice from "../components/ProblemNotice.svelte";
  import ReadState from "../components/ReadState.svelte";
  import {
    applyAttentionFrame,
    attentionStopped,
    markAttentionConnecting,
    markAttentionLive,
    startAttentionHold,
    type AttentionHold
  } from "../lib/attentionHold";
  import { onConnectionRecovered } from "../lib/connectionState";
  import { wrapDisplayCopy } from "../lib/displayCopy";
  import { humanErrorMessage } from "../lib/humanRefusal";
  import type { MutationJournal } from "../lib/mutationJournal";
  import {
    beginRead,
    confirmRead,
    failRead,
    retainedRead,
    updateConfirmed,
    type RetainedRead
  } from "../lib/readResource";
  import { runPath } from "../lib/route";
  import { newestReadOfEachRun, resolveWorkflowName, splitRunListRows } from "../lib/runList";
  import { readEveryRevision, readEveryRun } from "../lib/runPages";
  import { humanMove, runStanding, standingMarks } from "../lib/runState";
  import { seatCopy } from "../lib/seatCopy";
  import {
    connectionLabel,
    protocolDetail,
    protocolTitle,
    streamStopped
  } from "../lib/streamStatus";
  import { absorbAttentionRun, workbenchDecisionPins } from "../lib/workbenchAttention";
  import { THE_ONE_PROJECT } from "../lib/project";
  import { workbenchPageCopy } from "../lib/workbenchPageCopy";
  import { workbenchQuestionAttribute, workbenchQuestions } from "../lib/workbenchQuestions";
  import { WORKSHOP_DESTINATION, runsWaitingForYou } from "../lib/workshop";
  import { type SeatResource } from "../api/client";

  /**
   * The Workbench: what wants you now, what is moving, and the terminal you
   * work in (ADR 0019 §1, #1099).
   *
   * A decision stands pinned in its own non-scrolling region until it is
   * answered -- the whole point of issue #580, because a decision request once
   * got lost in the growing stream. Beneath it lies the living shelf: the runs
   * that are moving, each one click from its graph. The Board that used to hold
   * those runs is gone, and nothing shows them twice.
   *
   * The room is alive: it holds the attention stream the Board used to hold, so
   * a decision that opens while the operator is sitting here appears where it
   * belongs instead of waiting for the next visit. An event is only a nudge --
   * every one of them is projected through a fresh read of the same canonical
   * run list `load()` already reads, so what this room shows is always what
   * the API confirmed, never a frame's own story (#1148).
   */
  export let cockpitApi: CockpitApi;
  export let mutationJournal: MutationJournal;
  export let navigate: (path: string) => void;

  type WorkbenchRuns = {
    runs: RunV3[];
    /** Runs whose own projection failed (#1042): read apart, shown apart. */
    defective: DefectiveRunRow[];
    /** Null when the described catalog could not be read this round: enrichment, not a gate. */
    workflowNames: ReadonlyMap<string, string | null> | null;
  };

  type ReadFailure =
    | { kind: "unavailable"; title: string }
    | { kind: "incomplete"; title: string };

  /**
   * Where the seat stands for this room: still being read, answering at an
   * address, or not answering at all. "unreachable" carries every reason the
   * operator can do nothing about from here -- no seat declared, a refused
   * one, a terminal that stopped -- because the move out of all of them is the
   * same one sentence.
   */
  type SeatLink =
    | { kind: "reading" }
    | { kind: "alive"; url: string; projectId: string | null }
    | { kind: "unreachable" };

  /**
   * The width a terminal is still readable at: about eighty columns of the
   * cockpit's own monospace face plus the room's gutters. Below it the seat
   * shows its refusal instead of a terminal nobody can work in, and never a
   * second, narrower picture of one (#1099, operator ruling 05.09.).
   */
  const READABLE_TERMINAL_WIDTH = "(min-width: 40rem)";

  let live: RetainedRead<WorkbenchRuns, ReadFailure> = retainedRead<WorkbenchRuns, ReadFailure>();
  let hold: AttentionHold = startAttentionHold();
  let stream: RunEventSubscription | null = null;
  let streamFailureMessage: string | null = null;
  let disposed = false;
  /**
   * At most one `load()` in flight, and at most one more queued behind it
   * (#1148): the attention feed replays one durable event per run it has
   * ever named on every fresh connect, with no cursor to resume from, so a
   * room with many non-terminal runs sees a burst of nudges at once. Every
   * field a nudged card needs is already on the run list `load()` reads
   * (#1148 -- state, workflow name, work item, answer/refusal), so a nudge
   * earns one more list read, never a read of its own; a burst earns at
   * most one more after the read already running, never one each.
   */
  let loadInFlight = false;
  let loadPending = false;
  /** Whether the read queued behind the one in flight also wants the described catalog (#1148 REVISE M1). */
  let pendingReadCatalog = false;
  /**
   * Where this serve's terminal answers, as the door last said it. "reading"
   * is the moment before that answer is known: the room shows that it is
   * connecting rather than an empty frame or a refusal it cannot yet justify.
   */
  let expandedPinReference: string | null = null;
  let seat: SeatLink = { kind: "reading" };
  /**
   * Whether this window is wide enough to read a terminal in at all. Below
   * that width the seat carries the same refusal an unreachable one does
   * (#1099): a terminal squeezed under its readable width is not a smaller
   * terminal, it is an unusable one.
   */
  let readableWidth = true;

  /**
   * Whether this browser's own memory of pending sendings can be read at all
   * (#914). A poisoned journal blocks every read that would otherwise show a
   * pinned decision, so the whole room stands behind `PoisonedJournalDoor`'s
   * one honest sentence and its one door instead of quietly never showing the
   * cards that would have read it.
   */
  let journalPoisoned = false;
  let roomHeading: { focus(): void };

  const catalogPath = WORKSHOP_DESTINATION.catalog.path;

  onMount(() => {
    requestLoad(true);
    holdAttention();
    void readSeat();
    const stopWatchingWidth = watchReadableWidth();
    // A read that failed while the connection was lost stays failed once the
    // connection returns until something asks again -- reload was the only
    // way out (#700). The described catalog and the seat's own address return
    // here too: a serve that restarted while the tab was open draws a fresh
    // address, and nothing else would say so.
    const unsubscribeConnection = onConnectionRecovered(() => {
      requestLoad(true);
      void readSeat();
    });
    return () => {
      disposed = true;
      stream?.close();
      stream = null;
      stopWatchingWidth();
      unsubscribeConnection();
    };
  });

  /**
   * The seat's own read: one address, asked for again after a lost connection
   * and never remembered across reloads, because a serve draws a fresh one
   * whenever it starts.
   */
  async function readSeat(): Promise<void> {
    try {
      seat = seatLink(await cockpitApi.getSeat());
    } catch {
      seat = { kind: "unreachable" };
    }
  }

  function seatLink(read: SeatResource): SeatLink {
    return read.state === "ALIVE" && read.url !== null
      ? { kind: "alive", url: read.url, projectId: read.project_id }
      : { kind: "unreachable" };
  }

  /** Follow the window's width, so a resize answers without a reload. */
  function watchReadableWidth(): () => void {
    if (typeof globalThis.matchMedia !== "function") return () => {};
    const readable = globalThis.matchMedia(READABLE_TERMINAL_WIDTH);
    readableWidth = readable.matches;
    const follow = (): void => {
      readableWidth = readable.matches;
    };
    readable.addEventListener("change", follow);
    return () => readable.removeEventListener("change", follow);
  }

  /**
   * Every run that still moves or waits for a person, read fresh on each visit.
   *
   * The three non-terminal run states are one logical read: any of them
   * incomplete stops the confirm, because a part shown as the whole would hide
   * a run that wants you. A terminal run belongs to History and is never asked
   * for here. The catalog read is enrichment over that truth, never a gate on
   * it -- a run still confirms with its own real fields when its name could not
   * be resolved, falling back to the run id honestly.
   *
   * The list is the cold read; the attention hold nudges a fresh one of the
   * same shape through `requestLoad` so a decision that opens while the
   * operator is sitting here appears without a reload, without a second read
   * shaped only for the one run the nudge named (#1148).
   *
   * `readCatalog` tells the described catalog read (`listWorkflowRevisions`,
   * this room's `workflowNames`) apart from the run lists (#1148 REVISE M1):
   * it costs one read per revision the run list currently names, so a nudge --
   * which names no catalog change of its own, only a run to look at again --
   * keeps whatever this room already confirmed instead of paying that cost
   * again on every event.
   */
  async function load(readCatalog: boolean): Promise<void> {
    const begun = beginRead(live);
    // A background refresh over content already on screen stays silent: no
    // loading band, no shifted room (#1148 REVISE M2). Only the generation
    // advances, so a stale response still loses to a fresher one; the
    // visible request state carries over unchanged until this read lands.
    live = live.confirmed !== null ? { ...begun.read, request: live.request } : begun.read;
    try {
      const [started, waitingInput, waitingReconciliation, revisions] = await Promise.all([
        readEveryRun((after) => cockpitApi.listRuns(after, "STARTED")),
        readEveryRun((after) => cockpitApi.listRuns(after, "WAITING_INPUT")),
        readEveryRun((after) => cockpitApi.listRuns(after, "WAITING_RECONCILIATION")),
        readCatalog ? readEveryRevision((after) => cockpitApi.listWorkflowRevisions(after)) : null
      ]);
      const runReadings = [started, waitingInput, waitingReconciliation];
      if (runReadings.some((reading) => !reading.complete)) {
        live = failRead(live, begun.generation, {
          kind: "incomplete",
          title: wrapDisplayCopy(workbenchPageCopy.runsIncomplete)
        });
        return;
      }
      const workflowNames =
        revisions === null
          ? (live.confirmed?.workflowNames ?? null)
          : revisions.complete
            ? new Map(
                revisions.revisions.map((revision) => [revision.workflow_revision_hash, revision.name])
              )
            : null;
      const rows = newestReadOfEachRun(runReadings.flatMap((reading) => reading.runs));
      const { runs, defective } = splitRunListRows(rows);
      confirm(begun.generation, { runs, defective, workflowNames });
    } catch {
      live = failRead(live, begun.generation, {
        kind: "unavailable",
        title: wrapDisplayCopy(workbenchPageCopy.runsUnavailable)
      });
    }
  }

  function confirm(generation: number, confirmed: WorkbenchRuns): void {
    const before = live;
    live = confirmRead(live, generation, confirmed);
    if (live === before) return;
    publishCount(confirmed.runs);
  }

  /**
   * The hold of the attention stream: the one door through which this room
   * learns that something changed while the operator is looking at it.
   */
  function holdAttention(): void {
    if (stream !== null || attentionStopped(hold)) return;
    try {
      stream = cockpitApi.openAttentionEvents({
        opened: () => {
          hold = markAttentionLive(hold);
        },
        event: applyEvent,
        disconnected: () => {
          hold = markAttentionConnecting(hold, true);
        }
      });
    } catch (error) {
      hold = markAttentionConnecting(hold, true);
      streamFailureMessage = humanErrorMessage(
        error,
        wrapDisplayCopy(workbenchPageCopy.streamUnstartable)
      );
    }
  }

  function applyEvent(rawData: string): void {
    const applied = applyAttentionFrame(hold, rawData);
    hold = applied.hold;
    if (applied.event === null) {
      if (attentionStopped(hold)) {
        stream?.close();
        stream = null;
      }
      return;
    }
    // The event names which run to look at; what it now looks like is
    // already the next `load()`'s to answer (#1148), off the same list
    // every open reads. `isAttentionEvent` (attentionHold.ts) never names a
    // catalog change -- only WAITING_INPUT, AGENT_FAILED,
    // ACTION_RECONCILIATION_REQUIRED -- so this asks for the run lists alone
    // (#1148 REVISE M1).
    requestLoad();
  }

  /**
   * The one gate every `load()` trigger passes through -- the initial mount,
   * a returned connection (#700), a retry, and an attention nudge alike --
   * so a burst of any of them still costs at most the read already running
   * plus one more, never one each. `readCatalog` is true only for the
   * callers that open this room fresh -- mount, a returned connection, an
   * explicit Retry -- and false for an attention nudge (#1148 REVISE M1); a
   * queued call that wants the catalog still gets it even when the one that
   * wins the immediate read did not ask.
   */
  function requestLoad(readCatalog = false): void {
    pendingReadCatalog ||= readCatalog;
    if (loadInFlight) {
      loadPending = true;
      return;
    }
    void runLoad();
  }

  async function runLoad(): Promise<void> {
    loadInFlight = true;
    loadPending = false;
    const readCatalog = pendingReadCatalog;
    pendingReadCatalog = false;
    try {
      await load(readCatalog);
    } finally {
      loadInFlight = false;
    }
    if (loadPending && !disposed) requestLoad();
  }

  /** The rail's ochre count: the last confirmed read, and no earlier. */
  function publishCount(runs: readonly RunV3[]): void {
    runsWaitingForYou.set(runs.filter((run) => runStanding(run.state) === "waiting").length);
  }

  /**
   * One canonically read run, taken into what this room shows the moment a
   * pinned decision answers or resends (`PinnedDecision`'s own `onRunRead`) --
   * the attention feed's own nudges go through `requestLoad` instead (#1148),
   * since the list a fresh load reads already carries what a nudge would
   * have named.
   *
   * A run that still moves or waits stands here with its fresher truth; one
   * that has ended leaves for History the moment it does, so a pin never
   * lingers as a question the run no longer asks and the shelf never keeps a
   * finished row.
   *
   * Nothing is absorbed while this room holds no confirmed truth of its own --
   * inventing a one-run list out of a stream would dress a pending or failed
   * read up as a room.
   */
  // Returns whether the read could be taken in; see the note above.
  function absorbRun(read: RunV3): boolean {
    const confirmed = live.confirmed;
    if (confirmed === null) return false;
    const runs = absorbAttentionRun(confirmed.runs, read);
    live = updateConfirmed(live, { ...confirmed, runs });
    publishCount(runs);
    return true;
  }

  $: streamTitle = protocolTitle(hold);
  $: snapshot = live.confirmed;
  $: pins = workbenchDecisionPins(snapshot?.runs ?? []).map((run) => ({
    run,
    workflowName: resolveWorkflowName(run, snapshot?.workflowNames ?? null)
  }));
  // Whose terminal this is (#1099 line 5). The seat names its own project
  // while it lives; without one the room still says which project it is,
  // rather than leaving the frame nameless.
  $: seatProject =
    seat.kind === "alive" && seat.projectId !== null ? seat.projectId : THE_ONE_PROJECT;
  $: if (!pins.some((pin) => pin.run.public_run_reference === expandedPinReference)) {
    expandedPinReference = pins[0]?.run.public_run_reference ?? null;
  }
  // Everything the pins do not already hold as a stage: what is moving, and
  // what waits in a shape this room cannot answer inline (a reconciliation, a
  // run of an older format). Each is one row, one click from its graph -- and
  // nothing stands in both places.
  $: shelf = (snapshot?.runs ?? [])
    .filter((run) => !pins.some((pin) => pin.run.public_run_reference === run.public_run_reference))
    .map((run) => ({
      run,
      standing: runStanding(run.state),
      name: resolveWorkflowName(run, snapshot?.workflowNames ?? null),
      at: run.current_node_id,
      move: humanMove(run.state)
    }));
  /** Runs whose own projection failed (#1042): named, never folded into an empty shelf. */
  $: defective = snapshot?.defective ?? [];
</script>

<section class="workbench surface" aria-labelledby="workbench-title">
  <header class="surface-head">
    <h1 id="workbench-title" tabindex="-1" bind:this={roomHeading}>{wrapDisplayCopy(workbenchPageCopy.title)}</h1>
  </header>

  <!-- A healthy stream says nothing: a permanent "live" badge is chrome and a
       first connect is ordinary loading. A stream merely reconnecting is the
       generic reachability loss the central connection store already names once
       above every room (#700); this line speaks only for what is specific to
       this stream -- a real protocol or terminal failure. -->
  {#if streamStopped(hold)}
    <p class="stream-stopped" role="status">
      <span aria-hidden="true">◇</span>
      {wrapDisplayCopy(connectionLabel(hold))}
    </p>
  {/if}
  {#if hold.stream_failure !== null}
    <ProblemNotice problem={hold.stream_failure} />
  {:else if streamTitle !== null}
    <ProblemNotice title={wrapDisplayCopy(streamTitle)} message={protocolDetail(hold) ?? ""} />
  {/if}
  {#if streamFailureMessage !== null}
    <ProblemNotice message={streamFailureMessage} />
  {/if}

  <ReadState
    read={live}
    label={workbenchPageCopy.runsLabel}
    onRetry={() => requestLoad(true)}
  />
  {#if snapshot !== null && snapshot.workflowNames === null}
    <p class="names-notice" role="status">{wrapDisplayCopy(workbenchPageCopy.workflowNamesUnavailable)}</p>
  {/if}

  <PoisonedJournalDoor
    {mutationJournal}
    bind:poisoned={journalPoisoned}
    focusAfterHeal={roomHeading}
    doorAttributes={{ [workbenchQuestionAttribute]: workbenchQuestions.discardPoisonedJournalDoor.id }}
    confirmAttributes={{ [workbenchQuestionAttribute]: workbenchQuestions.discardPoisonedJournalConfirm.id }}
    cancelAttributes={{ [workbenchQuestionAttribute]: workbenchQuestions.discardPoisonedJournalCancel.id }}
  />

  {#if !journalPoisoned}

  {#if pins.length > 0}
    <section class="needs-you" aria-label={wrapDisplayCopy(workbenchPageCopy.pinnedDecisionsLabel)}>
      <ul class="needs-you-list">
        {#each pins as pin (pin.run.public_run_reference)}
          <li>
            <PinnedDecision
              run={pin.run}
              workflowName={pin.workflowName}
              {cockpitApi}
              {mutationJournal}
              onRunRead={(read) => { absorbRun(read); }}
              {navigate}
              compact={pin.run.public_run_reference !== expandedPinReference}
              onExpand={() => { expandedPinReference = pin.run.public_run_reference; }}
              onJournalPoisoned={() => { journalPoisoned = true; }}
            />
          </li>
        {/each}
      </ul>
    </section>
  {/if}

  <!-- The living shelf: what is moving, one click from its graph. No title
       above it -- a framed row that opens says what it is (ADR 0019 §3). -->
  {#if shelf.length > 0}
    <ul class="living-shelf">
      {#each shelf as row (row.run.public_run_reference)}
        {@const path = runPath(row.run.public_run_reference)}
        <li>
          <a
            class="living-row living-row-{row.standing}"
            href={path}
            onclick={(event) => { event.preventDefault(); navigate(path); }}
          >
            <span class="living-mark" aria-hidden="true">{standingMarks[row.standing]}</span>
            <span class="living-name">{row.name}</span>
            <span class="living-at">{row.at}</span>
            {#if row.move !== null}
              <span class="living-move">{wrapDisplayCopy(row.move)} →</span>
            {/if}
          </a>
        </li>
      {/each}
    </ul>
  {/if}

  <!-- A run whose own projection failed (#1042): named apart from the shelf
       it moves runs it could read on, never folded into an empty state and
       never opened -- there is no graph this room can show for it. -->
  {#if defective.length > 0}
    <ul class="living-shelf" aria-label={wrapDisplayCopy(workbenchPageCopy.defectiveRunsLabel)}>
      {#each defective as row (row.public_run_reference)}
        <DefectiveRunRowItem {row} />
      {/each}
    </ul>
  {/if}

  <!-- Nothing is moving and nothing waits: the room still teaches the one
       next move (REQ-UI-24) instead of standing blank above the terminal. -->
  {#if pins.length === 0 && shelf.length === 0 && defective.length === 0 && live.confirmed !== null}
    <div class="workbench-empty card empty-state">
      <h2>{wrapDisplayCopy(workbenchPageCopy.emptyTitle)}</h2>
      <p>{wrapDisplayCopy(workbenchPageCopy.emptyDescription)}</p>
      <a
        class="button primary"
        href={catalogPath}
        {...{ [workbenchQuestionAttribute]: workbenchQuestions.emptyStart.id }}
        onclick={(event) => { event.preventDefault(); navigate(catalogPath); }}
      >{wrapDisplayCopy(workbenchPageCopy.emptyStart)}</a>
    </div>
  {/if}

  <!-- The seat: the operator's own terminal, where the conversation used to
       be. The workshop frames it and says whose it is and what it may do; what
       happens inside belongs to the operator and the agent CLI, and nothing
       here reads or keeps it (#1099). -->
  <section class="seat" aria-label={wrapDisplayCopy(seatCopy.regionLabel)}>
    <p class="seat-bar">
      <strong>{seatProject}</strong>
      <span>{wrapDisplayCopy(seatCopy.attachment)}</span>
    </p>
    {#if seat.kind === "reading"}
      <p class="seat-connecting" role="status">{wrapDisplayCopy(seatCopy.connecting)}</p>
    {:else if seat.kind === "alive" && readableWidth}
      <iframe class="seat-terminal" title={wrapDisplayCopy(seatCopy.terminalTitle)} src={seat.url}></iframe>
    {:else if seat.kind === "alive"}
      <ProblemNotice
        title={wrapDisplayCopy(seatCopy.narrowTitle)}
        message={wrapDisplayCopy(seatCopy.narrowDetail)}
      />
    {:else}
      <ProblemNotice
        title={wrapDisplayCopy(seatCopy.unreachableTitle)}
        message={wrapDisplayCopy(seatCopy.unreachableDetail)}
      />
    {/if}
    <p class="seat-boundary">{wrapDisplayCopy(seatCopy.trustBoundary)}</p>
  </section>

  {/if}
</section>

<style>
  /* The pinned region and the ear are the two fixtures of the Workbench: they
     hold to the top and bottom of the stage while the conversation scrolls
     between them, so an open decision never leaves the screen (issue #580). The
     stage's own ground shows through, so each fixture wears it to occlude the
     lines sliding under its edge. */
  .needs-you {
    position: sticky;
    top: 0;
    z-index: 1;
    display: grid;
    gap: var(--space-3);
    min-height: 0;
    /* One expanded stage and about three compact decisions keep the ear and
       conversation in the 390px room; more remains reachable by this stack's
       own scroll, whose fade is the promised affordance. */
    max-height: calc(var(--tap) * 7 + var(--space-3) * 3);
    overflow-y: auto;
    mask-image: linear-gradient(to bottom, var(--mask-opaque) calc(100% - var(--space-3)), transparent);
    -webkit-mask-image: linear-gradient(to bottom, var(--mask-opaque) calc(100% - var(--space-3)), transparent);
    padding-block: var(--space-3);
    border-bottom: var(--edge) solid var(--line);
    background: var(--ground);
  }

  .needs-you-list {
    display: grid;
    gap: var(--space-3);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .needs-you-list li {
    min-width: 0;
  }

  .names-notice {
    margin: 0;
    color: var(--ink-dim);
    font-size: var(--text-xs);
  }

  .stream-stopped {
    margin: 0;
    color: var(--signal-failure);
  }

  .living-shelf {
    display: grid;
    gap: var(--space-2);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  /* A framed row that opens: what lies on the living shelf is still in hand
     (ADR 0019 §3), unlike History's ruled lines. */
  .living-row {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: var(--space-2) var(--space-3);
    min-height: var(--tap);
    border: var(--edge) solid var(--line);
    border-radius: var(--r-lg);
    padding: var(--space-3) var(--space-4);
    background: var(--panel2);
    color: inherit;
    font-size: var(--text-sm);
    text-decoration: none;
  }

  .living-row:hover,
  .living-row:focus-visible {
    border-color: var(--accent);
  }

  .living-row-running .living-mark {
    color: var(--signal-live);
  }

  .living-row-waiting .living-mark {
    color: var(--signal-attention-mark);
  }

  .living-name {
    font-weight: var(--weight-strong);
    overflow-wrap: anywhere;
  }

  /* Which hand is at work, as the node's own name -- the fact, not a state
     word beside a colour that already says it. */
  .living-at {
    margin-left: auto;
    color: var(--ink-dim);
    font-size: var(--text-xs);
  }

  /* The move a person still owes this run, where there is one: the row's own
     door already opens it, so the words name the move, not the state. */
  .living-move {
    color: var(--accent);
    font-size: var(--text-xs);
    font-weight: var(--weight-strong);
  }

  /* The seat sits where the conversation was: the room's own frame around a
     terminal it neither reads nor styles. The frame carries the border so the
     terminal's own ground can fill it edge to edge. */
  .seat {
    display: grid;
    gap: var(--space-2);
    min-width: 0;
  }

  .seat-bar {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: var(--space-2) var(--space-3);
    margin: 0;
    font-size: var(--text-sm);
  }

  .seat-bar span {
    color: var(--ink-dim);
    font-size: var(--text-xs);
  }

  .seat-terminal {
    /* Tall enough to hold a working session's last screenful without the room
       scrolling under it; the terminal keeps its own scrollback. As wide as
       the room gives it: a terminal reads by its columns. */
    width: 100%;
    height: var(--seat-height);
    min-width: 0;
    border: var(--edge) solid var(--line);
    border-radius: var(--r-lg);
    background: var(--panel2);
  }

  .seat-connecting,
  .seat-boundary {
    margin: 0;
    color: var(--ink-dim);
    font-size: var(--text-xs);
  }
</style>
