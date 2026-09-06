import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Locator, type Page } from "@playwright/test";

import type { RunListRow } from "../../src/api/client";
import { nodeDetailSchema } from "../../src/api/client";
import { backLinkCopy } from "../../src/lib/backLinkCopy";
import {
  catalogPageCopy,
  observedSourceHeading,
  startConfigurationLabel,
  workItemFor,
  workflowStartCopy
} from "../../src/lib/catalogPageCopy";
import { decodeUtf8Base64 } from "../../src/lib/exactBytes";
import { shortFingerprint } from "../../src/lib/fingerprint";
import { SCALAR_OR_ARRAY_ORDER_UNSUPPORTED_REASON } from "../../src/lib/orderSchema";
import { PRODUCT_NAME } from "../../src/lib/productName";
import { THE_ONE_PROJECT } from "../../src/lib/project";
import { retryLabel } from "../../src/lib/readStateCopy";
import { settingsPageCopy } from "../../src/lib/settingsPageCopy";
import { runPageCopy } from "../../src/lib/runPageCopy";
import { standingWords } from "../../src/lib/runState";
import { workbenchPageCopy } from "../../src/lib/workbenchPageCopy";
import { nodeAriaName, stateLabels } from "../../src/lib/stateMarkCopy";
import { workflowGraphCopy } from "../../src/lib/workflowGraphCopy";
import { healthyRunListItems } from "../support/runListRows";
import { WORK_ITEM_SCHEMA_DOCUMENT } from "./workItemSchema";

const foundReference = "run1.Zm91bmQtcnVu";
const absentReference = "run1.YWJzZW50LXJ1bg";
const startSheetProjectReference = "project1.YXRlbGllci0y";

interface StartSheetConfiguration {
  readonly hash: string;
  readonly authProfileHash: string;
  readonly providerId: string;
  readonly modelId: string;
  readonly startable: boolean;
}

async function routeStartSheetModelContract(
  page: Page,
  configurations: readonly StartSheetConfiguration[],
  roles: readonly string[] = ["builder"]
): Promise<void> {
  await page.route("**/atelier/api/v1/auth-profile-revisions?*", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        items: configurations.map((configuration, index) => ({
          profile_id: `operator-${index + 1}`,
          revision_number: 1,
          provider_id: configuration.providerId,
          auth_mode: "subscription",
          auth_profile_revision_hash: configuration.authProfileHash
        })),
        next_after_revision_hash: null
      })
    });
  });
  await page.route("**/atelier/api/v1/model-registries/*", async (route) => {
    const providerId = decodeURIComponent(new URL(route.request().url()).pathname.split("/").at(-1) ?? "");
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        provider_id: providerId,
        revision_number: 1,
        model_registry_revision_hash: "9".repeat(64),
        entries: configurations
          .filter((configuration) => configuration.providerId === providerId)
          .map((configuration) => ({
            model_id: configuration.modelId,
            agent_configuration_revision_hash: configuration.hash,
            source: "operator",
            provider_check: "checked"
          }))
      })
    });
  });
  await page.route("**/atelier/api/v1/projects", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ items: [{ public_project_reference: startSheetProjectReference }] })
    });
  });
  await page.route("**/atelier/api/v1/projects/*/model-resolution", async (route) => {
    const body = route.request().postDataJSON() as {
      workflow_revision_hash: string;
      overrides: Array<{ role: string; agent_configuration_revision_hash: string }>;
    };
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        project_id: "atelier-2",
        public_project_reference: startSheetProjectReference,
        workflow_revision_hash: body.workflow_revision_hash,
        resolutions: roles.map((role) => {
          const chosen = body.overrides.find((override) => override.role === role)
            ?.agent_configuration_revision_hash ?? null;
          const configuration = configurations.find((candidate) => candidate.hash === chosen);
          return {
            role,
            agent_configuration_revision_hash: chosen,
            source: chosen === null ? "uncast" : "chosen-now",
            model_id: configuration?.modelId ?? null,
            declared_difficulty: 2,
            default_difficulty: null,
            uncast_reason: chosen === null ? "no-project-default" : null,
            family_differs_from: null
          };
        })
      })
    });
  });
}

async function publishCheckedRegistryEntry(
  page: Page,
  providerId: string,
  modelId: string,
  configurationHash: string
): Promise<void> {
  const current = await page.request.get(`/atelier/api/v1/model-registries/${providerId}`);
  const currentRegistry = current.status() === 200
    ? await current.json() as {
        revision_number: number;
        entries: Array<{ model_id: string; agent_configuration_revision_hash: string }>;
      }
    : null;
  if (currentRegistry === null) expect(current.status()).toBe(404);
  const existingEntries = (currentRegistry?.entries ?? [])
    .filter((entry) =>
      entry.agent_configuration_revision_hash !== configurationHash && entry.model_id !== modelId
    )
    .map((entry) => ({
      model_id: entry.model_id,
      agent_configuration_revision_hash: entry.agent_configuration_revision_hash
    }));
  const registry = await page.request.put(`/atelier/api/v1/model-registries/${providerId}`, {
    data: {
      revision_number: (currentRegistry?.revision_number ?? 0) + 1,
      entries: [...existingEntries, {
        model_id: modelId,
        agent_configuration_revision_hash: configurationHash
      }]
    }
  });
  expect([200, 201]).toContain(registry.status());
  const checked = await page.request.post(`/atelier/api/v1/model-registries/${providerId}/validations`, {
    data: { agent_configuration_revision_hash: configurationHash }
  });
  expect([200, 201]).toContain(checked.status());
}

/**
 * A real HTTP answer the server gave, not a round trip that never happened
 * -- every read-recovery test below wants a page-local "unavailable", never
 * #700's own central, cross-page reachability signal (which `route.abort`
 * would trip, since that models an outage, not one read's own failure).
 */
const TEMPORARILY_UNAVAILABLE_PROBLEM = {
  type: "urn:atelier2:problem:v1:temporarily-unavailable",
  title: "Temporarily unavailable",
  status: 503,
  detail: "the durable store is unreachable"
} as const;

/**
 * This suite shares one server across every spec file (#742): a run another
 * file already started answers a real, unmocked read this test counts, so an
 * exact request-count proof needs the durable store back at its cold-boot
 * baseline first, instead of depending on running before every other spec in
 * the file listing.
 */
async function resetToKnownStore(page: Page): Promise<void> {
  const reset = await page.request.post("/__e2e/recompose?reset=true");
  expect(reset.status()).toBe(202);
  const expectedGeneration = await reset.text();
  await expect(async () => {
    expect(await (await page.request.get("/__e2e/generation")).text()).toBe(expectedGeneration);
  }).toPass({ timeout: 20_000 });
}

// Every executable V3 agent node declares exactly one output and the schema it
// must satisfy: that is `single-json-output/v1`, the one output shape a run
// enforces. Where a test is about something else, it pins the schema that admits
// any JSON value, so the node's contract says no more than the shape requires.
async function publishSchema(page: Page, document: string): Promise<string> {
  const published = await page.request.post("/atelier/api/v1/schema-revisions", {
    headers: { "content-type": "application/json" },
    data: document
  });
  expect([200, 201]).toContain(published.status());
  return (await published.json()).schema_revision_hash as string;
}

const anyJsonSchema = (page: Page): Promise<string> => publishSchema(page, "true");

function declaredOutput(schemaHash: string, name = "result"): string[] {
  return [
    "    outputs:",
    `      - name: ${name}`,
    "        schema:",
    `          ref: ${name}-schema`,
    `          revision: ${schemaHash}`
  ];
}

test("the target-UI shell names today's doors and does not fake the rest", async ({ page }) => {
  await page.goto("/atelier");
  await expect(page.getByRole("heading", { name: "Workbench" })).toBeVisible();

  const rail = page.getByRole("navigation", { name: "Workshop" });
  // The brand wordmark and the project name under Settings share one source
  // (#654), so the one product name stands in the rail exactly twice.
  await expect(rail.getByText(PRODUCT_NAME, { exact: true })).toHaveCount(2);
  await expect(page).toHaveTitle(PRODUCT_NAME);
  await expect(rail.getByRole("link", { name: "Workbench" })).toBeVisible();
  await expect(rail.getByRole("link", { name: "Catalog" })).toBeVisible();
  await expect(rail.getByRole("link", { name: "History" })).toBeVisible();
  await expect(rail.getByRole("link", { name: /Settings/ })).toBeVisible();
  // The rooms the picture retired leave no entry behind, and the rail holds no
  // slot that cannot be clicked (ADR 0019 §1 and §4).
  await expect(rail.getByRole("link", { name: "Board" })).toHaveCount(0);
  await expect(rail.getByRole("link", { name: "Workflows" })).toHaveCount(0);
  await expect(rail.getByText("Not built yet", { exact: true })).toHaveCount(0);
  await expect(rail.getByText("Profile", { exact: true })).toHaveCount(0);

  await rail.getByRole("link", { name: "History" }).click();
  await expect(page.getByRole("heading", { name: "History" })).toBeVisible();
  await expect(page).toHaveURL(/\/atelier\/history$/);

  await rail.getByRole("link", { name: "Catalog" }).click();
  await expect(page.getByRole("heading", { name: "Catalog" })).toBeVisible();
  await expect(page).toHaveURL(/\/atelier\/catalog$/);

  await rail.getByRole("link", { name: "Workbench" }).click();
  await expect(page.getByRole("heading", { name: "Workbench" })).toBeVisible();
  await expect(page).toHaveURL(/\/atelier\/chat$/);

  await page.screenshot({ path: "test-results/shell-desktop.png", fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByRole("navigation", { name: "Workshop" })).toBeVisible();
  await assertMobileSurface(page);
  await page.screenshot({ path: "test-results/shell-390x844.png", fullPage: true });
});

test("proves(core-surfaces-support-one-complete-keyboard-journey): chooses a checked role configuration from Catalog", async ({ page }) => {
  const api = "/atelier/api/v1";
  const workflowName = "keyboard-journey-catalog";
  const schemaHash = await anyJsonSchema(page);
  const workflow = await page.request.post(`${api}/workflow-revisions`, { headers: { "content-type": "application/yaml" }, data: ["format_version: 3", `name: ${workflowName}`, "nodes:", "  - id: build", "    type: agent", "    role: builder", "    mode: headless", "    instruction: Prove the Catalog start door.", ...declaredOutput(schemaHash), ""].join("\n") });
  expect(workflow.status()).toBe(201);
  const workflowRevisionHash = (await workflow.json()).workflow_revision_hash as string;
  const lineage = await page.request.post(`${api}/catalog-lineages`, { data: { kind: "workflow", catalog_revision_hash: workflowRevisionHash, actor: "e2e", activated_at: "2026-08-24T00:00:00Z" } });
  expect(lineage.status()).toBe(201);

  const configurationHash = "b".repeat(64);
  const authProfileRevisionHash = "a".repeat(64);
  await page.route("**/atelier/api/v1/agent-configuration-revisions?*", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        items: [{
          agent_configuration_revision_hash: configurationHash,
          auth_profile_revision_hash: authProfileRevisionHash,
          provider_id: "e2e",
          auth_mode: "subscription",
          model: "interim-model",
          executor_revision: "immediate/v1",
          requested_capability: "headless",
          startable: true,
          structurally_startable: true,
          not_startable_reason: null,
          provider_probe_problem_code: null,
          provider_probe_observed_at: null
        }],
        next_after_revision_hash: null
      })
    });
  });
  await routeStartSheetModelContract(page, [{
    hash: configurationHash,
    authProfileHash: authProfileRevisionHash,
    providerId: "e2e",
    modelId: "interim-model",
    startable: true
  }]);

  let receivedStart: Record<string, unknown> | null = null;
  let startedRun: Record<string, unknown> | null = null;
  await page.route("**/atelier/api/v1/runs", async (route) => {
    if (route.request().method() !== "POST") {
      await route.continue();
      return;
    }
    receivedStart = route.request().postDataJSON() as Record<string, unknown>;
    const runId = receivedStart.run_id as string;
    const publicRunReference = `run1.${Buffer.from(runId).toString("base64url")}`;
    const responseHash = "c".repeat(64);
    startedRun = {
      workflow_format_version: 3,
      run_id: runId,
      public_run_reference: publicRunReference,
      workflow_revision_hash: workflowRevisionHash,
      workflow_name: workflowName,
      agent_binding_set_hash: responseHash,
      run_configuration_revision_hash: responseHash,
      agent_bindings: [{
        role: "builder",
        agent_configuration_revision_hash: configurationHash,
        auth_profile_revision_hash: authProfileRevisionHash,
        profile_id: "interim-e2e",
        revision_number: 1,
        provider_id: "e2e",
        auth_mode: "subscription",
        model: "interim-model",
        executor_revision: "immediate/v1"
      }],
      orders: [],
      state_version: 0,
      state: "STARTED",
      current_node_id: "build",
      current_node_execution_id: responseHash,
      node_rail: [{ node_id: "build", state: "queued", attempt: null }],
      cancellation: { cancellable: false, reason: "between-nodes", target_node_execution_id: null },
      terminal_hash: null,
      latest_event_cursor: null
    };
    await route.fulfill({ status: 201, contentType: "application/json", body: JSON.stringify(startedRun) });
  });
  await page.route("**/atelier/api/v1/runs/*", async (route) => {
    if (route.request().method() === "GET" && startedRun !== null) {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(startedRun) });
      return;
    }
    await route.continue();
  });

  await page.goto(`/atelier/catalog/${workflowName}`);
  const opener = page.getByRole("button", { name: "Start" });
  await opener.focus();
  await page.keyboard.press("Enter");
  const sheet = page.getByRole("dialog", { name: workflowStartCopy.startTitle(workflowName) });
  await expect(sheet).toBeVisible();
  const picker = sheet.getByLabel(workflowStartCopy.configurationFor("builder"));
  await expect(picker).toHaveValue("");
  await picker.selectOption(configurationHash);
  await expect(sheet.getByText("Chosen now", { exact: true })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(sheet).toHaveCount(0);
  await expect(opener).toBeFocused();
  await opener.click();
  await expect(sheet).toBeVisible();
  await expect(picker).toHaveValue("");
  await picker.selectOption(configurationHash);
  const startRun = sheet.getByRole("button", { name: "Start run" });
  await expect(startRun).toBeEnabled();
  await startRun.focus();
  await page.keyboard.press("Enter");
  await expect.poll(() => receivedStart).not.toBeNull();
  expect(receivedStart).toEqual(expect.objectContaining({
    workflow_format_version: 3,
    workflow_revision_hash: workflowRevisionHash,
    agent_bindings: [{ role: "builder", agent_configuration_revision_hash: configurationHash }],
    orders: []
  }));
  await expect(page).toHaveURL(/\/atelier\/runs\/run1\./);
  await expect(page.getByRole("heading", { level: 1, name: workflowName })).toBeVisible();
});


