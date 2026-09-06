import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { expect, test, type Locator, type Page } from "@playwright/test";
import { z } from "zod";

import { nodeDetailSchema } from "../../src/api/client";
import { observedWorkItemLabel, workflowStartCopy } from "../../src/lib/catalogPageCopy";
import { decodeUtf8Base64 } from "../../src/lib/exactBytes";
import { WORK_ITEM_ORDER_SCHEMA_REVISION } from "../../src/lib/orderSchema";

const VIEWPORTS = [
  { width: 390, height: 844 },
  { width: 1280, height: 900 }
] as const;

// #1130 fix round finding 6: the committed `workflows/diff-review.yaml` pins
// both `diff` and `review_questions` as plain strings (`nonempty_string.json`)
// -- the exact bytes an operator's Git-source import would publish, read from
// this repository rather than rebuilt inline.
const WORKFLOWS_DIRECTORY = resolve(import.meta.dirname, "..", "..", "..", "workflows");
const REAL_DIFF_REVIEW_DOCUMENT = readFileSync(
  resolve(WORKFLOWS_DIRECTORY, "diff-review.yaml"),
  "utf8"
);
const REAL_NONEMPTY_STRING_SCHEMA = readFileSync(
  resolve(WORKFLOWS_DIRECTORY, "schemas", "nonempty_string.json"),
  "utf8"
);
const REAL_DIFF_REVIEW_FINDING_SCHEMA = readFileSync(
  resolve(WORKFLOWS_DIRECTORY, "schemas", "diff_review_finding.json"),
  "utf8"
);

const workItemSchemaDocument =
  '{"$schema":"https://json-schema.org/draft/2020-12/schema","additionalProperties":false,"properties":{"body":{"type":"string"},"change_marker":{"maxLength":1024,"minLength":1,"type":"string"},"digest":{"pattern":"^[0-9a-f]{64}$","type":"string"},"kind":{"enum":["issue","change_request"],"type":"string"},"observed_at":{"pattern":"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$","type":"string"},"reference":{"maxLength":1024,"minLength":1,"type":"string"},"scope":{"items":{"type":"string"},"type":"array","uniqueItems":true}},"required":["body","change_marker","digest","kind","observed_at","reference","scope"],"title":"work item","type":"object"}';
const observedWorkItemBody = "e2e observed work item gh:450 — Grüße 東京";
const workItemPickerName = `${workflowStartCopy.workItem} for work_item`;
// The tracker title the harness seeds for gh:450 (tests/e2e/serve_cockpit.py
// _E2E_TRACKER_ITEM_TITLE) -- the picker option now carries it beside the
// reference (#1030).
const observedWorkItemTitle = "e2e observed work item gh:450";
const observedWorkItemRevision = {
  body: observedWorkItemBody,
  change_marker: "e2e-etag-gh-450",
  digest: createHash("sha256").update(observedWorkItemBody, "utf8").digest("hex"),
  kind: "issue",
  observed_at: "2026-08-26T09:15:00Z",
  reference: "gh:450"
} as const;
const workItemOrderValueSchema = z
  .object({
    body: z.string(),
    change_marker: z.string().min(1),
    digest: z.string().regex(/^[0-9a-f]{64}$/),
    kind: z.enum(["issue", "change_request"]),
    observed_at: z.string().regex(/^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$/),
    reference: z.string().min(1)
  })
  .strict();

async function pickWorkItem(sheet: Locator, optionLabel: string): Promise<void> {
  const picker = sheet.getByRole("combobox", { name: workItemPickerName });
  await picker.focus();
  await picker.press("ArrowDown");
  const listbox = sheet.getByRole("listbox", { name: workItemPickerName });
  await expect(listbox).toBeVisible();
  // Once open, real focus and the combobox's `aria-activedescendant` both
  // move to the filter field (REQ-UIQ-05) -- the button keeps its own
  // combobox role for the closed state, but the field is now the live one.
  const filterField = sheet.getByLabel(workflowStartCopy.filterWorkItemsLabel);
  const option = sheet.getByRole("option", { name: optionLabel, exact: true });
  await expect(option).toBeVisible();
  const optionId = await option.getAttribute("id");
  expect(optionId).toBeTruthy();
  while ((await filterField.getAttribute("aria-activedescendant")) !== optionId) {
    const before = await filterField.getAttribute("aria-activedescendant");
    await filterField.press("ArrowDown");
    expect(await filterField.getAttribute("aria-activedescendant")).not.toBe(before);
  }
  await filterField.press("Enter");
  await expect(listbox).toHaveCount(0);
  await expect(picker).toContainText(optionLabel);
}

