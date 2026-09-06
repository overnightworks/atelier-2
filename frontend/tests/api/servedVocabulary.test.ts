import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import {
  EFFECT_CONFIRMATION_SOURCES,
  MAXIMUM_RUN_FORK_SUCCESSORS,
  MAXIMUM_TRANSCRIPT_STEP_CHARACTERS,
  NODE_STATES,
  PUBLIC_ATTEMPT_STATES,
  RUN_NOT_CANCELLABLE_REASONS,
  RUN_STATES_V3,
  problemDefinitions,
  agentConfigurationRevisionListItemObjectSchema,
  agentConfigurationRevisionPageSchema,
  agentDefinitionRevisionDetailSchema,
  agentDefinitionRevisionListItemSchema,
  agentDefinitionRevisionPageSchema,
  attemptTranscriptSchema,
  assistantTurnEventSchema,
  authProfileRevisionPageSchema,
  queueItemSchema,
  nodeDetailSchema,
  nodeRailEntrySchema,
  providerTerminalRefusalEventSchema,
  toolCalledEventSchema,
  toolReturnedEventSchema,
  transcriptTruncatedEventSchema,
  unrecognisedProviderOutputEventSchema,
  usageEventSchema,
  runV3Schema,
  observedQueueItemSchema,
  decodeStreamFrame
} from "../../src/api/client";

/**
 * The frozen OpenAPI document is the one object both sides can read: the server
 * renders it from its own vocabulary owner, and the checked-in artefact only
 * changes when someone decides a wire change. Reading it here is what makes
 * "the browser knows the states the server serves" a test instead of a habit.
 */
const servedDocument = JSON.parse(
  readFileSync(resolve(process.cwd(), "..", "tests", "api", "openapi_frozen.json"), "utf8")
) as {
  components: {
    schemas: Record<
      string,
      {
        enum?: string[];
        properties?: Record<
          string,
          {
            enum?: string[];
            const?: string;
            $ref?: string;
            anyOf?: Array<{ enum?: string[] }>;
            maxItems?: number;
          }
        >;
      }
    >;
  };
};

const PROBLEM_TYPE_PREFIX = "urn:atelier2:problem:v1:";

