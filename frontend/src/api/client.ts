import { z } from "zod";

import { HealthResource } from "./generated/health.zod";
import {
  CatalogAdmissionResource,
  CatalogNameResolutionResource,
  VersionedWorkflowRevisionPageResource,
  WaitAnswerSchemaResourceV3,
  WorkflowGraphResourceV3,
  WorkflowRevisionDetailResource,
  WorkflowRevisionSummaryResourceV2,
} from "./generated/workflowAndCatalog.zod";
import {
  ModelRegistryRevisionResource,
  ProjectListResource,
  ProjectModelDefaultsRevisionResource,
  ProjectModelResolutionResource,
  ProjectSourceConnectionRevisionResource,
  projectSourceListResourceItemsMax,
  ProjectSourceResource as ProjectSourceResourceGenerated,
  PublicProjectReference,
} from "./generated/projectsSourcesAndModels.zod";
import {
  AgentConfigurationRevisionListItemResource,
  AgentConfigurationRevisionPageResource,
  AgentConfigurationRevisionResource,
  AgentDefinitionRevisionDetailResource,
  AgentDefinitionRevisionListItemResource,
  AgentDefinitionRevisionPageResource,
  AgentDefinitionRevisionResource,
  AuthProfileRevisionPageResource,
  AuthProfileRevisionResource,
  QueueAdmissionResource,
  QueueItemPageResource,
  QueueItemResource,
  QueueLaunchBindingResource,
  QueuePriorityRankResource,
  QueueProposalResource,
} from "./generated/authAgentAndQueue.zod";
import {
  AgentBindingResourceV2,
  DefectiveRunRowResource,
  NodeDetailResource,
  NodeRailResource,
  NodeRefusalOutputResource,
  RunCancellabilityResource,
  RunForkOriginResource,
  RunForkSuccessorResource,
  RunListRowResource,
  runResourceV3AgentBindingsMax,
  runResourceV3ForkSuccessorsMax,
  RunResourceV3,
  RunTerminalAnswerOmittedResource,
  RunTerminalAnswerValueResource,
  VersionedRunPageResource,
} from "./generated/runsRailAndNodes.zod";
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
  .regex(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/);
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
 * `WaitAnswerSchemaResourceV3` carries `kind` and `values` as two separately
 * optional fields; only their pairing is a rule the generated shape cannot
 * state: `values` names the enum's own members, and only an `enum` kind
 * names any (`waitAnswer.ts`, #1091 PR #1108 finding 1).
 */
const waitAnswerSchemaV3Schema = WaitAnswerSchemaResourceV3.superRefine((entry, context) => {
  const namesValues = entry.values !== undefined && entry.values !== null;
  if ((entry.kind === "enum") !== namesValues) {
    context.addIssue({
      code: "custom",
      message: "values names the enum's own members, and only those",
    });
  }
});

const workflowGraphV3Schema = WorkflowGraphResourceV3.extend({
  wait_answer_schemas: z.array(waitAnswerSchemaV3Schema),
});

export const workflowRevisionDetailSchema = WorkflowRevisionDetailResource.extend({
  document_base64: standardBase64,
  graph: workflowGraphV3Schema,
});

export const projectSourceConnectionRevisionSchema = ProjectSourceConnectionRevisionResource.extend({
  revision_number: positiveSafeInteger,
});

/**
 * `connected_at` is optional in the served shape with no declared default;
 * absence on the wire means "never connected", so this overlay is the one
 * place that turns that absence into an explicit `null` for every caller.
 */
export const projectSourceResourceSchema = ProjectSourceResourceGenerated.extend({
  connected_at: z.union([recordedAtStamp, z.null()]).default(null),
  revision: positiveSafeInteger,
});

export const projectSourceListSchema = z.object({
  items: z.array(projectSourceResourceSchema).max(projectSourceListResourceItemsMax),
}).strict();

export type { HealthResource };

/**
 * The wire shape `GET /seat` answers: whether this serve holds a terminal
 * seat, and where the browser on this machine reaches it. The address is
 * drawn per serve and told to nobody else, so it is read fresh rather than
 * remembered.
 */
export const seatResourceSchema = z
  .object({
    state: z.enum(["ALIVE", "MISSING", "FAILED"]),
    url: z.string().min(1).nullable().default(null),
    project_id: z.string().min(1).nullable().default(null),
  })
  .strict();
export type SeatResource = z.infer<typeof seatResourceSchema>;

const providerIdSchema = z
  .string()
  .min(1)
  .max(64)
  .regex(/^[a-z][a-z0-9._-]*$/);

const modelRegistryRevisionSchema = ModelRegistryRevisionResource.extend({
  revision_number: positiveSafeInteger,
});

const projectModelDefaultsRevisionSchema = ProjectModelDefaultsRevisionResource.extend({
  revision_number: positiveSafeInteger,
});

const projectModelResolutionSchema = ProjectModelResolutionResource;

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

const authProfileRevisionSchema = AuthProfileRevisionResource.extend({
  revision_number: positiveSafeInteger,
});

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

const agentConfigurationRevisionSchema = AgentConfigurationRevisionResource;

export const agentConfigurationRevisionListItemObjectSchema =
  AgentConfigurationRevisionListItemResource;

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
export const agentConfigurationRevisionPageSchema =
  AgentConfigurationRevisionPageResource.extend({
    items: z.array(agentConfigurationRevisionListItemSchema),
  });

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

const queuePriorityRankSchema = QueuePriorityRankResource.extend({
  rank: positiveSafeInteger,
});

const queueProposalSchema = QueueProposalResource.extend({
  revision: positiveSafeInteger,
  policy_revision: positiveSafeInteger.nullable().optional(),
  priority: queuePriorityRankSchema,
});

const queueAdmissionSchema = QueueAdmissionResource.extend({
  proposal_revision: positiveSafeInteger.nullable().optional(),
});

const queueLaunchBindingSchema = QueueLaunchBindingResource.extend({
  proposal_revision: positiveSafeInteger,
});

export const queueItemSchema = QueueItemResource.extend({
  revision: nonnegativeSafeInteger,
  proposal: queueProposalSchema.nullable(),
  admission: queueAdmissionSchema.nullable(),
  launch_binding: queueLaunchBindingSchema.nullable(),
});

const queueItemPageSchema = QueueItemPageResource.extend({
  items: z.array(queueItemSchema),
});

export const agentDefinitionRevisionListItemSchema =
  AgentDefinitionRevisionListItemResource;

export const agentDefinitionRevisionPageSchema =
  AgentDefinitionRevisionPageResource.extend({
    items: z.array(agentDefinitionRevisionListItemSchema),
  });

export const agentDefinitionRevisionDetailSchema =
  AgentDefinitionRevisionDetailResource;

const agentDefinitionRevisionSchema = AgentDefinitionRevisionResource;

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

export const authProfileRevisionPageSchema = AuthProfileRevisionPageResource.extend({
  items: z.array(authProfileRevisionSchema),
});

/**
 * Reused-rail-node evidence is a cross-field rule no schema-only generator
 * states: either every source field of the prior run is named, or none is,
 * and only a succeeded node can be reused at all.
 */
export const nodeRailEntrySchema = NodeRailResource.extend({
  // The document defaults these four to `null`, which zod's own `.default()`
  // would make always-present on the decoded type; kept `.optional()` instead
  // so a caller before this evidence existed still decodes the same way.
  reused_from_run_reference: publicRunReference.nullable().optional(),
  source_event_hash: sha256.nullable().optional(),
  source_receipt_hash: sha256.nullable().optional(),
  source_declared_context_package_hash: sha256.nullable().optional(),
}).superRefine((entry, context) => {
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

export type RunStateV3 = RunResourceV3["state"];

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
 * #439 D3 makes cancellability the server's predicate, not the cockpit's
 * guess: a cancellable run names its target node execution, and a
 * non-cancellable one names exactly one reason -- a cross-field rule no
 * schema-only generator states.
 */
const runCancellabilitySchema = RunCancellabilityResource.superRefine(
  (cancellation, context) => {
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
  },
);

const agentBindingV2Schema = AgentBindingResourceV2.extend({
  revision_number: positiveSafeInteger,
});

// `public_run_reference` is a public-reference codec the document states only
// as a pattern; the base64url round trip it names is checked here, not there.
const runForkOriginSchema = RunForkOriginResource.extend({
  public_run_reference: publicRunReference,
});

const runForkSuccessorSchema = RunForkSuccessorResource.extend({
  public_run_reference: publicRunReference,
});

/**
 * @public re-exports the generated bound under its established name: the
 * decoder no longer references it directly (it decodes through
 * `NodeRefusalOutputResource` as generated), but `readableResultDisplay`'s
 * boundary test still builds an at-cap fixture against it, and the API
 * facade -- not `generated/**`, which stays internal -- is where that stays.
 */
export {
  nodeRefusalOutputResourceValueBase64Max as MAXIMUM_REFUSED_OUTPUT_BASE64_CHARACTERS,
} from "./generated/runsRailAndNodes.zod";

const nodeRefusalOutputSchema = NodeRefusalOutputResource;

const runTerminalAnswerOmittedSchema = RunTerminalAnswerOmittedResource.extend({
  maximum_bytes: nonnegativeSafeInteger,
});

const runTerminalAnswerSchema = z.discriminatedUnion("kind", [
  RunTerminalAnswerValueResource,
  runTerminalAnswerOmittedSchema,
]);

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
export const runV3Schema = RunResourceV3.extend({
  public_run_reference: publicRunReference,
  agent_bindings: z.array(agentBindingV2Schema).max(runResourceV3AgentBindingsMax),
  fork_origin: runForkOriginSchema.nullable().optional(),
  fork_successors: z
    .array(runForkSuccessorSchema)
    .max(runResourceV3ForkSuccessorsMax)
    .optional(),
  // What the terminal node wrote or refused, mutually exclusive, both
  // absent on a run that has not ended (#1045). History used to ask
  // `getNodeDetail` once per row for exactly these two facts.
  answer: runTerminalAnswerSchema.nullable().optional(),
  refusal_output: nodeRefusalOutputSchema.nullable().optional(),
  state_version: nonnegativeSafeInteger,
  // `event1.<run>.<sequence>` names a public run reference and a positive
  // sequence, both checked here; the document states only the pattern.
  latest_event_cursor: eventCursor.nullable(),
  node_rail: z.array(nodeRailEntrySchema).min(1),
  cancellation: runCancellabilitySchema,
});

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

export const nodeDetailSchema = NodeDetailResource.extend({
  public_run_reference: publicRunReference,
  transcript: attemptTranscriptSchema.nullable().optional(),
});

export type NodeDetail = z.infer<typeof nodeDetailSchema>;

const runListRowSchema = RunListRowResource.extend({ run: runV3Schema });

/**
 * One listed run whose own projection failed, told apart from a run instead
 * of hidden (#1042). The other rows on the same page prove nothing about
 * this one, so a run list answers with this row rather than refusing the
 * whole page for the sake of one entry.
 */
const defectiveRunRowSchema = DefectiveRunRowResource.extend({
  public_run_reference: publicRunReference,
});

const runListRowUnionSchema = z.discriminatedUnion("kind", [
  runListRowSchema,
  defectiveRunRowSchema,
]);

export type RunListRow = z.infer<typeof runListRowUnionSchema>;
export type DefectiveRunRow = z.infer<typeof defectiveRunRowSchema>;

const runPageSchema = VersionedRunPageResource.extend({
  items: z.array(runListRowUnionSchema),
  next_after: publicRunReference.nullable(),
});

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
        reason: z.enum([
          "agent-executor-binding-unavailable",
          "work-item-claim-unconfigured",
          "work-item-names-no-scope",
          "work-item-claim-refused-by-priority",
          "work-item-claim-ledger-unreadable",
          "work-item-claim-refused",
          "work-item-claim-touches-another-lane",
        ]),
        detail: z.string().nullable(),
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
  "agent-mode-mismatch": { status: 422, title: "Agent mode mismatch" },
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
  "queue-policy-not-set": {
    status: 404,
    title: "Queue project policy not found",
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
    "agent-mode-mismatch",
    problemDefinitions["agent-mode-mismatch"],
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
    "queue-policy-not-set",
    problemDefinitions["queue-policy-not-set"],
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
export type WorkflowRevisionPage = VersionedWorkflowRevisionPageResource;
export type WorkflowRevisionSummary = WorkflowRevisionSummaryResourceV2;
type AgentDefinitionRevisionDetail = z.infer<
  typeof agentDefinitionRevisionDetailSchema
>;
type CatalogNameResolution = CatalogNameResolutionResource;
type CatalogAdmission = CatalogAdmissionResource;
export type LibraryRecognition = z.infer<typeof libraryRecognitionSchema>;
export type CatalogIntakeKind = z.infer<typeof catalogIntakeKindSchema>;
type LibraryAddition = z.infer<typeof libraryAdditionSchema>;
type ProjectList = ProjectListResource;
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
  /** Whether this serve holds a terminal seat, and where it answers. */
  getSeat(): Promise<SeatResource>;
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
    getSeat: () =>
      requestJson(fetcher, "/atelier/api/v1/seat", {}, [200], seatResourceSchema),
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
        ProjectListResource,
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
        PublicProjectReference.parse(projectReference);
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
          : `/atelier/api/v1/workflow-revisions?limit=50&view=described&after_revision_hash=${encodeURIComponent(after)}`,
        {},
        [200],
        VersionedWorkflowRevisionPageResource,
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
    // The described listing does not carry lineage recency, so the picker
    // reads this existing resource for the head instead of inventing an
    // order from the hash-sorted page.
    getRevisionByName: async (kind: CatalogLineageKind, name: string) => {
      const resolution = await requestJson(
        fetcher,
        `/atelier/api/v1/catalog-revisions/by-name/${kind}/${encodeURIComponent(name)}`,
        {},
        [200],
        CatalogNameResolutionResource,
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
        CatalogAdmissionResource,
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
        CatalogAdmissionResource,
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

async function fetchWithConnectionSignal(
  fetcher: typeof fetch,
  target: string,
  init: RequestInit,
): Promise<Response> {
  try {
    const response = await fetcher(target, {
      ...init,
      headers: { accept: "application/json", ...init.headers },
    });
    reportConnectionRestored();
    return response;
  } catch (error) {
    // The round trip itself never happened -- a redeploy's outage (#700), not
    // a 4xx/5xx the server actually answered with, so this is the one signal
    // that means the workshop cannot be reached at all.
    reportConnectionLost();
    throw new CockpitRequestError(errorMessage(error), null, false, true);
  }
}

function parseNoContentResult<T>(
  acceptedStatuses: readonly number[],
  schema: z.ZodType<T>,
): HttpResult<T> {
  if (!acceptedStatuses.includes(204)) {
    throw new CockpitRequestError("The API returned undocumented HTTP 204.");
  }
  try {
    return { status: 204, value: schema.parse(undefined) };
  } catch {
    throw new CockpitRequestError(
      "The API response did not match the durable wire contract.",
    );
  }
}

async function parseJsonResponseBody(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    throw new CockpitRequestError("The API response was not valid JSON.");
  }
}

function rejectUndocumentedStatus(status: number, value: unknown): never {
  try {
    const problem = decodeProblem(value);
    if (problem.status !== status) {
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
    throw new CockpitRequestError(`The API returned undocumented HTTP ${status}.`);
  }
}

function parseAcceptedBody<T>(
  status: number,
  value: unknown,
  schema: z.ZodType<T>,
): HttpResult<T> {
  try {
    return { status, value: schema.parse(value) };
  } catch {
    throw new CockpitRequestError(
      "The API response did not match the durable wire contract.",
    );
  }
}

async function requestJsonResult<T>(
  fetcher: typeof fetch,
  target: string,
  init: RequestInit,
  acceptedStatuses: readonly number[],
  schema: z.ZodType<T>,
): Promise<HttpResult<T>> {
  const response = await fetchWithConnectionSignal(fetcher, target, init);
  // Disconnect answers 204 with an empty body; JSON parsing would invent a failure.
  if (response.status === 204) {
    return parseNoContentResult(acceptedStatuses, schema);
  }
  const value = await parseJsonResponseBody(response);
  if (!acceptedStatuses.includes(response.status)) {
    rejectUndocumentedStatus(response.status, value);
  }
  return parseAcceptedBody(response.status, value, schema);
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
    return Uint8Array.from(binary, (character) => character.codePointAt(0)!);
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
      character.codePointAt(0)!,
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

function base64PaddingLength(base64: string): number {
  if (base64.endsWith("==")) return 2;
  if (base64.endsWith("=")) return 1;
  return 0;
}

export function encodePublicRunReference(runId: string): string {
  const bytes = new TextEncoder().encode(runId);
  const binary = String.fromCodePoint(...bytes);
  const padded = btoa(binary).replaceAll("+", "-").replaceAll("/", "_");
  const paddingLength = base64PaddingLength(padded);
  const unpadded = paddingLength === 0 ? padded : padded.slice(0, -paddingLength);
  return `run1.${unpadded}`;
}

export function parseEventCursor(
  cursor: string,
): { publicRunReference: string; sequence: number } | null {
  const match = /^event1\.([A-Za-z0-9_-]+)\.([1-9]\d*)$/.exec(cursor);
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
