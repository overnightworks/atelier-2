import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  JournalUnreadableError,
  MutationJournal,
  cancelMutation,
  startMutation,
  type MutationEnvelope,
  type MutationEvidence
} from "../../src/lib/mutationJournal";
import { utf8Base64 } from "../support/exactBytes";

const revisionHash = "5e828c8d522a41e966cd17b8172ede0d954f44be653f832cd4f9dc9e8271fb9b";
const requestHash = "1f58b9145b24d108d7ac38887338b3ea3229833b9c1e418250343f907bfd1047";
const answerHash = "4523540f1504cd17100c4835e85b7eefd49911580f8efff0599a8f283be6b9e3";
const publicReference = "run1.cnVuLTE";

describe("MutationJournal exact transport truth", () => {
  beforeEach(() => sessionStorage.clear());

  it.each([publish(), start(), wait(), cancel()])(
    "retains exact Unicode/raw body bytes for $kind across uncertain reload",
    async (envelope) => {
      const journal = new MutationJournal(sessionStorage);
      const prepared = await journal.prepare(envelope);
      await journal.markUncertain(prepared.mutation_id);

      expect(await new MutationJournal(sessionStorage).get(prepared.mutation_id)).toEqual({
        ...prepared,
        delivery: "uncertain"
      });
    }
  );

  it("refuses kind-route-media-body and identity inconsistencies", async () => {
    const invalid = [
      { ...publish(), target: "/atelier/api/v1/runs" },
      { ...publish(), content_type: "application/json" },
      { ...start(), mutation_id: "start:other" },
      { ...start(), body_base64: startMutation("other", revisionHash, [], []).body_base64 },
      { ...wait(), target: "/atelier/api/v1/runs/run1.b3RoZXI/answers" },
      { ...wait(), body_base64: utf8Base64(waitBody("other")) },
      { ...cancel(), mutation_id: `cancel:${publicReference}:other` },
      { ...cancel(), idempotency_key: "another-key" },
      { ...cancel(), target: `/atelier/api/v1/runs/${publicReference}/answers` }
    ];
    for (const envelope of invalid) {
      const rejection = new MutationJournal(sessionStorage).prepare(envelope as MutationEnvelope);
      await expect(rejection).rejects.toThrow();
      // The caller's own envelope is invalid, not the stored journal --
      // JournalUnreadableError is entries()'s own type, never prepare()'s.
      await expect(rejection).rejects.not.toBeInstanceOf(JournalUnreadableError);
    }
  });

  it("rejects corrupt stored JSON, schema, and duplicate ids, each as a JournalUnreadableError", async () => {
    sessionStorage.setItem("atelier2.mutation-journal.v1", "{");
    const notJson = new MutationJournal(sessionStorage).entries();
    await expect(notJson).rejects.toThrow(/valid JSON/);
    await expect(notJson).rejects.toBeInstanceOf(JournalUnreadableError);

    sessionStorage.setItem("atelier2.mutation-journal.v1", JSON.stringify([{ ...start(), delivery: "prepared", extra: true }]));
    const unknownField = new MutationJournal(sessionStorage).entries();
    await expect(unknownField).rejects.toThrow(/unknown fields/);
    await expect(unknownField).rejects.toBeInstanceOf(JournalUnreadableError);

    sessionStorage.setItem(
      "atelier2.mutation-journal.v1",
      JSON.stringify([
        { ...start(), delivery: "prepared" },
        { ...start(), delivery: "uncertain" }
      ])
    );
    const duplicate = new MutationJournal(sessionStorage).entries();
    await expect(duplicate).rejects.toThrow(/duplicate/);
    await expect(duplicate).rejects.toBeInstanceOf(JournalUnreadableError);
  });

  it("treats stored field names as a set independent of JSON key order", async () => {
    const entry = { ...start(), delivery: "prepared" };
    sessionStorage.setItem(
      "atelier2.mutation-journal.v1",
      JSON.stringify([Object.fromEntries(Object.entries(entry).reverse())])
    );
    await expect(new MutationJournal(sessionStorage).entries()).resolves.toEqual([entry]);

    sessionStorage.setItem(
      "atelier2.mutation-journal.v1",
      JSON.stringify([{ ...entry, content_type: undefined }])
    );
    await expect(new MutationJournal(sessionStorage).entries()).rejects.toThrow(
      /unknown fields or missing fields/
    );
  });

  it("rejects stored byte/hash identity corruption", async () => {
    for (const corrupt of [
      { ...publish(), revision_hash: "d".repeat(64), mutation_id: `publish:${"d".repeat(64)}` },
      { ...wait(), answer_hash: "d".repeat(64) }
    ]) {
      sessionStorage.setItem(
        "atelier2.mutation-journal.v1",
        JSON.stringify([{ ...corrupt, delivery: "prepared" }])
      );
      const rejection = new MutationJournal(sessionStorage).entries();
      await expect(rejection).rejects.toThrow(/bytes|document/);
      await expect(rejection).rejects.toBeInstanceOf(JournalUnreadableError);
    }
  });

  it("refuses to reuse one mutation identity for different exact bytes", async () => {
    const journal = new MutationJournal(sessionStorage);
    await journal.prepare(publish());
    await expect(
      journal.prepare({ ...publish(), body_base64: utf8Base64("different") })
    ).rejects.toThrow();
  });

  it("retains a wait through 202 and unrelated durable evidence", async () => {
    const journal = new MutationJournal(sessionStorage);
    await journal.prepare(wait());

    expect(await journal.resolve(wait().mutation_id, httpEvidence(wait(), 202))).toBe(false);
    expect(
      await journal.resolve(wait().mutation_id, {
        type: "wait_answered",
        public_run_reference: publicReference,
        workflow_revision_hash: revisionHash,
        node_id: "other",
        node_execution_id: revisionHash,
        answer: "17",
        answer_hash: answerHash
      })
    ).toBe(false);
    expect(await journal.get(wait().mutation_id)).not.toBeNull();
  });

  it("clears each kind only with matching kind-specific proof", async () => {
    const scenarios: Array<[MutationEnvelope, MutationEvidence]> = [
      [
        publish(),
        {
          type: "publication_response",
          status: 201,
          target: publish().target,
          request_body_base64: publish().body_base64,
          revision_hash: revisionHash,
          document_base64: publish().body_base64
        }
      ],
      [
        start(),
        {
          type: "start_response",
          status: 201,
          target: start().target,
          request_body_base64: start().body_base64,
          run_id: "run-1",
          public_run_reference: publicReference,
          workflow_revision_hash: revisionHash
        }
      ],
      [
        wait(),
        {
          type: "wait_answered",
          public_run_reference: publicReference,
          workflow_revision_hash: revisionHash,
          node_id: "wait",
          node_execution_id: revisionHash,
          answer: "17",
          answer_hash: answerHash
        }
      ],
    ];
    for (const [envelope, evidence] of scenarios) {
      sessionStorage.clear();
      const journal = new MutationJournal(sessionStorage);
      await journal.prepare(envelope);
      expect(await journal.resolve(envelope.mutation_id, evidence)).toBe(true);
      expect(await journal.get(envelope.mutation_id)).toBeNull();
    }
  });

  it.each([201, 301, 400, 500, 503])(
    "retains an exact wait request after undocumented or failed HTTP %i",
    async (status) => {
      const journal = new MutationJournal(sessionStorage);
      const envelope = wait();
      await journal.prepare(envelope);
      expect(await journal.resolve(envelope.mutation_id, httpEvidence(envelope, status))).toBe(false);
      expect(await journal.get(envelope.mutation_id)).not.toBeNull();
    }
  );

  it.each([wait()])(
    "clears an exact $kind request after the documented HTTP 200",
    async (envelope) => {
      const journal = new MutationJournal(sessionStorage);
      await journal.prepare(envelope);

      expect(await journal.resolve(envelope.mutation_id, httpEvidence(envelope, 200))).toBe(true);
      expect(await journal.get(envelope.mutation_id)).toBeNull();
    }
  );

  it("clears an exact cancel on the terminal 200 but keeps it on the 202-accepted reply", async () => {
    const journal = new MutationJournal(sessionStorage);
    await journal.prepare(cancel());
    expect(await journal.resolve(cancel().mutation_id, httpEvidence(cancel(), 202))).toBe(false);
    expect(await journal.get(cancel().mutation_id)).not.toBeNull();

    expect(await journal.resolve(cancel().mutation_id, httpEvidence(cancel(), 200))).toBe(true);
    expect(await journal.get(cancel().mutation_id)).toBeNull();
  });

  it("remembers a 202-accepted delivery across a reload, apart from an unconfirmed one", async () => {
    const journal = new MutationJournal(sessionStorage);
    await journal.prepare(cancel());
    await journal.markAccepted(cancel().mutation_id);

    const reloaded = await new MutationJournal(sessionStorage).get(cancel().mutation_id);
    expect(reloaded?.delivery).toBe("accepted");

    await journal.markUncertain(cancel().mutation_id);
    expect((await new MutationJournal(sessionStorage).get(cancel().mutation_id))?.delivery).toBe(
      "uncertain"
    );
  });

  it("retains a V3 start that carries the exact order bytes", async () => {
    const envelope = startMutation(
      "run-1",
      revisionHash,
      [{ role: "cook", agent_configuration_revision_hash: "c".repeat(64) }],
      [{ name: "portions", value: '{"portions": 7}' }]
    );
    const journal = new MutationJournal(sessionStorage);
    await journal.prepare(envelope);
    await journal.markUncertain(envelope.mutation_id);

    expect(await new MutationJournal(sessionStorage).get(envelope.mutation_id)).toEqual({
      ...envelope,
      delivery: "uncertain"
    });
    const body = JSON.parse(globalThis.atob(envelope.body_base64)) as {
      orders: Array<{ name: string; value: string }>;
    };
    expect(body.orders).toEqual([{ name: "portions", value: '{"portions": 7}' }]);
  });

  it("retains a V3 start whose order names a published artifact", async () => {
    const artifactHash = "d".repeat(64);
    const envelope = startMutation(
      "run-1",
      revisionHash,
      [],
      [{ name: "diff", artifact_hash: artifactHash }]
    );
    const journal = new MutationJournal(sessionStorage);
    await journal.prepare(envelope);

    expect(await new MutationJournal(sessionStorage).get(envelope.mutation_id)).toEqual({
      ...envelope,
      delivery: "prepared"
    });
  });

  it("refuses a V3 start whose artifact-hash order is malformed", async () => {
    const bound = [{ role: "cook", agent_configuration_revision_hash: "c".repeat(64) }];
    await expect(
      new MutationJournal(sessionStorage).prepare(
        startMutation("run-1", revisionHash, bound, [{ name: "diff", artifact_hash: "not-a-hash" }])
      )
    ).rejects.toThrow(/invalid start mutation order/);
  });

  it("refuses a V3 start whose order is empty or duplicated", async () => {
    const journal = new MutationJournal(sessionStorage);
    const bound = [{ role: "cook", agent_configuration_revision_hash: "c".repeat(64) }];
    await expect(
      journal.prepare(
        startMutation("run-1", revisionHash, bound, [{ name: "portions", value: "" }])
      )
    ).rejects.toThrow(/invalid start mutation order/);
    await expect(
      journal.prepare(
        startMutation("run-1", revisionHash, bound, [
          { name: "portions", value: "1" },
          { name: "portions", value: "2" }
        ])
      )
    ).rejects.toThrow(/invalid start mutation order/);
  });

  it("fails loud when storage set or remove fails", async () => {
    const setFailure = new Error("set failed");
    const setStorage = storage({ setItem: vi.fn(() => { throw setFailure; }) });
    await expect(new MutationJournal(setStorage).prepare(start())).rejects.toThrow(setFailure);

    const removeFailure = new Error("remove failed");
    const removeStorage = storage({
      getItem: vi.fn(() => JSON.stringify([{ ...start(), delivery: "prepared" }])),
      removeItem: vi.fn(() => { throw removeFailure; })
    });
    await expect(
      new MutationJournal(removeStorage).discard(start().mutation_id)
    ).rejects.toThrow(removeFailure);
  });

  describe("discardPoisoned: the one way out of a journal entries() refuses to read (#914)", () => {
    it("removes a poisoned journal that entries() itself rejects, never reading it first", async () => {
      sessionStorage.setItem("atelier2.mutation-journal.v1", "{");
      const journal = new MutationJournal(sessionStorage);
      await expect(journal.entries()).rejects.toThrow(/valid JSON/);

      journal.discardPoisoned();
      expect(sessionStorage.getItem("atelier2.mutation-journal.v1")).toBeNull();
      await expect(journal.entries()).resolves.toEqual([]);
    });

    it("removes every remembered mutation, valid ones included -- there is no partial rescue", async () => {
      const journal = new MutationJournal(sessionStorage);
      await journal.prepare(start());
      await journal.prepare(wait());

      journal.discardPoisoned();
      expect(await journal.entries()).toEqual([]);
    });

    it("is a harmless no-op when there was nothing to forget", () => {
      const journal = new MutationJournal(sessionStorage);
      expect(() => journal.discardPoisoned()).not.toThrow();
      expect(sessionStorage.getItem("atelier2.mutation-journal.v1")).toBeNull();
    });
  });

  describe("rawStored: the exact bytes behind an entry, read without ever parsing it (#914)", () => {
    it("returns the raw stored text untouched, poisoned or not", () => {
      sessionStorage.setItem("atelier2.mutation-journal.v1", "{");
      expect(new MutationJournal(sessionStorage).rawStored()).toBe("{");
    });

    it("returns null when nothing is stored", () => {
      expect(new MutationJournal(sessionStorage).rawStored()).toBeNull();
    });
  });
});

