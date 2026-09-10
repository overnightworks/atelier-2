import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page, type Route } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { backLinkCopy } from "../../src/lib/backLinkCopy";
import { THE_ONE_PROJECT } from "../../src/lib/project";
import { catalogPageCopy } from "../../src/lib/catalogPageCopy";
import { runPageCopy } from "../../src/lib/runPageCopy";
import { humanMove, standingWords } from "../../src/lib/runState";
import { workbenchPageCopy } from "../../src/lib/workbenchPageCopy";
import {
  workbenchQuestionAttribute,
  workbenchQuestions
} from "../../src/lib/workbenchQuestions";
import {
  describeWorkbenchControlFacts,
  questionForWorkbenchControlFacts,
  workbenchInteractiveSelector,
  workbenchStageSelector,
  type WorkbenchControlFacts
} from "../support/workbenchControls";
import {
  unnamedAxeViolations,
  type AxeBaselineEntry,
  type CoreSurface
} from "../support/axeBaseline";
import {
  completedRun,
  runRow,
  startedRun,
  waitingInputRun,
  waitingReconciliationRun
} from "../support/runV3";

const foundReference = "run1.Zm91bmQtcnVu";
/** The workflow the e2e fixture publishes, and the detail surface it opens. */
const seededWorkflowName = "iterate-code";

function wrapped(text: string): string {
  return `[[[ ${text} ]]]`;
}
const baseline = JSON.parse(
  readFileSync(resolve(import.meta.dirname, "../support/axeBaseline.json"), "utf8")
) as AxeBaselineEntry[];

/**
 * The fixture host publishes one unnamed format-1 document, so no workflow
 * detail exists on it to scan. The detail surface is therefore served from
 * routed reads — the page under test is the real one; only what the wire
 * answers is staged.
 */
async function stageNamedWorkflow(page: Page): Promise<void> {
  const revisionHash = "a".repeat(64);
  await page.route(/\/workflow-revisions\?/, (route) =>
    route.fulfill({ json: { items: [], next_after_revision_hash: null } })
  );
  const summary = {
    workflow_revision_hash: revisionHash,
    workflow_format_version: 3,
    executable: true,
    not_executable_reason: null,
    name: seededWorkflowName,
    description: "build → review → fix, until green"
  };
  const graph = {
    workflow_format_version: 3,
    executable: true,
    not_executable_reason: null,
    node_count: 3,
    agent_roles: ["builder", "reviewer"],
    orders: [],
    wait_answer_schemas: [],
    node_previews: [
      { id: "build", kind: "agent", role: "builder", instruction_start: null, depends_on: [] },
      { id: "review", kind: "agent", role: "reviewer", instruction_start: null, depends_on: ["build"] },
      { id: "open pr", kind: "action", role: null, instruction_start: null, depends_on: ["review"] }
    ],
    loops: [
      {
        id: "until_green",
        member_node_ids: ["build", "review"],
        maximum_rounds: 3,
        repeat_while: { node: "review", verdict: "revise" }
      }
    ],
    name: seededWorkflowName,
    description: "build → review → fix, until green"
  };
  await page.route("**/atelier/api/v1/catalog-revisions/by-name/workflow/*", (route) =>
    route.fulfill({
      json: {
        display_name: seededWorkflowName,
        lineage_id: "b".repeat(64),
        catalog_revision_hash: revisionHash,
        revision_number: 1
      }
    })
  );
  await page.route(`**/atelier/api/v1/workflow-revisions/${revisionHash}`, (route) =>
    route.fulfill({ json: { workflow_revision_hash: revisionHash, document_base64: "YQ==", graph } })
  );
  // A regular expression, not a glob: `workflow-revisions?*` also matches the
  // detail path, and a later route wins, so the list would swallow it.
  await page.route(/\/workflow-revisions\?/, (route) =>
    route.fulfill({ json: { items: [summary], next_after_revision_hash: null } })
  );
}

