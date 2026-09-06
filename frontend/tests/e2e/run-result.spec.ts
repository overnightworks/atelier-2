import { expect, test, type Page } from "@playwright/test";
import { copyFileSync, existsSync, mkdirSync } from "node:fs";
import { resolve } from "node:path";

import { runPageCopy } from "../../src/lib/runPageCopy";
import { nodeAriaName } from "../../src/lib/stateMarkCopy";
import { runResultCopy } from "../../src/lib/runResultCopy";
import { standingWords } from "../../src/lib/runState";
import { workflowGraphCopy } from "../../src/lib/workflowGraphCopy";

const api = "/atelier/api/v1";

/**
 * #666 Result tab against the ruling that the run head is the one standing
 * sentence and the node's Result tab carries the decoded declared output
 * (blessed frame `#v8-14-run-log` in `docs/requirements/0003-ziel-ui-mockup-v8.html`).
 *
 * Result shots of this journey, same viewports and themes as `run-log.spec.ts`,
 * of the same room (workshop rail + stage):
 *   test-results/result-666/head-{1280,390}-{light,dark}.png
 *   test-results/result-666/result-{1280,390}-{light,dark}.png
 *
 * The vector is the exact one the issue was filed against: the harness's fake
 * conductor executor (`/__e2e/seed-conductor` in `tests/e2e/serve_cockpit.py`)
 * answers with `CONDUCTOR_REPORT_SCHEMA`'s own shape -- the same declared
 * object the bug report's screenshot showed printed as a raw JSON line. It
 * answers the one node of the run below, which is also that run's sink, so
 * this journey is the duplicate-answer case; the node panel's ordinary
 * rendering of a non-sink node's own answer is proven at the component level
 * in `tests/app/readableResultDisplay.test.ts`.
 *
 * `/__e2e/seed-conductor` durably mutates the one shared harness server's
 * state for every spec that runs after it, so this spec owns the state it
 * drives instead of depending on file order: it resets that server to its
 * cold-boot baseline and seeds its own conductor executor (below), whatever
 * another spec already did to the shared server.
 */
const CONDUCTOR_FAKE_ANSWER =
  "Nothing started: the workbench probe only asked for an answer.";
// The exact bytes `json.dumps` in `tests/e2e/serve_cockpit.py` wrote -- its
// default separators, not a compact re-serialization this page invents.
const CONDUCTOR_FAKE_REPORT_RAW = `{"answer": "${CONDUCTOR_FAKE_ANSWER}", "started_run_ids": [], "carried_context": "The workbench probe asked only for an answer.", "carried_context_truncated": false}`;

const frontendRoot = resolve(import.meta.dirname, "../..");
const shotDir = resolve(frontendRoot, "test-results/result-666");

const widths = [
  { name: "1280", width: 1280, height: 900 },
  { name: "390", width: 390, height: 844 }
] as const;
const themes = ["light", "dark"] as const;

async function shoot(page: Page, name: string): Promise<void> {
  mkdirSync(shotDir, { recursive: true });
  const frame = page.locator(".workshop");
  for (const theme of themes) {
    await page.emulateMedia({ colorScheme: theme });
    for (const viewport of widths) {
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      const artifact = test.info().outputPath(`${name}-${theme}-${viewport.name}.png`);
      await frame.screenshot({ path: artifact });
      copyFileSync(artifact, `${shotDir}/${name}-${viewport.name}-${theme}.png`);
    }
  }
  await page.emulateMedia({ colorScheme: "light" });
  await page.setViewportSize({ width: 1280, height: 900 });
}

/**
 * Opens the run page of a settled run whose one node answered with the
 * conductor's own report bytes.
 *
 * The live conductor conversation cannot carry this journey: its loop re-enters
 * `next_message` the moment `conduct` has answered, and a run's node rail is
 * fenced to the round the run stands in
 * (`atelier2.application.project_node_rail`), so a waiting conversation reads
 * "next_message — Needs you, conduct — Queued" and offers no settled node to
 * open. The bytes under test stay the production ones all the same: the run
 * below binds the seeded conductor's own fake executor, which answers every
 * node it runs with `CONDUCTOR_FAKE_REPORT`, to a one-node workflow that ends
 * when that node does.
 */
