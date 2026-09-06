import { expect, test, type Locator, type Page } from "@playwright/test";

import { backLinkCopy } from "../../src/lib/backLinkCopy";
import { shortPublicRunReference } from "../../src/lib/fingerprint";
import { historyPageCopy } from "../../src/lib/historyPageCopy";
import { historyWhenLabel } from "../../src/lib/historyRows";
import { WORK_ITEM_ORDER_SCHEMA_REVISION } from "../../src/lib/orderSchema";
import { standingWords } from "../../src/lib/runState";
import { WORK_ITEM_SCHEMA_DOCUMENT } from "./workItemSchema";

const VIEWPORTS = [
  { width: 1280, height: 900 },
  { width: 390, height: 844 }
] as const;

const RAW_PROVIDER_BYTES = "V3 provider bytes";

function historyCards(page: Page, workflowName: string) {
  return page.locator(".history-row").filter({ hasText: workflowName });
}

/** Characters actually painted in the element's box, skipping visually-hidden copy. */
async function renderedText(locator: Locator): Promise<string> {
  return locator.evaluate((element) => {
    const root = element.getBoundingClientRect();
    const range = document.createRange();
    const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
    let visible = "";
    let node = walker.nextNode();
    while (node !== null) {
      let ancestor = node.parentElement;
      let skip = false;
      while (ancestor !== null && element.contains(ancestor)) {
        if (ancestor.classList.contains("visually-hidden")) {
          skip = true;
          break;
        }
        ancestor = ancestor.parentElement;
      }
      if (!skip) {
        const text = node.textContent ?? "";
        for (let index = 0; index < text.length; index += 1) {
          range.setStart(node, index);
          range.setEnd(node, index + 1);
          for (const rect of range.getClientRects()) {
            if (rect.width < 0.5 || rect.height < 0.5) continue;
            const intersects =
              rect.left < root.right - 0.5 &&
              rect.right > root.left + 0.5 &&
              rect.top < root.bottom - 0.5 &&
              rect.bottom > root.top + 0.5;
            if (intersects) {
              visible += text[index] ?? "";
              break;
            }
          }
        }
      }
      node = walker.nextNode();
    }
    return visible.replace(/\s+/g, " ").trim();
  });
}

async function anyJsonSchema(page: Page): Promise<string> {
  const published = await page.request.post("/atelier/api/v1/schema-revisions", {
    headers: { "content-type": "application/json" },
    data: "true"
  });
  expect([200, 201]).toContain(published.status());
  return (await published.json()).schema_revision_hash as string;
}

async function publishCheckedModelRegistry(
  page: Page,
  providerId: string,
  modelId: string,
  configurationHash: string
): Promise<void> {
  const endpoint = `/atelier/api/v1/model-registries/${encodeURIComponent(providerId)}`;
  const current = await page.request.get(endpoint);
  expect([200, 404]).toContain(current.status());
  const registry = current.status() === 200
    ? await current.json() as {
      revision_number: number;
      entries: Array<{
        model_id: string;
        agent_configuration_revision_hash: string;
        source: string;
        provider_check: string;
      }>;
    }
    : undefined;
  const entriesByModelId = new Map(registry?.entries.map((entry) => [entry.model_id, entry]));
  entriesByModelId.set(modelId, {
    model_id: modelId,
    agent_configuration_revision_hash: configurationHash,
    source: "operator",
    provider_check: "checked"
  });
  const published = await page.request.put(endpoint, {
    data: {
      revision_number: registry === undefined ? 1 : registry.revision_number + 1,
      entries: [...entriesByModelId.values()].map((entry) => ({
        model_id: entry.model_id,
        agent_configuration_revision_hash: entry.agent_configuration_revision_hash
      }))
    }
  });
  expect([200, 201]).toContain(published.status());
  const registryBody = await published.json() as {
    entries: Array<{ agent_configuration_revision_hash: string; provider_check: string }>;
  };
  if (registryBody.entries.find(
    (entry) => entry.agent_configuration_revision_hash === configurationHash
  )?.provider_check !== "checked") {
    const validation = await page.request.post(`${endpoint}/validations`, {
      data: { agent_configuration_revision_hash: configurationHash }
    });
    expect([200, 201]).toContain(validation.status());
  }
}

async function publishWorkflow(page: Page, name: string, schemaHash: string): Promise<string> {
  const yaml = [
    "format_version: 3",
    `name: ${name}`,
    "nodes:",
    "  - id: build",
    "    type: agent",
    "    role: builder",
    "    mode: headless",
    "    instruction: Do the one thing.",
    "    outputs:",
    "      - name: result",
    "        schema:",
    "          ref: result-schema",
    `          revision: ${schemaHash}`,
    ""
  ].join("\n");
  const published = await page.request.post("/atelier/api/v1/workflow-revisions", {
    headers: { "content-type": "application/yaml" },
    data: yaml
  });
  expect(published.status()).toBe(201);
  return (await published.json()).workflow_revision_hash as string;
}