const surfaces: readonly {
  surface: CoreSurface;
  path: string;
  ready: (page: Page) => Promise<void>;
  pseudoReady?: (page: Page) => Promise<void>;
  prepare?: (page: Page) => Promise<void>;
}[] =
  [
    {
      surface: "workbench",
      path: "/atelier/chat",
      ready: async (page) => {
        await expect(page.getByRole("heading", { name: "Workbench", exact: true })).toBeVisible();
      },
      pseudoReady: async (page) => {
        await expect(page.getByRole("heading", { name: "[[[ Workbench ]]]" })).toBeVisible();
      }
    },
    {
      surface: "catalog",
      path: "/atelier/catalog",
      prepare: stageNamedWorkflow,
      ready: async (page) => {
        await expect(page.getByRole("heading", { level: 1, name: "Catalog" })).toBeVisible();
      },
      pseudoReady: async (page) => {
        await expect(page.getByRole("heading", { level: 1, name: "[[[ Catalog ]]]" })).toBeVisible();
      }
    },
    {
      surface: "settings",
      path: "/atelier/settings",
      ready: async (page) => {
        await expect(page.getByRole("heading", { name: THE_ONE_PROJECT })).toBeVisible();
      },
      pseudoReady: async (page) => {
        await expect(page.getByText("[[[ Sources ]]]", { exact: true })).toBeVisible();
      }
    },
    {
      surface: "run",
      path: `/atelier/runs/${foundReference}`,
      ready: async (page) => {
        await expect(page.getByRole("navigation", { name: backLinkCopy.whereYouAre })).toBeVisible();
        await expect(
          page.getByRole("heading", { name: "Prove one reconciliation" })
        ).toBeVisible();
      }
    },
    {
      surface: "workflow-detail",
      path: `/atelier/catalog/${encodeURIComponent(seededWorkflowName)}`,
      prepare: stageNamedWorkflow,
      ready: async (page) => {
        await expect(
          page.getByRole("heading", { level: 1, name: seededWorkflowName })
        ).toBeVisible();
      }
    },
    {
      surface: "history",
      path: "/atelier/history",
      ready: async (page) => {
        await expect(page.getByRole("heading", { name: "History" })).toBeVisible();
      }
    }
  ];

async function scanSurface(page: Page) {
  const scan = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag22aa"])
    .analyze();
  return scan.violations;
}

const workbenchViewports = [
  { width: 1280, height: 900 },
  { width: 390, height: 844 }
] as const;

type WorkbenchReadReply = "populated" | "unavailable" | "empty";

function requiredMove(state: Parameters<typeof humanMove>[0]): string {
  const move = humanMove(state);
  if (move === null) {
    throw new Error(`${state} must name a human move`);
  }
  return move;
}

/** One run in every standing this room can hold, terminal fixtures included. */
function workbenchRuns() {
  return [
    startedRun({ run_id: "run-a", public_run_reference: "run1.cnVuLWE" }),
    waitingInputRun({ run_id: "wait-a", public_run_reference: "run1.d2FpdC1h", latest_event_cursor: null }),
    waitingReconciliationRun({
      run_id: "wait-b",
      public_run_reference: "run1.d2FpdC1i",
      latest_event_cursor: null
    }),
    startedRun({ run_id: "fail-a", public_run_reference: "run1.ZmFpbC1h", state: "FAILED" }),
    completedRun({ run_id: "done-a", public_run_reference: "run1.ZG9uZS1h" })
  ];
}

/**
 * This suite shares one server across every spec file (#742): durable state
 * an earlier spec left behind (a completed run, a seeded fixture) can still
 * reach a mocked Workbench frame through a channel its route mocks don't
 * cover. Each mocked frame therefore resets the server to its cold-boot
 * baseline before it stages anything, instead of depending on running before
 * every other spec in the file listing.
 */
async function resetToKnownStore(page: Page): Promise<void> {
  const reset = await page.request.post("/__e2e/recompose?reset=true");
  expect(reset.status()).toBe(202);
  const expectedGeneration = await reset.text();
  await expect(async () => {
    expect(await (await page.request.get("/__e2e/generation")).text()).toBe(expectedGeneration);
  }).toPass({ timeout: 20_000 });
}

/**
 * A still room: the Workbench holds the attention stream, so a room whose list
 * reads are staged still absorbs whatever the live workshop nudges it about --
 * the fixture host's own seeded runs walked into these frames and made them
 * measure a room half staged and half live. The hold opens and stays quiet, so
 * a vocabulary or named-question gate measures exactly the room it staged.
 * That the hold really carries a new decision is proven where it belongs:
 * against the real server, in `cockpit.spec.ts`.
 */
