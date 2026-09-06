import { expect, test, type Page } from "@playwright/test";

import { catalogPageCopy, workflowStartCopy } from "../../src/lib/catalogPageCopy";
import { historyPageCopy } from "../../src/lib/historyPageCopy";
import { THE_ONE_PROJECT } from "../../src/lib/project";
import { runPageCopy } from "../../src/lib/runPageCopy";
import { seatCopy } from "../../src/lib/seatCopy";
import { standingWords } from "../../src/lib/runState";
import { stateLabels } from "../../src/lib/stateMarkCopy";
import { workbenchPageCopy } from "../../src/lib/workbenchPageCopy";

const shotDir = process.env.ATELIER2_SHOT_DIR ?? "";

/*
 * The mockup-comparison screenshots of every surface, at both widths and in
 * both themes.
 *
 * Not a gate itself: the evidence run `scripts/check_screenshot_review.py`
 * (REQ-UIQ-11) needs the operator to look at. Wired into CI's frontend job,
 * which sets ATELIER2_SHOT_DIR and uploads the result as a build artifact;
 * locally it is skipped unless that variable names where the images go.
 */
test.skip(shotDir === "", "no shot directory named");

// Not a gate but a sitting: two themes, two widths, every surface, and two
// runs staged live. It is allowed to take as long as that honestly takes.
test.setTimeout(300_000);

const widths = [
  { name: "1280", width: 1280, height: 900 },
  { name: "390", width: 390, height: 844 }
] as const;

/** Light and dark are skinned with one care, so both are photographed. */
const themes = ["light", "dark"] as const;

async function shoot(page: Page, name: string): Promise<void> {
  const workshop = page.locator(".workshop");
  for (const theme of themes) {
    await page.emulateMedia({ colorScheme: theme });
    for (const viewport of widths) {
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      await expect(workshop).toHaveJSProperty("offsetWidth", viewport.width);
      await page.screenshot({
        path: `${shotDir}/${theme}/${name}-${viewport.name}.png`,
        fullPage: true
      });
    }
  }
  await page.emulateMedia({ colorScheme: "light" });
}