async function startCompletedRun(
  page: Page,
  runId: string,
  revisionHash: string,
  agentHash: string
): Promise<{ reference: string; endedAt: string }> {
  const started = await page.request.post("/atelier/api/v1/runs", {
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
    const read = await page.request.get(`/atelier/api/v1/runs/${reference}`);
    expect((await read.json()).state).toBe("COMPLETED");
  }).toPass({ timeout: 20_000 });
  const finished = await page.request.get(`/atelier/api/v1/runs/${reference}`);
  const endedAt = (await finished.json()).ended_at as string;
  expect(endedAt).toBeTruthy();
  return { reference, endedAt };
}

async function startCompletedRunWithOrders(
  page: Page,
  runId: string,
  revisionHash: string,
  agentHash: string,
  orders: Array<Record<string, string>>
): Promise<{ reference: string; endedAt: string }> {
  const started = await page.request.post("/atelier/api/v1/runs", {
    data: {
      workflow_format_version: 3,
      run_id: runId,
      workflow_revision_hash: revisionHash,
      agent_bindings: [{ role: "builder", agent_configuration_revision_hash: agentHash }],
      orders
    }
  });
  expect(started.status()).toBe(201);
  const reference = (await started.json()).public_run_reference as string;
  await expect(async () => {
    const read = await page.request.get(`/atelier/api/v1/runs/${reference}`);
    expect((await read.json()).state).toBe("COMPLETED");
  }).toPass({ timeout: 20_000 });
  const finished = await page.request.get(`/atelier/api/v1/runs/${reference}`);
  const endedAt = (await finished.json()).ended_at as string;
  expect(endedAt).toBeTruthy();
  return { reference, endedAt };
}


test("proves(a-history-row-names-when-work-and-result): two finished runs of the same workflow name when they ran, a work-item dash, and a derived result that is not raw bytes", async ({
  page
}, testInfo) => {
  test.setTimeout(120_000);
  const token = `${testInfo.repeatEachIndex}-${testInfo.retry}`;
  const workflowName = `history-717-same-recipe-${token}`;
  const schemaHash = await anyJsonSchema(page);

  const auth = await page.request.post("/atelier/api/v1/auth-profile-revisions", {
    data: {
      profile_id: `history-717-${token}`,
      revision_number: 1,
      provider_id: "e2e-v3",
      auth_mode: "subscription"
    }
  });
  expect([200, 201]).toContain(auth.status());
  const configuration = await page.request.post("/atelier/api/v1/agent-configuration-revisions", {
    data: {
      model: `history-717-model-${token}`,
      auth_profile_revision_hash: (await auth.json()).auth_profile_revision_hash,
      executor_revision: "immediate/v1",
      requested_capability: "headless"
    }
  });
  expect([200, 201]).toContain(configuration.status());
  const agentHash = (await configuration.json()).agent_configuration_revision_hash as string;
  await publishCheckedModelRegistry(page, "e2e-v3", `history-717-model-${token}`, agentHash);

  const revisionHash = await publishWorkflow(page, workflowName, schemaHash);
  const first = await startCompletedRun(page, `history-717/first-${token}`, revisionHash, agentHash);
  const firstClock = historyWhenLabel(first.endedAt, new Date()).clock;
  await expect(async () => {
    expect(historyWhenLabel(new Date().toISOString(), new Date()).clock).not.toBe(firstClock);
  }).toPass({ timeout: 2_500 });
  const second = await startCompletedRun(page, `history-717/second-${token}`, revisionHash, agentHash);

  for (const viewport of VIEWPORTS) {
    await page.setViewportSize(viewport);
    await page.goto("/atelier/history");
    const rows = historyCards(page, workflowName);
    await expect(rows).toHaveCount(2);
    const header = page.locator(".history-head-row");
    if (viewport.width === 1280) {
      await expect(header).toBeVisible();
    } else {
      await expect(header).toBeHidden();
    }
    for (const row of await rows.all()) {
      await expect(row.locator(".row-purpose")).toHaveText(workflowName);
      await expect(row.locator(".row-result")).toContainText(historyPageCopy.outcome.text);
      await expect(row.locator(".row-result")).not.toContainText(RAW_PROVIDER_BYTES);
      await expect(row.locator(".row-result")).not.toHaveText(standingWords.done);
      await expect(row.locator(".row-when")).toBeVisible();
      if (viewport.width === 1280) {
        await expect(row.locator(".row-work-item")).toContainText(historyPageCopy.workItemPlaceholder);
      }
    }
    const firstText = await rows.nth(0).innerText();
    const secondText = await rows.nth(1).innerText();
    expect(firstText).not.toBe(secondText);
    const tokens = [
      (await rows.nth(0).locator(".row-run").textContent())?.trim(),
      (await rows.nth(1).locator(".row-run").textContent())?.trim()
    ];
    expect(tokens[0]).not.toBe(tokens[1]);
    expect(new Set(tokens)).toEqual(
      new Set([
        shortPublicRunReference(first.reference),
        shortPublicRunReference(second.reference)
      ])
    );

    if (viewport.width === 390) {
      const documentWidth = await page.evaluate(() => document.documentElement.scrollWidth);
      const viewportWidth = await page.evaluate(() => document.documentElement.clientWidth);
      expect(documentWidth).toBeLessThanOrEqual(viewportWidth);
    }
  }
});

