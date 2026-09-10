import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it, vi } from "vitest";

import {
  assistantTurnEventSchema,
  createCockpitApi,
  decodeCanonicalBase64,
  decodePublicRunReference,
  decodeProblem,
  decodeStreamFrame,
  encodePublicRunReference,
  MAXIMUM_TRANSCRIPT_STEP_CHARACTERS,
  nodeDetailSchema,
  nodeRailEntrySchema,
  parseEventCursor,
  projectSourceConnectionRevisionSchema,
  projectSourceListSchema,
  projectSourceResourceSchema,
  problemDefinitions,
  workflowRevisionDetailSchema,
  type Problem,
  type RunProjectionCorrupt
} from "../../src/api/client";
import { cancelMutation } from "../../src/lib/mutationJournal";
import { healthResource } from "../support/cockpitApi";
import { cancellableBlock, notCancellableBlock } from "../support/runV3";

const PROBLEM_TYPE_PREFIX = "urn:atelier2:problem:v1:";

/**
 * The frozen OpenAPI document is the one object both sides can read. The
 * schema-* problems are generated from SchemaDocumentRefusal, so a new enum
 * member publishes a type without anyone editing this file. Collecting every
 * type.const with the problem prefix, and the title.const and status.const
 * that sit beside it, is what makes that drift fail here.
 */
const servedDocument = JSON.parse(
  readFileSync(resolve(process.cwd(), "..", "tests", "api", "openapi_frozen.json"), "utf8")
) as {
  components: {
    schemas: Record<
      string,
      {
        properties?: {
          type?: { const?: string };
          title?: { const?: string };
          status?: { const?: number };
          state?: { enum?: string[] };
        };
      }
    >;
  };
  paths: Record<
    string,
    {
      get?: {
        parameters?: ReadonlyArray<{
          name: string;
          schema?: { pattern?: string; anyOf?: ReadonlyArray<{ pattern?: string }> };
        }>;
      };
    }
  >;
};

/**
 * The route names its own pagination cursor by the regex it accepts, not by
 * convention -- so reading that name from the served document, rather than
 * assuming it starts with "after", is what catches a client that asks under
 * any name the route does not read.
 */
const REVISION_HASH_PATTERN = "^[0-9a-f]{64}$";

function pageCursorParameterName(document: typeof servedDocument, path: string): string {
  const parameters = document.paths[path]?.get?.parameters ?? [];
  const cursor = parameters.find((parameter) => {
    const schema = parameter.schema;
    const variants = schema?.anyOf ?? (schema ? [schema] : []);
    return variants.some((variant) => variant.pattern === REVISION_HASH_PATTERN);
  });
  if (cursor === undefined) {
    throw new Error(`the served document names no hash-cursor parameter for ${path}`);
  }
  return cursor.name;
}

function publishedProblemDefinitions(document: typeof servedDocument) {
  return Object.fromEntries(
    Object.values(document.components.schemas).flatMap((schema) => {
      const type = schema.properties?.type?.const;
      return typeof type === "string" && type.startsWith(PROBLEM_TYPE_PREFIX)
        ? [
            [
              type.slice(PROBLEM_TYPE_PREFIX.length),
              { status: schema.properties?.status?.const, title: schema.properties?.title?.const }
            ]
          ]
        : [];
    })
  );
}

type Equal<Left, Right> =
  (<Value>() => Value extends Left ? 1 : 2) extends <Value>() => Value extends Right ? 1 : 2
    ? true
    : false;
type Assert<Value extends true> = Value;
export type ProblemTypeIsClosed = Assert<
  Equal<Problem["type"], `urn:atelier2:problem:v1:${keyof typeof problemDefinitions}`>
>;
export type ProblemVariantIsExact = Assert<
  Equal<
    Extract<Problem, { type: "urn:atelier2:problem:v1:run-not-found" }>,
    {
      type: "urn:atelier2:problem:v1:run-not-found";
      title: "Run not found";
      status: 404;
      detail: string;
    }
  >
>;
export type RunProjectionCorruptProblemIsDurableStateCorrupt = Assert<
  Equal<
    RunProjectionCorrupt["problem"]["type"],
    "urn:atelier2:problem:v1:durable-state-corrupt"
  >
>;

const digest = "a".repeat(64);
const publicReference = "run1.cnVuLTE";

/** A published V3 revision whose two-node body is declared as a bounded, verdict-exited loop. */
function v3RevisionWithLoop() {
  return {
    workflow_revision_hash: digest,
    document_base64: "YQ==",
    provenance: null,
    graph: {
      workflow_format_version: 3 as const,
      executable: true as const,
      not_executable_reason: null,
      node_count: 2,
      agent_roles: ["builder"],
      orders: [],
      wait_answer_schemas: [],
      node_previews: [
        {
          id: "implement",
          kind: "agent" as const,
          role: "builder",
          instruction_start: "Do the one thing this chain is for.",
          depends_on: []
        },
        {
          id: "review",
          kind: "agent" as const,
          role: "builder",
          instruction_start: "Check what the node before you did.",
          depends_on: ["implement"]
        }
      ],
      loops: [
        {
          id: "until_reviewed",
          member_node_ids: ["implement", "review"],
          maximum_rounds: 3,
          repeat_while: { node: "review", verdict: "revise" as const }
        }
      ],
      name: "Build and review until the review says it is done",
      description: null
    }
  };
}

/** The same two-node V3 revision, with no loop declared over its body. */
function v3RevisionWithoutLoop() {
  const withLoop = v3RevisionWithLoop();
  return {
    ...withLoop,
    graph: {
      ...withLoop.graph,
      loops: [],
      name: "Implement, then review, with no declared loop"
    }
  };
}

function event(event: string, fields: Record<string, unknown> = {}) {
  return {
    cursor: `event1.cnVuLTE.${fields.sequence ?? 1}`,
    sequence: fields.sequence ?? 1,
    public_run_reference: publicReference,
    workflow_revision_hash: digest,
    node_id: "node",
    node_execution_id: digest,
    event_hash: digest,
    event,
    ...fields
  };
}

function v3Event(eventName: string, fields: Record<string, unknown> = {}) {
  return {
    ...event(eventName, fields),
    workflow_format_version: 3,
    node_rail: [{ node_id: "agent", state: "working", attempt: null }]
  };
}

const v2Attempt = { attempt_id: digest, attempt_ordinal: 1 };

describe("the health probe #700's recovery reuses", () => {
  it("asks the health door and refuses fields the server did not declare", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify(healthResource()), {
        status: 200,
        headers: { "content-type": "application/json" }
      })
    );

    const health = await createCockpitApi(fetcher).health();

    expect(fetcher.mock.calls[0]?.[0]).toBe("/atelier/api/v1/health");
    expect(health).toEqual(healthResource());

    fetcher.mockResolvedValueOnce(
      new Response(JSON.stringify({ ...healthResource(), unexpected_field: true }), {
        status: 200,
        headers: { "content-type": "application/json" }
      })
    );
    await expect(createCockpitApi(fetcher).health()).rejects.toThrow("durable wire contract");
  });

  it("parses the redeploy block a stalled auto-redeploy watcher names", async () => {
    const blocked = healthResource({
      redeploy: { blocked_since: null, reason: "watcher status file unreadable" }
    });
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify(blocked), {
        status: 200,
        headers: { "content-type": "application/json" }
      })
    );

    await expect(createCockpitApi(fetcher).health()).resolves.toEqual(blocked);
  });
});

