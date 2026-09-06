import { z } from "zod";

import { HealthResource } from "./generated/health.zod";
import {
  reportConnectionLost,
  reportConnectionRestored,
} from "../lib/connectionState";
import type {
  CancelMutation,
  PublishMutation,
  StartMutation,
  WaitMutation,
} from "../lib/mutationJournal";

const sha256 = z.string().regex(/^[0-9a-f]{64}$/);
const recordedAtStamp = z
  .string()
  .regex(/^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$/);
const standardBase64 = z
  .string()
  .refine(
    isCanonicalStandardBase64,
    "base64 must use the canonical standard alphabet and padding",
  );
const publicRunReference = z
  .string()
  .refine(
    (value) => decodePublicRunReference(value) !== null,
    "public run reference must contain canonical unpadded base64url UTF-8",
  );
// The browser validates the public wire identity but never decodes it into the
// internal ProjectId. servedVocabulary pins this bound and shape to OpenAPI.
const publicProjectReference = z
  .string()
  .regex(/^project1\.[A-Za-z0-9_-]+$/)
  .max(5_471);
const eventCursor = z
  .string()
  .refine(
    (value) => parseEventCursor(value) !== null,
    "event cursor must contain a canonical run reference and safe positive sequence",
  );
const safeInteger = z
  .number()
  .refine(Number.isSafeInteger, "integer must be exactly representable");
const nonnegativeSafeInteger = safeInteger.refine((value) => value >= 0);
const positiveSafeInteger = safeInteger.refine((value) => value > 0);
const invalidFieldSchema = z
  .object({ path: z.string().min(1), reason: z.string().min(1) })
  .strict();

/**
 * A published V3 revision says what it is and whether this build runs it.
 *
 * `node_previews` is an excerpt — id, kind, role, instruction start, and the
 * authored `depends_on` edges — not the authored node. The browser must not
 * parse `document_base64` to learn the same facts. `orders` is the same class
 * of answer for the material a start must supply: name plus the author's own
 * `schema: {ref, revision}` hull, never the schema bytes.
 */
const workflowNodePreviewSchema = z
  .object({
    id: z.string().min(1),
    kind: z.enum(["agent", "deterministic", "wait", "subworkflow", "action"]),
    role: z.string().min(1).max(1_024).nullable(),
    instruction_start: z.string().min(1).max(120).nullable(),
    depends_on: z.array(z.string().min(1)),
  })
  .strict();

export { workflowNodePreviewSchema };

const workflowDeclaredSchemaSchema = z
  .object({
    ref: z.string().min(1),
    revision: z.string().min(1),
  })
  .strict();

const workflowDeclaredOrderSchema = z
  .object({
    name: z.string().min(1),
    schema: workflowDeclaredSchemaSchema,
  })
  .strict();

export { workflowDeclaredOrderSchema, workflowDeclaredSchemaSchema };

/**
 * The published bytes a `schema` revision pins, read only far enough to
 * summarize an order for a human -- this is not a JSON Schema evaluator, and
 * the browser must not pretend to be one. `atelier2.contracts.schemas_v3` is
 * the one place that actually enforces the closed Draft 2020-12 profile; this
 * type only says a schema document is JSON's own two possible schema shapes,
 * a boolean or an object, and leaves every keyword's value unconstrained.
 */
const jsonSchemaDocumentSchema = z.union([
  z.boolean(),
  z.record(z.string(), z.unknown()),
]);

export type JsonSchemaDocument = z.infer<typeof jsonSchemaDocumentSchema>;

/**
 * One waiting node's answer schema, classified as far as the server's excerpt
 * may without evaluating it. `kind` is `boolean` only where the schema's own
 * top level names `type: boolean`, `enum` only where it names `enum` (with
 * `values` the author's own members), `string` only where it names
 * `type: string` and no `enum` -- and `free` for every other shape, including
 * a schema the server's own excerpt has not yet resolved.
 *
 * `string_typed` is the one fact a composer needs to send `values` back the
 * way the door reads them: true names a schema whose own top level is
 * `type: string` (every `string` kind, and an `enum` that also names
 * `type: string`), the one shape whose door
 * (`schemas_v3.instance_for_schema`) reads an answer's raw UTF-8 text as the
 * value directly -- so `values` there already carries each member's raw
 * text, sent back verbatim (`waitAnswer.ts`, #1091 PR #1108 finding 1).
 * Every other `enum`, and `boolean`/`free`, carry `string_typed: false` and
 * `values` (where present) stay the JSON-encoded text they always were.
 */
const waitAnswerSchemaV3Schema = z
  .object({
    node_id: z.string().min(1),
    schema: workflowDeclaredSchemaSchema,
    kind: z.enum(["boolean", "enum", "string", "free"]),
    string_typed: z.boolean(),
    values: z.array(z.string()).nullable(),
  })
  .strict()
  .superRefine((entry, context) => {
    if ((entry.kind === "enum") !== (entry.values !== null)) {
      context.addIssue({
        code: "custom",
        message: "values names the enum's own members, and only those",
      });
    }
  });

export { waitAnswerSchemaV3Schema };

/**
 * The node and verdict that close a loop's round early, when the document
 * names one. Absent where the document declares no earlier exit at all.
 */
const workflowLoopVerdictSchema = z
  .object({
    node: z.string().min(1),
    verdict: z.enum(["accepted", "revise"]),
  })
  .strict();

/**
 * One declared loop of a published V3 revision: its body and its bound.
 *
 * `member_node_ids` names the loop's one-line body by the same ids
 * `node_previews` already carries, never the nodes a second time.
 */
const workflowLoopSchema = z
  .object({
    id: z.string().min(1),
    member_node_ids: z.array(z.string().min(1)).min(1),
    maximum_rounds: z.number().int().positive(),
    repeat_while: workflowLoopVerdictSchema.nullable(),
  })
  .strict();

const workflowGraphV3Schema = z
  .object({
    workflow_format_version: z.literal(3),
    executable: z.boolean(),
    not_executable_reason: z.string().nullable(),
    node_count: z.number().int().positive(),
    agent_roles: z.array(z.string().min(1)).max(100),
    orders: z.array(workflowDeclaredOrderSchema),
    wait_answer_schemas: z.array(waitAnswerSchemaV3Schema),
    node_previews: z.array(workflowNodePreviewSchema),
    loops: z.array(workflowLoopSchema),
    name: z.string().min(1),
    description: z.string().nullable(),
  })
  .strict();

/**
 * Where a revision's bytes first entered the catalog from, as it was then --
 * mirrors `WorkflowRevisionProvenanceResource`. The source travels as its
 * public `source1.` reference, never the durable id the store keeps, and
 * every field is an intake-time fact: where that source points *now* is
 * absent on purpose (see the Python resource's docstring).
 */
const workflowRevisionProvenanceSchema = z
  .object({
    source: z.string().regex(/^source1\.[A-Za-z0-9_-]+$/).max(94),
    source_commit: z.string().regex(/^[0-9a-f]{40,64}$/),
    source_path: z.string().min(1).max(1_024),
    intaken_at: recordedAtStamp,
  })
  .strict();

/**
 * The cockpit asks for the described listing, because a picker has to offer a
 * name rather than a hash. These fields mirror `WorkflowRevisionSummaryResourceV2`
 * in the frozen document and `servedVocabulary.test.ts` holds them to it: the
 * decoder is `.strict()`, so a field the server adds without this file throws on
 * every load instead of being quietly ignored.
 */
export const workflowRevisionSummarySchema = z
  .object({
    workflow_revision_hash: sha256,
    workflow_format_version: z.literal(3),
    executable: z.boolean(),
    not_executable_reason: z.string().nullable(),
    name: z.string(),
    description: z.string().nullable(),
    provenance: workflowRevisionProvenanceSchema.nullable().optional(),
  })
  .strict();

export const workflowRevisionDetailSchema = z
  .object({
    workflow_revision_hash: sha256,
    document_base64: standardBase64,
    graph: workflowGraphV3Schema,
    provenance: workflowRevisionProvenanceSchema.nullable().optional(),
  })
  .strict();

const workflowRevisionPageSchema = z
  .object({
    items: z.array(workflowRevisionSummarySchema),
    next_after_revision_hash: sha256.nullable(),
  })
  .strict();

export const projectResourceSchema = z
  .object({ public_project_reference: publicProjectReference })
  .strict();

export const projectListSchema = z
  .object({ items: z.array(projectResourceSchema).max(1) })
  .strict();

export const projectSourceConnectionRevisionSchema = z
  .object({
    public_project_reference: publicProjectReference,
    revision_number: positiveSafeInteger,
    source_kind: z.string().min(1).max(64),
    source_address: z.string().min(1).max(1_024),
    auth_method: z.literal("personal-access-token"),
    project_source_connection_revision_hash: sha256,
  })
  .strict();

const publicSourceReference = z
  .string()
  .regex(/^source1\.[A-Za-z0-9_-]+$/)
  .max(56);

export const projectSourceResourceSchema = z
  .object({
    public_source_reference: publicSourceReference,
    kind: z.string().min(1).max(64),
    address: z.string().min(1).max(1_024),
    scope: z.literal("issues").default("issues"),
    connected_at: z.union([recordedAtStamp, z.null()]).default(null),
    revision: positiveSafeInteger,
    auth_method: z.literal("personal-access-token"),
  })
  .strict();

export const projectSourceListSchema = z
  .object({ items: z.array(projectSourceResourceSchema) })
  .strict();

export type { HealthResource };

const providerIdSchema = z
  .string()
  .min(1)
  .max(64)
  .regex(/^[a-z][a-z0-9._-]*$/);
const exactModelIdSchema = z.string().min(1).max(1_024).regex(/^\S+$/u);

const modelRegistryEntrySchema = z
  .object({
    model_id: exactModelIdSchema,
    agent_configuration_revision_hash: sha256,
    source: z.enum(["discovered", "operator"]),
    provider_check: z.enum(["not-checked", "checked", "unknown-at-provider"]),
  })
  .strict();

export const modelRegistryRevisionSchema = z
  .object({
    provider_id: providerIdSchema,
    revision_number: positiveSafeInteger,
    model_registry_revision_hash: sha256,
    entries: z.array(modelRegistryEntrySchema).max(100),
  })
  .strict();

const projectModelDefaultSchema = z
  .object({
    difficulty: z.union([z.literal(1), z.literal(2), z.literal(3)]),
    model_registry_revision_hash: sha256,
    provider_id: providerIdSchema,
    model_id: exactModelIdSchema,
    agent_configuration_revision_hash: sha256,
  })
  .strict();

export const projectModelDefaultsRevisionSchema = z
  .object({
    project_id: z.string().min(1).max(1_024),
    public_project_reference: publicProjectReference,
    revision_number: positiveSafeInteger,
    project_model_defaults_revision_hash: sha256,
    defaults: z.array(projectModelDefaultSchema).max(3),
  })
  .strict();

const roleModelResolutionSchema = z
  .object({
    role: z.string().min(1).max(1_024),
    agent_configuration_revision_hash: sha256.nullable(),
    source: z.enum([
      "chosen-now",
      "pinned-in-workflow",
      "from-project",
      "uncast",
    ]),
    model_id: exactModelIdSchema.nullable(),
    declared_difficulty: z.union([z.literal(1), z.literal(2), z.literal(3)]),
    default_difficulty: z
      .union([z.literal(1), z.literal(2), z.literal(3)])
      .nullable(),
    uncast_reason: z
      .enum([
        "override-not-registered",
        "workflow-model-not-registered",
        "workflow-model-ambiguous",
        "no-project-default",
        "family-difference-unavailable",
      ])
      .nullable(),
    family_differs_from: z.string().min(1).max(1_024).nullable(),
  })
  .strict();

export const projectModelResolutionSchema = z
  .object({
    project_id: z.string().min(1).max(1_024),
    public_project_reference: publicProjectReference,
    workflow_revision_hash: sha256,
    resolutions: z.array(roleModelResolutionSchema).max(100),
  })
  .strict();

/**
 * What `GET /catalog-revisions/by-name/{kind}/{name}` answers: which revision
 * that catalog name holds under that kind. The described listing does not carry
 * lineage recency, so the picker reads this existing resource for the head
 * instead of inventing an order from the hash-sorted page.
 */
export const catalogNameResolutionSchema = z
  .object({
    display_name: z.string().min(1).max(128),
    lineage_id: sha256,
    catalog_revision_hash: sha256,
    revision_number: positiveSafeInteger,
  })
  .strict();

const catalogAdmissionSchema = z
  .object({
    display_name: z.string().min(1).max(128),
    lineage_id: sha256,
    catalog_revision_hash: sha256,
    revision_number: positiveSafeInteger,
  })
  .strict();

const libraryRecognitionSchema = z.discriminatedUnion("outcome", [
  z.object({
    outcome: z.literal("workflow"),
    workflow_format_version: z.literal(3),
    name: z.string(),
    description: z.string().nullable(),
  }).strict(),
  z.object({
    outcome: z.literal("agent_definition"),
    name: z.string().min(1),
    description: z.string().min(1),
    provider_id: z.string().min(1),
  }).strict(),
  z.object({
    outcome: z.literal("not_held"),
    kind: z.union([z.literal("skill"), z.literal("mcp_server")]),
    reason: z.string().min(1),
  }).strict(),
  z.object({
    outcome: z.literal("unrecognized"),
    refusals: z.array(z.object({
      kind: z.union([
        z.literal("workflow"),
        z.literal("agent_definition"),
        z.literal("skill"),
        z.literal("mcp_server"),
      ]),
      expected: z.string().min(1),
      refused_because: z.string().min(1),
    }).strict()),
  }).strict(),
]);

const catalogIntakeKindSchema = z.enum(["agent", "skill", "workflow"]);

const libraryAdditionSchema = z
  .object({
    intake_id: sha256,
    kind: catalogIntakeKindSchema,
  })
  .strict();

/**
 * The published kinds this cockpit gives a catalog lineage.
 *
 * The door itself takes every kind the registry publishes; these two are the
 * ones a person hands in here, and the ones whose documents author their own
 * catalog name.
 */
