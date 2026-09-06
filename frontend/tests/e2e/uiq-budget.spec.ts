import { expect, test, type Locator, type Page } from "@playwright/test";

import type { RunListRow, RunV3 } from "../../src/api/client";
import { catalogPageCopy } from "../../src/lib/catalogPageCopy";
import { conductorConversationCopy } from "../../src/lib/conductorConversation";
import { historyPageCopy } from "../../src/lib/historyPageCopy";
import { runPageCopy } from "../../src/lib/runPageCopy";
import { nodeAriaName } from "../../src/lib/stateMarkCopy";
import { standingWords } from "../../src/lib/runState";
import { settingsPageCopy } from "../../src/lib/settingsPageCopy";
import { workbenchPageCopy } from "../../src/lib/workbenchPageCopy";
import { WORKSHOP_DESTINATION } from "../../src/lib/workshop";
import { healthyRunListItems } from "../support/runListRows";

/**
 * Click and glance budgets from mockup v8 §07. Each number is a door count on
 * the named frame, not an invented allowance. This slice covers Workbench,
 * History, and Catalog's find-by-search, open-tile, reach-Start and
 * import-via-button tasks. reach-Start is Catalog + card until Start is in
 * view, not §07's 4-click start-by-hand / Start-run remainder. The Run view
 * adds its standing sentence and node Log path from frame #v8-14-run-log.
 * Queue-admit is named in §07 and has no Workbench door yet — not asserted
 * here. Settings' connect-a-source path is measured against mockup v8.
 *
 * Contract only: user-click count to the goal, and the named goal elements in
 * the viewport without scrolling at the picture's 390 and 1280 widths.
 */

/** Frame "Empty — only the ear" / "Loaded — the ear stays reachable without scrolling" (§02). */
const SEND_A_MESSAGE_CLICKS = 0;
const SEND_A_MESSAGE_GLANCES = 1;

/** Frame "Full — two open questions, two runs, the conversation with its cards" (§02). */
const ANSWER_A_DECISION_CLICKS = 1;
const ANSWER_A_DECISION_GLANCES = 1;
const DECISION_QUESTION = "Ship it, or hold it back?";

/** Frame "Full" / "Loaded — five runs, long conversation, the ear stays reachable without scrolling" (§02). */
const FIND_THE_RUNNING_RUN_CLICKS = 0;
const FIND_THE_RUNNING_RUN_GLANCES = 1;

/** Untitled populated History frame (§05). From another room: History + the line. */
const OPEN_FINISHED_RUN_FROM_HISTORY_CLICKS = 2;
const OPEN_FINISHED_RUN_FROM_HISTORY_GLANCES = 2;

/** Frame #v8-14-run-log. The standing sentence is already in the Run view head. */
const READ_RUN_STANDING_CLICKS = 0;
const READ_RUN_STANDING_GLANCES = 1;

/** Frame #v8-14-run-log. The graph node opens its panel; Log opens its stored-attempt view. */
const OPEN_NODE_LOG_CLICKS = 2;
const OPEN_NODE_LOG_GLANCES = 3;

/** Frame "Connect a source — the sheet · and the question before a disconnect" (§06). */
const CONNECT_A_SOURCE_CLICKS = 3;
const CONNECT_A_SOURCE_GLANCES = 2;
const CONNECT_A_SOURCE_DOOR = "Connect a source";

/** Frame "List" (§04). Search compact on the right: "I know the name." Fill is not a click; the goal glance is the found tile. From another room: Catalog. */
const FIND_BY_SEARCH_CLICKS = 1;
const FIND_BY_SEARCH_GLANCES = 1;

/** Frame "List" (§04). "the whole card is the click; no hash, no start on the card." From another room: Catalog + the card. */
const OPEN_TILE_CLICKS = 2;
const OPEN_TILE_GLANCES = 2;

/** Frame "Workflow detail — still" (§04). Start top right. Catalog + card until Start is in view, not §07's Start-run remainder. */
const REACH_START_CLICKS = 2;
const REACH_START_GLANCES = 1;

/** Frame "List" Import door + "Import — the sheet after the drop" (§04). §07 via Import: button · file · Add = 3 from Catalog; from another room Catalog +1. The declaring kind on that sheet is the extra click #66 requires and the picture left open. Glances: door, sheet (via-button analog of §07 "veil, sheet"). */
const IMPORT_VIA_BUTTON_CLICKS = 5;
const IMPORT_VIA_BUTTON_GLANCES = 2;