async function stageQuietAttention(page: Page): Promise<void> {
  await page.addInitScript(() =>
    Object.defineProperty(window, "EventSource", {
      value: class extends EventTarget {
        constructor() {
          super();
          queueMicrotask(() => this.dispatchEvent(new Event("open")));
        }
        close() {}
      }
    })
  );
}

async function routeWorkbenchReads(page: Page, read: () => WorkbenchReadReply): Promise<void> {
  await resetToKnownStore(page);
  await stageQuietAttention(page);
  await page.route("**/atelier/api/v1/runs*", async (route: Route) => {
    if (read() === "unavailable") {
      // A real HTTP answer the server gave, not a round trip that never
      // happened -- the page-local "unavailable" this test wants, never the
      // central, cross-page reachability signal #700 owns.
      await route.fulfill({
        status: 503,
        json: {
          type: "urn:atelier2:problem:v1:temporarily-unavailable",
          title: "Temporarily unavailable",
          status: 503,
          detail: "the durable run store is unreachable"
        }
      });
      return;
    }
    const state = new URL(route.request().url()).searchParams.get("state");
    const source = read() === "empty" ? [] : workbenchRuns();
    await route.fulfill({
      json: {
        items: source.filter((run) => state === null || run.state === state).map(runRow),
        next_after: null
      }
    });
  });
}

async function expectWorkbenchControlsAreInventoried(
  page: Page,
  expected: readonly string[]
): Promise<void> {
  const facts = (await page.locator(workbenchStageSelector).evaluate(
    (root, [selector, attribute]) =>
      [...root.querySelectorAll(selector as string)].map((element) => ({
        questionId: element.getAttribute(attribute as string),
        href: element.getAttribute("href"),
        ariaLabel: element.getAttribute("aria-label"),
        tag: element.tagName.toLowerCase()
      })),
    [workbenchInteractiveSelector, workbenchQuestionAttribute]
  )) as WorkbenchControlFacts[];
  const unanswered = facts.filter((item) => questionForWorkbenchControlFacts(item) === null);
  expect(
    unanswered.map(describeWorkbenchControlFacts),
    unanswered.map(describeWorkbenchControlFacts).join("; ")
  ).toEqual([]);
  expect(new Set(facts.map((item) => questionForWorkbenchControlFacts(item)?.id))).toEqual(
    new Set(expected)
  );
}

/** One run in each standing a surface groups by. */
function runsOfEveryStanding() {
  return [
    startedRun({ run_id: "running project", public_run_reference: "run1.cnVubmluZyBwcm9qZWN0" }),
    waitingInputRun({ run_id: "waiting project", public_run_reference: "run1.d2FpdGluZyBwcm9qZWN0", latest_event_cursor: null }),
    completedRun({ run_id: "done project", public_run_reference: "run1.ZG9uZSBwcm9qZWN0" })
  ];
}

async function expectWorkbenchCopyFits(page: Page, desktop: boolean): Promise<void> {
  const heading = page.getByRole("heading", { name: "[[[ Workbench ]]]" });
  const room = page.locator(workbenchStageSelector);
  expect(await heading.evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true);
  expect(await room.evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true);
  if (desktop) {
    expect(await room.evaluate((element) => {
      const parent = element.parentElement!;
      const style = getComputedStyle(parent); return element.clientWidth === parent.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
    })).toBe(true);
  }
}

// The skin answers `prefers-color-scheme`, so a contrast that only holds in
// light is only half a promise: both themes are scanned.
const themes = ["light", "dark"] as const;

test("proves(core-surfaces-have-no-unnamed-axe-violations): core surfaces have no unnamed axe-core violations", async ({ page }) => {
  for (const theme of themes) {
    await page.emulateMedia({ colorScheme: theme });
    for (const { surface, path, ready, prepare } of surfaces) {
      await prepare?.(page);
      await page.goto(path);
      await ready(page);
      const unnamed = unnamedAxeViolations(surface, await scanSurface(page), baseline);
      expect(unnamed, `${surface} in ${theme}: ${JSON.stringify(unnamed, null, 2)}`).toEqual([]);
    }
  }
});