/**
 * A click never asks the server for a page, so the project level looked right
 * while a reload of it answered 404. This walks the way an operator arrives from
 * outside — the pasted link — and then reloads the level he is standing on.
 */
test("opens Settings from a cold link and survives a reload", async ({ page }) => {
  await page.goto("/atelier/settings");
  await expect(page.getByRole("heading", { name: THE_ONE_PROJECT })).toBeVisible();

  await page.reload();
  await expect(page.getByRole("heading", { name: THE_ONE_PROJECT })).toBeVisible();
  await expect(page).toHaveURL(/\/atelier\/settings$/);
});

// The identifier stays "the-studio-…" (acceptance/440): the room whose read it
// measures is the Workbench since ADR 0019 retired the Board.
test("proves(the-studio-preserves-confirmed-truth-and-retries-only-its-failed-read): the Workbench recovers one retained three-list-plus-catalog read", async ({ page }) => {
  await resetToKnownStore(page);
  const runListPath = "/atelier/api/v1/runs";
  const catalogPath = "/atelier/api/v1/workflow-revisions";
  // The Workbench reads only the non-terminal run states -- what still moves
  // or wants a human now (operator ruling #667). A terminal run belongs to
  // History instead, so it is never asked for here.
  const expectedStates = ["STARTED", "WAITING_INPUT", "WAITING_RECONCILIATION"];
  let readsFail = true;
  const observed: Array<{ method: string; path: string; state: string | null }> = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname.startsWith("/atelier/api/v1")) {
      observed.push({
        method: request.method(),
        path: url.pathname,
        state: url.searchParams.get("state")
      });
    }
  });
  await page.route("**/atelier/api/v1/runs?*", async (route) => {
    if (readsFail) {
      await route.fulfill({ status: 503, json: TEMPORARILY_UNAVAILABLE_PROBLEM });
    } else {
      await route.continue();
    }
  });

  // The two boot-baseline fixture runs (#742) sit in WAITING_RECONCILIATION,
  // so once a real (unmocked) run-list read succeeds, the room's own pinned-
  // decision hold may react to them with its own detail and agent-
  // configuration reads -- legitimate and independent of the list-read retry
  // this test proves, but arriving at a moment this test cannot observe or
  // force, since it is the live attention hold's own async timing, not a
  // step this test drives. Named and excluded here by an allow-list of exact
  // paths, not by count, so the proof stays about what this test itself
  // drives regardless of whether the hold has reacted yet.
  const pinnedDecisionPaths = [
    `${runListPath}/${foundReference}`,
    `${runListPath}/${absentReference}`,
    "/atelier/api/v1/agent-configuration-revisions"
  ];

  const expectOnlyRoomRead = (): void => {
    const unrecognized = observed.filter(
      ({ path }) => !pinnedDecisionPaths.includes(path)
    );
    const runRequests = unrecognized.filter(({ path }) => path === runListPath);
    const catalogRequests = unrecognized.filter(({ path }) => path === catalogPath);
    expect(unrecognized).toHaveLength(runRequests.length + catalogRequests.length);
    expect(runRequests.every(({ method }) => method === "GET")).toBe(true);
    expect(runRequests.map(({ state }) => state).sort()).toEqual(expectedStates);
    expect(catalogRequests).toHaveLength(1);
    expect(catalogRequests[0]?.method).toBe("GET");
  };

  await page.goto("/atelier");
  await expect(page.getByText("Workbench runs unavailable")).toBeVisible();
  await expect(page.getByText(/Failed to fetch/)).toHaveCount(0);
  const retry = page.getByRole("button", { name: retryLabel(workbenchPageCopy.runsLabel) });
  await expect(retry).toHaveCount(1);
  const roomUrl = page.url();

  observed.length = 0;
  await retry.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByText("Workbench runs unavailable")).toBeVisible();
  await expect(retry).toBeFocused();
  expectOnlyRoomRead();
  expect(page.url()).toBe(roomUrl);

  await page.keyboard.press("Shift+Tab");
  await page.keyboard.press("Tab");
  await expect(retry).toBeFocused();
  await expectVisibleFocus(retry);
  await assertNoSeriousAccessibilityFindings(page);
  await page.addStyleTag({ content: "html { filter: grayscale(1); }" });
  await page.screenshot({
    path: "test-results/read-recovery-workbench-grayscale-desktop.png",
    fullPage: true
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await assertMobileSurface(page);
  await page.screenshot({
    path: "test-results/read-recovery-workbench-grayscale-390x844.png",
    fullPage: true
  });
  await page.locator("style").last().evaluate((element) => element.remove());

  readsFail = false;
  observed.length = 0;
  await page.getByRole("button", { name: retryLabel(workbenchPageCopy.runsLabel) }).click();
  const room = page.locator(".workbench");
  await expect(page.getByText("Workbench runs unavailable")).toHaveCount(0);
  await expect(room).toBeVisible();
  // One freshness model, once confirmed: no Refresh or Retry control remains
  // (#532) -- the redundant permanent control this lane removes.
  await expect(page.getByRole("button", { name: /workbench runs/ })).toHaveCount(0);
  expectOnlyRoomRead();
  expect(page.url()).toBe(roomUrl);
});

test("proves(Settings retains its current project surface on reload", async ({ page }) => {
  await page.goto("/atelier/settings");
  await expect(page.getByRole("heading", { name: THE_ONE_PROJECT })).toBeVisible();
  await page.reload();
  await expect(page.getByRole("heading", { name: THE_ONE_PROJECT })).toBeVisible();
  await expect(page.getByRole("region", { name: settingsPageCopy.sourcesTitle })).toBeVisible();
});

test("Catalog cold-loads and retains a slash-named workflow detail route across reload", async ({ page }) => {
  await page.goto("/atelier/catalog/catalog/detail");
  await expect(page.getByText("Workflow not found")).toBeVisible();
  await page.reload();
  await expect(page.getByText("Workflow not found")).toBeVisible();
});

test("Start sheet presents a current role configuration without retaining a draft", async ({ page }) => {
  const name = "current-sheet-configuration";
  const schema = await anyJsonSchema(page);
  const workflow = await page.request.post("/atelier/api/v1/workflow-revisions", {
    headers: { "content-type": "application/yaml" },
    data: [
      "format_version: 3",
      `name: ${name}`,
      "nodes:",
      "  - id: build",
      "    type: agent",
      "    role: builder",
      "    mode: headless",
      "    instruction: Choose now.",
      ...declaredOutput(schema),
      ""
    ].join("\n")
  });
  expect(workflow.status()).toBe(201);
  const admitted = await page.request.post("/atelier/api/v1/catalog-lineages", {
    data: { kind: "workflow", catalog_revision_hash: (await workflow.json()).workflow_revision_hash, actor: "e2e", activated_at: "2026-08-26T00:00:00Z" }
  });
  expect(admitted.status()).toBe(201);
  const configurationHash = "c".repeat(64);
  await page.route("**/atelier/api/v1/agent-configuration-revisions?*", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        items: [{
          agent_configuration_revision_hash: configurationHash,
          auth_profile_revision_hash: "d".repeat(64),
          provider_id: "e2e",
          auth_mode: "subscription",
          model: "sheet-model",
          executor_revision: "immediate/v1",
          requested_capability: "headless",
          startable: true,
          structurally_startable: true,
          not_startable_reason: null,
          provider_probe_problem_code: null,
          provider_probe_observed_at: null
        }],
        next_after_revision_hash: null
      })
    });
  });
  await routeStartSheetModelContract(page, [{
    hash: configurationHash,
    authProfileHash: "d".repeat(64),
    providerId: "e2e",
    modelId: "sheet-model",
    startable: true
  }]);

  await page.goto(`/atelier/catalog/${name}`);
  const opener = page.getByRole("button", { name: "Start" });
  await opener.click();
  const sheet = page.getByRole("dialog", { name: workflowStartCopy.startTitle(name) });
  const picker = sheet.getByLabel(workflowStartCopy.configurationFor("builder"));
  await picker.selectOption(configurationHash);
  await expect(sheet.locator(".role-source")).toHaveText("Chosen now");
  await page.keyboard.press("Escape");
  await opener.click();
  await expect(picker).toHaveValue("");
});

