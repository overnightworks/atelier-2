import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../../src/App.svelte";
import type {
  AgentConfigurationRevisionListItem,
  CockpitApi,
  JsonSchemaDocument,
  ProjectModelResolution,
  RunV3,
  WorkflowRevisionDetail
} from "../../src/api/client";
import {
  observedSourceHeading,
  pinnedModelLine,
  projectDefaultLine,
  startAccountSuffix,
  startUnavailableSuffix,
  workItemFor,
  workflowStartCopy
} from "../../src/lib/catalogPageCopy";
import { MutationJournal } from "../../src/lib/mutationJournal";
import { WORK_ITEM_ORDER_SCHEMA_REVISION } from "../../src/lib/orderSchema";
import { cockpitApiStub } from "../support/cockpitApi";
import { cancellableBlock } from "../support/runV3";

const revisionHash = "a".repeat(64);
const configurationHash = "b".repeat(64);
const publicReference = "run1.cnVuLW9yZGVy";
const workflowName = "cook-to-order";
const projectReference = "project1.dGVzdA";
const portionsArtifactHash = "9".repeat(64);

const portionsOrder = {
  name: "portions",
  schema: { ref: "portions-schema", revision: "schema-portions" }
};

const portionsSchema = {
  type: "object",
  required: ["portions"],
  additionalProperties: false,
  properties: { portions: { type: "integer", minimum: 0 } }
};

const workItemOrder = {
  name: "work",
  schema: { ref: "work-item-schema", revision: WORK_ITEM_ORDER_SCHEMA_REVISION }
};

// #438 Scheibe 1b (#1130): a string-schema order (`review_questions` on
// `diff-review`) and an object-schema order's Raw JSON door, each published
// as its own artifact before the start.
const reviewQuestionsOrder = {
  name: "review_questions",
  schema: { ref: "nonempty-string", revision: "5".repeat(64) }
};
const reviewQuestionsSchema = { type: "string", minLength: 1 };
const reviewQuestionsArtifactHash = "7".repeat(64);

const contextOrder = {
  name: "context",
  schema: { ref: "context-schema", revision: "6".repeat(64) }
};
const contextSchema = {
  type: "object",
  required: ["priority"],
  properties: { priority: { type: "string" } }
};
const contextArtifactHash = "8".repeat(64);

// #1130 fix round finding 1: a top-level object schema with a field the form
// cannot type -- an array, here, matching the real `accepted_sentences.json`
// shape -- classifies as `raw_object` and offers Raw JSON alone.
const weightsOrder = {
  name: "weights",
  schema: { ref: "weights-schema", revision: "0".repeat(64) }
};
const weightsSchema = {
  type: "object",
  required: ["sentences"],
  additionalProperties: false,
  properties: { sentences: { type: "array", items: { type: "string" } } }
};
const weightsArtifactHash = "3".repeat(64);

function stringAndObjectOrderSchema(schemaRevisionHash: string): JsonSchemaDocument {
  if (schemaRevisionHash === reviewQuestionsOrder.schema.revision) return reviewQuestionsSchema;
  if (schemaRevisionHash === contextOrder.schema.revision) return contextSchema;
  throw new Error(`unexpected schema revision ${schemaRevisionHash}`);
}

const groupedObservedQueueItems = {
  items: [
    {
      project_id: "atelier",
      tracker_item_reference: "gh:450",
      item_id: "1".repeat(64),
      revision: 0,
      title: "Preview door",
      title_observed_at: "2026-09-01T14:00:00Z",
      retired_at: null
    },
    {
      project_id: "atelier",
      tracker_item_reference: "gh:446",
      item_id: "3".repeat(64),
      revision: 0,
      title: null,
      title_observed_at: null,
      retired_at: null
    },
    {
      project_id: "infra",
      tracker_item_reference: "gl:12",
      item_id: "2".repeat(64),
      revision: 0,
      title: "Rotate keys",
      title_observed_at: "2026-09-01T14:00:00Z",
      retired_at: "2026-09-02T09:30:00Z"
    },
    {
      project_id: "infra",
      tracker_item_reference: "gl:13",
      item_id: "4".repeat(64),
      revision: 0,
      title: "Deploy runner",
      title_observed_at: "2026-09-01T14:00:00Z",
      retired_at: null
    }
  ],
  next_after: null
};

const workItemSchema = {
  title: "work item",
  type: "object",
  additionalProperties: false,
  required: ["body", "change_marker", "digest", "kind", "observed_at", "reference", "scope"],
  properties: {
    body: { type: "string" },
    change_marker: { type: "string" },
    digest: { type: "string" },
    kind: { type: "string" },
    observed_at: { type: "string" },
    reference: { type: "string" },
    scope: { type: "array" }
  }
};

function detail(orders = [portionsOrder]): WorkflowRevisionDetail {
  return {
    workflow_revision_hash: revisionHash,
    document_base64: "YQ==",
    provenance: null,
    graph: {
      workflow_format_version: 3,
      executable: true,
      not_executable_reason: null,
      node_count: 1,
      agent_roles: ["cook"],
      orders,
      wait_answer_schemas: [],
      node_previews: [],
      loops: [],
      name: workflowName,
      description: null
    }
  };
}

function startedRun(): RunV3 {
  return {
    workflow_format_version: 3,
    run_id: "run-order",
    public_run_reference: publicReference,
    workflow_revision_hash: revisionHash,
    workflow_name: "order material",
    agent_binding_set_hash: "c".repeat(64),
    run_configuration_revision_hash: "d".repeat(64),
    agent_bindings: [{
      role: "cook",
      agent_configuration_revision_hash: configurationHash,
      auth_profile_revision_hash: "f".repeat(64),
      profile_id: "test",
      revision_number: 1,
      provider_id: "test",
      auth_mode: "subscription",
      model: "cook-model",
      executor_revision: "immediate/v1"
    }],
    orders: [],
    state_version: 1,
    state: "STARTED",
    current_node_id: "cook",
    current_node_execution_id: revisionHash,
    node_rail: [{ node_id: "cook", state: "working", attempt: null }],
    cancellation: cancellableBlock(),
    terminal_hash: null,
    latest_event_cursor: null
  };
}

