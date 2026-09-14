import { expect, test, type Page } from "@playwright/test";

/**
 * History's own narrow-width proof (#717): a real run with an extravagantly
 * long workflow name must never widen the row past the viewport at 390px --
 * the Purpose cell is the workflow name, clips (`text-overflow: ellipsis`),
 * and does not overflow the frame. Joined order names are not purpose.
 */

async function publishArtifact(page: Page, content: string): Promise<string> {
  const published = await page.request.post("/atelier/api/v1/artifacts", {
    headers: { "content-type": "application/octet-stream" },
    data: content
  });
  expect([200, 201]).toContain(published.status());
  return (await published.json()).artifact_hash as string;
}

async function anyJsonSchema(page: Page): Promise<string> {
  const published = await page.request.post("/atelier/api/v1/schema-revisions", {
    headers: { "content-type": "application/json" },
    data: "true"
  });
  expect([200, 201]).toContain(published.status());
  return (await published.json()).schema_revision_hash as string;
}

const ORDER_NAMES = ["diff", "target_file", "base_branch", "release_notes", "reviewer_handle"];
const LONG_WORKFLOW_NAME =
  "rebuild-the-entire-continuous-integration-pipeline-end-to-end-with-full-coverage";

async function publishLongWorkflow(page: Page, schemaHash: string): Promise<string> {
  const yaml = [
    "format_version: 3",
    `name: ${LONG_WORKFLOW_NAME}`,
    "graph_inputs:",
    ...ORDER_NAMES.flatMap((name) => [
      `  - name: ${name}`,
      "    schema:",
      `      ref: ${name}-schema`,
      `      revision: ${schemaHash}`
    ]),
    "nodes:",
    "  - id: build",
    "    type: agent",
    "    role: builder",
    "    mode: headless",
    "    instruction: Do the one thing.",
    "    inputs:",
    ...ORDER_NAMES.flatMap((name) => [
      `      - name: ${name}`,
      "        from:",
      `          graph_input: ${name}`
    ]),
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

test("a long workflow name never widens History's row past 390px, and purpose is the workflow not joined orders", async (
  { page },
  testInfo
) => {
  const schemaHash = await anyJsonSchema(page);

  const auth = await page.request.post("/atelier/api/v1/auth-profile-revisions", {
    data: {
      profile_id: "history-quality",
      revision_number: 1,
      provider_id: "e2e-v3",
      auth_mode: "subscription"
    }
  });
  expect([200, 201]).toContain(auth.status());
  const configuration = await page.request.post("/atelier/api/v1/agent-configuration-revisions", {
    data: {
      model: "history-quality-model",
      auth_profile_revision_hash: (await auth.json()).auth_profile_revision_hash,
      executor_revision: "immediate/v1",
      requested_capability: "headless"
    }
  });
  expect([200, 201]).toContain(configuration.status());
  const agentHash = (await configuration.json()).agent_configuration_revision_hash as string;
  await publishCheckedModelRegistry(page, "e2e-v3", "history-quality-model", agentHash);

  const revisionHash = await publishLongWorkflow(page, schemaHash);
  const orders = [];
  for (const name of ORDER_NAMES) {
    orders.push({ name, artifact_hash: await publishArtifact(page, `"${name} material"`) });
  }

  const started = await page.request.post("/atelier/api/v1/runs", {
    data: {
      workflow_format_version: 3,
      run_id: "history-quality/long-purpose",
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

  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/atelier/history");
  const row = page.locator(".history-row").filter({ hasText: LONG_WORKFLOW_NAME }).first();
  await expect(row).toBeVisible();
  await expect(row.locator(".row-purpose")).toHaveText(LONG_WORKFLOW_NAME);
  for (const name of ORDER_NAMES) {
    await expect(row.locator(".row-purpose")).not.toContainText(name);
  }
  await expect(page.locator(".history-head-row")).toBeHidden();

  const documentWidth = await page.evaluate(() => document.documentElement.scrollWidth);
  const viewportWidth = await page.evaluate(() => document.documentElement.clientWidth);
  expect(documentWidth).toBeLessThanOrEqual(viewportWidth);

  await page.screenshot({
    path: testInfo.outputPath("history-390-long-purpose.png"),
    fullPage: true
  });
});
