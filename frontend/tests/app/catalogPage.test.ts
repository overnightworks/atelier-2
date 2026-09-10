import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/svelte";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "../../src/App.svelte";
import {
  CockpitRequestError,
  type AgentDefinitionRevisionListItem,
  type CatalogLineageKind,
  type CockpitApi,
  type Problem,
  type WorkflowRevisionSummary
} from "../../src/api/client";
import { catalogPageCopy, workflowDetailCopy, workflowStartCopy } from "../../src/lib/catalogPageCopy";
import { IMPORT_SHEET_KINDS } from "../../src/lib/catalogImport";
import { shortFingerprint } from "../../src/lib/fingerprint";
import {
  reportConnectionLost,
  reportConnectionRestored,
  restartNoticeCopy
} from "../../src/lib/connectionState";
import { MutationJournal } from "../../src/lib/mutationJournal";
import { retryLabel } from "../../src/lib/readStateCopy";
import { cockpitApiStub } from "../support/cockpitApi";

const WORKFLOW_HASH = "b".repeat(64);
const SIBLING_HASH = "f".repeat(64);
const AGENT_HASH = "d".repeat(64);
const LINEAGE_ID = "e".repeat(64);
const WORKFLOW_NAME = "iterate-code";
const SECOND_WORKFLOW_NAME = "review-code";
const EXACT_YAML = "format_version: 3\nname: iterate-code\n";

afterEach(() => {
  vi.restoreAllMocks();
  cleanup();
  reportConnectionRestored();
});

function openCatalog(overrides: Partial<CockpitApi> = {}) {
  window.history.replaceState(null, "", "/atelier/catalog");
  return render(App, {
    props: {
      cockpitApi: cockpitApiStub(overrides),
      mutationJournal: new MutationJournal(sessionStorage)
    }
  });
}

function workflowSummary(
  overrides: Partial<WorkflowRevisionSummary> = {}
): WorkflowRevisionSummary {
  return {
    workflow_revision_hash: WORKFLOW_HASH,
    workflow_format_version: 3,
    executable: true,
    not_executable_reason: null,
    name: WORKFLOW_NAME,
    description: "build, then review",
    provenance: null,
    ...overrides
  };
}

function agentItem(
  overrides: Partial<AgentDefinitionRevisionListItem> = {}
): AgentDefinitionRevisionListItem {
  return {
    agent_definition_revision_hash: AGENT_HASH,
    name: "scribe",
    description: "Writes what the stage needs.",
    ...overrides
  };
}

function listing(items: readonly WorkflowRevisionSummary[]): Partial<CockpitApi> {
  return {
    listWorkflowRevisions: vi.fn(async () => ({
      items: [...items],
      next_after_revision_hash: null
    }))
  };
}

function admittedName(): Partial<CockpitApi> {
  return {
    getRevisionByName: vi.fn(async () => ({
      display_name: WORKFLOW_NAME,
      lineage_id: LINEAGE_ID,
      catalog_revision_hash: WORKFLOW_HASH,
      revision_number: 1
    }))
  };
}

function unlistedName(): Partial<CockpitApi> {
  return {
    getRevisionByName: vi.fn(async () => {
      throw new CockpitRequestError("no such name", {
        type: "urn:atelier2:problem:v1:catalog-name-not-found",
        title: "Catalog name not found",
        status: 404,
        detail: "no such name"
      } as Problem);
    })
  };
}

function kindName(name: string): RegExp {
  return new RegExp(`^${name}$`);
}

function kindButton(name: string) {
  return screen.getByRole("button", { name: kindName(name) });
}

function offeredKindButtons() {
  return within(screen.getByRole("group", { name: catalogPageCopy.kind })).getAllByRole("button");
}