test("core surfaces render owned display strings under a pseudo-locale", async ({ page }) => {
  for (const { path, ready, pseudoReady, prepare } of surfaces) {
    await prepare?.(page);
    const separator = path.includes("?") ? "&" : "?";
    await page.goto(`${path}${separator}pseudo-locale=1`);
    if (pseudoReady !== undefined) {
      await pseudoReady(page);
    } else {
      await ready(page);
    }
    const rail = page.getByRole("navigation", { name: "Workshop" });
    await expect(rail.getByText("[[[ atelier ]]]", { exact: true })).toBeVisible();
    await expect(rail.getByText("[[[ Workbench ]]]", { exact: true })).toBeVisible();
    await expect(rail.getByText("[[[ Catalog ]]]", { exact: true })).toBeVisible();
    await expect(rail.getByText("[[[ History ]]]", { exact: true })).toBeVisible();
    await expect(rail.getByText("[[[ Settings ]]]", { exact: true })).toBeVisible();
    // The rooms the picture retired leave no entry behind, and the rail holds
    // no slot that cannot be clicked (ADR 0019 §4).
    await expect(rail.getByText("[[[ Board ]]]", { exact: true })).toHaveCount(0);
    await expect(rail.getByText("[[[ Workflows ]]]", { exact: true })).toHaveCount(0);
    await expect(rail.getByText("[[[ Not built yet ]]]", { exact: true })).toHaveCount(0);
    await expect(rail.getByText("[[[ Profile ]]]", { exact: true })).toHaveCount(0);
    await expect(rail.getByText(THE_ONE_PROJECT, { exact: true })).toBeVisible();
    if (path === "/atelier/catalog") {
      const catalogGroups = page.getByRole("group", { name: wrapped(catalogPageCopy.catalogGroups) });
      await expect(catalogGroups.locator(".filter-chip")).toHaveText([
        wrapped(catalogPageCopy.all),
        `${wrapped(catalogPageCopy.workflowsTitle)}1`,
        `${wrapped(catalogPageCopy.agentsTitle)}0`,
        `${wrapped(catalogPageCopy.skillsTitle)}0`
      ]);
      await expect(page.getByLabel(wrapped(catalogPageCopy.searchLabel))).toBeVisible();
      await expect(page.getByRole("region", { name: wrapped(catalogPageCopy.workflowsTitle) })).toHaveCount(1);
      await expect(page.getByRole("region", { name: wrapped(catalogPageCopy.agentsByProvider) })).toHaveCount(1);
      await expect(page.getByRole("link", { name: wrapped(seededWorkflowName) })).toBeVisible();
    }
  }
});

/**
 * The Workbench with work on it is staged, not inherited: the runs earlier
 * specs start finish on their own clock, so a read straight from the fixture
 * host is populated or empty depending on how long the spec before this one
 * took.
 */
async function stageWorkbenchWithWork(page: Page): Promise<void> {
  await resetToKnownStore(page);
  await stageQuietAttention(page);
  await page.route("**/atelier/api/v1/runs*", (route) => {
    const state = new URL(route.request().url()).searchParams.get("state");
    const items = runsOfEveryStanding().filter((run) => run.state === state).map(runRow);
    return route.fulfill({ json: { items, next_after: null } });
  });
}

// The identifier stays "studio-…" (acceptance/435): the room it measures is
// the Workbench since ADR 0019 retired the Board.
test("proves(studio-entry-copy-is-owned-and-survives-pseudo-locale): the Workbench keeps header and confirmed empty copy visible at desktop and 390px", async ({ page }) => {
  await stageWorkbenchWithWork(page);
  for (const viewport of workbenchViewports) {
    await page.setViewportSize(viewport);
    await page.goto("/atelier?pseudo-locale=1");
    await page.evaluate(() => window.scrollTo(0, 0));
    await expect(page.getByRole("heading", { name: "[[[ Workbench ]]]" })).toBeVisible();
    // Starting a workflow lives in the Catalog, not the Workbench head: no
    // Start control of any kind sits in this room.
    await expect(page.getByRole("link", { name: /Start/ })).toHaveCount(0);
    await expectWorkbenchCopyFits(page, viewport.width === 1280);
    await page.screenshot({
      path: `test-results/workbench-common-${viewport.width}.png`,
      fullPage: true
    });
  }

  await page.unroute("**/atelier/api/v1/runs*");
  await page.route("**/atelier/api/v1/runs*", (route) =>
    route.fulfill({ json: { items: [], next_after: null } })
  );

  for (const viewport of workbenchViewports) {
    await page.setViewportSize(viewport);
    await page.goto("/atelier?pseudo-locale=1");
    await page.evaluate(() => window.scrollTo(0, 0));

    await expect(page.getByRole("heading", { name: "[[[ Workbench ]]]" })).toBeVisible();
    await expect(
      page.getByRole("heading", { name: `[[[ ${workbenchPageCopy.emptyTitle} ]]]` })
    ).toBeVisible();
    await expect(
      page.getByText(wrapped(workbenchPageCopy.emptyDescription), { exact: true })
    ).toBeVisible();
    await expect(
      page.getByRole("link", { name: wrapped(workbenchPageCopy.emptyStart) })
    ).toBeVisible();
    await expectWorkbenchCopyFits(page, viewport.width === 1280);
    await page.screenshot({
      path: `test-results/workbench-empty-${viewport.width}.png`,
      fullPage: true
    });
  }
});