describe("the served vocabulary", () => {
  it("decodes exactly the queue-item fields the document serves", () => {
    const served = servedDocument.components.schemas.QueueItemResource;

    expect(Object.keys(queueItemSchema.shape).sort()).toEqual(
      Object.keys(served?.properties ?? {}).sort()
    );
    expect(Object.keys(observedQueueItemSchema.shape).sort()).toEqual([
      "item_id",
      "project_id",
      "retired_at",
      "revision",
      "title",
      "title_observed_at",
      "tracker_item_reference"
    ]);
  });

  it("proves(the-browser-and-the-served-contract-know-the-same-node-states): the browser decodes exactly the node states the document serves", () => {
    expect([...NODE_STATES]).toEqual(
      servedDocument.components.schemas.NodeRailResource?.properties?.state?.enum
    );
  });

  it("decodes exactly the V3 run states the document serves", () => {
    expect([...RUN_STATES_V3]).toEqual(
      servedDocument.components.schemas.RunResourceV3?.properties?.state?.enum
    );
  });

  it("decodes exactly the fork lineage fields the V3 run and rail serve", () => {
    expect(Object.keys(runV3Schema.shape).sort()).toEqual(
      Object.keys(servedDocument.components.schemas.RunResourceV3?.properties ?? {}).sort()
    );
    expect(Object.keys(nodeRailEntrySchema.shape).sort()).toEqual(
      Object.keys(servedDocument.components.schemas.NodeRailResource?.properties ?? {}).sort()
    );
    expect(MAXIMUM_RUN_FORK_SUCCESSORS).toBe(
      servedDocument.components.schemas.RunResourceV3?.properties?.fork_successors?.maxItems
    );
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

  it("decodes exactly the effect confirmation sources the document serves", () => {
    expect([...EFFECT_CONFIRMATION_SOURCES]).toEqual(
      servedDocument.components.schemas.EffectReceiptResource?.properties?.confirmation_source
        ?.enum
    );
  });

  it("proves(the-cockpit-and-the-served-contract-know-the-same-cancel-reasons): decodes exactly the run-cancel reasons the document serves", () => {
    const served = servedDocument.components.schemas.RunCancellabilityResource;
    const reasonEnum = served?.properties?.reason?.anyOf?.find(
      (option) => option.enum !== undefined
    )?.enum;

    expect([...RUN_NOT_CANCELLABLE_REASONS]).toEqual(reasonEnum);
  });

  it("proves(the-cockpit-decodes-the-served-run-cancel-problems): mirrors exactly the run-cancel problems the document serves", () => {
    const servedRunCancelProblems = Object.values(servedDocument.components.schemas)
      .map((schema) => schema.properties?.type?.const)
      .filter(
        (constant): constant is string =>
          typeof constant === "string" && constant.startsWith(PROBLEM_TYPE_PREFIX)
      )
      .map((urn) => urn.slice(PROBLEM_TYPE_PREFIX.length))
      .filter((code) => code === "run-not-cancellable" || code.startsWith("run-cancellation-"))
      .sort();

    expect(servedRunCancelProblems).toEqual([
      "run-cancellation-command-conflict",
      "run-cancellation-overtaken-by-success",
      "run-not-cancellable"
    ]);
    for (const code of servedRunCancelProblems) {
      expect(problemDefinitions[code as keyof typeof problemDefinitions]).toBeDefined();
    }
  });

  it("mirrors exactly the run-fork problems the document serves", () => {
    const servedRunForkProblems = Object.values(servedDocument.components.schemas)
      .map((schema) => schema.properties?.type?.const)
      .filter(
        (constant): constant is string =>
          typeof constant === "string" && constant.startsWith(PROBLEM_TYPE_PREFIX)
      )
      .map((urn) => urn.slice(PROBLEM_TYPE_PREFIX.length))
      .filter((code) => code.startsWith("run-fork-"))
      .sort();

    expect(servedRunForkProblems).toEqual([
      "run-fork-command-conflict",
      "run-fork-loop-unsupported",
      "run-fork-node-missing",
      "run-fork-origin-not-terminal",
      "run-fork-prefix-not-reusable"
    ]);
    for (const code of servedRunForkProblems) {
      expect(problemDefinitions[code as keyof typeof problemDefinitions]).toBeDefined();
    }
  });

  it("mirrors the attention feed's per-run corruption frame", () => {
    const served = servedDocument.components.schemas.RunProjectionCorruptResource;
    expect(served?.properties?.event?.const).toBe("RUN_PROJECTION_CORRUPT");
    expect(served?.properties?.problem).toEqual({
      $ref: "#/components/schemas/ProblemDurableStateCorrupt"
    });
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

  it("decodes exactly the agent attempt states the document serves", () => {
    // The rail's attempt is where the served document spells this vocabulary,
    // and it is nullable there because a succeeded attempt carries no state.
    expect([...PUBLIC_ATTEMPT_STATES]).toEqual(
      servedDocument.components.schemas.NodeRailAttemptResource?.properties?.state
        ?.anyOf?.[0]?.enum
    );
  });

  it("decodes exactly the fields the agent-configuration listing serves", () => {
    const served = servedDocument.components.schemas.AgentConfigurationRevisionPageResource;

    expect(Object.keys(agentConfigurationRevisionPageSchema.shape).sort()).toEqual(
      Object.keys(served?.properties ?? {}).sort()
    );
    expect(
      Object.keys(agentConfigurationRevisionListItemObjectSchema.shape).sort()
    ).toEqual(
      Object.keys(
        servedDocument.components.schemas.AgentConfigurationRevisionListItemResource
          ?.properties ?? {}
      ).sort()
    );
  });

  it("decodes only the closed startability pair on a listed configuration", () => {
    const sample = {
      model: "sonnet",
      auth_profile_revision_hash: "a".repeat(64),
      executor_revision: "claude-subscription/v1",
      provider_id: "anthropic",
      auth_mode: "subscription" as const,
      requested_capability: "headless" as const,
      agent_configuration_revision_hash: "b".repeat(64),
      startable: false,
      structurally_startable: false,
      not_startable_reason: "agent-executor-binding-unavailable" as const,
      provider_probe_problem_code: null,
      provider_probe_observed_at: null
    };

    expect(
      agentConfigurationRevisionPageSchema.parse({
        items: [sample],
        next_after_revision_hash: null
      }).items
    ).toEqual([sample]);
    expect(() =>
      agentConfigurationRevisionPageSchema.parse({
        items: [{ ...sample, startable: true }],
        next_after_revision_hash: null
      })
    ).toThrow();
  });

  it("decodes exactly the fields the agent-definition listing serves", () => {
    expect(Object.keys(agentDefinitionRevisionPageSchema.shape).sort()).toEqual(
      Object.keys(
        servedDocument.components.schemas.AgentDefinitionRevisionPageResource
          ?.properties ?? {}
      ).sort()
    );
    expect(Object.keys(agentDefinitionRevisionListItemSchema.shape).sort()).toEqual(
      Object.keys(
        servedDocument.components.schemas.AgentDefinitionRevisionListItemResource
          ?.properties ?? {}
      ).sort()
    );
  });

  it("decodes the agent-definition detail up to the lengths the document serves", () => {
    const served = servedDocument.components.schemas.AgentDefinitionRevisionDetailResource;
    const systemPromptCharacters = (
      served?.properties?.system_prompt as { maxLength?: number } | undefined
    )?.maxLength;
    const declaredTools = (
      served?.properties?.tools?.anyOf as
        | Array<{ maxItems?: number; items?: { maxLength?: number } }>
        | undefined
    )?.find((option) => option.maxItems !== undefined);
    const toolCharacters = declaredTools?.items?.maxLength;
    const longestTool = "t".repeat(toolCharacters ?? 0);
    const detail = {
      agent_definition_revision_hash: "a".repeat(64),
      name: "scribe",
      description: "Writes what the stage needs.",
      model: null,
      system_prompt: "p".repeat(systemPromptCharacters ?? 0),
      tools: Array.from({ length: declaredTools?.maxItems ?? 0 }, () => longestTool)
    };

    expect(Object.keys(agentDefinitionRevisionDetailSchema.shape).sort()).toEqual(
      Object.keys(served?.properties ?? {}).sort()
    );
    expect(systemPromptCharacters).toBe(16_384);
    expect(toolCharacters).toBe(16_384);
    expect(declaredTools?.maxItems).toBe(128);
    expect(agentDefinitionRevisionDetailSchema.parse(detail)).toEqual(detail);
    expect(() =>
      agentDefinitionRevisionDetailSchema.parse({
        ...detail,
        system_prompt: `${detail.system_prompt}p`
      })
    ).toThrow();
    expect(() =>
      agentDefinitionRevisionDetailSchema.parse({
        ...detail,
        tools: [...detail.tools, longestTool]
      })
    ).toThrow();
    expect(() =>
      agentDefinitionRevisionDetailSchema.parse({
        ...detail,
        tools: [`${longestTool}t`]
      })
    ).toThrow();
  });

  it("refuses a listed agent carrying a field the row has no reader for", () => {
    const listed = {
      agent_definition_revision_hash: "a".repeat(64),
      name: "scribe",
      description: "Writes what the stage needs."
    };

    expect(agentDefinitionRevisionListItemSchema.parse(listed)).toEqual(listed);
    expect(() =>
      agentDefinitionRevisionListItemSchema.parse({ ...listed, model: "sonnet" })
    ).toThrow();
  });

  it("decodes exactly the fields the auth-profile listing serves", () => {
    const served = servedDocument.components.schemas.AuthProfileRevisionPageResource;

    expect(Object.keys(authProfileRevisionPageSchema.shape).sort()).toEqual(
      Object.keys(served?.properties ?? {}).sort()
    );
  });

  it("decodes exactly the node-detail fields the document serves", () => {
    expect(Object.keys(nodeDetailSchema.shape).sort()).toEqual(
      Object.keys(servedDocument.components.schemas.NodeDetailResource?.properties ?? {}).sort()
    );
  });

  it("decodes exactly the attempt-transcript events the document serves", () => {
    const transcript = servedDocument.components.schemas.AttemptTranscriptResource as {
      properties?: {
        events?: {
          items?: {
            discriminator?: { mapping?: Record<string, string> };
          };
        };
      };
    };
    const mapping = transcript.properties?.events?.items?.discriminator?.mapping ?? {};
    const decoderByEvent = {
      "tool-called": toolCalledEventSchema,
      "tool-returned": toolReturnedEventSchema,
      "assistant-turn": assistantTurnEventSchema,
      usage: usageEventSchema,
      "provider-terminal-refusal": providerTerminalRefusalEventSchema,
      "unrecognised-provider-output": unrecognisedProviderOutputEventSchema,
      "transcript-truncated": transcriptTruncatedEventSchema
    };
    const servedEvents = Object.entries(mapping).map(([event, ref]) => {
      const resourceName = ref.split("/").at(-1) ?? "";
      return {
        event,
        resourceName,
        constValue: servedDocument.components.schemas[resourceName]?.properties?.event?.const
      };
    });

    expect(servedEvents.map(({ event }) => event).sort()).toEqual(
      servedEvents.map(({ constValue }) => constValue).sort()
    );
    expect(Object.keys(attemptTranscriptSchema.shape)).toEqual(
      Object.keys(servedDocument.components.schemas.AttemptTranscriptResource?.properties ?? {})
    );
    expect(Object.keys(decoderByEvent).sort()).toEqual(Object.keys(mapping).sort());
    for (const { event, resourceName } of servedEvents) {
      expect(
        Object.keys(decoderByEvent[event as keyof typeof decoderByEvent].shape).sort()
      ).toEqual(
        Object.keys(servedDocument.components.schemas[resourceName]?.properties ?? {}).sort()
      );
    }

    const beforeMoments = { origin: "v1-before-moments" as const };
    const events = [
      {
        event: "tool-called" as const,
        name: "Read",
        arguments: "{}",
        redacted: false,
        moment: beforeMoments
      },
      {
        event: "tool-returned" as const,
        name: "Read",
        result: "ok",
        redacted: false,
        moment: beforeMoments
      },
      {
        event: "assistant-turn" as const,
        text: "done",
        redacted: false,
        moment: beforeMoments
      },
      {
        event: "usage" as const,
        input_tokens: 0,
        output_tokens: 0,
        cache_read_input_tokens: 0,
        cache_creation_input_tokens: 0,
        moment: beforeMoments
      },
      {
        event: "provider-terminal-refusal" as const,
        terminal_reason: "rate_limit_error",
        api_error_status: "429",
        text: "refused",
        redacted: false,
        moment: beforeMoments
      },
      {
        event: "unrecognised-provider-output" as const,
        text: "raw",
        redacted: true,
        moment: beforeMoments
      },
      {
        event: "transcript-truncated" as const,
        dropped_events: 1,
        moment: beforeMoments
      }
    ];
    expect(events.map((step) => step.event).sort()).toEqual(Object.keys(mapping).sort());
    expect(attemptTranscriptSchema.parse({ events })).toEqual({ events });
  });

  it("bounds transcript step strings to the length the document serves", () => {
    const stepFields = [
      ["ToolCalledEventResource", "name"],
      ["ToolCalledEventResource", "arguments"],
      ["ToolReturnedEventResource", "name"],
      ["ToolReturnedEventResource", "result"],
      ["AssistantTurnEventResource", "text"],
      ["UnrecognisedProviderOutputEventResource", "text"]
    ] as const;

    for (const [resource, field] of stepFields) {
      expect(
        (
          servedDocument.components.schemas[resource]?.properties?.[field] as
            | { maxLength?: number }
            | undefined
        )?.maxLength
      ).toBe(MAXIMUM_TRANSCRIPT_STEP_CHARACTERS);
    }
    expect(MAXIMUM_TRANSCRIPT_STEP_CHARACTERS).toBe(8_192);
  });
});