describe("closed API decoders", () => {
  it("decodes a published revision and refuses an unknown field on it", () => {
    const decoded = workflowRevisionDetailSchema.parse(v3RevisionWithLoop());

    expect(decoded.graph.workflow_format_version).toBe(3);
    expect(() => workflowRevisionDetailSchema.parse({ ...decoded, invented: true })).toThrow();
  });

  it("decodes a declared loop's body, bound, and verdict exit", () => {
    const decoded = workflowRevisionDetailSchema.parse(v3RevisionWithLoop());

    if (decoded.graph.workflow_format_version !== 3) throw new Error("the V3 fixture changed");
    expect(decoded.graph.loops).toEqual([
      {
        id: "until_reviewed",
        member_node_ids: ["implement", "review"],
        maximum_rounds: 3,
        repeat_while: { node: "review", verdict: "revise" }
      }
    ]);
  });

  it("decodes a graph that declares no loop as an empty loop list", () => {
    const decoded = workflowRevisionDetailSchema.parse(v3RevisionWithoutLoop());

    if (decoded.graph.workflow_format_version !== 3) throw new Error("the V3 fixture changed");
    expect(decoded.graph.loops).toEqual([]);
  });

  it("refuses a loop verdict outside the closed vocabulary", () => {
    const revision = v3RevisionWithLoop();
    if (revision.graph.workflow_format_version !== 3) throw new Error("the V3 fixture changed");
    const [loop] = revision.graph.loops;
    if (loop === undefined) throw new Error("the loop fixture changed");

    expect(() =>
      workflowRevisionDetailSchema.parse({
        ...revision,
        graph: {
          ...revision.graph,
          loops: [{ ...loop, repeat_while: { node: "review", verdict: "maybe" } }]
        }
      })
    ).toThrow();
  });

  it.each([
    { kind: "enum" as const, values: ["accepted", "revise"], valid: true },
    { kind: "enum" as const, values: null, valid: false },
    { kind: "boolean" as const, values: null, valid: true },
    { kind: "boolean" as const, values: ["true"], valid: false }
  ])(
    "names values only for an enum wait-answer schema: $kind/$values",
    ({ kind, values, valid }) => {
      const revision = v3RevisionWithoutLoop();
      if (revision.graph.workflow_format_version !== 3) throw new Error("the V3 fixture changed");
      const waitAnswerSchema = {
        node_id: "implement",
        schema: { ref: "answer-schema", revision: "schema-answer" },
        kind,
        string_typed: false,
        values
      };
      const parse = () =>
        workflowRevisionDetailSchema.parse({
          ...revision,
          graph: { ...revision.graph, wait_answer_schemas: [waitAnswerSchema] }
        });

      if (valid) {
        expect(parse().graph.wait_answer_schemas).toEqual([waitAnswerSchema]);
      } else {
        expect(parse).toThrow();
      }
    }
  );

  it.each(["not-base64", "YQ", "YQ===", "Y Q==", "YQ-_", "===="])(
    "refuses a noncanonical document base64 value: %s",
    (document_base64) => {
      expect(() =>
        workflowRevisionDetailSchema.parse({
          ...v3RevisionWithoutLoop(),
          document_base64
        })
      ).toThrow();
    }
  );

  it.each([
    v3Event("ACTION_RECONCILIATION_REQUIRED", { request_base64: "eA==", request_hash: digest }),
    v3Event("ACTION_RECONCILIATION_RESOLVED", { receipt: receipt() }),
    v3Event("ACTION_COMPLETED", { receipt: receipt() })
  ])("decodes the V3 Action event family: $event", (value) => {
    expect(decodeStreamFrame(value).event).toBe(value.event);
  });

  it("refuses an unknown durable event kind instead of dropping it", () => {
    expect(() => decodeStreamFrame(v3Event("NODE_PROGRESS", { percent: 50 }))).toThrow();
  });

  it.each(["PROCESS_OUTPUT_LIMIT_EXCEEDED", "PROCESS_SUPERVISION_FAILED"])(
    "decodes the runner failure the served event family names: %s",
    (failureCode) => {
      expect(
        decodeStreamFrame(
          v3Event("AGENT_FAILED", {
            ...v2Attempt,
            failure_code: failureCode,
            reason: null
          })
        ).event
      ).toBe("AGENT_FAILED");
    }
  );

  it("refuses a failure code outside the published vocabulary", () => {
    expect(() =>
      decodeStreamFrame(
        v3Event("AGENT_FAILED", { ...v2Attempt, failure_code: "RUNNER_BROKE", reason: null })
      )
    ).toThrow();
  });

  it("decodes the attempt-less refusal with or without its sentence and refuses a forged attempt", () => {
    const refusal = { reason: "agent-executor-binding-unavailable" as const, detail: null };
    const explained = {
      reason: "work-item-claim-refused" as const,
      detail: "claim branch 'x' does not match checkout branch 'main'"
    };

    expect(decodeStreamFrame(v3Event("AGENT_FAILED", refusal))).toMatchObject(refusal);
    expect(decodeStreamFrame(v3Event("AGENT_FAILED", explained))).toMatchObject(explained);
    expect(() => decodeStreamFrame(v3Event("AGENT_FAILED", { ...refusal, ...v2Attempt }))).toThrow();
  });

  it("decodes the attention feed's per-run corruption frame", () => {
    const frame = decodeStreamFrame({
      event: "RUN_PROJECTION_CORRUPT",
      public_run_reference: "run1.cnVu",
      problem: {
        type: "urn:atelier2:problem:v1:durable-state-corrupt",
        title: "Durable state is corrupt",
        status: 500,
        detail: "Stop mutation and inspect the durable store."
      }
    });
    expect(frame.event).toBe("RUN_PROJECTION_CORRUPT");
  });

  it("refuses a RUN_PROJECTION_CORRUPT frame with a foreign problem type", () => {
    expect(() =>
      decodeStreamFrame({
        event: "RUN_PROJECTION_CORRUPT",
        public_run_reference: "run1.cnVu",
        problem: {
          type: "urn:atelier2:problem:v1:internal-error",
          title: "Internal error",
          status: 500,
          detail: "Retry only after the server fault has been inspected."
        }
      })
    ).toThrow();
  });

  it.each(["YQ", "YQ===", "Y Q==", "YQ-_", "===="])(
    "refuses noncanonical standard base64 in nested request/results: %s",
    (encoded) => {
      expect(() =>
        decodeStreamFrame(
          v3Event("ACTION_RECONCILIATION_REQUIRED", {
            request_base64: encoded,
            request_hash: digest
          })
        )
      ).toThrow();
      expect(() =>
        decodeStreamFrame(
          v3Event("ACTION_COMPLETED", {
            receipt: { ...receipt(), result_base64: encoded }
          })
        )
      ).toThrow();
    }
  );

  it("refuses a cursor whose run or sequence disagrees with the event", () => {
    expect(() =>
      decodeStreamFrame({ ...v3Event("WAITING_INPUT"), cursor: "event1.b3RoZXI.1" })
    ).toThrow();
    expect(() =>
      decodeStreamFrame({ ...v3Event("WAITING_INPUT"), cursor: "event1.cnVuLTE.2" })
    ).toThrow();
  });

  it.each([
    "event1.cnVuLTE.0",
    "event1.cnVuLTE.01",
    "event1.cnVuLTE.-1",
    "event1.cnVuLTE.+1",
    "event2.cnVuLTE.1",
    "event1.cnVuLTE==.1",
    "event1..1"
  ])("refuses a malformed or noncanonical event cursor: %s", (cursor) => {
    expect(() =>
      decodeStreamFrame({ ...v3Event("WAITING_INPUT"), cursor })
    ).toThrow();
  });

  it("decodes only the documented RFC 9457 problem union", () => {
    const problem = decodeProblem({
      type: "urn:atelier2:problem:v1:run-not-found",
      title: "Run not found",
      status: 404,
      detail: "Use a durable run."
    });
    expect(problem.type).toBe("urn:atelier2:problem:v1:run-not-found");
    expect(() => decodeProblem({ ...problem, type: "urn:atelier2:problem:v1:new-problem" })).toThrow();
  });

  it("decodes the library-document-ambiguous problem the recognition door answers", () => {
    const problem = decodeProblem({
      type: "urn:atelier2:problem:v1:library-document-ambiguous",
      title: "Document matches more than one library kind",
      status: 422,
      detail: "The document matches agent_definition and skill."
    });
    expect(problem.type).toBe("urn:atelier2:problem:v1:library-document-ambiguous");
    expect(problem.status).toBe(422);
  });

  it("decodes an agent-definition-revision-not-found problem the read door answers", () => {
    const problem = decodeProblem({
      type: "urn:atelier2:problem:v1:agent-definition-revision-not-found",
      title: "Agent definition revision not found",
      status: 404,
      detail: "Publish the exact agent definition revision before reading its fields."
    });
    expect(problem).toEqual({
      type: "urn:atelier2:problem:v1:agent-definition-revision-not-found",
      title: "Agent definition revision not found",
      status: 404,
      detail: "Publish the exact agent definition revision before reading its fields."
    });
  });

  it("decodes a published run-input-refused problem instead of calling it undocumented", () => {
    const problem = decodeProblem({
      type: "urn:atelier2:problem:v1:run-input-refused",
      title: "Run input refused",
      status: 422,
      detail: "Supply exactly the orders this workflow declares, each satisfying the schema its author pinned."
    });
    expect(problem).toEqual({
      type: "urn:atelier2:problem:v1:run-input-refused",
      title: "Run input refused",
      status: 422,
      detail: "Supply exactly the orders this workflow declares, each satisfying the schema its author pinned."
    });
  });

  it.each(Object.entries(problemDefinitions))(
    "binds problem %s to its exact title and status",
    (code, definition) => {
      const exact = {
        type: `urn:atelier2:problem:v1:${code}`,
        title: definition.title,
        status: definition.status,
        detail: "operation-specific detail",
        ...(code === "uncast-agent-roles"
          ? { uncast_roles: [{ role: "reviewer", reason: "no-project-default" }] }
          : {})
      };
      expect(decodeProblem(exact).detail).toBe("operation-specific detail");
      expect(() => decodeProblem({ ...exact, title: "Wrong" })).toThrow();
      expect(() => decodeProblem({ ...exact, status: definition.status + 1 })).toThrow();
    }
  );

  it("decodes exactly the problem definitions the document publishes", () => {
    expect(problemDefinitions).toEqual(publishedProblemDefinitions(servedDocument));
  });
});