test("a populated History row at 390 names the work item and stays inside the viewport", async ({
  page
}, testInfo) => {
  test.setTimeout(120_000);
  const token = `${testInfo.repeatEachIndex}-${testInfo.retry}`;
  const workflowName = `history-717-work-item-${token}`;
  const workItemSchema = await page.request.post("/atelier/api/v1/schema-revisions", {
    headers: { "content-type": "application/json" },
    data: WORK_ITEM_SCHEMA_DOCUMENT
  });
  expect([200, 201]).toContain(workItemSchema.status());
  expect((await workItemSchema.json()).schema_revision_hash).toBe(WORK_ITEM_ORDER_SCHEMA_REVISION);
  const schemaHash = await anyJsonSchema(page);

  const auth = await page.request.post("/atelier/api/v1/auth-profile-revisions", {
    data: {
      profile_id: `history-717-item-${token}`,
      revision_number: 1,
      provider_id: "e2e-v3",
      auth_mode: "subscription"
    }
  });
  expect([200, 201]).toContain(auth.status());
  const configuration = await page.request.post("/atelier/api/v1/agent-configuration-revisions", {
    data: {
      model: `history-717-item-model-${token}`,
      auth_profile_revision_hash: (await auth.json()).auth_profile_revision_hash,
      executor_revision: "immediate/v1",
      requested_capability: "headless"
    }
  });
  expect([200, 201]).toContain(configuration.status());
  const agentHash = (await configuration.json()).agent_configuration_revision_hash as string;
  await publishCheckedModelRegistry(page, "e2e-v3", `history-717-item-model-${token}`, agentHash);

  const yaml = [
    "format_version: 3",
    `name: ${workflowName}`,
    "graph_inputs:",
    "  - name: work_item",
    "    schema:",
    "      ref: work-item",
    `      revision: ${WORK_ITEM_ORDER_SCHEMA_REVISION}`,
    "nodes:",
    "  - id: build",
    "    type: agent",
    "    role: builder",
    "    mode: headless",
    "    instruction: Do the one thing.",
    "    inputs:",
    "      - name: work_item",
    "        from:",
    "          graph_input: work_item",
    "    outputs:",
    "      - name: result",
    "        schema:",
    "          ref: result-schema",
    `          revision: ${schemaHash}`,
    ""
  ].join("\n");
  const published = await page.request.post("/atelier/api/v1/workflow-revisions", {
    headers: { "content-type": "application/yaml" },
    data: yaml
  });
  expect(published.status()).toBe(201);
  const revisionHash = (await published.json()).workflow_revision_hash as string;
  await startCompletedRunWithOrders(
    page,
    `history-717/item-${token}`,
    revisionHash,
    agentHash,
    [{ name: "work_item", work_item: "gh:450" }]
  );

  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/atelier/history");
  const row = historyCards(page, workflowName).first();
  await expect(row).toBeVisible();
  await expect(page.locator(".history-head-row")).toBeHidden();
  await expect(row.locator(".row-purpose")).toHaveText(workflowName);
  await expect(row.locator(".row-purpose")).not.toContainText("work_item");
  await expect(row.locator(".row-work-item")).toBeVisible();
  await expect(row.locator(".row-work-item")).toContainText("#450");
  await expect(row.locator(".row-work-item")).not.toContainText("e2e observed work item");
  await expect(row.locator(".row-result")).toContainText(historyPageCopy.outcome.text);
  await expect(row.locator(".row-result")).not.toContainText(RAW_PROVIDER_BYTES);
  const whenRendered = await renderedText(row.locator(".row-when"));
  const workItemRendered = await renderedText(row.locator(".row-work-item"));
  const resultRendered = await renderedText(row.locator(".row-result"));
  expect(whenRendered.length).toBeGreaterThan(1);
  expect(workItemRendered).toContain("#450");
  expect(resultRendered).toContain(historyPageCopy.outcome.text);
  expect(resultRendered.length).toBeGreaterThan(1);
  const documentWidth = await page.evaluate(() => document.documentElement.scrollWidth);
  const viewportWidth = await page.evaluate(() => document.documentElement.clientWidth);
  expect(documentWidth).toBeLessThanOrEqual(viewportWidth);
  await page.screenshot({
    path: testInfo.outputPath("history-390-populated-row.png"),
    fullPage: true
  });
});