const VIEWPORTS = [
  { width: 1280, height: 900 },
  { width: 390, height: 844 }
] as const;

const API = "/atelier/api/v1";

type ClickBudget = {
  count: number;
  click(locator: Locator): Promise<void>;
  pickFile(
    locator: Locator,
    file: { name: string; mimeType: string; buffer: Buffer }
  ): Promise<void>;
};

function clickBudget(): ClickBudget {
  let count = 0;
  return {
    get count() {
      return count;
    },
    async click(locator) {
      count += 1;
      await locator.click();
    },
    async pickFile(locator, file) {
      count += 1;
      await locator.setInputFiles(file);
    }
  };
}

async function resetToKnownStore(page: Page): Promise<void> {
  const reset = await page.request.post("/__e2e/recompose?reset=true");
  expect(reset.status()).toBe(202);
  const expectedGeneration = await reset.text();
  await expect(async () => {
    expect(await (await page.request.get("/__e2e/generation")).text()).toBe(expectedGeneration);
  }).toPass({ timeout: 20_000 });
}

type InputFixtureRun = {
  public_run_reference: string;
  workflow_revision_hash: string;
  current_node_id: string;
};

async function retireReconciliationFixtures(page: Page): Promise<void> {
  const listed = await page.request.get(`${API}/runs?state=WAITING_RECONCILIATION&limit=50`);
  expect(listed.status()).toBe(200);
  const { items: rows } = (await listed.json()) as { items: RunListRow[] };
  const items: RunV3[] = healthyRunListItems(rows);
  expect(items).toHaveLength(2);

  for (const run of items) {
    expect(run.state).toBe("WAITING_RECONCILIATION");
    const retired = await page.request.post(
      `${API}/runs/${run.public_run_reference}/reconciliations`,
      {
        headers: { "content-type": "application/json" },
        data: {
          command_id: `reconcile-uiq-budget-${run.public_run_reference}`,
          // The served run resource carries no waiting block -- the question lives
        // on the durable intent, and a freshly seeded baseline intent stands
        // at its first version.
        expected_intent_state_version: 1,
          actor: "Playwright fixture isolation",
          evidence: "REQ-UIQ-02 stages its own Workbench shelf.",
          determination: { type: "operator_authoritative_absence" }
        }
      }
    );
    expect([200, 202]).toContain(retired.status());
  }

  await expect(async () => {
    const remaining = await page.request.get(`${API}/runs?state=WAITING_RECONCILIATION&limit=50`);
    expect(remaining.status()).toBe(200);
    expect(((await remaining.json()) as { items: unknown[] }).items).toHaveLength(0);
  }).toPass({ timeout: 20_000 });

  await expect(async () => {
    const states: string[] = [];
    for (const fixture of items) {
      const current = await page.request.get(`${API}/runs/${fixture.public_run_reference}`);
      expect(current.status()).toBe(200);
      const run = (await current.json()) as InputFixtureRun & { state: string };
      states.push(run.state);
      if (run.state === "WAITING_INPUT") {
        const fence = await page.request.get(
          `/__e2e/current-wait-execution?public_run_reference=${encodeURIComponent(run.public_run_reference)}`
        );
        expect(fence.status()).toBe(200);
        const { expected_node_execution_id: expectedNodeExecutionId } =
          (await fence.json()) as { expected_node_execution_id: string };
        const answered = await page.request.post(
          `${API}/runs/${run.public_run_reference}/answers`,
          {
            headers: { "content-type": "application/json" },
            data: {
              workflow_revision_hash: run.workflow_revision_hash,
              node_id: run.current_node_id,
              expected_node_execution_id: expectedNodeExecutionId,
              actor: "operator",
              answer_base64: "MQ=="
            }
          }
        );
        expect(answered.status()).toBe(202);
      }
    }
    expect(states).toEqual(["COMPLETED", "COMPLETED"]);
  }).toPass({ timeout: 20_000 });
}

async function publishSchema(page: Page, document: string): Promise<string> {
  const published = await page.request.post(`${API}/schema-revisions`, {
    headers: { "content-type": "application/json" },
    data: document
  });
  expect([200, 201]).toContain(published.status());
  return (await published.json()).schema_revision_hash as string;
}