async function settledConductorReportRun(page: Page): Promise<void> {
  // This suite shares one server across every spec file and across
  // `--repeat-each` (#742): a previous seed of the same conductor catalog
  // would conflict on the model registry. Reset to the cold-boot baseline,
  // then seed, so this journey owns its own conductor executor.
  await resetToKnownStore(page);
  const seeded = await page.request.post("/__e2e/seed-conductor");
  expect(seeded.ok(), await seeded.text()).toBeTruthy();
  const configurationHash = ((await seeded.json()) as { configuration_hash: string })
    .configuration_hash;

  const workflowYaml = [
    "format_version: 3",
    "name: One conductor report",
    "nodes:",
    "  - id: conduct",
    "    type: agent",
    "    role: conductor",
    // The seeded conductor executor is the doors-capable one, and its doors are
    // a tool-bearing call, so a `headless` node could never redeem it.
    "    mode: headless_with_tools",
    "    instruction: Answer with the report this journey is filed against.",
    ...declaredOutput(await anyJsonSchema(page)),
    ""
  ].join("\n");
  const published = await page.request.post(`${api}/workflow-revisions`, {
    headers: { "content-type": "application/yaml" },
    data: workflowYaml
  });
  expect(published.status(), await published.text()).toBe(201);

  const started = await page.request.post(`${api}/runs`, {
    data: {
      workflow_format_version: 3,
      run_id: "e2e/conductor-report",
      workflow_revision_hash: (await published.json()).workflow_revision_hash,
      agent_bindings: [
        { role: "conductor", agent_configuration_revision_hash: configurationHash }
      ],
      // A version-3 start declares its orders, empty ones included: without the
      // field the body is no start resource at all and dies in wire validation
      // (`StartRunRequestResourceV3`, `api/wire/requests.py`). It comes back
      // titled "invalid agent bindings" because that is how the handler reads
      // a refused start body mentioning bindings (`api/problems.py`) -- the
      // title is not a verdict on the bindings above.
      orders: []
    }
  });
  expect(started.status(), await started.text()).toBe(201);
  const publicRunReference = (await started.json()).public_run_reference as string;

  await expect(async () => {
    const read = await page.request.get(`${api}/runs/${publicRunReference}`);
    expect(read.status()).toBe(200);
    expect((await read.json()).state).toBe("COMPLETED");
  }).toPass({ timeout: 30_000 });
  await page.goto(`/atelier/runs/${publicRunReference}`);
}

async function resetToKnownStore(page: Page): Promise<void> {
  // This suite shares one server across every spec file (#742): reset to the
  // cold-boot baseline before starting a run whose rail this test must own.
  const reset = await page.request.post("/__e2e/recompose?reset=true");
  expect(reset.status()).toBe(202);
  const expectedGeneration = await reset.text();
  await expect(async () => {
    expect(await (await page.request.get("/__e2e/generation")).text()).toBe(expectedGeneration);
  }).toPass({ timeout: 20_000 });
}

// Every executable V3 agent node declares one output and the schema it must
// satisfy, so the test pins the schema that admits any JSON value unless it
// needs a node to refuse the provider's string output.
async function publishSchema(page: Page, document: string): Promise<string> {
  const published = await page.request.post("/atelier/api/v1/schema-revisions", {
    headers: { "content-type": "application/json" },
    data: document
  });
  expect([200, 201]).toContain(published.status());
  return (await published.json()).schema_revision_hash as string;
}