test("Catalog start sheet refuses an array order schema before starting", async ({ page }) => {
  const api = "/atelier/api/v1";
  const workflowName = "unsupported-order-shapes";
  const scalarSchema = await page.request.post(`${api}/schema-revisions`, {
    headers: { "content-type": "application/json" },
    data: '{"type":"string"}'
  });
  expect([200, 201]).toContain(scalarSchema.status());
  const scalarSchemaHash = (await scalarSchema.json()).schema_revision_hash as string;
  const arraySchema = await page.request.post(`${api}/schema-revisions`, {
    headers: { "content-type": "application/json" },
    data: '{"type":"array","items":{"type":"string"}}'
  });
  expect([200, 201]).toContain(arraySchema.status());
  const arraySchemaHash = (await arraySchema.json()).schema_revision_hash as string;
  const outputSchemaHash = await anyJsonSchema(page);
  const workflow = await page.request.post(`${api}/workflow-revisions`, {
    headers: { "content-type": "application/yaml" },
    data: [
      "format_version: 3",
      `name: ${workflowName}`,
      "graph_inputs:",
      "  - name: scalar_order",
      "    schema:",
      "      ref: scalar-order",
      `      revision: ${scalarSchemaHash}`,
      "  - name: array_order",
      "    schema:",
      "      ref: array-order",
      `      revision: ${arraySchemaHash}`,
      "nodes:",
      "  - id: build",
      "    type: agent",
      "    role: builder",
      "    mode: headless",
      "    instruction: Use the admitted orders.",
      "    inputs:",
      "      - name: scalar_order",
      "        from:",
      "          graph_input: scalar_order",
      "      - name: array_order",
      "        from:",
      "          graph_input: array_order",
      ...declaredOutput(outputSchemaHash),
      ""
    ].join("\n")
  });
  expect(workflow.status()).toBe(201);
  const admitted = await page.request.post(`${api}/catalog-lineages`, {
    data: {
      kind: "workflow",
      catalog_revision_hash: (await workflow.json()).workflow_revision_hash,
      actor: "e2e",
      activated_at: "2026-08-26T00:00:00Z"
    }
  });
  expect(admitted.status()).toBe(201);
  await page.route("**/atelier/api/v1/agent-configuration-revisions?*", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ items: [], next_after_revision_hash: null })
    });
  });
  await routeStartSheetModelContract(page, []);

  await page.goto(`/atelier/catalog/${workflowName}`);
  await page.getByRole("button", { name: "Start" }).click();
  const sheet = page.getByRole("dialog", { name: workflowStartCopy.startTitle(workflowName) });
  await expect(sheet).toBeVisible();
  const startRun = sheet.getByRole("button", { name: "Start run" });
  await expect(startRun).toBeDisabled();

  const scalarGroup = sheet.getByRole("group", { name: "Order scalar_order" });
  const scalarText = scalarGroup.getByLabel(workflowStartCopy.orderText);
  await expect(scalarText).toBeVisible();
  await scalarText.fill("a string order starts here");

  await expect(sheet.getByRole("group", { name: "Order array_order" }).getByRole("status")).toHaveText(
    SCALAR_OR_ARRAY_ORDER_UNSUPPORTED_REASON
  );
  await expect(startRun).toBeDisabled();
});

test("walks the whole workshop: the workbench into the run, and one named way back", async ({ page }) => {
  await page.goto("/atelier");
  await expect(page.getByRole("heading", { name: "Workbench" })).toBeVisible();

  // The living shelf beneath the pinned decisions: the run rows this room
  // holds, each one click from its graph.
  await page.locator("a.living-row").first().click();
  // A format-3 document always declares a name, and the header leads with it
  // rather than with the raw run id (#506).
  await expect(page.getByRole("heading", { name: "Prove one reconciliation" })).toBeVisible();
  // One way back, to the rail destination this page belongs to, and it never
  // repeats the page's own title beside it (operator ruling 23.08.).
  const trail = page.getByRole("navigation", { name: backLinkCopy.whereYouAre });
  await expect(trail.getByRole("link")).toHaveCount(1);
  await expect(trail.getByRole("link", { name: "Workbench" })).toBeVisible();
  await expect(trail).not.toContainText("Prove one reconciliation");
  await page.screenshot({ path: "test-results/run-trail-desktop.png", fullPage: true });
  await assertNoSeriousAccessibilityFindings(page);

  await trail.getByRole("link", { name: "Workbench" }).click();
  await expect(page.getByRole("heading", { name: "Workbench" })).toBeVisible();
  await expect(page).toHaveURL(/\/atelier\/chat$/);
});
async function assertMobileSurface(page: Page): Promise<void> {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth
  );
  expect(overflow).toBeLessThanOrEqual(0);
  const surfaces = page.locator("[role=alert], article");
  for (let index = 0; index < await surfaces.count(); index += 1) {
    const clipped = await surfaces.nth(index).evaluate(
      (element) => element.scrollWidth - element.clientWidth
    );
    expect(clipped, `surface ${index} must not clip content`).toBeLessThanOrEqual(0);
  }
  const controls = page.locator(
    "button, input[type=text], textarea, select, .determination-picker label, summary"
  );
  for (let index = 0; index < await controls.count(); index += 1) {
    const box = await controls.nth(index).boundingBox();
    expect(box, `control ${index} must be rendered`).not.toBeNull();
    expect(box?.height, `control ${index} must have a 44px touch target`).toBeGreaterThanOrEqual(44);
  }
  await assertNoSeriousAccessibilityFindings(page);
}

async function assertNoSeriousAccessibilityFindings(page: Page): Promise<void> {
  const scan = await new AxeBuilder({ page }).analyze();
  expect(
    scan.violations.filter((violation) =>
      violation.impact === "serious" || violation.impact === "critical"
    )
  ).toEqual([]);
}

async function expectVisibleFocus(control: Locator): Promise<void> {
  const outline = await control.evaluate((element) => {
    const style = getComputedStyle(element);
    return { style: style.outlineStyle, width: Number.parseFloat(style.outlineWidth) };
  });
  expect(outline.style).not.toBe("none");
  expect(outline.width).toBeGreaterThanOrEqual(3);
}

test("opens a V3 run at its own address and shows the line it drove", async ({ page }) => {
  const api = "/atelier/api/v1";
  const schemaHash = await anyJsonSchema(page);
  const workflowYaml = [
    "format_version: 3",
    "name: Two agents in a line",
    "nodes:",
    "  - id: implement",
    "    type: agent",
    "    role: builder",
    "    mode: headless",
    "    instruction: Do the one thing this chain is for.",
    ...declaredOutput(schemaHash),
    "  - id: review",
    "    type: agent",
    "    role: builder",
    "    mode: headless",
    "    instruction: Check what the node before you did.",
    "    depends_on: [implement]",
    ...declaredOutput(schemaHash),
    ""
  ].join("\n");

  const published = await page.request.post(`${api}/workflow-revisions`, {
    headers: { "content-type": "application/yaml" },
    data: workflowYaml
  });
  expect(published.status()).toBe(201);
  const revisionHash = (await published.json()).workflow_revision_hash as string;

  const auth = await page.request.post(`${api}/auth-profile-revisions`, {
    data: { profile_id: "v3-local", revision_number: 1, provider_id: "e2e-v3", auth_mode: "subscription" }
  });
  expect(auth.status()).toBe(201);
  const configuration = await page.request.post(`${api}/agent-configuration-revisions`, {
    data: {
      model: "v3-model",
      auth_profile_revision_hash: (await auth.json()).auth_profile_revision_hash,
      executor_revision: "immediate/v1",
      requested_capability: "headless"
    }
  });
  expect(configuration.status()).toBe(201);
  await publishCheckedRegistryEntry(
    page,
    "e2e-v3",
    "v3-model",
    (await configuration.json()).agent_configuration_revision_hash as string
  );

  const started = await page.request.post(`${api}/runs`, {
    data: {
      workflow_format_version: 2,
      run_id: "v3/seen-in-the-browser",
      workflow_revision_hash: revisionHash,
      agent_bindings: [
        {
          role: "builder",
          agent_configuration_revision_hash: (await configuration.json())
            .agent_configuration_revision_hash
        }
      ]
    }
  });
  expect(started.status(), await started.text()).toBe(201);
  const createdRun = await started.json();
  expect(createdRun.workflow_format_version).toBe(3);
  const reference = createdRun.public_run_reference as string;

  // The runtime drives the line without any further request; the read route is
  // what says it has, which is the vertical this page then renders.
  let terminal: string | null = null;
  await expect(async () => {
    const read = await page.request.get(`${api}/runs/${reference}`);
    expect(read.status()).toBe(200);
    const body = await read.json();
    expect(body.state).toBe("COMPLETED");
    expect(body.node_rail.map((entry: { node_id: string }) => entry.node_id)).toEqual([
      "implement",
      "review"
    ]);
    terminal = body.terminal_hash as string;
  }).toPass({ timeout: 15_000 });
  expect(terminal).not.toBeNull();
  if (terminal === null) {
    throw new Error("expected the completed run to name a terminal hash");
  }

  await page.goto(`/atelier/runs/${reference}`);

  await expect(page.getByRole("heading", { level: 1, name: "Two agents in a line" })).toBeVisible();
  const graph = page.getByRole("region", { name: workflowGraphCopy.label });
  await expect(graph.getByRole("button", { name: nodeAriaName("implement", "succeeded") })).toBeVisible();
  await expect(graph.getByRole("button", { name: nodeAriaName("review", "succeeded") })).toBeVisible();
  await expect(page.getByLabel(runPageCopy.whereThisRunStands)).toContainText("Done");
  await expect(page.getByLabel(runPageCopy.whereThisRunStands)).not.toContainText("Snapshot");
  // Not one fingerprint and not the run id stands on the main surface; they
  // live in the node's Evidence tab (operator ruling 23.08.).
  await expect(page.getByText("v3/seen-in-the-browser")).toHaveCount(0);
  await expect(page.getByRole("group", { name: "Terminal hash" })).toHaveCount(0);
  await expect(page.getByText(terminal)).toHaveCount(0);
  await expect(page.getByRole("button", { name: runPageCopy.readAgain })).toHaveCount(0);

  await graph.getByRole("button", { name: nodeAriaName("implement", "succeeded") }).click();
  await page.getByRole("tab", { name: runPageCopy.tabEvidence }).click();
  await expect(page.getByRole("group", { name: "Run id" })).toContainText(
    "v3/seen-in-the-browser"
  );
  await expect(page.getByRole("group", { name: "Terminal hash" })).toContainText(
    shortFingerprint(terminal)
  );

  await page.screenshot({ path: "test-results/v3-run-desktop.png", fullPage: true });
  await page.screenshot({ path: "test-results/v3-graph-desktop.png", fullPage: true });
  await assertNoSeriousAccessibilityFindings(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByRole("heading", { level: 1, name: "Two agents in a line" })).toBeVisible();
  expect(await page.evaluate(() => globalThis.document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: "test-results/v3-run-mobile.png", fullPage: true });
  await page.screenshot({ path: "test-results/v3-graph-390x844.png", fullPage: true });
});