function api(overrides: Partial<CockpitApi> = {}): CockpitApi {
  return cockpitApiStub({
    listWorkflowRevisions: vi.fn(async () => ({
      items: [{
        workflow_revision_hash: revisionHash,
        workflow_format_version: 3 as const,
        executable: true,
        not_executable_reason: null,
        name: workflowName,
        description: null,
        provenance: null
      }],
      next_after_revision_hash: null
    })),
    getRevisionByName: vi.fn(async () => ({
      display_name: workflowName,
      lineage_id: "e".repeat(64),
      catalog_revision_hash: revisionHash,
      revision_number: 1
    })),
    getWorkflowRevision: vi.fn(async () => detail()),
    getSchemaRevision: vi.fn(async () => portionsSchema),
    listAgentConfigurationRevisions: vi.fn(async () => ({
      items: [{
        agent_configuration_revision_hash: configurationHash,
        provider_id: "test",
        model: "cook-model",
        auth_mode: "subscription" as const,
        auth_profile_revision_hash: "f".repeat(64),
        executor_revision: "immediate/v1",
        requested_capability: "headless" as const,
        startable: true,
        structurally_startable: true,
        not_startable_reason: null,
        provider_probe_problem_code: null,
        provider_probe_observed_at: null
      }],
      next_after_revision_hash: null
    })),
    listAuthProfileRevisions: vi.fn(async () => ({
      items: [{
        profile_id: "test",
        revision_number: 1,
        provider_id: "test",
        auth_mode: "subscription" as const,
        auth_profile_revision_hash: "f".repeat(64)
      }],
      next_after_revision_hash: null
    })),
    getModelRegistry: vi.fn(async () => ({
      provider_id: "test",
      revision_number: 1,
      model_registry_revision_hash: "9".repeat(64),
      entries: [{
        model_id: "cook-model",
        agent_configuration_revision_hash: configurationHash,
        source: "discovered" as const,
        provider_check: "checked" as const
      }]
    })),
    listProjects: vi.fn(async () => ({
      items: [{ public_project_reference: projectReference }]
    })),
    resolveProjectModels: vi.fn(async (
      _project: string,
      workflowHash: string,
      overrides: Parameters<CockpitApi["resolveProjectModels"]>[2]
    ) => {
      const chosen = overrides.find((override) => override.role === "cook")
        ?.agent_configuration_revision_hash ?? null;
      return {
        project_id: "test",
        public_project_reference: projectReference,
        workflow_revision_hash: workflowHash,
        resolutions: [{
          role: "cook",
          agent_configuration_revision_hash: chosen,
          source: chosen === null ? "uncast" as const : "chosen-now" as const,
          model_id: chosen === null ? null : "cook-model",
          declared_difficulty: 2 as const,
          default_difficulty: null,
          uncast_reason: chosen === null ? "no-project-default" as const : null,
          family_differs_from: null
        }]
      };
    }),
    start: vi.fn(async () => ({ status: 201, value: startedRun() })),
    getRun: vi.fn(async () => startedRun()),
    publishArtifact: vi.fn(async () => ({
      status: 201,
      value: { artifact_hash: portionsArtifactHash }
    })),
    ...overrides
  });
}

async function openStart(cockpitApi: CockpitApi): Promise<void> {
  window.history.replaceState(null, "", `/atelier/catalog/${encodeURIComponent(workflowName)}`);
  render(App, {
    props: {
      cockpitApi,
      mutationJournal: new MutationJournal(sessionStorage),
      createRunId: () => "run-order"
    }
  });
  await fireEvent.click(await screen.findByRole("button", { name: "Start" }));
  await screen.findByRole("dialog", { name: workflowStartCopy.startTitle(workflowName) });
  await waitFor(() => expect(screen.queryByText("Preparing…")).toBeNull());
}

beforeEach(() => sessionStorage.clear());

afterEach(() => {
  vi.restoreAllMocks();
  cleanup();
});

