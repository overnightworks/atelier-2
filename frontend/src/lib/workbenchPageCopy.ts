/**
 * Copy the Workbench renders: the decisions that need the operator and the
 * runs that are moving. What the operator says to an agent is said in the
 * terminal seat, whose own frame is owned by `seatCopy.ts`.
 *
 * The Workbench owns what wants you now and what is moving (ADR 0019 §1). A
 * decision is pinned until it is answered, so a request can never scroll away
 * in a growing stream (issue #580); a run that is moving lies on the living
 * shelf beneath, one click from its graph.
 *
 * No issue number appears in any string below (Adressaten-Regel, operator
 * ruling 23.08.): repository bookkeeping is not operator-facing copy.
 *
 * What a run's *state* is called is deliberately not owned here: `standingWords`
 * in `runState.ts` owns that one word, and every room reads it, so a run cannot
 * be "Done" on one surface and "Completed" on another (operator ruling 23.08.).
 */
export const workbenchPageCopy = {
  title: "Workbench",

  openTheRun: "open the run",
  answerDecision: "Answer →",
  /** HEART "Decision as stage": the warm sentence after a successful answer. */
  answerLanded: "Your answer landed.",
  pinnedDecisionsLabel: "Open decisions",

  runsIncomplete: "Workbench runs incomplete",
  /**
   * What the room says when the live hold itself fails. The word for the
   * stream's own state is not owned here: `connectionLabels` in
   * `streamStatus.ts` owns it for every surface that holds a stream.
   */
  streamUnstartable: "The live hold on this workshop could not start.",
  runsUnavailable: "Workbench runs unavailable",
  runsLabel: "workbench runs",
  workflowNamesUnavailable: "Workflow names unavailable — showing run ids.",
  /**
   * A run whose own projection failed (#1042) reads as this quiet row, not
   * as an empty shelf and not as the whole room failing: the other runs
   * beside it read fine, and this is the one honest thing left to say about
   * the run that does not. `DefectiveRunRow.svelte` renders this row and
   * owns these three strings for every surface that lists runs, History
   * included -- one copy owner, not a second set of words for the same row
   * (operator ruling, #1042 review).
   */
  defectiveRunsLabel: "Runs that could not be read",
  defectiveRunTitle: "Could not be read",
  defectiveRunDetail: "Technical detail",

  emptyTitle: "Nothing is moving",
  /**
   * The empty room teaches the one next move (REQ-UI-24) instead of standing
   * blank: nothing waits and nothing runs, so the Catalog is where the next
   * run is started by hand -- the terminal below is the other way, and says
   * so itself.
   */
  emptyDescription: "Nothing waits on you and nothing is running. Start work from the Catalog, or from your terminal below.",
  emptyStart: "Open the Catalog"
} as const;