test("starts an admitted V3 workflow from its Catalog detail sheet", async ({ page }) => {
  const api = "/atelier/api/v1";
  const workflowName = "started-from-catalog-detail";
  const outputSchemaHash = await anyJsonSchema(page);
  const workflow = await page.request.post(`${api}/workflow-revisions`, {
    headers: { "content-type": "application/yaml" },
    data: [
      "format_version: 3",
      `name: ${workflowName}`,
      "nodes:",
      "  - id: build",
      "    type: agent",
      "    role: builder",
      "    mode: headless",
      "    instruction: Start this workflow from its Catalog detail.",
      ...declaredOutput(outputSchemaHash),
      ""
    ].join("\n")
  });
  expect(workflow.status()).toBe(201);
  const workflowRevisionHash = (await workflow.json()).workflow_revision_hash as string;
  const admitted = await page.request.post(`${api}/catalog-lineages`, {
    data: { kind: "workflow", catalog_revision_hash: workflowRevisionHash, actor: "e2e", activated_at: "2026-08-26T00:00:00Z" }
  });
  expect(admitted.status()).toBe(201);

  const configurationHash = "d".repeat(64);
  const authProfileRevisionHash = "e".repeat(64);
  await page.route("**/atelier/api/v1/agent-configuration-revisions?*", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        items: [{
          agent_configuration_revision_hash: configurationHash,
          auth_profile_revision_hash: authProfileRevisionHash,
          provider_id: "e2e-v3",
          auth_mode: "subscription",
          model: "named-model",
          executor_revision: "immediate/v1",
          requested_capability: "headless",
          startable: true,
          structurally_startable: true,
          not_startable_reason: null,
          provider_probe_problem_code: null,
          provider_probe_observed_at: null
        }],
        next_after_revision_hash: null
      })
    });
  });
  await routeStartSheetModelContract(page, [{
    hash: configurationHash,
    authProfileHash: authProfileRevisionHash,
    providerId: "e2e-v3",
    modelId: "named-model",
    startable: true
  }]);
  let receivedStart: Record<string, unknown> | null = null;
  let startedRun: Record<string, unknown> | null = null;
  await page.route("**/atelier/api/v1/runs", async (route) => {
    if (route.request().method() !== "POST") {
      await route.continue();
      return;
    }
    receivedStart = route.request().postDataJSON() as Record<string, unknown>;
    const runId = receivedStart.run_id as string;
    const publicRunReference = `run1.${Buffer.from(runId).toString("base64url")}`;
    const responseHash = "f".repeat(64);
    startedRun = {
      workflow_format_version: 3,
      run_id: runId,
      public_run_reference: publicRunReference,
      workflow_revision_hash: workflowRevisionHash,
      workflow_name: workflowName,
      agent_binding_set_hash: responseHash,
      run_configuration_revision_hash: responseHash,
      agent_bindings: [{
        role: "builder",
        agent_configuration_revision_hash: configurationHash,
        auth_profile_revision_hash: authProfileRevisionHash,
        profile_id: "routed-configuration",
        revision_number: 1,
        provider_id: "e2e-v3",
        auth_mode: "subscription",
        model: "named-model",
        executor_revision: "immediate/v1"
      }],
      orders: [],
      state_version: 0,
      state: "STARTED",
      current_node_id: "build",
      current_node_execution_id: responseHash,
      node_rail: [{ node_id: "build", state: "queued", attempt: null }],
      cancellation: { cancellable: false, reason: "between-nodes", target_node_execution_id: null },
      terminal_hash: null,
      latest_event_cursor: null
    };
    await route.fulfill({ status: 201, contentType: "application/json", body: JSON.stringify(startedRun) });
  });
  await page.route("**/atelier/api/v1/runs/*", async (route) => {
    if (route.request().method() === "GET" && startedRun !== null) {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(startedRun) });
      return;
    }
    await route.continue();
  });

  await page.goto(`/atelier/catalog/${workflowName}`);
  await page.getByRole("button", { name: "Start" }).click();
  const sheet = page.getByRole("dialog", { name: workflowStartCopy.startTitle(workflowName) });
  await expect(sheet).toBeVisible();
  await sheet.getByLabel(workflowStartCopy.configurationFor("builder")).selectOption(configurationHash);
  const startRun = sheet.getByRole("button", { name: "Start run" });
  await expect(startRun).toBeEnabled();
  await startRun.click();
  await expect.poll(() => receivedStart).not.toBeNull();
  expect(receivedStart).toEqual({
    workflow_format_version: 3,
    run_id: expect.any(String),
    workflow_revision_hash: workflowRevisionHash,
    agent_bindings: [{ role: "builder", agent_configuration_revision_hash: configurationHash }],
    orders: []
  });
  await expect(page).toHaveURL(/\/atelier\/runs\/run1\./);
  await expect(page.getByRole("heading", { level: 1, name: workflowName })).toBeVisible();
});

test("Catalog start sheet names current startability for checked configurations", async ({ page }) => {
  const name = "configuration-startability";
  const schema = await anyJsonSchema(page);
  const workflow = await page.request.post("/atelier/api/v1/workflow-revisions", { headers: { "content-type": "application/yaml" }, data: ["format_version: 3", `name: ${name}`, "nodes:", "  - id: build", "    type: agent", "    role: builder", "    mode: headless", "    instruction: Choose a working executor.", ...declaredOutput(schema), ""].join("\n") });
  expect(workflow.status()).toBe(201);
  expect((await page.request.post("/atelier/api/v1/catalog-lineages", { data: { kind: "workflow", catalog_revision_hash: (await workflow.json()).workflow_revision_hash, actor: "e2e", activated_at: "2026-08-26T00:00:00Z" } })).status()).toBe(201);
  const unavailableHash = "1".repeat(64);
  const availableHash = "2".repeat(64);
  await page.route("**/atelier/api/v1/agent-configuration-revisions?*", async (route) => await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ items: [
    { agent_configuration_revision_hash: unavailableHash, auth_profile_revision_hash: "3".repeat(64), provider_id: "e2e", auth_mode: "subscription", model: "unavailable", executor_revision: "immediate/v1", requested_capability: "headless", startable: false, structurally_startable: false, not_startable_reason: "agent-executor-binding-unavailable", provider_probe_problem_code: null, provider_probe_observed_at: null },
    { agent_configuration_revision_hash: availableHash, auth_profile_revision_hash: "4".repeat(64), provider_id: "e2e", auth_mode: "subscription", model: "available", executor_revision: "immediate/v1", requested_capability: "headless", startable: true, structurally_startable: true, not_startable_reason: null, provider_probe_problem_code: null, provider_probe_observed_at: null }
  ], next_after_revision_hash: null }) }));
  await routeStartSheetModelContract(page, [
    {
      hash: unavailableHash,
      authProfileHash: "3".repeat(64),
      providerId: "e2e",
      modelId: "unavailable",
      startable: false
    },
    {
      hash: availableHash,
      authProfileHash: "4".repeat(64),
      providerId: "e2e",
      modelId: "available",
      startable: true
    }
  ]);
  await page.goto(`/atelier/catalog/${name}`);
  await page.getByRole("button", { name: "Start" }).click();
  const picker = page.getByRole("dialog", { name: workflowStartCopy.startTitle(name) }).getByLabel(workflowStartCopy.configurationFor("builder"));
  await expect(picker.getByRole("option", {
    name: startConfigurationLabel("e2e", "available", "operator-2"),
    exact: true
  })).toHaveCount(1);
  await expect(picker.getByRole("option", {
    name: startConfigurationLabel("e2e", "unavailable", "operator-1"),
    exact: true
  })).toHaveAttribute("disabled", "");
  await picker.selectOption(availableHash);
  await expect(picker).toHaveValue(availableHash);
});
test("Catalog work-item start sheet sends a missing source to Settings", async ({ page }) => {
  const name = "no-source-settings";
  const workItem = await publishSchema(page, WORK_ITEM_SCHEMA_DOCUMENT);
  const output = await anyJsonSchema(page);
  const workflow = await page.request.post("/atelier/api/v1/workflow-revisions", { headers: { "content-type": "application/yaml" }, data: ["format_version: 3", `name: ${name}`, "graph_inputs:", "  - name: work_item", "    schema:", "      ref: work-item", `      revision: ${workItem}`, "nodes:", "  - id: build", "    type: agent", "    role: builder", "    mode: headless", "    instruction: Use the selected work item.", "    inputs:", "      - name: work_item", "        from:", "          graph_input: work_item", ...declaredOutput(output), ""].join("\n") });
  expect(workflow.status()).toBe(201);
  expect((await page.request.post("/atelier/api/v1/catalog-lineages", { data: { kind: "workflow", catalog_revision_hash: (await workflow.json()).workflow_revision_hash, actor: "e2e", activated_at: "2026-08-26T00:00:00Z" } })).status()).toBe(201);
  await page.route("**/atelier/api/v1/queue-items*", async (route) => await route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({
      items: [{
        project_id: "atelier",
        tracker_item_reference: "gh:450",
        item_id: "a".repeat(64),
        state: "OBSERVED",
        revision: 0,
        proposal: null,
        admission: null,
        launch_binding: null,
        blockers: [],
        tracker_enrichment: "ENRICHMENT_UNAVAILABLE",
        title: "Preview door",
        title_observed_at: "2026-09-01T14:00:00Z",
        retired_at: null
      }],
      next_after: null
    })
  }));
  await page.route("**/atelier/api/v1/agent-configuration-revisions?*", async (route) => await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ items: [], next_after_revision_hash: null }) }));
  await routeStartSheetModelContract(page, []);
  await page.goto(`/atelier/catalog/${name}`); await page.getByRole("button", { name: "Start" }).click();
  const sheet = page.getByRole("dialog", { name: workflowStartCopy.startTitle(name) });
  const picker = sheet.getByRole("combobox", { name: workItemFor("work_item") });
  await expect(picker).toBeVisible();
  await expect(sheet.getByText(workflowStartCopy.noSource)).toHaveCount(0);
  await expect(sheet.getByRole("button", { name: workflowStartCopy.connectSource })).toHaveCount(0);
  await picker.click();
  const listbox = sheet.getByRole("listbox", { name: workItemFor("work_item") });
  await expect(sheet.getByText(observedSourceHeading("atelier", workflowStartCopy.github))).toBeVisible();
  await expect(listbox.getByRole("option", { name: "#450 Preview door", exact: true })).toBeVisible();
  await expect(listbox.getByRole("option")).toHaveCount(1);
  await expect(page).not.toHaveURL(/\/atelier\/settings$/);
});
test("Settings keeps the project context available to the Catalog", async ({ page }) => {
  await page.goto("/atelier/settings");
  await expect(page.getByRole("heading", { name: THE_ONE_PROJECT })).toBeVisible();
  await expect(page.getByRole("region", { name: settingsPageCopy.sourcesTitle })).toBeVisible();
  await page.getByRole("link", { name: "Catalog" }).click();
  await expect(page.getByRole("heading", { name: "Catalog" })).toBeVisible();
});
test("Catalog detail draws a published V3 workflow before it starts", async ({ page }) => {
  const name = "catalog-detail-graph";
  const schema = await anyJsonSchema(page);
  const workflow = await page.request.post("/atelier/api/v1/workflow-revisions", { headers: { "content-type": "application/yaml" }, data: ["format_version: 3", `name: ${name}`, "nodes:", "  - id: build", "    type: agent", "    role: builder", "    mode: headless", "    instruction: Draw this first.", ...declaredOutput(schema), ""].join("\n") });
  expect(workflow.status()).toBe(201);
  expect((await page.request.post("/atelier/api/v1/catalog-lineages", { data: { kind: "workflow", catalog_revision_hash: (await workflow.json()).workflow_revision_hash, actor: "e2e", activated_at: "2026-08-26T00:00:00Z" } })).status()).toBe(201);
  await page.goto(`/atelier/catalog/${name}`);
  await expect(page.getByRole("heading", { name })).toBeVisible();
  await expect(page.getByRole("region", { name: workflowGraphCopy.label }).getByRole("button", { name: "build" })).toBeVisible();
});
test("watches a V3 chain move, node by node, without a reload", async ({ page }) => {
  const api = "/atelier/api/v1";
  const schemaHash = await anyJsonSchema(page);
  const workflowYaml = [
    "format_version: 3",
    "name: Two agents watched live",
    "nodes:",
    "  - id: implement",
    "    type: agent",
    "    role: builder",
    "    mode: headless",
    "    instruction: Do the one thing this chain is for.",
    ...declaredOutput(schemaHash),
    "  - id: review",
    "    type: agent",
    "    role: builder",
    "    mode: headless",
    "    instruction: Check what the node before you did.",
    "    depends_on: [implement]",
    ...declaredOutput(schemaHash),
    ""
  ].join("\n");

  const published = await page.request.post(`${api}/workflow-revisions`, {
    headers: { "content-type": "application/yaml" },
    data: workflowYaml
  });
  expect(published.status()).toBe(201);
  const revisionHash = (await published.json()).workflow_revision_hash as string;

  const auth = await page.request.post(`${api}/auth-profile-revisions`, {
    data: { profile_id: "v3-live", revision_number: 1, provider_id: "e2e-v3", auth_mode: "subscription" }
  });
  expect(auth.status()).toBe(201);
  const configuration = await page.request.post(`${api}/agent-configuration-revisions`, {
    data: {
      model: "v3-model",
      auth_profile_revision_hash: (await auth.json()).auth_profile_revision_hash,
      executor_revision: "immediate/v1",
      requested_capability: "headless"
    }
  });
  expect(configuration.status()).toBe(201);
  await publishCheckedRegistryEntry(
    page,
    "e2e-v3",
    "v3-model",
    (await configuration.json()).agent_configuration_revision_hash as string
  );

  const started = await page.request.post(`${api}/runs`, {
    data: {
      workflow_format_version: 2,
      run_id: "v3/watched-live",
      workflow_revision_hash: revisionHash,
      agent_bindings: [
        {
          role: "builder",
          agent_configuration_revision_hash: (await configuration.json())
            .agent_configuration_revision_hash
        }
      ]
    }
  });
  expect(started.status()).toBe(201);
  const reference = (await started.json()).public_run_reference as string;

  // Opened straight after the start, without waiting for the run to end: the
  // stream carries the line's events to the page as the runtime writes them,
  // and it carries the ones already written the same way -- which is what makes
  // this deterministic without making it a lie.
  await page.goto(`/atelier/runs/${reference}`);

  // The graph is the one picture of where the run stands: each node turns
  // Done on it as its event arrives, and nothing repeats that as a second list.
  const chain = page.getByRole("region", { name: workflowGraphCopy.label });
  await expect(chain.getByRole("button", { name: nodeAriaName("implement", "succeeded") })).toBeVisible({
    timeout: 20_000
  });
  await expect(chain.getByRole("button", { name: nodeAriaName("review", "succeeded") })).toBeVisible({
    timeout: 20_000
  });
  await expect(page.getByRole("list", { name: "What finished" })).toHaveCount(0);
  await expect(page.getByLabel(runPageCopy.whereThisRunStands)).toContainText(standingWords.done);

  await page.screenshot({ path: "test-results/v3-run-live.png", fullPage: true });
});