const configurationInput = {
  model: "claude-opus-5",
  auth_profile_revision_hash: digest,
  executor_revision: "claude-subscription/v1"
};

function configurationRevision(echo: Record<string, unknown>) {
  return {
    ...configurationInput,
    provider_id: "anthropic",
    auth_mode: "subscription",
    agent_configuration_revision_hash: digest,
    ...echo
  };
}

function publishing(revision: unknown) {
  return vi.fn<typeof fetch>().mockResolvedValue(
    new Response(JSON.stringify(revision), {
      status: 201,
      headers: { "content-type": "application/json" }
    })
  );
}

function sentBody(fetcher: ReturnType<typeof publishing>): unknown {
  return JSON.parse(String(fetcher.mock.calls[0]?.[1]?.body));
}

describe("agent configuration publication", () => {
  it("sends the requested capability and carries the echoed value back", async () => {
    const fetcher = publishing(configurationRevision({ requested_capability: "interactive" }));

    const published = await createCockpitApi(fetcher).publishAgentConfiguration({
      ...configurationInput,
      requested_capability: "interactive"
    });

    expect(sentBody(fetcher)).toEqual({
      ...configurationInput,
      requested_capability: "interactive"
    });
    expect(published.value.requested_capability).toBe("interactive");
  });

  it("leaves the capability out when none was requested and accepts the headless echo", async () => {
    const fetcher = publishing(configurationRevision({ requested_capability: "headless" }));

    const published = await createCockpitApi(fetcher).publishAgentConfiguration(configurationInput);

    expect(sentBody(fetcher)).toEqual(configurationInput);
    expect(published.value.requested_capability).toBe("headless");
  });

  it.each([
    ["omits the capability echo", configurationRevision({})],
    ["echoes a capability outside the contract", configurationRevision({ requested_capability: "supervised" })]
  ])("refuses a publication response that %s", async (_case, revision) => {
    const fetcher = publishing(revision);

    await expect(
      createCockpitApi(fetcher).publishAgentConfiguration(configurationInput)
    ).rejects.toThrow("did not match the durable wire contract");
  });
});

function receipt() {
  return {
    logical_effect_key: "effect-key",
    request_hash: digest,
    effect_id: "effect",
    result_hash: digest,
    result_base64: "",
    confirmation_source: "OPERATOR_FOUND",
    reconcile_command_id: "command"
  };
}

/**
 * The listing the cockpit asks for is a different shape from the one the route
 * answers by default, so the selector that asks for it is production behaviour
 * and not a detail of the URL. This double answers like the real route: the
 * enriched shape only when the selector is there, the frozen hash-only shape
 * otherwise. Drop `view=described` and the strict decoder meets a row without a
 * name and throws -- which is what an operator would meet.
 */
function servingRevisionsByView() {
  return vi.fn<typeof fetch>().mockImplementation(async (target) => {
    const described = String(target).includes("view=described");
    const item = described
      ? {
          workflow_revision_hash: digest,
          workflow_format_version: 3,
          executable: false,
          not_executable_reason: "agent forms nothing binds yet: outputs",
          name: "Nightly regression sweep",
          description: "Runs the sweep and files what it finds.",
          provenance: null
        }
      : { workflow_revision_hash: digest };
    return new Response(
      JSON.stringify({ items: [item], next_after_revision_hash: null }),
      { status: 200, headers: { "content-type": "application/json" } }
    );
  });
}

describe("the saved-workflow listing the cockpit asks for", () => {
  it("asks the route for the described view and decodes a non-empty page of it", async () => {
    const fetcher = servingRevisionsByView();

    const page = await createCockpitApi(fetcher).listWorkflowRevisions();

    expect(String(fetcher.mock.calls[0]?.[0])).toContain("view=described");
    expect(page.items).toEqual([
      {
        workflow_revision_hash: digest,
        workflow_format_version: 3,
        executable: false,
        not_executable_reason: "agent forms nothing binds yet: outputs",
        name: "Nightly regression sweep",
        description: "Runs the sweep and files what it finds.",
        provenance: null
      }
    ]);
  });

  it("resumes past a full page under the cursor name the route reads", async () => {
    const cursorParameterName = pageCursorParameterName(
      servedDocument,
      "/atelier/api/v1/workflow-revisions"
    );
    const secondPageHash = "b".repeat(64);
    const summary = (workflowRevisionHash: string) => ({
      workflow_revision_hash: workflowRevisionHash,
      workflow_format_version: 3,
      executable: false,
      not_executable_reason: "agent forms nothing binds yet: outputs",
      name: "Nightly regression sweep",
      description: "Runs the sweep and files what it finds.",
      provenance: null
    });
    const fetcher = vi.fn<typeof fetch>().mockImplementation(async (target) => {
      const resumed = String(target).includes(`${cursorParameterName}=${digest}`);
      return new Response(
        JSON.stringify({
          items: [summary(resumed ? secondPageHash : digest)],
          next_after_revision_hash: resumed ? null : digest
        }),
        { status: 200, headers: { "content-type": "application/json" } }
      );
    });
    const api = createCockpitApi(fetcher);

    const firstPage = await api.listWorkflowRevisions();
    const secondPage = await api.listWorkflowRevisions(firstPage.next_after_revision_hash ?? undefined);

    expect(secondPage.items[0]?.workflow_revision_hash).toEqual(secondPageHash);
  });
});

describe("the catalog name the picker asks for the head", () => {
  it.each(["workflow", "agent_definition"] as const)(
    "asks the by-name door of kind %s and decodes the resolution the document serves",
    async (kind) => {
      const body = {
        display_name: "drei-saetze-review-sehend",
        lineage_id: digest,
        catalog_revision_hash: digest,
        revision_number: 2
      };
      const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
        new Response(JSON.stringify(body), {
          status: 200,
          headers: { "content-type": "application/json" }
        })
      );

      const resolved = await createCockpitApi(fetcher).getRevisionByName(
        kind,
        "drei-saetze-review-sehend"
      );

      expect(String(fetcher.mock.calls[0]?.[0])).toBe(
        `/atelier/api/v1/catalog-revisions/by-name/${kind}/drei-saetze-review-sehend`
      );
      expect(resolved).toEqual(body);
    }
  );

  it("refuses a catalog head whose display name is not the asked name", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({
        display_name: "another-name",
        lineage_id: digest,
        catalog_revision_hash: digest,
        revision_number: 2
      }), { status: 200, headers: { "content-type": "application/json" } })
    );

    await expect(
      createCockpitApi(fetcher).getRevisionByName("workflow", "drei-saetze-review-sehend")
    ).rejects.toThrow(/another display name/);
  });

  it.each(["workflow", "agent_definition"] as const)(
    "proves(a-cockpit-published-v3-workflow-is-named-over-the-api): founds a %s lineage through the one door",
    async (kind) => {
      const body = {
        display_name: "diff-review",
        lineage_id: digest,
        catalog_revision_hash: digest,
        revision_number: 1
      };
      const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
        new Response(JSON.stringify(body), {
          status: 201,
          headers: { "content-type": "application/json" }
        })
      );

      const founded = await createCockpitApi(fetcher).foundCatalogLineage({
        kind,
        catalog_revision_hash: digest,
        actor: "atelier2-cockpit",
        activated_at: "2026-08-18T07:00:00Z"
      });

      expect(String(fetcher.mock.calls[0]?.[0])).toBe("/atelier/api/v1/catalog-lineages");
      expect(JSON.parse(String(fetcher.mock.calls[0]?.[1]?.body))).toEqual({
        kind,
        catalog_revision_hash: digest,
        actor: "atelier2-cockpit",
        activated_at: "2026-08-18T07:00:00Z"
      });
      expect(founded).toEqual({ status: 201, value: body });
    }
  );
});

