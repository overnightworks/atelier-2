import { expect, test, type Locator, type Page } from "@playwright/test";

import { conductorChatCopy } from "../../src/lib/conductorChatCopy";
import { conductorConversationCopy } from "../../src/lib/conductorConversation";
import { decodePublicRunReference, type RunListRow, type RunV3 } from "../../src/api/client";
import { humanProblemDetail } from "../../src/lib/humanRefusal";
import { journalPoisonedCopy } from "../../src/lib/journalPoisonedCopy";
import { runPageCopy } from "../../src/lib/runPageCopy";
import { standingWords } from "../../src/lib/runState";
import { MUTATION_JOURNAL_STORAGE_KEY } from "../../src/lib/storageKeys";
import { workbenchPageCopy } from "../../src/lib/workbenchPageCopy";
import { healthyRunListItems } from "../support/runListRows";

/**
 * The chat wire, driven as the operator drives it (#7): one served instance
 * first shows the honest "no conductor" refusal, then — after the harness
 * publishes the production conductor catalog and the fake doors-shaped
 * executor answers — a typed message becomes ONE engine run whose report
 * comes back into the same conversation.
 *
 * The reply text is `CONDUCTOR_FAKE_ANSWER` in `tests/e2e/serve_cockpit.py`,
 * asserted verbatim so the words a human reads are the proof.
 */
const CONDUCTOR_FAKE_ANSWER =
  "Nothing started: the workbench probe only asked for an answer.";

const widths = [
  { name: "1280", width: 1280, height: 900 },
  { name: "390", width: 390, height: 844 }
] as const;

const themes = ["light", "dark"] as const;

type InputFixtureRun = {
  public_run_reference: string;
  workflow_revision_hash: string;
  current_node_id: string;
};

/** Retire the harness's two cold-boot reconciliation examples. This scenario
 * proves exactly six open decisions, rather than accidentally borrowing those
 * unrelated fixture waits into the Workbench count or shelf. */
async function retireReconciliationFixtures(page: Page): Promise<void> {
  const listed = await page.request.get("/atelier/api/v1/runs?state=WAITING_RECONCILIATION&limit=50");
  expect(listed.status()).toBe(200);
  const { items: rows } = (await listed.json()) as { items: RunListRow[] };
  const items: RunV3[] = healthyRunListItems(rows);
  expect(items).toHaveLength(2);

  for (const run of items) {
    expect(run.state).toBe("WAITING_RECONCILIATION");
    const retired = await page.request.post(`/atelier/api/v1/runs/${run.public_run_reference}/reconciliations`, {
      headers: { "content-type": "application/json" },
      data: {
        command_id: `reconcile-e2e-isolation-${run.public_run_reference}`,
        // The served run resource carries no waiting block -- the question lives
        // on the durable intent, and a freshly seeded baseline intent stands
        // at its first version.
        expected_intent_state_version: 1,
        actor: "Playwright fixture isolation",
        evidence: "This six-decision scenario retires the cold-boot examples.",
        determination: { type: "operator_authoritative_absence" }
      }
    });
    expect([200, 202]).toContain(retired.status());
  }

  await expect(async () => {
    const remaining = await page.request.get("/atelier/api/v1/runs?state=WAITING_RECONCILIATION&limit=50");
    expect(remaining.status()).toBe(200);
    expect(((await remaining.json()) as { items: unknown[] }).items).toHaveLength(0);
    // 60s is a deadlock brake, not a latency contract: the resolutions advance
    // through the queue, and a loaded box pays for the whole suite before them.
  }).toPass({ timeout: 60_000 });

  // Reconciliation advances the fixtures asynchronously. Follow each captured
  // baseline run through its V1 input wait to completion, rather than merely
  // sampling the global waiting list before its worker has reached that wait.
  await expect(async () => {
    const states: string[] = [];
    for (const fixture of items) {
      const current = await page.request.get(`/atelier/api/v1/runs/${fixture.public_run_reference}`);
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
        const answered = await page.request.post(`/atelier/api/v1/runs/${run.public_run_reference}/answers`, {
          headers: { "content-type": "application/json" },
          data: {
            workflow_revision_hash: run.workflow_revision_hash,
            node_id: run.current_node_id,
            expected_node_execution_id: expectedNodeExecutionId,
            actor: "operator",
            answer_base64: "MQ=="
          }
        });
        expect(answered.status()).toBe(202);
      }
    }
    expect(states).toEqual(["COMPLETED", "COMPLETED"]);
  }).toPass({ timeout: 60_000 });
}

async function photograph(page: Page, name: string, scrollMobileMainToEnd = false): Promise<void> {
  for (const theme of themes) {
    await page.emulateMedia({ colorScheme: theme });
    for (const viewport of widths) {
      await page.setViewportSize({
        width: viewport.width,
        height: viewport.height
      });
      if (scrollMobileMainToEnd && viewport.width === 390) {
        await placeConversationAboveComposer(page);
      }
      await page.screenshot({
        path: test.info().outputPath(`${name}-${theme}-${viewport.name}.png`),
        fullPage: true
      });
    }
  }
  await page.emulateMedia({ colorScheme: "light" });
  await page.setViewportSize({ width: 1280, height: 900 });
}

/**
 * Scrolls the mobile stage to the newest line and returns only once that line
 * has come to rest. At 390 px the web fonts can still be loading, and a scroll
 * measured against a `scrollHeight` that grows afterwards leaves the newest
 * line below the composer instead of above it (CI run 33894818987). So this
 * waits for the fonts, then scrolls to the end again in each of two
 * consecutive frames until the line is painted in the same place twice.
 */
async function placeConversationAboveComposer(page: Page): Promise<void> {
  const stage = page.getByRole("main");
  const newestLine = page
    .getByRole("list", { name: workbenchPageCopy.transcriptLabel })
    .getByRole("listitem")
    .last();
  const scrollToEnd = () =>
    stage.evaluate((element) => {
      element.scrollTop = element.scrollHeight - element.clientHeight;
    });
  const placeOfNewestLineInTheNextFrame = () =>
    newestLine.evaluate(
      (line) =>
        new Promise<string>((paint) => {
          requestAnimationFrame(() => paint(JSON.stringify(line.getBoundingClientRect())));
        })
    );
  await page.evaluate(async () => {
    await document.fonts.ready;
  });
  await expect
    .poll(
      async () => {
        await scrollToEnd();
        const placeAfterTheFirstScroll = await placeOfNewestLineInTheNextFrame();
        await scrollToEnd();
        return (await placeOfNewestLineInTheNextFrame()) === placeAfterTheFirstScroll;
      },
      { timeout: 10_000 }
    )
    .toBe(true);
}

