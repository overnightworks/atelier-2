import type { DefectiveRunRow, RunListRow, RunV3 } from "../api/client";
import { parseUtc } from "./when";

/**
 * Last known movement on a run: the end if the wire has one, otherwise the start.
 * The list has no other clock. A row with neither stamp has none.
 */
export function runActivityAt(run: RunV3): string | null {
  return run.ended_at ?? run.started_at ?? null;
}

/**
 * Newest activity first. Rows without a stamp stay at the end in the order
 * the durable list already gave, because inventing a time would rank them.
 */
export function newestActivityFirst(runs: readonly RunV3[]): RunV3[] {
  return [...runs].sort((left, right) => {
    const leftAt = activityMs(left);
    const rightAt = activityMs(right);
    if (leftAt === null && rightAt === null) return 0;
    if (leftAt === null) return 1;
    if (rightAt === null) return -1;
    return rightAt - leftAt;
  });
}

function activityMs(run: RunV3): number | null {
  const stamp = runActivityAt(run);
  if (stamp === null) return null;
  const ms = parseUtc(stamp).getTime();
  return Number.isFinite(ms) ? ms : null;
}

/** The run a listed row names, healthy or defective alike (#1042). */
export function runListRowReference(row: RunListRow): string {
  return row.kind === "run" ? row.run.public_run_reference : row.public_run_reference;
}

/**
 * One entry per run, keeping the fresher read of it.
 *
 * A surface that asks several state lists at one moment gets them answered
 * separately, so a run that moves between two of those answers -- a wait that
 * opens while the started list is still on the wire -- comes back in both. A
 * run resource always outranks a defective row for the same reference: it is
 * strictly the more informative read. Between two run resources, the higher
 * `state_version` is the run's truth; between two defective rows, the later
 * read stands, in the absence of any state to compare.
 */
export function newestReadOfEachRun(rows: readonly RunListRow[]): RunListRow[] {
  const newest = new Map<string, RunListRow>();
  for (const row of rows) {
    const known = newest.get(runListRowReference(row));
    if (known === undefined || rowSupersedes(known, row)) {
      newest.set(runListRowReference(row), row);
    }
  }
  return [...newest.values()];
}

function rowSupersedes(known: RunListRow, candidate: RunListRow): boolean {
  if (known.kind === "defective") return true;
  if (candidate.kind === "defective") return false;
  return known.run.state_version <= candidate.run.state_version;
}

/**
 * The healthy runs and the defective rows of one read, told apart (#1042).
 *
 * Every reader downstream of the durable list -- the pinned decisions, the
 * living shelf, History -- already reasons about a run's own fields, so this
 * is where the union the wire answers with splits into the two shapes that
 * reasoning can use, once, rather than in every reader.
 */
export function splitRunListRows(rows: readonly RunListRow[]): {
  runs: RunV3[];
  defective: DefectiveRunRow[];
} {
  const runs: RunV3[] = [];
  const defective: DefectiveRunRow[] = [];
  for (const row of rows) {
    if (row.kind === "run") {
      runs.push(row.run);
    } else {
      defective.push(row);
    }
  }
  return { runs, defective };
}

/**
 * The defective rows with `row` among them, once per run.
 *
 * A run can be named unreadable by both the run list and the attention feed,
 * and the feed names it again on every reconnect; the room shows it once.
 */
export function withDefectiveRow(
  rows: readonly DefectiveRunRow[],
  row: DefectiveRunRow
): DefectiveRunRow[] {
  if (rows.some((known) => known.public_run_reference === row.public_run_reference)) {
    return [...rows];
  }
  return [...rows, row];
}

/**
 * Resolves a run's workflow name from the described catalog listing, keyed by
 * `workflow_revision_hash`.
 *
 * A hash the catalog never described, a described revision with no name (a V1
 * revision names nothing, per the served document), and a catalog this round
 * could not read at all (`null`) all fall back to the run id honestly --
 * never a placeholder that reads like a real name. The catalog read is
 * enrichment over the confirmed run list, not a gate on it: a run still shows
 * with its own real fields even when its name could not be resolved.
 */
export function resolveWorkflowName(
  run: RunV3,
  workflowNames: ReadonlyMap<string, string | null> | null
): string {
  return workflowNames?.get(run.workflow_revision_hash) ?? run.run_id;
}