async function publishCheckedRegistryEntry(
  page: Page,
  providerId: string,
  modelId: string,
  configurationHash: string
): Promise<void> {
  const current = await page.request.get(`${API}/model-registries/${providerId}`);
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
  const registry = await page.request.put(`${API}/model-registries/${providerId}`, {
    data: {
      revision_number: (currentRegistry?.revision_number ?? 0) + 1,
      entries: [
        ...existingEntries,
        { model_id: modelId, agent_configuration_revision_hash: configurationHash }
      ]
    }
  });
  expect([200, 201]).toContain(registry.status());
  const checked = await page.request.post(`${API}/model-registries/${providerId}/validations`, {
    data: { agent_configuration_revision_hash: configurationHash }
  });
  expect([200, 201]).toContain(checked.status());
}

async function publishWaitWorkflow(page: Page, name: string, prompt: string): Promise<string> {
  const schemaHash = await publishSchema(page, '{"type":"boolean"}');
  const published = await page.request.post(`${API}/workflow-revisions`, {
    headers: { "content-type": "application/yaml" },
    data: [
      "format_version: 3",
      `name: ${name}`,
      "nodes:",
      "  - id: ship",
      "    type: wait",
      `    prompt: ${prompt}`,
      "    outputs:",
      "      - name: decision",
      "        schema:",
      "          ref: decision-schema",
      `          revision: ${schemaHash}`,
      ""
    ].join("\n")
  });
  expect(published.status()).toBe(201);
  return (await published.json()).workflow_revision_hash as string;
}

async function startWaitRun(page: Page, runId: string, revisionHash: string): Promise<string> {
  const started = await page.request.post(`${API}/runs`, {
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
    const read = await page.request.get(`${API}/runs/${reference}`);
    expect((await read.json()).state).toBe("WAITING_INPUT");
  }).toPass({ timeout: 20_000 });
  return reference;
}

async function startHeldRun(page: Page, name: string, runId: string): Promise<string> {
  const schemaHash = await publishSchema(page, "true");
  const published = await page.request.post(`${API}/workflow-revisions`, {
    headers: { "content-type": "application/yaml" },
    data: [
      "format_version: 3",
      `name: ${name}`,
      "nodes:",
      "  - id: work",
      "    type: agent",
      "    role: builder",
      "    mode: headless",
      "    instruction: Stay in hand so the living row can be found.",
      "    outputs:",
      "      - name: result",
      "        schema:",
      "          ref: result-schema",
      `          revision: ${schemaHash}`,
      ""
    ].join("\n")
  });
  expect(published.status()).toBe(201);
  const revisionHash = (await published.json()).workflow_revision_hash as string;
  const auth = await page.request.post(`${API}/auth-profile-revisions`, {
    data: {
      profile_id: "uiq-budget-held",
      revision_number: 1,
      provider_id: "e2e-v3-held",
      auth_mode: "subscription"
    }
  });
  expect([200, 201]).toContain(auth.status());
  const configuration = await page.request.post(`${API}/agent-configuration-revisions`, {
    data: {
      model: "uiq-budget-held-model",
      auth_profile_revision_hash: (await auth.json()).auth_profile_revision_hash,
      executor_revision: "held/v1",
      requested_capability: "headless"
    }
  });
  expect([200, 201]).toContain(configuration.status());
  const agentHash = (await configuration.json()).agent_configuration_revision_hash as string;
  await publishCheckedRegistryEntry(page, "e2e-v3-held", "uiq-budget-held-model", agentHash);
  const started = await page.request.post(`${API}/runs`, {
    data: {
      workflow_format_version: 3,
      run_id: runId,
      workflow_revision_hash: revisionHash,
      agent_bindings: [{ role: "builder", agent_configuration_revision_hash: agentHash }],
      orders: []
    }
  });
  expect(started.status()).toBe(201);
  const reference = (await started.json()).public_run_reference as string;
  await expect(async () => {
    const read = await page.request.get(`${API}/runs/${reference}`);
    expect((await read.json()).state).toBe("STARTED");
  }).toPass({ timeout: 20_000 });
  return reference;
}

