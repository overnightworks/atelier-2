import type { OpenApiDocument } from "orval";

/**
 * Deterministic manipulation of the frozen input document, shared by every
 * `orval.config.ts` project: walking `$ref`s to find a root's transitive
 * schema closure, and synthesizing the problem-family root from the document
 * itself rather than a hand-maintained aggregate.
 */

export function findRefs(value: unknown): string[] {
  if (Array.isArray(value)) return value.flatMap(findRefs);
  if (value && typeof value === "object") {
    const record = value as Record<string, unknown>;
    const ownRef = typeof record.$ref === "string" ? [record.$ref] : [];
    return ownRef.concat(Object.values(record).flatMap(findRefs));
  }
  return [];
}

export function collectSchemaNames(
  refs: string[],
  schemas: Record<string, unknown>,
  collected = new Set<string>(),
): Set<string> {
  for (const ref of refs) {
    const match = /^#\/components\/schemas\/(.+)$/.exec(ref);
    if (!match || collected.has(match[1])) continue;
    collected.add(match[1]);
    collectSchemaNames(findRefs(schemas[match[1]]), schemas, collected);
  }
  return collected;
}

const PROBLEM_SCHEMA_PREFIX = "Problem";
const PROBLEM_UNION_DISCRIMINATOR_PROPERTY = "type";

/** The name orval assigns the synthesized problem-union root schema and export. */
const PROBLEM_UNION_SCHEMA_NAME = "AnyProblem";

/**
 * Every `Problem*` component of the served contract is a self-contained
 * RFC 9457 body: no aggregate the server hands the browser names them
 * together. This synthesizes that root once, from the document's own
 * component names sorted alphabetically, so a new server-side problem
 * publishes into the union without anyone editing this file. The root
 * carries a `discriminator` naming the `type` field so
 * `output.override.zod.generateDiscriminatedUnion` on the problem project
 * lowers it straight to `zod.discriminatedUnion`.
 */
export function synthesizeProblemUnion(spec: OpenApiDocument): OpenApiDocument {
  const schemas = spec.components?.schemas ?? {};
  const problemNames = Object.keys(schemas)
    .filter((name) => name.startsWith(PROBLEM_SCHEMA_PREFIX))
    .sort();
  if (problemNames.length === 0) {
    throw new Error(
      "orval.transform.ts expected at least one Problem* component in the frozen document",
    );
  }
  const problemRefs = problemNames.map((name) => `#/components/schemas/${name}`);
  const keptSchemaNames = collectSchemaNames(problemRefs, schemas);
  return {
    ...spec,
    paths: {},
    components: {
      ...spec.components,
      schemas: {
        ...Object.fromEntries(
          Object.entries(schemas).filter(([name]) => keptSchemaNames.has(name)),
        ),
        [PROBLEM_UNION_SCHEMA_NAME]: {
          oneOf: problemRefs.map((ref) => ({ $ref: ref })),
          discriminator: { propertyName: PROBLEM_UNION_DISCRIMINATOR_PROPERTY },
        },
      },
    },
  } as OpenApiDocument;
}