describe("the catalog room", () => {
  it("greets an empty atelier by naming the door that fills it", async () => {
    openCatalog();

    expect(
      (await screen.findByText(catalogPageCopy.catalogEmpty)).isConnected
    ).toBe(true);
  });

  it("names the source of empty skills only when the Skills group is open", async () => {
    openCatalog({ ...listing([workflowSummary()]), ...admittedName() });

    await screen.findByText(WORKFLOW_NAME);
    await fireEvent.click(screen.getByRole("button", { name: /Skills\s*0/ }));
    expect((await screen.findByText(catalogPageCopy.skillsNone)).isConnected).toBe(true);
  });

  it("wears no provenance pill for a workflow dropped in by hand, since every card would then wear the same one", async () => {
    openCatalog({ ...listing([workflowSummary()]), ...admittedName() });

    const card = (await screen.findByText(WORKFLOW_NAME)).closest("li");
    expect(card).not.toBeNull();
    expect(within(card as HTMLElement).queryByText(/·/)).toBeNull();
  });

  it("names a connected source's commit and path on the card that carries one", async () => {
    const sourceCommit = "a".repeat(40);
    openCatalog({
      ...listing([
        workflowSummary({
          provenance: {
            source: "source1.git-abc123",
            source_commit: sourceCommit,
            source_path: "workflows/iterate-code.yaml",
            intaken_at: "2026-08-27T10:00:00Z"
          }
        })
      ]),
      ...admittedName()
    });

    const card = (await screen.findByText(WORKFLOW_NAME)).closest("li");
    expect(card).not.toBeNull();
    const fact = workflowDetailCopy.sourceFact(sourceCommit, "workflows/iterate-code.yaml");
    const pill = within(card as HTMLElement).getByText(fact.label);
    expect(pill.isConnected).toBe(true);
    expect(pill.title).toBe(fact.title);
  });

  it("leaves an admitted workflow calm with no second admission door", async () => {
    openCatalog({ ...listing([workflowSummary()]), ...admittedName() });

    const card = (await screen.findByText(WORKFLOW_NAME)).closest("li");
    expect(card).not.toBeNull();
    expect(screen.queryByRole("button", { name: /Admit/ })).toBeNull();
  });

  it("carries a newer revision as a compact tile pill", async () => {
    openCatalog({
      ...listing([
        workflowSummary({ workflow_revision_hash: WORKFLOW_HASH }),
        workflowSummary({ workflow_revision_hash: SIBLING_HASH })
      ]),
      ...admittedName()
    });

    expect(await screen.findAllByText(WORKFLOW_NAME)).toHaveLength(1);
    expect((await screen.findByText(catalogPageCopy.newerRevision)).isConnected).toBe(true);
  });

  it("does not mark a workflow with no newer revision", async () => {
    openCatalog({ ...listing([workflowSummary()]), ...admittedName() });

    await screen.findByText(WORKFLOW_NAME);
    expect(screen.queryByText(catalogPageCopy.newerRevision)).toBeNull();
  });

  it("opens a named workflow's own detail room from the card door", async () => {
    openCatalog({
      ...listing([workflowSummary()]),
      ...admittedName(),
      getWorkflowRevision: vi.fn(async () => ({
        workflow_revision_hash: WORKFLOW_HASH,
        document_base64: "YQ==",
        provenance: null,
        graph: {
          workflow_format_version: 3 as const,
          executable: true,
          not_executable_reason: null,
          node_count: 1,
          agent_roles: [],
          orders: [],
          wait_answer_schemas: [],
          node_previews: [],
          loops: [],
          name: WORKFLOW_NAME,
          description: null
        }
      }))
    });
    await screen.findByText(WORKFLOW_NAME);

    fireEvent.click(screen.getByRole("link", { name: WORKFLOW_NAME }));

    expect(
      (await screen.findByRole("heading", { name: WORKFLOW_NAME })).isConnected
    ).toBe(true);
  });

  it("proves(a-details-panel-shows-the-published-substance) proves(a-revision-hash-is-a-proof-anchor): names the published revision as proof and summarizes each declared order", async () => {
    const orderSchemaHash = "c".repeat(64);
    openCatalog({
      ...listing([workflowSummary()]),
      ...admittedName(),
      getWorkflowRevision: vi.fn(async () => ({
        workflow_revision_hash: WORKFLOW_HASH,
        document_base64: "YQ==",
        provenance: null,
        graph: {
          workflow_format_version: 3 as const,
          executable: true,
          not_executable_reason: null,
          node_count: 1,
          agent_roles: [],
          orders: [{ name: "portions", schema: { ref: "portions-schema", revision: orderSchemaHash } }],
          wait_answer_schemas: [],
          node_previews: [],
          loops: [],
          name: WORKFLOW_NAME,
          description: null
        }
      })),
      getSchemaRevision: vi.fn(async () => ({
        type: "object",
        properties: { portions: { type: "integer" } },
        required: ["portions"]
      }))
    });

    await screen.findByText(WORKFLOW_NAME);
    await fireEvent.click(screen.getByRole("link", { name: WORKFLOW_NAME }));

    expect(screen.queryByRole("group", { name: workflowDetailCopy.workflowRevision })).toBeNull();
    const technicalSummary = await screen.findByText(workflowDetailCopy.technical, { selector: "summary" });
    const technical = technicalSummary.closest("details");
    expect(technical?.open).toBe(false);

    await fireEvent.click(technicalSummary);

    const revision = await screen.findByRole("group", { name: workflowDetailCopy.workflowRevision });
    expect(technical?.open).toBe(true);
    expect(revision.textContent).toContain(shortFingerprint(WORKFLOW_HASH));
    expect(revision.textContent).toContain(workflowDetailCopy.sealsWorkflowRevision);
    expect((await screen.findByText(workflowDetailCopy.formatVersion(3))).isConnected).toBe(true);
    expect(screen.queryByText(workflowDetailCopy.source)).toBeNull();
    expect((await screen.findByRole("heading", { name: workflowDetailCopy.orders })).isConnected).toBe(true);
    expect(screen.getByText(/portions-schema/).isConnected).toBe(true);
    expect(screen.getByText(/portions · integer · required/).isConnected).toBe(true);
  });

  it("proves(a-revision-detail-names-its-source-as-commit-and-path): a revision with a connected source shows its commit and path in the Technical fold, never the opaque source reference", async () => {
    const sourceCommit = "b8cfa0d5".repeat(5);
    const sourcePath = "workflows/iterate-code.yaml";
    openCatalog({
      ...listing([workflowSummary()]),
      ...admittedName(),
      getWorkflowRevision: vi.fn(async () => ({
        workflow_revision_hash: WORKFLOW_HASH,
        document_base64: "YQ==",
        provenance: {
          source: "source1.leonardo-atelier-kit",
          source_commit: sourceCommit,
          source_path: sourcePath,
          intaken_at: "2026-08-29T00:00:00Z"
        },
        graph: {
          workflow_format_version: 3 as const,
          executable: true,
          not_executable_reason: null,
          node_count: 1,
          agent_roles: [],
          orders: [],
          wait_answer_schemas: [],
          node_previews: [],
          loops: [],
          name: WORKFLOW_NAME,
          description: null
        }
      }))
    });

    await screen.findByText(WORKFLOW_NAME);
    await fireEvent.click(screen.getByRole("link", { name: WORKFLOW_NAME }));
    await fireEvent.click(await screen.findByText(workflowDetailCopy.technical, { selector: "summary" }));

    const source = await screen.findByText(workflowDetailCopy.source);
    const sourceRow = source.closest("div");
    const sourceValue = sourceRow?.querySelector("dd");
    expect(sourceValue?.textContent).toBe("b8cfa0d5 · workflows/iterate-code.yaml");
    expect(sourceValue?.title).toBe(sourceCommit);
    expect(screen.queryByText(/source1\./)).toBeNull();
    expect((await screen.findByText(workflowDetailCopy.formatVersion(3))).isConnected).toBe(true);
  });

  it("counts the workflow tiles after collapsing a real-shaped library listing", async () => {
    const secondHash = "1".repeat(64);
    const thirdHash = "2".repeat(64);
    const fourthHash = "3".repeat(64);
    const firstLineageId = "5".repeat(64);
    const thirdLineageId = "6".repeat(64);
    const firstName = "catalog-first";
    const secondName = "catalog-second";
    const thirdName = "catalog-third";
    openCatalog({
      ...listing([
        workflowSummary({ name: firstName, workflow_revision_hash: WORKFLOW_HASH }),
        workflowSummary({ name: firstName, workflow_revision_hash: SIBLING_HASH }),
        workflowSummary({ name: secondName, workflow_revision_hash: secondHash }),
        workflowSummary({ name: thirdName, workflow_revision_hash: thirdHash }),
        workflowSummary({ name: thirdName, workflow_revision_hash: fourthHash })
      ]),
      getRevisionByName: vi.fn(async (_kind: CatalogLineageKind, name: string) => {
        if (name === firstName || name === thirdName) {
          return {
            display_name: name,
            lineage_id: name === firstName ? firstLineageId : thirdLineageId,
            catalog_revision_hash: name === firstName ? WORKFLOW_HASH : thirdHash,
            revision_number: 1
          };
        }
        throw new CockpitRequestError("no such name", {
          type: "urn:atelier2:problem:v1:catalog-name-not-found",
          title: "Catalog name not found",
          status: 404,
          detail: "no such name"
        } as Problem);
      }),
      listAgentDefinitionRevisions: vi.fn(async () => ({
        items: [agentItem(), agentItem({ agent_definition_revision_hash: "4".repeat(64), name: "reviewer" })],
        next_after_revision_hash: null
      }))
    });

    await screen.findByRole("link", { name: firstName });
    expect(screen.getByRole("button", { name: /Workflows\s*3/ })).toBeTruthy();
    expect(screen.getByRole("button", { name: /Agents\s*2/ })).toBeTruthy();
    expect(screen.getByRole("button", { name: /Skills\s*0/ })).toBeTruthy();
    expect(screen.getAllByRole("link", { name: /catalog-(first|second|third)/ })).toHaveLength(3);
  });

  it("withholds catalog group counts until both collections confirm", async () => {
    let resolveWorkflows!: (page: { items: WorkflowRevisionSummary[]; next_after_revision_hash: null }) => void;
    const workflowsPending = new Promise<{ items: WorkflowRevisionSummary[]; next_after_revision_hash: null }>((resolve) => {
      resolveWorkflows = resolve;
    });
    let resolveAgents!: (page: { items: AgentDefinitionRevisionListItem[]; next_after_revision_hash: null }) => void;
    const agentsPending = new Promise<{ items: AgentDefinitionRevisionListItem[]; next_after_revision_hash: null }>((resolve) => {
      resolveAgents = resolve;
    });

    const listWorkflowRevisions = vi.fn(() => workflowsPending);
    const listAgentDefinitionRevisions = vi.fn(() => agentsPending);
    openCatalog({ listWorkflowRevisions, listAgentDefinitionRevisions });

    await waitFor(() => expect(listWorkflowRevisions).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(listAgentDefinitionRevisions).toHaveBeenCalledTimes(1));

    resolveAgents({ items: [agentItem()], next_after_revision_hash: null });
    await waitFor(() => expect(screen.getAllByRole("status")).toHaveLength(1));

    expect(screen.queryByRole("group", { name: catalogPageCopy.catalogGroups })).toBeNull();
    expect(screen.queryByRole("button", { name: /Workflows\s*0/ })).toBeNull();

    resolveWorkflows({ items: [], next_after_revision_hash: null });
    expect((await screen.findByRole("button", { name: /Workflows\s*0/ })).isConnected).toBe(true);
  });

  it("keeps confirmed tiles visible and names a failed workflow refresh", async () => {
    let leaveAgentRefreshPending!: () => void;
    const agentRefresh = new Promise<{
      items: AgentDefinitionRevisionListItem[];
      next_after_revision_hash: null;
    }>((resolve) => {
      leaveAgentRefreshPending = () => {
        resolve({ items: [], next_after_revision_hash: null });
      };
    });
    const listWorkflowRevisions = vi
      .fn()
      .mockResolvedValueOnce({ items: [workflowSummary()], next_after_revision_hash: null })
      .mockRejectedValueOnce(new CockpitRequestError("the store is asleep"));
    openCatalog({
      listWorkflowRevisions,
      listAgentDefinitionRevisions: vi
        .fn()
        .mockResolvedValueOnce({ items: [], next_after_revision_hash: null })
        .mockImplementationOnce(() => agentRefresh),
      ...admittedName()
    });

    expect((await screen.findByRole("link", { name: WORKFLOW_NAME })).isConnected).toBe(true);
    reportConnectionLost();
    reportConnectionRestored();

    expect((await screen.findByText(catalogPageCopy.workflowsUnavailable)).isConnected).toBe(true);
    expect(screen.getByRole("button", { name: retryLabel(catalogPageCopy.workflowsLabel) }).isConnected).toBe(true);
    expect(screen.getByRole("link", { name: WORKFLOW_NAME }).isConnected).toBe(true);
    expect(screen.queryByText(catalogPageCopy.catalogEmpty)).toBeNull();

    leaveAgentRefreshPending();
  });

  it("does not replace a confirmed-empty catalog with an empty message after a failed refresh", async () => {
    const listWorkflowRevisions = vi
      .fn()
      .mockResolvedValueOnce({ items: [], next_after_revision_hash: null })
      .mockRejectedValueOnce(new CockpitRequestError("the store is asleep"));
    openCatalog({ listWorkflowRevisions });

    expect((await screen.findByText(catalogPageCopy.catalogEmpty)).isConnected).toBe(true);
    reportConnectionLost();
    reportConnectionRestored();

    expect((await screen.findByText(catalogPageCopy.workflowsUnavailable)).isConnected).toBe(true);
    expect(screen.getByRole("button", { name: retryLabel(catalogPageCopy.workflowsLabel) }).isConnected).toBe(true);
    expect(screen.queryByText(catalogPageCopy.catalogEmpty)).toBeNull();
  });

  it("opens the selected workflow's start sheet instead of leaving the catalog detail", async () => {
    openCatalog({
      ...listing([workflowSummary()]),
      ...admittedName(),
      getWorkflowRevision: vi.fn(async () => ({
        workflow_revision_hash: WORKFLOW_HASH,
        document_base64: "YQ==",
        provenance: null,
        graph: {
          workflow_format_version: 3 as const,
          executable: true,
          not_executable_reason: null,
          node_count: 1,
          agent_roles: ["builder"],
          orders: [],
          wait_answer_schemas: [],
          node_previews: [],
          loops: [],
          name: WORKFLOW_NAME,
          description: null
        }
      })),
      listAgentConfigurationRevisions: vi.fn(async () => ({
        items: [
          {
            agent_configuration_revision_hash: AGENT_HASH,
            provider_id: "claude",
            model: "test-model",
            auth_mode: "subscription" as const,
            auth_profile_revision_hash: "a".repeat(64),
            executor_revision: "test/v1",
            requested_capability: "headless" as const,
            startable: true,
            structurally_startable: true,
            not_startable_reason: null,
            provider_probe_problem_code: null,
            provider_probe_observed_at: null
          }
        ],
        next_after_revision_hash: null
      })),
      listAuthProfileRevisions: vi.fn(async () => ({
        items: [{
          profile_id: "operator",
          revision_number: 1,
          provider_id: "claude",
          auth_mode: "subscription" as const,
          auth_profile_revision_hash: "a".repeat(64)
        }],
        next_after_revision_hash: null
      })),
      getModelRegistry: vi.fn(async () => ({
        provider_id: "claude",
        revision_number: 1,
        model_registry_revision_hash: "9".repeat(64),
        entries: [{
          model_id: "test-model",
          agent_configuration_revision_hash: AGENT_HASH,
          source: "discovered" as const,
          provider_check: "checked" as const
        }]
      })),
      listProjects: vi.fn(async () => ({
        items: [{ public_project_reference: "project1.YXRlbGllcg" }]
      })),
      resolveProjectModels: vi.fn(async (_project, workflowRevisionHash) => ({
        project_id: "atelier",
        public_project_reference: "project1.YXRlbGllcg",
        workflow_revision_hash: workflowRevisionHash,
        resolutions: [{
          role: "builder",
          agent_configuration_revision_hash: null,
          source: "uncast" as const,
          model_id: null,
          declared_difficulty: 2 as const,
          default_difficulty: null,
          uncast_reason: "no-project-default" as const,
          family_differs_from: null
        }]
      }))
    });
    await screen.findByText(WORKFLOW_NAME);
    await fireEvent.click(screen.getByRole("link", { name: WORKFLOW_NAME }));

    await fireEvent.click(await screen.findByRole("button", { name: catalogPageCopy.start }));

    expect((await screen.findByRole("heading", { name: workflowStartCopy.startTitle(WORKFLOW_NAME) })).isConnected).toBe(
      true
    );
    expect(await screen.findByLabelText(workflowStartCopy.configurationFor("builder"))).toBeTruthy();
    const startRun = screen.getByRole("button", { name: workflowStartCopy.startRun }) as HTMLButtonElement;
    expect(startRun.disabled).toBe(true);
    expect(startRun.title).toBe(workflowStartCopy.startNeedsConfiguration("builder"));
    expect(screen.queryByRole("button", { name: /Why builder/ })).toBeNull();
  });

  it("keeps a published workflow outside the catalog without a second admission door", async () => {
    openCatalog({ ...listing([workflowSummary()]), ...unlistedName() });

    await screen.findByText(WORKFLOW_NAME);
    expect(screen.queryByRole("button", { name: /Admit/ })).toBeNull();
  });

  it("recognizes and admits a workflow in one import confirmation", async () => {
    let admitted = false;
    const addLibraryDocument = vi.fn(async () => {
      admitted = true;
      return {
        status: 201,
        value: { intake_id: WORKFLOW_HASH, kind: "workflow" as const }
      };
    });
    const publish = vi.fn(async () => ({
      status: 201,
      value: {
        workflow_revision_hash: WORKFLOW_HASH,
        document_base64: "YQ==",
        provenance: null,
        graph: {
          workflow_format_version: 3 as const,
          executable: true,
          not_executable_reason: null,
          node_count: 1,
          agent_roles: [],
          orders: [],
          wait_answer_schemas: [],
          node_previews: [],
          loops: [],
          name: WORKFLOW_NAME,
          description: null
        }
      }
    }));
    const foundCatalogLineage = vi.fn(async () => ({
      status: 201,
      value: {
        display_name: WORKFLOW_NAME,
        lineage_id: LINEAGE_ID,
        catalog_revision_hash: WORKFLOW_HASH,
        revision_number: 1
      }
    }));
    openCatalog({
      ...listing([workflowSummary()]),
      getRevisionByName: vi.fn(async () => {
        if (!admitted) {
          throw new CockpitRequestError("no such name", {
            type: "urn:atelier2:problem:v1:catalog-name-not-found",
            title: "Catalog name not found",
            status: 404,
            detail: "no such name"
          } as Problem);
        }
        return {
          display_name: WORKFLOW_NAME,
          lineage_id: LINEAGE_ID,
          catalog_revision_hash: WORKFLOW_HASH,
          revision_number: 1
        };
      }),
      recognizeLibraryDocument: vi.fn(async () => ({
        outcome: "workflow" as const,
        workflow_format_version: 3 as const,
        name: WORKFLOW_NAME,
        description: null
      })),
      addLibraryDocument,
      publish,
      foundCatalogLineage
    });

    await fireEvent.click(await screen.findByRole("button", { name: catalogPageCopy.import }));
    expect(screen.queryByRole("dialog", { name: catalogPageCopy.import })).toBeNull();
    const file = { name: "iterate-code.yaml", arrayBuffer: async () => new TextEncoder().encode(EXACT_YAML).buffer };
    await fireEvent.change(screen.getByLabelText(catalogPageCopy.filePicker), { target: { files: [file] } });
    const add = await screen.findByRole("button", { name: catalogPageCopy.addToCatalog });
    expect((add as HTMLButtonElement).disabled).toBe(true);
    await fireEvent.click(screen.getByRole("button", { name: catalogPageCopy.kindWorkflow }));
    await fireEvent.click(add);

    expect((await screen.findByText(WORKFLOW_NAME)).isConnected).toBe(true);
    expect(addLibraryDocument).toHaveBeenCalledWith(
      expect.any(Uint8Array),
      "workflow",
      expect.any(String),
      expect.any(String)
    );
  });

  it("Import opens the import sheet, and Escape closes it and returns focus to the button that opened it", async () => {
    openCatalog({
      recognizeLibraryDocument: vi.fn(async () => ({
        outcome: "workflow" as const,
        workflow_format_version: 3 as const,
        name: WORKFLOW_NAME,
        description: null
      }))
    });

    const importButton = await screen.findByRole("button", { name: catalogPageCopy.import });
    expect(screen.queryByRole("dialog", { name: catalogPageCopy.import })).toBeNull();

    await fireEvent.click(importButton);
    const file = { name: "iterate-code.yaml", arrayBuffer: async () => new TextEncoder().encode(EXACT_YAML).buffer };
    await fireEvent.change(screen.getByLabelText(catalogPageCopy.filePicker), { target: { files: [file] } });
    const sheet = await screen.findByRole("dialog", { name: catalogPageCopy.import });
    expect(sheet.isConnected).toBe(true);

    // A real browser turns Escape on an open modal `<dialog>` into its own
    // "cancel" event; jsdom does not implement that translation, so the test
    // fires the event the sheet's own `oncancel` handler already answers to.
    await fireEvent(sheet, new Event("cancel", { cancelable: true }));

    expect(screen.queryByRole("dialog", { name: catalogPageCopy.import })).toBeNull();
    expect(document.activeElement).toBe(importButton);
  });

  it("recognizes a file dropped anywhere on the Catalog page", async () => {
    openCatalog({
      recognizeLibraryDocument: vi.fn(async () => ({
        outcome: "workflow" as const,
        workflow_format_version: 3 as const,
        name: WORKFLOW_NAME,
        description: null
      }))
    });
    const file = {
      name: "iterate-code.yaml",
      arrayBuffer: async () => new TextEncoder().encode(EXACT_YAML).buffer
    };

    await fireEvent.drop(screen.getByRole("region", { name: catalogPageCopy.title }), {
      dataTransfer: { files: [file] }
    });

    expect((await screen.findByRole("button", { name: catalogPageCopy.addToCatalog })).isConnected).toBe(true);
  });

  it("proves(a-revision-no-run-can-start-says-so-before-the-operator-tries) proves(a-revision-no-run-can-start-says-so-where-it-was-published): carries a blocked workflow with an honest tile pill", async () => {
    openCatalog({
      ...listing([
        workflowSummary({
          executable: false,
          not_executable_reason: "agent forms nothing binds yet: outputs"
        })
      ]),
      ...unlistedName()
    });

    await screen.findByText(WORKFLOW_NAME);
    expect((await screen.findByText(catalogPageCopy.notExecutable)).isConnected).toBe(true);
    expect(screen.getByTitle(
      "This workflow declares no output. Add one outputs: entry on the agent node and publish again."
    )).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Admit/ })).toBeNull();
  });

  it("proves(the-picker-offers-every-saved-workflow-not-only-its-first-page): lists a workflow that arrives only after the first page", async () => {
    openCatalog({
      listWorkflowRevisions: vi.fn(async (after?: string) =>
        after === undefined
          ? { items: [workflowSummary()], next_after_revision_hash: WORKFLOW_HASH }
          : {
              items: [
                workflowSummary({
                  workflow_revision_hash: SIBLING_HASH,
                  name: SECOND_WORKFLOW_NAME
                })
              ],
              next_after_revision_hash: null
            }
      ),
      getRevisionByName: vi.fn(async (_kind: CatalogLineageKind, name: string) => ({
        display_name: name,
        lineage_id: LINEAGE_ID,
        catalog_revision_hash: name === WORKFLOW_NAME ? WORKFLOW_HASH : SIBLING_HASH,
        revision_number: 1
      }))
    });

    expect((await screen.findByText(SECOND_WORKFLOW_NAME)).isConnected).toBe(true);
  });

  it("carries a published agent as a provider tile without an unavailable detail door", async () => {
    openCatalog({
      listAgentDefinitionRevisions: vi.fn(async () => ({
        items: [agentItem()],
        next_after_revision_hash: null
      }))
    });

    expect((await screen.findByText("scribe")).isConnected).toBe(true);
    expect(screen.queryByRole("link", { name: "scribe" })).toBeNull();
  });

  it("marks which provider an imported agent belongs to", async () => {
    openCatalog({
      listAgentDefinitionRevisions: vi.fn(async () => ({
        items: [agentItem()],
        next_after_revision_hash: null
      }))
    });

    expect((await screen.findByLabelText(catalogPageCopy.agentProviderClaude)).isConnected).toBe(true);
  });

  it("proves(the-person-declares-the-kind-on-the-import-sheet): the person chooses the kind on the sheet and that choice is the intake kind", async () => {
    const addLibraryDocument = vi.fn(async () => ({
      status: 201,
      value: { intake_id: AGENT_HASH, kind: "agent" as const }
    }));
    const publishAgentDefinition = vi.fn(async () => ({
      status: 201,
      value: { agent_definition_revision_hash: AGENT_HASH }
    }));
    openCatalog({
      recognizeLibraryDocument: vi.fn(async () => ({
        outcome: "agent_definition" as const,
        name: "walker",
        description: "Walks the surface.",
        provider_id: "anthropic"
      })),
      addLibraryDocument,
      publishAgentDefinition,
      listAgentDefinitionRevisions: vi.fn(async () => ({
        items: [],
        next_after_revision_hash: null
      }))
    });

    const file = { name: "walker.md", arrayBuffer: async () => new TextEncoder().encode("agent").buffer };
    await fireEvent.change(screen.getByLabelText(catalogPageCopy.filePicker), { target: { files: [file] } });

    expect((await screen.findByText(catalogPageCopy.oneAgent)).isConnected).toBe(true);
    expect(screen.getByText("walker").tagName).toBe("STRONG");
    const agentChip = kindButton(catalogPageCopy.kindAgent);
    expect(agentChip.getAttribute("aria-pressed")).toBe("false");
    expect(kindButton(catalogPageCopy.kindWorkflow).getAttribute("aria-pressed")).toBe("false");
    const add = screen.getByRole("button", { name: catalogPageCopy.addToCatalog });
    expect((add as HTMLButtonElement).disabled).toBe(true);
    expect(add.getAttribute("title")).toBe(catalogPageCopy.noKindDeclared);
    await fireEvent.click(agentChip);
    expect(agentChip.getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByText(catalogPageCopy.oneAgent).isConnected).toBe(true);
    await fireEvent.click(add);

    await waitFor(() => {
      expect(addLibraryDocument).toHaveBeenCalledWith(
        expect.any(Uint8Array),
        "agent",
        expect.any(String),
        expect.any(String)
      );
    });
  });

  it("proves(an-uncertain-recognition-asks-for-a-kind-instead-of-refusing): an uncertain recognition leads to a kind choice instead of a refusal", async () => {
    const addLibraryDocument = vi.fn();
    openCatalog({
      recognizeLibraryDocument: vi.fn(async () => ({
        outcome: "unrecognized" as const,
        refusals: [{ kind: "workflow" as const, expected: "format_version", refused_because: "missing" }]
      })),
      addLibraryDocument
    });

    const file = { name: "notes.md", arrayBuffer: async () => new TextEncoder().encode("notes").buffer };
    await fireEvent.change(screen.getByLabelText(catalogPageCopy.filePicker), { target: { files: [file] } });

    expect((await screen.findByText("notes.md")).isConnected).toBe(true);
    expect(screen.getByText(catalogPageCopy.oneFile).isConnected).toBe(true);
    expect(screen.queryByRole("button", { name: catalogPageCopy.close })).toBeNull();
    expect(screen.getByRole("button", { name: catalogPageCopy.cancel })).toBeTruthy();
    const add = screen.getByRole("button", { name: catalogPageCopy.addToCatalog });
    expect((add as HTMLButtonElement).disabled).toBe(true);
    expect(add.getAttribute("title")).toBe(catalogPageCopy.noKindDeclared);
    expect(kindButton(catalogPageCopy.kindWorkflow).getAttribute("aria-pressed")).toBe("false");
    expect(kindButton(catalogPageCopy.kindAgent).getAttribute("aria-pressed")).toBe("false");
    expect(offeredKindButtons()).toHaveLength(IMPORT_SHEET_KINDS.length);
    expect(addLibraryDocument).not.toHaveBeenCalled();
  });

  it("proves(#1500): a locked Add to catalog says why, not just grey", async () => {
    openCatalog({
      recognizeLibraryDocument: vi.fn(async () => ({
        outcome: "unrecognized" as const,
        refusals: [{ kind: "workflow" as const, expected: "format_version", refused_because: "missing" }]
      }))
    });

    const file = { name: "notes.md", arrayBuffer: async () => new TextEncoder().encode("notes").buffer };
    await fireEvent.change(screen.getByLabelText(catalogPageCopy.filePicker), { target: { files: [file] } });

    const add = await screen.findByRole("button", { name: catalogPageCopy.addToCatalog });
    expect((add as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText(catalogPageCopy.noKindDeclared)).toBeTruthy();

    await fireEvent.click(kindButton(catalogPageCopy.kindWorkflow));

    expect(screen.queryByText(catalogPageCopy.noKindDeclared)).toBeNull();
    expect((add as HTMLButtonElement).disabled).toBe(false);
  });

  it("proves(#1500): focus alone is not a choice, so the sentence and the button never disagree", async () => {
    openCatalog({
      recognizeLibraryDocument: vi.fn(async () => ({
        outcome: "unrecognized" as const,
        refusals: [{ kind: "workflow" as const, expected: "format_version", refused_because: "missing" }]
      }))
    });

    const file = { name: "notes.md", arrayBuffer: async () => new TextEncoder().encode("notes").buffer };
    await fireEvent.change(screen.getByLabelText(catalogPageCopy.filePicker), { target: { files: [file] } });

    // The sheet moves focus to the first kind chip on open (keyboard-first),
    // which is exactly the moment a focus ring could be misread as a choice.
    const workflowChip = await screen.findByRole("button", { name: catalogPageCopy.kindWorkflow });
    await waitFor(() => expect(document.activeElement).toBe(workflowChip));

    expect(workflowChip.getAttribute("aria-pressed")).toBe("false");
    expect(screen.getByText(catalogPageCopy.noKindDeclared)).toBeTruthy();
    expect(
      (screen.getByRole("button", { name: catalogPageCopy.addToCatalog }) as HTMLButtonElement).disabled
    ).toBe(true);
  });

  it("Skill is not a choice on the Import sheet, so an uncertain file cannot be declared as Skill and the sheet never silently closes", async () => {
    const addLibraryDocument = vi.fn();
    openCatalog({
      recognizeLibraryDocument: vi.fn(async () => ({
        outcome: "unrecognized" as const,
        refusals: [{ kind: "workflow" as const, expected: "format_version", refused_because: "missing" }]
      })),
      addLibraryDocument
    });

    const file = { name: "notes.md", arrayBuffer: async () => new TextEncoder().encode("notes").buffer };
    await fireEvent.change(screen.getByLabelText(catalogPageCopy.filePicker), { target: { files: [file] } });

    expect((await screen.findByText("notes.md")).isConnected).toBe(true);
    expect(kindButton(catalogPageCopy.kindWorkflow)).toBeTruthy();
    expect(kindButton(catalogPageCopy.kindAgent)).toBeTruthy();
    expect(offeredKindButtons()).toHaveLength(IMPORT_SHEET_KINDS.length);
    const add = screen.getByRole("button", { name: catalogPageCopy.addToCatalog });
    expect((add as HTMLButtonElement).disabled).toBe(true);
    await fireEvent.click(add);
    expect(screen.getByRole("dialog", { name: catalogPageCopy.import })).toBeTruthy();
    expect(screen.getByRole("button", { name: catalogPageCopy.addToCatalog })).toBeTruthy();
    expect(addLibraryDocument).not.toHaveBeenCalled();

    await fireEvent.click(kindButton(catalogPageCopy.kindWorkflow));
    expect(kindButton(catalogPageCopy.kindWorkflow).getAttribute("aria-pressed")).toBe("true");
    expect((screen.getByRole("button", { name: catalogPageCopy.addToCatalog }) as HTMLButtonElement).disabled).toBe(false);
    await fireEvent.click(kindButton(catalogPageCopy.kindAgent));
    expect(kindButton(catalogPageCopy.kindAgent).getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByRole("dialog", { name: catalogPageCopy.import })).toBeTruthy();
    expect(addLibraryDocument).not.toHaveBeenCalled();
  });

  it("proves(a-mistaken-kind-is-visible-on-the-sheet-before-the-catalog-changes): a mistaken kind is named on the sheet before anything stands in the catalog", async () => {
    const addLibraryDocument = vi.fn(async () => ({
      status: 201,
      value: { intake_id: WORKFLOW_HASH, kind: "workflow" as const }
    }));
    const publish = vi.fn(async () => {
      throw new CockpitRequestError("invalid", {
        type: "urn:atelier2:problem:v1:invalid-workflow-document",
        title: "Invalid workflow document",
        status: 422,
        detail: "missing format_version"
      } as Problem, true);
    });
    openCatalog({
      recognizeLibraryDocument: vi.fn(async () => ({
        outcome: "unrecognized" as const,
        refusals: [{ kind: "workflow" as const, expected: "format_version", refused_because: "missing" }]
      })),
      addLibraryDocument,
      publish
    });

    const file = { name: "notes.md", arrayBuffer: async () => new TextEncoder().encode("notes").buffer };
    await fireEvent.change(screen.getByLabelText(catalogPageCopy.filePicker), { target: { files: [file] } });
    await screen.findByText("notes.md");
    await fireEvent.click(kindButton(catalogPageCopy.kindWorkflow));
    await fireEvent.click(screen.getByRole("button", { name: catalogPageCopy.addToCatalog }));

    expect((await screen.findByText(catalogPageCopy.notAWorkflow)).isConnected).toBe(true);
    expect(screen.queryByText("invalid-workflow-document")).toBeNull();
    expect(screen.getByRole("button", { name: catalogPageCopy.addToCatalog })).toBeTruthy();
    expect(screen.getByRole("button", { name: catalogPageCopy.cancel })).toBeTruthy();
    expect(kindButton(catalogPageCopy.kindWorkflow).getAttribute("aria-pressed")).toBe("true");
    expect(screen.queryByText("notes.md", { selector: "li *" })).toBeNull();
    expect(addLibraryDocument).toHaveBeenCalledWith(
      expect.any(Uint8Array),
      "workflow",
      expect.any(String),
      expect.any(String)
    );
  });

  it("still closes a kind the library recognises but does not hold, without adding it", async () => {
    const addLibraryDocument = vi.fn();
    openCatalog({
      recognizeLibraryDocument: vi.fn(async () => ({
        outcome: "not_held" as const,
        kind: "mcp_server" as const,
        reason: "the library does not hold MCP servers yet"
      })),
      addLibraryDocument
    });

    const file = { name: ".mcp.json", arrayBuffer: async () => new TextEncoder().encode("{}").buffer };
    await fireEvent.change(screen.getByLabelText(catalogPageCopy.filePicker), { target: { files: [file] } });

    expect((await screen.findByText("the library does not hold MCP servers yet")).isConnected).toBe(true);
    expect(screen.getByRole("button", { name: catalogPageCopy.close })).toBeTruthy();
    expect(screen.queryByRole("button", { name: catalogPageCopy.addToCatalog })).toBeNull();
    expect(addLibraryDocument).not.toHaveBeenCalled();
  });

  it("says the workflow list is unavailable rather than showing it as empty", async () => {
    openCatalog({
      listWorkflowRevisions: vi.fn(async () => {
        throw new CockpitRequestError("the store is asleep");
      })
    });

    await waitFor(() => {
      expect(screen.getByText(catalogPageCopy.workflowsUnavailable).isConnected).toBe(true);
    });
    expect(screen.queryByText(catalogPageCopy.catalogEmpty)).toBeNull();
  });

  it("says the agent list is unavailable rather than showing it as empty", async () => {
    openCatalog({
      listAgentDefinitionRevisions: vi.fn(async () => {
        throw new CockpitRequestError("the store is asleep");
      })
    });

    await waitFor(() => {
      expect(screen.getByText(catalogPageCopy.agentsUnavailable).isConnected).toBe(true);
    });
    expect(screen.queryByText(catalogPageCopy.catalogEmpty)).toBeNull();
  });

  it("names no local failure while the whole workshop reads unreachable, and reads itself again once the connection returns (#700)", async () => {
    const listWorkflowRevisions = vi.fn().mockRejectedValue(new Error("Failed to fetch"));
    const listAgentDefinitionRevisions = vi.fn().mockRejectedValue(new Error("Failed to fetch"));
    openCatalog({ listWorkflowRevisions, listAgentDefinitionRevisions });
    await screen.findByRole("heading", { name: "Catalog" });

    reportConnectionLost();
    await waitFor(() => {
      expect(document.querySelector(".notice-banner")?.textContent).toContain(restartNoticeCopy);
    });
    // The shell's one line above already names the outage; this room adds no
    // second, page-local echo of the same fact for either list.
    expect(screen.queryByText(catalogPageCopy.workflowsUnavailable)).toBeNull();
    expect(screen.queryByText(catalogPageCopy.agentsUnavailable)).toBeNull();

    listWorkflowRevisions.mockResolvedValue({ items: [], next_after_revision_hash: null });
    listAgentDefinitionRevisions.mockResolvedValue({ items: [], next_after_revision_hash: null });
    reportConnectionRestored();

    expect((await screen.findByText(catalogPageCopy.catalogEmpty)).isConnected).toBe(true);
    expect(screen.queryByText(catalogPageCopy.workflowsUnavailable)).toBeNull();
    expect(screen.queryByText(catalogPageCopy.agentsUnavailable)).toBeNull();
  });
});