export type CatalogLineageKind = "workflow" | "agent_definition";

interface CatalogAdmissionInput {
  kind: CatalogLineageKind;
  catalog_revision_hash: string;
  actor: string;
  activated_at: string;
}

interface CatalogRetirementInput {
  actor: string;
  activated_at: string;
}

const authProfileInputSchema = z
  .object({
    profile_id: z.string().min(1).max(1_024),
    revision_number: positiveSafeInteger,
    provider_id: z.string().min(1).max(64),
    auth_mode: z.enum(["subscription", "api_key"]),
  })
  .strict();

const authProfileRevisionSchema = authProfileInputSchema
  .extend({ auth_profile_revision_hash: sha256 })
  .strict();

const agentConfigurationInputSchema = z
  .object({
    model: z.string().min(1).max(1_024),
    auth_profile_revision_hash: sha256,
    executor_revision: z.string().min(1).max(1_024),
    requested_capability: z
      .enum(["headless", "headless_with_tools", "interactive"])
      .optional(),
  })
  .strict();

const agentConfigurationRevisionSchema = agentConfigurationInputSchema
  .extend({
    provider_id: z.string().min(1).max(64),
    auth_mode: z.enum(["subscription", "api_key"]),
    requested_capability: z.enum([
      "headless",
      "headless_with_tools",
      "interactive",
    ]),
    agent_configuration_revision_hash: sha256,
  })
  .strict();

export const agentConfigurationRevisionListItemObjectSchema =
  agentConfigurationRevisionSchema
    .extend({
      startable: z.boolean(),
      structurally_startable: z.boolean(),
      not_startable_reason: z
        .enum([
          "agent-executor-binding-unavailable",
          "model-not-registered",
          "provider-probe-receipt-missing",
          "provider-probe-failed",
        ])
        .nullable(),
      provider_probe_problem_code: z.string().min(1).nullable(),
      provider_probe_observed_at: recordedAtStamp.nullable(),
    })
    .strict();

const agentConfigurationRevisionListItemSchema =
  agentConfigurationRevisionListItemObjectSchema.superRefine(
    (item, context) => {
      if (item.startable && !item.structurally_startable) {
        context.addIssue({
          code: "custom",
          message:
            "agent configuration startability cannot hold without its own structural startability",
        });
      }
      if (item.startable !== (item.not_startable_reason === null)) {
        context.addIssue({
          code: "custom",
          message: "agent configuration startability and reason disagree",
        });
      }
      if (
        !item.structurally_startable &&
        item.not_startable_reason !== "agent-executor-binding-unavailable"
      ) {
        context.addIssue({
          code: "custom",
          message:
            "a structurally unavailable configuration must carry its own reason",
        });
      }
      if (
        item.structurally_startable &&
        item.not_startable_reason === "agent-executor-binding-unavailable"
      ) {
        context.addIssue({
          code: "custom",
          message:
            "a structurally startable configuration cannot carry the executor-unavailable reason",
        });
      }
      const carriesProbeFailureEvidence =
        item.provider_probe_problem_code !== null ||
        item.provider_probe_observed_at !== null;
      if ((item.not_startable_reason === "provider-probe-failed") !== carriesProbeFailureEvidence) {
        context.addIssue({
          code: "custom",
          message: "provider probe failure evidence and its reason must agree",
        });
      }
      if (
        carriesProbeFailureEvidence &&
        (item.provider_probe_problem_code === null || item.provider_probe_observed_at === null)
      ) {
        context.addIssue({
          code: "custom",
          message:
            "a provider probe failure names both its problem code and when it was observed",
        });
      }
    },
  );

/**
 * Listing adds the deployment's current startability decision. Publication
 * remains its immutable resource, so the browser never mistakes a host fact
 * for revision identity.
 */
export const agentConfigurationRevisionPageSchema = z
  .object({
    items: z.array(agentConfigurationRevisionListItemSchema),
    next_after_revision_hash: sha256.nullable(),
  })
  .strict();

/** An item a connected tracker has observed for the served project. */
const queueObservationFields = {
  title: z.string().min(1).nullable(),
  title_observed_at: recordedAtStamp.nullable(),
  retired_at: recordedAtStamp.nullable()
} as const;

export const observedQueueItemSchema = z
  .object({
    project_id: z.string().min(1),
    tracker_item_reference: z.string().min(1),
    item_id: sha256,
    revision: nonnegativeSafeInteger,
    ...queueObservationFields
  })
  .strict();

const queueProposalSchema = z
  .object({
    revision: positiveSafeInteger,
    priority: z.object({ rank: positiveSafeInteger }).strict(),
    workflow_lineage_id: sha256,
    prerequisite_item_ids: z.array(sha256),
    automation_disposition: z.enum(["HUMAN_REQUIRED", "AUTOMATION_AUTHORIZED"]),
    policy_revision: positiveSafeInteger.nullable(),
    source: z.enum(["OPERATOR", "POLICY_DEFAULT"])
  })
  .strict();

const queueAdmissionSchema = z
  .object({
    proposal_revision: positiveSafeInteger.nullable(),
    authority: z.enum(["OPERATOR", "AUTOMATION_RULE"]).nullable(),
    rationale: z.string().min(1)
  })
  .strict();

const queueLaunchBindingSchema = z
  .object({
    proposal_revision: positiveSafeInteger,
    run_id: z.string().min(1),
    workflow_revision_hash: sha256
  })
  .strict();

export const queueItemSchema = z
  .object({
    project_id: z.string().min(1),
    tracker_item_reference: z.string().min(1),
    item_id: sha256,
    state: z.enum(["OBSERVED", "PROPOSED", "ADMITTED"]),
    revision: nonnegativeSafeInteger,
    proposal: queueProposalSchema.nullable(),
    admission: queueAdmissionSchema.nullable(),
    launch_binding: queueLaunchBindingSchema.nullable(),
    blockers: z.array(
      z.enum([
        "PRIORITY_UNSET",
        "HUMAN_REQUIRED",
        "PREREQUISITE_OPEN",
        "PREREQUISITE_FAILED",
        "CAP_REACHED",
        "BINDING_UNRESOLVED",
        "REQUIRED_ORDER_UNAVAILABLE",
        "START_REFUSED",
        "LEGACY_REVIEW_REQUIRED"
      ])
    ),
    tracker_enrichment: z.literal("ENRICHMENT_UNAVAILABLE"),
    ...queueObservationFields
  })
  .strict();

const queueItemPageSchema = z
  .object({ items: z.array(queueItemSchema), next_after: sha256.nullable() })
  .strict();

/**
 * One published agent definition as its author named it, and no more.
 *
 * An imported agent is provider-bound and passed through whole, so the rest of
 * the file belongs to that provider's runtime rather than to a catalog row.
 */
export const agentDefinitionRevisionListItemSchema = z
  .object({
    agent_definition_revision_hash: sha256,
    name: z.string().min(1),
    description: z.string().min(1),
  })
  .strict();

export const agentDefinitionRevisionPageSchema = z
  .object({
    items: z.array(agentDefinitionRevisionListItemSchema),
    next_after_revision_hash: sha256.nullable(),
  })
  .strict();

/**
 * One published agent definition read back by its hash, which is where the
 * cockpit learns the name its frontmatter authored: publication answers the
 * hash alone, and the catalog lineage is founded under the authored name.
 */
export const agentDefinitionRevisionDetailSchema = z
  .object({
    agent_definition_revision_hash: sha256,
    name: z.string().min(1),
    description: z.string().min(1),
    model: z.string().min(1).nullable().optional(),
    system_prompt: z.string().min(1).max(16_384),
    tools: z.array(z.string().min(1).max(16_384)).max(128).nullable().optional(),
  })
  .strict();

const agentDefinitionRevisionSchema = z
  .object({ agent_definition_revision_hash: sha256 })
  .strict();

/**
 * `contracts/artifacts.py::MAXIMUM_ARTIFACT_BYTES`, mirrored here as a plain
 * number the way every other server-owned wire bound already is on this side
 * (`MAXIMUM_REFUSED_OUTPUT_BASE64_CHARACTERS` above). The start sheet reads
 * it to show a string order's byte count as it is typed; the server still
 * owns enforcing it -- an oversized publish still refuses named
 * (`artifact-too-large`) rather than being blocked silently here first.
 */
export const MAXIMUM_ARTIFACT_BYTES = 1_048_576;

const artifactResourceSchema = z.object({ artifact_hash: sha256 }).strict();
type ArtifactResource = z.infer<typeof artifactResourceSchema>;

/**
 * The listing of published auth profiles, in the item form publication already
 * answers with. Held to the frozen document by servedVocabulary.
 */
export const authProfileRevisionPageSchema = z
  .object({
    items: z.array(authProfileRevisionSchema),
    next_after_revision_hash: sha256.nullable(),
  })
  .strict();

const agentBindingV2Schema = z
  .object({
    role: z.string().min(1),
    agent_configuration_revision_hash: sha256,
    auth_profile_revision_hash: sha256,
    profile_id: z.string().min(1),
    revision_number: positiveSafeInteger,
    provider_id: z.string().min(1),
    auth_mode: z.enum(["subscription", "api_key"]),
    model: z.string().min(1),
    executor_revision: z.string().min(1),
  })
  .strict();

/** The attempt states the served document names, in the order it names them. */
export const PUBLIC_ATTEMPT_STATES = [
  "PREPARED",
  "POSSIBLY_RAN",
  "CANCEL_REQUESTED",
  "CANCELLED",
  "INTERRUPTED",
  "FAILED",
  "SUCCEEDED",
] as const;

/** The states the served document names; tests/api/servedVocabulary holds them to it. */
export const NODE_STATES = [
  "queued",
  "working",
  "needs_you",
  "succeeded",
  "failed",
  "cancelled",
  "interrupted",
] as const;

export const nodeRailEntrySchema = z
  .object({
    node_id: z.string().min(1),
    state: z.enum(NODE_STATES),
    attempt: z
      .object({
        ordinal: z.union([z.literal(1), z.literal(2)]),
        state: z.enum(PUBLIC_ATTEMPT_STATES).nullable(),
      })
      .strict()
      .nullable(),
    reused_from_run_reference: publicRunReference.nullable().optional(),
    source_event_hash: sha256.nullable().optional(),
    source_receipt_hash: sha256.nullable().optional(),
    source_declared_context_package_hash: sha256.nullable().optional(),
  })
  .strict()
  .superRefine((entry, context) => {
    const reuseEvidence = [
      entry.reused_from_run_reference,
      entry.source_event_hash,
      entry.source_receipt_hash,
      entry.source_declared_context_package_hash,
    ];
    const namedEvidence = reuseEvidence.filter((value) => value != null).length;
    if (namedEvidence !== 0 && namedEvidence !== reuseEvidence.length) {
      context.addIssue({
        code: "custom",
        message: "a reused rail node names its complete source evidence",
      });
    }
    if (namedEvidence === reuseEvidence.length && entry.state !== "succeeded") {
      context.addIssue({
        code: "custom",
        message: "only a succeeded rail node can be reused",
      });
    }
  });

/**
 * A V3 run as the server answers it -- the only run format the wire still
 * serves (#901 slice 4; the start door refuses every other format and the
 * server projects format 3 alone since PR #920).
 *
 * A V3 run names its current node by id and carries no `waiting` block,
 * although it does reach WAITING_INPUT: what a V3 Wait node asks and which
 * schema judges the answer belong to the document, and the rail is what marks
 * the node owing a person a move.
 */
/** The V3 run states the served document names; tests/api/servedVocabulary holds them to it. */
export const RUN_STATES_V3 = [
  "STARTED",
  "WAITING_RECONCILIATION",
  "WAITING_INPUT",
  "COMPLETED",
  "FAILED",
  "CANCELLED",
] as const;

export type RunStateV3 = (typeof RUN_STATES_V3)[number];

/** Why the server says a V3 run cannot be operator-cancelled; #439 D3's closed set. */
export const RUN_NOT_CANCELLABLE_REASONS = [
  "between-nodes",
  "waiting-for-you",
  "node-runs-no-agent",
  "already-cancelling",
  "already-ended",
  "answer-in-flight",
] as const;

export type RunNotCancellableReason =
  (typeof RUN_NOT_CANCELLABLE_REASONS)[number];

/**
 * Whether a V3 run can be operator-cancelled right now, said by the server.
 *
 * #439 D3 makes this the server's predicate, not the cockpit's guess: a
 * cancellable run names the `target_node_execution_id` a cancel fences on, and a
 * run that cannot be cancelled names the closed reason token instead. The server
 * sends it on every V3 run, so it is decoded as required here: the cancel
 * control reads it to know whether the run can be stopped and, when it cannot,
 * which sentence to show instead of a grey nothing.
 */
const runCancellabilitySchema = z
  .object({
    cancellable: z.boolean(),
    reason: z.enum(RUN_NOT_CANCELLABLE_REASONS).nullable(),
    target_node_execution_id: sha256.nullable(),
  })
  .strict()
  .superRefine((cancellation, context) => {
    if (
      cancellation.cancellable !==
      (cancellation.target_node_execution_id !== null)
    ) {
      context.addIssue({
        code: "custom",
        message: "a cancellable run names its target node execution",
      });
    }
    if (cancellation.cancellable !== (cancellation.reason === null)) {
      context.addIssue({
        code: "custom",
        message: "a non-cancellable run names exactly one reason",
      });
    }
  });

/**
 * One order a V3 run was started with, told safely -- never its own bytes.
 *
 * An order's material can be a secret a caller pasted by mistake, or an
 * artifact up to the server's own artifact size bound, and this resource is
 * served on every listed run -- so it never echoes the order's bytes at all.
 * `bytes` is how large the order's material is; `schema_revision_hash` is
 * the schema it satisfies. No text preview travels here yet: that needs a
 * redaction owner the server does not carry until #666 lands.
 */
