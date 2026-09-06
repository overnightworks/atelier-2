import type { JsonSchemaDocument } from "../api/client";

/** One field an order's schema names, read only far enough to summarize it. */
export interface OrderSchemaField {
  readonly name: string;
  readonly types: readonly string[] | null;
  readonly required: boolean;
}

export interface OrderSchemaSummary {
  readonly fields: readonly OrderSchemaField[];
  /**
   * The one field a human editor may ask for as plain text: present only when
   * the schema requires exactly one field, and that field is declared as a
   * bare string. Every other shape -- more than one required field, no
   * declared type, a non-string type -- keeps the JSON editor, because
   * wrapping the typed text into an object would either lose the other
   * required fields or guess a type this module was not told.
   */
  readonly singleRequiredStringField: string | null;
}

/** A schema document, read once, paired with the human summary read from it. */
export interface OrderSchemaResource {
  readonly summary: OrderSchemaSummary;
  readonly document: JsonSchemaDocument;
}

/** The start sheet's one honest rendering for a published V3 order schema. */
export type StartOrderSchemaShape =
  | { readonly kind: "work_item" }
  | { readonly kind: "string" }
  | { readonly kind: "inline_object" }
  | { readonly kind: "raw_object" }
  | { readonly kind: "unsupported"; readonly reason: string };

/**
 * Every refusal names a way out (#438 Zeile 11): the CLI and the HTTP API
 * accept any order this door cannot render, through the same publish-as-
 * artifact start every door now shares.
 */
const UNSUPPORTED_ORDER_WAY_OUT =
  "Start this workflow through the CLI or the HTTP API instead.";

/**
 * The backend only accepts an observed work item beneath this published
 * revision. The workflow wire already carries that pin, so the start sheet
 * mirrors the server's discriminator instead of inferring it from field names.
 */
export const WORK_ITEM_ORDER_SCHEMA_REVISION =
  "0bffb19e43509cb4cb446dd33c1d70b0d19f8a9a65cbe270d382edc2860d2921";

/**
 * What a person reads for a schema this door cannot render at all -- a
 * scalar other than a string (boolean, number, null) or an array. Exported
 * so the surfaces that assert this refusal (unit and end-to-end) read it
 * from its one owner instead of retyping it.
 */
export const SCALAR_OR_ARRAY_ORDER_UNSUPPORTED_REASON =
  "This order's schema is not a string, an object, or a work item. " + UNSUPPORTED_ORDER_WAY_OUT;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** The `type` keyword's own declared names, single or a union -- never guessed. */
function declaredTypes(subschema: unknown): readonly string[] | null {
  if (!isRecord(subschema)) return null;
  const declared = subschema.type;
  if (typeof declared === "string") return [declared];
  if (Array.isArray(declared) && declared.every((entry) => typeof entry === "string")) {
    return declared as string[];
  }
  return null;
}

function declaredProperties(document: Record<string, unknown>): Record<string, unknown> {
  return isRecord(document.properties) ? document.properties : {};
}

function declaredRequired(document: Record<string, unknown>): readonly string[] {
  return Array.isArray(document.required)
    ? document.required.filter((entry): entry is string => typeof entry === "string")
    : [];
}

const WORK_ITEM_FIELDS = [
  "body",
  "change_marker",
  "digest",
  "kind",
  "observed_at",
  "reference"
] as const;

/**
 * The tracker order's published schema has one closed, neutral shape. The
 * browser checks the canonical shape as a second guard after its revision pin.
 */
function isWorkItemOrderSchema(document: Record<string, unknown>): boolean {
  const properties = declaredProperties(document);
  const required = declaredRequired(document);
  return (
    document.title === "work item" &&
    fieldTypesAreExactly(declaredTypes(document), "object") &&
    document.additionalProperties === false &&
    required.length === WORK_ITEM_FIELDS.length &&
    WORK_ITEM_FIELDS.every((field) => required.includes(field)) &&
    Object.keys(properties).length === WORK_ITEM_FIELDS.length &&
    WORK_ITEM_FIELDS.every((field) => field in properties)
  );
}

/**
 * A start never manufactures an object for a scalar or array schema. It can
 * render the canonical tracker picker and ordinary object fields; a top-level
 * `object` schema with a field this form cannot type -- an array, a bare
 * `enum`, a nested object -- still gets a way in, through Raw JSON alone
 * (`raw_object`), rather than the refusal a shape outside `object`/`string`/
 * `work_item` still gets.
 */
export function classifyStartOrderSchema(
  document: JsonSchemaDocument,
  schemaRevision: string
): StartOrderSchemaShape {
  if (!isRecord(document)) {
    return {
      kind: "unsupported",
      reason: `This order's schema is not one the start sheet can read. ${UNSUPPORTED_ORDER_WAY_OUT}`
    };
  }
  if (isWorkItemOrderSchema(document)) {
    if (schemaRevision === WORK_ITEM_ORDER_SCHEMA_REVISION) return { kind: "work_item" };
    return {
      kind: "unsupported",
      reason:
        "This work-item-shaped order does not use the canonical work-item schema. " +
        UNSUPPORTED_ORDER_WAY_OUT
    };
  }
  if (fieldTypesAreExactly(declaredTypes(document), "string")) {
    return { kind: "string" };
  }
  if (!fieldTypesAreExactly(declaredTypes(document), "object")) {
    return { kind: "unsupported", reason: SCALAR_OR_ARRAY_ORDER_UNSUPPORTED_REASON };
  }
  const properties = declaredProperties(document);
  const required = declaredRequired(document);
  const fieldNames = [...new Set([...Object.keys(properties), ...required])];
  const supported = new Set(["boolean", "number", "integer", "string"]);
  const hasUnencodableField = fieldNames.some((name) => {
    const types = declaredTypes(properties[name]);
    return types === null || types.length !== 1 || !supported.has(types[0] ?? "");
  });
  return { kind: hasUnencodableField ? "raw_object" : "inline_object" };
}

/**
 * Every field a schema document names, for a person who cannot read JSON
 * Schema. This reads only `type`, `properties` and `required` at the
 * document's own top level -- the same three facts the door already shows in
 * `atelier2.contracts.schemas_v3`'s vocabulary, never the full Draft 2020-12
 * evaluation that module alone owns.
 */
export function summarizeOrderSchema(document: JsonSchemaDocument): OrderSchemaSummary {
  if (!isRecord(document)) return { fields: [], singleRequiredStringField: null };
  const properties = declaredProperties(document);
  const required = declaredRequired(document);
  const names = [...new Set([...Object.keys(properties), ...required])];
  const fields = names.map((name) => ({
    name,
    types: declaredTypes(properties[name]),
    required: required.includes(name)
  }));
  const topLevelTypes = declaredTypes(document);
  const [onlyRequired] = required;
  const singleRequiredStringField =
    required.length === 1 &&
    onlyRequired !== undefined &&
    (topLevelTypes === null || fieldTypesAreExactly(topLevelTypes, "object")) &&
    fieldTypesAreExactly(declaredTypes(properties[onlyRequired]), "string")
      ? onlyRequired
      : null;
  return { fields, singleRequiredStringField };
}

function fieldTypesAreExactly(types: readonly string[] | null, only: string): boolean {
  return types !== null && types.length === 1 && types[0] === only;
}

/** The words a summarized field's declared type reads as, or "any" when none is named. */
export function typeLabel(types: readonly string[] | null): string {
  return types === null || types.length === 0 ? "any" : types.join(" or ");
}
