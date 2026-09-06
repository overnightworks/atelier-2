import { expect, test, type Page } from "@playwright/test";
import { existsSync, mkdirSync } from "node:fs";
import { resolve } from "node:path";

import { runPageCopy } from "../../src/lib/runPageCopy";
import { nodeAriaName } from "../../src/lib/stateMarkCopy";

/**
 * #666 Log tab against the blessed picture frame `#v8-14-run-log`.
 *
 * Mockup shots (gitignored, taken from that frame):
 *   test-results/log-666/mockup-{1280,390}-{light,dark}.png
 * Result shots of this journey, same viewports and themes, of the same
 * room (workshop rail + stage), so the artifacts are comparable:
 *   test-results/log-666/result-{1280,390}-{light,dark}.png
 */
test.setTimeout(180_000);

const LOG_TAB_INSTRUCTION_MARK = "atelier2-e2e-log-tab-canary-666";
const PLANTED_CANARY = "sk-ant" + "-plantedcanarysecret0123456789";
const frontendRoot = resolve(import.meta.dirname, "../..");
const shotDir = resolve(frontendRoot, "test-results/log-666");
const mockupHtml = resolve(
  frontendRoot,
  "../docs/requirements/0003-ziel-ui-mockup-v8.html"
);

const widths = [
  { name: "1280", width: 1280, height: 900 },
  { name: "390", width: 390, height: 844 }
] as const;
const themes = ["light", "dark"] as const;

async function publishSchema(page: Page, document: string): Promise<string> {
  const published = await page.request.post("/atelier/api/v1/schema-revisions", {
    headers: { "content-type": "application/json" },
    data: document
  });
  expect([200, 201]).toContain(published.status());
  return (await published.json()).schema_revision_hash as string;
}

function declaredOutput(schemaHash: string, name = "result"): string[] {
  return [
    "    outputs:",
    `      - name: ${name}`,
    "        schema:",
    `          ref: ${name}-schema`,
    `          revision: ${schemaHash}`
  ];
}

async function publishCheckedRegistryEntry(
  page: Page,
  providerId: string,
  modelId: string,
  configurationHash: string
): Promise<void> {
  const current = await page.request.get(`/atelier/api/v1/model-registries/${providerId}`);
  const currentRegistry =
    current.status() === 200
      ? ((await current.json()) as {
          revision_number: number;
          entries: Array<{ model_id: string; agent_configuration_revision_hash: string }>;
        })
      : null;
  if (currentRegistry === null) expect(current.status()).toBe(404);
  const existingEntries = (currentRegistry?.entries ?? [])
    .filter(
      (entry) =>
        entry.agent_configuration_revision_hash !== configurationHash && entry.model_id !== modelId
    )
    .map((entry) => ({
      model_id: entry.model_id,
      agent_configuration_revision_hash: entry.agent_configuration_revision_hash
    }));
  const registry = await page.request.put(`/atelier/api/v1/model-registries/${providerId}`, {
    data: {
      revision_number: (currentRegistry?.revision_number ?? 0) + 1,
      entries: [
        ...existingEntries,
        {
          model_id: modelId,
          agent_configuration_revision_hash: configurationHash
        }
      ]
    }
  });
  expect([200, 201]).toContain(registry.status());
  const checked = await page.request.post(
    `/atelier/api/v1/model-registries/${providerId}/validations`,
    { data: { agent_configuration_revision_hash: configurationHash } }
  );
  expect([200, 201]).toContain(checked.status());
}

async function shootMockupFrame(page: Page): Promise<void> {
  mkdirSync(shotDir, { recursive: true });
  for (const theme of themes) {
    for (const viewport of widths) {
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      await page.emulateMedia({ colorScheme: theme });
      await page.goto(`file://${mockupHtml}`);
      await page.evaluate((nextTheme) => {
        document.documentElement.setAttribute("data-theme", nextTheme);
      }, theme);
      const frame = page.locator("#v8-14-run-log");
      await frame.waitFor({ state: "visible" });
      await frame.screenshot({ path: `${shotDir}/mockup-${viewport.name}-${theme}.png` });
    }
  }
  await page.emulateMedia({ colorScheme: "light" });
  await page.setViewportSize({ width: 1280, height: 900 });
}

