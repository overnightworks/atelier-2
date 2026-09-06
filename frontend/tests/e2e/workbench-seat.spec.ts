import { expect, test, type Page } from "@playwright/test";

import { seatCopy } from "../../src/lib/seatCopy";
import { workbenchPageCopy } from "../../src/lib/workbenchPageCopy";

/**
 * The Workbench as the operator meets it (#1099): the stage above, and his own
 * terminal where the web chat used to be.
 *
 * The terminal in this run is the harness's own fixture page, not a real ttyd
 * on a real tmux session -- the pipeline's runner carries neither binary
 * (`tests/e2e/serve_cockpit.py` says so at startup, and
 * `tests/integration/test_terminal_seat_live.py` drives the real ones where
 * they exist). What is proven here is the room: the frame and whose seat it
 * is, the address a reload reattaches to, the refusal below the readable
 * width, and the stage standing unchanged beside all of it.
 */
const WIDE = { width: 1280, height: 900 };
const NARROW = { width: 390, height: 844 };

const shotDir = process.env.ATELIER2_SHOT_DIR ?? "";

async function openWorkbench(page: Page): Promise<void> {
  await page.goto("/atelier/chat");
  await expect(page.getByRole("heading", { name: workbenchPageCopy.title })).toBeVisible();
}

async function photographSeat(page: Page, name: string): Promise<void> {
  const path =
    shotDir === "" ? test.info().outputPath(`${name}.png`) : `${shotDir}/${name}.png`;
  await page.screenshot({ path, fullPage: true });
}

/** The line the fixture terminal prints, marking one living session. */
async function seatSessionLine(page: Page): Promise<string> {
  const terminal = page.frameLocator(".seat-terminal").locator("#terminal");
  await expect(terminal).toContainText("$ claude");
  return (await terminal.textContent()) ?? "";
}