const runOrderSchema = z
  .object({
    name: z.string().min(1),
    bytes: nonnegativeSafeInteger,
    schema_revision_hash: sha256,
  })
  .strict();


const runForkOriginSchema = z
  .object({
    public_run_reference: publicRunReference,
    terminal_hash: sha256,
    restart_from_node_id: z.string().min(1),
    fork_hash: sha256,
  })
  .strict();

const runForkSuccessorSchema = z
  .object({
    public_run_reference: publicRunReference,
    restart_from_node_id: z.string().min(1),
    fork_hash: sha256,
  })
  .strict();

export const MAXIMUM_RUN_FORK_SUCCESSORS = 100;

/**
 * One node of a run, as `GET /runs/{ref}/nodes/{node_id}` answers it.
 *
 * Five answers, each allowed to be absent, because the absence is the answer: a
 * node that has not run yet was asked nothing this reader can prove, wrote
 * nothing and has no receipt, and a refusal exists only where something really
 * refuses. The panel renders exactly that and invents no placeholder.
 *
 * `refusal_output` is deliberately not `answer`: it is a redacted presentation
 * of the bytes a schema owner judged and refused, never the value the run
 * accepted (#664), and it is present only where that judgment happened and
 * the store still held something safe to show. Optional, like `started_at` /
 * `ended_at` above -- an older test fixture or a caller of a build before
 * #664 omits the key entirely, and that reads exactly as the absence it is
 * (`?? null` at every reader), never a parse failure.
 *
 * `transcript` is the decoded, already-redacted steps of the attempt that
 * named one. Optional the same way: the server omits the key when no attempt
 * stored a transcript, and an older fixture that never heard of the field
 * still decodes. Usage is still not a receipt field; it can appear as a
 * `usage` transcript event when the attempt stored one. Duration sits beside
 * the attempt as started_at / ended_at, not on the receipt.
 */
const nodeProvenanceSchema = z
  .object({
    role: z.string().min(1),
    provider_id: z.string().min(1),
    model: z.string().min(1),
    executor_revision: z.string().min(1),
    executor_operational_identity: z.string().min(1),
    auth_mode: z.string().min(1),
    profile_id: z.string().min(1),
    agent_configuration_revision_hash: sha256,
    request_hash: sha256,
    receipt_hash: sha256,
  })
  .strict();

const nodeAnswerSchema = z
  .object({ value_base64: z.string(), value_hash: sha256 })
  .strict();

/**
 * `base64_characters_for(maximum_redacted_length(MAXIMUM_AGENT_OUTPUT_BYTES_V2))`
 * (`api/references.py`), mirrored here as a plain number the way every other
 * server-owned wire bound already is on this side (see `.max(64)` above).
 * Only a V3 agent node's own schema-refused output ever reaches
 * `refusal_output`, and every executor adapter already refuses the domain
 * more than 49,152 bytes before that judgment runs -- but the server redacts
 * credential shapes out of that text before it is served (#664), and
 * replacing a short credential with the longer `[redacted]` marker can grow
 * it. `maximum_redacted_length` is the redaction owner's own declared
 * worst case for that growth, so this bound rests on the agent output cap
 * *and* that owner's own number, not a third one invented on this side.
 */
export const MAXIMUM_REFUSED_OUTPUT_BASE64_CHARACTERS = 81_920;

const nodeRefusalOutputSchema = z
  .object({
    value_base64: z.string().max(MAXIMUM_REFUSED_OUTPUT_BASE64_CHARACTERS),
    value_hash: sha256,
  })
  .strict();

/**
 * `base64_characters_for(MAXIMUM_AGENT_OUTPUT_BYTES_V2)` (`api/references.py`),
 * mirrored here the same way `MAXIMUM_REFUSED_OUTPUT_BASE64_CHARACTERS` above
 * already is. `RunResourceV3.answer` (#1045) is served on every listed run,
 * repeated once per row, so unlike `nodeAnswerSchema` (unbounded: a single
 * node detail read shares no byte bound across node kinds) it needs one.
 */
const MAXIMUM_RUN_TERMINAL_ANSWER_BASE64_CHARACTERS = 65_536;

const runTerminalAnswerValueSchema = z
  .object({
    kind: z.literal("value"),
    value_base64: z.string().max(MAXIMUM_RUN_TERMINAL_ANSWER_BASE64_CHARACTERS),
    value_hash: sha256,
  })
  .strict();

/**
 * A terminal node did write an answer, but not the value this row carries
 * (#1045): a bare `null` would read the same as a node that wrote nothing at
 * all, so an oversized value is named instead of hidden behind that absence.
 */
const runTerminalAnswerOmittedSchema = z
  .object({
    kind: z.literal("omitted"),
    reason: z.literal("too_large"),
    maximum_bytes: nonnegativeSafeInteger,
  })
  .strict();

const runTerminalAnswerSchema = z.discriminatedUnion("kind", [
  runTerminalAnswerValueSchema,
  runTerminalAnswerOmittedSchema,
]);


export const runV3Schema = z
  .object({
    workflow_format_version: z.literal(3),
    run_id: z.string().min(1),
    public_run_reference: publicRunReference,
    workflow_revision_hash: sha256,
    // A format-3 document always declares a name, so this is never absent
    // (#1045). History used to ask `getWorkflowRevision` once per distinct
    // revision hash for exactly this; this is that fact carried on the row.
    workflow_name: z.string().min(1),
    agent_binding_set_hash: sha256,
    run_configuration_revision_hash: sha256,
    agent_bindings: z.array(agentBindingV2Schema).max(100),
    orders: z.array(runOrderSchema),
    // The tracker reference one of `orders` above names, read against the
    // published `work_item` order schema server-side (#1045) -- never a
    // guess parsed from a node's job text. `1_024` mirrors
    // `MAXIMUM_TRACKER_ITEM_REFERENCE_CHARACTERS` (`contracts/queue_projection.py`).
    work_item_reference: z.string().min(1).max(1_024).nullable().optional(),
    fork_origin: runForkOriginSchema.nullable().optional(),
    fork_successors: z
      .array(runForkSuccessorSchema)
      .max(MAXIMUM_RUN_FORK_SUCCESSORS)
      .optional(),
    // What the terminal node wrote or refused, mutually exclusive, both
    // absent on a run that has not ended (#1045). History used to ask
    // `getNodeDetail` once per row for exactly these two facts.
    answer: runTerminalAnswerSchema.nullable().optional(),
    refusal_output: nodeRefusalOutputSchema.nullable().optional(),
    state_version: nonnegativeSafeInteger,
    state: z.enum(RUN_STATES_V3),
    current_node_id: z.string().min(1),
    current_node_execution_id: sha256,
    node_rail: z.array(nodeRailEntrySchema).min(1),
    cancellation: runCancellabilitySchema,
    terminal_hash: sha256.nullable(),
    latest_event_cursor: eventCursor.nullable(),
    started_at: z.string().nullable().optional(),
    ended_at: z.string().nullable().optional(),
  })
  .strict();

/**
 * `MAXIMUM_TRANSCRIPT_STEP_CHARACTERS` (`contracts/agent_transcripts.py`),
 * mirrored here as a plain number the way every other server-owned wire bound
 * already is on this side.
 */
export const MAXIMUM_TRANSCRIPT_STEP_CHARACTERS = 8_192;

const transcriptStepTextSchema = z.string().max(MAXIMUM_TRANSCRIPT_STEP_CHARACTERS);

const transcriptRecordedMomentSchema = z
  .object({
    recorded_at: recordedAtStamp,
    origin: z.literal("recorded"),
  })
  .strict();

const transcriptBeforeMomentsSchema = z
  .object({
    origin: z.literal("v1-before-moments"),
  })
  .strict();

const transcriptEventMomentSchema = z.discriminatedUnion("origin", [
  transcriptRecordedMomentSchema,
  transcriptBeforeMomentsSchema,
]);

export const toolCalledEventSchema = z
  .object({
    event: z.literal("tool-called"),
    name: transcriptStepTextSchema,
    arguments: transcriptStepTextSchema,
    redacted: z.boolean(),
    moment: transcriptEventMomentSchema,
  })
  .strict();

export const toolReturnedEventSchema = z
  .object({
    event: z.literal("tool-returned"),
    name: transcriptStepTextSchema,
    result: transcriptStepTextSchema,
    redacted: z.boolean(),
    moment: transcriptEventMomentSchema,
  })
  .strict();

export const assistantTurnEventSchema = z
  .object({
    event: z.literal("assistant-turn"),
    text: transcriptStepTextSchema,
    redacted: z.boolean(),
    moment: transcriptEventMomentSchema,
  })
  .strict();

export const usageEventSchema = z
  .object({
    event: z.literal("usage"),
    input_tokens: nonnegativeSafeInteger,
    output_tokens: nonnegativeSafeInteger,
    cache_read_input_tokens: nonnegativeSafeInteger,
    cache_creation_input_tokens: nonnegativeSafeInteger,
    moment: transcriptEventMomentSchema,
  })
  .strict();

export const providerTerminalRefusalEventSchema = z
  .object({
    event: z.literal("provider-terminal-refusal"),
    terminal_reason: transcriptStepTextSchema,
    api_error_status: transcriptStepTextSchema,
    text: transcriptStepTextSchema,
    redacted: z.boolean(),
    moment: transcriptEventMomentSchema,
  })
  .strict();

export const unrecognisedProviderOutputEventSchema = z
  .object({
    event: z.literal("unrecognised-provider-output"),
    text: transcriptStepTextSchema,
    redacted: z.boolean(),
    moment: transcriptEventMomentSchema,
  })
  .strict();

export const transcriptTruncatedEventSchema = z
  .object({
    event: z.literal("transcript-truncated"),
    dropped_events: positiveSafeInteger,
    moment: transcriptEventMomentSchema,
  })
  .strict();

export const attemptTranscriptSchema = z
  .object({
    events: z
      .array(
        z.discriminatedUnion("event", [
          toolCalledEventSchema,
          toolReturnedEventSchema,
          assistantTurnEventSchema,
          usageEventSchema,
          providerTerminalRefusalEventSchema,
          unrecognisedProviderOutputEventSchema,
          transcriptTruncatedEventSchema,
        ]),
      )
      .min(1),
  })
  .strict();

export type AttemptTranscript = z.infer<typeof attemptTranscriptSchema>;

export const nodeDetailSchema = z
  .object({
    run_id: z.string().min(1),
    public_run_reference: publicRunReference,
    node_id: z.string().min(1),
    state: z.enum(NODE_STATES),
    job_base64: z.string().nullable(),
    job_hash: sha256.nullable(),
    answer: nodeAnswerSchema.nullable(),
    provenance: nodeProvenanceSchema.nullable(),
    refusal: z.string().nullable(),
    refusal_output: nodeRefusalOutputSchema.nullable().optional(),
    started_at: z.string().nullable().optional(),
    ended_at: z.string().nullable().optional(),
    transcript: attemptTranscriptSchema.nullable().optional(),
  })
  .strict();

export type NodeDetail = z.infer<typeof nodeDetailSchema>;

/**
 * `MAXIMUM_RUN_ROW_DEFECT_DETAIL_CHARACTERS` (`contracts/run_projections.py`),
 * mirrored here as a plain number the way every other server-owned wire bound
 * already is on this side.
 */
const MAXIMUM_RUN_ROW_DEFECT_DETAIL_CHARACTERS = 512;

const runListRowSchema = z
  .object({
    kind: z.literal("run"),
    run: runV3Schema,
  })
  .strict();

/**
 * One listed run whose own projection failed, told apart from a run instead
 * of hidden (#1042). The other rows on the same page prove nothing about
 * this one, so a run list answers with this row rather than refusing the
 * whole page for the sake of one entry.
 */
const defectiveRunRowSchema = z
  .object({
    kind: z.literal("defective"),
    public_run_reference: publicRunReference,
    problem_code: z.literal("durable-state-corrupt"),
    detail: z.string().min(1).max(MAXIMUM_RUN_ROW_DEFECT_DETAIL_CHARACTERS),
  })
  .strict();

const runListRowUnionSchema = z.discriminatedUnion("kind", [
  runListRowSchema,
  defectiveRunRowSchema,
]);

export type RunListRow = z.infer<typeof runListRowUnionSchema>;
export type DefectiveRunRow = z.infer<typeof defectiveRunRowSchema>;

const runPageSchema = z
  .object({
    items: z.array(runListRowUnionSchema),
    next_after: publicRunReference.nullable(),
  })
  .strict();

export const EFFECT_CONFIRMATION_SOURCES = [
  "ADAPTER_READBACK",
  "ADAPTER_EXECUTION",
  "OPERATOR_FOUND",
  "OPERATOR_AUTHORIZED_EXECUTION",
  "FORK_REFERENCE",
] as const;

const receiptSchema = z
  .object({
    logical_effect_key: z.string().min(1),
    request_hash: sha256,
    effect_id: z.string().min(1),
    result_hash: sha256,
    result_base64: standardBase64,
    confirmation_source: z.enum(EFFECT_CONFIRMATION_SOURCES),
    reconcile_command_id: z.string().min(1).nullable(),
  })
  .strict();

const eventBase = {
  cursor: eventCursor,
  sequence: positiveSafeInteger,
  public_run_reference: publicRunReference,
  workflow_revision_hash: sha256,
  node_id: z.string().min(1),
  node_execution_id: sha256,
  event_hash: sha256,
};

const v2AttemptEvent = {
  attempt_id: sha256,
  attempt_ordinal: z.union([z.literal(1), z.literal(2)]),
};
const v2CancellationEvent = {
  ...v2AttemptEvent,
  command_id: z.string().min(1).max(1_024),
  replacement: z.enum(["NONE", "ONE"]),
};
const v2Disposition = z.enum([
  "NEVER_LAUNCHED",
  "EXITED_BEFORE_SIGNAL",
  "REAPED_AFTER_TERM",
  "REAPED_AFTER_KILL",
  "OWNER_LOST_AFTER_PARENT_DEATH",
]);