describe("the schema-generated fields on the catalog start sheet", () => {
  it("shows every declared order's material fields without its schema identifier or hash", async () => {
    const cockpitApi = api();
    await openStart(cockpitApi);

    const order = await screen.findByRole("group", { name: "Order portions" });
    expect(order.textContent).toContain("portions");
    expect(order.textContent).not.toContain("portions-schema");
    expect(order.textContent).not.toContain("schema-portions");
    expect(within(order).getByLabelText("portions (integer) *")).toBeTruthy();

    cleanup();
    const orderlessApi = api({ getWorkflowRevision: vi.fn(async () => detail([])) });
    await openStart(orderlessApi);
    expect(screen.queryByRole("group", { name: /^Order / })).toBeNull();
  });

  it("keeps Start run unavailable until every required schema field and role is chosen", async () => {
    const cockpitApi = api();
    await openStart(cockpitApi);

    const start = screen.getByRole("button", { name: "Start run" });
    expect((start as HTMLButtonElement).disabled).toBe(true);
    await fireEvent.change(screen.getByLabelText(workflowStartCopy.configurationFor("cook")), { target: { value: configurationHash } });
    expect((start as HTMLButtonElement).disabled).toBe(true);
    await fireEvent.input(screen.getByLabelText("portions (integer) *"), { target: { value: "7" } });
    expect((start as HTMLButtonElement).disabled).toBe(false);
  });

  it("publishes its typed schema values as an artifact and starts with the returned hash", async () => {
    const cockpitApi = api();
    await openStart(cockpitApi);

    await fireEvent.input(screen.getByLabelText("portions (integer) *"), { target: { value: "7" } });
    await fireEvent.change(screen.getByLabelText(workflowStartCopy.configurationFor("cook")), { target: { value: configurationHash } });
    await fireEvent.click(screen.getByRole("button", { name: "Start run" }));

    await waitFor(() => expect(cockpitApi.start).toHaveBeenCalledTimes(1));
    expect(cockpitApi.publishArtifact).toHaveBeenCalledWith('{"portions":7}');
    const mutation = vi.mocked(cockpitApi.start).mock.calls[0]?.[0];
    const request = JSON.parse(globalThis.atob(mutation?.body_base64 ?? ""));
    expect(request.orders).toEqual([{ name: "portions", artifact_hash: portionsArtifactHash }]);
    expect(request.agent_bindings).toEqual([
      { role: "cook", agent_configuration_revision_hash: configurationHash }
    ]);
    await waitFor(() =>
      expect(window.location.pathname).toBe(`/atelier/runs/${publicReference}`)
    );
  });

  it("groups observed work items by source and starts the selected item through its V3 wire shape", async () => {
    const cockpitApi = api({
      getWorkflowRevision: vi.fn(async () => detail([workItemOrder])),
      getSchemaRevision: vi.fn(async () => workItemSchema),
      listObservedQueueItems: vi.fn(async () => groupedObservedQueueItems)
    });
    await openStart(cockpitApi);

    const picker = screen.getByRole("combobox", { name: workItemFor("work") });
    const workItemOrderGroup = screen.getByRole("group", { name: workflowStartCopy.workItem });
    expect(workItemOrderGroup.textContent).not.toContain(`work-item-schema@${WORK_ITEM_ORDER_SCHEMA_REVISION}`);
    await fireEvent.click(picker);
    expect(screen.getByText(observedSourceHeading("atelier", workflowStartCopy.github))).toBeTruthy();
    expect(screen.getByText(observedSourceHeading("infra", workflowStartCopy.gitlab))).toBeTruthy();
    expect(screen.getByRole("option", { name: "#450 Preview door" })).toBeTruthy();
    expect(screen.getByRole("option", { name: "#446" })).toBeTruthy();
    expect(screen.queryByRole("option", { name: "!12 Rotate keys" })).toBeNull();
    expect(screen.getByRole("option", { name: "!13 Deploy runner" })).toBeTruthy();
    expect(screen.queryByText(`${workflowStartCopy.github} · gh:450`)).toBeNull();
    expect(screen.queryByRole("note")).toBeNull();
    expect(screen.getByRole("group", { name: "Roles" })).toBeTruthy();

    expect(screen.getByRole("listbox", { name: workItemFor("work") })).toBeTruthy();
    await fireEvent.click(screen.getByRole("option", { name: "#450 Preview door" }));
    expect(picker.textContent).toContain("#450 Preview door");
    await fireEvent.change(screen.getByLabelText(workflowStartCopy.configurationFor("cook")), {
      target: { value: configurationHash }
    });
    expect((screen.getByLabelText(workflowStartCopy.configurationFor("cook")) as HTMLSelectElement).value).toBe(
      configurationHash
    );
    await waitFor(() => expect(screen.getByText("Chosen now")).toBeTruthy());
    await fireEvent.click(screen.getByRole("button", { name: "Start run" }));

    await waitFor(() => expect(cockpitApi.start).toHaveBeenCalledTimes(1));
    const mutation = vi.mocked(cockpitApi.start).mock.calls[0]?.[0];
    const request = JSON.parse(globalThis.atob(mutation?.body_base64 ?? ""));
    expect(request.orders).toEqual([{ name: "work", work_item: "gh:450" }]);
  });

  it("walks the work-item picker by keyboard, focuses the filter field while open, returns focus to the combobox once closed, and names the listbox", async () => {
    const cockpitApi = api({
      getWorkflowRevision: vi.fn(async () => detail([workItemOrder])),
      getSchemaRevision: vi.fn(async () => workItemSchema),
      listObservedQueueItems: vi.fn(async () => groupedObservedQueueItems)
    });
    await openStart(cockpitApi);

    const picker = screen.getByRole("combobox", { name: workItemFor("work") });
    picker.focus();
    expect(document.activeElement).toBe(picker);

    await fireEvent.keyDown(picker, { key: "ArrowDown" });
    const listbox = screen.getByRole("listbox", { name: workItemFor("work") });
    expect(listbox).toBeTruthy();
    const first = screen.getByRole("option", { name: "#450 Preview door" });
    const second = screen.getByRole("option", { name: "#446" });
    const third = screen.getByRole("option", { name: "!13 Deploy runner" });
    // REQ-UIQ-05: opening lands both real keyboard focus and the combobox
    // contract's `aria-activedescendant` in the filter field, the one place
    // ArrowUp/ArrowDown/Enter/Escape actually reach as a keyboard user tabs
    // or types through the picker -- not the still-labelled but now-inert
    // combobox button, which keeps only the closed-state contract.
    let filterField = screen.getByLabelText(workflowStartCopy.filterWorkItemsLabel);
    expect(filterField.getAttribute("aria-activedescendant")).toBe(first.id);
    await waitFor(() => expect(document.activeElement).toBe(filterField));

    await fireEvent.keyDown(picker, { key: "ArrowDown" });
    expect(filterField.getAttribute("aria-activedescendant")).toBe(second.id);
    await fireEvent.keyDown(picker, { key: "ArrowDown" });
    expect(filterField.getAttribute("aria-activedescendant")).toBe(third.id);
    await fireEvent.keyDown(picker, { key: "ArrowUp" });
    expect(filterField.getAttribute("aria-activedescendant")).toBe(second.id);

    await fireEvent.keyDown(picker, { key: "Escape" });
    expect(screen.queryByRole("listbox", { name: workItemFor("work") })).toBeNull();
    expect(screen.getByRole("dialog", { name: workflowStartCopy.startTitle(workflowName) })).toBeTruthy();
    expect(document.activeElement).toBe(picker);
    expect(picker.textContent).toContain("Choose");

    await fireEvent.keyDown(picker, { key: "ArrowUp" });
    expect(screen.getByRole("listbox", { name: workItemFor("work") })).toBeTruthy();
    filterField = screen.getByLabelText(workflowStartCopy.filterWorkItemsLabel);
    expect(filterField.getAttribute("aria-activedescendant")).toBe(
      screen.getByRole("option", { name: "!13 Deploy runner" }).id
    );
    await fireEvent.keyDown(picker, { key: " " });
    expect(screen.queryByRole("listbox", { name: workItemFor("work") })).toBeNull();
    expect(picker.textContent).toContain("!13 Deploy runner");
    expect(document.activeElement).toBe(picker);

    await fireEvent.keyDown(picker, { key: "ArrowDown" });
    filterField = screen.getByLabelText(workflowStartCopy.filterWorkItemsLabel);
    expect(filterField.getAttribute("aria-activedescendant")).toBe(
      screen.getByRole("option", { name: "!13 Deploy runner" }).id
    );
    await fireEvent.keyDown(picker, { key: "ArrowUp" });
    expect(filterField.getAttribute("aria-activedescendant")).toBe(
      screen.getByRole("option", { name: "#446" }).id
    );
    await fireEvent.keyDown(picker, { key: "Enter" });
    expect(screen.queryByRole("listbox", { name: workItemFor("work") })).toBeNull();
    expect(picker.textContent).toContain("#446");
    expect(document.activeElement).toBe(picker);
  });

  it("holds Start when a work-item order has no observed source and leads to Settings", async () => {
    const cockpitApi = api({
      getWorkflowRevision: vi.fn(async () => detail([workItemOrder])),
      getSchemaRevision: vi.fn(async () => workItemSchema)
    });
    await openStart(cockpitApi);

    expect(screen.getByText(workflowStartCopy.noSource)).toBeTruthy();
    await fireEvent.change(screen.getByLabelText(workflowStartCopy.configurationFor("cook")), {
      target: { value: configurationHash }
    });
    const startRun = screen.getByRole("button", { name: workflowStartCopy.startRun }) as HTMLButtonElement;
    expect(startRun.disabled).toBe(true);
    expect(startRun.title).toBe(workflowStartCopy.startNeedsWorkItemSource);
    expect(screen.queryByText(workflowStartCopy.startNeedsWorkItem)).toBeNull();
    await fireEvent.click(screen.getByRole("button", { name: workflowStartCopy.connectSource }));
    await waitFor(() => expect(window.location.pathname).toBe("/atelier/settings"));
  });

  it("shows the honest all-retired state, not a Choose that opens nothing, when every observed item from a connected source is retired", async () => {
    const cockpitApi = api({
      getWorkflowRevision: vi.fn(async () => detail([workItemOrder])),
      getSchemaRevision: vi.fn(async () => workItemSchema),
      listObservedQueueItems: vi.fn(async () => ({
        items: [
          {
            project_id: "atelier",
            tracker_item_reference: "gh:450",
            item_id: "1".repeat(64),
            revision: 0,
            title: "Preview door",
            title_observed_at: "2026-09-01T14:00:00Z",
            retired_at: "2026-09-02T09:30:00Z"
          }
        ],
        next_after: null
      }))
    });
    await openStart(cockpitApi);

    expect(screen.queryByText(workflowStartCopy.noSource)).toBeNull();
    expect(screen.queryByRole("button", { name: workflowStartCopy.connectSource })).toBeNull();
    expect(screen.getByText(workflowStartCopy.allRetired)).toBeTruthy();
    expect(screen.queryByRole("combobox", { name: workItemFor("work") })).toBeNull();
    await fireEvent.change(screen.getByLabelText(workflowStartCopy.configurationFor("cook")), {
      target: { value: configurationHash }
    });
    const startRun = screen.getByRole("button", { name: workflowStartCopy.startRun }) as HTMLButtonElement;
    expect(startRun.disabled).toBe(true);
    expect(startRun.title).toBe(workflowStartCopy.startNeedsWorkItem);
  });

  it("refuses a work-item lookalike before it can enable Start", async () => {
    const cockpitApi = api({
      getWorkflowRevision: vi.fn(async () =>
        detail([{ ...workItemOrder, schema: { ...workItemOrder.schema, revision: "f".repeat(64) } }])
      ),
      getSchemaRevision: vi.fn(async () => workItemSchema)
    });
    await openStart(cockpitApi);

    expect((await screen.findByRole("status")).textContent).toContain("canonical work-item schema");
    await fireEvent.change(screen.getByLabelText(workflowStartCopy.configurationFor("cook")), {
      target: { value: configurationHash }
    });
    expect((screen.getByRole("button", { name: "Start run" }) as HTMLButtonElement).disabled).toBe(true);
    expect(cockpitApi.start).not.toHaveBeenCalled();
  });

  it("refuses a scalar schema before it can send an invented object", async () => {
    const cockpitApi = api({
      getWorkflowRevision: vi.fn(async () =>
        detail([{ name: "approved", schema: { ref: "flag", revision: "schema-flag" } }])
      ),
      getSchemaRevision: vi.fn(async () => ({ type: "boolean" }))
    });
    await openStart(cockpitApi);

    expect((await screen.findByRole("status")).textContent).toContain(
      "not a string, an object, or a work item"
    );
    await fireEvent.change(screen.getByLabelText(workflowStartCopy.configurationFor("cook")), {
      target: { value: configurationHash }
    });
    expect((screen.getByRole("button", { name: "Start run" }) as HTMLButtonElement).disabled).toBe(true);
    expect(cockpitApi.start).not.toHaveBeenCalled();
  });

  it("opens modally, contains keyboard focus, and returns it to Start on Escape", async () => {
    const showModal = vi.fn(function (this: HTMLDialogElement) {
      this.open = true;
    });
    Object.defineProperty(HTMLDialogElement.prototype, "showModal", {
      configurable: true,
      value: showModal
    });
    const cockpitApi = api();
    window.history.replaceState(null, "", `/atelier/catalog/${encodeURIComponent(workflowName)}`);
    render(App, {
      props: {
        cockpitApi,
        mutationJournal: new MutationJournal(sessionStorage),
        createRunId: () => "run-order"
      }
    });
    const start = await screen.findByRole("button", { name: "Start" });
    start.focus();
    await fireEvent.click(start);
    const dialog = await screen.findByRole("dialog", { name: workflowStartCopy.startTitle(workflowName) });
    await waitFor(() => expect(screen.queryByText("Preparing…")).toBeNull());
    const cancel = within(dialog).getByRole("button", { name: "Cancel" });
    const portions = within(dialog).getByLabelText("portions (integer) *");
    expect(showModal).toHaveBeenCalledOnce();
    expect(document.activeElement).toBe(cancel);
    expect(within(dialog).getAllByRole("button", { name: "Cancel" })).toHaveLength(1);

    await fireEvent.keyDown(cancel, { key: "Tab" });
    expect(document.activeElement).toBe(portions);
    await fireEvent.keyDown(portions, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(cancel);
    await fireEvent.keyDown(dialog, { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: workflowStartCopy.startTitle(workflowName) })).toBeNull();
    await waitFor(() => expect(document.activeElement).toBe(start));
  });
});