async function startFinishedRun(page: Page, name: string, runId: string): Promise<string> {
  const schemaHash = await publishSchema(page, "true");
  const published = await page.request.post(`${API}/workflow-revisions`, {
    headers: { "content-type": "application/yaml" },
    data: [
      "format_version: 3",
      `name: ${name}`,
      "nodes:",
      "  - id: work",
      "    type: agent",
      "    role: builder",
      "    mode: headless",
      "    instruction: Finish so History can open the result.",
      "    outputs:",
      "      - name: result",
      "        schema:",
      "          ref: result-schema",
      `          revision: ${schemaHash}`,
      ""
    ].join("\n")
  });
  expect(published.status()).toBe(201);
  const revisionHash = (await published.json()).workflow_revision_hash as string;
  const auth = await page.request.post(`${API}/auth-profile-revisions`, {
    data: {
      profile_id: "uiq-budget-done",
      revision_number: 1,
      provider_id: "e2e-v3",
      auth_mode: "subscription"
    }
  });
  expect([200, 201]).toContain(auth.status());
  const configuration = await page.request.post(`${API}/agent-configuration-revisions`, {
    data: {
      model: "uiq-budget-done-model",
      auth_profile_revision_hash: (await auth.json()).auth_profile_revision_hash,
      executor_revision: "immediate/v1",
      requested_capability: "headless"
    }
  });
  expect([200, 201]).toContain(configuration.status());
  const agentHash = (await configuration.json()).agent_configuration_revision_hash as string;
  await publishCheckedRegistryEntry(page, "e2e-v3", "uiq-budget-done-model", agentHash);
  const started = await page.request.post(`${API}/runs`, {
    data: {
      workflow_format_version: 3,
      run_id: runId,
      workflow_revision_hash: revisionHash,
      agent_bindings: [{ role: "builder", agent_configuration_revision_hash: agentHash }],
      orders: []
    }
  });
  expect(started.status()).toBe(201);
  const reference = (await started.json()).public_run_reference as string;
  await expect(async () => {
    const read = await page.request.get(`${API}/runs/${reference}`);
    expect((await read.json()).state).toBe("COMPLETED");
  }).toPass({ timeout: 20_000 });
  return reference;
}

async function openWorkbench(page: Page): Promise<void> {
  await page.goto("/atelier/chat");
  await expect(page.getByRole("heading", { name: workbenchPageCopy.title })).toBeVisible();
  await page.locator(".workshop-stage").evaluate((element) => {
    element.scrollTop = 0;
  });
}

function rail(page: Page) {
  return page.getByRole("navigation", { name: "Workshop" });
}

function catalogTile(page: Page, name: string): Locator {
  return page
    .getByRole("listitem")
    .filter({ hasText: name })
    .getByRole("link", { name, exact: true });
}

function catalogImportDocument(schemaHash: string, name: string): string {
  return [
    "format_version: 3",
    `name: ${name}`,
    "nodes:",
    "  - id: ask",
    "    type: wait",
    "    prompt: Is the import door open?",
    "    outputs:",
    "      - name: answer",
    "        schema:",
    "          ref: answer-schema",
    `          revision: ${schemaHash}`,
    ""
  ].join("\n");
}