test("proves(studio-populated-copy-is-owned-and-survives-pseudo-locale): the Workbench keeps populated and unavailable copy visible at desktop and 390px", async ({ page }) => {
  let reply: WorkbenchReadReply = "populated";
  await routeWorkbenchReads(page, () => reply);

  for (const viewport of workbenchViewports) {
    await page.setViewportSize(viewport);
    reply = "populated";
    await page.goto("/atelier?pseudo-locale=1");
    await page.evaluate(() => window.scrollTo(0, 0));
    await expect(page.getByRole("heading", { name: "[[[ Workbench ]]]" })).toBeVisible();
    // The waiting-for-an-answer run stands pinned in the Open-decisions
    // region; the reconciliation this room cannot answer inline stays a row.
    await expect(
      page.getByRole("region", { name: wrapped(workbenchPageCopy.pinnedDecisionsLabel) })
    ).toBeVisible();
    await expect(
      page.getByText(`${wrapped(requiredMove("WAITING_RECONCILIATION"))} →`)
    ).toBeVisible();
    // The fail-a and done-a fixtures in this same set are terminal: this room
    // never asks for their state at all, so nothing of theirs is here for
    // History to duplicate (#667).
    await expect(page.getByText(wrapped(standingWords.failed))).toHaveCount(0);
    await expect(page.getByText(wrapped(standingWords.done))).toHaveCount(0);
    await expectWorkbenchCopyFits(page, viewport.width === 1280);
    await page.screenshot({
      path: `test-results/workbench-populated-${viewport.width}.png`,
      fullPage: true
    });
  }

  for (const viewport of workbenchViewports) {
    await page.setViewportSize(viewport);
    reply = "unavailable";
    await page.goto("/atelier?pseudo-locale=1");
    await page.evaluate(() => window.scrollTo(0, 0));
    await expect(page.getByRole("heading", { name: "[[[ Workbench ]]]" })).toBeVisible();
    await expect(page.getByText(wrapped(workbenchPageCopy.runsUnavailable))).toBeVisible();
    await expectWorkbenchCopyFits(page, viewport.width === 1280);
    await page.screenshot({
      path: `test-results/workbench-unavailable-${viewport.width}.png`,
      fullPage: true
    });
  }
});

test("proves(every-rendered-workbench-control-is-inventoried): every rendered Workbench control is inventoried with a question-shaped entry, populated and empty", async ({ page }) => {
  let reply: WorkbenchReadReply = "populated";
  await routeWorkbenchReads(page, () => reply);

  for (const viewport of workbenchViewports) {
    await page.setViewportSize(viewport);
    reply = "populated";
    await page.goto("/atelier");
    await expect(page.getByRole("heading", { name: workbenchPageCopy.title })).toBeVisible();
    await expect(
      page.getByRole("region", { name: workbenchPageCopy.pinnedDecisionsLabel })
    ).toBeVisible();
    // Once the read confirms, ReadState.svelte mounts no control at all (#532).
    await expect(
      page.getByRole("button", { name: /workbench runs/ })
    ).toHaveCount(0);
    await expectWorkbenchControlsAreInventoried(page, [
      workbenchQuestions.openRun.id
    ]);
  }

  for (const viewport of workbenchViewports) {
    await page.setViewportSize(viewport);
    reply = "empty";
    await page.goto("/atelier");
    await expect(
      page.getByRole("heading", { name: workbenchPageCopy.emptyTitle })
    ).toBeVisible();
    await expect(
      page.getByRole("link", { name: workbenchPageCopy.emptyStart })
    ).toBeVisible();
    await expectWorkbenchControlsAreInventoried(page, [
      workbenchQuestions.emptyStart.id
    ]);
  }
});

