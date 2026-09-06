import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/svelte";
import axe from "axe-core";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "../../src/App.svelte";
import type { CockpitApi, ObservedQueueItemPage, WorkflowRevisionDetail } from "../../src/api/client";
import {
  observedSourceHeading,
  workItemFor,
  workflowStartCopy
} from "../../src/lib/catalogPageCopy";
import { MutationJournal } from "../../src/lib/mutationJournal";
import { WORK_ITEM_ORDER_SCHEMA_REVISION } from "../../src/lib/orderSchema";
import { cockpitApiStub } from "../support/cockpitApi";

/**
 * The start sheet's work-item picker filter (#962 Scheibe 8, operator ruling
 * 31.08. line 4): typing narrows the grouped list by reference or title, and
 * the honest "no match" empty state replaces a silently unopenable `Choose`
 * button. The all-retired gate is `materialField.test.ts`'s own assertion,
 * next to the Start-disabled reason it shares a fixture with.
 *
 * `frontend/tests/e2e/catalog-start.spec.ts` proves the same narrowing
 * through the real interface, but the harness seeds exactly one observed
 * item, so it cannot show a group dropping out. This file mounts the real
 * app against a `CockpitApi` double instead -- the economical way to reach
 * that edge (mehrere Quellen, Groß-/Kleinschreibung) without a live tracker
 * fixture.
 */

const revisionHash = "a".repeat(64);
const workflowName = "chooser-filter-focus";