/**
 * A format-3 event, in the shape the service answers with.
 *
 * The forms are the served wire's own (#249), read here rather than invented a
 * third time: the API answers them, the command reads them (#253), and this is
 * the surface the operator actually watches. A version-3 line writes its agent
 * events through the same attempt store as a version-2 one and its pauses
 * through the same wait path, so the attempt and the rail travel the same way.
 * Its answer is base64 rather than the V2 shape's decimal text, because a V3
 * wait admits whatever its declared schema admits. A cancelled pause is its own
 * kind here and nowhere else: an operator can end a run resting at a wait, and
 * that event -- naming only the command that ordered it -- is the whole
 * attestation, because a pause has no attempt to stamp. A linear Action
 * persists the same durable-effect kinds V2 already names, so those receipts
 * travel here with the rail. Subworkflow events stay absent: no format-3 run
 * persists that kind today.
 */
const v3EventBase = {
  workflow_format_version: z.literal(3),
  ...eventBase,
  node_rail: z.array(nodeRailEntrySchema).min(1),
};

const runEventSchema = z
  .union([
    z
      .object({
        ...v3EventBase,
        ...v2AttemptEvent,
        event: z.literal("AGENT_COMPLETED"),
        output_base64: standardBase64,
        output_hash: sha256,
      })
      .strict(),
    z
      .object({
        ...v3EventBase,
        ...v2AttemptEvent,
        event: z.literal("AGENT_FAILED"),
        failure_code: z.enum([
          "PROCESS_EXITED_UNSUCCESSFULLY",
          "PROCESS_OUTPUT_LIMIT_EXCEEDED",
          "PROCESS_SUPERVISION_FAILED",
          "OUTPUT_SCHEMA_REFUSED",
          "AGENT_REFUSED",
          "PROJECT_VERIFICATION_FAILED",
          "CANDIDATE_CAPTURE_FAILED",
          "CANDIDATE_UNCHANGED",
          "PRODUCED_VALUE_REFUSED",
        ]),
        reason: z.string().min(1).nullable(),
      })
      .strict(),
    z
      .object({
        ...v3EventBase,
        event: z.literal("AGENT_FAILED"),
        reason: z.literal("agent-executor-binding-unavailable"),
      })
      .strict(),
    z
      .object({
        ...v3EventBase,
        ...v2CancellationEvent,
        event: z.literal("AGENT_CANCEL_REQUESTED"),
      })
      .strict(),
    z
      .object({
        ...v3EventBase,
        ...v2CancellationEvent,
        event: z.literal("AGENT_CANCELLED"),
        disposition: v2Disposition,
        replacement_attempt_id: sha256.nullable(),
      })
      .strict(),
    z
      .object({
        ...v3EventBase,
        ...v2CancellationEvent,
        event: z.literal("AGENT_INTERRUPTED"),
        disposition: v2Disposition,
        replacement_attempt_id: sha256.nullable(),
      })
      .strict(),
    z
      .object({
        ...v3EventBase,
        event: z.literal("ACTION_RECONCILIATION_REQUIRED"),
        request_base64: standardBase64,
        request_hash: sha256,
      })
      .strict(),
    z
      .object({
        ...v3EventBase,
        event: z.literal("ACTION_RECONCILIATION_RESOLVED"),
        receipt: receiptSchema,
      })
      .strict(),
    z
      .object({
        ...v3EventBase,
        event: z.literal("ACTION_COMPLETED"),
        receipt: receiptSchema,
      })
      .strict(),
    z.object({ ...v3EventBase, event: z.literal("WAITING_INPUT") }).strict(),
    z
      .object({
        ...v3EventBase,
        event: z.literal("WAIT_ANSWERED"),
        answer_base64: standardBase64,
        answer_hash: sha256,
        actor: z.enum(["operator", "legacy-unattributed"]),
      })
      .strict(),
    z
      .object({
        ...v3EventBase,
        event: z.literal("WAIT_CANCELLED"),
        command_id: z.string().min(1).max(1_024),
      })
      .strict(),
  ])
  .superRefine(validateEventCursor);

function validateEventCursor(
  event: { cursor: string; public_run_reference: string; sequence: number },
  context: z.RefinementCtx,
): void {
  const parsedCursor = parseEventCursor(event.cursor);
  if (
    parsedCursor?.publicRunReference !== event.public_run_reference ||
    parsedCursor.sequence !== event.sequence
  ) {
    context.addIssue({
      code: "custom",
      message: "event cursor, run reference, and sequence disagree",
    });
  }
}

export const problemDefinitions = {
  "auth-profile-revision-conflict": {
    status: 409,
    title: "Auth profile revision conflict",
  },
  "auth-profile-revision-collision": {
    status: 409,
    title: "Auth profile revision collision",
  },
  "auth-profile-revision-not-found": {
    status: 404,
    title: "Auth profile revision not found",
  },
  "agent-executor-binding-unavailable": {
    status: 409,
    title: "Agent executor binding unavailable",
  },
  "agent-configuration-revision-collision": {
    status: 409,
    title: "Agent configuration revision collision",
  },
  "agent-configuration-revision-not-found": {
    status: 404,
    title: "Agent configuration revision not found",
  },
  "invalid-agent-bindings": { status: 422, title: "Invalid agent bindings" },
  "uncast-agent-roles": { status: 422, title: "Agent roles need models" },
  "binding-constraint-refused": {
    status: 422,
    title: "Binding constraint refused",
  },
  "invalid-agent-attempt-id": {
    status: 400,
    title: "Invalid agent attempt id",
  },
  "agent-attempt-not-found": { status: 404, title: "Agent attempt not found" },
  "agent-attempt-not-current": {
    status: 409,
    title: "Agent attempt is not current",
  },
  "agent-attempt-cancellation-stale": {
    status: 409,
    title: "Agent attempt cancellation is stale",
  },
  "agent-attempt-terminal": { status: 409, title: "Agent attempt is terminal" },
  "cancellation-command-conflict": {
    status: 409,
    title: "Cancellation command conflict",
  },
  "replacement-not-allowed": {
    status: 409,
    title: "Replacement is not allowed",
  },
  "invalid-public-run-reference": {
    status: 400,
    title: "Invalid public run reference",
  },
  "invalid-public-project-reference": {
    status: 400,
    title: "Invalid public project reference",
  },
  "invalid-public-source-reference": {
    status: 400,
    title: "Invalid public source reference",
  },
  "invalid-event-cursor": { status: 400, title: "Invalid event cursor" },
  "invalid-revision-hash": { status: 400, title: "Invalid revision hash" },
  "event-cursor-run-mismatch": {
    status: 409,
    title: "Event cursor belongs to another run",
  },
  "event-cursor-ahead": {
    status: 409,
    title: "Event cursor is ahead of durable history",
  },
  "invalid-request": { status: 422, title: "Invalid request" },
  "invalid-base64": { status: 422, title: "Invalid base64" },
  "invalid-workflow-document": {
    status: 422,
    title: "Invalid workflow document",
  },
  "artifact-empty": { status: 422, title: "Artifact refused" },
  "artifact-too-large": { status: 422, title: "Artifact refused" },
  "invalid-artifact-hash": { status: 400, title: "Invalid artifact hash" },
  "artifact-not-found": { status: 404, title: "Artifact not found" },
  "adapter-operation-document-too-large": {
    status: 422,
    title: "Invalid adapter operation document",
  },
  "adapter-operation-document-not-utf8": {
    status: 422,
    title: "Invalid adapter operation document",
  },
  "adapter-operation-not-an-operation-object": {
    status: 422,
    title: "Invalid adapter operation document",
  },
  "adapter-operation-unknown-field": {
    status: 422,
    title: "Invalid adapter operation document",
  },
  "adapter-operation-missing-operation": {
    status: 422,
    title: "Invalid adapter operation document",
  },
  "adapter-operation-unknown-operation": {
    status: 422,
    title: "Invalid adapter operation document",
  },
  "adapter-operation-revision-collision": {
    status: 409,
    title: "Adapter operation revision collision",
  },
  "schema-document-too-large": {
    status: 422,
    title: "Invalid schema document",
  },
  "schema-document-not-utf8": { status: 422, title: "Invalid schema document" },
  "schema-document-carries-byte-order-mark": {
    status: 422,
    title: "Invalid schema document",
  },
  "schema-document-not-json": { status: 422, title: "Invalid schema document" },
  "schema-non-canonical-number": {
    status: 422,
    title: "Invalid schema document",
  },
  "schema-duplicate-object-key": {
    status: 422,
    title: "Invalid schema document",
  },
  "schema-document-too-deep": { status: 422, title: "Invalid schema document" },
  "schema-too-many-values": { status: 422, title: "Invalid schema document" },
  "schema-forbidden-keyword": { status: 422, title: "Invalid schema document" },
  "schema-nonlocal-reference": {
    status: 422,
    title: "Invalid schema document",
  },
  "schema-unresolvable-reference": {
    status: 422,
    title: "Invalid schema document",
  },
  "schema-non-terminating-reference-cycle": {
    status: 422,
    title: "Invalid schema document",
  },
  "schema-unsupported-dialect": {
    status: 422,
    title: "Invalid schema document",
  },
  "schema-not-a-schema": { status: 422, title: "Invalid schema document" },
  "schema-revision-collision": {
    status: 409,
    title: "Schema revision collision",
  },
  "schema-revision-not-found": {
    status: 404,
    title: "Schema revision not found",
  },
  "budget-document-too-large": {
    status: 422,
    title: "Invalid budget document",
  },
  "budget-document-not-utf8": { status: 422, title: "Invalid budget document" },
  "budget-not-a-budget-object": {
    status: 422,
    title: "Invalid budget document",
  },
  "budget-unknown-field": { status: 422, title: "Invalid budget document" },
  "budget-missing-attempt-deadline": {
    status: 422,
    title: "Invalid budget document",
  },
  "budget-value-not-a-positive-int64": {
    status: 422,
    title: "Invalid budget document",
  },
  "budget-revision-collision": {
    status: 409,
    title: "Budget revision collision",
  },
  "tool-document-too-large": {
    status: 422,
    title: "Invalid tool grant document",
  },
  "tool-document-not-utf8": {
    status: 422,
    title: "Invalid tool grant document",
  },
  "tool-not-a-grant-object": {
    status: 422,
    title: "Invalid tool grant document",
  },
  "tool-missing-capability": {
    status: 422,
    title: "Invalid tool grant document",
  },
  "tool-unknown-capability": {
    status: 422,
    title: "Invalid tool grant document",
  },
  "tool-unknown-field": { status: 422, title: "Invalid tool grant document" },
  "tool-grant-revision-collision": {
    status: 409,
    title: "Tool grant revision collision",
  },
  "agent-definition-document-not-utf8": {
    status: 422,
    title: "Invalid agent definition document",
  },
  "agent-definition-frontmatter-missing": {
    status: 422,
    title: "Invalid agent definition document",
  },
  "agent-definition-frontmatter-unterminated": {
    status: 422,
    title: "Invalid agent definition document",
  },
  "agent-definition-frontmatter-unparsable": {
    status: 422,
    title: "Invalid agent definition document",
  },
  "agent-definition-frontmatter-not-a-mapping": {
    status: 422,
    title: "Invalid agent definition document",
  },
  "agent-definition-field-unknown": {
    status: 422,
    title: "Invalid agent definition document",
  },
  "agent-definition-field-missing": {
    status: 422,
    title: "Invalid agent definition document",
  },
  "agent-definition-field-duplicated": {
    status: 422,
    title: "Invalid agent definition document",
  },
  "agent-definition-field-type-unexpected": {
    status: 422,
    title: "Invalid agent definition document",
  },
  "agent-definition-field-empty": {
    status: 422,
    title: "Invalid agent definition document",
  },
  "agent-definition-tool-duplicated": {
    status: 422,
    title: "Invalid agent definition document",
  },
  "agent-definition-too-many-tools": {
    status: 422,
    title: "Invalid agent definition document",
  },
  "agent-definition-system-prompt-missing": {
    status: 422,
    title: "Invalid agent definition document",
  },
  "agent-definition-document-too-large": {
    status: 422,
    title: "Invalid agent definition document",
  },
  "agent-definition-revision-collision": {
    status: 409,
    title: "Agent definition revision collision",
  },
  "agent-definition-revision-not-found": {
    status: 404,
    title: "Agent definition revision not found",
  },
  "library-document-ambiguous": {
    status: 422,
    title: "Document matches more than one library kind",
  },
  "unsupported-media-type": { status: 415, title: "Unsupported media type" },
  "not-acceptable": { status: 406, title: "Not acceptable" },
  "catalog-revision-unpublished": {
    status: 409,
    title: "Catalog revision is unpublished",
  },
  "catalog-name-held": { status: 409, title: "Catalog name is held" },
  "catalog-revision-owned": { status: 409, title: "Catalog revision is owned" },
  "project-unknown": { status: 404, title: "Project unknown" },
  "model-registry-missing": { status: 404, title: "Model registry not found" },
  "model-registry-revision-conflict": {
    status: 409,
    title: "Model registry revision conflict",
  },
  "model-registry-revision-collision": {
    status: 409,
    title: "Model registry revision collision",
  },
  "project-model-defaults-missing": {
    status: 404,
    title: "Project model defaults not found",
  },
  "project-model-defaults-revision-conflict": {
    status: 409,
    title: "Project model defaults revision conflict",
  },
  "project-model-defaults-revision-collision": {
    status: 409,
    title: "Project model defaults revision collision",
  },
  "catalog-lineage-missing": {
    status: 404,
    title: "Catalog lineage not found",
  },
  "catalog-name-not-found": { status: 404, title: "Catalog name not found" },
  "catalog-lineage-retired": { status: 410, title: "Catalog lineage retired" },
  "catalog-revision-not-a-member": {
    status: 409,
    title: "Catalog revision is not a member",
  },
  "invalid-catalog-position": {
    status: 400,
    title: "Invalid catalog position",
  },
  "workflow-revision-not-found": {
    status: 404,
    title: "Workflow revision not found",
  },
  "run-not-found": { status: 404, title: "Run not found" },
  "node-not-found": { status: 404, title: "Node not found" },
  "revision-collision": { status: 409, title: "Workflow revision collision" },
  "workflow-format-not-executable": {
    status: 409,
    title: "Workflow format is not executable",
  },
  "run-input-refused": { status: 422, title: "Run input refused" },
  "run-identity-conflict": { status: 409, title: "Run identity conflict" },
  "run-fork-origin-not-terminal": {
    status: 409,
    title: "Run fork origin is not terminal",
  },
  "run-fork-node-missing": {
    status: 409,
    title: "Run fork node is missing",
  },
  "run-fork-loop-unsupported": {
    status: 409,
    title: "Run fork loop is unsupported",
  },
  "run-fork-prefix-not-reusable": {
    status: 409,
    title: "Run fork prefix is not reusable",
  },
  "run-fork-command-conflict": {
    status: 409,
    title: "Run fork command conflict",
  },
  "answer-revision-conflict": {
    status: 409,
    title: "Answer revision conflict",
  },
  "answer-state-conflict": { status: 409, title: "Answer state conflict" },
  "reconciliation-target-missing": {
    status: 409,
    title: "Reconciliation target missing",
  },
  "reconciliation-stale": { status: 409, title: "Reconciliation is stale" },
  "reconciliation-command-conflict": {
    status: 409,
    title: "Reconciliation command conflict",
  },
  "reconciliation-determination-conflict": {
    status: 409,
    title: "Reconciliation determination conflict",
  },
  "reconciliation-rejected": {
    status: 409,
    title: "Reconciliation was rejected",
  },
  "run-not-cancellable": { status: 409, title: "Run is not cancellable" },
  "run-cancellation-command-conflict": {
    status: 409,
    title: "Run cancellation command conflict",
  },
  "run-cancellation-overtaken-by-success": {
    status: 409,
    title: "Run cancellation overtaken by success",
  },
  "project-source-not-connected": {
    status: 409,
    title: "Project source not connected",
  },
  "project-source-already-connected": {
    status: 409,
    title: "Project source already connected",
  },
  "project-source-unknown": {
    status: 404,
    title: "Project source unknown",
  },
  "project-source-disconnected": {
    status: 409,
    title: "Project source disconnected",
  },
  "project-source-invalid": {
    status: 422,
    title: "Project source invalid",
  },
  "project-source-token-refused": {
    status: 422,
    title: "Project source token refused",
  },
  "project-source-unavailable": {
    status: 503,
    title: "Project source unavailable",
  },
  "project-source-payload-malformed": {
    status: 502,
    title: "Project source payload malformed",
  },
  "queue-admission-revision-conflict": {
    status: 409,
    title: "Queue admission revision conflict",
  },
  "queue-admission-already-decided": {
    status: 409,
    title: "Queue item is already admitted",
  },
  "queue-admission-authority-refused": {
    status: 409,
    title: "Queue admission authority refused",
  },
  "queue-admission-proposal-required": {
    status: 409,
    title: "Queue admission requires a proposal",
  },
  "queue-policy-revision-conflict": {
    status: 409,
    title: "Queue policy revision conflict",
  },
  "queue-proposal-revision-conflict": {
    status: 409,
    title: "Queue proposal revision conflict",
  },
  "queue-proposal-already-decided": {
    status: 409,
    title: "Queue proposal already decided",
  },
  "queue-proposal-refused": {
    status: 422,
    title: "Queue proposal refused",
  },
  "route-not-found": { status: 404, title: "Route not found" },
  "method-not-allowed": { status: 405, title: "Method not allowed" },
  "temporarily-unavailable": { status: 503, title: "Temporarily unavailable" },
  "durable-projection-unrepresentable": {
    status: 500,
    title: "Durable projection cannot be represented",
  },
  "durable-state-corrupt": { status: 500, title: "Durable state is corrupt" },
  "answer-execution-stale": { status: 409, title: "Answer execution is stale" },
  "internal-error": { status: 500, title: "Internal error" },
} as const;