describe("answering a wait over the existing door", () => {
  function answerMutation() {
    return {
      mutation_id: `wait:run1.cnVu:${digest}`,
      kind: "wait" as const,
      target: "/atelier/api/v1/runs/run1.cnVu/answers",
      content_type: "application/json" as const,
      body_base64: btoa(
        JSON.stringify({
          workflow_revision_hash: digest,
          node_id: "ask",
          expected_node_execution_id: digest,
          actor: "operator",
          answer_base64: btoa("true")
        })
      ),
      public_run_reference: "run1.cnVu",
      workflow_revision_hash: digest,
      node_id: "ask",
      expected_node_execution_id: digest,
      actor: "operator" as const,
      answer_base64: btoa("true"),
      answer_hash: digest
    };
  }

  it("proves(a-waiting-v3-run-is-answerable-on-its-run-page): decodes a V3 run the answers door returns", async () => {
    const run = {
      workflow_format_version: 3,
      run_id: "v3/answer-card",
      public_run_reference: "run1.cnVu",
      workflow_revision_hash: digest,
      workflow_name: "answer card",
      agent_binding_set_hash: digest,
      run_configuration_revision_hash: digest,
      agent_bindings: [],
      orders: [],
      state_version: 2,
      state: "COMPLETED",
      current_node_id: "ask",
      current_node_execution_id: digest,
      node_rail: [{ node_id: "ask", state: "succeeded", attempt: null }],
      cancellation: notCancellableBlock("already-ended"),
      terminal_hash: digest,
      latest_event_cursor: "event1.cnVu.1"
    };
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify(run), {
        status: 202,
        headers: { "content-type": "application/json" }
      })
    );
    const mutation = answerMutation();

    const answered = await createCockpitApi(fetcher).answer(mutation);

    expect(String(fetcher.mock.calls[0]?.[0])).toBe("/atelier/api/v1/runs/run1.cnVu/answers");
    expect(answered).toEqual({ status: 202, value: run });
  });

  it.each([
    ["answer-execution-stale", 409, true],
    ["durable-state-corrupt", 500, true],
    ["temporarily-unavailable", 503, false]
  ] as const)(
    "classifies %s at HTTP %i as definitive=%s",
    async (code, status, definitive) => {
      const problem = {
        type: `${PROBLEM_TYPE_PREFIX}${code}`,
        title: problemDefinitions[code].title,
        status,
        detail: "The durable answer door names this outcome."
      };
      const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
        new Response(JSON.stringify(problem), {
          status,
          headers: { "content-type": "application/problem+json" }
        })
      );

      await expect(createCockpitApi(fetcher).answer(answerMutation())).rejects.toMatchObject({
        problem,
        definitive_failure: definitive
      });
    }
  );
});

describe("cancelling a run over its cancel door", () => {
  const request = cancelMutation(publicReference, digest, "cancel-key-1");

  function cancellingRun() {
    return {
      workflow_format_version: 3,
      run_id: "v3/cancel",
      public_run_reference: publicReference,
      workflow_revision_hash: digest,
      workflow_name: "cancel run",
      agent_binding_set_hash: "b".repeat(64),
      run_configuration_revision_hash: "c".repeat(64),
      agent_bindings: [],
      orders: [],
      state_version: 3,
      state: "STARTED",
      current_node_id: "review",
      current_node_execution_id: digest,
      node_rail: [{ node_id: "review", state: "working", attempt: null }],
      cancellation: notCancellableBlock("already-cancelling"),
      terminal_hash: null,
      latest_event_cursor: "event1.cnVu.3"
    };
  }

  it("posts the exact command to the cancel door and decodes the run it returns", async () => {
    const run = cancellingRun();
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify(run), {
        status: 202,
        headers: { "content-type": "application/json" }
      })
    );

    const result = await createCockpitApi(fetcher).cancelRun(request);

    expect(String(fetcher.mock.calls[0]?.[0])).toBe(
      `/atelier/api/v1/runs/${publicReference}/cancellations`
    );
    expect(result).toEqual({ status: 202, value: run });
  });

  it.each([
    "run-not-cancellable",
    "run-cancellation-command-conflict",
    "run-cancellation-overtaken-by-success"
  ] as const)(
    "reads a 409 %s as a definitive refusal carrying its decoded problem, never a retryable one",
    async (code) => {
      const problem = {
        type: `${PROBLEM_TYPE_PREFIX}${code}`,
        title: problemDefinitions[code].title,
        status: 409,
        detail: "The server's own words for why this cancel cannot land."
      };
      const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
        new Response(JSON.stringify(problem), {
          status: 409,
          headers: { "content-type": "application/json" }
        })
      );

      await expect(createCockpitApi(fetcher).cancelRun(request)).rejects.toMatchObject({
        definitive_failure: true,
        problem
      });
    }
  );
});

describe("forking a finished run from a node", () => {
  it("posts the closed fork body and decodes the successor the door returns", async () => {
    const successor = {
      workflow_format_version: 3,
      run_id: "v3/forked",
      public_run_reference: "run1.Zm9yaw",
      workflow_revision_hash: digest,
      workflow_name: "forked run",
      agent_binding_set_hash: "b".repeat(64),
      run_configuration_revision_hash: "c".repeat(64),
      agent_bindings: [],
      orders: [],
      fork_origin: {
        public_run_reference: publicReference,
        terminal_hash: digest,
        restart_from_node_id: "review",
        fork_hash: digest
      },
      state_version: 1,
      state: "STARTED",
      current_node_id: "review",
      current_node_execution_id: "e".repeat(64),
      node_rail: [
        {
          node_id: "implement",
          state: "succeeded",
          attempt: null,
          reused_from_run_reference: publicReference,
          source_event_hash: digest,
          source_receipt_hash: digest,
          source_declared_context_package_hash: digest
        },
        { node_id: "review", state: "working", attempt: null }
      ],
      cancellation: cancellableBlock(),
      terminal_hash: null,
      latest_event_cursor: null
    };
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify(successor), {
        status: 201,
        headers: { "content-type": "application/json" }
      })
    );

    const result = await createCockpitApi(fetcher).forkRun({
      publicRunReference: publicReference,
      idempotencyKey: "fork-key-1",
      restartFromNodeId: "review"
    });

    expect(String(fetcher.mock.calls[0]?.[0])).toBe(
      `/atelier/api/v1/runs/${publicReference}/forks`
    );
    expect(JSON.parse(String(fetcher.mock.calls[0]?.[1]?.body))).toEqual({
      idempotency_key: "fork-key-1",
      restart_from_node_id: "review"
    });
    expect(result).toEqual({ status: 201, value: successor });
  });
});

describe("the published agent-configuration listing", () => {
  it("asks the collection with the house page bound and decodes the item form", async () => {
    const item = {
      model: "sonnet",
      auth_profile_revision_hash: digest,
      executor_revision: "claude-subscription/v1",
      provider_id: "anthropic",
      auth_mode: "subscription",
      requested_capability: "headless",
      agent_configuration_revision_hash: digest,
      startable: true,
      structurally_startable: true,
      not_startable_reason: null,
      provider_probe_problem_code: null,
      provider_probe_observed_at: null
    };
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ items: [item], next_after_revision_hash: null }), {
        status: 200,
        headers: { "content-type": "application/json" }
      })
    );

    const page = await createCockpitApi(fetcher).listAgentConfigurationRevisions();

    expect(String(fetcher.mock.calls[0]?.[0])).toBe(
      "/atelier/api/v1/agent-configuration-revisions?limit=50"
    );
    expect(page.items).toEqual([item]);
  });

  it("refuses a list item whose startability and reason disagree", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(
        JSON.stringify({
          items: [
            {
              model: "sonnet",
              auth_profile_revision_hash: digest,
              executor_revision: "claude-subscription/v1",
              provider_id: "anthropic",
              auth_mode: "subscription",
              requested_capability: "headless",
              agent_configuration_revision_hash: digest,
              startable: false,
              structurally_startable: true,
              not_startable_reason: null
            }
          ],
          next_after_revision_hash: null
        }),
        { status: 200, headers: { "content-type": "application/json" } }
      )
    );

    await expect(createCockpitApi(fetcher).listAgentConfigurationRevisions()).rejects.toThrow(
      "response did not match the durable wire contract"
    );
  });

  it("refuses a list item claiming startability without its own structural startability", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(
        JSON.stringify({
          items: [
            {
              model: "sonnet",
              auth_profile_revision_hash: digest,
              executor_revision: "claude-subscription/v1",
              provider_id: "anthropic",
              auth_mode: "subscription",
              requested_capability: "headless",
              agent_configuration_revision_hash: digest,
              startable: true,
              structurally_startable: false,
              not_startable_reason: null
            }
          ],
          next_after_revision_hash: null
        }),
        { status: 200, headers: { "content-type": "application/json" } }
      )
    );

    await expect(createCockpitApi(fetcher).listAgentConfigurationRevisions()).rejects.toThrow(
      "response did not match the durable wire contract"
    );
  });

  const baseListedItem = {
    model: "sonnet",
    auth_profile_revision_hash: digest,
    executor_revision: "claude-subscription/v1",
    provider_id: "anthropic",
    auth_mode: "subscription",
    requested_capability: "headless",
    agent_configuration_revision_hash: digest
  };

  it("accepts a structurally unavailable item that names its own binding reason", async () => {
    const item = {
      ...baseListedItem,
      startable: false,
      structurally_startable: false,
      not_startable_reason: "agent-executor-binding-unavailable",
      provider_probe_problem_code: null,
      provider_probe_observed_at: null
    };
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ items: [item], next_after_revision_hash: null }), {
        status: 200,
        headers: { "content-type": "application/json" }
      })
    );

    const page = await createCockpitApi(fetcher).listAgentConfigurationRevisions();

    expect(page.items).toEqual([item]);
  });

  it("refuses a structurally unavailable item whose reason is not the binding reason", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(
        JSON.stringify({
          items: [
            {
              ...baseListedItem,
              startable: false,
              structurally_startable: false,
              not_startable_reason: "model-not-registered",
              provider_probe_problem_code: null,
              provider_probe_observed_at: null
            }
          ],
          next_after_revision_hash: null
        }),
        { status: 200, headers: { "content-type": "application/json" } }
      )
    );

    await expect(createCockpitApi(fetcher).listAgentConfigurationRevisions()).rejects.toThrow(
      "response did not match the durable wire contract"
    );
  });

  it("refuses a structurally startable item that still carries the binding-unavailable reason", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(
        JSON.stringify({
          items: [
            {
              ...baseListedItem,
              startable: false,
              structurally_startable: true,
              not_startable_reason: "agent-executor-binding-unavailable",
              provider_probe_problem_code: null,
              provider_probe_observed_at: null
            }
          ],
          next_after_revision_hash: null
        }),
        { status: 200, headers: { "content-type": "application/json" } }
      )
    );

    await expect(createCockpitApi(fetcher).listAgentConfigurationRevisions()).rejects.toThrow(
      "response did not match the durable wire contract"
    );
  });

  it("refuses a provider-probe-failed reason that names none of its own evidence", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(
        JSON.stringify({
          items: [
            {
              ...baseListedItem,
              startable: false,
              structurally_startable: true,
              not_startable_reason: "provider-probe-failed",
              provider_probe_problem_code: null,
              provider_probe_observed_at: null
            }
          ],
          next_after_revision_hash: null
        }),
        { status: 200, headers: { "content-type": "application/json" } }
      )
    );

    await expect(createCockpitApi(fetcher).listAgentConfigurationRevisions()).rejects.toThrow(
      "response did not match the durable wire contract"
    );
  });
});