const workItemOrder = {
  name: "work",
  schema: { ref: "work-item-schema", revision: WORK_ITEM_ORDER_SCHEMA_REVISION }
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

const groupedQueueItems: ObservedQueueItemPage = {
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
      item_id: "2".repeat(64),
      revision: 0,
      title: "Loopback refusal",
      title_observed_at: "2026-09-01T14:00:00Z",
      retired_at: null
    },
    {
      project_id: "infra",
      tracker_item_reference: "gl:12",
      item_id: "3".repeat(64),
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

function detail(): WorkflowRevisionDetail {
  return {
    workflow_revision_hash: revisionHash,
    document_base64: "YQ==",
    provenance: null,
    graph: {
      workflow_format_version: 3,
      executable: true,
      not_executable_reason: null,
      node_count: 1,
      agent_roles: [],
      orders: [workItemOrder],
      wait_answer_schemas: [],
      node_previews: [],
      loops: [],
      name: workflowName,
      description: null
    }
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
    getSchemaRevision: vi.fn(async () => workItemSchema),
    ...overrides
  });
}

async function openStart(cockpitApi: CockpitApi): Promise<void> {
  window.history.replaceState(null, "", `/atelier/catalog/${encodeURIComponent(workflowName)}`);
  render(App, {
    props: { cockpitApi, mutationJournal: new MutationJournal(sessionStorage) }
  });
  await fireEvent.click(await screen.findByRole("button", { name: "Start" }));
  await screen.findByRole("dialog", { name: workflowStartCopy.startTitle(workflowName) });
  await waitFor(() => expect(screen.queryByText("Preparing…")).toBeNull());
}

async function openPicker(): Promise<HTMLElement> {
  const picker = screen.getByRole("combobox", { name: workItemFor("work") });
  await fireEvent.click(picker);
  return picker;
}

function filterField(): HTMLInputElement {
  return screen.getByLabelText(workflowStartCopy.filterWorkItemsLabel) as HTMLInputElement;
}

afterEach(() => {
  cleanup();
});

describe("work-item picker filter", () => {
  it("proves(the-work-item-picker-filters-by-number-and-title-and-says-when-nothing-matches): narrows the grouped list by number, dropping an emptied group's heading", async () => {
    const cockpitApi = api({ listObservedQueueItems: vi.fn(async () => groupedQueueItems) });
    await openStart(cockpitApi);
    await openPicker();

    await fireEvent.input(filterField(), { target: { value: "#45" } });

    expect(screen.getByRole("option", { name: "#450 Preview door" })).toBeTruthy();
    expect(screen.queryByRole("option", { name: "#446 Loopback refusal" })).toBeNull();
    expect(screen.queryByRole("option", { name: "!13 Deploy runner" })).toBeNull();
    expect(screen.getByText(observedSourceHeading("atelier", workflowStartCopy.github))).toBeTruthy();
    expect(screen.queryByText(observedSourceHeading("infra", workflowStartCopy.gitlab))).toBeNull();
  });

  it("matches the raw reference without the adapter's # grammar", async () => {
    const cockpitApi = api({ listObservedQueueItems: vi.fn(async () => groupedQueueItems) });
    await openStart(cockpitApi);
    await openPicker();

    await fireEvent.input(filterField(), { target: { value: "450" } });

    expect(screen.getByRole("option", { name: "#450 Preview door" })).toBeTruthy();
    expect(screen.queryByRole("option", { name: "#446 Loopback refusal" })).toBeNull();
  });

  it("proves(the-work-item-picker-filters-by-number-and-title-and-says-when-nothing-matches): narrows by a title word, case-insensitively", async () => {
    const cockpitApi = api({ listObservedQueueItems: vi.fn(async () => groupedQueueItems) });
    await openStart(cockpitApi);
    await openPicker();

    await fireEvent.input(filterField(), { target: { value: "DEPLOY" } });

    expect(screen.getByRole("option", { name: "!13 Deploy runner" })).toBeTruthy();
    expect(screen.queryByRole("option", { name: "#450 Preview door" })).toBeNull();
  });

  it("proves(the-work-item-picker-filters-by-number-and-title-and-says-when-nothing-matches): says when nothing matches instead of an empty or silently-closed list", async () => {
    const cockpitApi = api({ listObservedQueueItems: vi.fn(async () => groupedQueueItems) });
    await openStart(cockpitApi);
    await openPicker();

    await fireEvent.input(filterField(), { target: { value: "#999" } });

    expect(screen.getByText(workflowStartCopy.noWorkItemMatch("#999"))).toBeTruthy();
    expect(screen.queryByRole("option")).toBeNull();
    expect(screen.getByRole("listbox", { name: workItemFor("work") })).toBeTruthy();
  });

  it("never offers a retired item, filtered or not", async () => {
    const cockpitApi = api({ listObservedQueueItems: vi.fn(async () => groupedQueueItems) });
    await openStart(cockpitApi);
    await openPicker();

    expect(screen.queryByRole("option", { name: "!12 Rotate keys" })).toBeNull();
    await fireEvent.input(filterField(), { target: { value: "rotate" } });
    expect(screen.getByText(workflowStartCopy.noWorkItemMatch("rotate"))).toBeTruthy();
  });

  it("keeps ArrowDown within the filtered set, not the full one", async () => {
    const cockpitApi = api({ listObservedQueueItems: vi.fn(async () => groupedQueueItems) });
    await openStart(cockpitApi);
    await openPicker();

    // A keyboard user's ArrowDown reaches the filter field, not the
    // combobox button underneath it (REQ-UIQ-05) -- the state a real user
    // produces once the picker is open. Real focus and the combobox's
    // `aria-activedescendant` both live on that field while it is open, so
    // this is where a screen reader announces the active option too.
    const filter = filterField();
    await fireEvent.input(filter, { target: { value: "#45" } });
    const only = screen.getByRole("option", { name: "#450 Preview door" });
    expect(filter.getAttribute("aria-activedescendant")).toBe(only.id);

    await fireEvent.keyDown(filter, { key: "ArrowDown" });
    expect(filter.getAttribute("aria-activedescendant")).toBe(only.id);
  });

  it("focuses the filter field when the picker opens, and lets Tab from the combobox reach it (REQ-UIQ-05)", async () => {
    const cockpitApi = api({ listObservedQueueItems: vi.fn(async () => groupedQueueItems) });
    await openStart(cockpitApi);
    const picker = await openPicker();

    await waitFor(() => expect(document.activeElement).toBe(filterField()));

    // Tab must not be intercepted to close the picker first (that was the
    // defect): the field has to still be there, immediately after the
    // combobox in tab order, for the browser's own Tab action to reach.
    picker.focus();
    fireEvent.keyDown(picker, { key: "Tab" });
    expect(screen.getByRole("listbox", { name: workItemFor("work") })).toBeTruthy();
    const dialog = screen.getByRole("dialog", { name: workflowStartCopy.startTitle(workflowName) });
    const focusable = Array.from(
      dialog.querySelectorAll<HTMLElement>(
        'button:not([disabled]):not([role="option"]), input:not([disabled]), select:not([disabled]), a[href]'
      )
    );
    expect(focusable[focusable.indexOf(picker) + 1]).toBe(filterField());
  });

  it("resets the filter when the picker is closed and reopened", async () => {
    const cockpitApi = api({ listObservedQueueItems: vi.fn(async () => groupedQueueItems) });
    await openStart(cockpitApi);
    const picker = await openPicker();

    await fireEvent.input(filterField(), { target: { value: "#450" } });
    expect(screen.queryByRole("option", { name: "!13 Deploy runner" })).toBeNull();

    await fireEvent.click(picker);
    await fireEvent.click(picker);

    expect(filterField().value).toBe("");
    expect(screen.getByRole("option", { name: "!13 Deploy runner" })).toBeTruthy();
  });

  it("the open, filtered picker has no WCAG 2.1/2.2 AA violation axe can catch in jsdom", async () => {
    const cockpitApi = api({ listObservedQueueItems: vi.fn(async () => groupedQueueItems) });
    await openStart(cockpitApi);
    await openPicker();
    await fireEvent.input(filterField(), { target: { value: "#45" } });

    const results = await axe.run(screen.getByRole("dialog"), {
      runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag22aa"] }
    });
    // jsdom cannot compute color-contrast; that lands in `incomplete`, not
    // `violations`, and this scan does not need it either way.
    const violations = results.violations.map((violation) => `${violation.id}: ${violation.help}`);
    expect(violations, violations.join("\n")).toEqual([]);
  });
});