const problemSchema = z.discriminatedUnion("type", [
  problemVariant(
    "auth-profile-revision-conflict",
    problemDefinitions["auth-profile-revision-conflict"],
  ),
  problemVariant(
    "auth-profile-revision-collision",
    problemDefinitions["auth-profile-revision-collision"],
  ),
  problemVariant(
    "auth-profile-revision-not-found",
    problemDefinitions["auth-profile-revision-not-found"],
  ),
  problemVariant(
    "agent-executor-binding-unavailable",
    problemDefinitions["agent-executor-binding-unavailable"],
  ),
  problemVariant(
    "agent-configuration-revision-collision",
    problemDefinitions["agent-configuration-revision-collision"],
  ),
  problemVariant(
    "agent-configuration-revision-not-found",
    problemDefinitions["agent-configuration-revision-not-found"],
  ),
  problemVariant(
    "invalid-agent-bindings",
    problemDefinitions["invalid-agent-bindings"],
  ),
  problemVariant(
    "uncast-agent-roles",
    problemDefinitions["uncast-agent-roles"],
  ),
  problemVariant(
    "binding-constraint-refused",
    problemDefinitions["binding-constraint-refused"],
  ),
  problemVariant(
    "invalid-agent-attempt-id",
    problemDefinitions["invalid-agent-attempt-id"],
  ),
  problemVariant(
    "agent-attempt-not-found",
    problemDefinitions["agent-attempt-not-found"],
  ),
  problemVariant(
    "agent-attempt-not-current",
    problemDefinitions["agent-attempt-not-current"],
  ),
  problemVariant(
    "agent-attempt-cancellation-stale",
    problemDefinitions["agent-attempt-cancellation-stale"],
  ),
  problemVariant(
    "agent-attempt-terminal",
    problemDefinitions["agent-attempt-terminal"],
  ),
  problemVariant(
    "cancellation-command-conflict",
    problemDefinitions["cancellation-command-conflict"],
  ),
  problemVariant(
    "replacement-not-allowed",
    problemDefinitions["replacement-not-allowed"],
  ),
  problemVariant(
    "invalid-public-run-reference",
    problemDefinitions["invalid-public-run-reference"],
  ),
  problemVariant(
    "invalid-public-project-reference",
    problemDefinitions["invalid-public-project-reference"],
  ),
  problemVariant(
    "invalid-public-source-reference",
    problemDefinitions["invalid-public-source-reference"],
  ),
  problemVariant(
    "invalid-event-cursor",
    problemDefinitions["invalid-event-cursor"],
  ),
  problemVariant(
    "invalid-revision-hash",
    problemDefinitions["invalid-revision-hash"],
  ),
  problemVariant(
    "event-cursor-run-mismatch",
    problemDefinitions["event-cursor-run-mismatch"],
  ),
  problemVariant(
    "event-cursor-ahead",
    problemDefinitions["event-cursor-ahead"],
  ),
  problemVariant("invalid-request", problemDefinitions["invalid-request"]),
  problemVariant("invalid-base64", problemDefinitions["invalid-base64"]),
  problemVariant(
    "invalid-workflow-document",
    problemDefinitions["invalid-workflow-document"],
  ),
  problemVariant("artifact-empty", problemDefinitions["artifact-empty"]),
  problemVariant(
    "artifact-too-large",
    problemDefinitions["artifact-too-large"],
  ),
  problemVariant(
    "invalid-artifact-hash",
    problemDefinitions["invalid-artifact-hash"],
  ),
  problemVariant("artifact-not-found", problemDefinitions["artifact-not-found"]),
  problemVariant(
    "adapter-operation-document-too-large",
    problemDefinitions["adapter-operation-document-too-large"],
  ),
  problemVariant(
    "adapter-operation-document-not-utf8",
    problemDefinitions["adapter-operation-document-not-utf8"],
  ),
  problemVariant(
    "adapter-operation-not-an-operation-object",
    problemDefinitions["adapter-operation-not-an-operation-object"],
  ),
  problemVariant(
    "adapter-operation-unknown-field",
    problemDefinitions["adapter-operation-unknown-field"],
  ),
  problemVariant(
    "adapter-operation-missing-operation",
    problemDefinitions["adapter-operation-missing-operation"],
  ),
  problemVariant(
    "adapter-operation-unknown-operation",
    problemDefinitions["adapter-operation-unknown-operation"],
  ),
  problemVariant(
    "adapter-operation-revision-collision",
    problemDefinitions["adapter-operation-revision-collision"],
  ),
  problemVariant(
    "schema-document-too-large",
    problemDefinitions["schema-document-too-large"],
  ),
  problemVariant(
    "schema-document-not-utf8",
    problemDefinitions["schema-document-not-utf8"],
  ),
  problemVariant(
    "schema-document-carries-byte-order-mark",
    problemDefinitions["schema-document-carries-byte-order-mark"],
  ),
  problemVariant(
    "schema-document-not-json",
    problemDefinitions["schema-document-not-json"],
  ),
  problemVariant(
    "schema-non-canonical-number",
    problemDefinitions["schema-non-canonical-number"],
  ),
  problemVariant(
    "schema-duplicate-object-key",
    problemDefinitions["schema-duplicate-object-key"],
  ),
  problemVariant(
    "schema-document-too-deep",
    problemDefinitions["schema-document-too-deep"],
  ),
  problemVariant(
    "schema-too-many-values",
    problemDefinitions["schema-too-many-values"],
  ),
  problemVariant(
    "schema-forbidden-keyword",
    problemDefinitions["schema-forbidden-keyword"],
  ),
  problemVariant(
    "schema-nonlocal-reference",
    problemDefinitions["schema-nonlocal-reference"],
  ),
  problemVariant(
    "schema-unresolvable-reference",
    problemDefinitions["schema-unresolvable-reference"],
  ),
  problemVariant(
    "schema-non-terminating-reference-cycle",
    problemDefinitions["schema-non-terminating-reference-cycle"],
  ),
  problemVariant(
    "schema-unsupported-dialect",
    problemDefinitions["schema-unsupported-dialect"],
  ),
  problemVariant(
    "schema-not-a-schema",
    problemDefinitions["schema-not-a-schema"],
  ),
  problemVariant(
    "schema-revision-collision",
    problemDefinitions["schema-revision-collision"],
  ),
  problemVariant(
    "schema-revision-not-found",
    problemDefinitions["schema-revision-not-found"],
  ),
  problemVariant(
    "budget-document-too-large",
    problemDefinitions["budget-document-too-large"],
  ),
  problemVariant(
    "budget-document-not-utf8",
    problemDefinitions["budget-document-not-utf8"],
  ),
  problemVariant(
    "budget-not-a-budget-object",
    problemDefinitions["budget-not-a-budget-object"],
  ),
  problemVariant(
    "budget-unknown-field",
    problemDefinitions["budget-unknown-field"],
  ),
  problemVariant(
    "budget-missing-attempt-deadline",
    problemDefinitions["budget-missing-attempt-deadline"],
  ),
  problemVariant(
    "budget-value-not-a-positive-int64",
    problemDefinitions["budget-value-not-a-positive-int64"],
  ),
  problemVariant(
    "budget-revision-collision",
    problemDefinitions["budget-revision-collision"],
  ),
  problemVariant(
    "tool-document-too-large",
    problemDefinitions["tool-document-too-large"],
  ),
  problemVariant(
    "tool-document-not-utf8",
    problemDefinitions["tool-document-not-utf8"],
  ),
  problemVariant(
    "tool-not-a-grant-object",
    problemDefinitions["tool-not-a-grant-object"],
  ),
  problemVariant(
    "tool-missing-capability",
    problemDefinitions["tool-missing-capability"],
  ),
  problemVariant(
    "tool-unknown-capability",
    problemDefinitions["tool-unknown-capability"],
  ),
  problemVariant(
    "tool-unknown-field",
    problemDefinitions["tool-unknown-field"],
  ),
  problemVariant(
    "tool-grant-revision-collision",
    problemDefinitions["tool-grant-revision-collision"],
  ),
  problemVariant(
    "agent-definition-document-not-utf8",
    problemDefinitions["agent-definition-document-not-utf8"],
  ),
  problemVariant(
    "agent-definition-frontmatter-missing",
    problemDefinitions["agent-definition-frontmatter-missing"],
  ),
  problemVariant(
    "agent-definition-frontmatter-unterminated",
    problemDefinitions["agent-definition-frontmatter-unterminated"],
  ),
  problemVariant(
    "agent-definition-frontmatter-unparsable",
    problemDefinitions["agent-definition-frontmatter-unparsable"],
  ),
  problemVariant(
    "agent-definition-frontmatter-not-a-mapping",
    problemDefinitions["agent-definition-frontmatter-not-a-mapping"],
  ),
  problemVariant(
    "agent-definition-field-unknown",
    problemDefinitions["agent-definition-field-unknown"],
  ),
  problemVariant(
    "agent-definition-field-missing",
    problemDefinitions["agent-definition-field-missing"],
  ),
  problemVariant(
    "agent-definition-field-duplicated",
    problemDefinitions["agent-definition-field-duplicated"],
  ),
  problemVariant(
    "agent-definition-field-type-unexpected",
    problemDefinitions["agent-definition-field-type-unexpected"],
  ),
  problemVariant(
    "agent-definition-field-empty",
    problemDefinitions["agent-definition-field-empty"],
  ),
  problemVariant(
    "agent-definition-tool-duplicated",
    problemDefinitions["agent-definition-tool-duplicated"],
  ),
  problemVariant(
    "agent-definition-too-many-tools",
    problemDefinitions["agent-definition-too-many-tools"],
  ),
  problemVariant(
    "agent-definition-system-prompt-missing",
    problemDefinitions["agent-definition-system-prompt-missing"],
  ),
  problemVariant(
    "agent-definition-document-too-large",
    problemDefinitions["agent-definition-document-too-large"],
  ),
  problemVariant(
    "agent-definition-revision-collision",
    problemDefinitions["agent-definition-revision-collision"],
  ),
  problemVariant(
    "agent-definition-revision-not-found",
    problemDefinitions["agent-definition-revision-not-found"],
  ),
  problemVariant(
    "library-document-ambiguous",
    problemDefinitions["library-document-ambiguous"],
  ),
  problemVariant(
    "unsupported-media-type",
    problemDefinitions["unsupported-media-type"],
  ),
  problemVariant("not-acceptable", problemDefinitions["not-acceptable"]),
  problemVariant(
    "catalog-revision-unpublished",
    problemDefinitions["catalog-revision-unpublished"],
  ),
  problemVariant("catalog-name-held", problemDefinitions["catalog-name-held"]),
  problemVariant(
    "catalog-revision-owned",
    problemDefinitions["catalog-revision-owned"],
  ),
  problemVariant("project-unknown", problemDefinitions["project-unknown"]),
  problemVariant(
    "model-registry-missing",
    problemDefinitions["model-registry-missing"],
  ),
  problemVariant(
    "model-registry-revision-conflict",
    problemDefinitions["model-registry-revision-conflict"],
  ),
  problemVariant(
    "model-registry-revision-collision",
    problemDefinitions["model-registry-revision-collision"],
  ),
  problemVariant(
    "project-model-defaults-missing",
    problemDefinitions["project-model-defaults-missing"],
  ),
  problemVariant(
    "project-model-defaults-revision-conflict",
    problemDefinitions["project-model-defaults-revision-conflict"],
  ),
  problemVariant(
    "project-model-defaults-revision-collision",
    problemDefinitions["project-model-defaults-revision-collision"],
  ),
  problemVariant(
    "catalog-lineage-missing",
    problemDefinitions["catalog-lineage-missing"],
  ),
  problemVariant(
    "catalog-name-not-found",
    problemDefinitions["catalog-name-not-found"],
  ),
  problemVariant(
    "catalog-lineage-retired",
    problemDefinitions["catalog-lineage-retired"],
  ),
  problemVariant(
    "catalog-revision-not-a-member",
    problemDefinitions["catalog-revision-not-a-member"],
  ),
  problemVariant(
    "invalid-catalog-position",
    problemDefinitions["invalid-catalog-position"],
  ),
  problemVariant(
    "workflow-revision-not-found",
    problemDefinitions["workflow-revision-not-found"],
  ),
  problemVariant("run-not-found", problemDefinitions["run-not-found"]),
  problemVariant("node-not-found", problemDefinitions["node-not-found"]),
  problemVariant(
    "revision-collision",
    problemDefinitions["revision-collision"],
  ),
  problemVariant(
    "workflow-format-not-executable",
    problemDefinitions["workflow-format-not-executable"],
  ),
  problemVariant("run-input-refused", problemDefinitions["run-input-refused"]),
  problemVariant(
    "run-identity-conflict",
    problemDefinitions["run-identity-conflict"],
  ),
  problemVariant(
    "run-fork-origin-not-terminal",
    problemDefinitions["run-fork-origin-not-terminal"],
  ),
  problemVariant(
    "run-fork-node-missing",
    problemDefinitions["run-fork-node-missing"],
  ),
  problemVariant(
    "run-fork-loop-unsupported",
    problemDefinitions["run-fork-loop-unsupported"],
  ),
  problemVariant(
    "run-fork-prefix-not-reusable",
    problemDefinitions["run-fork-prefix-not-reusable"],
  ),
  problemVariant(
    "run-fork-command-conflict",
    problemDefinitions["run-fork-command-conflict"],
  ),
  problemVariant(
    "answer-revision-conflict",
    problemDefinitions["answer-revision-conflict"],
  ),
  problemVariant(
    "answer-state-conflict",
    problemDefinitions["answer-state-conflict"],
  ),
  problemVariant(
    "reconciliation-target-missing",
    problemDefinitions["reconciliation-target-missing"],
  ),
  problemVariant(
    "reconciliation-stale",
    problemDefinitions["reconciliation-stale"],
  ),
  problemVariant(
    "reconciliation-command-conflict",
    problemDefinitions["reconciliation-command-conflict"],
  ),
  problemVariant(
    "reconciliation-determination-conflict",
    problemDefinitions["reconciliation-determination-conflict"],
  ),
  problemVariant(
    "reconciliation-rejected",
    problemDefinitions["reconciliation-rejected"],
  ),
  problemVariant(
    "run-not-cancellable",
    problemDefinitions["run-not-cancellable"],
  ),
  problemVariant(
    "run-cancellation-command-conflict",
    problemDefinitions["run-cancellation-command-conflict"],
  ),
  problemVariant(
    "run-cancellation-overtaken-by-success",
    problemDefinitions["run-cancellation-overtaken-by-success"],
  ),
  problemVariant(
    "project-source-not-connected",
    problemDefinitions["project-source-not-connected"],
  ),
  problemVariant(
    "project-source-already-connected",
    problemDefinitions["project-source-already-connected"],
  ),
  problemVariant(
    "project-source-unknown",
    problemDefinitions["project-source-unknown"],
  ),
  problemVariant(
    "project-source-disconnected",
    problemDefinitions["project-source-disconnected"],
  ),
  problemVariant(
    "project-source-invalid",
    problemDefinitions["project-source-invalid"],
  ),
  problemVariant(
    "project-source-token-refused",
    problemDefinitions["project-source-token-refused"],
  ),
  problemVariant(
    "project-source-unavailable",
    problemDefinitions["project-source-unavailable"],
  ),
  problemVariant(
    "project-source-payload-malformed",
    problemDefinitions["project-source-payload-malformed"],
  ),
  problemVariant(
    "queue-admission-revision-conflict",
    problemDefinitions["queue-admission-revision-conflict"],
  ),
  problemVariant(
    "queue-admission-already-decided",
    problemDefinitions["queue-admission-already-decided"],
  ),
  problemVariant(
    "queue-admission-authority-refused",
    problemDefinitions["queue-admission-authority-refused"],
  ),
  problemVariant(
    "queue-admission-proposal-required",
    problemDefinitions["queue-admission-proposal-required"],
  ),
  problemVariant(
    "queue-policy-revision-conflict",
    problemDefinitions["queue-policy-revision-conflict"],
  ),
  problemVariant(
    "queue-proposal-revision-conflict",
    problemDefinitions["queue-proposal-revision-conflict"],
  ),
  problemVariant(
    "queue-proposal-already-decided",
    problemDefinitions["queue-proposal-already-decided"],
  ),
  problemVariant(
    "queue-proposal-refused",
    problemDefinitions["queue-proposal-refused"],
  ),
  problemVariant("route-not-found", problemDefinitions["route-not-found"]),
  problemVariant(
    "method-not-allowed",
    problemDefinitions["method-not-allowed"],
  ),
  problemVariant(
    "temporarily-unavailable",
    problemDefinitions["temporarily-unavailable"],
  ),
  problemVariant(
    "durable-projection-unrepresentable",
    problemDefinitions["durable-projection-unrepresentable"],
  ),
  problemVariant(
    "durable-state-corrupt",
    problemDefinitions["durable-state-corrupt"],
  ),
  problemVariant(
    "answer-execution-stale",
    problemDefinitions["answer-execution-stale"],
  ),
  problemVariant("internal-error", problemDefinitions["internal-error"]),
]);