// The phone width from the picture, and a laptop width wide enough that a
// fixed-width table or graph would otherwise hide its own overflow (#435).
const overflowViewports = [
  { width: 390, height: 844 },
  { width: 1440, height: 900 }
] as const;

/**
 * `.workshop-stage { overflow: auto }` (styles.css) can round its own
 * scrollbar gutter into `clientWidth` by a device pixel, which would flake a
 * strict compare between runs; one CSS pixel of slack absorbs that without
 * hiding a real overflow, which is always many pixels wide.
 */
const OVERFLOW_TOLERANCE_PX = 1;

/**
 * The document and the room's own scroll area are the two places a stray
 * fixed width, an unshrinkable flex child, or a wide table sitting outside
 * its own `overflow-x: auto` container would show up as a horizontal
 * scrollbar. A table, graph, or code block that scrolls inside its own
 * container is unaffected: only the two outer measurements below are wider
 * than their viewport when something has actually broken layout.
 */
async function overflowsItsViewport(page: Page): Promise<boolean> {
  return page.evaluate((tolerance) => {
    const overflows = (element: Element) => element.scrollWidth > element.clientWidth + tolerance;
    const stage = document.querySelector(".workshop-stage");
    return overflows(document.documentElement) || (stage !== null && overflows(stage));
  }, OVERFLOW_TOLERANCE_PX);
}

/**
 * Every keyframe animation and transition in the skin is forced to a
 * near-zero, single-iteration duration under `prefers-reduced-motion: reduce`
 * (styles.css). Waiting on the `finished` promise of every animation that
 * will actually finish -- never an arbitrary sleep -- lets that settle
 * deterministically: an infinite one (`iterations: Infinity`) never finishes
 * by definition, so only the bounded ones are awaited. Whatever is still
 * `running` afterwards, with an effective duration over a millisecond or
 * unbounded iterations, is a real animation the skin forgot to gate --
 * including one driven by `Element.animate()`, which this CSS rule never
 * reaches at all.
 */
async function stillAnimatesUnderReducedMotion(page: Page): Promise<boolean> {
  return page.evaluate(async () => {
    const hasBoundedIterations = (animation: Animation) => {
      const iterations = animation.effect?.getComputedTiming().iterations;
      return iterations !== undefined && Number.isFinite(iterations);
    };
    await Promise.allSettled(
      document
        .getAnimations()
        .filter(hasBoundedIterations)
        .map((animation) => animation.finished)
    );
    const SETTLED_DURATION_MS = 1;
    return document.getAnimations().some((animation) => {
      if (animation.playState !== "running") return false;
      const timing = animation.effect?.getComputedTiming();
      const duration = typeof timing?.duration === "number" ? timing.duration : Infinity;
      return timing?.iterations === Infinity || duration > SETTLED_DURATION_MS;
    });
  });
}

/**
 * `foundReference` -- the run every other test in this file opens -- sits in
 * WAITING_RECONCILIATION, so its graph never mounts the CSS pulse
 * (`pipe-pulse`, WorkflowGraphDrawing.svelte) a reduced-motion assertion is
 * meant to prove stays off: a room where nothing moves cannot prove that
 * motion is suppressed. This starts one node's pulse for real, through the
 * same public run API `uiq-budget.spec.ts` and `vision-shots.spec.ts`
 * already stage a working run through -- not a new seed door.
 */