test("draws a running V3 chain as a graph while a node is still working", async ({ page }) => {
  const api = "/atelier/api/v1";
  const schemaHash = await anyJsonSchema(page);
  const workflowYaml = [
    "format_version: 3",
    "name: Two agents drawn live",
    "nodes:",
    "  - id: implement",
    "    type: agent",
    "    role: builder",
    "    mode: headless",
    "    instruction: Do the one thing this chain is for.",
    ...declaredOutput(schemaHash),
    "  - id: review",
    "    type: agent",
    "    role: builder",
    "    mode: headless",
    "    instruction: Check what the node before you did.",
    "    depends_on: [implement]",
    ...declaredOutput(schemaHash),
    ""
  ].join("\n");

  const published = await page.request.post(`${api}/workflow-revisions`, {
    headers: { "content-type": "application/yaml" },
    data: workflowYaml
  });
  expect(published.status()).toBe(201);
  const revisionHash = (await published.json()).workflow_revision_hash as string;

  const auth = await page.request.post(`${api}/auth-profile-revisions`, {
    data: { profile_id: "v3-drawn", revision_number: 1, provider_id: "e2e-v3-slow", auth_mode: "subscription" }
  });
  expect(auth.status()).toBe(201);
  const configuration = await page.request.post(`${api}/agent-configuration-revisions`, {
    data: {
      model: "v3-model",
      auth_profile_revision_hash: (await auth.json()).auth_profile_revision_hash,
      executor_revision: "delayed/v1",
      requested_capability: "headless"
    }
  });
  expect(configuration.status()).toBe(201);
  await publishCheckedRegistryEntry(
    page,
    "e2e-v3-slow",
    "v3-model",
    (await configuration.json()).agent_configuration_revision_hash as string
  );

  const started = await page.request.post(`${api}/runs`, {
    data: {
      workflow_format_version: 2,
      run_id: "v3/drawn-while-running",
      workflow_revision_hash: revisionHash,
      agent_bindings: [
        {
          role: "builder",
          agent_configuration_revision_hash: (await configuration.json())
            .agent_configuration_revision_hash
        }
      ]
    }
  });
  expect(started.status()).toBe(201);
  const reference = (await started.json()).public_run_reference as string;

  await page.goto(`/atelier/runs/${reference}`);

  const graph = page.getByRole("region", { name: workflowGraphCopy.label });
  await expect(graph).toBeVisible();
  await expect(graph.getByRole("button", { name: /implement/ })).toBeVisible();
  await expect(graph.getByRole("button", { name: /review/ })).toBeVisible();
  const working = graph.getByRole("button", { name: new RegExp(`${stateLabels.working}$`) });
  await expect(working).toBeVisible({ timeout: 10_000 });
  await expect(working).toHaveAttribute("data-live", "true");
  await expect(page.getByRole("progressbar")).toHaveCount(0);
  // The log that does not exist is named where a log would live, on the node
  // itself, rather than as a standing box on the run surface.
  await working.click();
  await page.getByRole("tab", { name: runPageCopy.tabLog }).click();
  await expect(page.getByText(runPageCopy.processLogInLease)).toBeVisible();
  await working.click();
  await expect(graph.locator('[data-node-id="implement"]')).toHaveAttribute("data-layer", "0");
  await expect(graph.locator('[data-node-id="review"]')).toHaveAttribute("data-layer", "1");

  await page.screenshot({ path: "test-results/v3-graph-running-desktop.png", fullPage: true });
  await assertNoSeriousAccessibilityFindings(page);

  await page.setViewportSize({ width: 390, height: 844 });
  await expect(graph.getByRole("button", { name: new RegExp(`${stateLabels.working}$`) })).toBeVisible();
  await assertMobileSurface(page);
  await page.screenshot({ path: "test-results/v3-graph-running-390x844.png", fullPage: true });
});

test("proves(the-cockpit-cancels-a-real-run-by-keyboard): cancels a running V3 run by keyboard, on the real backend (#439 P6)", async ({
  page
}) => {
  // The real-interface proof the earlier phases pinned only in unit/vitest: a
  // genuinely-cancelable V3 run, seeded against the real store, is reached and
  // stopped by keyboard alone. What is proven end-to-end here: the server's own
  // cancelability predicate renders the control (never a mock), the staged
  // decision is keyboard-reachable and keyboard-operable, and confirming drives
  // the one audited command to a durable `Cancelled` standing with no false
  // state and no second cancel. What stays at the layer that can force each
  // branch deterministically: the cancel-wins / overtaken-by-success / terminal-
  // retry race outcomes live in tests/integration/test_run_cancellation.py, and
  // the uncertain/reload honesty in frontend/tests/app/v3RunCockpit.test.ts.
  const api = "/atelier/api/v1";
  const cancel = runPageCopy.cancel;
  const schemaHash = await anyJsonSchema(page);
  const workflowYaml = [
    "format_version: 3",
    "name: One agent an operator stops",
    "nodes:",
    "  - id: work",
    "    type: agent",
    "    role: builder",
    "    mode: headless",
    "    instruction: Do the one thing this chain is for.",
    ...declaredOutput(schemaHash),
    ""
  ].join("\n");

  const published = await page.request.post(`${api}/workflow-revisions`, {
    headers: { "content-type": "application/yaml" },
    data: workflowYaml
  });
  expect(published.status()).toBe(201);
  const revisionHash = (await published.json()).workflow_revision_hash as string;

  const auth = await page.request.post(`${api}/auth-profile-revisions`, {
    data: { profile_id: "v3-cancel", revision_number: 1, provider_id: "e2e-v3-held", auth_mode: "subscription" }
  });
  expect(auth.status()).toBe(201);
  const configuration = await page.request.post(`${api}/agent-configuration-revisions`, {
    data: {
      model: "v3-model",
      auth_profile_revision_hash: (await auth.json()).auth_profile_revision_hash,
      executor_revision: "held/v1",
      requested_capability: "headless"
    }
  });
  expect(configuration.status()).toBe(201);
  await publishCheckedRegistryEntry(
    page,
    "e2e-v3-held",
    "v3-model",
    (await configuration.json()).agent_configuration_revision_hash as string
  );

  const started = await page.request.post(`${api}/runs`, {
    data: {
      workflow_format_version: 2,
      run_id: "v3/operator-cancels",
      workflow_revision_hash: revisionHash,
      agent_bindings: [
        {
          role: "builder",
          agent_configuration_revision_hash: (await configuration.json())
            .agent_configuration_revision_hash
        }
      ]
    }
  });
  expect(started.status()).toBe(201);
  const reference = (await started.json()).public_run_reference as string;

  // Wait until the real server itself says this run is cancellable -- the held
  // attempt arms and stays live -- before loading the page, so the first read
  // the cockpit does already carries the cancel predicate.
  await expect
    .poll(
      async () => {
        const read = await page.request.get(`${api}/runs/${reference}`);
        if (!read.ok()) return null;
        return ((await read.json()).cancellation?.cancellable ?? null) as boolean | null;
      },
      { timeout: 15_000 }
    )
    .toBe(true);

  await page.goto(`/atelier/runs/${reference}`);

  // The control exists only because the real server said this run is cancellable
  // (RunResourceV3.cancellation), not because the rail was guessed at.
  const opener = page.getByRole("button", { name: cancel.open });
  await expect(opener).toBeVisible({ timeout: 10_000 });

  // Keyboard reachability and operability: focus lands on the opener, Enter
  // stages the decision, the dialog traps focus between its two honest buttons,
  // and Enter on Cancel run confirms -- no pointer at any step.
  await opener.focus();
  await expect(opener).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { name: cancel.question })).toBeVisible();
  const confirmButton = page.getByRole("button", { name: cancel.confirm });
  const dismissButton = page.getByRole("button", { name: cancel.dismiss });
  await expect(dismissButton).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(confirmButton).toBeFocused();
  await page.keyboard.press("Enter");

  // The command was durably accepted (202): the card reads the honest in-flight
  // word, never a grey nothing and never a premature "cancelled".
  await expect(page.getByText(cancel.accepted).first()).toBeVisible();

  // The real backend ends the run under its own cancel: the standing becomes
  // Cancelled, and the run never re-offers a fresh cancel over a stopped run.
  await expect(page.getByLabel(runPageCopy.whereThisRunStands)).toContainText(
    standingWords.cancelled,
    { timeout: 20_000 }
  );
  await expect(opener).toHaveCount(0);

  // The same stopped truth reads on a narrow phone width, carried by word.
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByLabel(runPageCopy.whereThisRunStands)).toContainText(
    standingWords.cancelled
  );
  await assertNoSeriousAccessibilityFindings(page);
});