const streamFailureSchema = z
  .object({ event: z.literal("STREAM_FAILED"), problem: problemSchema })
  .strict();

const durableStateCorruptProblemSchema = problemVariant(
  "durable-state-corrupt",
  problemDefinitions["durable-state-corrupt"],
);

const runProjectionCorruptSchema = z
  .object({
    event: z.literal("RUN_PROJECTION_CORRUPT"),
    public_run_reference: publicRunReference,
    problem: durableStateCorruptProblemSchema,
  })
  .strict();

const streamFrameSchema = z.union([
  streamFailureSchema,
  runProjectionCorruptSchema,
  runEventSchema,
]);

export type Problem = z.infer<typeof problemSchema>;
export type StreamFailure = z.infer<typeof streamFailureSchema>;
export type RunProjectionCorrupt = z.infer<typeof runProjectionCorruptSchema>;
export type StreamFrame = z.infer<typeof streamFrameSchema>;
export type RunV3 = z.infer<typeof runV3Schema>;
export type RunEvent = z.infer<typeof runEventSchema>;
export type WorkflowRevisionDetail = z.infer<
  typeof workflowRevisionDetailSchema
>;
export type RunPage = z.infer<typeof runPageSchema>;
export type WorkflowRevisionPage = z.infer<typeof workflowRevisionPageSchema>;
export type WorkflowRevisionSummary = z.infer<
  typeof workflowRevisionSummarySchema
>;
type AgentDefinitionRevisionDetail = z.infer<
  typeof agentDefinitionRevisionDetailSchema
>;
type CatalogNameResolution = z.infer<typeof catalogNameResolutionSchema>;
type CatalogAdmission = z.infer<typeof catalogAdmissionSchema>;
export type LibraryRecognition = z.infer<typeof libraryRecognitionSchema>;
export type CatalogIntakeKind = z.infer<typeof catalogIntakeKindSchema>;
type LibraryAddition = z.infer<typeof libraryAdditionSchema>;
type ProjectList = z.infer<typeof projectListSchema>;
type ProjectSourceConnectionRevision = z.infer<
  typeof projectSourceConnectionRevisionSchema
>;
export type ProjectSourceResource = z.infer<typeof projectSourceResourceSchema>;
type ProjectSourceList = z.infer<typeof projectSourceListSchema>;
export type ModelRegistryRevision = z.infer<typeof modelRegistryRevisionSchema>;
export type ProjectModelDefaultsRevision = z.infer<
  typeof projectModelDefaultsRevisionSchema
>;
export type ProjectModelResolution = z.infer<
  typeof projectModelResolutionSchema
>;

interface ProjectModelDefaultsRevisionInput {
  revision_number: number;
  defaults: ProjectModelDefaultsRevision["defaults"];
}

interface ModelRegistryRevisionInput {
  revision_number: number;
  entries: Array<
    Pick<
      ModelRegistryRevision["entries"][number],
      "model_id" | "agent_configuration_revision_hash"
    >
  >;
}

export interface ExactModelRegistryRevisionWrite {
  input: ModelRegistryRevisionInput;
  body: string;
}

export interface ExactProjectModelDefaultsRevisionWrite {
  input: ProjectModelDefaultsRevisionInput;
  body: string;
}

type AuthProfileInput = z.infer<typeof authProfileInputSchema>;
export type AuthProfileRevision = z.infer<typeof authProfileRevisionSchema>;
export type AuthProfileRevisionPage = z.infer<
  typeof authProfileRevisionPageSchema
>;
export type AgentConfigurationInput = z.infer<
  typeof agentConfigurationInputSchema
>;
export type AgentConfigurationRevision = z.infer<
  typeof agentConfigurationRevisionSchema
>;
export type AgentConfigurationRevisionListItem = z.infer<
  typeof agentConfigurationRevisionListItemSchema
>;
type AgentDefinitionRevision = z.infer<
  typeof agentDefinitionRevisionSchema
>;
export type AgentDefinitionRevisionListItem = z.infer<
  typeof agentDefinitionRevisionListItemSchema
>;
type AgentDefinitionRevisionPage = z.infer<
  typeof agentDefinitionRevisionPageSchema
>;
export type AgentConfigurationRevisionPage = z.infer<
  typeof agentConfigurationRevisionPageSchema
>;
export type ObservedQueueItem = z.infer<typeof observedQueueItemSchema>;
/** What the queue page keeps after this client filters the served page. */
export type ObservedQueueItemPage = {
  items: ObservedQueueItem[];
  next_after: string | null;
};

interface HttpResult<T> {
  status: number;
  value: T;
}

