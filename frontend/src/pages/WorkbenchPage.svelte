<script lang="ts">
  import { onMount, tick } from "svelte";

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
  import {
    currentChatTranscript,
    sendChatTurn,
    subscribeChatTranscript,
    type ChatMessage
  } from "../lib/chatTranscript";
  import {
    answerConductorWait,
    conductorConversationCopy,
    decodeConductorEvent,
    emptyConductorTranscript,
    rememberConductorRun,
    rememberedConductorRun,
    reduceConductorEvent,
    startConductorConversation,
    type ConductorMessage,
    type ConductorTranscript
  } from "../lib/conductorConversation";
  import { conductorChatCopy } from "../lib/conductorChatCopy";
  import {
    conductorConversationShape,
    orderedConductorCandidates,
    resolveConductorConnection,
    type ConductorConnectionState
  } from "../lib/conductorEpisode";
  import { connectionState, onConnectionRecovered, restartNoticeCopy } from "../lib/connectionState";
  import { wrapDisplayCopy } from "../lib/displayCopy";
  import { humanErrorMessage } from "../lib/humanRefusal";
  import { JournalUnreadableError, type MutationJournal } from "../lib/mutationJournal";
  import {
    beginRead,
    confirmRead,
    failRead,
    retainedRead,
    updateConfirmed,
    type RetainedRead
  } from "../lib/readResource";
  import { runPageCopy } from "../lib/runPageCopy";
  import { runPath } from "../lib/route";
  import { newestReadOfEachRun, resolveWorkflowName, splitRunListRows } from "../lib/runList";
  import { readEveryRevision, readEveryRun } from "../lib/runPages";
  import { loadPendingWaitAnswer, type PendingWaitLookup } from "../lib/waitAnswerDelivery";
  import { humanMove, runHasEnded, runStanding, standingMarks } from "../lib/runState";
  import {
    connectionLabel,
    protocolDetail,
    protocolTitle,
    streamStopped
  } from "../lib/streamStatus";
  import { ageLabel } from "../lib/when";
  import { absorbAttentionRun, workbenchDecisionPins } from "../lib/workbenchAttention";
  import { workbenchPageCopy } from "../lib/workbenchPageCopy";
  import { workbenchQuestionAttribute, workbenchQuestions } from "../lib/workbenchQuestions";
  import { WORKSHOP_DESTINATION, runsWaitingForYou } from "../lib/workshop";

  /**
   * The Workbench: what wants you now, what is moving, what was said, and the
   * ear (ADR 0019 §1).
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
   * Whether a conductor reads this ear. "reading" is the moment before the
   * answer is known and "unreadable" the honest state when the reads themselves
   * failed -- neither pretends any of `ConductorConnectionState`'s own answers,
   * because either would be a guess dressed as a fact.
   */
  type ConductorLink = { kind: "reading" } | { kind: "unreadable" } | ConductorConnectionState;

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
  let transcript: readonly ChatMessage[] = currentChatTranscript();
  let conversationTranscript: readonly (ChatMessage | ConductorMessage)[] = transcript;
  let conductorTranscript: ConductorTranscript = emptyConductorTranscript();
  let conductorRun: RunV3 | null = null;
  let conductorStream: RunEventSubscription | null = null;
  let conductorStreamReference: string | null = null;
  let firstConversationMessage: string | null = null;
  let conductorDeliveryBusy = false;
  let conductorDeliveryFailure: string | null = null;
  /**
   * Every conductor message that failed to send, kept apart from the durable
   * transcript (none of them ever became one of its events) so each can
   * stand in the conversation with its own resend control instead of
   * vanishing the moment the composer moved on, or a second failure erasing
   * the first (#1078 B4, #1078 review).
   */
  let failedConductorMessages: string[] = [];
  let typed = "";
  let expandedPinReference: string | null = null;
  let composer: { focus(): void };
  let conductorLink: ConductorLink = { kind: "reading" };

  /**
   * Whether this browser's own memory of pending sendings can be read at all
   * (#914). A poisoned journal blocks every read that would otherwise show a
   * pinned decision or the conductor's own pending wait, so the whole room
   * stands behind `PoisonedJournalDoor`'s one honest sentence and its one
   * door instead of quietly never showing the cards that would have read it.
   */
  let journalPoisoned = false;
  let roomHeading: { focus(): void };

  const speakerLabels: Record<ChatMessage["speaker"], string> = {
    you: workbenchPageCopy.youLabel,
    house: workbenchPageCopy.houseLabel
  };

  const catalogPath = WORKSHOP_DESTINATION.catalog.path;
  const settingsPath = WORKSHOP_DESTINATION.settings.path;

  onMount(() => {
    requestLoad(true);
    holdAttention();
    void resolveConductor();
    const unsubscribe = subscribeChatTranscript((next) => {
      transcript = next;
    });
    // A read that failed while the connection was lost stays failed once the
    // connection returns until something asks again -- reload was the only
    // way out (#700). The described catalog and the conductor revision
    // resolution return here too, same as a fresh open (#1148 REVISE M1):
    // the connection was down long enough that either could have moved, and
    // the attention feed carries no event that would say so on its own.
    const unsubscribeConnection = onConnectionRecovered(() => {
      requestLoad(true);
      void resolveConductor();
    });
    return () => {
      disposed = true;
      stream?.close();
      stream = null;
      conductorStream?.close();
      conductorStream = null;
      unsubscribe();
      unsubscribeConnection();
    };
  });

  async function resolveConductor(): Promise<void> {
    try {
      const state = await resolveConductorConnection(cockpitApi);
      conductorLink = state;
      if (state.kind === "connected" && live.confirmed !== null) {
        void selectConductorConversation(live.confirmed.runs);
      }
      if (state.kind === "connected") void restoreConductorConversation();
    } catch {
      conductorLink = { kind: "unreadable" };
    }
  }

  /**
   * Re-reads the conductor's own pending wait once the journal has just
   * healed (#914 second half, #1131): un-hiding the pinned-decision region
   * already lets each pin mount and read the now-healthy journal on its
   * own; the conductor's own pending wait needs this one explicit nudge,
   * since nothing else prompts it right now.
   */
  function healJournal(): void {
    if (conductorRun !== null) void refreshConductorRun(conductorRun.public_run_reference);
  }

  function followConductor(run: RunV3): void {
    if (conductorStreamReference === run.public_run_reference) return;
    conductorStream?.close();
    conductorStream = null;
    conductorStreamReference = run.public_run_reference;
    rememberConductorRun(sessionStorage, run.public_run_reference);
    conductorTranscript = emptyConductorTranscript();
    conductorStream = cockpitApi.openRunEvents(run.public_run_reference, {
      opened: () => {},
      event: (rawData) => {
        const event = decodeConductorEvent(rawData);
        if (event === null) return;
        conductorTranscript = reduceConductorEvent(conductorTranscript, event);
        void refreshConductorRun(run.public_run_reference);
      },
      disconnected: () => {}
    });
  }

  async function restoreConductorConversation(): Promise<void> {
    const publicRunReference = rememberedConductorRun(sessionStorage);
    if (publicRunReference === null || conductorRun !== null) return;
    try {
      const run = await cockpitApi.getRun(publicRunReference);
      const revision = await cockpitApi.getWorkflowRevision(run.workflow_revision_hash);
      if (conductorConversationShape(revision) === null || conductorRun !== null) return;
      conductorRun = run;
      followConductor(run);
      await refreshConductorRun(run.public_run_reference);
    } catch {
      // A stale local reference is not a conversation. The normal live-run
      // selection remains the only fallback instead of inventing one.
    }
  }

  /**
   * Walks the run list newest first (`orderedConductorCandidates`'s own
   * tie-break) and resolves one candidate's revision at a time, stopping at
   * the first conductor-shaped one (#1148 REVISE M3): the ordinary room,
   * where the newest non-terminal run already carries the answer, costs one
   * `getWorkflowRevision` lookup, never one per distinct revision the run
   * list happens to carry.
   */
  async function selectConductorConversation(runs: readonly RunV3[]): Promise<void> {
    const checkedHashes: string[] = [];
    let selected: RunV3 | null = null;
    for (const run of orderedConductorCandidates(runs)) {
      const workflowRevisionHash = run.workflow_revision_hash;
      if (checkedHashes.includes(workflowRevisionHash)) continue;
      checkedHashes.push(workflowRevisionHash);
      try {
        const revision = await cockpitApi.getWorkflowRevision(workflowRevisionHash);
        if (conductorConversationShape(revision) !== null) {
          selected = run;
          break;
        }
      } catch {
        // An unreadable revision names no conductor either; the next
        // candidate's own revision still gets its own honest answer.
      }
    }
    if (selected === null || selected.public_run_reference === conductorRun?.public_run_reference) return;
    conductorRun = selected;
    followConductor(selected);
    void refreshConductorRun(selected.public_run_reference);
  }

  /**
   * The conductor's own pending-wait read, kept apart from `refreshConductorRun`'s
   * outer catch so a poisoned journal is told apart from an ordinary network
   * failure -- the outer catch's "retry next frame" comment is honest only for
   * the latter (#914). A poisoned journal instead raises the room's one shared
   * notice, the same one a pinned decision's own read would raise if it ran.
   */
  async function readPendingConductorWait(run: RunV3): Promise<PendingWaitLookup | null> {
    try {
      return await loadPendingWaitAnswer(
        mutationJournal,
        run.public_run_reference,
        run.workflow_revision_hash,
        run.current_node_id,
        run.current_node_execution_id
      );
    } catch (error) {
      if (!(error instanceof JournalUnreadableError)) throw error;
      journalPoisoned = true;
      return null;
    }
  }

  async function refreshConductorRun(publicRunReference: string): Promise<void> {
    try {
      const refreshed = await cockpitApi.getRun(publicRunReference);
      if (conductorRun?.public_run_reference !== publicRunReference) return;
      conductorRun = refreshed;
      if (firstConversationMessage !== null && refreshed.state === "WAITING_INPUT") {
        const firstMessage = firstConversationMessage;
        firstConversationMessage = null;
        await deliverConductorMessage(refreshed, firstMessage);
        return;
      }
      if (refreshed.state === "WAITING_INPUT") {
        const pending = await readPendingConductorWait(refreshed);
        if (pending === null) return;
        conductorDeliveryBusy = pending.kind === "found";
        if (pending.kind === "corrupt") conductorDeliveryFailure = pending.message;
      } else {
        conductorDeliveryBusy = refreshed.state === "STARTED" || refreshed.state === "WAITING_RECONCILIATION";
      }
    } catch (error) {
      if (firstConversationMessage !== null) {
        // The run itself did start (`send` already got a run back), so this
        // read failure is not "nothing was sent" -- it is the one message
        // still waiting to become that run's first wait answer. Swallowing
        // it here left the composer empty with no error and no way back
        // (#1078 B4): it now fails the same way any other delivery does,
        // with the text kept and a resend in the transcript.
        const pendingMessage = firstConversationMessage;
        firstConversationMessage = null;
        conductorDeliveryBusy = false;
        recordFailedConductorMessage(
          pendingMessage,
          humanErrorMessage(error, runPageCopy.answerUnconfirmed)
        );
        return;
      }
      // The stream remains the durable transcript. The next frame retries the
      // canonical run read without replacing it with a guess.
    }
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
   * `readCatalog` tells the two reads bundled into that same shape apart
   * (#1148 REVISE M1): the described catalog (`listWorkflowRevisions`, this
   * room's `workflowNames`) and, through `confirm`, which revisions carry a
   * conductor conversation (`getWorkflowRevision` per unique hash). Both cost
   * one read per revision the run list currently names, so a nudge -- which
   * names no catalog change of its own, only a run to look at again -- keeps
   * whatever this room already confirmed for both instead of paying that
   * cost again on every event.
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
      confirm(begun.generation, { runs, defective, workflowNames }, readCatalog);
    } catch {
      live = failRead(live, begun.generation, {
        kind: "unavailable",
        title: wrapDisplayCopy(workbenchPageCopy.runsUnavailable)
      });
    }
  }

  function confirm(generation: number, confirmed: WorkbenchRuns, readCatalog: boolean): void {
    const before = live;
    live = confirmRead(live, generation, confirmed);
    if (live === before) return;
    publishCount(confirmed.runs);
    // Resolving which revision carries a conductor conversation is the same
    // per-hash catalog cost `readCatalog` already named (#1148 REVISE M1): a
    // nudge that only refreshed the run lists keeps whichever conversation
    // is already followed instead of asking `getWorkflowRevision` again for
    // every hash the list now shows.
    if (readCatalog && conductorLink.kind === "connected") {
      void selectConductorConversation(confirmed.runs);
    }
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

  /**
   * A connected conductor starts one loop run for the first message, then
   * turns every later message into its current wait answer. "unreadable"
   * keeps the standing honest local refusal, where nothing was started is
   * still the whole truth. "absent", "unbound", "not-startable" and
   * "reading" (#1103, #1114) are a different kind of state: a real reason —
   * or, for "reading", a real unknown still in flight — the composer is
   * locked for, so nothing is sent at all rather than accepted and
   * swallowed. A message sent while "reading" would otherwise fall into the
   * "unreadable"/local-chat branch below and be answered locally, and the
   * moment the read resolves to "connected" that locally-answered turn has
   * nothing to do with the real conversation the operator meant to start.
   *
   * A lost connection (#700), or one of those four locked states, keeps the
   * message in the box instead: the send button is disabled the same moment,
   * so this guard only catches the keyboard's Enter shortcut racing that
   * disable.
   */
  async function send(event: Event): Promise<void> {
    event.preventDefault();
    await attemptSend(typed.trim());
  }

  /**
   * The transcript's own Resend control: the same send path, the failed
   * text -- but narrowed to the conductor path only (#1078 fix round 3,
   * finding 3). A fresh message while the link is "unreadable" may still be
   * answered by the local-chat fallback below in `attemptSend` (by design,
   * `chatTranscript.ts`); a message that already failed to reach the
   * conductor must never take that fallback instead, or a Resend the
   * operator meant for the real conversation would be answered by a house
   * sentence that never reached it. The failed line stays standing,
   * unchanged, until the link reads "connected" again.
   */
  async function resendFailedConductorMessage(message: string): Promise<void> {
    if (conductorLink.kind !== "connected") return;
    // Removed inside `attemptSend`, only once its guards pass and only for
    // this exact message (#1078 review): an early return here must not lose
    // the standing failed line, and a second failure elsewhere in the list
    // must not disappear either (#1078 review).
    await attemptSend(message);
  }

  /**
   * The one place a conductor message leaves the composer, whether typed and
   * submitted or resent from a failed line. The composer's own text is never
   * cleared here before a write is confirmed (#1078 B4): the connected
   * branches below only ever clear `typed` once `deliverConductorMessage`
   * itself confirms a send, and a failure instead appends the attempted text
   * to `failedConductorMessages` so the transcript can offer Resend on each
   * failed line without the operator having to remember or retype it.
   */
  async function attemptSend(message: string): Promise<void> {
    if (message.length === 0 || $connectionState === "reconnecting" || composerLocked) return;
    if (conductorLink.kind === "connected") {
      if (
        conductorDeliveryBusy ||
        (conductorRun !== null &&
          conductorRun.state !== "WAITING_INPUT" &&
          !runHasEnded(conductorRun.state))
      ) {
        return;
      }
      conductorDeliveryFailure = null;
      // Removed only when this attempt is for a standing failed message of
      // its own (#1078 review): an unrelated new send, or a second failure,
      // must not destroy a failed line that was never resent.
      failedConductorMessages = failedConductorMessages.filter((failed) => failed !== message);
      if (conductorRun?.state === "WAITING_INPUT") {
        await deliverConductorMessage(conductorRun, message);
      } else {
        conductorDeliveryBusy = true;
        firstConversationMessage = message;
        try {
          conductorRun = await startConductorConversation(cockpitApi, conductorLink.connection);
          followConductor(conductorRun);
          await refreshConductorRun(conductorRun.public_run_reference);
        } catch (error) {
          conductorDeliveryBusy = false;
          firstConversationMessage = null;
          recordFailedConductorMessage(message, humanErrorMessage(error, conductorChatCopy.startRefused));
        }
      }
    } else {
      // The local-chat fallback stays on this page, but it shares the one
      // send path every other branch uses (#1078 review): the caller's own
      // already-trimmed `message`, and the composer clears only when it
      // still holds the exact text that was sent.
      sendChatTurn(message);
      transcript = currentChatTranscript();
      if (typed.trim() === message) typed = "";
    }
    await tick();
    composer.focus();
  }

  function recordFailedConductorMessage(message: string, reason: string): void {
    failedConductorMessages = [...failedConductorMessages, message];
    conductorDeliveryFailure = reason;
  }

  async function deliverConductorMessage(run: RunV3, message: string): Promise<void> {
    conductorDeliveryBusy = true;
    try {
      const outcome = await answerConductorWait(cockpitApi, mutationJournal, run, message);
      if (outcome.kind === "failed") {
        recordFailedConductorMessage(message, outcome.message);
        return;
      }
      conductorRun = outcome.run;
      // The write is confirmed (or accepted-uncertain, #959's own retry
      // journal still covers that case) -- only now does the composer that
      // held this exact text actually clear (#1078 B4), and only when it
      // still holds it: a resend or a deferred first message can confirm a
      // send while the composer already moved on to something else.
      if (typed.trim() === message) typed = "";
    } catch (error) {
      // A retry of an already-open wait with edited text can conflict with
      // its own earlier, differently-worded attempt still in the journal
      // (mutationJournal.ts) -- the failed line's Resend reuses this same
      // function instead of leaving the room silently stuck (#959).
      recordFailedConductorMessage(message, humanErrorMessage(error, runPageCopy.answerUnconfirmed));
    } finally {
      conductorDeliveryBusy = false;
    }
  }

  /**
   * Enter sends, Shift+Enter keeps writing -- the shape every composer has, and
   * the reason the field is a textarea: a message to the house is often more
   * than one line.
   */
  function keydown(event: KeyboardEvent): void {
    if (event.key !== "Enter" || event.shiftKey) return;
    void send(event);
  }

  $: streamTitle = protocolTitle(hold);
  $: snapshot = live.confirmed;
  $: pins = workbenchDecisionPins(snapshot?.runs ?? [])
    .filter(
      (run) =>
        run.public_run_reference !== conductorRun?.public_run_reference &&
        (conductorLink.kind !== "connected" ||
          run.workflow_revision_hash !== conductorLink.connection.workflowRevisionHash)
    )
    .map((run) => ({
    run,
    workflowName: resolveWorkflowName(run, snapshot?.workflowNames ?? null)
  }));
  $: conversationTranscript = conductorLink.kind === "connected"
    ? conductorTranscript.messages
    : transcript;
  $: conversationRunPath =
    conductorLink.kind === "connected" && conductorRun !== null
      ? runPath(conductorRun.public_run_reference)
      : null;
  $: conversationComplete = conductorLink.kind === "connected" &&
    conductorRun?.state === "COMPLETED" &&
    conductorTranscript.messages.filter((message) => message.speaker === "house").length >=
      conductorLink.connection.maximumRounds;
  // "begins" only holds before the conversation's own first round has
  // actually landed; once one has, the same wait's next message continues it.
  $: connectedComposerHint =
    conductorRun !== null && runHasEnded(conductorRun.state) && conductorRun.state !== "COMPLETED"
      ? conductorConversationCopy.endedHint
      : conductorTranscript.messages.length > 0
        ? conductorConversationCopy.composerHintOngoing
        : conductorConversationCopy.composerHint;
  // The one relative rendering of a failed probe's own instant (`when.ts`
  // owns the formatting); null for every other not-startable reason.
  $: probeFailedAgo =
    conductorLink.kind === "not-startable" &&
    conductorLink.notStartableReason === "provider-probe-failed" &&
    conductorLink.providerProbeObservedAt !== null
      ? ageLabel(conductorLink.providerProbeObservedAt, new Date(), "ago")
      : null;
  // The composer is visibly locked, not merely quiet, whenever the server
  // named a real reason nothing can be sent (#1103), or whenever whether a
  // conductor is even there is still unknown ("reading", #1114): sending
  // then would silently fall into the local-chat branch and be answered as
  // if no conductor existed, and the operator's words would part ways with
  // the conversation the moment the read resolves. This is the one flag both
  // the button's `disabled` attribute and the Enter-key guard in `send`
  // read, so the two can never drift apart.
  $: composerLocked =
    conductorLink.kind === "absent" ||
    conductorLink.kind === "unbound" ||
    conductorLink.kind === "not-startable" ||
    conductorLink.kind === "reading";
  // Send's own condition -- a busy or locked composer cannot send behind the
  // operator's back.
  $: sendDisabled =
    $connectionState === "reconnecting" ||
    conductorDeliveryBusy ||
    composerLocked ||
    (conductorRun !== null && conductorRun.state !== "WAITING_INPUT" && !runHasEnded(conductorRun.state));
  // The transcript's own Resend narrows further than Send (#1078 fix round
  // 3, finding 3): every reason `sendDisabled` names still applies, plus one
  // Resend alone must refuse -- the link reading "unreadable" -- because a
  // fresh message there is allowed to fall into the local-chat fallback by
  // design, but a message that already failed the real conductor must never
  // take that fallback on Resend. `resendFailedConductorMessage` reads the
  // same `conductorLink.kind !== "connected"` guard, so the two can never
  // drift apart.
  $: resendDisabled = sendDisabled || conductorLink.kind !== "connected";
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
    onHealed={healJournal}
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

  {#if conversationTranscript.length === 0 && failedConductorMessages.length === 0}
    <div class="workbench-empty card empty-state">
      <h2>{wrapDisplayCopy(workbenchPageCopy.emptyTitle)}</h2>
      {#if conductorLink.kind === "connected"}
        <p>{wrapDisplayCopy(conductorConversationCopy.emptyDescription)}</p>
      {:else if conductorLink.kind === "unbound"}
        <p>{wrapDisplayCopy(workbenchPageCopy.emptyDescriptionUnbound(conductorLink.role))}</p>
        <a
          class="button primary"
          href={settingsPath}
          {...{ [workbenchQuestionAttribute]: workbenchQuestions.emptyOpenSettings.id }}
          onclick={(event) => { event.preventDefault(); navigate(settingsPath); }}
        >{wrapDisplayCopy(workbenchPageCopy.openSettings)}</a>
      {:else if conductorLink.kind === "not-startable"}
        <p>
          {wrapDisplayCopy(
            workbenchPageCopy.emptyDescriptionNotStartable(
              conductorLink.modelId,
              conductorLink.notStartableReason,
              probeFailedAgo
            )
          )}
        </p>
        <a
          class="button primary"
          href={settingsPath}
          {...{ [workbenchQuestionAttribute]: workbenchQuestions.emptyOpenSettings.id }}
          onclick={(event) => { event.preventDefault(); navigate(settingsPath); }}
        >{wrapDisplayCopy(workbenchPageCopy.openSettings)}</a>
      {:else}
        <p>{wrapDisplayCopy(workbenchPageCopy.emptyDescription)}</p>
        <a
          class="button primary"
          href={catalogPath}
          {...{ [workbenchQuestionAttribute]: workbenchQuestions.emptyStart.id }}
          onclick={(event) => { event.preventDefault(); navigate(catalogPath); }}
        >{wrapDisplayCopy(workbenchPageCopy.emptyStart)}</a>
      {/if}
    </div>
  {:else}
    <ol class="conversation" aria-label={wrapDisplayCopy(workbenchPageCopy.transcriptLabel)}>
      {#each conversationTranscript as message (message.id)}
        <li class="conversation-line conversation-line-{message.speaker}">
          <p class="conversation-message">
            <span class="conversation-speaker">{wrapDisplayCopy(speakerLabels[message.speaker])}</span>
            {message.text}
          </p>
        </li>
      {/each}
      {#each failedConductorMessages as failedMessage (failedMessage)}
        <!-- Never one of the durable transcript's own events (the write it
             would have produced never confirmed, #1078 B4): a local line,
             kept only until its own Resend or a later successful send for
             the same text removes it -- a second failure stands beside the
             first rather than replacing it (#1078 review). Keyed by the
             message text itself, not position: an index key would let
             removing an earlier failure reuse a later failure's own DOM
             node and event handler instead of destroying the right one. -->
        <li class="conversation-line conversation-line-you conversation-line-failed">
          <p class="conversation-message">
            <span class="conversation-speaker">{wrapDisplayCopy(speakerLabels.you)}</span>
            {failedMessage}
          </p>
          <p class="conversation-failed-notice" role="status">
            {wrapDisplayCopy(workbenchPageCopy.conductorMessageFailed)}
            <!-- `resendDisabled`, not `sendDisabled` (#1078 fix round 3,
                 finding 3): while the link reads "unreadable" the composer
                 hint below already names why nothing can go out
                 (`conductorChatCopy.connectionUnknown`), and this same
                 sentence is why Resend stays locked here too, instead of
                 silently answering this failed line from the local-chat
                 fallback that hint describes. -->
            <button
              type="button"
              disabled={resendDisabled}
              onclick={() => resendFailedConductorMessage(failedMessage)}
              {...{ [workbenchQuestionAttribute]: workbenchQuestions.resendConductorMessage.id }}
            >{wrapDisplayCopy(workbenchPageCopy.resendConductorMessage)}</button>
          </p>
        </li>
      {/each}
    </ol>
    <!-- One link for the whole conversation, because the whole conversation is
         one run (#658). Per line it was the episode model speaking, where each
         message had a run of its own; here it would repeat the same href once
         per round, up to the loop's ceiling. -->
    {#if conversationRunPath !== null}
      <p class="conversation-run">
        <a
          class="conversation-run-link"
          href={conversationRunPath}
          onclick={(event) => {
            event.preventDefault();
            navigate(conversationRunPath);
          }}
        >{wrapDisplayCopy(conductorChatCopy.openEpisode)}</a>
      </p>
    {/if}
  {/if}

  <form class="composer" aria-label={wrapDisplayCopy(workbenchPageCopy.composerRegionLabel)} onsubmit={send}>
    <label class="composer-label" for="workbench-message">
      {wrapDisplayCopy(workbenchPageCopy.composerLabel)}
    </label>
    <div class="composer-row">
      <textarea
        id="workbench-message"
        rows="2"
        bind:value={typed}
        bind:this={composer}
        onkeydown={keydown}
      ></textarea>
      <button
        class="primary"
        type="submit"
        disabled={sendDisabled}
        {...{ [workbenchQuestionAttribute]: workbenchQuestions.saySomething.id }}
      >{wrapDisplayCopy(workbenchPageCopy.send)}</button>
    </div>
    {#if $connectionState === "reconnecting"}
      <!-- The ear always names its own state in one sentence (HEART, "The
           ear"): while the connection is lost that sentence is this one,
           replacing whatever it would otherwise say, and no separate banner
           repeats it above (#700, App.svelte). -->
      <p class="composer-hint">{wrapDisplayCopy(restartNoticeCopy)}</p>
    {:else if conductorLink.kind === "connected"}
      <p class="composer-hint">{wrapDisplayCopy(connectedComposerHint)}</p>
    {:else if conductorLink.kind === "reading"}
      <p class="composer-hint">{wrapDisplayCopy(workbenchPageCopy.composerHintReading)}</p>
    {:else if conductorLink.kind === "absent"}
      <p class="composer-hint">{wrapDisplayCopy(workbenchPageCopy.composerHint)}</p>
    {:else if conductorLink.kind === "unbound" && conversationTranscript.length > 0}
      <!-- The empty room's own card already names this exact reason (above,
           `emptyDescriptionUnbound`) whenever the conversation is empty; this
           hint only repeats it when that card is not on screen -- the same
           complementary rule as "not-startable" below (#1103). -->
      <p class="composer-hint">{wrapDisplayCopy(workbenchPageCopy.composerHintUnbound(conductorLink.role))}</p>
    {:else if conductorLink.kind === "not-startable" && conversationTranscript.length > 0}
      <!-- The empty room's own card already names this exact reason (below,
           `emptyDescriptionNotStartable`) whenever the conversation is empty;
           this hint only repeats it when that card is not on screen -- a
           not-startable conductor reached after "unreadable" had already
           taken a locally-answered turn, then the connection recovered and
           resolved to a real reason (#700, #1103). -->
      <p class="composer-hint">
        {wrapDisplayCopy(
          workbenchPageCopy.composerHintNotStartable(
            conductorLink.modelId,
            conductorLink.notStartableReason,
            probeFailedAgo
          )
        )}
      </p>
    {:else if conductorLink.kind === "unreadable"}
      <p class="composer-hint">{wrapDisplayCopy(conductorChatCopy.connectionUnknown)}</p>
    {/if}
    {#if conversationComplete}
      <p class="composer-hint" role="status">{wrapDisplayCopy(conductorConversationCopy.complete)}</p>
    {/if}
    {#if conductorDeliveryFailure !== null}
      <p class="composer-hint" role="status">{conductorDeliveryFailure}</p>
    {/if}
  </form>
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

  .conversation {
    display: grid;
    gap: var(--space-3);
    max-width: var(--reading-width);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .conversation-line {
    display: flex;
    min-width: 0;
  }

  .conversation-line-you {
    justify-content: flex-end;
  }

  .conversation-message {
    display: grid;
    gap: var(--space-1);
    max-width: 92%;
    margin: 0;
    border: var(--edge) solid var(--line);
    border-radius: var(--r-lg);
    padding: var(--space-3) var(--space-4);
    background: var(--panel2);
    font-size: var(--text-sm);
    overflow-wrap: anywhere;
    /* A conductor reply may carry its own line breaks; they are part of what
       it said. */
    white-space: pre-line;
  }

  .conversation-run {
    max-width: var(--reading-width);
    margin: var(--space-2) 0 0;
  }

  .conversation-run-link {
    font-size: var(--text-xs);
  }

  /* Your own line is the paper one shade deeper, mixed from the ground the
     workshop already uses rather than a grey pasted onto a warm house. */
  .conversation-line-you .conversation-message {
    border-color: color-mix(in srgb, var(--ink) 20%, var(--line));
    background: var(--chip);
  }

  .conversation-speaker {
    color: var(--ink-dim);
    font-size: var(--text-2xs);
    font-weight: var(--weight-strong);
  }

  /* The one line that never became a durable transcript event: the same
     attention color every other refusal on this page already wears
     (`.notice`, `styles.css`), so a failed send reads as a problem at a
     glance rather than as an ordinary reply. */
  .conversation-line-failed .conversation-message {
    border-color: color-mix(in srgb, var(--signal-attention-mark) 45%, var(--line));
  }

  .conversation-failed-notice {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    max-width: 92%;
    margin: var(--space-1) 0 0;
    margin-left: auto;
    color: var(--signal-attention);
    font-size: var(--text-xs);
  }

  .composer {
    position: sticky;
    bottom: 0;
    display: grid;
    gap: var(--space-2);
    max-width: var(--reading-width);
    padding-top: var(--space-3);
    border-top: var(--edge) solid var(--line);
    background: var(--ground);
  }

  .composer-label {
    color: var(--ink-dim);
    font-size: var(--text-xs);
    font-weight: var(--weight-strong);
  }

  .composer-row {
    display: flex;
    align-items: flex-end;
    gap: var(--space-3);
  }

  .composer-row textarea {
    flex: 1;
    font-family: var(--sans);
    font-size: var(--text-sm);
  }

  .composer-hint {
    margin: 0;
    color: var(--ink-dim);
    font-size: var(--text-xs);
  }

  @media (max-width: 48rem) {
    .composer {
      gap: var(--space-1);
      padding-top: var(--space-1);
    }

    /* The sticky rail overlays the stream at this narrow height. Keep the
       first visible conversation line below its fade without spending more
       room in the document flow that anchors the composer. */
    .needs-you ~ .conversation {
      translate: 0 var(--space-5);
    }
  }
</style>