describe("string orders and Raw JSON on the catalog start sheet (#438 Scheibe 1b)", () => {
  it("publishes a string order's typed text as its artifact and starts with the returned hash", async () => {
    const cockpitApi = api({
      getWorkflowRevision: vi.fn(async () => detail([reviewQuestionsOrder])),
      getSchemaRevision: vi.fn(async (hash: string) => stringAndObjectOrderSchema(hash)),
      publishArtifact: vi.fn(async () => ({
        status: 201,
        value: { artifact_hash: reviewQuestionsArtifactHash }
      }))
    });
    await openStart(cockpitApi);

    const group = screen.getByRole("group", { name: "Order review_questions" });
    await fireEvent.input(within(group).getByLabelText(workflowStartCopy.orderText), {
      target: { value: "Does the retry stay idempotent?" }
    });
    await fireEvent.change(screen.getByLabelText(workflowStartCopy.configurationFor("cook")), {
      target: { value: configurationHash }
    });
    await fireEvent.click(screen.getByRole("button", { name: "Start run" }));

    await waitFor(() => expect(cockpitApi.start).toHaveBeenCalledTimes(1));
    expect(cockpitApi.publishArtifact).toHaveBeenCalledWith("Does the retry stay idempotent?");
    const mutation = vi.mocked(cockpitApi.start).mock.calls[0]?.[0];
    const request = JSON.parse(globalThis.atob(mutation?.body_base64 ?? ""));
    expect(request.orders).toEqual([
      { name: "review_questions", artifact_hash: reviewQuestionsArtifactHash }
    ]);
  });

  it("reads a string order's file content into its exact text", async () => {
    const cockpitApi = api({
      getWorkflowRevision: vi.fn(async () => detail([reviewQuestionsOrder])),
      getSchemaRevision: vi.fn(async (hash: string) => stringAndObjectOrderSchema(hash))
    });
    await openStart(cockpitApi);

    const group = screen.getByRole("group", { name: "Order review_questions" });
    const file = new File(
      ["Does the diff keep the retry idempotent?"],
      "questions.txt",
      { type: "text/plain" }
    );
    const fileInput = within(group).getByLabelText(
      workflowStartCopy.publishFromFile("review_questions")
    ) as HTMLInputElement;
    await fireEvent.change(fileInput, { target: { files: [file] } });

    await waitFor(() =>
      expect(
        (within(group).getByLabelText(workflowStartCopy.orderText) as HTMLTextAreaElement).value
      ).toBe("Does the diff keep the retry idempotent?")
    );
  });

  it("publishes an object order's Raw JSON text as its artifact and starts with the returned hash", async () => {
    const cockpitApi = api({
      getWorkflowRevision: vi.fn(async () => detail([contextOrder])),
      getSchemaRevision: vi.fn(async (hash: string) => stringAndObjectOrderSchema(hash)),
      publishArtifact: vi.fn(async () => ({
        status: 201,
        value: { artifact_hash: contextArtifactHash }
      }))
    });
    await openStart(cockpitApi);

    const group = screen.getByRole("group", { name: "Order context" });
    await fireEvent.click(within(group).getByText(workflowStartCopy.rawJson));
    await fireEvent.input(within(group).getByLabelText(workflowStartCopy.rawJsonFor("context")), {
      target: { value: '{"priority": "high"}' }
    });
    await fireEvent.change(screen.getByLabelText(workflowStartCopy.configurationFor("cook")), {
      target: { value: configurationHash }
    });
    await fireEvent.click(screen.getByRole("button", { name: "Start run" }));

    await waitFor(() => expect(cockpitApi.start).toHaveBeenCalledTimes(1));
    expect(cockpitApi.publishArtifact).toHaveBeenCalledWith('{"priority": "high"}');
    const mutation = vi.mocked(cockpitApi.start).mock.calls[0]?.[0];
    const request = JSON.parse(globalThis.atob(mutation?.body_base64 ?? ""));
    expect(request.orders).toEqual([{ name: "context", artifact_hash: contextArtifactHash }]);
  });

  it("refuses invalid Raw JSON with its position, names the way out, and keeps Start locked", async () => {
    const cockpitApi = api({
      getWorkflowRevision: vi.fn(async () => detail([contextOrder])),
      getSchemaRevision: vi.fn(async (hash: string) => stringAndObjectOrderSchema(hash))
    });
    await openStart(cockpitApi);

    const group = screen.getByRole("group", { name: "Order context" });
    await fireEvent.click(within(group).getByText(workflowStartCopy.rawJson));
    await fireEvent.input(within(group).getByLabelText(workflowStartCopy.rawJsonFor("context")), {
      target: { value: '{priority: "high"}' }
    });
    await fireEvent.change(screen.getByLabelText(workflowStartCopy.configurationFor("cook")), {
      target: { value: configurationHash }
    });

    const alert = await within(group).findByRole("alert");
    expect(alert.textContent).toContain("line 1");
    expect(alert.textContent).toContain(workflowStartCopy.rawJsonWayOutBesideForm);
    expect((screen.getByRole("button", { name: "Start run" }) as HTMLButtonElement).disabled).toBe(true);
    expect(cockpitApi.publishArtifact).not.toHaveBeenCalled();
  });

  it("renders Raw JSON alone for an object order the form cannot encode, and starts with the returned hash (#1130 finding 1)", async () => {
    const cockpitApi = api({
      getWorkflowRevision: vi.fn(async () => detail([weightsOrder])),
      getSchemaRevision: vi.fn(async () => weightsSchema),
      publishArtifact: vi.fn(async () => ({
        status: 201,
        value: { artifact_hash: weightsArtifactHash }
      }))
    });
    await openStart(cockpitApi);

    const group = screen.getByRole("group", { name: "Order weights" });
    expect(within(group).queryByText(workflowStartCopy.rawJson)).toBeNull();
    expect(within(group).queryByLabelText(/sentences/)).toBeNull();
    await fireEvent.input(within(group).getByLabelText(workflowStartCopy.rawJsonFor("weights")), {
      target: { value: '{"sentences": ["a", "b"]}' }
    });
    await fireEvent.change(screen.getByLabelText(workflowStartCopy.configurationFor("cook")), {
      target: { value: configurationHash }
    });
    await fireEvent.click(screen.getByRole("button", { name: "Start run" }));

    await waitFor(() => expect(cockpitApi.start).toHaveBeenCalledTimes(1));
    expect(cockpitApi.publishArtifact).toHaveBeenCalledWith('{"sentences": ["a", "b"]}');
    const mutation = vi.mocked(cockpitApi.start).mock.calls[0]?.[0];
    const request = JSON.parse(globalThis.atob(mutation?.body_base64 ?? ""));
    expect(request.orders).toEqual([{ name: "weights", artifact_hash: weightsArtifactHash }]);
  });

  it("names the no-form way out for invalid Raw JSON on an order Raw JSON alone can reach (#1130 finding 2)", async () => {
    const cockpitApi = api({
      getWorkflowRevision: vi.fn(async () => detail([weightsOrder])),
      getSchemaRevision: vi.fn(async () => weightsSchema)
    });
    await openStart(cockpitApi);

    const group = screen.getByRole("group", { name: "Order weights" });
    await fireEvent.input(within(group).getByLabelText(workflowStartCopy.rawJsonFor("weights")), {
      target: { value: '{sentences: ["a", "b"]}' }
    });
    await fireEvent.change(screen.getByLabelText(workflowStartCopy.configurationFor("cook")), {
      target: { value: configurationHash }
    });

    const alert = await within(group).findByRole("alert");
    expect(alert.textContent).toContain(workflowStartCopy.rawJsonWayOutAlone);
    expect(alert.textContent).not.toContain(workflowStartCopy.rawJsonWayOutBesideForm);
    expect((screen.getByRole("button", { name: "Start run" }) as HTMLButtonElement).disabled).toBe(true);
    expect(cockpitApi.publishArtifact).not.toHaveBeenCalled();
  });
});