async function stageWorkingRun(page: Page, runId: string): Promise<string> {
  const schema = await page.request.post("/atelier/api/v1/schema-revisions", {
    headers: { "content-type": "application/json" },
    data: "true"
  });
  expect([200, 201]).toContain(schema.status());
  const schemaHash = (await schema.json()).schema_revision_hash as string;

  const workflow = await page.request.post("/atelier/api/v1/workflow-revisions", {
    headers: { "content-type": "application/yaml" },
    data: [
      "format_version: 3",
      "name: ui-quality-working",
      "nodes:",
      "  - id: work",
      "    type: agent",
      "    role: builder",
      "    mode: headless",
      "    instruction: Stay working so a reduced-motion check has a real pulse to prove off.",
      "    outputs:",
      "      - name: result",
      "        schema:",
      "          ref: result-schema",
      `          revision: ${schemaHash}`,
      ""
    ].join("\n")
  });
  // Content-addressed: the second viewport's run publishes the identical
  // document, which the API answers 200 (already stored) rather than 201.
  expect([200, 201]).toContain(workflow.status());
  const revisionHash = (await workflow.json()).workflow_revision_hash as string;

  const auth = await page.request.post("/atelier/api/v1/auth-profile-revisions", {
    data: {
      profile_id: `ui-quality-working-${runId}`,
      revision_number: 1,
      provider_id: "e2e-v3-held",
      auth_mode: "subscription"
    }
  });
  expect([200, 201]).toContain(auth.status());
  const configuration = await page.request.post("/atelier/api/v1/agent-configuration-revisions", {
    data: {
      model: "ui-quality-working-model",
      auth_profile_revision_hash: (await auth.json()).auth_profile_revision_hash,
      executor_revision: "held/v1",
      requested_capability: "headless"
    }
  });
  expect([200, 201]).toContain(configuration.status());
  const agentHash = (await configuration.json()).agent_configuration_revision_hash as string;

  const currentRegistry = await page.request.get("/atelier/api/v1/model-registries/e2e-v3-held");
  const revisionNumber = currentRegistry.status() === 200
    ? ((await currentRegistry.json()) as { revision_number: number }).revision_number + 1
    : 1;
  const registry = await page.request.put("/atelier/api/v1/model-registries/e2e-v3-held", {
    data: {
      revision_number: revisionNumber,
      entries: [{ model_id: "ui-quality-working-model", agent_configuration_revision_hash: agentHash }]
    }
  });
  expect([200, 201]).toContain(registry.status());
  const validation = await page.request.post(
    "/atelier/api/v1/model-registries/e2e-v3-held/validations",
    { data: { agent_configuration_revision_hash: agentHash } }
  );
  expect([200, 201]).toContain(validation.status());

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
    expect((await read.json()).state).toBe("STARTED");
  }).toPass({ timeout: 20_000 });
  return reference;
}

type OverflowMotionCase = {
  name: string;
  /** Navigates to the surface and waits for its own ready signal. */
  open: (page: Page) => Promise<void>;
};

const overflowMotionCases: readonly OverflowMotionCase[] = [
  ...surfaces.map(
    ({ surface, path, ready, prepare }): OverflowMotionCase => ({
      name: surface,
      open: async (page) => {
        await prepare?.(page);
        await page.goto(path);
        await ready(page);
      }
    })
  ),
  {
    name: "run-working",
    open: async (page) => {
      const reference = await stageWorkingRun(page, `ui-quality/working-${test.info().workerIndex}-${Date.now()}`);
      await page.goto(`/atelier/runs/${reference}`);
      await expect(page.getByLabel(runPageCopy.whereThisRunStands)).toBeVisible();
      await expect(page.locator('[data-live="true"]')).toBeVisible();
    }
  }
];

// No `proves(...)` marker: #1495's own acceptance line is "none, Prüfscheibe
// ohne geregelten Operator-Satz" -- no acceptance/*.toml sentence names
// overflow or reduced motion, and a test may not invent one the gate would
// then have to treat as ruled.
test.describe("core surfaces have no page overflow and settle without animation under reduced motion", () => {
  for (const overflowMotionCase of overflowMotionCases) {
    for (const viewport of overflowViewports) {
      test(`${overflowMotionCase.name} at ${viewport.width}px`, async ({ page }) => {
        await page.emulateMedia({ reducedMotion: "reduce" });
        await page.setViewportSize(viewport);
        await overflowMotionCase.open(page);
        // Waits for the one shared loading skeleton (LoadingState.svelte,
        // behind ReadState on every read-driven surface) to clear, so the
        // measurements below see the loaded list, card, or confirmed empty
        // state -- never the skeleton that renders before it.
        await expect(page.locator(".loading-state")).toHaveCount(0);

        expect(
          await overflowsItsViewport(page),
          `${overflowMotionCase.name} at ${viewport.width}px overflows the page or the workshop stage`
        ).toBe(false);

        expect(
          await stillAnimatesUnderReducedMotion(page),
          `${overflowMotionCase.name} at ${viewport.width}px keeps animating under reduced motion`
        ).toBe(false);
      });
    }
  }
});
