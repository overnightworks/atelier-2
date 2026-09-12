import {
  defineConfig,
  type OpenApiDocument,
  type OpenApiParameterObject,
} from "orval";

import {
  collectSchemaNames,
  findRefs,
  synthesizeProblemUnion,
} from "./orval.transform";

/**
 * Orval's `input.filters` can only scope generation by OpenAPI tags, which
 * this document does not carry, so a transformer picks the roots instead:
 * one root per operation whose response body this file decodes, walked out
 * to its transitive `$ref`s. Request bodies stay out of every root here --
 * they belong to the hand-written facade (`CockpitApi`), which owns what it
 * sends, not what the wire hands back.
 */
interface OperationRoot {
  readonly path: string;
  readonly method: "get" | "post" | "put";
  readonly keptStatuses: readonly string[];
}

const HEALTH_OPERATION_PATH = "/atelier/api/v1/health";

const HEALTH_ROOTS: readonly OperationRoot[] = [
  { path: HEALTH_OPERATION_PATH, method: "get", keptStatuses: ["200"] },
];

const WORKFLOW_AND_CATALOG_ROOTS: readonly OperationRoot[] = [
  {
    path: "/atelier/api/v1/workflow-revisions",
    method: "get",
    keptStatuses: ["200"],
  },
  {
    path: "/atelier/api/v1/workflow-revisions/{workflow_revision_hash}",
    method: "get",
    keptStatuses: ["200"],
  },
  {
    path: "/atelier/api/v1/catalog-lineages",
    method: "post",
    keptStatuses: ["201"],
  },
  {
    path: "/atelier/api/v1/catalog-lineages/{lineage_id}/members",
    method: "post",
    keptStatuses: ["201"],
  },
  {
    path: "/atelier/api/v1/catalog-revisions/by-name/{kind}/{name}",
    method: "get",
    keptStatuses: ["200"],
  },
];

const PROJECTS_SOURCES_AND_MODELS_ROOTS: readonly OperationRoot[] = [
  { path: "/atelier/api/v1/projects", method: "get", keptStatuses: ["200"] },
  {
    path: "/atelier/api/v1/projects/{public_project_reference}/source-connection",
    method: "get",
    keptStatuses: ["200"],
  },
  {
    path: "/atelier/api/v1/projects/{public_project_reference}/sources",
    method: "get",
    keptStatuses: ["200"],
  },
  {
    path: "/atelier/api/v1/projects/{public_project_reference}/sources",
    method: "post",
    keptStatuses: ["201"],
  },
  {
    path: "/atelier/api/v1/projects/{public_project_reference}/sources/{public_source_reference}/token",
    method: "put",
    keptStatuses: ["200"],
  },
  {
    path: "/atelier/api/v1/model-registries/{provider_id}",
    method: "get",
    keptStatuses: ["200"],
  },
  {
    path: "/atelier/api/v1/model-registries/{provider_id}",
    method: "put",
    keptStatuses: ["200", "201"],
  },
  {
    path: "/atelier/api/v1/model-registries/{provider_id}/validations",
    method: "post",
    keptStatuses: ["200", "201"],
  },
  {
    path: "/atelier/api/v1/projects/{public_project_reference}/model-defaults",
    method: "get",
    keptStatuses: ["200"],
  },
  {
    path: "/atelier/api/v1/projects/{public_project_reference}/model-defaults",
    method: "put",
    keptStatuses: ["200", "201"],
  },
  {
    path: "/atelier/api/v1/projects/{public_project_reference}/model-resolution",
    method: "post",
    keptStatuses: ["200"],
  },
];

const RUNS_RAIL_AND_NODES_ROOTS: readonly OperationRoot[] = [
  { path: "/atelier/api/v1/runs", method: "get", keptStatuses: ["200"] },
  {
    path: "/atelier/api/v1/runs",
    method: "post",
    keptStatuses: ["200", "201"],
  },
  {
    path: "/atelier/api/v1/runs/{public_ref}",
    method: "get",
    keptStatuses: ["200"],
  },
  {
    path: "/atelier/api/v1/runs/{public_ref}/forks",
    method: "post",
    keptStatuses: ["200", "201"],
  },
  {
    path: "/atelier/api/v1/runs/{public_ref}/answers",
    method: "post",
    keptStatuses: ["202"],
  },
  {
    path: "/atelier/api/v1/runs/{public_ref}/cancellations",
    method: "post",
    keptStatuses: ["200", "202"],
  },
  {
    path: "/atelier/api/v1/runs/{public_ref}/nodes/{node_id}",
    method: "get",
    keptStatuses: ["200"],
  },
];

const AUTH_AGENT_AND_QUEUE_ROOTS: readonly OperationRoot[] = [
  {
    path: "/atelier/api/v1/auth-profile-revisions",
    method: "get",
    keptStatuses: ["200"],
  },
  {
    path: "/atelier/api/v1/auth-profile-revisions",
    method: "post",
    keptStatuses: ["200", "201"],
  },
  {
    path: "/atelier/api/v1/agent-configuration-revisions",
    method: "get",
    keptStatuses: ["200"],
  },
  {
    path: "/atelier/api/v1/agent-configuration-revisions",
    method: "post",
    keptStatuses: ["200", "201"],
  },
  {
    path: "/atelier/api/v1/agent-definition-revisions",
    method: "get",
    keptStatuses: ["200"],
  },
  {
    path: "/atelier/api/v1/agent-definition-revisions",
    method: "post",
    keptStatuses: ["200", "201"],
  },
  {
    path: "/atelier/api/v1/agent-definition-revisions/{agent_definition_revision_hash}",
    method: "get",
    keptStatuses: ["200"],
  },
  {
    path: "/atelier/api/v1/queue-items",
    method: "get",
    keptStatuses: ["200"],
  },
];

