import type { AgentConfigurationRevisionListItem, CockpitApi, RunV3, WorkflowRevisionDetail } from "../api/client";
import { problemCode } from "./catalogName";
import { readEveryAgentConfiguration } from "./runPages";

const CONDUCTOR_WORKFLOW_NAME = "conductor";

export interface ConductorConnection {
  workflowRevisionHash: string;
  role: string;
  agentConfigurationRevisionHash: string;
  maximumRounds: number;
}

/** Why the conductor's bound configuration will not start, exactly as the server names it. */
export type ConductorNotStartableReason = NonNullable<
  AgentConfigurationRevisionListItem["not_startable_reason"]
>;

/**
 * What the server says about the one conductor conversation, discriminated
 * instead of folded onto `null` (#1103): five distinct causes -- no catalog
 * name, a foreign document shape, no project, a role with no binding, and a
 * bound configuration that cannot start -- used to read as one silent
 * refusal. The first three stay `absent`, because none names anything an
 * operator could act on differently; `unbound` and `not-startable` carry
 * exactly what a start would have refused on, passed through rather than
 * reworded.
 */
export type ConductorConnectionState =
  | { kind: "absent" }
  | { kind: "unbound"; role: string }
  | {
      kind: "not-startable";
      agentConfigurationRevisionHash: string;
      providerId: string;
      modelId: string;
      notStartableReason: ConductorNotStartableReason;
      providerProbeProblemCode: string | null;
      providerProbeObservedAt: string | null;
    }
  | { kind: "connected"; connection: ConductorConnection };

/**
 * The published loop a Workbench conversation can operate. Its first node is
 * the input-bearing wait, so the start door has no graph inputs to invent.
 */
export async function resolveConductorConnection(
  cockpitApi: CockpitApi
): Promise<ConductorConnectionState> {
  let workflowRevisionHash: string;
  try {
    const resolution = await cockpitApi.getRevisionByName("workflow", CONDUCTOR_WORKFLOW_NAME);
    workflowRevisionHash = resolution.catalog_revision_hash;
  } catch (error) {
    const code = problemCode(error);
    if (code === "catalog-name-not-found" || code === "catalog-lineage-retired") {
      return { kind: "absent" };
    }
    throw error;
  }

  const revision = await cockpitApi.getWorkflowRevision(workflowRevisionHash);
  const shape = conductorConversationShape(revision);
  if (shape === null) return { kind: "absent" };

  const projects = await cockpitApi.listProjects();
  const project = projects.items[0];
  if (project === undefined) return { kind: "absent" };
  const resolution = await cockpitApi.resolveProjectModels(
    project.public_project_reference,
    workflowRevisionHash,
    []
  );
  const boundConfigurationHash = resolution.resolutions.find(
    (binding) => binding.role === shape.role
  )?.agent_configuration_revision_hash;
  if (boundConfigurationHash === undefined || boundConfigurationHash === null) {
    return { kind: "unbound", role: shape.role };
  }

  const reading = await readEveryAgentConfiguration((after) =>
    cockpitApi.listAgentConfigurationRevisions(after)
  );
  const bound = reading.configurations.find(
    (configuration) =>
      configuration.agent_configuration_revision_hash === boundConfigurationHash
  );
  // A binding the catalog no longer lists is honestly no different from one
  // that was never bound: neither names anything this room could show apart
  // from "absent".
  if (bound === undefined) return { kind: "absent" };
  if (!bound.startable) {
    return {
      kind: "not-startable",
      agentConfigurationRevisionHash: boundConfigurationHash,
      providerId: bound.provider_id,
      modelId: bound.model,
      // `bound.startable` is false, so the server's own invariant
      // (`AgentConfigurationRevisionListItemResource`) guarantees a reason.
      notStartableReason: bound.not_startable_reason as ConductorNotStartableReason,
      providerProbeProblemCode: bound.provider_probe_problem_code,
      providerProbeObservedAt: bound.provider_probe_observed_at
    };
  }
  return {
    kind: "connected",
    connection: {
      workflowRevisionHash,
      role: shape.role,
      agentConfigurationRevisionHash: boundConfigurationHash,
      maximumRounds: shape.maximumRounds
    }
  };
}

export function conductorConversationShape(
  revision: WorkflowRevisionDetail
): { role: string; maximumRounds: number } | null {
  const graph = revision.graph;
  if (!graph.executable || graph.orders.length !== 0) return null;
  const [role, ...moreRoles] = graph.agent_roles;
  const [loop, ...moreLoops] = graph.loops;
  if (role === undefined || moreRoles.length > 0 || loop === undefined || moreLoops.length > 0) {
    return null;
  }
  const members = new Set(loop.member_node_ids);
  const waits = graph.node_previews.filter((node) => members.has(node.id) && node.kind === "wait");
  const agents = graph.node_previews.filter((node) => members.has(node.id) && node.kind === "agent");
  const [waitAnswer, ...moreWaitAnswers] = graph.wait_answer_schemas.filter(
    (answer) => answer.node_id === "next_message"
  );
  return loop.repeat_while === null &&
    waits.length === 1 &&
    agents.length === 1 &&
    waits[0]?.id === "next_message" &&
    agents[0]?.id === "conduct" &&
    agents[0]?.role === role &&
    // The conductor's own fixed message schema (`CONDUCTOR_MESSAGE_SCHEMA`,
    // `host/conductor_workflow.py`) is `{type: "string", minLength: 1}`, so
    // its wait classifies "string" (#1091), not "free".
    waitAnswer?.kind === "string" &&
    moreWaitAnswers.length === 0
    ? { role, maximumRounds: loop.maximum_rounds }
    : null;
}

/**
 * Every non-terminal run that could become the one live conductor
 * conversation, newest first: the tie-break a redeploy needs, since a run
 * still on a since-retired conductor revision must lose to one just started
 * on the current revision with the same timestamp.
 *
 * Shared with `WorkbenchPage.svelte`'s own selection (#1148 REVISE M3): that
 * caller walks this order lazily, resolving one candidate's revision at a
 * time and stopping at the first conductor-shaped one, instead of resolving
 * every distinct revision the run list carries up front -- a cost that would
 * otherwise scale with the number of distinct workflows shown, not with how
 * many of them could ever be selected.
 */
export function orderedConductorCandidates(runs: readonly RunV3[]): RunV3[] {
  return runs
    .filter(
      (run) =>
        (run.state === "STARTED" || run.state === "WAITING_INPUT" || run.state === "WAITING_RECONCILIATION") &&
        hasStartedStamp(run)
    )
    .sort((a, b) => {
      const aStamp = a.started_at as string;
      const bStamp = b.started_at as string;
      if (aStamp !== bStamp) return aStamp > bStamp ? -1 : 1;
      return a.public_run_reference > b.public_run_reference ? -1 : 1;
    });
}

function hasStartedStamp(run: RunV3): run is RunV3 & { started_at: string } {
  return typeof run.started_at === "string" && Number.isFinite(Date.parse(run.started_at));
}