export interface CockpitApi {
  /** The cheap read #700's bounded recovery probe reuses -- no purpose-built endpoint. */
  health(signal?: AbortSignal): Promise<HealthResource>;
  listRuns(after?: string, state?: RunV3["state"]): Promise<RunPage>;
  listProjects(): Promise<ProjectList>;
  getProjectSourceConnection(
    publicProjectReference: string,
  ): Promise<ProjectSourceConnectionRevision>;
  listProjectSources(publicProjectReference: string): Promise<ProjectSourceList>;
  connectProjectSource(
    publicProjectReference: string,
    request: { address: string; token: string },
  ): Promise<ProjectSourceResource>;
  disconnectProjectSource(
    publicProjectReference: string,
    publicSourceReference: string,
  ): Promise<void>;
  rotateProjectSourceToken(
    publicProjectReference: string,
    publicSourceReference: string,
    request: { token: string },
  ): Promise<ProjectSourceResource>;
  getModelRegistry(providerId: string): Promise<ModelRegistryRevision>;
  putModelRegistry(
    providerId: string,
    write: ExactModelRegistryRevisionWrite,
  ): Promise<HttpResult<ModelRegistryRevision>>;
  validateModelRegistryEntry(
    providerId: string,
    agentConfigurationRevisionHash: string,
  ): Promise<HttpResult<ModelRegistryRevision>>;
  getProjectModelDefaults(
    publicProjectReference: string,
  ): Promise<ProjectModelDefaultsRevision>;
  putProjectModelDefaults(
    publicProjectReference: string,
    write: ExactProjectModelDefaultsRevisionWrite,
  ): Promise<HttpResult<ProjectModelDefaultsRevision>>;
  resolveProjectModels(
    publicProjectReference: string,
    workflowRevisionHash: string,
    overrides: Array<{
      role: string;
      agent_configuration_revision_hash: string;
    }>,
  ): Promise<ProjectModelResolution>;
  listWorkflowRevisions(after?: string): Promise<WorkflowRevisionPage>;
  listAgentConfigurationRevisions(
    after?: string,
  ): Promise<AgentConfigurationRevisionPage>;
  listAuthProfileRevisions(after?: string): Promise<AuthProfileRevisionPage>;
  listAgentDefinitionRevisions(
    after?: string,
  ): Promise<AgentDefinitionRevisionPage>;
  recognizeLibraryDocument(
    document: Uint8Array,
    fileName: string | null,
  ): Promise<LibraryRecognition>;
  addLibraryDocument(
    document: Uint8Array,
    kind: CatalogIntakeKind,
    actor: string,
    activatedAt: string,
  ): Promise<HttpResult<LibraryAddition>>;
  publishAgentDefinition(
    document: string,
  ): Promise<HttpResult<AgentDefinitionRevision>>;
  getAgentDefinitionRevision(
    revisionHash: string,
  ): Promise<AgentDefinitionRevisionDetail>;
  /**
   * Publish material by its exact bytes and answer with the address it
   * hashes to (`POST /artifacts`, #1089). A caller may hand text, which
   * publishes as its exact UTF-8 encoding -- the same content-addressed door
   * either way, so publishing the same bytes twice answers the same hash.
   */
  publishArtifact(content: Uint8Array | string): Promise<HttpResult<ArtifactResource>>;
  listObservedQueueItems(after?: string): Promise<ObservedQueueItemPage>;
  getRevisionByName(
    kind: CatalogLineageKind,
    name: string,
  ): Promise<CatalogNameResolution>;
  foundCatalogLineage(
    input: CatalogAdmissionInput,
  ): Promise<HttpResult<CatalogAdmission>>;
  admitCatalogMember(
    lineageId: string,
    input: CatalogAdmissionInput,
  ): Promise<HttpResult<CatalogAdmission>>;
  retireCatalogLineage(
    lineageId: string,
    input: CatalogRetirementInput,
  ): Promise<void>;
  publish(
    mutation: PublishMutation,
  ): Promise<HttpResult<WorkflowRevisionDetail>>;
  publishAuthProfile(
    input: AuthProfileInput,
  ): Promise<HttpResult<AuthProfileRevision>>;
  publishAgentConfiguration(
    input: AgentConfigurationInput,
  ): Promise<HttpResult<AgentConfigurationRevision>>;
  start(mutation: StartMutation): Promise<HttpResult<RunV3>>;
  answer(mutation: WaitMutation): Promise<HttpResult<RunV3>>;
  cancelRun(mutation: CancelMutation): Promise<HttpResult<RunV3>>;
  forkRun(request: {
    publicRunReference: string;
    idempotencyKey: string;
    restartFromNodeId: string;
  }): Promise<HttpResult<RunV3>>;
  getRun(publicReference: string): Promise<RunV3>;
  getNodeDetail(publicReference: string, nodeId: string): Promise<NodeDetail>;
  getWorkflowRevision(revisionHash: string): Promise<WorkflowRevisionDetail>;
  getSchemaRevision(schemaRevisionHash: string): Promise<JsonSchemaDocument>;
  openRunEvents(
    publicReference: string,
    handlers: RunEventHandlers,
  ): RunEventSubscription;
  openAttentionEvents(handlers: RunEventHandlers): RunEventSubscription;
}

export interface RunEventHandlers {
  opened(): void;
  event(rawData: string): void;
  disconnected(): void;
}

export interface RunEventSubscription {
  close(): void;
}

interface EventSourcePort extends RunEventSubscription {
  addEventListener(type: string, listener: EventListener): void;
}

export type EventSourceFactory = (target: string) => EventSourcePort;

export class CockpitRequestError extends Error {
  constructor(
    message: string,
    readonly problem: Problem | null = null,
    readonly definitive_failure = false,
    /** The round trip itself never happened -- not a 4xx/5xx the server
     * answered with, and not a contract violation in what it did answer
     * (#700). The one throw site that catches a failed `fetch` sets this;
     * every other one names a specific violation whose own message stays
     * worth reading. */
    readonly transport_failure = false,
  ) {
    super(message);
  }
}

function runListTarget(after?: string, state?: RunV3["state"]): string {
  const query = new URLSearchParams({ limit: "50" });
  if (after !== undefined) query.set("after", after);
  if (state !== undefined) query.set("state", state);
  return `/atelier/api/v1/runs?${query.toString()}`;
}

export function createCockpitApi(
  fetcher: typeof fetch = globalThis.fetch,
  eventSourceFactory: EventSourceFactory = (target) => new EventSource(target),
): CockpitApi {
  return {
    health: (signal?: AbortSignal) =>
      requestJson(
        fetcher,
        "/atelier/api/v1/health",
        { signal },
        [200],
        HealthResource,
      ),
    listRuns: (after?: string, state?: RunV3["state"]) =>
      requestJson(
        fetcher,
        runListTarget(after, state),
        {},
        [200],
        runPageSchema,
      ),
    listProjects: () =>
      requestJson(
        fetcher,
        "/atelier/api/v1/projects",
        {},
        [200],
        projectListSchema,
      ),
    getProjectSourceConnection: async (publicProjectReference) => {
      const connection = await requestJson(
        fetcher,
        `/atelier/api/v1/projects/${encodeURIComponent(publicProjectReference)}/source-connection`,
        {},
        [200],
        projectSourceConnectionRevisionSchema,
      );
      if (connection.public_project_reference !== publicProjectReference) {
        throw new CockpitRequestError(
          "The source connection named another project.",
        );
      }
      return connection;
    },
    listProjectSources: (publicProjectReference) =>
      requestJson(
        fetcher,
        `/atelier/api/v1/projects/${encodeURIComponent(publicProjectReference)}/sources`,
        {},
        [200],
        projectSourceListSchema,
      ),
    connectProjectSource: (publicProjectReference, request) =>
      requestJson(
        fetcher,
        `/atelier/api/v1/projects/${encodeURIComponent(publicProjectReference)}/sources`,
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            address: request.address,
            token: request.token,
          }),
        },
        [201],
        projectSourceResourceSchema,
      ),
    disconnectProjectSource: async (
      publicProjectReference,
      publicSourceReference,
    ) => {
      await requestJson(
        fetcher,
        `/atelier/api/v1/projects/${encodeURIComponent(publicProjectReference)}/sources/${encodeURIComponent(publicSourceReference)}`,
        { method: "DELETE" },
        [204],
        z.undefined(),
      );
    },
    rotateProjectSourceToken: (
      publicProjectReference,
      publicSourceReference,
      request,
    ) =>
      requestJson(
        fetcher,
        `/atelier/api/v1/projects/${encodeURIComponent(publicProjectReference)}/sources/${encodeURIComponent(publicSourceReference)}/token`,
        {
          method: "PUT",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ token: request.token }),
        },
        [200],
        projectSourceResourceSchema,
      ),
    getModelRegistry: async (providerId) => {
      const exactProviderId = providerIdSchema.parse(providerId);
      const registry = await requestJson(
        fetcher,
        `/atelier/api/v1/model-registries/${encodeURIComponent(exactProviderId)}`,
        {},
        [200],
        modelRegistryRevisionSchema,
      );
      if (registry.provider_id !== exactProviderId) {
        throw new CockpitRequestError(
          "The model registry response named another provider.",
        );
      }
      return registry;
    },
    putModelRegistry: async (providerId, write) => {
      const exactProviderId = providerIdSchema.parse(providerId);
      if (write.body !== JSON.stringify(write.input)) {
        throw new CockpitRequestError(
          "The frozen model-registry bytes did not name the sent input.",
        );
      }
      const result = await requestJsonResult(
        fetcher,
        `/atelier/api/v1/model-registries/${encodeURIComponent(exactProviderId)}`,
        {
          method: "PUT",
          headers: { "content-type": "application/json" },
          body: write.body,
        },
        [200, 201],
        modelRegistryRevisionSchema,
      );
      if (result.value.provider_id !== exactProviderId) {
        throw new CockpitRequestError(
          "The model registry response named another provider.",
        );
      }
      return result;
    },
    validateModelRegistryEntry: async (
      providerId,
      agentConfigurationRevisionHash,
    ) => {
      const exactProviderId = providerIdSchema.parse(providerId);
      const exactConfigurationHash = sha256.parse(
        agentConfigurationRevisionHash,
      );
      const result = await requestJsonResult(
        fetcher,
        `/atelier/api/v1/model-registries/${encodeURIComponent(exactProviderId)}/validations`,
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            agent_configuration_revision_hash: exactConfigurationHash,
          }),
        },
        [200, 201],
        modelRegistryRevisionSchema,
      );
      if (result.value.provider_id !== exactProviderId) {
        throw new CockpitRequestError(
          "The model registry response named another provider.",
        );
      }
      return result;
    },
    getProjectModelDefaults: async (publicProjectReference) => {
      const defaults = await requestJson(
        fetcher,
        `/atelier/api/v1/projects/${encodeURIComponent(publicProjectReference)}` +
          "/model-defaults",
        {},
        [200],
        projectModelDefaultsRevisionSchema,
      );
      if (defaults.public_project_reference !== publicProjectReference) {
        throw new CockpitRequestError(
          "The model defaults response named another project.",
        );
      }
      return defaults;
    },
    putProjectModelDefaults: async (publicProjectReference, write) => {
      if (write.body !== JSON.stringify(write.input)) {
        throw new CockpitRequestError(
          "The frozen model-default bytes did not name the sent input.",
        );
      }
      const result = await requestJsonResult(
        fetcher,
        `/atelier/api/v1/projects/${encodeURIComponent(publicProjectReference)}` +
          "/model-defaults",
        {
          method: "PUT",
          headers: { "content-type": "application/json" },
          body: write.body,
        },
        [200, 201],
        projectModelDefaultsRevisionSchema,
      );
      if (result.value.public_project_reference !== publicProjectReference) {
        throw new CockpitRequestError(
          "The model defaults response named another project.",
        );
      }
      return result;
    },
    resolveProjectModels: async (
      projectReference,
      workflowRevisionHash,
      overrides,
    ) => {
      const exactProjectReference =
        publicProjectReference.parse(projectReference);
      const exactWorkflowRevisionHash = sha256.parse(workflowRevisionHash);
      const resolution = await requestJson(
        fetcher,
        `/atelier/api/v1/projects/${encodeURIComponent(exactProjectReference)}/model-resolution`,
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            workflow_revision_hash: exactWorkflowRevisionHash,
            overrides,
          }),
        },
        [200],
        projectModelResolutionSchema,
      );
      if (resolution.public_project_reference !== exactProjectReference) {
        throw new CockpitRequestError(
          "The model resolution response named another project.",
        );
      }
      if (resolution.workflow_revision_hash !== exactWorkflowRevisionHash) {
        throw new CockpitRequestError(
          "The model resolution response named another workflow.",
        );
      }
      return resolution;
    },
    listWorkflowRevisions: (after?: string) =>
      requestJson(
        fetcher,
        after === undefined
          ? "/atelier/api/v1/workflow-revisions?limit=50&view=described"
          : `/atelier/api/v1/workflow-revisions?limit=50&view=described&after=${encodeURIComponent(after)}`,
        {},
        [200],
        workflowRevisionPageSchema,
      ),
    listAgentConfigurationRevisions: (after?: string) =>
      requestJson(
        fetcher,
        after === undefined
          ? "/atelier/api/v1/agent-configuration-revisions?limit=50"
          : `/atelier/api/v1/agent-configuration-revisions?limit=50&after_revision_hash=${encodeURIComponent(after)}`,
        {},
        [200],
        agentConfigurationRevisionPageSchema,
      ),
    listAuthProfileRevisions: (after?: string) =>
      requestJson(
        fetcher,
        after === undefined
          ? "/atelier/api/v1/auth-profile-revisions?limit=50"
          : `/atelier/api/v1/auth-profile-revisions?limit=50&after_revision_hash=${encodeURIComponent(after)}`,
        {},
        [200],
        authProfileRevisionPageSchema,
      ),
    listObservedQueueItems: (after?: string) =>
      requestJson(
        fetcher,
        after === undefined
          ? "/atelier/api/v1/queue-items?limit=50"
          : `/atelier/api/v1/queue-items?limit=50&after=${encodeURIComponent(after)}`,
        {},
        [200],
        queueItemPageSchema
      ).then((page) => ({
        items: page.items
          .filter((item) => item.state === "OBSERVED")
          .map((item) => ({
            project_id: item.project_id,
            tracker_item_reference: item.tracker_item_reference,
            item_id: item.item_id,
            revision: item.revision,
            title: item.title,
            title_observed_at: item.title_observed_at,
            retired_at: item.retired_at
          })),
        next_after: page.next_after
      })),
    listAgentDefinitionRevisions: (after?: string) =>
      requestJson(
        fetcher,
        after === undefined
          ? "/atelier/api/v1/agent-definition-revisions?limit=50"
          : `/atelier/api/v1/agent-definition-revisions?limit=50&after_revision_hash=${encodeURIComponent(after)}`,
        {},
        [200],
        agentDefinitionRevisionPageSchema,
      ),
    recognizeLibraryDocument: (document, fileName) =>
      requestJson(
        fetcher,
        libraryDocumentTarget("/atelier/api/v1/library/recognitions", fileName),
        {
          method: "POST",
          headers: { "content-type": "application/octet-stream" },
          body: opaqueDocumentBody(document),
        },
        [200],
        libraryRecognitionSchema,
      ),
    addLibraryDocument: (document, kind, actor, activatedAt) =>
      requestJsonResult(
        fetcher,
        libraryAdditionTarget(kind, actor, activatedAt),
        {
          method: "POST",
          headers: { "content-type": "application/octet-stream" },
          body: opaqueDocumentBody(document),
        },
        [200, 201],
        libraryAdditionSchema,
      ),
    // The authored file travels as the exact bytes the author wrote, so the
    // hash the store answers with is the hash of what is on their disk.
    publishAgentDefinition: (document: string) =>
      requestJsonResult(
        fetcher,
        "/atelier/api/v1/agent-definition-revisions",
        {
          method: "POST",
          headers: { "content-type": "text/markdown" },
          body: new TextEncoder().encode(document),
        },
        [200, 201],
        agentDefinitionRevisionSchema,
      ),
    getAgentDefinitionRevision: (revisionHash: string) =>
      requestJson(
        fetcher,
        `/atelier/api/v1/agent-definition-revisions/${encodeURIComponent(revisionHash)}`,
        {},
        [200],
        agentDefinitionRevisionDetailSchema,
      ),
    publishArtifact: (content) =>
      requestJsonResult(
        fetcher,
        "/atelier/api/v1/artifacts",
        {
          method: "POST",
          headers: { "content-type": "application/octet-stream" },
          body: opaqueDocumentBody(
            typeof content === "string" ? new TextEncoder().encode(content) : content,
          ),
        },
        [200, 201],
        artifactResourceSchema,
      ),
    getRevisionByName: async (kind: CatalogLineageKind, name: string) => {
      const resolution = await requestJson(
        fetcher,
        `/atelier/api/v1/catalog-revisions/by-name/${kind}/${encodeURIComponent(name)}`,
        {},
        [200],
        catalogNameResolutionSchema,
      );
      if (resolution.display_name !== name) {
        throw new CockpitRequestError(
          "The catalog response named another display name.",
        );
      }
      return resolution;
    },
    foundCatalogLineage: (input) =>
      requestJsonResult(
        fetcher,
        "/atelier/api/v1/catalog-lineages",
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            kind: input.kind,
            catalog_revision_hash: input.catalog_revision_hash,
            actor: input.actor,
            activated_at: input.activated_at,
          }),
        },
        [200, 201],
        catalogAdmissionSchema,
      ),
    admitCatalogMember: (lineageId, input) =>
      requestJsonResult(
        fetcher,
        `/atelier/api/v1/catalog-lineages/${encodeURIComponent(lineageId)}/members`,
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            kind: input.kind,
            catalog_revision_hash: input.catalog_revision_hash,
            actor: input.actor,
            activated_at: input.activated_at,
          }),
        },
        [200, 201],
        catalogAdmissionSchema,
      ),
    retireCatalogLineage: (lineageId, input) =>
      requestJson(
        fetcher,
        `/atelier/api/v1/catalog-lineages/${encodeURIComponent(lineageId)}/retirements`,
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            actor: input.actor,
            activated_at: input.activated_at,
          }),
        },
        [204],
        z.undefined(),
      ),
    publish: async (mutation) =>
      requestJsonResult(
        fetcher,
        mutation.target,
        {
          method: "POST",
          headers: { "content-type": "application/yaml" },
          body: exactBody(mutation.body_base64),
        },
        [200, 201],
        workflowRevisionDetailSchema,
      ),
    publishAuthProfile: async (input) =>
      requestJsonResult(
        fetcher,
        "/atelier/api/v1/auth-profile-revisions",
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(authProfileInputSchema.parse(input)),
        },
        [200, 201],
        authProfileRevisionSchema,
      ),
    publishAgentConfiguration: async (input) =>
      requestJsonResult(
        fetcher,
        "/atelier/api/v1/agent-configuration-revisions",
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(agentConfigurationInputSchema.parse(input)),
        },
        [200, 201],
        agentConfigurationRevisionSchema,
      ),
    start: async (mutation) =>
      requestJsonResult(
        fetcher,
        mutation.target,
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: exactBody(mutation.body_base64),
        },
        [200, 201],
        runV3Schema,
      ),
    answer: async (mutation) => {
      const result = await requestJsonResult(
        fetcher,
        mutation.target,
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: exactBody(mutation.body_base64),
        },
        [202],
        runV3Schema,
      );
      if (
        result.value.public_run_reference !== mutation.public_run_reference ||
        result.value.workflow_revision_hash !== mutation.workflow_revision_hash
      ) {
        throw new CockpitRequestError(
          "The answer response did not match the exact durable run.",
        );
      }
      return result;
    },
    cancelRun: async (mutation) => {
      const result = await requestJsonResult(
        fetcher,
        mutation.target,
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: exactBody(mutation.body_base64),
        },
        [200, 202],
        runV3Schema,
      );
      if (result.value.public_run_reference !== mutation.public_run_reference) {
        throw new CockpitRequestError(
          "The cancel response named a different run than the one it was for.",
        );
      }
      return { status: result.status, value: result.value };
    },
    forkRun: async ({ publicRunReference, idempotencyKey, restartFromNodeId }) => {
      const result = await requestJsonResult(
        fetcher,
        `/atelier/api/v1/runs/${encodeURIComponent(publicRunReference)}/forks`,
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            idempotency_key: idempotencyKey,
            restart_from_node_id: restartFromNodeId,
          }),
        },
        [200, 201],
        runV3Schema,
      );
      return { status: result.status, value: result.value };
    },
    getRun: (publicReference) =>
      requestJson(
        fetcher,
        `/atelier/api/v1/runs/${encodeURIComponent(publicReference)}`,
        {},
        [200],
        runV3Schema,
      ),
    getNodeDetail: async (publicReference, nodeId) => {
      const detail = await requestJson(
        fetcher,
        `/atelier/api/v1/runs/${encodeURIComponent(publicReference)}` +
          `/nodes/${encodeURIComponent(nodeId)}`,
        {},
        [200],
        nodeDetailSchema,
      );
      if (detail.node_id !== nodeId) {
        throw new CockpitRequestError("The node response named another node.");
      }
      return detail;
    },
    getWorkflowRevision: async (revisionHash) => {
      const revision = await requestJson(
        fetcher,
        `/atelier/api/v1/workflow-revisions/${encodeURIComponent(revisionHash)}`,
        {},
        [200],
        workflowRevisionDetailSchema,
      );
      if (revision.workflow_revision_hash !== revisionHash) {
        throw new CockpitRequestError(
          "The workflow response did not match the requested revision.",
        );
      }
      return revision;
    },
    getSchemaRevision: (schemaRevisionHash) =>
      requestJson(
        fetcher,
        `/atelier/api/v1/schema-revisions/${encodeURIComponent(schemaRevisionHash)}`,
        {},
        [200],
        jsonSchemaDocumentSchema,
      ),
    openRunEvents: (publicReference, handlers) => {
      if (decodePublicRunReference(publicReference) === null) {
        throw new CockpitRequestError(
          "The run event target was not a valid public reference.",
        );
      }
      return subscribeEventSource(
        eventSourceFactory(
          `/atelier/api/v1/runs/${encodeURIComponent(publicReference)}/events`,
        ),
        handlers,
      );
    },
    openAttentionEvents: (handlers) =>
      subscribeEventSource(
        eventSourceFactory("/atelier/api/v1/events"),
        handlers,
      ),
  };
}

