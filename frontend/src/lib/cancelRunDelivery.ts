import {
  CockpitRequestError,
  type CockpitApi,
  type Problem,
  type RunV3
} from "../api/client";
import { humanErrorMessage } from "./humanRefusal";
import {
  cancelMutation,
  cancelMutationId,
  createCancelIdempotencyKey,
  JournalUnreadableError,
  MutationJournal,
  type CancelMutation,
  type MutationDelivery
} from "./mutationJournal";
import { runPageCopy } from "./runPageCopy";

/**
 * A cancel the durable journal still holds for a run, carrying how far it got
 * on the wire so a reload can tell an accepted (202) cancel that is genuinely
 * stopping the run from one that was never confirmed.
 */
export type PendingCancel = CancelMutation & { delivery: MutationDelivery };

/**
 * The one audited path a V3 run-cancel travels, the same discipline the wait
 * answer and reconciliation already follow (#439 P5).
 *
 * A cancel is a consequential mutation with real failure and conflict forms, so
 * it carries the same durable pending/uncertain/retry journal: a lost response
 * replays the exact command the server minted its durable id from, never a
 * second cancel. The server's own outcome decides the honest ending -- a run
 * that finished before the cancel reached it is reported as overtaken, never as
 * cancelled.
 */

export type CancelOutcome =
  /** 200: the run reached its cancelled terminal under this exact command. */
  | { kind: "cancelled"; run: RunV3 }
  /** 202: the command is durably accepted and the run is stopping. */
  | { kind: "cancelling"; pending: CancelMutation; run: RunV3 }
  /** A refusal the server marks definitive (overtaken, not-cancellable, conflict): no retry helps. */
  | { kind: "refused"; problem: Problem | null; message: string }
  /** A network failure or unconfirmed reply: the exact command stays for Retry or Discard. */
  | { kind: "uncertain"; pending: CancelMutation; message: string };

/**
 * Journals one run's exact cancel before it reaches the wire, reusing the
 * command already saved for this run's current step so a second confirmation
 * replays the same idempotency key rather than minting a second cancel.
 */
export async function prepareCancel(
  mutationJournal: MutationJournal,
  publicRunReference: string,
  expectedNodeExecutionId: string
): Promise<CancelMutation> {
  const existing = await mutationJournal.get(
    cancelMutationId(publicRunReference, expectedNodeExecutionId)
  );
  if (existing !== null) {
    if (existing.kind !== "cancel") {
      throw new Error("The saved request belongs to another operation.");
    }
    return existing;
  }
  const mutation = cancelMutation(
    publicRunReference,
    expectedNodeExecutionId,
    createCancelIdempotencyKey()
  );
  const prepared = await mutationJournal.prepare(mutation);
  if (prepared.kind !== "cancel") {
    throw new Error("The saved request belongs to another operation.");
  }
  return prepared;
}

/**
 * Sends one journaled cancel and settles it: a 200 resolves the entry as the
 * definitive cancelled terminal; a 202 keeps it, uncertain, while the run
 * stops; a definitive refusal discards it and names the honest reason; anything
 * else keeps the exact command for Retry or Discard.
 */
export async function deliverCancel(
  cockpitApi: CockpitApi,
  mutationJournal: MutationJournal,
  mutation: CancelMutation,
  fallbackMessage: string = runPageCopy.cancel.unconfirmed
): Promise<CancelOutcome> {
  try {
    const result = await cockpitApi.cancelRun(mutation);
    const resolved = await mutationJournal.resolve(mutation.mutation_id, {
      type: "cancel_response",
      status: result.status,
      target: mutation.target,
      request_body_base64: mutation.body_base64
    });
    if (result.status === 200 && !resolved) {
      throw new Error("The workshop reported a cancel this page did not send.");
    }
    if (result.status === 202 && resolved) {
      throw new Error("Your cancel was reported as final while it is still being carried out.");
    }
    if (result.value.public_run_reference !== mutation.public_run_reference) {
      throw new CockpitRequestError(
        "The workshop answered with a different run than the one this cancel was for."
      );
    }
    if (result.status === 202) {
      const accepted = await mutationJournal.markAccepted(mutation.mutation_id);
      if (accepted.kind !== "cancel") {
        throw new Error("The accepted request belongs to another operation.");
      }
      return { kind: "cancelling", pending: accepted, run: result.value };
    }
    return { kind: "cancelled", run: result.value };
  } catch (error) {
    // The journal itself, not this delivery, refused: settling the failure
    // would read it again and refuse identically, so this propagates the one
    // true reason instead of `settleCancelFailure` masking it with a second,
    // unrelated-looking throw from its own read.
    if (error instanceof JournalUnreadableError) throw error;
    return settleCancelFailure(mutationJournal, mutation, error, fallbackMessage);
  }
}

/**
 * A refusal the server itself marks definitive -- the run finished first, the
 * run cannot be cancelled now, or the key conflicts with another command --
 * discards the journal entry: no retry can change that answer. Every other
 * failure keeps the exact command, marked uncertain, so Retry and Discard stay
 * meaningful.
 */
async function settleCancelFailure(
  mutationJournal: MutationJournal,
  mutation: CancelMutation,
  error: unknown,
  fallbackMessage: string
): Promise<CancelOutcome> {
  const message = humanErrorMessage(error, fallbackMessage);
  if (error instanceof CockpitRequestError && error.definitive_failure) {
    await mutationJournal.discard(mutation.mutation_id);
    return { kind: "refused", problem: error.problem, message };
  }
  if ((await mutationJournal.get(mutation.mutation_id)) === null) {
    return { kind: "uncertain", pending: mutation, message };
  }
  const uncertain = await mutationJournal.markUncertain(mutation.mutation_id);
  return {
    kind: "uncertain",
    pending: uncertain.kind === "cancel" ? uncertain : mutation,
    message
  };
}

/**
 * Reads whichever cancel the durable journal still holds for this run, so a
 * reload while a cancel is in flight shows it as pending rather than offering a
 * fresh one. Keyed by the run, not the step, so a reload finds it even after the
 * server's predicate stopped naming a cancellable target. Its `delivery` rides
 * along so the card can tell an accepted (202) cancel from an unconfirmed one.
 */
export async function loadPendingCancelForRun(
  mutationJournal: MutationJournal,
  publicRunReference: string
): Promise<PendingCancel | null> {
  const entries = await mutationJournal.entries();
  const entry = entries.find(
    (candidate) => candidate.kind === "cancel" && candidate.public_run_reference === publicRunReference
  );
  return entry === undefined ? null : (entry as PendingCancel);
}