function anyJsonSchema(page: Page): Promise<string> {
  return publishSchema(page, "true");
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

async function startV3Line(
  page: Page,
  runId: string,
  name: string,
  implementSchemaHash: string,
  reviewSchemaHash: string
): Promise<{ public_run_reference: string }> {
  const workflowYaml = [
    "format_version: 3",
    `name: ${name}`,
    "nodes:",
    "  - id: implement",
    "    type: agent",
    "    role: builder",
    "    mode: headless",
    "    instruction: Do the one thing this chain is for.",
    ...declaredOutput(implementSchemaHash),
    "  - id: review",
    "    type: agent",
    "    role: builder",
    "    mode: headless",
    "    instruction: Check what the node before you did.",
    "    depends_on: [implement]",
    ...declaredOutput(reviewSchemaHash),
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
  expect(started.status(), await started.text()).toBe(201);
  const createdRun = await started.json();
  expect(createdRun.workflow_format_version).toBe(3);
  return { public_run_reference: createdRun.public_run_reference as string };
}

test("the Result tab carries the decoded result; the run head is only the standing sentence", async ({
  page
}) => {
  test.setTimeout(120_000);

  await settledConductorReportRun(page);
  await expect(page.getByLabel(runPageCopy.whereThisRunStands)).toContainText(standingWords.done, {
    timeout: 30_000
  });

  await expect(page.locator("#run-outcome")).toHaveCount(0);
  await expect(page.getByRole("region", { name: runPageCopy.tabResult })).toHaveCount(0);
  await expect(page.getByText(CONDUCTOR_FAKE_ANSWER, { exact: true })).toHaveCount(0);
  await shoot(page, "head");

  await page.getByRole("button", { name: nodeAriaName("conduct", "succeeded") }).click();
  const panel = page.getByRole("complementary");
  await expect(panel.getByRole("heading", { name: "conduct" })).toBeVisible();
  await expect(panel.getByRole("tab", { name: runPageCopy.tabResult })).toHaveAttribute(
    "aria-selected",
    "true"
  );
  await expect(panel.getByText(CONDUCTOR_FAKE_ANSWER, { exact: true })).toBeVisible();
  const exactFold = panel.locator("details").filter({ hasText: runResultCopy.exactText });
  await expect(exactFold).toBeVisible();
  await expect(exactFold).not.toHaveAttribute("open");
  await expect(panel.getByText(CONDUCTOR_FAKE_REPORT_RAW)).not.toBeVisible();
  await expect(panel.getByRole("link", { name: "Shown above" })).toHaveCount(0);
  await expect(page.locator("#run-outcome")).toHaveCount(0);
  await shoot(page, "result");

  for (const theme of themes) {
    for (const viewport of widths) {
      for (const name of ["head", "result"] as const) {
        const stable = `${shotDir}/${name}-${viewport.name}-${theme}.png`;
        expect(existsSync(stable), `missing result shot ${stable}`).toBe(true);
      }
    }
  }
});

test("proves(a-finished-run-can-be-started-again-from-a-node): forks a finished V3 run from a later node and shows what is carried over", async ({
  page
}) => {
  test.setTimeout(180_000);
  const fork = runPageCopy.fork;
  await page.setViewportSize({ width: 1280, height: 900 });
  await resetToKnownStore(page);

  const schemaHash = await anyJsonSchema(page);
  const started = await startV3Line(
    page,
    "v3/fork-completed",
    "Two agents in a line",
    schemaHash,
    schemaHash
  );
  const originReference = started.public_run_reference;

  await expect(async () => {
    const read = await page.request.get(`${api}/runs/${originReference}`);
    expect(read.status()).toBe(200);
    const body = await read.json();
    expect(body.state).toBe("COMPLETED");
    expect(body.node_rail.map((entry: { node_id: string }) => entry.node_id)).toEqual([
      "implement",
      "review"
    ]);
  }).toPass({ timeout: 15_000 });

  await page.goto(`/atelier/runs/${originReference}`);
  const graph = page.getByRole("region", { name: workflowGraphCopy.label });
  await graph.getByRole("button", { name: nodeAriaName("review", "succeeded") }).click();
  const panel = page.getByRole("complementary");
  await expect(panel.getByRole("heading", { name: "review" })).toBeVisible();

  await panel.getByRole("button", { name: fork.retryHere }).click();
  const sheet = page.getByRole("dialog", { name: fork.sheetLabel });
  await expect(sheet).toBeVisible();
  expect(await sheet.evaluate((element) => (element as HTMLDialogElement).matches(":modal"))).toBe(
    true
  );
  await expect(sheet.getByRole("heading", { name: fork.confirmTitle("review") })).toBeVisible();
  await expect(sheet.getByText(fork.carriedOver).locator("..")).toContainText("implement");
  await expect(sheet.getByText(fork.runsAgain).locator("..")).toContainText("review");
  await expect(sheet.getByText(fork.deferralSentence)).toBeVisible();

  await sheet.getByRole("button", { name: fork.startAgain }).click();
  await expect(page).not.toHaveURL(new RegExp(`${originReference}$`));
  const successorReference = new URL(page.url()).pathname.split("/").pop() ?? "";
  expect(successorReference.startsWith("run1.")).toBe(true);
  expect(successorReference).not.toBe(originReference);
  await expect(page.getByText(/Fork of /)).toBeVisible();

  const origin = await page.request.get(`${api}/runs/${originReference}`);
  expect(origin.ok()).toBeTruthy();
  const originBody = (await origin.json()) as {
    public_run_reference: string;
    fork_successors?: Array<{ public_run_reference: string }>;
  };
  expect(originBody.public_run_reference).toBe(originReference);
  expect(
    originBody.fork_successors?.some(
      (row) => row.public_run_reference === successorReference
    )
  ).toBe(true);
});

test("proves(a-failed-run-can-be-started-again-from-the-failed-node): the failed step restarts the run and reuses only the prefix before it", async ({
  page
}) => {
  test.setTimeout(180_000);
  const fork = runPageCopy.fork;
  await page.setViewportSize({ width: 1280, height: 900 });
  await resetToKnownStore(page);

  const implementSchemaHash = await anyJsonSchema(page);
  const reviewSchemaHash = await publishSchema(page, '{"type":"object"}');
  const started = await startV3Line(
    page,
    "v3/fork-failed",
    "A line that fails review",
    implementSchemaHash,
    reviewSchemaHash
  );
  const originReference = started.public_run_reference;

  await expect(async () => {
    const read = await page.request.get(`${api}/runs/${originReference}`);
    expect(read.status()).toBe(200);
    const body = await read.json();
    expect(body.state).toBe("FAILED");
    expect(
      body.node_rail.map((entry: { node_id: string; state: string }) => ({
        node_id: entry.node_id,
        state: entry.state
      }))
    ).toEqual([
      { node_id: "implement", state: "succeeded" },
      { node_id: "review", state: "failed" }
    ]);
  }).toPass({ timeout: 15_000 });

  await page.goto(`/atelier/runs/${originReference}`);
  const panel = page.getByRole("complementary");
  await expect(panel.getByRole("heading", { name: "review" })).toBeVisible();

  await panel.getByRole("button", { name: fork.retryHere }).click();
  const sheet = page.getByRole("dialog", { name: fork.sheetLabel });
  await expect(sheet).toBeVisible();
  expect(await sheet.evaluate((element) => (element as HTMLDialogElement).matches(":modal"))).toBe(
    true
  );
  await sheet.getByRole("button", { name: fork.startAgain }).click();

  await expect(page).not.toHaveURL(new RegExp(`${originReference}$`));
  const successorReference = new URL(page.url()).pathname.split("/").pop() ?? "";
  expect(successorReference.startsWith("run1.")).toBe(true);
  expect(successorReference).not.toBe(originReference);

  await expect(async () => {
    const read = await page.request.get(`${api}/runs/${successorReference}`);
    expect(read.status()).toBe(200);
    const body = await read.json();
    const implement = body.node_rail.find((entry: { node_id: string }) => entry.node_id === "implement");
    expect(implement?.reused_from_run_reference ?? null).toBe(originReference);
    const review = body.node_rail.find((entry: { node_id: string }) => entry.node_id === "review");
    expect(review?.reused_from_run_reference ?? null).toBeNull();
  }).toPass({ timeout: 15_000 });
});