/**
 * Resets the shared server and publishes the production conductor catalog,
 * the setup every P5 driver test below starts from -- one owner instead of
 * four copies of the same two calls.
 */
async function resetAndSeedConductor(page: Page): Promise<{ workflow_revision_hash: string }> {
  const reset = await page.request.post("/__e2e/recompose?reset=true");
  expect(reset.status()).toBe(202);
  const expectedGeneration = await reset.text();
  await expect(async () => {
    expect(await (await page.request.get("/__e2e/generation")).text()).toBe(expectedGeneration);
  }).toPass({ timeout: 20_000 });
  const seeded = await page.request.post("/__e2e/seed-conductor");
  expect(seeded.ok()).toBeTruthy();
  return (await seeded.json()) as { workflow_revision_hash: string };
}

/**
 * Opens the Workbench and sends the conversation's first message over the
 * real composer, the one act every driver test below needs proven live.
 *
 * Under load the fake executor's own subprocess step -- not the browser --
 * is what can run long: a locator timeout dressed the wait for that as a
 * render wait, and a loaded box paid for both together (CI red on #1070,
 * #1073, #1074, #1086, #1087, always green on rerun -- #1093). This instead
 * waits, bounded and loud, on the harness's own fence for the round the send
 * just opened, then treats the DOM assertion as the short render proof it
 * actually is.
 */
async function startConversationOverUi(page: Page, message: string, workflowRevisionHash: string): Promise<Locator> {
  await page.goto("/atelier/chat");
  // The connection read spans several round trips (`resolveConductorConnection`,
  // conductorEpisode.ts); Send stays locked until it resolves (#1103, #1114),
  // so this waits for the connected hint -- proof the room is done reading,
  // not merely that the button happens to look clickable.
  await expect(page.getByText(conductorConversationCopy.composerHint)).toBeVisible();
  await page.getByLabel(workbenchPageCopy.composerLabel).fill(message);
  await page.getByRole("button", { name: workbenchPageCopy.send }).click();
  await waitForFreshConductorRound(page, workflowRevisionHash);
  await expect(page.getByText(CONDUCTOR_FAKE_ANSWER)).toBeVisible({ timeout: 5_000 });
  const conversationLink = page.getByRole("link", { name: conductorChatCopy.openEpisode });
  await expect(conversationLink).toBeVisible();
  return conversationLink;
}

/**
 * The run reference a "See the run" link now names.
 *
 * `route.ts` owns the same `/atelier/runs/{reference}` shape but pulls in a
 * JSON module Playwright's own test loader cannot import (Node's ESM loader
 * demands an import attribute this repository's build tooling does not add);
 * this reads the same path shape without that owner, then hands the
 * extracted text to the production reference codec so a malformed link still
 * fails loud rather than being trusted uninspected.
 */
async function conversationRunReference(conversationLink: Locator): Promise<string> {
  const href = await conversationLink.getAttribute("href");
  const match = href === null ? null : /^\/atelier\/runs\/([^/]+)$/.exec(href);
  const candidate = match?.[1];
  if (candidate === undefined || decodePublicRunReference(candidate) === null) {
    throw new Error(`The conversation link did not resolve to a run: ${href ?? "(none)"}`);
  }
  return candidate;
}

type ConductorRunHandle = {
  publicRunReference: string;
  workflowRevisionHash: string;
  currentNodeId: string;
};

async function readConductorRun(page: Page, publicRunReference: string): Promise<ConductorRunHandle> {
  const read = await page.request.get(`/atelier/api/v1/runs/${publicRunReference}`);
  expect(read.status()).toBe(200);
  const run = (await read.json()) as { workflow_revision_hash: string; current_node_id: string };
  return {
    publicRunReference,
    workflowRevisionHash: run.workflow_revision_hash,
    currentNodeId: run.current_node_id
  };
}

/**
 * Waits for the conversation's current round to settle at its wait -- the
 * same fence `retireReconciliationFixtures` polls above -- and returns the
 * execution id an answer must name to land in exactly this round.
 *
 * An accepted answer (202) only records itself as pending; the round it
 * closes and the next round's wait it opens are the *asynchronous* loop
 * advance, so the wait this fence reads back right after posting can still be
 * the very round just answered, not yet applied. `settledPast`, when given,
 * makes this fence keep polling until the id it returns differs from the one
 * a caller just answered -- the honest wait for "this round has genuinely
 * closed", never a fixed sleep standing in for it.
 */
async function currentWaitExecutionId(
  page: Page,
  publicRunReference: string,
  settledPast?: string
): Promise<string> {
  let expectedNodeExecutionId = "";
  await expect(async () => {
    const fence = await page.request.get(
      `/__e2e/current-wait-execution?public_run_reference=${encodeURIComponent(publicRunReference)}`
    );
    expect(
      fence.status(),
      `run ${publicRunReference}: the harness's next-round fence answered HTTP ${fence.status()} (no fresh WAITING_INPUT yet)`
    ).toBe(200);
    ({ expected_node_execution_id: expectedNodeExecutionId } = (await fence.json()) as {
      expected_node_execution_id: string;
    });
    if (settledPast !== undefined) {
      expect(
        expectedNodeExecutionId,
        `run ${publicRunReference}: the fence still names the already-answered round ${settledPast}`
      ).not.toBe(settledPast);
    }
  }).toPass({ timeout: 30_000 });
  return expectedNodeExecutionId;
}

