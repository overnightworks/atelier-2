import { defineConfig, type OpenApiDocument } from "orval";

/**
 * Restricts the frozen document to the roots this slice's client actually
 * calls (#1317): the `GET /health` operation and the component schemas its
 * 200 response transitively references. Every generated-schema slice narrows
 * this the same way for its own roots; Orval's own `input.filters` cannot
 * prune paths without OpenAPI tags, which the served document does not
 * carry, so the transformer walks `$ref`s itself.
 */
const HEALTH_OPERATION_PATH = "/atelier/api/v1/health";

function findRefs(value: unknown): string[] {
  if (Array.isArray(value)) return value.flatMap(findRefs);
  if (value && typeof value === "object") {
    const record = value as Record<string, unknown>;
    const ownRef = typeof record.$ref === "string" ? [record.$ref] : [];
    return ownRef.concat(Object.values(record).flatMap(findRefs));
  }
  return [];
}

function collectSchemaNames(
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

function restrictToHealthOperation(spec: OpenApiDocument): OpenApiDocument {
  const healthPathItem = spec.paths?.[HEALTH_OPERATION_PATH];
  if (!healthPathItem?.get) {
    throw new Error(
      `orval.config.ts expected a GET ${HEALTH_OPERATION_PATH} operation in the frozen document`,
    );
  }
  const okResponse = healthPathItem.get.responses?.["200"];
  const schemas = spec.components?.schemas ?? {};
  const keptSchemaNames = collectSchemaNames(findRefs(okResponse), schemas);
  return {
    ...spec,
    paths: {
      [HEALTH_OPERATION_PATH]: {
        ...healthPathItem,
        get: { ...healthPathItem.get, responses: { "200": okResponse } },
      },
    },
    components: {
      ...spec.components,
      schemas: Object.fromEntries(
        Object.entries(schemas).filter(([name]) => keptSchemaNames.has(name)),
      ),
    },
  } as OpenApiDocument;
}

export default defineConfig({
  cockpit: {
    input: {
      target: "../tests/api/openapi_frozen.json",
      override: { transformer: restrictToHealthOperation },
    },
    output: {
      target: "./src/api/generated",
      schemas: { path: "./src/api/generated", type: "zod", routes: { default: "model" } },
      mode: "split",
      client: "zod",
      indexFiles: false,
      clean: true,
      override: {
        zod: {
          generateReusableSchemas: true,
          strict: { body: true, response: true },
        },
      },
    },
  },
});