describe("the observed queue a start-sheet work-item picker reads", () => {
  const observedItem = {
    project_id: "atelier",
    tracker_item_reference: "gh:450",
    item_id: digest,
    state: "OBSERVED",
    revision: 0,
    proposal: null,
    admission: null,
    launch_binding: null,
    blockers: [],
    tracker_enrichment: "ENRICHMENT_UNAVAILABLE",
    title: "Preview door",
    title_observed_at: "2026-09-01T14:00:00Z",
    retired_at: "2026-09-02T09:30:00Z"
  };
  const proposedItem = {
    ...observedItem,
    tracker_item_reference: "gh:451",
    item_id: "b".repeat(64),
    state: "PROPOSED",
    revision: 1,
    proposal: {
      revision: 1,
      priority: { rank: 1 },
      workflow_lineage_id: digest,
      prerequisite_item_ids: [],
      automation_disposition: "HUMAN_REQUIRED",
      policy_revision: 1,
      source: "OPERATOR"
    }
  };
  const mappedObservedItem = {
    project_id: observedItem.project_id,
    tracker_item_reference: observedItem.tracker_item_reference,
    item_id: observedItem.item_id,
    revision: observedItem.revision,
    title: observedItem.title,
    title_observed_at: observedItem.title_observed_at,
    retired_at: observedItem.retired_at
  };

  it("asks the served observed queue page and decodes its items and cursor", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ items: [observedItem, proposedItem], next_after: null }), {
        status: 200,
        headers: { "content-type": "application/json" }
      })
    );

    const page = await createCockpitApi(fetcher).listObservedQueueItems();

    expect(String(fetcher.mock.calls[0]?.[0])).toBe("/atelier/api/v1/queue-items?limit=50");
    expect(page).toEqual({ items: [mappedObservedItem], next_after: null });
  });

  it("resumes the observed queue page at the after cursor", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ items: [observedItem], next_after: digest }), {
        status: 200,
        headers: { "content-type": "application/json" }
      })
    );

    const page = await createCockpitApi(fetcher).listObservedQueueItems(digest);

    expect(String(fetcher.mock.calls[0]?.[0])).toBe(
      `/atelier/api/v1/queue-items?limit=50&after=${digest}`
    );
    expect(page).toEqual({ items: [mappedObservedItem], next_after: digest });
  });

  it("keeps a null title and timestamp when the projection has no observation", async () => {
    const unobservedItem = {
      ...observedItem,
      title: null,
      title_observed_at: null,
      retired_at: null
    };
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(
        JSON.stringify({
          items: [unobservedItem],
          next_after: null
        }),
        { status: 200, headers: { "content-type": "application/json" } }
      )
    );

    await expect(createCockpitApi(fetcher).listObservedQueueItems()).resolves.toEqual({
      items: [{
        project_id: unobservedItem.project_id,
        tracker_item_reference: unobservedItem.tracker_item_reference,
        item_id: unobservedItem.item_id,
        revision: unobservedItem.revision,
        title: null,
        title_observed_at: null,
        retired_at: null
      }],
      next_after: null
    });
  });

  it("refuses an observation stamp that is not the canonical zero-padded UTC form", async () => {
    const unpaddedStampItem = { ...observedItem, title_observed_at: "2026-9-01T14:00:00Z" };
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(
        JSON.stringify({ items: [unpaddedStampItem], next_after: null }),
        { status: 200, headers: { "content-type": "application/json" } }
      )
    );

    await expect(createCockpitApi(fetcher).listObservedQueueItems()).rejects.toThrow(
      "response did not match the durable wire contract"
    );
  });
});

describe("the published agent definitions the catalog reads", () => {
  const digest = "a".repeat(64);

  it("asks the listing door for one page", async () => {
    const item = {
      agent_definition_revision_hash: digest,
      name: "scribe",
      description: "Writes what the stage needs."
    };
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ items: [item], next_after_revision_hash: null }), {
        status: 200,
        headers: { "content-type": "application/json" }
      })
    );

    const page = await createCockpitApi(fetcher).listAgentDefinitionRevisions();

    expect(String(fetcher.mock.calls[0]?.[0])).toBe(
      "/atelier/api/v1/agent-definition-revisions?limit=50"
    );
    expect(page.items).toEqual([item]);
  });

  it("sends the authored file as the exact Markdown bytes the door takes", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ agent_definition_revision_hash: digest }), {
        status: 201,
        headers: { "content-type": "application/json" }
      })
    );
    const authored = "---\nname: scribe\ndescription: Writes.\n---\n\nYou write.\n";

    const result = await createCockpitApi(fetcher).publishAgentDefinition(authored);

    const request = fetcher.mock.calls[0]?.[1];
    expect(String(fetcher.mock.calls[0]?.[0])).toBe(
      "/atelier/api/v1/agent-definition-revisions"
    );
    expect(request?.method).toBe("POST");
    expect((request?.headers as Record<string, string>)["content-type"]).toBe(
      "text/markdown"
    );
    expect(new TextDecoder().decode(request?.body as Uint8Array)).toBe(authored);
    expect(result).toEqual({
      status: 201,
      value: { agent_definition_revision_hash: digest }
    });
  });

  it("carries the refusal the door named instead of a sentence of its own", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(
        JSON.stringify({
          type: "urn:atelier2:problem:v1:agent-definition-field-missing",
          title: "Invalid agent definition document",
          status: 422,
          detail: "agent-definition-field-missing: name"
        }),
        { status: 422, headers: { "content-type": "application/problem+json" } }
      )
    );

    await expect(
      createCockpitApi(fetcher).publishAgentDefinition(
        "---\ndescription: A nameless agent.\n---\nBody.\n"
      )
    ).rejects.toThrow("agent-definition-field-missing: name");
  });
});