test("a node whose answer its own contract refuses never reports success", async ({
  page
}, testInfo) => {
  // The provider's prose cannot become success just because it reached the
  // execution boundary. Its first refusal durably ends ordinal one, then orders
  // one repair attempt. This uses the existing delayed fake so the browser can
  // read that ordinal-two repair while it is live before the same fake refuses
  // its answer too.
  const api = "/atelier/api/v1";
  const proofInstance = testInfo.repeatEachIndex + 1;
  const workflowName = `the chain the operator watched ${proofInstance}`;
  const profileId = `v3-refused-twice-${proofInstance}`;
  const model = `v3-refused-twice-model-${proofInstance}`;
  const runId = `v3/the-silent-one-${proofInstance}`;

  const schemaHash = await publishSchema(page, '{"type": "object"}');

  const workflowYaml = [
    "format_version: 3",
    `name: ${workflowName}`,
    "nodes:",
    "  - id: implement",
    "    type: agent",
    "    role: builder",
    "    mode: headless",
    "    instruction: Write three German sentences about code review.",
    ...declaredOutput(schemaHash, "draft"),
    "  - id: review",
    "    type: agent",
    "    role: builder",
    "    mode: headless",
    "    instruction: Judge the draft you were handed.",
    "    depends_on: [implement]",
    "    inputs:",
    "      - name: draft",
    "        from:",
    "          node: implement",
    "          output: draft",
    ...declaredOutput(schemaHash, "findings"),
    ""
  ].join("\n");
  const published = await page.request.post(`${api}/workflow-revisions`, {
    headers: { "content-type": "application/yaml" },
    data: workflowYaml
  });
  expect(published.status()).toBe(201);
  const revisionHash = (await published.json()).workflow_revision_hash as string;

  const auth = await page.request.post(`${api}/auth-profile-revisions`, {
    data: {
      profile_id: profileId,
      revision_number: 1,
      provider_id: "e2e-v3-slow",
      auth_mode: "subscription"
    }
  });
  expect(auth.status()).toBe(201);
  const configuration = await page.request.post(`${api}/agent-configuration-revisions`, {
    data: {
      model,
      auth_profile_revision_hash: (await auth.json()).auth_profile_revision_hash,
      executor_revision: "delayed/v1",
      requested_capability: "headless"
    }
  });
  expect(configuration.status()).toBe(201);
  await publishCheckedRegistryEntry(
    page,
    "e2e-v3-slow",
    model,
    (await configuration.json()).agent_configuration_revision_hash as string
  );

  const started = await page.request.post(`${api}/runs`, {
    data: {
      workflow_format_version: 2,
      run_id: runId,
      workflow_revision_hash: revisionHash,
      agent_bindings: [
        {
          role: "builder",
          agent_configuration_revision_hash: (await configuration.json())
            .agent_configuration_revision_hash
        }
      ]
    }
  });
  expect(started.status()).toBe(201);
  const reference = (await started.json()).public_run_reference as string;

  // Ordinal one is durably refused before the repair attempt runs. The one
  // active rail entry is therefore ordinal two; its public state may already
  // have crossed the process-launch boundary, but the run is still STARTED and
  // no terminal evidence exists.
  await expect(async () => {
    const read = await page.request.get(`${api}/runs/${reference}`);
    expect(read.status()).toBe(200);
    const body = await read.json();
    expect(body.state).toBe("STARTED");
    expect(body.current_node_id).toBe("implement");
    expect(body.terminal_hash).toBeNull();
    expect(body.node_rail).toEqual([
      {
        node_id: "implement",
        state: "working",
        attempt: { ordinal: 2, state: "POSSIBLY_RAN" }
      },
      { node_id: "review", state: "queued", attempt: null }
    ]);
  }).toPass({ timeout: 15_000 });

  // The same second refusal is terminal: the retained rail attempt is ordinal
  // two, no third round exists, and the dependent review remains queued.
  await expect(async () => {
    const read = await page.request.get(`${api}/runs/${reference}`);
    expect(read.status()).toBe(200);
    const body = await read.json();
    expect(body.state).toBe("FAILED");
    expect(body.current_node_id).toBe("implement");
    expect(body.terminal_hash).toMatch(/^[0-9a-f]{64}$/);
    expect(body.node_rail).toEqual([
      {
        node_id: "implement",
        state: "failed",
        attempt: { ordinal: 2, state: "FAILED" }
      },
      { node_id: "review", state: "queued", attempt: null }
    ]);
  }).toPass({ timeout: 15_000 });

  // V3 deliberately has no duplicated `agent_attempts` list on its run
  // resource; the durable stream is the public ordered list of attempt endings.
  // Both entries must be schema refusals, so ordinal two did not quietly become
  // a success and no ordinal three was ordered.
  const eventStream = await page.request.get(`${api}/runs/${reference}/events`, {
    headers: { accept: "text/event-stream" }
  });
  expect(eventStream.status()).toBe(200);
  const refusalEvents = (await eventStream.text())
    .trim()
    .split(/\r?\n\r?\n/)
    .map((frame) => {
      const data = frame.split(/\r?\n/).find((line) => line.startsWith("data: "));
      if (data === undefined) throw new Error("run event stream frame has no data");
      return JSON.parse(data.slice("data: ".length)) as {
        event: string;
        node_id: string;
        failure_code: string;
        reason: string | null;
        attempt_ordinal: number;
      };
    });
  expect(refusalEvents.map((event) => ({
    event: event.event,
    node_id: event.node_id,
    failure_code: event.failure_code,
    reason: event.reason,
    attempt_ordinal: event.attempt_ordinal
  }))).toEqual([
    {
      event: "AGENT_FAILED",
      node_id: "implement",
      failure_code: "OUTPUT_SCHEMA_REFUSED",
      reason: "output-schema-refused: instance-not-json: Expecting value",
      attempt_ordinal: 1
    },
    {
      event: "AGENT_FAILED",
      node_id: "implement",
      failure_code: "OUTPUT_SCHEMA_REFUSED",
      reason: "output-schema-refused: instance-not-json: Expecting value",
      attempt_ordinal: 2
    }
  ]);

  await page.goto(`/atelier/runs/${reference}`);
  await expect(page.getByRole("heading", { level: 1, name: workflowName })).toBeVisible();
  await expect(page.getByText(runId)).toHaveCount(0);
  await expect(page.getByLabel(runPageCopy.whereThisRunStands)).toContainText("Failed");
  await expect(page.getByLabel(runPageCopy.whereThisRunStands)).not.toContainText("Done");
  await expect(page.getByRole("button", { name: nodeAriaName("implement", "failed") })).toBeVisible();
  await expect(page.getByRole("button", { name: new RegExp(stateLabels.working) })).toHaveCount(0);

  await page.getByRole("button", { name: nodeAriaName("implement", "failed") }).click();
  // A node that stopped opens on Result, where the refusal that stopped it
  // stands. Nothing was written, and the panel says so rather than dressing
  // the silence as a value.
  await expect(page.getByRole("tabpanel")).toContainText("Nothing written.");
  await expect(page.getByRole("tabpanel")).not.toContainText("yet");
  await page.getByRole("tab", { name: runPageCopy.tabPrompt }).click();
  await expect(page.getByRole("tabpanel")).toContainText(
    "Write three German sentences about code review."
  );
  await page.getByRole("tab", { name: runPageCopy.tabEvidence }).click();
  await expect(page.getByRole("group", { name: "Run id" })).toContainText(runId);
  await expect(page.getByText("a moment")).toHaveCount(0);
  await page.screenshot({ path: "test-results/v3-node-refusal.png", fullPage: true });
});

test("clicking a finished node shows its whole log", async ({ page }) => {
  // The other half of the panel: a node that did produce a value shows all of it.
  // The timeline keeps the value short so movement stays readable; the panel
  // shows the whole log the operator asked for.
  const api = "/atelier/api/v1";
  const schemaHash = await anyJsonSchema(page);

  const workflowYaml = [
    "format_version: 3",
    "name: the chain the operator read",
    "nodes:",
    "  - id: implement",
    "    type: agent",
    "    role: builder",
    "    mode: headless",
    "    instruction: Write three German sentences about code review.",
    ...declaredOutput(schemaHash, "draft"),
    "  - id: review",
    "    type: agent",
    "    role: builder",
    "    mode: headless",
    "    instruction: Judge the draft you were handed.",
    "    depends_on: [implement]",
    "    inputs:",
    "      - name: draft",
    "        from:",
    "          node: implement",
    "          output: draft",
    ...declaredOutput(schemaHash, "findings"),
    ""
  ].join("\n");
  const published = await page.request.post(`${api}/workflow-revisions`, {
    headers: { "content-type": "application/yaml" },
    data: workflowYaml
  });
  expect(published.status()).toBe(201);
  const revisionHash = (await published.json()).workflow_revision_hash as string;

  const auth = await page.request.post(`${api}/auth-profile-revisions`, {
    data: {
      profile_id: "v3-read",
      revision_number: 1,
      provider_id: "e2e-v3",
      auth_mode: "subscription"
    }
  });
  expect(auth.status()).toBe(201);
  const configuration = await page.request.post(`${api}/agent-configuration-revisions`, {
    data: {
      model: "v3-model",
      auth_profile_revision_hash: (await auth.json()).auth_profile_revision_hash,
      executor_revision: "immediate/v1",
      requested_capability: "headless"
    }
  });
  expect(configuration.status()).toBe(201);
  await publishCheckedRegistryEntry(
    page,
    "e2e-v3",
    "v3-model",
    (await configuration.json()).agent_configuration_revision_hash as string
  );

  const started = await page.request.post(`${api}/runs`, {
    data: {
      workflow_format_version: 2,
      run_id: "v3/the-read-one",
      workflow_revision_hash: revisionHash,
      agent_bindings: [
        {
          role: "builder",
          agent_configuration_revision_hash: (await configuration.json())
            .agent_configuration_revision_hash
        }
      ]
    }
  });
  expect(started.status()).toBe(201);
  const reference = (await started.json()).public_run_reference as string;

  await expect(async () => {
    const read = await page.request.get(`${api}/runs/${reference}`);
    expect(read.status()).toBe(200);
    expect((await read.json()).state).toBe("COMPLETED");
  }).toPass({ timeout: 15_000 });

  await page.goto(`/atelier/runs/${reference}`);
  await expect(page.getByRole("heading", { level: 1, name: "the chain the operator read" })).toBeVisible();
  await expect(page.getByText("v3/the-read-one")).toHaveCount(0);

  await page.getByRole("button", { name: /implement/ }).click();
  // A finished node opens on what it produced; what it was asked is one tab
  // away, and the whole picture never leaks onto the run surface.
  await expect(page.getByRole("tabpanel")).toContainText("V3 provider bytes");
  await page.getByRole("tab", { name: runPageCopy.tabPrompt }).click();
  await expect(page.getByRole("tabpanel")).toContainText(
    "Write three German sentences about code review."
  );
  await expect(page.getByRole("list", { name: "What finished" })).toHaveCount(0);

  await page.getByRole("tab", { name: runPageCopy.tabEvidence }).click();
  await expect(page.getByRole("group", { name: "Run id" })).toContainText("v3/the-read-one");
  const who = page.getByRole("region", { name: "Who" });
  await expect(who.getByText("Declared model")).toBeVisible();
  await expect(who.getByText("v3-model")).toBeVisible();
  await expect(who.getByText("Resolved model")).toBeVisible();
  await expect(who.getByText("not recorded", { exact: true })).toHaveCount(2);
  await expect(page.getByText(/not recorded yet/)).toHaveCount(0);
  await expect(page.getByRole("alert")).toHaveCount(0);
  await page.screenshot({ path: "test-results/v3-node-detail.png", fullPage: true });
});

test("opening a Catalog detail draws its nodes before a run exists", async ({
  page
}) => {
  const api = "/atelier/api/v1";
  const workflowName = "catalog-node-previews";
  const workflowYaml = [
    "format_version: 3",
    `name: ${workflowName}`,
    "nodes:",
    "  - id: implement",
    "    type: agent",
    "    role: builder",
    "    mode: headless",
    "    instruction: Implement every acceptance sentence of the bound story.",
    "  - id: review",
    "    type: agent",
    "    role: reviewer",
    "    mode: headless",
    "    instruction: Name every defect with the sentence it violates.",
    "    depends_on: [implement]",
    ""
  ].join("\n");
  const published = await page.request.post(`${api}/workflow-revisions`, {
    headers: { "content-type": "application/yaml" },
    data: workflowYaml
  });
  expect(published.status()).toBe(201);
  const admitted = await page.request.post(`${api}/catalog-lineages`, {
    data: {
      kind: "workflow",
      catalog_revision_hash: (await published.json()).workflow_revision_hash,
      actor: "e2e",
      activated_at: "2026-08-26T00:00:00Z"
    }
  });
  expect(admitted.status()).toBe(201);

  await page.goto(`/atelier/catalog/${workflowName}`);
  const graph = page.getByRole("region", { name: workflowGraphCopy.label });
  await expect(graph.getByRole("button", { name: "implement" })).toBeVisible();
  await expect(graph.getByRole("button", { name: "review" })).toBeVisible();
  await graph.getByRole("button", { name: "implement" }).click();
  const detail = page.getByRole("complementary");
  await expect(detail.getByRole("heading", { name: "implement" })).toBeVisible();
  await expect(detail).toContainText("builder");
  await expect(detail).toContainText("Implement every acceptance sentence of the bound story.");
});