async function shootRunFrame(page: Page, name: string): Promise<void> {
  const frame = page.locator(".workshop");
  for (const theme of themes) {
    await page.emulateMedia({ colorScheme: theme });
    for (const viewport of widths) {
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      await frame.screenshot({
        path: `${shotDir}/${name}-${viewport.name}-${theme}.png`
      });
    }
  }
  await page.emulateMedia({ colorScheme: "light" });
  await page.setViewportSize({ width: 1280, height: 900 });
}

test("the Log tab shows the stored transcript, redacts the canary, and photographs both widths", async ({
  page
}) => {
  await shootMockupFrame(page);

  const stamp = `${Date.now()}-${test.info().repeatEachIndex}`;
  const runId = `v3/log-tab-${stamp}`;
  const api = "/atelier/api/v1";
  const schemaHash = await publishSchema(page, "true");
  const workflowYaml = [
    "format_version: 3",
    "name: Sweep the docs",
    "nodes:",
    "  - id: planner",
    "    type: agent",
    "    role: builder",
    "    mode: headless",
    "    instruction: Name the work.",
    ...declaredOutput(schemaHash),
    "  - id: builder",
    "    type: agent",
    "    role: builder",
    "    mode: headless",
    "    instruction: Do the one thing this chain is for.",
    "    depends_on: [planner]",
    ...declaredOutput(schemaHash),
    "  - id: reviewer",
    "    type: agent",
    "    role: builder",
    "    mode: headless",
    `    instruction: Check the changed files against the review brief. ${LOG_TAB_INSTRUCTION_MARK}`,
    "    depends_on: [builder]",
    ...declaredOutput(schemaHash),
    ""
  ].join("\n");

  const published = await page.request.post(`${api}/workflow-revisions`, {
    headers: { "content-type": "application/yaml" },
    data: workflowYaml
  });
  expect([200, 201]).toContain(published.status());
  const revisionHash = (await published.json()).workflow_revision_hash as string;

  const auth = await page.request.post(`${api}/auth-profile-revisions`, {
    data: {
      profile_id: `log-tab-${stamp}`,
      revision_number: 1,
      provider_id: "e2e-v3",
      auth_mode: "subscription"
    }
  });
  expect(auth.status(), await auth.text()).toBe(201);
  const configuration = await page.request.post(`${api}/agent-configuration-revisions`, {
    data: {
      model: `log-tab-${stamp}`,
      auth_profile_revision_hash: (await auth.json()).auth_profile_revision_hash,
      executor_revision: "immediate/v1",
      requested_capability: "headless"
    }
  });
  expect(configuration.status(), await configuration.text()).toBe(201);
  const configurationHash = (await configuration.json())
    .agent_configuration_revision_hash as string;
  await publishCheckedRegistryEntry(page, "e2e-v3", `log-tab-${stamp}`, configurationHash);

  const started = await page.request.post(`${api}/runs`, {
    data: {
      workflow_format_version: 2,
      run_id: runId,
      workflow_revision_hash: revisionHash,
      agent_bindings: [
        {
          role: "builder",
          agent_configuration_revision_hash: configurationHash
        }
      ]
    }
  });
  expect(started.status(), await started.text()).toBe(201);
  const publicReference = (await started.json()).public_run_reference as string;

  await expect(async () => {
    const read = await page.request.get(`${api}/runs/${publicReference}`);
    expect(read.status()).toBe(200);
    const body = await read.json();
    expect(body.state).toBe("FAILED");
    expect(body.current_node_id).toBe("reviewer");
  }).toPass({ timeout: 20_000 });

  const nodeResponse = await page.request.get(`${api}/runs/${publicReference}/nodes/reviewer`);
  expect(nodeResponse.status()).toBe(200);
  const served = await nodeResponse.text();
  expect(served).not.toContain(PLANTED_CANARY);
  const nodeBody = JSON.parse(served) as {
    transcript?: { events: Array<{ event: string; text?: string; redacted?: boolean }> };
  };
  expect(nodeBody.transcript).toBeTruthy();
  const stdoutEvent = nodeBody.transcript?.events.find(
    (event) => event.event === "unrecognised-provider-output"
  );
  expect(stdoutEvent).toMatchObject({
    event: "unrecognised-provider-output",
    redacted: true
  });
  expect(stdoutEvent?.text).toContain("[redacted]");
  expect(stdoutEvent?.text).not.toContain(PLANTED_CANARY);
  expect(stdoutEvent?.text).toContain("checking changed files");

  await page.goto(`/atelier/runs/${publicReference}`);
  await expect(page.getByRole("heading", { level: 1, name: "Sweep the docs" })).toBeVisible({
    timeout: 30_000
  });

  await page.getByRole("button", { name: nodeAriaName("reviewer", "failed") }).click();
  const panel = page.getByRole("complementary");
  await expect(panel.getByRole("heading", { name: "reviewer" })).toBeVisible();

  const tablist = panel.getByRole("tablist", { name: runPageCopy.tabsLabel });
  await panel.getByRole("tab", { name: runPageCopy.tabLog }).click();

  const transcript = panel.getByRole("region", { name: runPageCopy.transcriptRegion });
  await expect(transcript).toBeVisible();
  await expect(transcript.getByText(runPageCopy.assistantTurn).first()).toBeVisible();
  await expect(
    transcript.getByText("I will check the changed files against the review brief.")
  ).toBeVisible();
  await expect(transcript.getByText(runPageCopy.doorCall).first()).toBeVisible();
  await expect(transcript.getByText("Read", { exact: true })).toBeVisible();
  await expect(transcript.getByText(runPageCopy.doorAnswer).first()).toBeVisible();
  await expect(transcript.getByText("Read 128 lines.")).toBeVisible();
  await expect(transcript.getByText(runPageCopy.usage)).toBeVisible();
  await expect(transcript.getByText(/12,400 input/)).toBeVisible();
  await expect(transcript.getByText(/680 output tokens/)).toBeVisible();
  await expect(transcript.getByText(runPageCopy.attemptStdout)).toBeVisible();
  await expect(transcript.getByText("Failed").first()).toBeVisible();
  await expect(transcript.getByText(runPageCopy.redacted)).toBeVisible();
  await expect(page.getByText(PLANTED_CANARY)).toHaveCount(0);
  await expect(transcript.getByText(/checking changed files/)).toBeVisible();

  const folded = transcript.locator("details").first();
  await expect(folded).not.toHaveAttribute("open");
  await folded.locator("summary").press("Enter");
  await expect(
    transcript.getByText('{"path":"docs/requirements/0003-ziel-ui.md"}')
  ).toBeVisible();
  await folded.locator("summary").press("Enter");
  await expect(folded).not.toHaveAttribute("open");

  await page.setViewportSize({ width: 390, height: 844 });
  const tabMetrics = await tablist.evaluate((element) => ({
    scrollWidth: element.scrollWidth,
    clientWidth: element.clientWidth
  }));
  expect(tabMetrics.scrollWidth).toBeLessThanOrEqual(tabMetrics.clientWidth);
  const evidence = tablist.getByRole("tab", { name: runPageCopy.tabEvidence });
  await expect(evidence).toBeVisible();
  const evidenceBox = await evidence.boundingBox();
  const listBox = await tablist.boundingBox();
  expect(evidenceBox).not.toBeNull();
  expect(listBox).not.toBeNull();
  expect(evidenceBox?.width ?? 0).toBeGreaterThan(40);
  expect((evidenceBox?.x ?? 0) + (evidenceBox?.width ?? 0)).toBeLessThanOrEqual(
    (listBox?.x ?? 0) + (listBox?.width ?? 0) + 1
  );
  await page.setViewportSize({ width: 1280, height: 900 });

  await shootRunFrame(page, "result");

  for (const theme of themes) {
    for (const viewport of widths) {
      const mockup = `${shotDir}/mockup-${viewport.name}-${theme}.png`;
      const result = `${shotDir}/result-${viewport.name}-${theme}.png`;
      expect(existsSync(result), `missing result shot ${result}`).toBe(true);
      expect(existsSync(mockup), `missing mockup shot ${mockup}`).toBe(true);
    }
  }
});