test("the workbench seats the operator at a terminal, and a reload reattaches to the same session (#1099 lines 1, 2, 3, 5, 7, 13, 21)", async ({
  page
}) => {
  await page.setViewportSize(WIDE);
  await openWorkbench(page);

  const seat = page.getByRole("region", { name: seatCopy.regionLabel });
  await expect(seat).toBeVisible();
  // Whose seat this is, and what it may do, both readable without searching.
  await expect(seat.getByText(seatCopy.attachment)).toBeVisible();
  await expect(seat.getByText(seatCopy.trustBoundary)).toBeVisible();
  // One terminal, this project's, and no second one beside it.
  await expect(page.locator(".seat-terminal")).toHaveCount(1);
  const session = await seatSessionLine(page);

  // Nothing of the web chat is left: no composer, no transcript, no send.
  await expect(page.locator("form.composer")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Send" })).toHaveCount(0);
  await expect(page.locator(".conversation")).toHaveCount(0);
  await photographSeat(page, "workbench-seat-1280");

  await page.reload();
  await expect(page.getByRole("heading", { name: workbenchPageCopy.title })).toBeVisible();
  expect(await seatSessionLine(page)).toBe(session);
});

// Not #1099 line 8: this run is started through the API beside the seat, not
// typed into the terminal through the atelier's MCP door. That half of the
// line is deferred to the operator's live proof (#1099); what stands here is
// the cockpit half -- a run that arrives while the terminal is on screen is
// an ordinary row, and the seat neither swallows it nor marks it.
test("a run that arrives while the seat is on screen is an ordinary row beside it", async ({
  page
}) => {
  await page.setViewportSize(WIDE);
  await openWorkbench(page);
  await expect(page.locator(".seat-terminal")).toHaveCount(1);

  const runId = `workbench/seat-started-${Date.now()}`;
  const schema = await page.request.post("/atelier/api/v1/schema-revisions", {
    headers: { "content-type": "application/json" },
    data: '{"type":"boolean"}'
  });
  expect([200, 201]).toContain(schema.status());
  const schemaRevisionHash = (await schema.json()).schema_revision_hash as string;
  const workflow = await page.request.post("/atelier/api/v1/workflow-revisions", {
    headers: { "content-type": "application/yaml" },
    data: [
      "format_version: 3",
      "name: Started beside the seat",
      "nodes:",
      "  - id: ask",
      "    type: wait",
      "    prompt: Did this run arrive on the shelf?",
      `    outputs: [{name: answer, schema: {ref: decision, revision: ${schemaRevisionHash}}}]`,
      ""
    ].join("\n")
  });
  expect(workflow.status()).toBe(201);
  const started = await page.request.post("/atelier/api/v1/runs", {
    data: {
      workflow_format_version: 3,
      run_id: runId,
      workflow_revision_hash: (await workflow.json()).workflow_revision_hash as string,
      agent_bindings: [],
      orders: []
    }
  });
  expect(started.status()).toBe(201);

  // The same row any other run gets: no second sort of run, and no reload.
  await expect(
    page.getByRole("heading", { name: "Did this run arrive on the shelf?" })
  ).toBeVisible({ timeout: 20_000 });
  await expect(page.locator(".seat-terminal")).toHaveCount(1);
});

test("a terminal that stopped answering leaves one refusal with the way out, and the room working (#1099 lines 9, 10, 11)", async ({
  page
}) => {
  const missing = await page.request.post("/__e2e/seat-terminal?state=missing");
  expect(missing.ok()).toBeTruthy();
  try {
    await page.setViewportSize(WIDE);
    await openWorkbench(page);

    const seat = page.getByRole("region", { name: seatCopy.regionLabel });
    await expect(seat.getByText(seatCopy.unreachableTitle)).toBeVisible();
    await expect(seat.getByText(seatCopy.unreachableDetail)).toBeVisible();
    await expect(page.locator(".seat-terminal")).toHaveCount(0);
    // The refusal takes nothing else down with it, and it is one sentence in
    // one place -- never a second copy of itself.
    await expect(page.getByRole("heading", { name: workbenchPageCopy.title })).toBeVisible();
    await expect(seat.getByText(seatCopy.unreachableTitle)).toHaveCount(1);
    await expect(seat.getByText(seatCopy.trustBoundary)).toBeVisible();
  } finally {
    const answering = await page.request.post("/__e2e/seat-terminal?state=answering");
    expect(answering.ok()).toBeTruthy();
  }
});

test.describe("on a phone", () => {
  // A phone is a place the operator works from, so the terminal opens there
  // too (operator ruling 06.09.: "Terminal geht nicht auf dem Phone? Das muss
  // gehen"). Touch is on, because reaching the terminal with a finger is the
  // first half of that.
  test.use({ viewport: NARROW, hasTouch: true });

  test("the terminal takes the phone's whole width, and a tap types into it (#1099 lines 2, 5, 13, 21 and the 06.09. ruling)", async ({
    page
  }) => {
    await openWorkbench(page);

    const terminal = page.locator(".seat-terminal");
    await expect(terminal).toHaveCount(1);
    // The type size a phone can read, asked of ttyd's own client through the
    // address it reads its options from.
    expect(await terminal.getAttribute("src")).toContain("fontSize=12");
    const frame = await terminal.boundingBox();
    expect(frame?.width).toBe(NARROW.width);

    await seatSessionLine(page);
    const prompt = page.frameLocator(".seat-terminal").locator("#prompt");
    await prompt.tap();
    await prompt.fill("list_workflows");
    await prompt.press("Enter");

    await expect(page.frameLocator(".seat-terminal").locator("#terminal")).toContainText(
      "> list_workflows"
    );
    await photographSeat(page, "workbench-seat-390");
  });
});

test("proves(a-decision-opens-on-the-workbench-while-you-watch): a decision that opens while you watch appears at 1280 and 390 without a reload", async ({
  page
}) => {
  test.setTimeout(120_000);

  const reset = await page.request.post("/__e2e/recompose?reset=true");
  expect(reset.status()).toBe(202);
  const expectedGeneration = await reset.text();
  await expect(async () => {
    expect(await (await page.request.get("/__e2e/generation")).text()).toBe(expectedGeneration);
  }).toPass({ timeout: 20_000 });

  const schema = await page.request.post("/atelier/api/v1/schema-revisions", {
    headers: { "content-type": "application/json" },
    data: '{"type":"boolean"}'
  });
  expect([200, 201]).toContain(schema.status());
  const schemaRevisionHash = (await schema.json()).schema_revision_hash as string;
  const question = "May this wait open while you watch?";
  const runId = "workbench/live-attention-while-watching";

  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto("/atelier/chat");
  await expect(page.getByRole("heading", { name: workbenchPageCopy.title })).toBeVisible();
  const card = page.locator(".pinned-decision").filter({ hasText: question });
  await expect(card).toHaveCount(0);
  const openedUrl = page.url();

  const workflow = await page.request.post("/atelier/api/v1/workflow-revisions", {
    headers: { "content-type": "application/yaml" },
    data: [
      "format_version: 3",
      "name: Opened while watching",
      "nodes:",
      "  - id: ask",
      "    type: wait",
      `    prompt: ${question}`,
      `    outputs: [{name: answer, schema: {ref: decision, revision: ${schemaRevisionHash}}}]`,
      ""
    ].join("\n")
  });
  expect(workflow.status()).toBe(201);
  const started = await page.request.post("/atelier/api/v1/runs", {
    data: {
      workflow_format_version: 3,
      run_id: runId,
      workflow_revision_hash: (await workflow.json()).workflow_revision_hash as string,
      agent_bindings: [],
      orders: []
    }
  });
  expect(started.status()).toBe(201);

  await expect(card).toBeVisible({ timeout: 20_000 });
  expect(page.url()).toBe(openedUrl);

  await page.setViewportSize({ width: 390, height: 844 });
  await expect(card).toBeVisible();
  expect(page.url()).toBe(openedUrl);
});