test("a finished code-review row at 1280 shows a visible derived Result, never Done or raw bytes", async ({
  page
}, testInfo) => {
  test.setTimeout(120_000);
  const token = `${testInfo.repeatEachIndex}-${testInfo.retry}`;
  const workflowName = "code-review";
  const schemaHash = await anyJsonSchema(page);

  const auth = await page.request.post("/atelier/api/v1/auth-profile-revisions", {
    data: {
      profile_id: `history-717-result-${token}`,
      revision_number: 1,
      provider_id: "e2e-v3",
      auth_mode: "subscription"
    }
  });
  expect([200, 201]).toContain(auth.status());
  const configuration = await page.request.post("/atelier/api/v1/agent-configuration-revisions", {
    data: {
      model: `history-717-result-model-${token}`,
      auth_profile_revision_hash: (await auth.json()).auth_profile_revision_hash,
      executor_revision: "immediate/v1",
      requested_capability: "headless"
    }
  });
  expect([200, 201]).toContain(configuration.status());
  const agentHash = (await configuration.json()).agent_configuration_revision_hash as string;
  await publishCheckedModelRegistry(page, "e2e-v3", `history-717-result-model-${token}`, agentHash);

  const revisionHash = await publishWorkflow(page, workflowName, schemaHash);
  const finished = await startCompletedRun(
    page,
    `history-717/code-review-${token}`,
    revisionHash,
    agentHash
  );

  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto("/atelier/history");
  const row = page.locator(".history-row").filter({
    has: page.locator(`a.history-row-open[href="/atelier/runs/${finished.reference}"]`)
  });
  await expect(row).toBeVisible();
  await expect(row.locator(".row-purpose")).toHaveText(workflowName);
  await expect(row.locator(".row-result")).toContainText(historyPageCopy.outcome.text);
  await expect(row.locator(".row-result")).not.toContainText(RAW_PROVIDER_BYTES);
  await expect(row.locator(".row-result")).not.toHaveText(standingWords.done);
  const resultRendered = await renderedText(row.locator(".row-result"));
  expect(resultRendered.length).toBeGreaterThan(0);
  expect(resultRendered).toContain(historyPageCopy.outcome.text);
  expect(resultRendered).not.toBe(standingWords.done);
  expect(resultRendered).not.toContain(RAW_PROVIDER_BYTES);
});

test("opening a finished run from History, the trail leads back to History", async ({
  page
}, testInfo) => {
  test.setTimeout(120_000);
  const token = `${testInfo.repeatEachIndex}-${testInfo.retry}`;
  const workflowName = `history-back-link-${token}`;
  const schemaHash = await anyJsonSchema(page);

  const auth = await page.request.post("/atelier/api/v1/auth-profile-revisions", {
    data: {
      profile_id: `history-back-link-${token}`,
      revision_number: 1,
      provider_id: "e2e-v3",
      auth_mode: "subscription"
    }
  });
  expect([200, 201]).toContain(auth.status());
  const configuration = await page.request.post("/atelier/api/v1/agent-configuration-revisions", {
    data: {
      model: `history-back-link-model-${token}`,
      auth_profile_revision_hash: (await auth.json()).auth_profile_revision_hash,
      executor_revision: "immediate/v1",
      requested_capability: "headless"
    }
  });
  expect([200, 201]).toContain(configuration.status());
  const agentHash = (await configuration.json()).agent_configuration_revision_hash as string;
  await publishCheckedModelRegistry(page, "e2e-v3", `history-back-link-model-${token}`, agentHash);

  const revisionHash = await publishWorkflow(page, workflowName, schemaHash);
  await startCompletedRun(page, `history-back-link/${token}`, revisionHash, agentHash);

  await page.goto("/atelier/history");
  const row = historyCards(page, workflowName).first();
  await expect(row).toBeVisible();
  await row.click();

  await expect(page.getByRole("heading", { level: 1, name: workflowName })).toBeVisible();
  const trail = page.getByRole("navigation", { name: backLinkCopy.whereYouAre });
  await expect(trail.getByRole("link", { name: backLinkCopy.history })).toBeVisible();
  await expect(trail.getByRole("link", { name: backLinkCopy.workbench })).toHaveCount(0);
  await trail.getByRole("link", { name: backLinkCopy.history }).click();

  await expect(page).toHaveURL(/\/atelier\/history$/);
  await expect(page.getByRole("heading", { name: historyPageCopy.title })).toBeVisible();
});
