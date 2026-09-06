import { describe, expect, it } from "vitest";

import {
  classifyStartOrderSchema,
  WORK_ITEM_ORDER_SCHEMA_REVISION,
  summarizeOrderSchema,
  typeLabel
} from "../../src/lib/orderSchema";

describe("summarizing an order's schema for a human", () => {
  it("lists every declared field with its type and whether it is required", () => {
    const document = {
      type: "object",
      required: ["message"],
      properties: {
        message: { type: "string", minLength: 1 },
        tone: { type: "string" }
      }
    };
    const summary = summarizeOrderSchema(document);
    expect(summary.fields).toEqual([
      { name: "message", types: ["string"], required: true },
      { name: "tone", types: ["string"], required: false }
    ]);
  });

  it("names the one field a human editor may fill as plain text", () => {
    const document = {
      type: "object",
      required: ["message"],
      properties: { message: { type: "string" } }
    };
    expect(summarizeOrderSchema(document).singleRequiredStringField).toBe("message");
  });

  it("names no human field when a declared type is not a bare object", () => {
    expect(
      summarizeOrderSchema({
        type: "array",
        required: ["message"],
        properties: { message: { type: "string" } }
      }).singleRequiredStringField
    ).toBeNull();
  });

  it("names no human field when more than one field is required", () => {
    const document = {
      type: "object",
      required: ["message", "prior_transcript", "dropped_oldest_messages"],
      properties: {
        message: { type: "string", minLength: 1 },
        prior_transcript: { type: "array" },
        dropped_oldest_messages: { type: "integer" }
      }
    };
    const summary = summarizeOrderSchema(document);
    expect(summary.singleRequiredStringField).toBeNull();
    expect(summary.fields).toHaveLength(3);
  });

  it("names no human field when the one required field is not a string", () => {
    const document = {
      type: "object",
      required: ["portions"],
      properties: { portions: { type: "integer" } }
    };
    expect(summarizeOrderSchema(document).singleRequiredStringField).toBeNull();
  });

  it("reads a schema declaring no properties or required fields as empty", () => {
    expect(summarizeOrderSchema(true)).toEqual({ fields: [], singleRequiredStringField: null });
    expect(summarizeOrderSchema({})).toEqual({ fields: [], singleRequiredStringField: null });
  });
});

describe("typeLabel", () => {
  it("joins a union of declared types and falls back to 'any'", () => {
    expect(typeLabel(["string"])).toBe("string");
    expect(typeLabel(["string", "null"])).toBe("string or null");
    expect(typeLabel(null)).toBe("any");
  });
});

describe("classifying a schema for the start sheet", () => {
  it("recognizes the canonical work-item document and ordinary object fields", () => {
    expect(
      classifyStartOrderSchema({
        title: "work item",
        type: "object",
        additionalProperties: false,
        required: [
          "body",
          "change_marker",
          "digest",
          "kind",
          "observed_at",
          "reference",
          "scope"
        ],
        properties: {
          body: { type: "string" },
          change_marker: { type: "string" },
          digest: { type: "string" },
          kind: { type: "string" },
          observed_at: { type: "string" },
          reference: { type: "string" },
          scope: { type: "array" }
        }
      }, WORK_ITEM_ORDER_SCHEMA_REVISION)
    ).toEqual({ kind: "work_item" });
    expect(
      classifyStartOrderSchema({
        type: "object",
        properties: { portions: { type: "integer" } },
        required: ["portions"]
      }, "schema-portions")
    ).toEqual({ kind: "inline_object" });
  });

  it("refuses a noncanonical work-item lookalike before it could make a work-item start", () => {
    expect(
      classifyStartOrderSchema({
        title: "work item",
        type: "object",
        additionalProperties: false,
        required: [
          "body",
          "change_marker",
          "digest",
          "kind",
          "observed_at",
          "reference",
          "scope"
        ],
        properties: {
          body: { type: "string" },
          change_marker: { type: "string" },
          digest: { type: "string" },
          kind: { type: "string" },
          observed_at: { type: "string" },
          reference: { type: "string" },
          scope: { type: "array" }
        }
      }, "f".repeat(64))
    ).toMatchObject({ kind: "unsupported" });
  });

  it.each([true, { type: "boolean" }, { type: "array" }])(
    "refuses an unsupported start shape before it can be serialized",
    (document) => {
      expect(classifyStartOrderSchema(document, "schema-other").kind).toBe("unsupported");
    }
  );

  it("classifies a top-level object with a field the form cannot encode as raw_object, not unsupported (#1130)", () => {
    expect(
      classifyStartOrderSchema(
        { type: "object", properties: { nested: { type: "object" } } },
        "schema-other"
      )
    ).toEqual({ kind: "raw_object" });
  });

  it("classifies the accepted_sentences-shaped schema (an array field) as raw_object", () => {
    expect(
      classifyStartOrderSchema(
        {
          type: "object",
          additionalProperties: false,
          required: ["sentences"],
          properties: {
            sentences: {
              type: "array",
              items: {
                type: "object",
                additionalProperties: false,
                required: ["id", "sentence"],
                properties: {
                  id: { type: "string" },
                  sentence: { type: "string" }
                }
              }
            }
          }
        },
        "schema-accepted-sentences"
      )
    ).toEqual({ kind: "raw_object" });
  });

  it.each([{ type: "string" }, { type: "string", minLength: 1 }])(
    "recognizes a string schema, minLength or not, as a string order (#438 A5)",
    (document) => {
      expect(classifyStartOrderSchema(document, "schema-string")).toEqual({ kind: "string" });
    }
  );

  it("names a way out on every refusal (#438 Zeile 11)", () => {
    expect(classifyStartOrderSchema({ type: "array" }, "schema-other")).toMatchObject({
      kind: "unsupported",
      reason: expect.stringContaining("CLI or the HTTP API")
    });
  });
});