describe("the one-step catalog import", () => {
  const document = new TextEncoder().encode("format_version: 3\nname: import-proof\n");

  it("recognizes opaque file bytes before the operator confirms an addition", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({
        outcome: "workflow",
        workflow_format_version: 3,
        name: "import-proof",
        description: null
      }), { status: 200, headers: { "content-type": "application/json" } })
    );

    const recognition = await createCockpitApi(fetcher).recognizeLibraryDocument(
      document,
      "import-proof.yaml"
    );

    const request = fetcher.mock.calls[0]?.[1];
    expect(String(fetcher.mock.calls[0]?.[0])).toBe(
      "/atelier/api/v1/library/recognitions?file_name=import-proof.yaml"
    );
    expect((request?.headers as Record<string, string>)["content-type"]).toBe(
      "application/octet-stream"
    );
    expect(new TextDecoder().decode(request?.body as ArrayBuffer)).toBe(
      new TextDecoder().decode(document)
    );
    expect(recognition.outcome).toBe("workflow");
  });

  it("posts the declared kind with the opaque bytes", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({
        intake_id: digest,
        kind: "workflow"
      }), { status: 201, headers: { "content-type": "application/json" } })
    );

    const addition = await createCockpitApi(fetcher).addLibraryDocument(
      document,
      "workflow",
      "atelier2-cockpit",
      "2026-08-27T10:00:00Z"
    );

    const request = fetcher.mock.calls[0]?.[1];
    expect(String(fetcher.mock.calls[0]?.[0])).toBe(
      "/atelier/api/v1/library/additions?kind=workflow&actor=atelier2-cockpit&activated_at=2026-08-27T10%3A00%3A00Z"
    );
    expect(new TextDecoder().decode(request?.body as ArrayBuffer)).toBe(
      new TextDecoder().decode(document)
    );
    expect(addition.status).toBe(201);
    expect(addition.value).toEqual({ intake_id: digest, kind: "workflow" });
  });
});

describe("the run listing the studio opens on", () => {
  const v3Run = {
    workflow_format_version: 3,
    run_id: "run-1",
    public_run_reference: publicReference,
    workflow_revision_hash: digest,
    workflow_name: "listed run",
    agent_binding_set_hash: "b".repeat(64),
    run_configuration_revision_hash: "c".repeat(64),
    agent_bindings: [],
    orders: [],
    state_version: 1,
    state: "STARTED",
    current_node_id: "implement",
    current_node_execution_id: digest,
    node_rail: [{ node_id: "implement", state: "working", attempt: null }],
    cancellation: cancellableBlock(),
    terminal_hash: null,
    latest_event_cursor: null
  };

  it("proves(the-run-listing-holds-every-format-the-api-answers-with): decodes a page that holds a version 3 run instead of failing the whole studio", async () => {
    // The operator's own repro: one V3 run exists, and every level that lists
    // runs -- the studio and the project -- answered "Request failed — wire
    // contract" because the page decoder knew only V1 and V2. The detail page
    // had been taught V3; the listing had not, so a single V3 run took down the
    // page the workshop opens on.
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(
        JSON.stringify({ items: [{ kind: "run", run: v3Run }], next_after: null }),
        { status: 200, headers: { "content-type": "application/json" } }
      )
    );

    const page = await createCockpitApi(fetcher).listRuns();

    expect(fetcher.mock.calls[0]?.[0]).toBe("/atelier/api/v1/runs?limit=50");
    expect(page.items).toHaveLength(1);
    const row = page.items[0];
    if (row?.kind !== "run") {
      throw new Error("expected a decoded run row");
    }
    expect(row.run.public_run_reference).toBe(publicReference);
    expect(row.run.state).toBe("STARTED");
  });

  it("decodes a run whose own projection failed as a defective row, not a run (#1042)", async () => {
    // A run list answers for every entry it can: the row a corrupt run
    // becomes carries its reference and reason, never bent into a run shape
    // it does not have.
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(
        JSON.stringify({
          items: [
            {
              kind: "defective",
              public_run_reference: publicReference,
              problem_code: "durable-state-corrupt",
              detail: "run current node is absent from its workflow graph"
            }
          ],
          next_after: null
        }),
        { status: 200, headers: { "content-type": "application/json" } }
      )
    );

    const page = await createCockpitApi(fetcher).listRuns();

    expect(page.items).toEqual([
      {
        kind: "defective",
        public_run_reference: publicReference,
        problem_code: "durable-state-corrupt",
        detail: "run current node is absent from its workflow graph"
      }
    ]);
  });

  it("decodes a run started with an order, its size and pinned schema, never its bytes", async () => {
    const orderedRun = {
      ...v3Run,
      run_id: "run-with-an-order",
      orders: [
        {
          name: "headline",
          bytes: 19,
          schema_revision_hash: "d".repeat(64)
        }
      ]
    };
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(
        JSON.stringify({
          items: [{ kind: "run", run: orderedRun }],
          next_after: null
        }),
        { status: 200, headers: { "content-type": "application/json" } }
      )
    );

    const page = await createCockpitApi(fetcher).listRuns();

    expect(page.items).toHaveLength(1);
    const row = page.items[0];
    if (row?.kind !== "run") {
      throw new Error("expected a decoded run row");
    }
    expect(row.run.orders).toEqual([
      { name: "headline", bytes: 19, schema_revision_hash: "d".repeat(64) }
    ]);
  });

  it("asks the list for one durable state when the studio names that state", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ items: [], next_after: null }), {
        status: 200,
        headers: { "content-type": "application/json" }
      })
    );

    await createCockpitApi(fetcher).listRuns(undefined, "WAITING_INPUT");

    expect(fetcher.mock.calls[0]?.[0]).toBe(
      "/atelier/api/v1/runs?limit=50&state=WAITING_INPUT"
    );
  });
});

describe("the project listing the picker will consume", () => {
  it("asks the zero-or-one project door and refuses fields the server did not declare", async () => {
    const project = { public_project_reference: "project1.dGVhbS9yZWQ" };
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ items: [project] }), {
        status: 200,
        headers: { "content-type": "application/json" }
      })
    );

    const listed = await createCockpitApi(fetcher).listProjects();

    expect(fetcher.mock.calls[0]?.[0]).toBe("/atelier/api/v1/projects");
    expect(listed.items).toEqual([project]);

    fetcher.mockResolvedValueOnce(
      new Response(JSON.stringify({ items: [{ ...project, project_id: "team/red" }] }), {
        status: 200,
        headers: { "content-type": "application/json" }
      })
    );
    await expect(createCockpitApi(fetcher).listProjects()).rejects.toThrow(
      "durable wire contract"
    );
  });
});

describe("the project source connection Settings will read", () => {
  const projectReference = "project1.dGVhbS9yZWQ";
  const connection = {
    public_project_reference: projectReference,
    revision_number: 3,
    source_kind: "github",
    source_address: "FlexOr2/atelier-2",
    auth_method: "personal-access-token" as const,
    project_source_connection_revision_hash: "a".repeat(64)
  };

  it("asks the source-connection door and decodes only its declared resource", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify(connection), {
        status: 200,
        headers: { "content-type": "application/json" }
      })
    );

    const read = await createCockpitApi(fetcher).getProjectSourceConnection(projectReference);

    expect(fetcher.mock.calls[0]?.[0]).toBe(
      `/atelier/api/v1/projects/${projectReference}/source-connection`
    );
    expect(read).toEqual(projectSourceConnectionRevisionSchema.parse(connection));
  });

  it("refuses extra fields and a response for another project", async () => {
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ ...connection, credential_directory: "/operator/credentials" }), {
          status: 200,
          headers: { "content-type": "application/json" }
        })
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({ ...connection, public_project_reference: "project1.b3RoZXI" }),
          { status: 200, headers: { "content-type": "application/json" } }
        )
      );
    const client = createCockpitApi(fetcher);

    await expect(client.getProjectSourceConnection(projectReference)).rejects.toThrow(
      "durable wire contract"
    );
    await expect(client.getProjectSourceConnection(projectReference)).rejects.toThrow(
      /another project/
    );
  });
});

