import type { AgentConfigurationRevisionListItem } from "../api/client";
import type { CatalogNameState } from "./catalogName";

/** Git's own short-hash convention (`git rev-parse --short`). */
const GIT_SHORT_COMMIT_LENGTH = 8;

/**
 * Where a revision's bytes came from, when a connected source delivered
 * them: the shortened label a chip or row can hold, and the full commit
 * still reachable through `title` for a hover reveal (#1077, #1112).
 */
export interface SourceFact {
  readonly label: string;
  readonly title: string;
}

/**
 * Copy the Catalog room renders: what this atelier can run, and where it came
 * from.
 *
 * One owner per room, the convention `workbenchPageCopy` and `railCopy`
 * already hold to, so `?pseudo-locale=1` (`wrapDisplayCopy`) proves every
 * string on this surface has a source.
 *
 * The words are the operator's, not the store's: a person recognises
 * "Available workflows", never "published revisions of kind workflow".
 */
export const catalogPageCopy = {
  title: "Catalog",
  import: "Import",
  filePicker: "Catalog file picker",
  oneWorkflow: "1 workflow",
  oneAgent: "1 agent",
  oneFile: "1 file",

  all: "All",
  workflowsTitle: "Workflows",
  workflowsLabel: "workflows",
  agentsTitle: "Agents",
  agentsLabel: "agents",
  agentsByProvider: "Agents by provider",
  skillsTitle: "Skills",
  catalogGroups: "Catalog groups",
  search: "Search…",
  searchLabel: "Search the catalog",
  catalogEmpty: "Drop a workflow, an agent, or a plugin folder — anywhere on this page.",
  workflowsUnavailable: "Workflows unavailable",
  workflowsIncomplete: "Workflows incomplete",

  agentsUnavailable: "Agents unavailable",
  skillsNone: "A plugin folder brings skills.",

  // The provider an imported agent belongs to. The import door takes exactly
  // one authoring format — the Markdown definition with frontmatter, which is
  // Claude's — so the format the bytes arrived in is what names the provider.
  // A door for another format (`.toml` for Codex) names its own.
  agentProviderClaude: "Claude",
  noDescription: "No description.",

  newerRevisionHint: "A newer published revision is available.",
  notAdmittedHint: "This published workflow is not in the catalog yet.",
  newerRevision: "Newer revision",
  notAdmitted: "Not in catalog",
  notExecutable: "Not executable",

  // The catalog detail owns the only manual start door (ADR 0019 §1).
  start: "Start",

  cancel: "Cancel",
  close: "Close",
  kind: "Kind",
  kindWorkflow: "Workflow",
  kindAgent: "Agent",
  addToCatalog: "Add to catalog",
  addingToCatalog: "Adding…",
  noKindDeclared: "Choose a kind above to add this to the catalog.",
  importFailed: "This file could not be imported.",
  recognitionFailed: "This file could not be recognized.",
  notAWorkflow: "This is not a workflow — nothing was added.",
  notAnAgent: "This is not an agent — nothing was added."
} as const;

/**
 * The detail behind one catalog entry: its still graph, the node panel, and
 * what the room says when the name is gone. Same owner as the list, because
 * the detail is this room's own page (ADR 0019 §1), not a room of its own.
 */
