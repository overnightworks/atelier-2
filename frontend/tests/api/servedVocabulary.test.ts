import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import {
  EFFECT_CONFIRMATION_SOURCES,
  MAXIMUM_TRANSCRIPT_STEP_CHARACTERS,
  RUN_NOT_CANCELLABLE_REASONS,
  problemDefinitions,
  attemptTranscriptSchema,
  assistantTurnEventSchema,
  providerTerminalRefusalEventSchema,
  toolCalledEventSchema,
  toolReturnedEventSchema,
  transcriptTruncatedEventSchema,
  unrecognisedProviderOutputEventSchema,
  usageEventSchema,
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
          }
        >;
      }
    >;
  };
};

const PROBLEM_TYPE_PREFIX = "urn:atelier2:problem:v1:";

describe("the served vocabulary", () => {
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
