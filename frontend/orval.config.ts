import { defineConfig, type OpenApiDocument } from "orval";

/**
 * Orval's `input.filters` can only scope generation by OpenAPI tags, which
 * this document does not carry, so a transformer picks the roots instead.
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
      // A single named file the facade imports directly: `client:"zod"` has no
      // HTTP operations of its own, so a per-operation target file would carry
      // no caller while `CockpitApi` stays hand-written (`override.zod.generate`
      // below already empties its would-be content).
      target: "./src/api/generated/health.zod.ts",
      mode: "single",
      client: "zod",
      override: {
        zod: {
          generateReusableSchemas: true,
          strict: { body: true, response: true },
          generate: {
            param: false,
            query: false,
            header: false,
            body: false,
            response: false,
          },
        },
      },
    },
  },
});