function resolvedRole(
  changes: Partial<ProjectModelResolution["resolutions"][number]> = {}
): ProjectModelResolution["resolutions"][number] {
  return {
    role: "cook",
    agent_configuration_revision_hash: configurationHash,
    source: "from-project",
    model_id: "cook-model",
    declared_difficulty: 2,
    default_difficulty: 2,
    uncast_reason: null,
    family_differs_from: null,
    ...changes
  };
}

function configuration(
  hash: string,
  model: string,
  startable = true
): AgentConfigurationRevisionListItem {
  return {
    agent_configuration_revision_hash: hash,
    provider_id: "test",
    model,
    auth_mode: "subscription",
    auth_profile_revision_hash: "f".repeat(64),
    executor_revision: "immediate/v1",
    requested_capability: "headless",
    startable,
    structurally_startable: startable,
    not_startable_reason: startable ? null : "agent-executor-binding-unavailable",
    provider_probe_problem_code: null,
    provider_probe_observed_at: null
  };
}

function modelApi(
  resolveProjectModels: CockpitApi["resolveProjectModels"],
  configurations: readonly AgentConfigurationRevisionListItem[] = [
    configuration(configurationHash, "cook-model")
  ],
  overrides: Partial<CockpitApi> = {}
): CockpitApi {
  return api({
    getWorkflowRevision: vi.fn(async () => detail([])),
    listAgentConfigurationRevisions: vi.fn(async () => ({
      items: [...configurations],
      next_after_revision_hash: null
    })),
    getModelRegistry: vi.fn(async () => ({
      provider_id: "test",
      revision_number: 1,
      model_registry_revision_hash: "9".repeat(64),
      entries: configurations.map((item) => ({
        model_id: item.model,
        agent_configuration_revision_hash: item.agent_configuration_revision_hash,
        source: "discovered" as const,
        provider_check: "checked" as const
      }))
    })),
    resolveProjectModels,
    ...overrides
  });
}