export const workflowDetailCopy = {
  detailUnavailable: "Workflow detail unavailable",
  detailLabel: "workflow detail",
  technical: "Technical",
  notFoundTitle: "Workflow not found",
  notFoundDescription: "No published workflow carries this name.",
  graphUnavailable: "This revision's graph cannot be drawn here.",
  workflowRevision: "Workflow revision",
  sealsWorkflowRevision: "the published workflow revision",
  // A revision's format, straight from its own document -- the mockup's
  // "workflow/v3" (v8 :1528), never a bare number a reader has to place.
  format: "Format",
  formatVersion: (version: number) => `workflow/v${version}`,
  // Where this revision's bytes first entered the catalog: the honest wire
  // facts (commit, path), never the opaque `source1.` reference a person
  // cannot read (#1077). A file-imported revision carries no provenance at
  // all, so this row is absent for it -- no placeholder invented, matching
  // the catalog card's own bare chip (#1112). `sourceFact` is the one owner
  // for this format across the room: the catalog card's chip
  // (`catalogRows.ts`) and this fold both call it, so `<8-hex commit> ·
  // <path>` reads identically everywhere.
  source: "Source",
  sourceFact: (commit: string, path: string): SourceFact => ({
    label: `${commit.slice(0, GIT_SHORT_COMMIT_LENGTH)} · ${path}`,
    title: commit
  }),
  orders: "Orders",
  noOrders: "No orders declared.",
  schema: "Schema",
  schemaUnavailable: "Schema summary unavailable.",
  schemaAcceptsAny: "Any JSON value.",
  required: "required",
  panelTitle: "Node",
  panelRole: "Role",
  panelPromptStart: "Prompt template",
  panelNoRole: "This node declares no role.",
  panelNoPromptStart: "No prompt excerpt is published for this node.",
  panelClose: "Close node detail",
  notAdmittedNote: "Not admitted to the catalog.",
  retiredNote: "Retired",
  retiredNotice: "This workflow's catalog lineage was retired. Starting it is not offered here.",
  retire: "Retire",
  retireTitle: (name: string) => `Retire ${name}?`,
  retireDisappears: "Leaves",
  retireDisappearsFact: "It leaves Catalog and can no longer be started.",
  retireStays: "History",
  retireStaysFact: "Past runs and immutable revisions remain reachable.",
  retirePermanent: "Permanent",
  retirePermanentFact: "This workflow cannot return to Catalog. There is no way back.",
  retireFailed: "This workflow could not be retired."
} as const;

/** Copy owned by the catalog detail's one manual-start sheet. */
export const workflowStartCopy = {
  startTitle: (name: string) => `Start ${name}`,
  configurationFor: (role: string) => `Configuration for ${role}`,
  preparing: "Preparing…",
  sheetUnavailable: "The start sheet could not be prepared.",
  workItem: "Work item",
  noSource: "No source",
  connectSource: "Connect one in Settings",
  unknownSource: "Other source",
  allRetired: "Nothing open to start",
  filterWorkItemsPlaceholder: "Number or title…",
  filterWorkItemsLabel: "Filter the work item picker",
  noWorkItemMatch: (query: string) => `No item matches "${query}"`,
  orderUnavailable: "This order shape cannot be started here.",
  roles: "Roles",
  choose: "Choose",
  chosenNow: "Chosen now",
  pinnedInWorkflow: "pinned in workflow",
  nextHigher: "next higher",
  unavailable: "Unavailable",
  trueLabel: "True",
  falseLabel: "False",
  startRun: "Start run",
  startNeedsWorkItem: "Choose a work item before starting.",
  startNeedsWorkItemSource: "Connect a source in Settings before starting.",
  startNeedsOrder: "Complete each required order before starting.",
  startPreparing: "Preparing the start options.",
  startNeedsConfiguration: (role: string) => `Choose a configuration for ${role} before starting.`,
  tryAgain: "Try again",
  cancel: "Cancel",
  retry: "Retry",
  startUnavailable: "The run could not be started.",
  github: "GitHub",
  gitlab: "GitLab",
  configurationsIncomplete: "Agent configurations are incomplete.",
  accountsIncomplete: "Accounts are incomplete.",
  servedProjectMissing: "Served project missing.",
  rolesUnresolved: "Model resolution did not name exactly these roles.",
  observedQueueIncomplete: "Observed queue items are incomplete.",
  startResponseChangedRoles: "The start response changed the selected roles.",
  startResponseUnproven: "The start response did not prove the exact request.",
  orderText: "Text",
  publishFromFile: (orderName: string) => `Publish ${orderName} from a file`,
  rawJson: "Raw JSON",
  rawJsonFor: (orderName: string) => `Raw JSON for ${orderName}`,
  // The way out a Raw JSON syntax refusal names (#438 Zeile 11, #1130 finding
  // 2): an order that keeps its per-field form beside Raw JSON can send a
  // person back to that form, but an order Raw JSON alone can reach
  // (`raw_object`) has no form to name -- so the two sentences differ.
  rawJsonWayOutBesideForm: "Fix the JSON, or clear this field and fill the form above instead.",
  rawJsonWayOutAlone: "Fix the JSON to start; this order has no field form."
} as const;