/**
 * Waits, bounded and loud, for a conversation just started over the UI to
 * settle at its own fresh wait -- the run reference is not yet known to a
 * conversation that has no reply yet, so this rediscovers it on every retry
 * (the durable WAITING_INPUT list carrying this conductor revision, the same
 * lookup `retireReconciliationFixtures` already performs) and only then
 * confirms the harness's own fence for it, both inside one bounded poll so
 * this replaces the locator timeout it stands in for rather than stacking a
 * second one on top of it. On timeout the failure names the revision, the
 * run found so far (if any), and the last state this fence actually
 * observed.
 */
async function waitForFreshConductorRound(
  page: Page,
  workflowRevisionHash: string
): Promise<{ publicRunReference: string; expectedNodeExecutionId: string }> {
  let publicRunReference = "";
  let expectedNodeExecutionId = "";
  await expect(async () => {
    const waiting = await page.request.get("/atelier/api/v1/runs?state=WAITING_INPUT&limit=50");
    expect(waiting.status()).toBe(200);
    const { items: rows } = (await waiting.json()) as { items: RunListRow[] };
    const matches = healthyRunListItems(rows).filter(
      (run) => run.workflow_revision_hash === workflowRevisionHash
    );
    expect(
      matches,
      `revision ${workflowRevisionHash}: expected exactly one run waiting for input, found ${matches.length}`
    ).toHaveLength(1);
    const [match] = matches;
    if (match === undefined) throw new Error(`revision ${workflowRevisionHash}: no matching run after the length check above`);
    publicRunReference = match.public_run_reference;

    const fence = await page.request.get(
      `/__e2e/current-wait-execution?public_run_reference=${encodeURIComponent(publicRunReference)}`
    );
    expect(
      fence.status(),
      `run ${publicRunReference}: the harness's next-round fence answered HTTP ${fence.status()} (no fresh WAITING_INPUT yet)`
    ).toBe(200);
    ({ expected_node_execution_id: expectedNodeExecutionId } = (await fence.json()) as {
      expected_node_execution_id: string;
    });
  }).toPass({ timeout: 55_000 });
  return { publicRunReference, expectedNodeExecutionId };
}

/**
 * Answers the conversation's current round through the same production door
 * the composer uses (`POST .../answers`), without paying for 24 browser
 * round-trips: the fake conductor's own decode is unbilled either way (#7),
 * so what a 24-round proof needs to skip is 24 UI interactions -- typing,
 * clicking, waiting on renders -- not the round's own durable machinery.
 *
 * `settledPast` names the previous round's own execution id once a caller is
 * driving round after round of the same run: this round's wait must first be
 * confirmed open under a genuinely new id (see `currentWaitExecutionId`)
 * before it is answered, or the answer would still name the prior round.
 * Returns the execution id this call answered, so the next round in a chain
 * can require its own wait to have moved past it.
 */
async function answerConductorRoundDirectly(
  page: Page,
  run: ConductorRunHandle,
  text: string,
  settledPast?: string
): Promise<string> {
  const expectedNodeExecutionId = await currentWaitExecutionId(page, run.publicRunReference, settledPast);
  const answered = await page.request.post(`/atelier/api/v1/runs/${run.publicRunReference}/answers`, {
    headers: { "content-type": "application/json" },
    data: {
      workflow_revision_hash: run.workflowRevisionHash,
      node_id: run.currentNodeId,
      expected_node_execution_id: expectedNodeExecutionId,
      actor: "operator",
      answer_base64: Buffer.from(JSON.stringify(text)).toString("base64")
    }
  });
  expect(answered.status()).toBe(202);
  return expectedNodeExecutionId;
}

test("the composer stays honestly locked without a conductor, then starts one conversation run", async ({ page }) => {
  test.setTimeout(120_000);

  // This suite shares one server across every spec file (#742): a conductor
  // another file already seeded would still answer here. This test's own
  // first act resets the server to its cold-boot baseline -- guaranteed
  // unseeded -- instead of depending on running before `workbench-conductor`
  // in the file listing.
  const reset = await page.request.post("/__e2e/recompose?reset=true");
  expect(reset.status()).toBe(202);
  const expectedGeneration = await reset.text();
  await expect(async () => {
    expect(await (await page.request.get("/__e2e/generation")).text()).toBe(expectedGeneration);
  }).toPass({ timeout: 20_000 });

  // Before any conductor exists: the composer says so and Send is visibly
  // locked (#1103) -- nothing pretends to listen, and nothing accepts a
  // message it would silently swallow.
  await page.goto("/atelier/chat");
  await expect(page.getByRole("heading", { name: "Workbench" })).toBeVisible();
  await expect(page.getByText(workbenchPageCopy.composerHint)).toBeVisible();
  await expect(page.getByRole("button", { name: workbenchPageCopy.send })).toBeDisabled();
  await photograph(page, "workbench-not-connected");

  // The harness publishes the production conductor catalog: schemas, the
  // conductor document from its own owner and agent configuration.
  const seeded = await page.request.post("/__e2e/seed-conductor");
  expect(seeded.ok()).toBeTruthy();
  const seededConductor = (await seeded.json()) as { workflow_revision_hash: string };

  // A reload resolves the connection fresh; the composer now says a
  // conductor is connected, and the same surface carries one loop run.
  await page.reload();
  await expect(page.getByText(conductorConversationCopy.composerHint)).toBeVisible();
  await page
    .getByLabel(workbenchPageCopy.composerLabel)
    .fill("Starte nichts, antworte nur kurz.");
  await page.getByRole("button", { name: workbenchPageCopy.send }).click();

  // The first message starts exactly one loop run and becomes its first wait
  // answer; the report returns through that run's full event stream. Waits on
  // the harness's own fence first (see `startConversationOverUi`'s doc) --
  // the render check below is then a short proof, not the wait itself.
  const { publicRunReference, expectedNodeExecutionId: firstRoundExecutionId } = await waitForFreshConductorRound(
    page,
    seededConductor.workflow_revision_hash
  );
  await expect(page.getByText(CONDUCTOR_FAKE_ANSWER)).toBeVisible({ timeout: 5_000 });
  const conversationLink = page.getByRole("link", { name: conductorChatCopy.openEpisode });
  await expect(conversationLink).toBeVisible();
  await page.getByLabel(workbenchPageCopy.composerLabel).fill("Und noch eine Nachricht.");
  await page.getByRole("button", { name: workbenchPageCopy.send }).click();
  await currentWaitExecutionId(page, publicRunReference, firstRoundExecutionId);
  // Two reply lines, counted by the text they carry. Not `{ exact: true }`:
  // Playwright's exact text match compares a whole element's text, and a
  // transcript line also carries its speaker label, so an exact match finds
  // nothing at all rather than the reply (CI run 33506793797). The smallest
  // element holding the reply is the line itself, which is what makes this a
  // count of lines.
  await expect(page.getByText(CONDUCTOR_FAKE_ANSWER)).toHaveCount(2, {
    timeout: 5_000
  });
  // One run, one link: a second round adds a reply, never a second way to open
  // the same conversation.
  await expect(conversationLink).toHaveCount(1);

  // A reload re-resolves the connection and re-opens the run's durable event
  // stream, whose full history the server replays (#7): the conversation
  // survives a reload rather than starting over.
  await page.reload();
  await expect(page.getByText(CONDUCTOR_FAKE_ANSWER)).toHaveCount(2, {
    timeout: 60_000
  });

  // The reply already rendered from the run's own event stream, but this
  // list is the queue-driven projection every other WAITING_INPUT read in
  // this file barriers on (`waitForFreshConductorRound`, the bounded-decision
  // count below): it settles after the durable state, not with it, so a
  // single read here would race the same queue on a loaded box (#747).
  await expect(async () => {
    const waitingConversations = await page.request.get("/atelier/api/v1/runs?state=WAITING_INPUT&limit=50");
    expect(waitingConversations.status()).toBe(200);
    const waitingConversationRows = (await waitingConversations.json()) as { items: RunListRow[] };
    expect(
      healthyRunListItems(waitingConversationRows.items).filter(
        (run) => run.workflow_revision_hash === seededConductor.workflow_revision_hash
      )
    ).toHaveLength(1);
  }).toPass({ timeout: 20_000 });
  await photograph(page, "workbench-conductor-reply");

  // The linked run page is the reply's manual counterpart: the conversation
  // is an ordinary run anyone can open (Keine-Sonderautoritaet, #7).
  await conversationLink.click();
  await expect(page).toHaveURL(/\/atelier\/runs\/run1\./);
});