describe("the project source collection Settings writes", () => {
  const projectReference = "project1.dGVhbS9yZWQ";
  const sourceReference = "source1.MzgwZjI3YTEtNmRlMC01NjNkLTQwYWItYzg1MzBmOWMyNWNj";
  const source = {
    public_source_reference: sourceReference,
    kind: "github",
    address: "FlexOr2/atelier-2",
    scope: "issues" as const,
    connected_at: null,
    revision: 2,
    auth_method: "personal-access-token" as const
  };
  const token = "write-only-token";

  function jsonOk(body: unknown, status = 200): Response {
    return new Response(JSON.stringify(body), {
      status,
      headers: { "content-type": "application/json" }
    });
  }

  it("lists the collection and defaults omitted scope and connection time", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      jsonOk({
        items: [{
          public_source_reference: sourceReference,
          kind: "github",
          address: "FlexOr2/atelier-2",
          revision: 2,
          auth_method: "personal-access-token"
        }]
      })
    );

    const read = await createCockpitApi(fetcher).listProjectSources(projectReference);

    expect(fetcher.mock.calls[0]?.[0]).toBe(
      `/atelier/api/v1/projects/${projectReference}/sources`
    );
    expect(read).toEqual(projectSourceListSchema.parse({ items: [source] }));
  });

  it("posts only address and token, then returns the created resource without a token", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonOk(source, 201));

    const created = await createCockpitApi(fetcher).connectProjectSource(projectReference, {
      address: "FlexOr2/atelier-2",
      token
    });

    const init = fetcher.mock.calls[0]?.[1];
    expect(fetcher.mock.calls[0]?.[0]).toBe(
      `/atelier/api/v1/projects/${projectReference}/sources`
    );
    expect(init?.method).toBe("POST");
    expect((init?.headers as Record<string, string>)["content-type"]).toBe("application/json");
    expect(init?.body).toBe(JSON.stringify({ address: "FlexOr2/atelier-2", token }));
    expect(JSON.parse(String(init?.body))).toEqual({ address: "FlexOr2/atelier-2", token });
    expect(created).toEqual(projectSourceResourceSchema.parse(source));
    expect(JSON.stringify(created)).not.toContain(token);
  });

  it("puts only a token and keeps the returned resource free of it", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonOk(source));

    const rotated = await createCockpitApi(fetcher).rotateProjectSourceToken(
      projectReference,
      sourceReference,
      { token }
    );

    const init = fetcher.mock.calls[0]?.[1];
    expect(fetcher.mock.calls[0]?.[0]).toBe(
      `/atelier/api/v1/projects/${projectReference}/sources/${sourceReference}/token`
    );
    expect(init?.method).toBe("PUT");
    expect(init?.body).toBe(JSON.stringify({ token }));
    expect(rotated).toEqual(source);
    expect(JSON.stringify(rotated)).not.toContain(token);
  });

  it("disconnects a 204 empty body without parsing JSON", async () => {
    const json = vi.fn();
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue({
      status: 204,
      headers: new Headers(),
      json
    } as unknown as Response);

    await expect(
      createCockpitApi(fetcher).disconnectProjectSource(projectReference, sourceReference)
    ).resolves.toBeUndefined();
    expect(fetcher.mock.calls[0]?.[1]?.method).toBe("DELETE");
    expect(json).not.toHaveBeenCalled();
  });

  it("refuses extra fields and a too-long source reference", async () => {
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(jsonOk({ items: [{ ...source, credential_directory: "/secret" }] }))
      .mockResolvedValueOnce(jsonOk({
        ...source,
        public_source_reference: `source1.${"A".repeat(49)}`
      }, 201));
    const client = createCockpitApi(fetcher);

    await expect(client.listProjectSources(projectReference)).rejects.toThrow(
      "durable wire contract"
    );
    await expect(
      client.connectProjectSource(projectReference, { address: source.address, token })
    ).rejects.toThrow("durable wire contract");
  });

  it("refuses a source list carrying more connections than a project may hold", async () => {
    const second = {
      ...source,
      public_source_reference: "source1.YWx0ZXJuYXRlLXNvdXJjZS1yZWZlcmVuY2UtYWFhYQ",
      address: "github.com/other/repo"
    };
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonOk({ items: [source, second] }));

    await expect(
      createCockpitApi(fetcher).listProjectSources(projectReference)
    ).rejects.toThrow("durable wire contract");
  });
});

describe("the project model configuration the start sheet consumes", () => {
  const projectReference = "project1.dGVhbS9yZWQ";
  const configurationHash = "d".repeat(64);
  const registry = {
    provider_id: "openai",
    revision_number: 1,
    model_registry_revision_hash: "a".repeat(64),
    entries: [{
      model_id: "gpt-5.6",
      agent_configuration_revision_hash: configurationHash,
      source: "discovered",
      provider_check: "checked"
    }]
  };

  it("reads a provider registry and resolves project roles", async () => {
    const resolution = {
      project_id: "team/red",
      public_project_reference: projectReference,
      workflow_revision_hash: "b".repeat(64),
      resolutions: [{
        role: "builder",
        agent_configuration_revision_hash: configurationHash,
        source: "pinned-in-workflow",
        model_id: "gpt-5.6",
        declared_difficulty: 2,
        default_difficulty: 2,
        uncast_reason: null,
        family_differs_from: null
      }]
    };
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(new Response(JSON.stringify(registry), { status: 200, headers: { "content-type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify(resolution), { status: 200, headers: { "content-type": "application/json" } }));
    const client = createCockpitApi(fetcher);

    expect((await client.getModelRegistry("openai")).entries[0]?.model_id).toBe("gpt-5.6");
    expect((await client.resolveProjectModels(projectReference, "b".repeat(64), [])).resolutions[0]?.source).toBe("pinned-in-workflow");
    expect(fetcher.mock.calls[1]?.[1]?.body).toBe(JSON.stringify({ workflow_revision_hash: "b".repeat(64), overrides: [] }));
  });

  it.each([
    {
      name: "a registry naming another provider",
      response: { ...registry, provider_id: "anthropic" },
      read: (client: ReturnType<typeof createCockpitApi>) => client.getModelRegistry("openai"),
      message: "model registry response named another provider"
    },
    {
      name: "a resolution naming another project",
      response: {
        project_id: "team/blue",
        public_project_reference: "project1.dGVhbS9ibHVl",
        workflow_revision_hash: "b".repeat(64),
        resolutions: []
      },
      read: (client: ReturnType<typeof createCockpitApi>) =>
        client.resolveProjectModels(projectReference, "b".repeat(64), []),
      message: "model resolution response named another project"
    },
    {
      name: "a resolution naming another workflow",
      response: {
        project_id: "team/red",
        public_project_reference: projectReference,
        workflow_revision_hash: "c".repeat(64),
        resolutions: []
      },
      read: (client: ReturnType<typeof createCockpitApi>) =>
        client.resolveProjectModels(projectReference, "b".repeat(64), []),
      message: "model resolution response named another workflow"
    }
  ])("refuses $name", async ({ response, read, message }) => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify(response), { status: 200, headers: { "content-type": "application/json" } })
    );

    await expect(read(createCockpitApi(fetcher))).rejects.toThrow(message);
  });
});

describe("the node a click asks the server about", () => {
  const nodeDetail = {
    run_id: "run-1",
    public_run_reference: publicReference,
    node_id: "review",
    state: "queued",
    job_base64: null,
    job_hash: null,
    answer: null,
    provenance: null,
    refusal: null
  };

  it("proves(a-click-into-a-node-shows-what-it-was-asked-and-wrote): asks the node route and decodes the answer", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify(nodeDetail), {
        status: 200,
        headers: { "content-type": "application/json" }
      })
    );

    const detail = await createCockpitApi(fetcher).getNodeDetail(publicReference, "review");

    expect(String(fetcher.mock.calls[0]?.[0])).toBe(
      `/atelier/api/v1/runs/${publicReference}/nodes/review`
    );
    expect(detail.node_id).toBe("review");
    expect(detail.refusal).toBeNull();
  });

  it("proves(a-click-into-a-node-shows-what-it-was-asked-and-wrote): decodes a node that ran, with its job, its answer and its provenance", async () => {
    const ran = {
      ...nodeDetail,
      node_id: "implement",
      state: "succeeded",
      job_base64: btoa("Write three German sentences."),
      job_hash: digest,
      answer: { value_base64: btoa("Ein gutes Review."), value_hash: "d".repeat(64) },
      provenance: {
        role: "builder",
        provider_id: "anthropic",
        model: "sonnet",
        executor_revision: "headless-print-json/v1",
        executor_operational_identity: "headless-print-json/v1",
        auth_mode: "subscription",
        profile_id: "operator-subscription",
        agent_configuration_revision_hash: "e".repeat(64),
        request_hash: "f".repeat(64),
        receipt_hash: "a".repeat(64)
      }
    };
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify(ran), {
        status: 200,
        headers: { "content-type": "application/json" }
      })
    );

    const detail = await createCockpitApi(fetcher).getNodeDetail(publicReference, "implement");

    expect(detail.provenance?.model).toBe("sonnet");
    expect(detail.answer?.value_hash).toBe("d".repeat(64));
    // The two hashes are different values, and the decoder keeps them apart.
    expect(detail.job_hash).not.toBe(detail.provenance?.request_hash);
  });

  it("refuses an answer that names another node instead of showing it", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ ...nodeDetail, node_id: "somewhere-else" }), {
        status: 200,
        headers: { "content-type": "application/json" }
      })
    );

    await expect(
      createCockpitApi(fetcher).getNodeDetail(publicReference, "review")
    ).rejects.toThrow(/named another node/);
  });

  function answering(payload: unknown) {
    return vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify(payload), {
        status: 200,
        headers: { "content-type": "application/json" }
      })
    );
  }

  const beforeMoments = { origin: "v1-before-moments" as const };
  const succeededTranscript = {
    events: [
      {
        event: "tool-called" as const,
        name: "Read",
        arguments: '{"file_path":"src/app.ts"}',
        redacted: false,
        moment: beforeMoments
      },
      {
        event: "tool-returned" as const,
        name: "Read",
        result: "export function start() {}",
        redacted: false,
        moment: beforeMoments
      },
      {
        event: "assistant-turn" as const,
        text: "I read the file and will write the three sentences.",
        redacted: false,
        moment: beforeMoments
      },
      {
        event: "usage" as const,
        input_tokens: 1200,
        output_tokens: 48,
        cache_read_input_tokens: 0,
        cache_creation_input_tokens: 0,
        moment: beforeMoments
      }
    ]
  };

  it("asks the node route and decodes a succeeded attempt transcript", async () => {
    const payload = {
      ...nodeDetail,
      node_id: "implement",
      state: "succeeded",
      transcript: succeededTranscript
    };
    const fetcher = answering(payload);

    const detail = await createCockpitApi(fetcher).getNodeDetail(publicReference, "implement");

    expect(String(fetcher.mock.calls[0]?.[0])).toBe(
      `/atelier/api/v1/runs/${publicReference}/nodes/implement`
    );
    expect(detail.transcript).toEqual(succeededTranscript);
  });

  it("carries a failed attempt's already-redacted stdout as unrecognised provider output", async () => {
    const servedStdout = {
      event: "unrecognised-provider-output" as const,
      text: "fatal: token [redacted] rejected",
      redacted: true,
      moment: beforeMoments
    };
    const fetcher = answering({
      ...nodeDetail,
      state: "failed",
      transcript: { events: [servedStdout] }
    });

    const detail = await createCockpitApi(fetcher).getNodeDetail(publicReference, "review");

    expect(detail.transcript).toEqual({ events: [servedStdout] });
  });

  it("still decodes a node payload that omits transcript", async () => {
    const detail = await createCockpitApi(answering(nodeDetail)).getNodeDetail(
      publicReference,
      "review"
    );

    expect(detail).not.toHaveProperty("transcript");
    expect(nodeDetailSchema.parse({ ...nodeDetail, transcript: null }).transcript).toBeNull();
  });

  it("refuses an empty transcript events list", () => {
    expect(() =>
      nodeDetailSchema.parse({ ...nodeDetail, transcript: { events: [] } })
    ).toThrow();
  });

  it("refuses an unknown transcript event kind", () => {
    expect(() =>
      nodeDetailSchema.parse({
        ...nodeDetail,
        transcript: {
          events: [{ event: "thinking", text: "a private chain of thought", redacted: false }]
        }
      })
    ).toThrow();
  });

  it("refuses transcript keys the wire does not serve", () => {
    const turn = {
      event: "assistant-turn" as const,
      text: "done",
      redacted: false,
      moment: beforeMoments
    };

    expect(() =>
      nodeDetailSchema.parse({
        ...nodeDetail,
        transcript: { events: [turn], kind: "claude-json" }
      })
    ).toThrow();
    expect(() =>
      nodeDetailSchema.parse({
        ...nodeDetail,
        transcript: { events: [turn], document: "e30=" }
      })
    ).toThrow();
    expect(() =>
      nodeDetailSchema.parse({
        ...nodeDetail,
        transcript: { events: [{ ...turn, invented: true }] }
      })
    ).toThrow();
    expect(() =>
      nodeDetailSchema.parse({
        ...nodeDetail,
        transcript: {
          events: [
            {
              event: "usage",
              input_tokens: 1,
              output_tokens: 1,
              cache_read_input_tokens: 0,
              cache_creation_input_tokens: 0,
              redacted: false,
              moment: beforeMoments
            }
          ]
        }
      })
    ).toThrow();
  });

  it("accepts a transcript step at the 8192-character bound and refuses one character over", () => {
    const atBound = {
      event: "assistant-turn" as const,
      text: "a".repeat(MAXIMUM_TRANSCRIPT_STEP_CHARACTERS),
      redacted: false,
      moment: beforeMoments
    };

    expect(
      nodeDetailSchema.parse({ ...nodeDetail, transcript: { events: [atBound] } }).transcript
    ).toEqual({ events: [atBound] });
    expect(() =>
      nodeDetailSchema.parse({
        ...nodeDetail,
        transcript: { events: [{ ...atBound, text: `${atBound.text}a` }] }
      })
    ).toThrow();
  });

  it("keeps a recorded transcript moment with origin recorded", () => {
    const turn = {
      event: "assistant-turn" as const,
      text: "done",
      redacted: false,
      moment: {
        origin: "recorded" as const,
        recorded_at: "2026-08-18T15:00:00Z"
      }
    };

    expect(assistantTurnEventSchema.parse(turn)).toEqual(turn);
  });

  it("keeps a v1-before-moments transcript moment and does not treat it as missing", () => {
    const turn = {
      event: "assistant-turn" as const,
      text: "done",
      redacted: false,
      moment: { origin: "v1-before-moments" as const }
    };
    const decoded = assistantTurnEventSchema.parse(turn);

    expect(decoded).toEqual(turn);
    expect(decoded.moment.origin).toBe("v1-before-moments");
  });

  it("refuses a transcript event that omits moment", () => {
    expect(() =>
      assistantTurnEventSchema.parse({
        event: "assistant-turn",
        text: "done",
        redacted: false
      })
    ).toThrow();
  });

  it("refuses a third transcript moment origin, a missing origin, recorded without recorded_at, and v1-before-moments with a time", () => {
    const turn = {
      event: "assistant-turn" as const,
      text: "done",
      redacted: false
    };

    expect(() =>
      assistantTurnEventSchema.parse({
        ...turn,
        moment: { origin: "unknown", recorded_at: "2026-08-18T15:00:00Z" }
      })
    ).toThrow();
    expect(() =>
      assistantTurnEventSchema.parse({
        ...turn,
        moment: { recorded_at: "2026-08-18T15:00:00Z" }
      })
    ).toThrow();
    expect(() =>
      assistantTurnEventSchema.parse({
        ...turn,
        moment: { origin: "recorded" }
      })
    ).toThrow();
    expect(() =>
      assistantTurnEventSchema.parse({
        ...turn,
        moment: {
          origin: "v1-before-moments",
          recorded_at: "2026-08-18T15:00:00Z"
        }
      })
    ).toThrow();
  });
});