function publish(): MutationEnvelope {
  return {
    mutation_id: `publish:${revisionHash}`,
    kind: "publish",
    target: "/atelier/api/v1/workflow-revisions",
    content_type: "application/yaml",
    body_base64: utf8Base64("job: Grüße 東京\n"),
    revision_hash: revisionHash
  };
}

function start(): MutationEnvelope {
  return startMutation("run-1", revisionHash, [], []);
}

function wait(): MutationEnvelope {
  return {
    mutation_id: `wait:${publicReference}:${revisionHash}`,
    kind: "wait",
    target: `/atelier/api/v1/runs/${publicReference}/answers`,
    content_type: "application/json",
    body_base64: utf8Base64(waitBody("wait")),
    public_run_reference: publicReference,
    workflow_revision_hash: revisionHash,
    node_id: "wait",
    expected_node_execution_id: revisionHash,
    actor: "operator",
    answer_base64: "MTc=",
    answer_hash: answerHash
  };
}

function waitBody(node_id: string): string {
  return JSON.stringify({
    workflow_revision_hash: revisionHash,
    node_id,
    expected_node_execution_id: revisionHash,
    actor: "operator",
    answer_base64: "MTc="
  });
}

function cancel(): Extract<MutationEnvelope, { kind: "cancel" }> {
  return cancelMutation(publicReference, requestHash, "cancel-key-1");
}

function httpEvidence(envelope: MutationEnvelope, status: number): MutationEvidence {
  const type = envelope.kind === "wait" ? "wait_response" : "cancel_response";
  return {
    type,
    status,
    target: envelope.target,
    request_body_base64: envelope.body_base64
  };
}

function storage(overrides: Partial<Storage>): Storage {
  return {
    getItem: vi.fn(() => null),
    setItem: vi.fn(),
    removeItem: vi.fn(),
    clear: vi.fn(),
    key: vi.fn(() => null),
    length: 0,
    ...overrides
  };
}