async function anyJsonSchema(page: Page): Promise<string> {
  const published = await page.request.post("/atelier/api/v1/schema-revisions", {
    headers: { "content-type": "application/json" },
    data: '{"$schema":"https://json-schema.org/draft/2020-12/schema"}'
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

async function immediateAgent(page: Page): Promise<string> {
  const auth = await page.request.post("/atelier/api/v1/auth-profile-revisions", {
    data: {
      profile_id: "shots",
      revision_number: 1,
      provider_id: "e2e-v3",
      auth_mode: "subscription"
    }
  });
  expect([200, 201]).toContain(auth.status());
  const configuration = await page.request.post("/atelier/api/v1/agent-configuration-revisions", {
    data: {
      model: "shot-model",
      auth_profile_revision_hash: (await auth.json()).auth_profile_revision_hash,
      executor_revision: "immediate/v1",
      requested_capability: "headless"
    }
  });
  expect([200, 201]).toContain(configuration.status());
  const configurationHash = (await configuration.json()).agent_configuration_revision_hash as string;
  await publishCheckedModelRegistry(page, "e2e-v3", "shot-model", configurationHash);
  return configurationHash;
}

/**
 * A run that fails, and one that is still working, so the evidence series can
 * show the two states a calm room is judged on: brick that is unmistakably
 * not clay, and the blue that means something is actually running.
 */
async function agentOf(
  page: Page,
  profileId: string,
  providerId: string,
  executorRevision: string
): Promise<string> {
  const auth = await page.request.post("/atelier/api/v1/auth-profile-revisions", {
    data: { profile_id: profileId, revision_number: 1, provider_id: providerId, auth_mode: "subscription" }
  });
  expect([200, 201]).toContain(auth.status());
  const configuration = await page.request.post("/atelier/api/v1/agent-configuration-revisions", {
    data: {
      model: "shot-model",
      auth_profile_revision_hash: (await auth.json()).auth_profile_revision_hash,
      executor_revision: executorRevision,
      requested_capability: "headless"
    }
  });
  expect([200, 201]).toContain(configuration.status());
  const configurationHash = (await configuration.json()).agent_configuration_revision_hash as string;
  await publishCheckedModelRegistry(page, providerId, "shot-model", configurationHash);
  return configurationHash;
}

async function chainOf(page: Page, name: string, schemaHash: string, nodeIds: readonly string[]): Promise<string> {
  const lines = ["format_version: 3", `name: ${name}`, "nodes:"];
  nodeIds.forEach((nodeId, index) => {
    lines.push(
      `  - id: ${nodeId}`,
      "    type: agent",
      "    role: builder",
      "    mode: headless",
      `    instruction: Do the ${nodeId} step.`,
      ...(index === 0 ? [] : [`    depends_on: [${nodeIds[index - 1]}]`]),
      `    outputs: [{name: ${nodeId}_result, schema: {ref: any, revision: ${schemaHash}}}]`
    );
  });
  const published = await page.request.post("/atelier/api/v1/workflow-revisions", {
    headers: { "content-type": "application/yaml" },
    data: `${lines.join("\n")}\n`
  });
  expect(published.status()).toBe(201);
  return (await published.json()).workflow_revision_hash as string;
}

async function startRun(page: Page, runId: string, revisionHash: string, agentHash: string): Promise<string> {
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
  return (await started.json()).public_run_reference as string;
}

async function runReaches(page: Page, reference: string, state: string): Promise<void> {
  await expect(async () => {
    const read = await page.request.get(`/atelier/api/v1/runs/${reference}`);
    expect((await read.json()).state).toBe(state);
  }).toPass({ timeout: 30_000 });
}

test("captures every surface at both widths", async ({ page }) => {
  const schemaHash = await anyJsonSchema(page);
  const agentHash = await immediateAgent(page);

  // The Workbench as a cold server serves it: the terminal, and whatever the
  // harness's own baseline is already carrying above it. Not the empty room --
  // that baseline always holds its two reconciliation fixtures, so the empty
  // room's own card is proven where it can be staged, in
  // `frontend/tests/app/workbenchPage.test.ts` (REQ-UI-24).
  await page.goto("/atelier/chat");
  await expect(page.getByRole("heading", { name: workbenchPageCopy.title })).toBeVisible();
  await expect(page.getByRole("region", { name: seatCopy.regionLabel })).toBeVisible();
  await shoot(page, "workbench-cold");

  const iterate = await page.request.post("/atelier/api/v1/workflow-revisions", {
    headers: { "content-type": "application/yaml" },
    data: [
      "format_version: 3",
      "name: iterate-code",
      "description: build → review → fix, until green",
      "nodes:",
      "  - id: build",
      "    type: agent",
      "    role: builder",
      "    mode: headless",
      "    instruction: Build the candidate.",
      `    outputs: [{name: draft, schema: {ref: any, revision: ${schemaHash}}}]`,
      "  - id: review",
      "    type: agent",
      "    role: builder",
      "    mode: headless",
      "    instruction: Review the candidate.",
      "    depends_on: [build]",
      `    outputs: [{name: verdict, schema: {ref: any, revision: ${schemaHash}}}]`,
      "  - id: gate",
      "    type: wait",
      "    prompt: The review is green. Merge this, or name the blocking defect.",
      "    depends_on: [review]",
      `    outputs: [{name: decision, schema: {ref: any, revision: ${schemaHash}}}]`,
      ""
    ].join("\n")
  });
  expect(iterate.status()).toBe(201);
  const iterateHash = (await iterate.json()).workflow_revision_hash as string;
  expect(
    (
      await page.request.post("/atelier/api/v1/catalog-lineages", {
        data: {
          kind: "workflow",
          catalog_revision_hash: iterateHash,
          actor: "shots",
          activated_at: "2026-08-23T00:00:00Z"
        }
      })
    ).status()
  ).toBe(201);

  const started = await page.request.post("/atelier/api/v1/runs", {
    data: {
      workflow_format_version: 3,
      run_id: "demo/waiting-gate",
      workflow_revision_hash: iterateHash,
      agent_bindings: [{ role: "builder", agent_configuration_revision_hash: agentHash }],
      orders: []
    }
  });
  expect(started.status()).toBe(201);
  const reference = (await started.json()).public_run_reference as string;

  await expect(async () => {
    const read = await page.request.get(`/atelier/api/v1/runs/${reference}`);
    expect((await read.json()).state).toBe("WAITING_INPUT");
  }).toPass({ timeout: 20_000 });

  await page.goto("/atelier/chat");
  await expect(page.getByRole("heading", { name: workbenchPageCopy.title })).toBeVisible();
  // The waiting run staged above is pinned here: the decision that needs a
  // person, held in the open-decisions region so it never scrolls away (#580).
  await expect(page.getByRole("heading", { name: "The review is green. Merge this, or name the blocking defect." })).toBeVisible();
  // The terminal stands below the stage, framed by the room that neither
  // reads nor keeps what is in it (#1099).
  await expect(page.getByRole("region", { name: seatCopy.regionLabel })).toBeVisible();
  await shoot(page, "workbench-needs-you");

  await page.goto("/atelier/catalog/iterate-code");
  await expect(page.getByRole("heading", { level: 1, name: "iterate-code" })).toBeVisible();
  await shoot(page, "workflow-detail");

  await page.getByRole("button", { name: catalogPageCopy.start }).click();
  await expect(
    page.getByRole("heading", { name: workflowStartCopy.startTitle("iterate-code") })
  ).toBeVisible();
  await shoot(page, "workflow-start-sheet");
  await page.getByRole("button", { name: workflowStartCopy.cancel }).first().click();

  await page.goto("/atelier/catalog");
  await expect(
    page.getByRole("heading", { level: 1, name: catalogPageCopy.title })
  ).toBeVisible();
  await shoot(page, "catalog");

  await page.goto(`/atelier/runs/${reference}`);
  await expect(page.getByLabel(runPageCopy.whereThisRunStands)).toContainText(standingWords.waiting);
  await shoot(page, "run-waiting");

  await page.getByRole("button", { name: /build/ }).click();
  await expect(page.getByRole("tablist")).toBeVisible();
  await shoot(page, "run-node-tabs");
  await page.getByRole("tab", { name: runPageCopy.tabEvidence }).click();
  await shoot(page, "run-node-evidence");

  await page.goto(`/atelier/runs/${reference}`);
  await page.getByLabel(runPageCopy.answerLabel).fill("merge it");
  await page.getByRole("button", { name: runPageCopy.answerSubmit }).click();
  await expect(async () => {
    const read = await page.request.get(`/atelier/api/v1/runs/${reference}`);
    expect((await read.json()).state).toBe("COMPLETED");
  }).toPass({ timeout: 20_000 });
  await page.goto(`/atelier/runs/${reference}`);
  await expect(page.getByLabel(runPageCopy.whereThisRunStands)).toContainText(standingWords.done);
  await shoot(page, "run-answered");

  // A run that its own contract stopped: the agent answers prose where the
  // node declared an object, so nothing writes a success and the run fails.
  const strictSchema = await page.request.post("/atelier/api/v1/schema-revisions", {
    headers: { "content-type": "application/json" },
    data: '{"type": "object"}'
  });
  expect([200, 201]).toContain(strictSchema.status());
  const strictHash = (await strictSchema.json()).schema_revision_hash as string;
  const failing = await chainOf(page, "publish-the-release", strictHash, ["implement", "review"]);
  const failedReference = await startRun(page, "demo/failed-contract", failing, agentHash);
  await runReaches(page, failedReference, "FAILED");
  await page.goto(`/atelier/runs/${failedReference}`);
  await expect(page.getByLabel(runPageCopy.whereThisRunStands)).toContainText(standingWords.failed);
  await shoot(page, "run-failed");

  // A run that is still working: the delayed executor holds each node long
  // enough for the whole series to be photographed while it runs.
  const slowAgent = await agentOf(page, "shots-slow", "e2e-v3-slow", "delayed/v1");
  const running = await chainOf(page, "rebuild-the-index", schemaHash, [
    "gather", "compare", "rewrite", "verify"
  ]);
  const runningReference = await startRun(page, "demo/still-running", running, slowAgent);
  await page.goto(`/atelier/runs/${runningReference}`);
  await expect(
    page.getByRole("button", { name: new RegExp(`${stateLabels.working}$`) })
  ).toBeVisible({ timeout: 20_000 });
  await shoot(page, "run-running");

  await page.goto("/atelier");
  await expect(page.getByRole("heading", { name: workbenchPageCopy.title })).toBeVisible();
  // Clay, blue and quiet ink on one shelf: what waits and what moves, judged
  // against each other in the room that owns both.
  await shoot(page, "workbench-populated");

  await page.goto("/atelier/history");
  await expect(page.getByRole("heading", { name: historyPageCopy.title })).toBeVisible();
  await shoot(page, "history");

  await page.goto("/atelier/settings");
  await expect(page.getByRole("heading", { name: THE_ONE_PROJECT })).toBeVisible();
  await shoot(page, "project");
});
