import { CockpitRequestError, type CockpitApi, type RunV3 } from "../api/client";
import { humanErrorMessage } from "./humanRefusal";
import {
  JournalUnreadableError,
  MutationJournal,
  waitMutation,
  waitMutationId,
  type WaitMutation
} from "./mutationJournal";
import { runPageCopy } from "./runPageCopy";
import { encodeWaitAnswer } from "./waitAnswer";

/**
 * The one audited path a V3 wait answer travels, whichever surface sends it
 * (the run page's composer, or the Board's inline decision buttons, #572).
 *
 * Two surfaces answering the same waiting node carry the same durable
 * pending/uncertain/retry journal and the same conflict handling -- that
 * protocol must evolve together, so it has exactly one owner here rather
 * than a second implementation beside it.
 */

export type WaitAnswerOutcome =
  | { kind: "confirmed"; run: RunV3 }
  | { kind: "uncertain"; pending: WaitMutation; run: RunV3 }
  | { kind: "failed"; pending: WaitMutation | null; message: string };

/**
 * Builds one V3 wait's exact mutation and journals it as prepared, before it
 * ever reaches the wire.
 *
 * `isStringSchema` is the caller's own knowledge of the waiting node's schema
 * (`WaitAnswerSchemaResourceV3.kind === "string"`) -- the one fact
 * `encodeWaitAnswer` needs to send raw text verbatim instead of a
 * JSON-encoded string (#1091).
 */
export async function prepareWaitAnswer(
  mutationJournal: MutationJournal,
  publicRunReference: string,
  workflowRevisionHash: string,
  nodeId: string,
  expectedNodeExecutionId: string,
  typedOrExactAnswer: string,
  isStringSchema: boolean
): Promise<WaitMutation> {
  const mutation = await waitMutation(
    publicRunReference,
    workflowRevisionHash,
    nodeId,
    expectedNodeExecutionId,
    encodeWaitAnswer(typedOrExactAnswer, isStringSchema)
  );
  const prepared = await mutationJournal.prepare(mutation);
  if (prepared.kind !== "wait") {
    throw new Error("The saved request belongs to another operation.");
  }
  return prepared;
}

/**
 * Sends one journaled wait answer and settles it: a definitive 200 resolves
 * the journal entry and confirms the run the wire actually returned; a 202
 * marks the entry uncertain rather than pretending it is settled; anything
 * else -- a network failure, a refused answer, a response that does not
 * match what was sent -- comes back as `failed`, with the journal already
 * discarded when the server's own refusal says retrying cannot help.
 */
export async function deliverWaitAnswer(
  cockpitApi: CockpitApi,
  mutationJournal: MutationJournal,
  mutation: WaitMutation,
  fallbackMessage: string = runPageCopy.answerUnconfirmed
): Promise<WaitAnswerOutcome> {
  try {
    const result = await cockpitApi.answer(mutation);
    const resolved = await mutationJournal.resolve(mutation.mutation_id, {
      type: "wait_response",
      status: result.status,
      target: mutation.target,
      request_body_base64: mutation.body_base64
    });
    if (result.status === 200 && !resolved) {
      throw new Error("The workshop confirmed a different answer than the one that was sent.");
    }
    if (result.status === 202 && resolved) {
      throw new Error("Your answer was reported as stored while it is still pending.");
    }
    if (result.value.public_run_reference !== mutation.public_run_reference) {
      throw new CockpitRequestError(
        "The workshop answered with a different run than the one this answer was for."
      );
    }
    if (result.status === 202) {
      const uncertain = await mutationJournal.markUncertain(mutation.mutation_id);
      if (uncertain.kind !== "wait") {
        throw new Error("The accepted request belongs to another operation.");
      }
      return { kind: "uncertain", pending: uncertain, run: result.value };
    }
    return { kind: "confirmed", run: result.value };
  } catch (error) {
    // The journal itself, not this delivery, refused: recording the failure
    // would read it again and refuse identically, so this propagates the one
    // true reason instead of `recordWaitAnswerFailure` masking it with a
    // second, unrelated-looking throw from its own read.
    if (error instanceof JournalUnreadableError) throw error;
    return {
      kind: "failed",
      pending: await recordWaitAnswerFailure(mutationJournal, mutation, error),
      message: humanErrorMessage(error, fallbackMessage)
    };
  }
}

/**
 * A refusal the server itself marks definitive (`definitive_failure`, e.g. the
 * durable run moved past this node already) discards the journal entry: no
 * retry can turn that answer into a different one. Every other failure keeps
 * the entry, marked uncertain, so Retry and Discard stay meaningful.
 */
async function recordWaitAnswerFailure(
  mutationJournal: MutationJournal,
  pending: WaitMutation,
  error: unknown
): Promise<WaitMutation | null> {
  if (error instanceof CockpitRequestError && error.definitive_failure) {
    await mutationJournal.discard(pending.mutation_id);
    return null;
  }
  if ((await mutationJournal.get(pending.mutation_id)) === null) {
    return pending;
  }
  const uncertain = await mutationJournal.markUncertain(pending.mutation_id);
  return uncertain.kind === "wait" ? uncertain : pending;
}

export type PendingWaitLookup =
  | { kind: "none" }
  | { kind: "found"; pending: WaitMutation }
  | { kind: "corrupt"; message: string };

/**
 * Reads whichever earlier wait answer the durable journal still holds for
 * this exact node execution, before this surface has sent one of its own -- the same
 * identity (`public_run_reference` + `expected_node_execution_id`) the run page and the Board
 * both key their journal entry on, so an answer begun on one surface is seen
 * as pending on the other rather than offered twice.
 */
export async function loadPendingWaitAnswer(
  mutationJournal: MutationJournal,
  publicRunReference: string,
  workflowRevisionHash: string,
  nodeId: string,
  expectedNodeExecutionId: string
): Promise<PendingWaitLookup> {
  const entry = await mutationJournal.get(
    waitMutationId(publicRunReference, expectedNodeExecutionId)
  );
  if (entry === null) {
    return { kind: "none" };
  }
  if (entry.kind !== "wait") {
    return { kind: "corrupt", message: "The saved request identity belongs to another operation." };
  }
  if (
    entry.workflow_revision_hash !== workflowRevisionHash ||
    entry.node_id !== nodeId ||
    entry.expected_node_execution_id !== expectedNodeExecutionId ||
    entry.public_run_reference !== publicRunReference
  ) {
    return { kind: "corrupt", message: "The saved exact answer does not belong to this waiting node." };
  }
  return { kind: "found", pending: entry };
}