test("a declared order is a material field on start, and the typed value travels as that order", async ({
  page
}, testInfo) => {
  const api = "/atelier/api/v1";
  const repetition = testInfo.repeatEachIndex;
  const workflowName = `cook-to-order-${repetition}`;
  const profileId = `cook-order-${repetition}`;
  const modelId = `cook-sonnet-${repetition}`;
  const schema = await page.request.post(`${api}/schema-revisions`, {
    headers: { "content-type": "application/json" },
    data: '{"type":"object","properties":{"portions":{"type":"integer","minimum":1}},"required":["portions"],"additionalProperties":false}'
  });
  expect([200, 201]).toContain(schema.status());
  const schemaHash = (await schema.json()).schema_revision_hash as string;

  const auth = await page.request.post(`${api}/auth-profile-revisions`, {
    data: {
      profile_id: profileId,
      revision_number: 1,
      provider_id: "e2e-v3",
      auth_mode: "subscription"
    }
  });
  expect(auth.status()).toBe(201);
  const configuration = await page.request.post(`${api}/agent-configuration-revisions`, {
    data: {
      model: modelId,
      auth_profile_revision_hash: (await auth.json()).auth_profile_revision_hash,
      executor_revision: "immediate/v1",
      requested_capability: "headless"
    }
  });
  expect(configuration.status()).toBe(201);
  const configurationHash = (await configuration.json()).agent_configuration_revision_hash as string;
  await publishCheckedRegistryEntry(page, "e2e-v3", modelId, configurationHash);

  const answerSchemaHash = await anyJsonSchema(page);
  const workflowYaml = [
    "format_version: 3",
    `name: ${workflowName}`,
    "graph_inputs:",
    "  - name: portions",
    "    schema:",
    "      ref: portions-schema",
    `      revision: ${schemaHash}`,
    "nodes:",
    "  - id: cook",
    "    type: agent",
    "    role: cook",
    "    mode: headless",
    "    instruction: Cook exactly what the order says.",
    "    inputs:",
    "      - name: portions",
    "        from:",
    "          graph_input: portions",
    ...declaredOutput(answerSchemaHash),
    ""
  ].join("\n");
  const published = await page.request.post(`${api}/workflow-revisions`, {
    headers: { "content-type": "application/yaml" },
    data: workflowYaml
  });
  expect(published.status()).toBe(201);
  const admitted = await page.request.post(`${api}/catalog-lineages`, {
    data: {
      kind: "workflow",
      catalog_revision_hash: (await published.json()).workflow_revision_hash,
      actor: "e2e",
      activated_at: "2026-08-26T00:00:00Z"
    }
  });
  expect(admitted.status()).toBe(201);

  await page.goto(`/atelier/catalog/${workflowName}`);
  await page.getByRole("button", { name: "Start" }).click();
  const sheet = page.getByRole("dialog", { name: workflowStartCopy.startTitle(workflowName) });
  const order = sheet.getByRole("group", { name: "Order portions" });
  const material = order.getByRole("spinbutton", { name: "portions (integer) *", exact: true });
  await expect(material).toBeVisible();
  await expect(material).toHaveValue("");
  await expect(sheet.getByRole("button", { name: "Start run" })).toBeDisabled();
  await material.fill("7");
  await sheet.getByLabel(workflowStartCopy.configurationFor("cook")).selectOption(configurationHash);

  // #1130: the order travels as a published artifact, not an inline value
  // (#438 ruled line 3), so the wire proof is the artifact request plus an
  // {name, artifact_hash} order, not the typed value on the /runs body.
  const artifactRequests: string[] = [];
  page.on("request", (request) => {
    if (request.method() === "POST" && new URL(request.url()).pathname === "/atelier/api/v1/artifacts") {
      artifactRequests.push(request.url());
    }
  });
  const runsRequest = page.waitForRequest(
    (request) =>
      request.method() === "POST" && new URL(request.url()).pathname === "/atelier/api/v1/runs"
  );
  await sheet.getByRole("button", { name: "Start run" }).click();
  const runsBody = (await runsRequest).postDataJSON() as {
    orders: ReadonlyArray<Record<string, unknown>>;
  };
  await expect(page.getByRole("heading", { level: 1, name: workflowName })).toBeVisible({
    timeout: 20_000
  });
  expect(runsBody.orders).toEqual([{ name: "portions", artifact_hash: expect.any(String) }]);
  expect(runsBody.orders[0]).not.toHaveProperty("value");
  expect(artifactRequests).toHaveLength(1);

  await expect(page).toHaveURL(/\/atelier\/runs\//);
  const publicRef = new URL(page.url()).pathname.split("/").at(-1);
  let job: string | null = null;
  await expect.poll(async () => {
    const response = await page.request.get(`${api}/runs/${publicRef}/nodes/cook`);
    if (!response.ok()) return null;
    const detail = nodeDetailSchema.parse(await response.json());
    if (detail.job_base64 === null || detail.job_base64.length === 0) return null;
    job = decodeUtf8Base64(detail.job_base64);
    return job;
  }, { timeout: 15_000 }).not.toBeNull();
  expect(job).toContain('{"portions":7}');
});

test("the Catalog detail names the admitted head of a V3 lineage", async ({
  page
}) => {
  const api = "/atelier/api/v1";
  const lineageName = "lineage-grouping-271";
  const schemaHash = await anyJsonSchema(page);
  const olderYaml = [
    "format_version: 3",
    `name: ${lineageName}`,
    "description: The first admitted member.",
    "nodes:",
    "  - id: implement",
    "    type: agent",
    "    role: builder",
    "    mode: headless",
    "    instruction: Write the first admitted draft.",
    ...declaredOutput(schemaHash),
    ""
  ].join("\n");
  const newestYaml = [
    "format_version: 3",
    `name: ${lineageName}`,
    "description: The catalog head.",
    "nodes:",
    "  - id: implement",
    "    type: agent",
    "    role: builder",
    "    mode: headless",
    "    instruction: Write the later admitted draft.",
    ""
  ].join("\n");

  const olderPublished = await page.request.post(`${api}/workflow-revisions`, {
    headers: { "content-type": "application/yaml" },
    data: olderYaml
  });
  expect(olderPublished.status()).toBe(201);
  const olderHash = (await olderPublished.json()).workflow_revision_hash as string;
  const newestPublished = await page.request.post(`${api}/workflow-revisions`, {
    headers: { "content-type": "application/yaml" },
    data: newestYaml
  });
  expect(newestPublished.status()).toBe(201);
  const newestHash = (await newestPublished.json()).workflow_revision_hash as string;

  const founded = await page.request.post(`${api}/catalog-lineages`, {
    data: {
      kind: "workflow",
      catalog_revision_hash: olderHash,
      actor: "e2e",
      activated_at: "2026-08-17T00:00:00Z"
    }
  });
  expect(founded.status()).toBe(201);
  const lineageId = (await founded.json()).lineage_id as string;
  const admitted = await page.request.post(`${api}/catalog-lineages/${lineageId}/members`, {
    data: {
      kind: "workflow",
      catalog_revision_hash: newestHash,
      actor: "e2e",
      activated_at: "2026-08-17T00:00:01Z"
    }
  });
  expect(admitted.status()).toBe(201);
  const head = await page.request.get(`${api}/catalog-revisions/by-name/workflow/${lineageName}`);
  expect(head.status()).toBe(200);
  expect((await head.json()).catalog_revision_hash).toBe(newestHash);

  await page.goto("/atelier/catalog");
  const row = page.getByRole("listitem").filter({ hasText: lineageName });
  await expect(row).toContainText("The catalog head.");
  await expect(row).not.toContainText("The first admitted member.");
  await row.getByRole("link", { name: lineageName }).click();
  await expect(page).toHaveURL(`/atelier/catalog/${lineageName}`);
  await expect(page.getByRole("heading", { level: 1, name: lineageName })).toBeVisible();
  await expect(page.getByRole("button", { name: "Start" })).toBeDisabled();
  await expect(page.getByText("Add one outputs: entry")).toBeVisible();
});

/**
 * The room is alive while the operator stands in it: the Workbench holds the
 * attention stream the Board used to hold, so a wait that opens after the page
 * was read appears where it belongs -- with no navigation and no reload, which
 * is exactly what this test refuses to perform.
 */
test("a wait that opens while the operator stands in the room appears without a reload", async ({
  page
}) => {
  const api = "/atelier/api/v1";
  const schemaHash = await anyJsonSchema(page);
  const workflowName = "Opened while you watched";
  const runId = "workbench/opened-while-watching";
  const question = "Ship it, or hold it back?";

  await page.goto("/atelier");
  await expect(page.getByRole("heading", { name: "Workbench" })).toBeVisible();
  const openedUrl = page.url();
  await expect(page.locator("section.pinned-decision").filter({ hasText: question })).toHaveCount(0);

  const published = await page.request.post(`${api}/workflow-revisions`, {
    headers: { "content-type": "application/yaml" },
    data: [
      "format_version: 3",
      `name: ${workflowName}`,
      "nodes:",
      "  - id: ask",
      "    type: wait",
      `    prompt: ${question}`,
      ...declaredOutput(schemaHash, "verdict"),
      ""
    ].join("\n")
  });
  expect(published.status()).toBe(201);
  const started = await page.request.post(`${api}/runs`, {
    data: {
      workflow_format_version: 3,
      run_id: runId,
      workflow_revision_hash: (await published.json()).workflow_revision_hash as string,
      agent_bindings: [],
      orders: []
    }
  });
  expect(started.status()).toBe(201);

  // No goto, no reload: the stream nudges, the canonical read answers, and the
  // stage stands where the decision belongs.
  const pin = page.locator("section.pinned-decision").filter({ hasText: question });
  await expect(pin).toBeVisible({ timeout: 20_000 });
  await expect(pin.getByRole("heading", { name: question })).toBeVisible();
  // The catalog read that names a run happened before this workflow existed,
  // so the sender line falls back to the run's own id rather than inventing a
  // name -- the honesty `resolveWorkflowName` holds to. Re-reading the names
  // on a nudge is a named gap, not this slice.
  await expect(pin).toContainText(runId);
  await expect(pin).not.toContainText(workflowName);
  await expect(
    page.getByRole("navigation", { name: "Workshop" }).getByRole("link", { name: /Workbench/ })
  ).toContainText(/[1-9]/);
  expect(page.url()).toBe(openedUrl);
});

test("the Workbench pins a run that is waiting for a person, by its catalog name", async ({ page }) => {
  const api = "/atelier/api/v1";
  const schemaHash = await anyJsonSchema(page);
  const foregroundRunId = "workbench/a-foreground-decision";
  const runId = "workbench/waiting-inbox";
  const workflowName = "Waiting on the workbench";
  const question = "Approve this, or name the blocking defect.";
  const published = await page.request.post(`${api}/workflow-revisions`, {
    headers: { "content-type": "application/yaml" },
    data: [
      "format_version: 3",
      `name: ${workflowName}`,
      "nodes:",
      "  - id: ask",
      "    type: wait",
      `    prompt: ${question}`,
      ...declaredOutput(schemaHash, "approval"),
      ""
    ].join("\n")
  });
  expect(published.status()).toBe(201);
  const revisionHash = (await published.json()).workflow_revision_hash as string;

  // The first decision is expanded. A second named waiting run proves the
  // compact pin still carries the sender, question, and clear way to answer.
  const foreground = await page.request.post(`${api}/runs`, {
    data: {
      workflow_format_version: 3,
      run_id: foregroundRunId,
      workflow_revision_hash: revisionHash,
      agent_bindings: [],
      orders: []
    }
  });
  expect(foreground.status()).toBe(201);
  const started = await page.request.post(`${api}/runs`, {
    data: {
      workflow_format_version: 3,
      run_id: runId,
      workflow_revision_hash: revisionHash,
      agent_bindings: [],
      orders: []
    }
  });
  expect(started.status()).toBe(201);
  const reference = (await started.json()).public_run_reference as string;

  await expect(async () => {
    const listed = await page.request.get(`${api}/runs?state=WAITING_INPUT&limit=50`);
    expect(listed.status()).toBe(200);
    const body = (await listed.json()) as { items: RunListRow[] };
    expect(healthyRunListItems(body.items).some((run) => run.run_id === runId)).toBe(true);
  }).toPass({ timeout: 15_000 });

  await page.goto("/atelier");
  // This backend is shared across every earlier test in this file, so other
  // runs may already wait here: this run is named in its sender line, not counted.
  const pin = page.locator(
    `section.pinned-decision-compact[aria-labelledby="pinned-decision-title-${reference}"]`
  );
  await expect(pin).toBeVisible();
  await expect(pin.locator(".from")).toContainText(`ask · ${workflowName}`);
  await expect(pin.getByText(question, { exact: true })).toBeVisible();
  await expect(pin.getByText("Answer →", { exact: true })).toBeVisible();
  // The one number the rail carries, ochre and only where something wants you.
  const workbenchLink = page
    .getByRole("navigation", { name: "Workshop" })
    .getByRole("link", { name: /Workbench/ });
  await expect(workbenchLink).toContainText(/[1-9]/);

  await page.screenshot({ path: "test-results/workbench-inbox-desktop.png", fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(pin).toBeVisible();
  await assertMobileSurface(page);
  await page.screenshot({ path: "test-results/workbench-inbox-390x844.png", fullPage: true });

  await pin.getByText("Answer →", { exact: true }).click();
  const expandedPin = page.locator(
    `section.pinned-decision:has(a[href="/atelier/runs/${reference}"])`
  );
  await expect(expandedPin.getByRole("heading", { name: question })).toBeVisible();
  const door = expandedPin.getByRole("link", { name: "open the run" });
  await expect(door).toHaveCount(1);
  await door.click();
  await expect(page).toHaveURL(new RegExp(`/atelier/runs/${reference.replace(".", "\\.")}$`));
});

test("an admitted V3 workflow is named by the Catalog", async ({
  page
}) => {
  const api = "/atelier/api/v1";
  const lineageName = "name-admission-213";
  const schemaHash = await anyJsonSchema(page);
  const yaml = [
    "format_version: 3",
    `name: ${lineageName}`,
    "description: Named by the cockpit after publish.",
    "nodes:",
    "  - id: implement",
    "    type: agent",
    "    role: builder",
    "    mode: headless",
    "    instruction: Write the admitted draft.",
    ...declaredOutput(schemaHash),
    ""
  ].join("\n");

  const published = await page.request.post(`${api}/workflow-revisions`, {
    headers: { "content-type": "application/yaml" },
    data: yaml
  });
  expect(published.status()).toBe(201);
  const workflowRevisionHash = (await published.json()).workflow_revision_hash as string;
  const admitted = await page.request.post(`${api}/catalog-lineages`, {
    data: { kind: "workflow", catalog_revision_hash: workflowRevisionHash, actor: "e2e", activated_at: "2026-08-26T00:00:00Z" }
  });
  expect(admitted.status()).toBe(201);

  const head = await page.request.get(`${api}/catalog-revisions/by-name/workflow/${lineageName}`);
  expect(head.status()).toBe(200);
  const named = await head.json();
  expect(named.display_name).toBe(lineageName);
  expect(named.lineage_id).toMatch(/^[0-9a-f]{64}$/);
  expect(named.catalog_revision_hash).toMatch(/^[0-9a-f]{64}$/);

  await page.goto("/atelier/catalog");
  const row = page.getByRole("listitem").filter({ hasText: lineageName });
  await expect(row.getByRole("link", { name: lineageName })).toBeVisible();
  await row.getByRole("link", { name: lineageName }).click();
  await expect(page).toHaveURL(`/atelier/catalog/${lineageName}`);
  await expect(page.getByRole("heading", { level: 1, name: lineageName })).toBeVisible();
});

test("the Catalog keeps unpublished and unnamable workflows visible", async ({
  page
}, testInfo) => {
  const api = "/atelier/api/v1";
  const repetition = testInfo.repeatEachIndex;
  const unlisted = `unlisted-213-${repetition}`;
  const unnamable = `Der erste Lauf auf 213 ${repetition}`;
  const schemaHash = await anyJsonSchema(page);
  const unlistedYaml = [
    "format_version: 3",
    `name: ${unlisted}`,
    "nodes:",
    "  - id: implement",
    "    type: agent",
    "    role: builder",
    "    mode: headless",
    "    instruction: Published and never admitted.",
    ...declaredOutput(schemaHash),
    ""
  ].join("\n");
  const unnamableYaml = [
    "format_version: 3",
    `name: ${unnamable}`,
    "nodes:",
    "  - id: implement",
    "    type: agent",
    "    role: builder",
    "    mode: headless",
    "    instruction: This title cannot be a catalog name.",
    ""
  ].join("\n");

  expect(
    (await page.request.post(`${api}/workflow-revisions`, {
      headers: { "content-type": "application/yaml" },
      data: unlistedYaml
    })).status()
  ).toBe(201);
  expect(
    (await page.request.post(`${api}/workflow-revisions`, {
      headers: { "content-type": "application/yaml" },
      data: unnamableYaml
    })).status()
  ).toBe(201);

  await page.goto("/atelier/catalog");
  const unpublished = page.getByRole("listitem").filter({ hasText: unlisted });
  await expect(unpublished.getByText(catalogPageCopy.notAdmitted)).toBeVisible();
  await expect(unpublished.getByRole("button", { name: /Admit/ })).toHaveCount(0);
  await unpublished.getByRole("link", { name: unlisted }).click();
  await expect(page.getByRole("heading", { level: 1, name: unlisted })).toBeVisible();
  await expect(page.getByRole("button", { name: catalogPageCopy.start })).toBeDisabled();

  await page.goto("/atelier/catalog");
  const unnamed = page.getByRole("listitem").filter({ hasText: unnamable });
  await expect(unnamed).toBeVisible();
  await expect(unnamed.getByRole("link", { name: unnamable })).toBeVisible();
  await expect(unnamed.getByRole("button", { name: /Admit/ })).toHaveCount(0);
});

test("a waiting V3 run is answerable on its own run page", async ({ page }) => {
  const api = "/atelier/api/v1";
  const schemaHash = await anyJsonSchema(page);
  const runId = "v3/answer-card";
  const published = await page.request.post(`${api}/workflow-revisions`, {
    headers: { "content-type": "application/yaml" },
    data: [
      "format_version: 3",
      "name: answer-card-194",
      "nodes:",
      "  - id: ask",
      "    type: wait",
      "    prompt: Approve this, or name the blocking defect.",
      ...declaredOutput(schemaHash, "approval"),
      ""
    ].join("\n")
  });
  expect(published.status()).toBe(201);
  const revisionHash = (await published.json()).workflow_revision_hash as string;

  const started = await page.request.post(`${api}/runs`, {
    data: {
      workflow_format_version: 3,
      run_id: runId,
      workflow_revision_hash: revisionHash,
      agent_bindings: [],
      orders: []
    }
  });
  expect(started.status()).toBe(201);
  const reference = (await started.json()).public_run_reference as string;

  await expect(async () => {
    const read = await page.request.get(`${api}/runs/${reference}`);
    expect(read.status()).toBe(200);
    expect((await read.json()).state).toBe("WAITING_INPUT");
  }).toPass({ timeout: 15_000 });

  await page.goto(`/atelier/runs/${reference}`);
  await expect(page.getByRole("heading", { level: 1, name: "answer-card-194" })).toBeVisible();
  await expect(page.getByText(runId)).toHaveCount(0);
  // The waiting step presents itself as its question, never as its type and
  // id (operator, 23.08.).
  const question = "Approve this, or name the blocking defect.";
  await expect(page.getByRole("heading", { level: 2, name: question })).toBeVisible();
  await expect(page.getByText("WAIT ask")).toHaveCount(0);
  const card = page.getByRole("region", { name: question });
  // Plain words, not JSON: the composer must not fail a person on syntax.
  await card.getByRole("textbox", { name: runPageCopy.answerLabel }).fill("approved");
  await card.getByRole("button", { name: runPageCopy.answerSubmit }).click();

  await expect(async () => {
    const read = await page.request.get(`${api}/runs/${reference}`);
    expect(read.status()).toBe(200);
    const body = await read.json();
    expect(body.state).toBe("COMPLETED");
    expect(body.run_id).toBe(runId);
    expect(body.terminal_hash).toMatch(/^[0-9a-f]{64}$/);
  }).toPass({ timeout: 20_000 });

  await page.goto(`/atelier/runs/${reference}`);
  await expect(page.getByRole("heading", { level: 2, name: question })).toHaveCount(0);
  await expect(page.getByLabel(runPageCopy.whereThisRunStands)).toContainText(standingWords.done);
  await expect(page.getByText(/not yet/)).toHaveCount(0);
  // The run head's exact facts line (#553): started/ended/duration, in place
  // of the "Exact time" reveal link it replaces.
  await expect(page.getByText(/started .* · ended .* · duration/)).toBeVisible();
  await expect(page.getByText("Exact time")).toHaveCount(0);

  await page.screenshot({
    path: "test-results/v3-answer-card-desktop.png",
    fullPage: true
  });

  await page.getByRole("button", { name: `ask — ${standingWords.done}` }).click();
  const panel = page.getByRole("complementary");
  // The node panel's own facts line (#553) replaces the "Done" chip: one
  // joined string, state word first, so this pins the structure the line
  // always has rather than the exact joined text. #562 landed, so an
  // answered Wait now carries real started_at/ended_at like any other node.
  await expect(panel.getByText(/^Done · started .* · ended .* · duration/)).toBeVisible();
  // #562: an answered Wait's own bytes are readable again, not the interim
  // "answer itself is not yet kept readable" copy.
  await expect(panel.getByRole("tabpanel", { name: runPageCopy.tabResult })).toHaveText(
    '"approved"'
  );

  await page.screenshot({
    path: "test-results/v3-run-done-meta-lines-desktop.png",
    fullPage: true
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({
    path: "test-results/v3-run-done-meta-lines-mobile.png",
    fullPage: true
  });
  await page.setViewportSize({ width: 1280, height: 900 });
});

test("a waiting V3 run with a boolean answer schema offers decision buttons", async ({ page }) => {
  const api = "/atelier/api/v1";
  const booleanSchemaHash = await publishSchema(page, '{"type": "boolean"}');
  const runId = "v3/decision-buttons";
  const published = await page.request.post(`${api}/workflow-revisions`, {
    headers: { "content-type": "application/yaml" },
    data: [
      "format_version: 3",
      "name: decision-buttons-553",
      "nodes:",
      "  - id: ship",
      "    type: wait",
      "    prompt: Ship it, or hold it back?",
      ...declaredOutput(booleanSchemaHash, "decision"),
      ""
    ].join("\n")
  });
  expect(published.status()).toBe(201);
  const revisionHash = (await published.json()).workflow_revision_hash as string;

  const started = await page.request.post(`${api}/runs`, {
    data: {
      workflow_format_version: 3,
      run_id: runId,
      workflow_revision_hash: revisionHash,
      agent_bindings: [],
      orders: []
    }
  });
  expect(started.status()).toBe(201);
  const reference = (await started.json()).public_run_reference as string;

  await expect(async () => {
    const read = await page.request.get(`${api}/runs/${reference}`);
    expect(read.status()).toBe(200);
    expect((await read.json()).state).toBe("WAITING_INPUT");
  }).toPass({ timeout: 15_000 });

  /**
   * The real route now classifies a published boolean schema as `kind:
   * "boolean"` on its own -- proven directly against
   * `GET workflow-revisions/{hash}` by
   * `test_a_published_wait_schema_reads_back_classified_over_the_real_route`
   * (tests/integration/test_workflow_v3_publication.py), no intercept. This
   * intercept stays here anyway, deliberately, so this UI-focused screenshot
   * test asserts the composer's own contract -- what it renders once
   * `kind`/`values` say `boolean` -- without depending on the real read
   * route's timing or schema realism.
   */
  await page.route(`**/atelier/api/v1/workflow-revisions/${revisionHash}`, async (route) => {
    const real = await page.request.get(`${api}/workflow-revisions/${revisionHash}`);
    const body = await real.json();
    body.graph.wait_answer_schemas = [
      {
        node_id: "ship",
        schema: { ref: "decision-schema", revision: booleanSchemaHash },
        kind: "boolean",
        string_typed: false,
        values: null
      }
    ];
    await route.fulfill({
      status: real.status(),
      contentType: "application/json",
      body: JSON.stringify(body)
    });
  });

  await page.goto(`/atelier/runs/${reference}`);
  const question = "Ship it, or hold it back?";
  await expect(page.getByRole("heading", { level: 2, name: question })).toBeVisible();
  const card = page.getByRole("region", { name: question });
  await expect(card.getByRole("textbox")).toHaveCount(0);
  const yes = card.getByRole("button", { name: runPageCopy.answerYes });
  const no = card.getByRole("button", { name: runPageCopy.answerNo });
  await expect(yes).toBeVisible();
  await expect(no).toBeVisible();

  await page.screenshot({
    path: "test-results/v3-wait-decision-buttons-desktop.png",
    fullPage: true
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({
    path: "test-results/v3-wait-decision-buttons-mobile.png",
    fullPage: true
  });
  await page.setViewportSize({ width: 1280, height: 900 });

  await yes.click();
  await expect(page.getByText(`${runPageCopy.answeredPrefix} ${runPageCopy.answerYes}`)).toBeVisible();

  await expect(async () => {
    const read = await page.request.get(`${api}/runs/${reference}`);
    expect(read.status()).toBe(200);
    const body = await read.json();
    expect(body.state).toBe("COMPLETED");
    expect(body.terminal_hash).toMatch(/^[0-9a-f]{64}$/);
  }).toPass({ timeout: 20_000 });
});