describe("a run's node rail", () => {
  it("proves(the-browser-and-the-served-contract-know-the-same-node-states): accepts exactly the node states the document declares", () => {
    const servedStates = servedDocument.components.schemas.NodeRailResource?.properties?.state?.enum;

    expect(nodeRailEntrySchema.shape.state.options).toEqual(servedStates);
    expect(nodeDetailSchema.shape.state.options).toEqual(servedStates);
  });

  it("refuses partial reuse evidence and reuse on a node that did not succeed", () => {
    const completeEvidence = {
      reused_from_run_reference: "run1.cnVu",
      source_event_hash: "a".repeat(64),
      source_receipt_hash: "b".repeat(64),
      source_declared_context_package_hash: "c".repeat(64)
    };
    const ordinary = { node_id: "implement", state: "succeeded", attempt: null };

    for (const field of Object.keys(completeEvidence) as Array<keyof typeof completeEvidence>) {
      const partial: Partial<typeof completeEvidence> = { ...completeEvidence };
      delete partial[field];
      expect(nodeRailEntrySchema.safeParse({ ...ordinary, ...partial }).success).toBe(false);
    }
    expect(
      nodeRailEntrySchema.safeParse({
        ...ordinary,
        state: "failed",
        ...completeEvidence
      }).success
    ).toBe(false);
    expect(nodeRailEntrySchema.safeParse({ ...ordinary, ...completeEvidence }).success).toBe(true);
  });
});

describe("the canonical base64 and public run reference codecs", () => {
  it("round-trips every byte value through canonical base64", () => {
    const bytes = Uint8Array.from({ length: 256 }, (_, index) => index);
    const encoded = btoa(String.fromCodePoint(...bytes));

    expect(decodeCanonicalBase64(encoded)).toEqual(bytes);
  });

  it("refuses base64 that is not the canonical standard alphabet and padding", () => {
    expect(decodeCanonicalBase64("not base64!!")).toBeNull();
  });

  it.each([
    ["run", "run1.cnVu"],
    ["runs", "run1.cnVucw"],
    ["run-1", "run1.cnVuLTE"]
  ])("round-trips a run id needing every base64 padding length (%s)", (runId, reference) => {
    expect(encodePublicRunReference(runId)).toBe(reference);
    expect(decodePublicRunReference(reference)).toBe(runId);
  });

  it("refuses a public run reference whose body is not canonical unpadded base64url", () => {
    expect(decodePublicRunReference("run1.cnVu==")).toBeNull();
  });
});

describe("event cursor parsing", () => {
  it("parses a canonical run reference and positive sequence", () => {
    expect(parseEventCursor("event1.cnVu.12")).toEqual({
      publicRunReference: "run1.cnVu",
      sequence: 12
    });
  });

  it("refuses a sequence with a leading zero", () => {
    expect(parseEventCursor("event1.cnVu.012")).toBeNull();
  });

  it("refuses a sequence that is not all digits", () => {
    expect(parseEventCursor("event1.cnVu.1x")).toBeNull();
  });
});