test("keeps many open decisions bounded, with one hairline and one promoted stage", async ({ page }) => {
  test.setTimeout(120_000);

  const reset = await page.request.post("/__e2e/recompose?reset=true");
  expect(reset.status()).toBe(202);
  const expectedGeneration = await reset.text();
  await expect(async () => {
    expect(await (await page.request.get("/__e2e/generation")).text()).toBe(expectedGeneration);
  }).toPass({ timeout: 20_000 });
  await retireReconciliationFixtures(page);
  // A conductor is now required to unlock Send at all (#1103): the fixture's
  // own message below needs somewhere to land, and the conductor's own run
  // never joins these six -- the pinned rail filters it out by reference
  // (WorkbenchPage.svelte).
  const seeded = await page.request.post("/__e2e/seed-conductor");
  expect(seeded.ok()).toBeTruthy();
  const seededConductor = (await seeded.json()) as { workflow_revision_hash: string };

  const schema = await page.request.post("/atelier/api/v1/schema-revisions", {
    headers: { "content-type": "application/json" },
    data: '{"type":"boolean"}'
  });
  expect([200, 201]).toContain(schema.status());
  const schemaRevisionHash = (await schema.json()).schema_revision_hash as string;

  for (let index = 1; index <= 6; index += 1) {
    const workflow = await page.request.post("/atelier/api/v1/workflow-revisions", {
      headers: { "content-type": "application/yaml" },
      data: [
        "format_version: 3",
        `name: Decision ${index}`,
        "nodes:",
        "  - id: ask",
        "    type: wait",
        `    prompt: Should decision ${index} move on?`,
        `    outputs: [{name: answer, schema: {ref: decision, revision: ${schemaRevisionHash}}}]`,
        ""
      ].join("\n")
    });
    expect(workflow.status()).toBe(201);
    const started = await page.request.post("/atelier/api/v1/runs", {
      data: {
        workflow_format_version: 3,
        run_id: `workbench/bounded-decision-${index}`,
        workflow_revision_hash: (await workflow.json()).workflow_revision_hash as string,
        agent_bindings: [],
        orders: []
      }
    });
    expect(started.status()).toBe(201);
  }

  // A 201 says the run exists, not that its wait is durable yet: each run
  // still advances through the queue to its wait node. Barrier on the durable
  // list -- the event that settles the count -- so the exact assertions below
  // read a settled store instead of racing the queue on a loaded box (#747).
  await expect(async () => {
    const waiting = await page.request.get(
      "/atelier/api/v1/runs?state=WAITING_INPUT&limit=50"
    );
    expect(waiting.status()).toBe(200);
    const { items } = (await waiting.json()) as { items: unknown[] };
    expect(items).toHaveLength(6);
  }).toPass({ timeout: 60_000 });

  await page.goto("/atelier/chat");
  await expect(page.getByRole("link", { name: "Workbench 6 needs you" })).toBeVisible({ timeout: 20_000 });
  const pinnedRegion = page.getByRole("region", {
    name: workbenchPageCopy.pinnedDecisionsLabel
  });
  const decisions = pinnedRegion.getByRole("region");
  await expect(decisions).toHaveCount(6, { timeout: 20_000 });
  const expandedDecision = pinnedRegion.locator(".pinned-decision:not(.pinned-decision-compact)");
  await expect(expandedDecision).toHaveCount(1);
  const expandedRunDoor = expandedDecision.getByRole("link", {
    name: workbenchPageCopy.openTheRun
  });
  await expect(expandedRunDoor).toBeVisible();
  const compactControls = pinnedRegion.getByRole("button", {
    name: workbenchPageCopy.answerDecision
  });
  await expect(compactControls).toHaveCount(5);

  // Each compact decision can be brought fully into the rail by its own
  // scrolling surface -- walking down to the last one and back up to the first,
  // so the rail is reachable in both directions; the already-visible expanded
  // decision owns its run door. Reachability is the contract here, which is why
  // nothing asserts that the rail scrolled at all: whether six decisions outgrow
  // its maximum height is a matter of font metrics and render timing under load,
  // and demanding it made this test fail 8 of 10 runs (#1155).
  const everyCompactControlRevealsInRail = await compactControls.evaluateAll((controls) => {
    const rail = controls[0]?.closest<HTMLElement>(".needs-you");
    if (rail === null || rail === undefined) return false;
    const revealsInRail = (control: Element) => {
      control.scrollIntoView({ block: "nearest" });
      const railBox = rail.getBoundingClientRect();
      const controlBox = control.getBoundingClientRect();
      return controlBox.top >= railBox.top && controlBox.bottom <= railBox.bottom;
    };
    const downTheRailAndBack = [...controls, ...[...controls].reverse()];
    return downTheRailAndBack.every(revealsInRail);
  });
  expect(everyCompactControlRevealsInRail).toBe(true);

  await pinnedRegion.evaluate((element) => {
    element.scrollTop = 0;
  });
  // One locator identity, resolved once: a second `.first()` call after the
  // rail re-renders could pick a different section than the one whose label
  // was just read, promoting the wrong decision and leaving this one compact
  // (CI run 33494214855, attempt 1).
  const firstCompactControl = compactControls.first();
  const promotedDecisionLabel = await firstCompactControl.locator("..").getAttribute("aria-labelledby");
  if (promotedDecisionLabel === null) throw new Error("The compact decision has no accessible label.");
  const promotedDecision = pinnedRegion.locator(`section[aria-labelledby="${promotedDecisionLabel}"]`);
  await promotedDecision.getByRole("button", { name: workbenchPageCopy.answerDecision }).click();
  await expect(expandedDecision).toHaveCount(1);
  await expect(promotedDecision).not.toHaveClass(/pinned-decision-compact/);
  await expect(promotedDecision.getByRole("link", { name: workbenchPageCopy.openTheRun })).toHaveCount(1);

  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("main").evaluate((element) => {
    element.scrollTo(0, 0);
  });
  await pinnedRegion.evaluate((element) => {
    element.scrollTop = 0;
  });
  await expect(compactControls).toHaveCount(5);
  const compactRail = await pinnedRegion.evaluate((element) => {
    const style = getComputedStyle(element);
    const rail = element.getBoundingClientRect();
    const controls = Array.from(element.querySelectorAll<HTMLElement>(".compact-answer"));
    const controlFits = (control: HTMLElement) => {
      const box = control.getBoundingClientRect();
      return box.top >= rail.top && box.bottom <= rail.bottom;
    };
    const controlFallsBeyondTheFold = (control: HTMLElement) => {
      const box = control.getBoundingClientRect();
      return box.top >= rail.bottom || box.bottom > rail.bottom;
    };
    return {
      fullyVisibleControlCount: controls.filter(controlFits).length,
      beyondFoldControlCount: controls.filter(controlFallsBeyondTheFold).length,
      canScroll: element.scrollHeight > element.clientHeight,
      maskImage: style.maskImage,
      maxHeight: style.maxHeight,
      overflowY: style.overflowY
    };
  });
  // The picture promises a bounded rail with a direct compact move, while the
  // remaining controls stay behind its scrolling, faded fold. Which row first
  // crosses that fold varies with the browser's font metrics.
  expect(compactRail.maxHeight).not.toBe("none");
  expect(compactRail.overflowY).toBe("auto");
  expect(compactRail.maskImage).not.toBe("none");
  expect(compactRail.fullyVisibleControlCount).toBeGreaterThanOrEqual(1);
  expect(compactRail.beyondFoldControlCount).toBeGreaterThanOrEqual(1);
  expect(compactRail.canScroll).toBe(true);

  // The connection read spans several round trips (#1103, #1114); Send stays
  // locked until it resolves, so this waits for the connected hint before
  // typing rather than racing a button that only looks clickable.
  await expect(page.getByText(conductorConversationCopy.composerHint)).toBeVisible();
  await page.getByLabel(workbenchPageCopy.composerLabel).fill("Keep this conversation on screen.");
  await page.getByRole("button", { name: workbenchPageCopy.send }).click();
  // Waits on the harness's own fence, the same reason `startConversationOverUi`
  // does (see its doc): the conversation list this measures below is empty
  // until the round actually lands, not the instant Send is clicked.
  await waitForFreshConductorRound(page, seededConductor.workflow_revision_hash);
  await placeConversationAboveComposer(page);
  const transcript = page.getByRole("list", { name: workbenchPageCopy.transcriptLabel });
  const newestLine = transcript.getByRole("listitem").last();
  const phone = page.viewportSize();
  const pinnedBox = await pinnedRegion.boundingBox();
  const composerBox = await page.getByRole("form", { name: workbenchPageCopy.composerRegionLabel }).boundingBox();
  const newestLineBox = await newestLine.boundingBox();
  expect(phone).not.toBeNull();
  expect(pinnedBox).not.toBeNull();
  expect(composerBox).not.toBeNull();
  expect(newestLineBox).not.toBeNull();
  if (phone === null || pinnedBox === null || composerBox === null || newestLineBox === null) {
    throw new Error("The bounded Workbench did not lay out every fixture.");
  }
  // The blessed picture holds the rail and the ear to the top and bottom of the
  // scrolling stage while the transcript passes beneath both
  // (docs/requirements/0003-ziel-ui-mockup-v8.html). So having scrolled to the
  // end of the conversation, a person still sees both fixtures whole and reads
  // the newest line in the room they leave open between them.
  expect(pinnedBox.y).toBeGreaterThanOrEqual(0);
  expect(pinnedBox.y + pinnedBox.height).toBeLessThanOrEqual(phone.height);
  expect(composerBox.y).toBeGreaterThanOrEqual(0);
  expect(composerBox.y + composerBox.height).toBeLessThanOrEqual(phone.height);
  expect(pinnedBox.y + pinnedBox.height).toBeLessThanOrEqual(composerBox.y);
  expect(newestLineBox.y).toBeGreaterThanOrEqual(pinnedBox.y + pinnedBox.height);
  expect(newestLineBox.y + newestLineBox.height).toBeLessThanOrEqual(composerBox.y);
  // Standing clear of both boxes is not yet being visible: either fixture wears
  // the stage's ground and would occlude the line sliding under it. Ask the
  // page what a finger would touch at the middle of that newest line.
  const newestLineAnswersAtItsCentre = await newestLine.evaluate((line) => {
    const box = line.getBoundingClientRect();
    const touched = document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2);
    return touched !== null && line.contains(touched);
  });
  expect(newestLineAnswersAtItsCentre).toBe(true);

  await page.getByRole("main").evaluate((stage) => {
    stage.scrollTo(0, 0);
  });
  const railBoxAtRest = await pinnedRegion.boundingBox();
  const transcriptBoxAtRest = await transcript.boundingBox();
  expect(railBoxAtRest).not.toBeNull();
  expect(transcriptBoxAtRest).not.toBeNull();
  if (railBoxAtRest === null || transcriptBoxAtRest === null) {
    throw new Error("The bounded Workbench did not lay out every fixture.");
  }
  // Unscrolled, nothing has slid anywhere yet: the conversation begins below
  // the rail rather than beneath it.
  expect(transcriptBoxAtRest.y).toBeGreaterThanOrEqual(railBoxAtRest.y + railBoxAtRest.height);

  const borderTokens = await page.evaluate(() => {
    const style = getComputedStyle(document.documentElement);
    return {
      compact: style.getPropertyValue("--edge").trim(),
      expanded: style.getPropertyValue("--edge-strong").trim()
    };
  });
  const expandedBorderWidth = await expandedDecision.evaluate((element) => getComputedStyle(element).borderTopWidth);
  const compactBorderWidth = await decisions
    .filter({
      has: page.getByRole("button", { name: workbenchPageCopy.answerDecision })
    })
    .first()
    .evaluate((element) => getComputedStyle(element).borderTopWidth);
  expect(expandedBorderWidth).toBe(borderTokens.expanded);
  expect(compactBorderWidth).toBe(borderTokens.compact);

  const compactLayout = await compactControls.first().evaluate((element) => {
    const question = element.querySelector<HTMLElement>(".compact-question");
    if (question === null) throw new Error("The compact decision question is missing.");
    const controlStyle = getComputedStyle(element);
    const questionStyle = getComputedStyle(question);
    return {
      flexWrap: controlStyle.flexWrap,
      minWidth: questionStyle.minWidth,
      overflow: questionStyle.overflow,
      textOverflow: questionStyle.textOverflow,
      whiteSpace: questionStyle.whiteSpace
    };
  });
  expect(compactLayout.flexWrap).toBe("wrap");
  expect(compactLayout.minWidth).toBe("160px");
  expect(compactLayout.overflow).toBe("visible");
  expect(compactLayout.textOverflow).toBe("clip");
  expect(compactLayout.whiteSpace).toBe("normal");

  // The conductor's own conversation is a seventh run now waiting for its
  // next message (its loop reopened "next_message" once the fake executor
  // answered the fresh round fenced above) -- counted in the rail's badge
  // like any other waiting run, but never in the pinned rail below, which
  // filters the conductor's own run out by reference (WorkbenchPage.svelte).
  await expect(page.getByRole("link", { name: "Workbench 7 needs you" })).toBeVisible();
  await expect(decisions).toHaveCount(6);
  await photograph(page, "workbench-bounded-decisions", true);
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

test("many rounds stay one History row, and the round cap at 24 starts a new conversation at 25", async ({
  page
}) => {
  test.setTimeout(180_000);

  const seededConductor = await resetAndSeedConductor(page);
  const conversationLink = await startConversationOverUi(page, "Round 1.", seededConductor.workflow_revision_hash);
  const run = await readConductorRun(page, await conversationRunReference(conversationLink));

  // Rounds 2-23 through the same production answer door the composer uses,
  // never through 22 browser round-trips over a fake that already answers
  // instantly -- see `answerConductorRoundDirectly`. Each round's own
  // execution id chains into the next call so it only answers once that
  // round's wait has genuinely opened, not the still-pending prior one.
  let settledExecutionId: string | undefined;
  for (let round = 2; round <= 23; round += 1) {
    settledExecutionId = await answerConductorRoundDirectly(page, run, `Round ${round}.`, settledExecutionId);
  }

  // A reload after 23 rounds still shows every one of them: reload takes
  // back no round (#658), which is what the live round-24 proof below relies
  // on to start from a real, durable 23-round thread.
  await page.reload();
  await expect(page.getByText(CONDUCTOR_FAKE_ANSWER)).toHaveCount(23, { timeout: 60_000 });

  await page.getByLabel(workbenchPageCopy.composerLabel).fill("Round 24, the cap.");
  await page.getByRole("button", { name: workbenchPageCopy.send }).click();
  await expect(page.getByText(CONDUCTOR_FAKE_ANSWER)).toHaveCount(24, { timeout: 60_000 });
  await expect(page.getByText(conductorConversationCopy.complete)).toBeVisible();

  // Message 25 is honestly a new, unrelated conversation, not a 25th round of
  // the capped one: a fresh run, a fresh single-reply transcript.
  await page.getByLabel(workbenchPageCopy.composerLabel).fill("Round 25 begins a new conversation.");
  await page.getByRole("button", { name: workbenchPageCopy.send }).click();
  await expect(page.getByText(CONDUCTOR_FAKE_ANSWER)).toHaveCount(1, { timeout: 60_000 });
  const newConversationLink = page.getByRole("link", { name: conductorChatCopy.openEpisode });
  await expect(newConversationLink).toHaveCount(1);
  expect(await conversationRunReference(newConversationLink)).not.toBe(run.publicRunReference);

  // The 24-round, now-finished conversation is one History row, not 24 --
  // identified by its own run's link (HistoryPage.svelte), never merely by
  // the workflow name every conductor row shares.
  await page.goto("/atelier/history");
  const historyRow = page.locator(".history-row").filter({
    has: page.locator(`a[href="/atelier/runs/${run.publicRunReference}"]`)
  });
  await expect(historyRow).toHaveCount(1);
});

test("a second, conflicting answer that names an already-claimed round is refused at the surface, and the accepted message stays part of the one conversation", async ({
  page,
  browser
}) => {
  test.setTimeout(120_000);

  const seededConductor = await resetAndSeedConductor(page);
  const conversationLink = await startConversationOverUi(page, "Round 1.", seededConductor.workflow_revision_hash);
  const run = await readConductorRun(page, await conversationRunReference(conversationLink));

  // Round 2's real answer claims its execution id first.
  const claimedExecutionId = await answerConductorRoundDirectly(page, run, "Round 2, the real answer.");

  // A second, differently-worded answer naming that same, already-claimed
  // execution id -- an overlapping retry landing on the wrong round -- is a
  // conflict on a valid round, pinned exactly so it can never again be told
  // as the corrupt-store refusal.
  const overlappingRetry = await page.request.post(`/atelier/api/v1/runs/${run.publicRunReference}/answers`, {
    headers: { "content-type": "application/json" },
    data: {
      workflow_revision_hash: run.workflowRevisionHash,
      node_id: run.currentNodeId,
      expected_node_execution_id: claimedExecutionId,
      actor: "operator",
      answer_base64: Buffer.from(JSON.stringify("Stale retry that must be refused.")).toString("base64")
    }
  });
  expect(overlappingRetry.status()).toBe(409);
  const overlappingProblem = (await overlappingRetry.json()) as { type: string; detail: string };
  expect(overlappingProblem.type).toBe("urn:atelier2:problem:v1:answer-state-conflict");

  // The accepted round-2 answer stays exactly where it landed; the refused
  // retry never wrote itself into the conversation at all.
  await page.reload();
  await expect(page.getByText(CONDUCTOR_FAKE_ANSWER)).toHaveCount(2, { timeout: 60_000 });
  await expect(page.getByText("Round 2, the real answer.")).toBeVisible();
  await expect(page.getByText("Stale retry that must be refused.")).toHaveCount(0);

  // The same collision, driven from the browser itself (#1062: "am Browser
  // gezeigt"): a second tab resolves the same running conversation and, once
  // round 3 is open (the reload above already proved it), races the first
  // tab to answer it. The loser's composer must show a refusal -- computed
  // by the same production reader the app itself uses
  // (`humanProblemDetail`, `frontend/src/lib/humanRefusal.ts`) from the wire
  // problem just pinned above, never a copy of its English retyped here.
  const refusalSentence = humanProblemDetail({
    type: "urn:atelier2:problem:v1:answer-state-conflict",
    detail: overlappingProblem.detail
  });
  const secondContext = await browser.newContext();
  try {
    const secondPage = await secondContext.newPage();
    await secondPage.goto("/atelier/chat");
    await expect(secondPage.getByText(CONDUCTOR_FAKE_ANSWER)).toHaveCount(2, { timeout: 20_000 });

    const firstMessage = "Tab one races round three.";
    const secondMessage = "Tab two races round three.";
    await page.getByLabel(workbenchPageCopy.composerLabel).fill(firstMessage);
    await secondPage.getByLabel(workbenchPageCopy.composerLabel).fill(secondMessage);
    // Fired together, not awaited one after the other: the race this proves
    // is between two clicks a few milliseconds apart, not between a click and
    // the real agent round each tab's own reactive stream would otherwise
    // have time to observe first.
    await Promise.all([
      page.getByRole("button", { name: workbenchPageCopy.send }).click(),
      secondPage.getByRole("button", { name: workbenchPageCopy.send }).click()
    ]);

    await expect(async () => {
      const firstRefused = await page.getByText(refusalSentence).count();
      const secondRefused = await secondPage.getByText(refusalSentence).count();
      expect(firstRefused + secondRefused).toBe(1);
    }).toPass({ timeout: 30_000 });

    // Exactly one of the two racing messages became round 3's durable
    // answer; the transcript never carries both.
    await page.reload();
    await expect(page.getByText(CONDUCTOR_FAKE_ANSWER)).toHaveCount(3, { timeout: 60_000 });
    const firstLanded = await page.getByText(firstMessage).count();
    const secondLanded = await page.getByText(secondMessage).count();
    expect(firstLanded + secondLanded).toBe(1);
  } finally {
    await secondContext.close();
  }
});

test("cancel in wait ends the conversation, and the composer opens a new one instead of continuing it", async ({
  page
}) => {
  test.setTimeout(120_000);

  const seededConductor = await resetAndSeedConductor(page);
  const conversationLink = await startConversationOverUi(page, "Round 1.", seededConductor.workflow_revision_hash);
  const publicRunReference = await conversationRunReference(conversationLink);

  await conversationLink.click();
  const cancel = runPageCopy.cancel;
  const opener = page.getByRole("button", { name: cancel.open });
  await expect(opener).toBeVisible({ timeout: 20_000 });
  await opener.click();
  await expect(page.getByRole("heading", { name: cancel.question })).toBeVisible();
  await page.getByRole("button", { name: cancel.confirm }).click();
  await expect(page.getByLabel(runPageCopy.whereThisRunStands)).toContainText(standingWords.cancelled, {
    timeout: 20_000
  });

  await page.goto("/atelier/chat");
  await expect(page.getByText(conductorConversationCopy.endedHint)).toBeVisible({ timeout: 20_000 });

  // The composer stays open, and the next message starts a new conversation
  // rather than continuing the one cancel just ended. The ended conversation's
  // own transcript (its "Round 1." and reply) is still shown beside the
  // hint, so the proof waits for this message's own, uniquely-worded text --
  // never merely a reply count the ended conversation already satisfies.
  const newMessage = "A new conversation after cancel.";
  await page.getByLabel(workbenchPageCopy.composerLabel).fill(newMessage);
  await page.getByRole("button", { name: workbenchPageCopy.send }).click();
  await expect(page.getByText(newMessage)).toBeVisible({ timeout: 60_000 });
  await expect(page.getByText(CONDUCTOR_FAKE_ANSWER)).toBeVisible({ timeout: 60_000 });
  const newConversationLink = page.getByRole("link", { name: conductorChatCopy.openEpisode });
  await expect(newConversationLink).toBeVisible();
  expect(await conversationRunReference(newConversationLink)).not.toBe(publicRunReference);
});

test("a second tab reconstructs the same open conversation and starts nothing silently", async ({
  page,
  browser
}) => {
  test.setTimeout(120_000);

  const seededConductor = await resetAndSeedConductor(page);
  const conversationLink = await startConversationOverUi(page, "Round 1.", seededConductor.workflow_revision_hash);
  const publicRunReference = await conversationRunReference(conversationLink);

  const secondContext = await browser.newContext();
  try {
    const secondPage = await secondContext.newPage();
    await secondPage.goto("/atelier/chat");
    // No sessionStorage stamp of its own: this tab falls back to the same
    // live-conversation selection the Workbench always runs, and reconstructs
    // the running conversation rather than showing an empty one.
    await expect(secondPage.getByText(CONDUCTOR_FAKE_ANSWER)).toBeVisible({ timeout: 20_000 });
    const secondConversationLink = secondPage.getByRole("link", { name: conductorChatCopy.openEpisode });
    expect(await conversationRunReference(secondConversationLink)).toBe(publicRunReference);

    // Opening the second tab started nothing silently: still exactly one
    // waiting run for this conductor revision.
    const waiting = await secondPage.request.get("/atelier/api/v1/runs?state=WAITING_INPUT&limit=50");
    expect(waiting.status()).toBe(200);
    const waitingRows = (await waiting.json()) as { items: RunListRow[] };
    const stillOneRun = healthyRunListItems(waitingRows.items).filter(
      (run) => run.workflow_revision_hash === seededConductor.workflow_revision_hash
    );
    expect(stillOneRun).toHaveLength(1);
  } finally {
    await secondContext.close();
  }
});

test("a mocked 5xx on the wait answer keeps the composer text and its own Resend delivers the same message", async ({
  page
}) => {
  // #1078 B4 (Opus field report): a typed message twice vanished on send with
  // no POST and no error. This proves the fix against a real failed POST
  // rather than the component-level mocks in workbenchPage.test.ts: the
  // composer keeps the words, the transcript carries the failed line with
  // Resend, and Resend's own POST is the round that actually lands.
  const seededConductor = await resetAndSeedConductor(page);
  await startConversationOverUi(page, "Round 1.", seededConductor.workflow_revision_hash);

  let refusedOnce = false;
  await page.route("**/atelier/api/v1/runs/**/answers", async (route) => {
    if (refusedOnce) {
      await route.continue();
      return;
    }
    refusedOnce = true;
    await route.fulfill({ status: 500, contentType: "application/json", body: "{}" });
  });

  const secondMessage = "Round 2, after the mocked 5xx.";
  await page.getByLabel(workbenchPageCopy.composerLabel).fill(secondMessage);
  await page.getByRole("button", { name: workbenchPageCopy.send }).click();

  await expect(page.getByText(workbenchPageCopy.conductorMessageFailed)).toBeVisible();
  await expect(page.getByLabel(workbenchPageCopy.composerLabel)).toHaveValue(secondMessage);

  await page.getByRole("button", { name: workbenchPageCopy.resendConductorMessage }).click();

  await expect(page.getByText(workbenchPageCopy.conductorMessageFailed)).toHaveCount(0);
  await expect(page.getByLabel(workbenchPageCopy.composerLabel)).toHaveValue("");
  await expect(page.getByText(secondMessage)).toBeVisible();
  expect(refusedOnce).toBe(true);
});

test("a poisoned mutation journal shows one sentence and one door, and forgetting it heals the room without a reload", async ({
  page
}) => {
  test.setTimeout(120_000);

  const seededConductor = await resetAndSeedConductor(page);
  const conversationLink = await startConversationOverUi(page, "Round 1.", seededConductor.workflow_revision_hash);
  const publicRunReference = await conversationRunReference(conversationLink);
  const firstRoundExecutionId = await currentWaitExecutionId(page, publicRunReference);

  // Poison this browser's own memory of pending sendings -- the same corrupt
  // JSON `mutationJournal.test.ts` proves `entries()` itself refuses to read.
  await page.evaluate(
    (key) => window.sessionStorage.setItem(key, "{"),
    MUTATION_JOURNAL_STORAGE_KEY
  );
  await page.reload();

  await expect(page.getByText(journalPoisonedCopy.sentence)).toBeVisible({ timeout: 20_000 });
  // Every card that would have read this same journal stays unshown -- no
  // stuck skeleton, no card that silently never appears.
  await expect(page.getByText(CONDUCTOR_FAKE_ANSWER)).toHaveCount(0);
  await expect(page.getByLabel(workbenchPageCopy.composerLabel)).toHaveCount(0);

  await page.getByRole("button", { name: journalPoisonedCopy.door }).click();
  const confirmDialog = page.getByRole("dialog", { name: journalPoisonedCopy.confirmLabel });
  await expect(
    confirmDialog.getByRole("heading", { name: journalPoisonedCopy.confirmQuestion })
  ).toBeVisible();
  await expect(confirmDialog.getByText(journalPoisonedCopy.disappearsFact)).toBeVisible();
  await expect(confirmDialog.getByText(journalPoisonedCopy.staysFact)).toBeVisible();
  await expect(confirmDialog.getByText(journalPoisonedCopy.permanentFact)).toBeVisible();
  await confirmDialog.getByRole("button", { name: journalPoisonedCopy.confirm }).click();

  // Healed in the same page, no reload: the notice retires and the
  // conversation that was reading this journal comes straight back.
  await expect(page.getByText(journalPoisonedCopy.sentence)).toHaveCount(0);
  await expect(page.getByText(CONDUCTOR_FAKE_ANSWER)).toBeVisible({ timeout: 20_000 });
  expect(
    await page.evaluate((key) => window.sessionStorage.getItem(key), MUTATION_JOURNAL_STORAGE_KEY)
  ).toBeNull();

  // The next start goes through: the healed journal accepts a fresh wait
  // answer over the composer's own audited path.
  await page.getByLabel(workbenchPageCopy.composerLabel).fill("Round 2 after forgetting.");
  await page.getByRole("button", { name: workbenchPageCopy.send }).click();
  await currentWaitExecutionId(page, publicRunReference, firstRoundExecutionId);
  await expect(page.getByText(CONDUCTOR_FAKE_ANSWER)).toHaveCount(2, { timeout: 20_000 });
});