test("proves(core-tasks-meet-named-click-and-glance-budgets): Workbench, History, Catalog and Run view core tasks stay inside mockup v8 click and glance budgets at 390 and 1280", async ({
  page
}, testInfo) => {
  test.setTimeout(300_000);
  const suffix = testInfo.repeatEachIndex;
  const runningName = `uiq-running-${suffix}`;
  const finishedName = `uiq-finished-${suffix}`;
  const waitingName = `uiq-waiting-${suffix}`;

  await resetToKnownStore(page);
  await retireReconciliationFixtures(page);
  await startFinishedRun(page, finishedName, `uiq/finished-${suffix}`);
  await startHeldRun(page, runningName, `uiq/running-${suffix}`);
  const waitRevision = await publishWaitWorkflow(page, waitingName, DECISION_QUESTION);
  const catalogSchemaHash = await publishSchema(page, "true");
  // The send-a-message task below needs a real conductor to send into
  // (#1103): without one the composer stays honestly locked and Enter does
  // nothing, so this budget seeds the production conductor catalog the same
  // way `workbench-conductor.spec.ts` does. A server-side publish, not a
  // browser action, so it costs neither a click nor a glance.
  const seeded = await page.request.post("/__e2e/seed-conductor");
  expect(seeded.ok()).toBeTruthy();

  for (const [index, viewport] of VIEWPORTS.entries()) {
    await page.setViewportSize(viewport);
    await startWaitRun(page, `uiq/waiting-${suffix}-${viewport.width}`, waitRevision);
    await openWorkbench(page);
    // A fresh page load resolves the conductor's own connection, and
    // restores its already-started run, asynchronously; typing before that
    // settles would either find Send disabled (locked while "reading",
    // #1103, #1114) or race a second run into existence instead of
    // continuing the one from the last viewport. The connected composer hint
    // is the actual signal that resolution finished -- the first viewport's
    // message is the conversation's own first round, so it still "begins"
    // it, but the reload before every later viewport replays that round's
    // events, so the composer already carries `composerHintOngoing` by then
    // (`connectedComposerHint`, WorkbenchPage.svelte).
    const resolvedComposerHint =
      index === 0 ? conductorConversationCopy.composerHint : conductorConversationCopy.composerHintOngoing;
    await expect(page.getByText(resolvedComposerHint)).toBeVisible({
      timeout: 20_000
    });
    await expect(page.getByRole("button", { name: workbenchPageCopy.send })).toBeEnabled({
      timeout: 20_000
    });

    const composer = page.getByLabel(workbenchPageCopy.composerLabel);
    const sendGlances = [composer];
    for (const glance of sendGlances) {
      await expect(glance, `send-a-message glance at ${viewport.width}`).toBeInViewport();
    }
    expect(sendGlances).toHaveLength(SEND_A_MESSAGE_GLANCES);
    const send = clickBudget();
    const spoken = `budget probe ${viewport.width}`;
    await composer.fill(spoken);
    await composer.press("Enter");
    await expect(page.getByText(spoken)).toBeVisible();
    expect(send.count, `send-a-message clicks at ${viewport.width}`).toBeLessThanOrEqual(
      SEND_A_MESSAGE_CLICKS
    );

    const findRunning = clickBudget();
    const runningRow = page.getByRole("link", { name: new RegExp(runningName) });
    await expect(runningRow).toBeVisible({ timeout: 20_000 });
    const runningGlances = [runningRow];
    for (const glance of runningGlances) {
      await expect(glance, `find-the-running-run glance at ${viewport.width}`).toBeInViewport();
    }
    expect(runningGlances).toHaveLength(FIND_THE_RUNNING_RUN_GLANCES);
    expect(findRunning.count, `find-the-running-run clicks at ${viewport.width}`).toBeLessThanOrEqual(
      FIND_THE_RUNNING_RUN_CLICKS
    );

    const yes = page.getByRole("button", { name: runPageCopy.answerYes });
    await expect(page.getByRole("heading", { name: DECISION_QUESTION })).toBeVisible({
      timeout: 20_000
    });
    const decisionGlances = [yes];
    for (const glance of decisionGlances) {
      await expect(glance, `answer-a-decision glance at ${viewport.width}`).toBeInViewport();
    }
    expect(decisionGlances).toHaveLength(ANSWER_A_DECISION_GLANCES);
    const answer = clickBudget();
    await answer.click(yes);
    await expect(yes).toHaveCount(0, { timeout: 20_000 });
    expect(answer.count, `answer-a-decision clicks at ${viewport.width}`).toBeLessThanOrEqual(
      ANSWER_A_DECISION_CLICKS
    );
  }

  for (const viewport of VIEWPORTS) {
    await page.setViewportSize(viewport);
    await openWorkbench(page);

    const historyPath = clickBudget();
    await historyPath.click(rail(page).getByRole("link", { name: WORKSHOP_DESTINATION.history.label }));
    await expect(page.getByRole("heading", { name: historyPageCopy.title })).toBeVisible();
    const finishedRow = page.getByRole("link", { name: new RegExp(finishedName) });
    await expect(finishedRow).toBeVisible();
    await expect(finishedRow, `history line glance at ${viewport.width}`).toBeInViewport();
    let historyGlances = 1;
    await historyPath.click(finishedRow);
    await expect(page).toHaveURL(/\/atelier\/runs\/run1\./);
    const resultSentence = page.getByLabel(runPageCopy.whereThisRunStands);
    await expect(resultSentence).toContainText(standingWords.done);
    await expect(resultSentence, `history sentence glance at ${viewport.width}`).toBeInViewport();
    historyGlances += 1;
    expect(historyGlances).toBe(OPEN_FINISHED_RUN_FROM_HISTORY_GLANCES);
    expect(historyPath.count).toBeLessThanOrEqual(OPEN_FINISHED_RUN_FROM_HISTORY_CLICKS);

    const readStanding = clickBudget();
    await expect(resultSentence, `read-run-standing glance at ${viewport.width}`).toBeInViewport();
    const standingGlances = 1;
    expect(standingGlances).toBe(READ_RUN_STANDING_GLANCES);
    expect(readStanding.count, `read-run-standing clicks at ${viewport.width}`).toBeLessThanOrEqual(
      READ_RUN_STANDING_CLICKS
    );

    const nodeLogPath = clickBudget();
    const node = page.getByRole("button", { name: nodeAriaName("work", "succeeded") });
    await expect(node).toBeVisible();
    await expect(node, `open-node-log node glance at ${viewport.width}`).toBeInViewport();
    let nodeLogGlances = 1;
    await nodeLogPath.click(node);
    const panel = page.getByRole("complementary");
    await expect(panel.getByRole("heading", { name: "work" })).toBeVisible();
    const log = panel.getByRole("tab", { name: runPageCopy.tabLog });
    await expect(log, `open-node-log tab glance at ${viewport.width}`).toBeInViewport();
    nodeLogGlances += 1;
    await nodeLogPath.click(log);
    await expect(log).toHaveAttribute("aria-selected", "true");
    const storedAttempt = panel.getByRole("tabpanel", { name: runPageCopy.tabLog });
    await expect(storedAttempt).toBeVisible();
    await expect(storedAttempt, `open-node-log transcript glance at ${viewport.width}`).toBeInViewport();
    nodeLogGlances += 1;
    expect(nodeLogGlances).toBe(OPEN_NODE_LOG_GLANCES);
    expect(nodeLogPath.count, `open-node-log clicks at ${viewport.width}`).toBeLessThanOrEqual(
      OPEN_NODE_LOG_CLICKS
    );
  }

  for (const viewport of VIEWPORTS) {
    await page.setViewportSize(viewport);
    const catalogName = `uiq-catalog-${suffix}-${viewport.width}`;
    const catalogLink = rail(page).getByRole("link", {
      name: WORKSHOP_DESTINATION.catalog.label
    });

    await openWorkbench(page);
    const importPath = clickBudget();
    await importPath.click(catalogLink);
    await expect(page.getByRole("heading", { name: catalogPageCopy.title })).toBeVisible();
    const importButton = page.getByRole("button", { name: catalogPageCopy.import });
    await expect(importButton).toBeVisible();
    await expect(importButton, `import-via-button door glance at ${viewport.width}`).toBeInViewport();
    let importGlances = 1;
    await importPath.click(importButton);
    await expect(page.getByRole("dialog", { name: catalogPageCopy.import })).toHaveCount(0);
    await importPath.pickFile(page.getByLabel(catalogPageCopy.filePicker), {
      name: `${catalogName}.yaml`,
      mimeType: "application/octet-stream",
      buffer: Buffer.from(catalogImportDocument(catalogSchemaHash, catalogName))
    });
    const importSheet = page.getByRole("dialog", { name: catalogPageCopy.import });
    const addToCatalog = page.getByRole("button", { name: catalogPageCopy.addToCatalog });
    await expect(importSheet).toBeVisible({ timeout: 20_000 });
    await expect(importSheet, `import-via-button sheet glance at ${viewport.width}`).toBeInViewport();
    importGlances += 1;
    await expect(addToCatalog, `import-via-button add door at ${viewport.width}`).toBeInViewport();
    expect(importGlances).toBe(IMPORT_VIA_BUTTON_GLANCES);
    await importPath.click(importSheet.getByRole("button", { name: catalogPageCopy.kindWorkflow, exact: true }));
    await importPath.click(addToCatalog);
    await expect(importSheet).toHaveCount(0, { timeout: 20_000 });
    await expect(catalogTile(page, catalogName)).toBeVisible({ timeout: 20_000 });
    expect(importPath.count, `import-via-button clicks at ${viewport.width}`).toBeLessThanOrEqual(
      IMPORT_VIA_BUTTON_CLICKS
    );

    await openWorkbench(page);
    const findPath = clickBudget();
    await findPath.click(catalogLink);
    await expect(page.getByRole("heading", { name: catalogPageCopy.title })).toBeVisible();
    const search = page.getByLabel(catalogPageCopy.searchLabel);
    const foundTile = catalogTile(page, catalogName);
    await expect(search).toBeVisible();
    await search.fill(catalogName);
    await expect(foundTile).toBeVisible();
    const findGlances = [foundTile];
    for (const glance of findGlances) {
      await expect(glance, `find-by-search glance at ${viewport.width}`).toBeInViewport();
    }
    expect(findGlances).toHaveLength(FIND_BY_SEARCH_GLANCES);
    expect(findPath.count, `find-by-search clicks at ${viewport.width}`).toBeLessThanOrEqual(
      FIND_BY_SEARCH_CLICKS
    );

    await openWorkbench(page);
    const openPath = clickBudget();
    await openPath.click(catalogLink);
    await expect(page.getByRole("heading", { name: catalogPageCopy.title })).toBeVisible();
    const tile = catalogTile(page, catalogName);
    await expect(tile).toBeVisible();
    await expect(tile, `open-tile card glance at ${viewport.width}`).toBeInViewport();
    let openGlances = 1;
    await openPath.click(tile);
    const detailHeading = page.getByRole("heading", { name: catalogName, exact: true });
    await expect(detailHeading).toBeVisible();
    await expect(detailHeading, `open-tile glance at ${viewport.width}`).toBeInViewport();
    openGlances += 1;
    expect(openGlances).toBe(OPEN_TILE_GLANCES);
    expect(openPath.count, `open-tile clicks at ${viewport.width}`).toBeLessThanOrEqual(
      OPEN_TILE_CLICKS
    );

    const start = page.getByRole("button", { name: catalogPageCopy.start, exact: true });
    await expect(start).toBeEnabled();
    const startGlances = [start];
    for (const glance of startGlances) {
      await expect(glance, `reach-Start glance at ${viewport.width}`).toBeInViewport();
    }
    expect(startGlances).toHaveLength(REACH_START_GLANCES);
    expect(openPath.count, `reach-Start clicks at ${viewport.width}`).toBeLessThanOrEqual(
      REACH_START_CLICKS
    );
  }
});