export function startConfigurationLabel(
  providerId: string,
  modelId: string,
  accountId: string
): string {
  return `${providerId} · ${modelId} · Account ${accountId}`;
}

export function startAccountSuffix(accountId: string): string {
  return ` · Account ${accountId}`;
}

/** The wire's own closed vocabulary (`client.ts`'s generated enum) for why a
 * listed agent configuration cannot start -- never widened to a bare
 * `string`, so a fifth reason breaks this file's build until it is mapped. */
type NotStartableReason = NonNullable<AgentConfigurationRevisionListItem["not_startable_reason"]>;

/**
 * One sentence per reason, true of every case that reason covers
 * (`agent_catalog.py`'s precedence), not just its most common one: a model
 * can lack a checked registration because none was ever made, because a newer
 * revision superseded it, or because an entry is still unchecked, and a live
 * check can be missing because none was ever taken or because the only one on
 * file is no longer current.
 *
 * No sentence names its role -- the row's own label carries it -- and none
 * names a repair this surface does not offer: nothing in the console renews a
 * live check, so those two sentences point at the one door the role's own
 * select does have.
 */
const NOT_STARTABLE_SENTENCE: Readonly<Record<NotStartableReason, string>> = {
  "agent-executor-binding-unavailable":
    "This model cannot run in this workshop — choose a different one.",
  "model-not-registered":
    "This model is not checked in Settings — check or correct it there.",
  "provider-probe-receipt-missing":
    "This model has no current live check — choose a different one.",
  "provider-probe-failed":
    "This model's last live check failed — choose a different one."
};

/** Why this role cannot start, or the bare badge when the host named no reason. */
export function startNotStartableReason(reason: NotStartableReason | null): string {
  return reason === null ? workflowStartCopy.unavailable : NOT_STARTABLE_SENTENCE[reason];
}

export function pinnedModelLine(model: string, account: string): string {
  return `${workflowStartCopy.pinnedInWorkflow} → ${model}${account}`;
}

export function projectDefaultLine(
  difficulty: number,
  model: string,
  nextHigher: boolean,
  account: string
): string {
  const fallback = nextHigher ? ` (${workflowStartCopy.nextHigher})` : "";
  return `difficulty ${difficulty} → ${model}${fallback}${account}`;
}

export function startOrderGroup(name: string): string {
  return `Order ${name}`;
}

/** A string order's exact bytes, against the artifact store's own published bound. */
export function startOrderByteCount(bytes: number, maximumBytes: number): string {
  return `${bytes.toLocaleString("en-US")} / ${maximumBytes.toLocaleString("en-US")} bytes`;
}

export function workItemFor(orderName: string): string {
  return `${workflowStartCopy.workItem} for ${orderName}`;
}

export function observedSourceHeading(projectId: string, platform: string): string {
  return `${projectId} · ${platform}`;
}

export function observedWorkItemLabel(reference: string, title: string | null): string {
  return title === null ? reference : `${reference} ${title}`;
}

/**
 * The short state a card or detail header wears beside a name that is not
 * the catalog's current head for it -- `null` for the ordinary case, an
 * admitted head, which wears no note at all.
 *
 * Read-only browsing shows every published name it can identify rather than
 * hiding one, unlike a project configuration picker (a write
 * precondition: it must bind to a live catalog member, so it drops what
 * cannot be bound). This surface only answers "what can the house do", so an
 * honest note beats disappearing content -- the same choice the saved-workflow
 * picker on the start door already makes for the same three states.
 */
export function catalogStateNote(state: CatalogNameState | undefined): string | null {
  if (state === undefined || state.kind === "admitted") return null;
  if (state.kind === "retired") return workflowDetailCopy.retiredNote;
  return workflowDetailCopy.notAdmittedNote;
}