function observedWorkItemRevisionFromJob(job: string): z.infer<typeof workItemOrderValueSchema> {
  for (const block of job.split("\n\n")) {
    if (!block.startsWith("{")) continue;
    try {
      return workItemOrderValueSchema.parse(JSON.parse(block));
    } catch {
      continue;
    }
  }
  throw new Error("the node job did not carry an observed work item revision");
}

async function readObservedWorkItemRevision(
  page: Page,
  publicRef: string,
  nodeId: string
): Promise<z.infer<typeof workItemOrderValueSchema> | null> {
  const response = await page.request.get(`/atelier/api/v1/runs/${publicRef}/nodes/${nodeId}`);
  if (!response.ok()) return null;
  const detail = nodeDetailSchema.parse(await response.json());
  if (detail.node_id !== nodeId) {
    throw new Error("The node response named another node.");
  }
  if (detail.job_base64 === null || detail.job_base64.length === 0) return null;
  const job = decodeUtf8Base64(detail.job_base64);
  if (job === null) throw new Error("The node job was not UTF-8.");
  return observedWorkItemRevisionFromJob(job);
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
    .filter((entry) => entry.agent_configuration_revision_hash !== configurationHash)
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

async function publishStartableConfiguration(page: Page, token: string): Promise<{
  agentConfigurationRevisionHash: string;
  authProfileRevisionHash: string;
}> {
  const profileId = `start-sheet-e2e-${token}`;
  const modelId = `start-sheet-model-${token}`;
  const auth = await page.request.post("/atelier/api/v1/auth-profile-revisions", {
    data: {
      profile_id: profileId,
      revision_number: 1,
      provider_id: "e2e-v3",
      auth_mode: "subscription"
    }
  });
  expect(auth.status()).toBe(201);
  const authProfileRevisionHash = (await auth.json()).auth_profile_revision_hash as string;
  const configuration = await page.request.post("/atelier/api/v1/agent-configuration-revisions", {
    data: {
      model: modelId,
      auth_profile_revision_hash: authProfileRevisionHash,
      executor_revision: "immediate/v1",
      requested_capability: "headless"
    }
  });
  expect(configuration.status()).toBe(201);
  const agentConfigurationRevisionHash =
    (await configuration.json()).agent_configuration_revision_hash as string;
  await publishCheckedRegistryEntry(
    page,
    "e2e-v3",
    modelId,
    agentConfigurationRevisionHash
  );
  return {
    agentConfigurationRevisionHash,
    authProfileRevisionHash
  };
}

test("proves(a-v3-workflow-is-started-from-the-picker) proves(the-work-item-picker-filters-by-number-and-title-and-says-when-nothing-matches): starts an admitted Catalog workflow with its observed work item at 390 and 1280", async ({ page }, testInfo) => {
  test.setTimeout(240_000);
  for (const viewport of VIEWPORTS) {
    await page.setViewportSize(viewport);
    const token = `${viewport.width}-${testInfo.repeatEachIndex}`;
    const workflowName = `start-sheet-work-item-e2e-${token}`;
    const profileId = `start-sheet-e2e-${token}`;
    const workItemSchema = await page.request.post("/atelier/api/v1/schema-revisions", {
      headers: { "content-type": "application/json" },
      data: workItemSchemaDocument
    });
    expect([200, 201]).toContain(workItemSchema.status());
    const workItemSchemaHash = (await workItemSchema.json()).schema_revision_hash as string;
    expect(workItemSchemaHash).toBe(WORK_ITEM_ORDER_SCHEMA_REVISION);
    const configuration = await publishStartableConfiguration(page, token);
    const outputSchema = await page.request.post("/atelier/api/v1/schema-revisions", {
      headers: { "content-type": "application/json" },
      data: "true"
    });
    expect([200, 201]).toContain(outputSchema.status());
    const outputSchemaHash = (await outputSchema.json()).schema_revision_hash as string;
    const published = await page.request.post("/atelier/api/v1/workflow-revisions", {
      headers: { "content-type": "application/yaml" },
      data: [
        "format_version: 3",
        `name: ${workflowName}`,
        "graph_inputs:",
        "  - name: work_item",
        "    schema:",
        "      ref: work-item",
        `      revision: ${workItemSchemaHash}`,
        "nodes:",
        "  - id: build",
        "    type: agent",
        "    role: builder",
        "    mode: headless",
        "    instruction: Build the selected work item.",
        "    inputs:",
        "      - name: work_item",
        "        from:",
        "          graph_input: work_item",
        "    outputs:",
        "      - name: result",
        "        schema:",
        "          ref: result-schema",
        `          revision: ${outputSchemaHash}`,
        ""
      ].join("\n")
    });
    expect(published.status()).toBe(201);
    const workflowRevisionHash = (await published.json()).workflow_revision_hash as string;
    const admitted = await page.request.post("/atelier/api/v1/catalog-lineages", {
      data: {
        kind: "workflow",
        catalog_revision_hash: workflowRevisionHash,
        actor: profileId,
        activated_at: "2026-08-26T00:00:00Z"
      }
    });
    expect(admitted.status()).toBe(201);

    await page.goto(`/atelier/catalog/${workflowName}`);
    const opener = page.getByRole("button", { name: "Start" });
    await opener.click();
    const sheet = page.getByRole("dialog", { name: workflowStartCopy.startTitle(workflowName) });
    await expect(sheet).toBeVisible();
    const workItem = sheet.getByRole("combobox", { name: workItemPickerName });
    await expect(sheet.getByRole("button", { name: workflowStartCopy.cancel })).toBeFocused();
    await expect(workItem).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(sheet).toHaveCount(0);
    await expect(opener).toBeFocused();
    await opener.click();
    await expect(sheet).toBeVisible();
    await workItem.focus();
    await page.keyboard.press("ArrowDown");
    await expect(sheet.getByRole("listbox", { name: workItemPickerName })).toBeVisible();
    const optionLabel = observedWorkItemLabel("#450", observedWorkItemTitle);
    const filterField = sheet.getByLabel(workflowStartCopy.filterWorkItemsLabel);
    await expect(filterField).toBeVisible();
    await filterField.fill("#999");
    await expect(sheet.getByText(workflowStartCopy.noWorkItemMatch("#999"))).toBeVisible();
    await expect(sheet.getByRole("option", { name: optionLabel })).toHaveCount(0);
    await filterField.fill("#450");
    await expect(sheet.getByRole("option", { name: optionLabel })).toBeVisible();
    await filterField.fill(observedWorkItemTitle.split(" ")[1]!.toUpperCase());
    await expect(sheet.getByRole("option", { name: optionLabel })).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(sheet.getByRole("listbox", { name: workItemPickerName })).toHaveCount(0);
    await expect(sheet).toBeVisible();
    await expect(workItem).toBeFocused();
    await pickWorkItem(sheet, optionLabel);
    const roleConfiguration = sheet.getByLabel(workflowStartCopy.configurationFor("builder"));
    await roleConfiguration.selectOption(configuration.agentConfigurationRevisionHash);
    await expect(sheet.getByText("Chosen now", { exact: true })).toBeVisible();

    const startRun = sheet.getByRole("button", { name: workflowStartCopy.startRun });
    await expect(startRun).toBeEnabled();
    await startRun.click();
    await expect(page).toHaveURL(/\/atelier\/runs\//);
    await expect(page.getByRole("heading", { name: workflowName })).toBeVisible();
    const publicRef = new URL(page.url()).pathname.split("/").at(-1);
    expect(publicRef).toBeTruthy();
    await expect.poll(async () => {
      return await readObservedWorkItemRevision(page, publicRef!, "build");
    }, { timeout: 15_000 }).toEqual(observedWorkItemRevision);
  }
});

test("starts a diff-review-shaped workflow with review_questions typed as text and diff as an object order's Raw JSON, both through the real artifact door, and sends orders on the wire as {name, artifact_hash} (#438 Scheibe 1b)", async ({ page }, testInfo) => {
  test.setTimeout(240_000);
  const token = `${testInfo.repeatEachIndex}-${Date.now()}`;
  const workflowName = `diff-review-e2e-${token}`;
  const profileId = `diff-review-e2e-${token}`;
  const reviewQuestionsText =
    "Does the retry stay idempotent when the second call arrives first?";
  const diffRawJson = '{"file": "src/atelier2/api/routes/runs.py"}';

  const reviewQuestionsSchema = await page.request.post("/atelier/api/v1/schema-revisions", {
    headers: { "content-type": "application/json" },
    data: '{"$schema":"https://json-schema.org/draft/2020-12/schema","type":"string","minLength":1}'
  });
  expect([200, 201]).toContain(reviewQuestionsSchema.status());
  const reviewQuestionsSchemaHash =
    (await reviewQuestionsSchema.json()).schema_revision_hash as string;

  const diffSchema = await page.request.post("/atelier/api/v1/schema-revisions", {
    headers: { "content-type": "application/json" },
    data:
      '{"$schema":"https://json-schema.org/draft/2020-12/schema","type":"object",' +
      '"required":["file"],"properties":{"file":{"type":"string"}},' +
      '"additionalProperties":false}'
  });
  expect([200, 201]).toContain(diffSchema.status());
  const diffSchemaHash = (await diffSchema.json()).schema_revision_hash as string;

  const configuration = await publishStartableConfiguration(page, token);
  const outputSchema = await page.request.post("/atelier/api/v1/schema-revisions", {
    headers: { "content-type": "application/json" },
    data: "true"
  });
  expect([200, 201]).toContain(outputSchema.status());
  const outputSchemaHash = (await outputSchema.json()).schema_revision_hash as string;

  const published = await page.request.post("/atelier/api/v1/workflow-revisions", {
    headers: { "content-type": "application/yaml" },
    data: [
      "format_version: 3",
      `name: ${workflowName}`,
      "graph_inputs:",
      "  - name: review_questions",
      "    schema:",
      "      ref: nonempty-string",
      `      revision: ${reviewQuestionsSchemaHash}`,
      "  - name: diff",
      "    schema:",
      "      ref: diff-object",
      `      revision: ${diffSchemaHash}`,
      "nodes:",
      "  - id: review",
      "    type: agent",
      "    role: reviewer",
      "    mode: headless",
      "    instruction: Review the diff using the review questions.",
      "    inputs:",
      "      - name: review_questions",
      "        from:",
      "          graph_input: review_questions",
      "      - name: diff",
      "        from:",
      "          graph_input: diff",
      "    outputs:",
      "      - name: result",
      "        schema:",
      "          ref: result-schema",
      `          revision: ${outputSchemaHash}`,
      ""
    ].join("\n")
  });
  expect(published.status()).toBe(201);
  const workflowRevisionHash = (await published.json()).workflow_revision_hash as string;
  const admitted = await page.request.post("/atelier/api/v1/catalog-lineages", {
    data: {
      kind: "workflow",
      catalog_revision_hash: workflowRevisionHash,
      actor: profileId,
      activated_at: "2026-09-04T00:00:00Z"
    }
  });
  expect(admitted.status()).toBe(201);

  await page.goto(`/atelier/catalog/${workflowName}`);
  await page.getByRole("button", { name: "Start" }).click();
  const sheet = page.getByRole("dialog", { name: workflowStartCopy.startTitle(workflowName) });
  await expect(sheet).toBeVisible();

  const reviewQuestionsGroup = sheet.getByRole("group", { name: "Order review_questions" });
  await reviewQuestionsGroup.getByLabel(workflowStartCopy.orderText).fill(reviewQuestionsText);

  const diffGroup = sheet.getByRole("group", { name: "Order diff" });
  await diffGroup.getByText(workflowStartCopy.rawJson, { exact: true }).click();
  await diffGroup.getByLabel(workflowStartCopy.rawJsonFor("diff")).fill(diffRawJson);

  const roleConfiguration = sheet.getByLabel(workflowStartCopy.configurationFor("reviewer"));
  await roleConfiguration.selectOption(configuration.agentConfigurationRevisionHash);
  await expect(sheet.getByText("Chosen now", { exact: true })).toBeVisible();

  // #1130 fix round finding 5: prove the wire shape itself, not only the
  // resulting node job -- each non-work-item order publishes its own
  // artifact, and the start request names it only by hash, never by value.
  const artifactRequests: string[] = [];
  page.on("request", (request) => {
    if (request.method() === "POST" && new URL(request.url()).pathname === "/atelier/api/v1/artifacts") {
      artifactRequests.push(request.url());
    }
  });

  const startRun = sheet.getByRole("button", { name: workflowStartCopy.startRun });
  await expect(startRun).toBeEnabled();
  const runsRequest = page.waitForRequest(
    (request) =>
      request.method() === "POST" && new URL(request.url()).pathname === "/atelier/api/v1/runs"
  );
  await startRun.click();
  const runsBody = (await runsRequest).postDataJSON() as {
    orders: ReadonlyArray<Record<string, unknown>>;
  };
  expect(runsBody.orders).toEqual([
    { name: "review_questions", artifact_hash: expect.any(String) },
    { name: "diff", artifact_hash: expect.any(String) }
  ]);
  for (const order of runsBody.orders) expect(order).not.toHaveProperty("value");
  expect(artifactRequests).toHaveLength(2);

  await expect(page).toHaveURL(/\/atelier\/runs\//);
  const publicRef = new URL(page.url()).pathname.split("/").at(-1);
  expect(publicRef).toBeTruthy();

  let job: string | null = null;
  await expect.poll(async () => {
    const response = await page.request.get(`/atelier/api/v1/runs/${publicRef}/nodes/review`);
    if (!response.ok()) return null;
    const detail = nodeDetailSchema.parse(await response.json());
    if (detail.job_base64 === null || detail.job_base64.length === 0) return null;
    job = decodeUtf8Base64(detail.job_base64);
    return job;
  }, { timeout: 15_000 }).not.toBeNull();
  expect(job).toContain(`--- order: review_questions ---\n\n${reviewQuestionsText}`);
  expect(job).toContain(`--- order: diff ---\n\n${diffRawJson}`);
});

test("starts the real diff-review revision from the catalog with review_questions and diff both typed as text (#1130 Done-when)", async ({ page }, testInfo) => {
  test.setTimeout(240_000);
  const token = `${testInfo.repeatEachIndex}-${Date.now()}`;
  const profileId = `diff-review-real-e2e-${token}`;
  const reviewQuestionsText = "Does this diff leave an obsolete schema reference?";
  const diffText =
    "diff --git a/workflows/diff-review.yaml b/workflows/diff-review.yaml\n+one guard clause added";

  const nonemptyStringSchema = await page.request.post("/atelier/api/v1/schema-revisions", {
    headers: { "content-type": "application/json" },
    data: REAL_NONEMPTY_STRING_SCHEMA
  });
  expect([200, 201]).toContain(nonemptyStringSchema.status());

  const findingSchema = await page.request.post("/atelier/api/v1/schema-revisions", {
    headers: { "content-type": "application/json" },
    data: REAL_DIFF_REVIEW_FINDING_SCHEMA
  });
  expect([200, 201]).toContain(findingSchema.status());

  const configuration = await publishStartableConfiguration(page, token);

  // The exact committed document, published and admitted through the same
  // doors an operator's Git-source import uses -- not a hand-typed rebuild
  // of its shape (#1130 Done-when).
  const published = await page.request.post("/atelier/api/v1/workflow-revisions", {
    headers: { "content-type": "application/yaml" },
    data: REAL_DIFF_REVIEW_DOCUMENT
  });
  expect(published.status()).toBe(201);
  const workflowRevisionHash = (await published.json()).workflow_revision_hash as string;
  const admitted = await page.request.post("/atelier/api/v1/catalog-lineages", {
    data: {
      kind: "workflow",
      catalog_revision_hash: workflowRevisionHash,
      actor: profileId,
      activated_at: "2026-09-04T00:00:00Z"
    }
  });
  expect(admitted.status()).toBe(201);

  await page.goto("/atelier/catalog/diff-review");
  await page.getByRole("button", { name: "Start" }).click();
  const sheet = page.getByRole("dialog", { name: workflowStartCopy.startTitle("diff-review") });
  await expect(sheet).toBeVisible();

  const reviewQuestionsGroup = sheet.getByRole("group", { name: "Order review_questions" });
  await expect(reviewQuestionsGroup.getByText(workflowStartCopy.rawJson)).toHaveCount(0);
  await reviewQuestionsGroup.getByLabel(workflowStartCopy.orderText).fill(reviewQuestionsText);

  const diffGroup = sheet.getByRole("group", { name: "Order diff" });
  await expect(diffGroup.getByText(workflowStartCopy.rawJson)).toHaveCount(0);
  await diffGroup.getByLabel(workflowStartCopy.orderText).fill(diffText);

  const roleConfiguration = sheet.getByLabel(workflowStartCopy.configurationFor("reviewer"));
  await roleConfiguration.selectOption(configuration.agentConfigurationRevisionHash);
  await expect(sheet.getByText("Chosen now", { exact: true })).toBeVisible();

  const startRun = sheet.getByRole("button", { name: workflowStartCopy.startRun });
  await expect(startRun).toBeEnabled();
  await startRun.click();
  await expect(page).toHaveURL(/\/atelier\/runs\//);
  const publicRef = new URL(page.url()).pathname.split("/").at(-1);
  expect(publicRef).toBeTruthy();

  let job: string | null = null;
  await expect.poll(async () => {
    const response = await page.request.get(`/atelier/api/v1/runs/${publicRef}/nodes/review`);
    if (!response.ok()) return null;
    const detail = nodeDetailSchema.parse(await response.json());
    if (detail.job_base64 === null || detail.job_base64.length === 0) return null;
    job = decodeUtf8Base64(detail.job_base64);
    return job;
  }, { timeout: 15_000 }).not.toBeNull();
  expect(job).toContain(`--- order: review_questions ---\n\n${reviewQuestionsText}`);
  expect(job).toContain(`--- order: diff ---\n\n${diffText}`);
});