function restrictToOperations(roots: readonly OperationRoot[]) {
  return function restrict(spec: OpenApiDocument): OpenApiDocument {
    const schemas = spec.components?.schemas ?? {};
    const keptSchemaNames = new Set<string>();
    const keptPaths: NonNullable<OpenApiDocument["paths"]> = {};
    for (const root of roots) {
      const operation = spec.paths?.[root.path]?.[root.method];
      if (!operation) {
        throw new Error(
          `orval.config.ts expected a ${root.method.toUpperCase()} ${root.path} operation in the frozen document`,
        );
      }
      const keptResponses = Object.fromEntries(
        root.keptStatuses.map((status) => {
          const response = operation.responses?.[status];
          if (!response) {
            throw new Error(
              `orval.config.ts expected a ${status} response on ${root.method.toUpperCase()} ${root.path} in the frozen document`,
            );
          }
          return [status, response];
        }),
      );
      collectSchemaNames(findRefs(keptResponses), schemas, keptSchemaNames);
      // The request body drops out entirely: this file decodes response
      // bodies only, and a request-body `$ref` must not pull an otherwise
      // unrelated schema into the reusable-schema output. A path parameter
      // stays, because OpenAPI validation requires one declared per `{...}`
      // placeholder in the path -- but its schema is flattened to a plain
      // string, since `generate.param` is off and no code reads its type.
      const pathParametersOnly = (operation.parameters ?? [])
        .filter(
          (parameter): parameter is OpenApiParameterObject =>
            !("$ref" in parameter) && parameter.in === "path",
        )
        .map((parameter) => ({ ...parameter, schema: { type: "string" as const } }));
      keptPaths[root.path] = {
        ...keptPaths[root.path],
        [root.method]: {
          operationId: operation.operationId,
          parameters: pathParametersOnly,
          responses: keptResponses,
        },
      };
    }
    return {
      ...spec,
      paths: keptPaths,
      components: {
        ...spec.components,
        schemas: Object.fromEntries(
          Object.entries(schemas).filter(([name]) => keptSchemaNames.has(name)),
        ),
      },
    } as OpenApiDocument;
  };
}

// `client:"zod"` has no HTTP operations of its own, so a per-operation target
// file would carry no caller while `CockpitApi` stays hand-written -- this
// override empties its would-be operation content and keeps only the named
// reusable schemas each project's transformer selected.
const ZOD_SCHEMAS_ONLY = {
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
} as const;

// The synthesized problem root carries a `discriminator`, so this project
// alone asks orval to lower it to `zod.discriminatedUnion` instead of a
// plain `zod.union` (`@orval/zod`'s `generateDiscriminatedUnion` reads that
// `discriminator.propertyName` and each member's literal `type`).
const PROBLEM_SCHEMAS_ONLY = {
  zod: { ...ZOD_SCHEMAS_ONLY.zod, generateDiscriminatedUnion: true },
} as const;

export default defineConfig({
  cockpit: {
    input: {
      target: "../tests/api/openapi_frozen.json",
      override: { transformer: restrictToOperations(HEALTH_ROOTS) },
    },
    output: {
      // A single named file the facade imports directly.
      target: "./src/api/generated/health.zod.ts",
      mode: "single",
      client: "zod",
      override: ZOD_SCHEMAS_ONLY,
    },
  },
  workflowAndCatalog: {
    input: {
      target: "../tests/api/openapi_frozen.json",
      override: { transformer: restrictToOperations(WORKFLOW_AND_CATALOG_ROOTS) },
    },
    output: {
      target: "./src/api/generated/workflowAndCatalog.zod.ts",
      mode: "single",
      client: "zod",
      override: ZOD_SCHEMAS_ONLY,
    },
  },
  projectsSourcesAndModels: {
    input: {
      target: "../tests/api/openapi_frozen.json",
      override: {
        transformer: restrictToOperations(PROJECTS_SOURCES_AND_MODELS_ROOTS),
      },
    },
    output: {
      target: "./src/api/generated/projectsSourcesAndModels.zod.ts",
      mode: "single",
      client: "zod",
      override: ZOD_SCHEMAS_ONLY,
    },
  },
  authAgentAndQueue: {
    input: {
      target: "../tests/api/openapi_frozen.json",
      override: { transformer: restrictToOperations(AUTH_AGENT_AND_QUEUE_ROOTS) },
    },
    output: {
      target: "./src/api/generated/authAgentAndQueue.zod.ts",
      mode: "single",
      client: "zod",
      override: ZOD_SCHEMAS_ONLY,
    },
  },
  runsRailAndNodes: {
    input: {
      target: "../tests/api/openapi_frozen.json",
      override: { transformer: restrictToOperations(RUNS_RAIL_AND_NODES_ROOTS) },
    },
    output: {
      target: "./src/api/generated/runsRailAndNodes.zod.ts",
      mode: "single",
      client: "zod",
      override: ZOD_SCHEMAS_ONLY,
    },
  },
  problem: {
    input: {
      target: "../tests/api/openapi_frozen.json",
      override: { transformer: synthesizeProblemUnion },
    },
    output: {
      target: "./src/api/generated/problem.zod.ts",
      mode: "single",
      client: "zod",
      override: PROBLEM_SCHEMAS_ONLY,
    },
  },
});