function libraryDocumentTarget(path: string, fileName: string | null): string {
  if (fileName === null) return path;
  return `${path}?${new URLSearchParams({ file_name: fileName }).toString()}`;
}

function libraryAdditionTarget(
  kind: CatalogIntakeKind,
  actor: string,
  activatedAt: string,
): string {
  return `/atelier/api/v1/library/additions?${new URLSearchParams({
    kind,
    actor,
    activated_at: activatedAt,
  }).toString()}`;
}

function opaqueDocumentBody(document: Uint8Array): ArrayBuffer {
  return document.slice().buffer as ArrayBuffer;
}

function subscribeEventSource(
  source: EventSourcePort,
  handlers: RunEventHandlers,
): RunEventSubscription {
  source.addEventListener("open", () => {
    reportConnectionRestored();
    handlers.opened();
  });
  source.addEventListener("message", (event) => {
    if (event instanceof MessageEvent && typeof event.data === "string") {
      handlers.event(event.data);
    }
  });
  source.addEventListener("error", () => {
    // The browser's own EventSource already retries; this only names the
    // fact centrally (#700) so every surface reads it, not just this stream.
    reportConnectionLost();
    handlers.disconnected();
  });
  return source;
}

export function decodeProblem(value: unknown): Problem {
  return problemSchema.parse(value);
}

export function decodeStreamFrame(value: unknown): StreamFrame {
  return streamFrameSchema.parse(value);
}

export function isStreamFailure(frame: StreamFrame): frame is StreamFailure {
  return frame.event === "STREAM_FAILED";
}

export function isRunProjectionCorrupt(
  frame: StreamFrame,
): frame is RunProjectionCorrupt {
  return frame.event === "RUN_PROJECTION_CORRUPT";
}

async function requestJson<T>(
  fetcher: typeof fetch,
  target: string,
  init: RequestInit,
  acceptedStatuses: readonly number[],
  schema: z.ZodType<T>,
): Promise<T> {
  return (
    await requestJsonResult(fetcher, target, init, acceptedStatuses, schema)
  ).value;
}

async function requestJsonResult<T>(
  fetcher: typeof fetch,
  target: string,
  init: RequestInit,
  acceptedStatuses: readonly number[],
  schema: z.ZodType<T>,
): Promise<HttpResult<T>> {
  let response: Response;
  try {
    response = await fetcher(target, {
      ...init,
      headers: { accept: "application/json", ...init.headers },
    });
  } catch (error) {
    // The round trip itself never happened -- a redeploy's outage (#700), not
    // a 4xx/5xx the server actually answered with, so this is the one signal
    // that means the workshop cannot be reached at all.
    reportConnectionLost();
    throw new CockpitRequestError(errorMessage(error), null, false, true);
  }
  reportConnectionRestored();
  // Disconnect answers 204 with an empty body; JSON parsing would invent a failure.
  if (response.status === 204) {
    if (!acceptedStatuses.includes(204)) {
      throw new CockpitRequestError(
        `The API returned undocumented HTTP ${response.status}.`,
      );
    }
    try {
      return { status: 204, value: schema.parse(undefined) };
    } catch {
      throw new CockpitRequestError(
        "The API response did not match the durable wire contract.",
      );
    }
  }
  let value: unknown;
  try {
    value = await response.json();
  } catch {
    throw new CockpitRequestError("The API response was not valid JSON.");
  }
  if (!acceptedStatuses.includes(response.status)) {
    try {
      const problem = decodeProblem(value);
      if (problem.status !== response.status) {
        throw new CockpitRequestError(
          "The problem body disagreed with the HTTP status.",
        );
      }
      throw new CockpitRequestError(
        problem.detail,
        problem,
        problem.status < 500 || problem.type.endsWith(":durable-state-corrupt"),
      );
    } catch (error) {
      if (error instanceof CockpitRequestError) throw error;
      throw new CockpitRequestError(
        `The API returned undocumented HTTP ${response.status}.`,
      );
    }
  }
  try {
    return { status: response.status, value: schema.parse(value) };
  } catch {
    throw new CockpitRequestError(
      "The API response did not match the durable wire contract.",
    );
  }
}

function exactBody(bodyBase64: string): ArrayBuffer {
  const bytes = decodeCanonicalBase64(bodyBase64);
  if (bytes === null) {
    throw new CockpitRequestError("The saved exact request bytes are corrupt.");
  }
  const copy = new Uint8Array(bytes.byteLength);
  copy.set(bytes);
  return copy.buffer;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "The API request failed.";
}

function problemVariant<
  const Code extends keyof typeof problemDefinitions,
  const Title extends (typeof problemDefinitions)[Code]["title"],
  const Status extends (typeof problemDefinitions)[Code]["status"],
>(code: Code, definition: { readonly title: Title; readonly status: Status }) {
  const fields = {
    type: z.literal(`urn:atelier2:problem:v1:${code}` as const),
    title: z.literal(definition.title),
    status: z.literal(definition.status),
    detail: z.string(),
  };
  if (code === "invalid-request" || code === "run-input-refused") {
    return z
      .object({
        ...fields,
        invalid_fields: z.array(invalidFieldSchema).optional(),
      })
      .strict();
  }
  if (code === "uncast-agent-roles") {
    return z
      .object({
        ...fields,
        uncast_roles: z
          .array(
            z
              .object({
                role: z.string().min(1).max(1_024),
                reason: z.enum([
                  "override-not-registered",
                  "workflow-model-not-registered",
                  "workflow-model-ambiguous",
                  "no-project-default",
                  "family-difference-unavailable",
                ]),
                family_differs_from: z
                  .string()
                  .min(1)
                  .max(1_024)
                  .nullable()
                  .optional(),
              })
              .strict(),
          )
          .min(1),
      })
      .strict();
  }
  return z.object(fields).strict();
}

function isCanonicalStandardBase64(value: string): boolean {
  return decodeCanonicalBase64(value) !== null;
}

export function decodeCanonicalBase64(value: string): Uint8Array | null {
  if (
    !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(
      value,
    )
  ) {
    return null;
  }
  try {
    const binary = atob(value);
    if (btoa(binary) !== value) {
      return null;
    }
    return Uint8Array.from(binary, (character) => character.charCodeAt(0));
  } catch {
    return null;
  }
}

export function decodePublicRunReference(reference: string): string | null {
  if (!reference.startsWith("run1.")) {
    return null;
  }
  const encoded = reference.slice("run1.".length);
  if (!/^[A-Za-z0-9_-]+$/.test(encoded)) {
    return null;
  }
  try {
    const standard = encoded.replaceAll("-", "+").replaceAll("_", "/");
    const binary = atob(standard + "=".repeat((4 - (standard.length % 4)) % 4));
    const bytes = Uint8Array.from(binary, (character) =>
      character.charCodeAt(0),
    );
    const runId = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
    if (runId.length === 0 || encodePublicRunReference(runId) !== reference) {
      return null;
    }
    return runId;
  } catch {
    return null;
  }
}

export function encodePublicRunReference(runId: string): string {
  const bytes = new TextEncoder().encode(runId);
  const binary = String.fromCharCode(...bytes);
  return `run1.${btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "")}`;
}

export function parseEventCursor(
  cursor: string,
): { publicRunReference: string; sequence: number } | null {
  const match = /^event1\.([A-Za-z0-9_-]+)\.([1-9][0-9]*)$/.exec(cursor);
  if (match === null) {
    return null;
  }
  const encodedRun = match[1];
  const encodedSequence = match[2];
  if (encodedRun === undefined || encodedSequence === undefined) {
    return null;
  }
  const publicReference = `run1.${encodedRun}`;
  const sequence = Number(encodedSequence);
  if (
    decodePublicRunReference(publicReference) === null ||
    !Number.isSafeInteger(sequence)
  ) {
    return null;
  }
  return { publicRunReference: publicReference, sequence };
}