test("Settings connect-a-source stays inside mockup v8 click and glance budgets at 390 and 1280", async ({
  page
}) => {
  test.setTimeout(60_000);
  await resetToKnownStore(page);

  for (const viewport of VIEWPORTS) {
    await page.setViewportSize(viewport);
    await openWorkbench(page);
    const connectPath = clickBudget();
    await connectPath.click(
      rail(page).getByRole("link", { name: WORKSHOP_DESTINATION.settings.label })
    );
    const sources = page.getByRole("heading", { name: settingsPageCopy.sourcesTitle });
    await expect(sources).toBeVisible();
    await expect(sources, `settings room glance at ${viewport.width}`).toBeInViewport();
    await page.route("**/atelier/api/v1/projects/*/sources", async (route) => {
      if (route.request().method() !== "POST") {
        await route.fallback();
        return;
      }
      await route.fulfill({
        status: 201,
        json: {
          public_source_reference: "source1.dGVzdA",
          kind: "github",
          address: "github.com/FlexOr2/docs",
          scope: "issues",
          connected_at: null,
          revision: 1,
          auth_method: "personal-access-token"
        }
      });
    });
    const connectDoor = page.getByRole("button", { name: CONNECT_A_SOURCE_DOOR });
    await expect(connectDoor, `connect-a-source room glance at ${viewport.width}`).toBeInViewport();
    let connectGlances = 1;
    await connectPath.click(connectDoor);
    const sheet = page.getByRole("heading", { name: CONNECT_A_SOURCE_DOOR });
    await expect(sheet, `connect-a-source sheet glance at ${viewport.width}`).toBeInViewport();
    connectGlances += 1;
    expect(connectGlances).toBe(CONNECT_A_SOURCE_GLANCES);
    const dialog = page.getByRole("dialog", { name: CONNECT_A_SOURCE_DOOR });
    await dialog.getByRole("textbox", { name: settingsPageCopy.where }).fill("github.com/FlexOr2/docs");
    await dialog.getByLabel(settingsPageCopy.token).fill("test-token");
    await connectPath.click(page.getByRole("button", { name: settingsPageCopy.connect, exact: true }));
    expect(connectPath.count).toBeLessThanOrEqual(CONNECT_A_SOURCE_CLICKS);
  }
});