function projectResolution(
  workflowHash: string,
  resolutions: ProjectModelResolution["resolutions"]
): ProjectModelResolution {
  return {
    project_id: "test",
    public_project_reference: projectReference,
    workflow_revision_hash: workflowHash,
    resolutions
  };
}

describe("the catalog start sheet's project model resolution", () => {
  it("refuses a resolution with an extra role instead of starting a changed cast", async () => {
    const resolveProjectModels = vi.fn(async (
      _project: string,
      workflowHash: string
    ) => projectResolution(workflowHash, [
      resolvedRole(),
      resolvedRole({ role: "extra" })
    ]));
    const cockpitApi = modelApi(resolveProjectModels);
    await openStart(cockpitApi);

    expect(screen.getByRole("alert").textContent).toContain(
      workflowStartCopy.rolesUnresolved
    );
    expect(cockpitApi.start).not.toHaveBeenCalled();
  });

  it("shows next-higher provenance and starts defaults only after resolving them again", async () => {
    const resolveProjectModels = vi.fn(async (
      _project: string,
      workflowHash: string
    ) => projectResolution(workflowHash, [resolvedRole({ default_difficulty: 3 })]));
    const cockpitApi = modelApi(resolveProjectModels);
    await openStart(cockpitApi);

    const picker = screen.getByLabelText(workflowStartCopy.configurationFor("cook"));
    expect((picker as HTMLSelectElement).value).toBe(configurationHash);
    expect(within(picker).getByRole("option", {
      name: projectDefaultLine(2, "cook-model", true, startAccountSuffix("test"), "")
    })).toBeTruthy();
    expect(screen.queryByText("Next higher difficulty")).toBeNull();
    await fireEvent.click(screen.getByRole("button", { name: "Start run" }));

    await waitFor(() => expect(cockpitApi.start).toHaveBeenCalledTimes(1));
    expect(resolveProjectModels).toHaveBeenCalledTimes(2);
    const mutation = vi.mocked(cockpitApi.start).mock.calls[0]?.[0];
    const request = JSON.parse(globalThis.atob(mutation?.body_base64 ?? ""));
    expect(request.agent_bindings).toEqual([]);
  });

  it("shows a workflow pin, lets this run override it, and sends only that override", async () => {
    const resolveProjectModels = vi.fn(async (
      _project: string,
      workflowHash: string,
      overrides: Parameters<CockpitApi["resolveProjectModels"]>[2]
    ) => projectResolution(workflowHash, [resolvedRole({
      source: overrides.length === 0 ? "pinned-in-workflow" : "chosen-now",
      default_difficulty: null
    })]));
    const cockpitApi = modelApi(resolveProjectModels);
    await openStart(cockpitApi);

    expect(within(screen.getByLabelText(workflowStartCopy.configurationFor("cook"))).getByRole("option", {
      name: pinnedModelLine("cook-model", startAccountSuffix("test"), "")
    })).toBeTruthy();
    await fireEvent.change(screen.getByLabelText(workflowStartCopy.configurationFor("cook")), {
      target: { value: configurationHash }
    });
    await waitFor(() => expect(screen.getByText("Chosen now")).toBeTruthy());
    await fireEvent.click(screen.getByRole("button", { name: "Start run" }));

    await waitFor(() => expect(cockpitApi.start).toHaveBeenCalledTimes(1));
    const mutation = vi.mocked(cockpitApi.start).mock.calls[0]?.[0];
    const request = JSON.parse(globalThis.atob(mutation?.body_base64 ?? ""));
    expect(request.agent_bindings).toEqual([{
      role: "cook",
      agent_configuration_revision_hash: configurationHash
    }]);
  });

  it("uses the dropdown as the one ochre Choose carrier and names why Start is disabled", async () => {
    const resolveProjectModels = vi.fn(async (
      _project: string,
      workflowHash: string,
      overrides: Parameters<CockpitApi["resolveProjectModels"]>[2]
    ) => projectResolution(workflowHash, [overrides.length === 0
      ? resolvedRole({
          agent_configuration_revision_hash: null,
          source: "uncast",
          model_id: null,
          default_difficulty: null,
          uncast_reason: "family-difference-unavailable",
          family_differs_from: "builder"
        })
      : resolvedRole({ source: "chosen-now", default_difficulty: null })]));
    const cockpitApi = modelApi(resolveProjectModels);
    await openStart(cockpitApi);

    const picker = screen.getByLabelText(workflowStartCopy.configurationFor("cook"));
    expect(picker.classList).toContain("needs-choice");
    expect(screen.getAllByText("Choose")).toHaveLength(1);
    expect((screen.getByRole("button", { name: "Start run" }) as HTMLButtonElement).title).toBe(
      workflowStartCopy.startNeedsConfiguration("cook")
    );
    expect(screen.queryByRole("button", { name: "Why cook needs a configuration" })).toBeNull();

    await fireEvent.change(picker, { target: { value: configurationHash } });
    await waitFor(() => expect(screen.getByText("Chosen now")).toBeTruthy());
    expect(resolveProjectModels.mock.calls.at(-1)?.[2]).toEqual([{
      role: "cook",
      agent_configuration_revision_hash: configurationHash
    }]);
    expect((screen.getByRole("button", { name: "Start run" }) as HTMLButtonElement).disabled)
      .toBe(false);
  });

  it("returns a refused override to Choose and keeps Start honestly disabled", async () => {
    const otherHash = "8".repeat(64);
    const resolveProjectModels = vi.fn(async (
      _project: string,
      workflowHash: string,
      overrides: Parameters<CockpitApi["resolveProjectModels"]>[2]
    ) => projectResolution(workflowHash, [overrides.length === 0
      ? resolvedRole()
      : resolvedRole({
          agent_configuration_revision_hash: null,
          source: "uncast",
          model_id: null,
          default_difficulty: null,
          uncast_reason: "override-not-registered"
        })]));
    const cockpitApi = modelApi(resolveProjectModels, [
      configuration(configurationHash, "cook-model"),
      configuration(otherHash, "other-model")
    ]);
    await openStart(cockpitApi);

    await fireEvent.change(screen.getByLabelText(workflowStartCopy.configurationFor("cook")), {
      target: { value: otherHash }
    });
    await waitFor(() => expect(
      (screen.getByLabelText(workflowStartCopy.configurationFor("cook")) as HTMLSelectElement).value
    ).toBe(""));
    expect(screen.getAllByText("Choose")).toHaveLength(1);
    expect((screen.getByRole("button", { name: "Start run" }) as HTMLButtonElement).title).toBe(
      workflowStartCopy.startNeedsConfiguration("cook")
    );
    expect(screen.queryByRole("button", { name: "Why cook needs a configuration" })).toBeNull();
    expect(cockpitApi.start).not.toHaveBeenCalled();
  });

  it("keeps an unavailable resolved configuration selected until a healthy override replaces it", async () => {
    const healthyHash = "8".repeat(64);
    const resolveProjectModels = vi.fn(async (
      _project: string,
      workflowHash: string,
      overrides: Parameters<CockpitApi["resolveProjectModels"]>[2]
    ) => projectResolution(workflowHash, [overrides.length === 0
      ? resolvedRole()
      : resolvedRole({
          agent_configuration_revision_hash: healthyHash,
          source: "chosen-now",
          model_id: "healthy-model",
          default_difficulty: null
        })]));
    const cockpitApi = modelApi(resolveProjectModels, [
      configuration(configurationHash, "cook-model", false),
      configuration(healthyHash, "healthy-model")
    ]);
    await openStart(cockpitApi);

    const picker = screen.getByLabelText(workflowStartCopy.configurationFor("cook")) as HTMLSelectElement;
    expect(picker.value).toBe(configurationHash);
    expect(picker.selectedOptions[0]?.disabled).toBe(true);
    expect(picker.selectedOptions[0]?.textContent).toBe(
      projectDefaultLine(2, "cook-model", false, startAccountSuffix("test"), startUnavailableSuffix())
    );
    expect((screen.getByRole("button", { name: "Start run" }) as HTMLButtonElement).disabled)
      .toBe(true);

    await fireEvent.change(picker, { target: { value: healthyHash } });
    await waitFor(() => expect(screen.getByText("Chosen now")).toBeTruthy());
    expect(picker.value).toBe(healthyHash);
    expect((screen.getByRole("button", { name: "Start run" }) as HTMLButtonElement).disabled)
      .toBe(false);
  });

  it("drops a vanished project default during the mandatory pre-start resolution", async () => {
    const resolveProjectModels = vi
      .fn<CockpitApi["resolveProjectModels"]>()
      .mockImplementationOnce(async (_project, workflowHash) =>
        projectResolution(workflowHash, [resolvedRole()]))
      .mockImplementationOnce(async (_project, workflowHash) =>
        projectResolution(workflowHash, [resolvedRole({
          agent_configuration_revision_hash: null,
          source: "uncast",
          model_id: null,
          default_difficulty: null,
          uncast_reason: "no-project-default"
        })]));
    const cockpitApi = modelApi(resolveProjectModels);
    await openStart(cockpitApi);

    expect((screen.getByLabelText(workflowStartCopy.configurationFor("cook")) as HTMLSelectElement)
      .selectedOptions[0]?.textContent).toBe(
      projectDefaultLine(2, "cook-model", false, startAccountSuffix("test"), "")
    );
    await fireEvent.click(screen.getByRole("button", { name: "Start run" }));

    await waitFor(() => expect(
      (screen.getByLabelText(workflowStartCopy.configurationFor("cook")) as HTMLSelectElement).value
    ).toBe(""));
    expect(screen.getAllByText("Choose")).toHaveLength(1);
    expect(cockpitApi.start).not.toHaveBeenCalled();
  });

  it("does not let a late resolution replace a newer manual choice", async () => {
    const olderHash = "7".repeat(64);
    const newerHash = "8".repeat(64);
    let releaseOlder!: (resolution: ProjectModelResolution) => void;
    const olderResolution = new Promise<ProjectModelResolution>((resolve) => {
      releaseOlder = resolve;
    });
    const resolveProjectModels = vi.fn((
      _project: string,
      workflowHash: string,
      overrides: Parameters<CockpitApi["resolveProjectModels"]>[2]
    ) => {
      const selected = overrides[0]?.agent_configuration_revision_hash;
      if (selected === olderHash) return olderResolution;
      return Promise.resolve(projectResolution(workflowHash, [resolvedRole({
        agent_configuration_revision_hash: selected ?? configurationHash,
        source: selected === undefined ? "from-project" : "chosen-now",
        model_id: selected === newerHash ? "newer-model" : "cook-model",
        default_difficulty: selected === undefined ? 2 : null
      })]));
    });
    const cockpitApi = modelApi(resolveProjectModels, [
      configuration(configurationHash, "cook-model"),
      configuration(olderHash, "older-model"),
      configuration(newerHash, "newer-model")
    ]);
    await openStart(cockpitApi);

    const picker = screen.getByLabelText(workflowStartCopy.configurationFor("cook")) as HTMLSelectElement;
    await fireEvent.change(picker, { target: { value: olderHash } });
    await fireEvent.change(picker, { target: { value: newerHash } });
    await waitFor(() => expect(picker.value).toBe(newerHash));
    releaseOlder(projectResolution(revisionHash, [resolvedRole({
      agent_configuration_revision_hash: olderHash,
      source: "chosen-now",
      model_id: "older-model",
      default_difficulty: null
    })]));

    await waitFor(() => expect(resolveProjectModels).toHaveBeenCalledTimes(3));
    expect(picker.value).toBe(newerHash);
    expect(picker.selectedOptions[0]?.textContent).toContain("newer-model");
  });

  it.each([
    "workflow-model-not-registered",
    "workflow-model-ambiguous"
  ] as const)("keeps Start disabled when the %s refusal has no configuration", async (reason) => {
    const resolveProjectModels = vi.fn(async (
      _project: string,
      workflowHash: string
    ) => projectResolution(workflowHash, [resolvedRole({
      agent_configuration_revision_hash: null,
      source: "uncast",
      model_id: null,
      default_difficulty: null,
      uncast_reason: reason
    })]));
    await openStart(modelApi(resolveProjectModels));

    expect(screen.getAllByText("Choose")).toHaveLength(1);
    expect((screen.getByRole("button", { name: "Start run" }) as HTMLButtonElement).title).toBe(
      workflowStartCopy.startNeedsConfiguration("cook")
    );
    expect(screen.queryByRole("button", { name: "Why cook needs a configuration" })).toBeNull();
  });

  it.each(["registry", "profile"] as const)(
    "excludes a configuration whose %s provider does not join the checked tuple",
    async (mismatch) => {
      const resolveProjectModels = vi.fn(async (
        _project: string,
        workflowHash: string
      ) => projectResolution(workflowHash, [resolvedRole({
        agent_configuration_revision_hash: null,
        source: "uncast",
        model_id: null,
        default_difficulty: null,
        uncast_reason: "no-project-default"
      })]));
      const overrides: Partial<CockpitApi> = mismatch === "registry"
        ? { getModelRegistry: vi.fn(async () => ({
            provider_id: "other",
            revision_number: 1,
            model_registry_revision_hash: "9".repeat(64),
            entries: [{
              model_id: "cook-model",
              agent_configuration_revision_hash: configurationHash,
              source: "discovered" as const,
              provider_check: "checked" as const
            }]
          })) }
        : { listAuthProfileRevisions: vi.fn(async () => ({
            items: [{
              profile_id: "test",
              revision_number: 1,
              provider_id: "other",
              auth_mode: "subscription" as const,
              auth_profile_revision_hash: "f".repeat(64)
            }],
            next_after_revision_hash: null
          })) };
      await openStart(modelApi(resolveProjectModels, undefined, overrides));

      const picker = screen.getByLabelText(workflowStartCopy.configurationFor("cook"));
      expect(within(picker).queryByRole("option", { name: /cook-model/ })).toBeNull();
      expect(screen.getAllByText("Choose")).toHaveLength(1);
    }
  );

  it.each([
    ["changed hash", [{ ...startedRun().agent_bindings[0]!, agent_configuration_revision_hash: "8".repeat(64) }]],
    ["extra role", [startedRun().agent_bindings[0]!, { ...startedRun().agent_bindings[0]!, role: "extra" }]]
  ] as const)("refuses a start response with a %s", async (_case, agentBindings) => {
    const resolveProjectModels = vi.fn(async (
      _project: string,
      workflowHash: string
    ) => projectResolution(workflowHash, [resolvedRole()]));
    const cockpitApi = modelApi(resolveProjectModels, undefined, {
      start: vi.fn(async () => ({
        status: 201,
        value: { ...startedRun(), agent_bindings: [...agentBindings] }
      }))
    });
    await openStart(cockpitApi);

    await fireEvent.click(screen.getByRole("button", { name: "Start run" }));

    expect((await screen.findByRole("alert")).textContent).toContain(
      "The start response changed the selected roles."
    );
    expect(screen.getByLabelText(workflowStartCopy.configurationFor("cook"))).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Start run" })).toBeNull();
    expect(screen.getByRole("button", { name: "Try again" })).toBeTruthy();
    expect(window.location.pathname).toBe(`/atelier/catalog/${encodeURIComponent(workflowName)}`);
  });
});
