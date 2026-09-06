import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { cleanup, render, screen, waitFor } from "@testing-library/svelte";
import axe from "axe-core";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../../src/App.svelte";
import type { CockpitApi } from "../../src/api/client";
import { catalogPageCopy } from "../../src/lib/catalogPageCopy";
import { historyPageCopy } from "../../src/lib/historyPageCopy";
import { MutationJournal } from "../../src/lib/mutationJournal";
import { THE_ONE_PROJECT } from "../../src/lib/project";
import { readStateCopy } from "../../src/lib/readStateCopy";
import { cockpitRoute, type CockpitRoute } from "../../src/lib/route";
import { settingsPageCopy } from "../../src/lib/settingsPageCopy";
import { workbenchPageCopy } from "../../src/lib/workbenchPageCopy";
import { WORKSHOP_DESTINATION } from "../../src/lib/workshop";
import { cockpitApiStub } from "../support/cockpitApi";
import {
  PUBLIC_REFERENCE_PLACEHOLDER,
  SERVED_PATHS,
  WORKFLOW_NAME_PLACEHOLDER
} from "../support/servedPaths";
import { publicReference, startedRun, workflowName, workflowRevision } from "../support/runV3";

/**
 * REQ-UIQ-05: the core surfaces meet WCAG 2.2 AA, or the violation
 * carries an item. This checker runs axe-core in the unit tree to the
 * wcag2a, wcag2aa, and wcag22aa tags against each distinct served page.
 *
 * The pages are every SERVED_PATHS entry that cockpitRoute opens
 * (placeholders filled from the same fixtures the other app tests
 * already use), once per page so /atelier, /atelier/, and
 * /atelier/chat are one Workbench. A served path that opens
 * not-found is a defect. The four rail destinations in
 * WORKSHOP_DESTINATION must be among them.
 *
 * A violation is tied to an item by an atelier-2 GitHub issue URL
 * (`https://github.com/FlexOr2/atelier-2/issues/<n>`) on the same axe
 * rule id and the same surface. The run names that URL so a reader can
 * follow the link. A row without an issue URL is not an exception.
 * This checker does not query GitHub to prove the item is still open.
 *
 * jsdom does not load index.html, so the document language is stamped
 * from that file before a scan; a missing lang there is a violation.
 * Incomplete results (jsdom cannot prove color-contrast) are not
 * violations — they are outside what this tree can show. A question
 * sheet the operator has not opened and a live browser's contrast
 * engine are outside what this open scans.
 */

const SAMPLE_WORKFLOW_NAME = "iterate-code";
const WORKFLOW_HASH = "b".repeat(64);
const PROJECT_REFERENCE = "project1.dGVzdA";
const ISSUE_URL = /^https:\/\/github\.com\/FlexOr2\/atelier-2\/issues\/\d+$/;
const WCAG_TAGS = ["wcag2a", "wcag2aa", "wcag22aa"] as const;
const NAMED_ITEM = "https://github.com/FlexOr2/atelier-2/issues/890";

type ServedPage = Exclude<CockpitRoute["page"], "not-found">;

type ServedSurface = {
  page: ServedPage;
  path: string;
};

type NamedException = {
  id: string;
  surface: string;
  item: string;
};

const NAMED_EXCEPTIONS: readonly NamedException[] = [];

type AxeRuleHit = {
  id: string;
  help: string;
};

function instantiate(servedPath: string): string {
  return servedPath
    .replace(PUBLIC_REFERENCE_PLACEHOLDER, publicReference)
    .replace(WORKFLOW_NAME_PLACEHOLDER, SAMPLE_WORKFLOW_NAME);
}

function distinctServedPages(): ServedSurface[] {
  const seen = new Set<ServedPage>();
  const surfaces: ServedSurface[] = [];
  for (const servedPath of SERVED_PATHS) {
    const path = instantiate(servedPath);
    const route = cockpitRoute(path);
    if (route.page === "not-found") {
      throw new Error(`${servedPath} is served but opens no page`);
    }
    if (seen.has(route.page)) continue;
    seen.add(route.page);
    surfaces.push({ page: route.page, path });
  }
  return surfaces;
}

function headingName(page: ServedPage): string {
  if (page === "workbench") return workbenchPageCopy.title;
  if (page === "catalog") return catalogPageCopy.title;
  if (page === "history") return historyPageCopy.title;
  if (page === "settings") return THE_ONE_PROJECT;
  if (page === "run") return workflowName;
  if (page === "workflow") return SAMPLE_WORKFLOW_NAME;
  const exhaustive: never = page;
  return exhaustive;
}

function unnamedLine(surface: string, violation: AxeRuleHit): string {
  return `${surface}: ${violation.id} — ${violation.help}. Fix this on ${surface}, or attach an atelier-2 issue URL (https://github.com/FlexOr2/atelier-2/issues/<n>) to the same axe rule id and the same surface.`;
}

function namedLine(surface: string, violation: AxeRuleHit, item: string): string {
  return `${surface}: ${violation.id} is named by ${item}`;
}

function itemTying(
  violation: AxeRuleHit,
  surface: string,
  exceptions: readonly NamedException[]
): string | null {
  const match = exceptions.find(
    (entry) => entry.id === violation.id && entry.surface === surface && ISSUE_URL.test(entry.item)
  );
  return match === undefined ? null : match.item;
}

function partitionViolations(
  surface: string,
  violations: readonly AxeRuleHit[],
  exceptions: readonly NamedException[]
): { unnamed: string[]; named: string[] } {
  const unnamed: string[] = [];
  const named: string[] = [];
  for (const violation of violations) {
    const item = itemTying(violation, surface, exceptions);
    if (item === null) unnamed.push(unnamedLine(surface, violation));
    else named.push(namedLine(surface, violation, item));
  }
  return { unnamed, named };
}

function stampProductionDocumentShell(): void {
  const index = readFileSync(resolve(process.cwd(), "index.html"), "utf8");
  const lang = /<html\b[^>]*\slang="([^"]+)"/i.exec(index)?.[1];
  if (lang === undefined) document.documentElement.removeAttribute("lang");
  else document.documentElement.setAttribute("lang", lang);
}

function surfaceApi(): CockpitApi {
  return cockpitApiStub({
    listProjects: vi.fn(async () => ({
      items: [{ public_project_reference: PROJECT_REFERENCE }]
    })),
    listProjectSources: vi.fn(async () => ({ items: [] })),
    getProjectModelDefaults: vi.fn(async () => ({
      project_id: "atelier",
      public_project_reference: PROJECT_REFERENCE,
      revision_number: 1,
      project_model_defaults_revision_hash: "d".repeat(64),
      defaults: []
    })),
    getRun: vi.fn(async () => startedRun()),
    getWorkflowRevision: vi.fn(async (hash: string) =>
      hash === WORKFLOW_HASH
        ? {
            workflow_revision_hash: WORKFLOW_HASH,
            document_base64: "YQ==",
            provenance: null,
            graph: {
              workflow_format_version: 3 as const,
              executable: true,
              not_executable_reason: null,
              node_count: 1,
              agent_roles: [],
              orders: [],
              wait_answer_schemas: [],
              node_previews: [],
              loops: [],
              name: SAMPLE_WORKFLOW_NAME,
              description: null
            }
          }
        : workflowRevision()
    ),
    getRevisionByName: vi.fn(async () => ({
      display_name: SAMPLE_WORKFLOW_NAME,
      lineage_id: "e".repeat(64),
      catalog_revision_hash: WORKFLOW_HASH,
      revision_number: 1
    })),
    listWorkflowRevisions: vi.fn(async () => ({
      items: [
        {
          workflow_revision_hash: WORKFLOW_HASH,
          workflow_format_version: 3 as const,
          executable: true,
          not_executable_reason: null,
          name: SAMPLE_WORKFLOW_NAME,
          description: "build",
          provenance: null
        }
      ],
      next_after_revision_hash: null
    }))
  });
}

function openAt(path: string): void {
  window.history.replaceState(null, "", path);
  render(App, {
    props: {
      cockpitApi: surfaceApi(),
      mutationJournal: new MutationJournal(sessionStorage)
    }
  });
}

async function waitUntilUp(page: ServedPage): Promise<void> {
  expect(
    (await screen.findByRole("heading", { level: 1, name: headingName(page) })).isConnected
  ).toBe(true);
  if (page === "workbench" || page === "catalog" || page === "history") {
    await waitFor(() => {
      expect(screen.queryAllByText(readStateCopy.looking, { exact: false })).toEqual([]);
    });
    return;
  }
  if (page === "settings") {
    expect(
      (await screen.findByRole("heading", { name: settingsPageCopy.sourcesTitle })).isConnected
    ).toBe(true);
  }
}

async function scanAxe(): Promise<{ violations: AxeRuleHit[]; incomplete: AxeRuleHit[] }> {
  const results = await axe.run(document, {
    runOnly: { type: "tag", values: [...WCAG_TAGS] }
  });
  return {
    violations: results.violations.map((violation) => ({ id: violation.id, help: violation.help })),
    incomplete: results.incomplete.map((violation) => ({ id: violation.id, help: violation.help }))
  };
}

function stripCatalogLinkName(): void {
  const catalog = screen.getByRole("link", { name: WORKSHOP_DESTINATION.catalog.label });
  catalog.replaceChildren();
  catalog.removeAttribute("aria-label");
  catalog.removeAttribute("title");
}

async function openWorkbenchWithNamelessCatalogLink(): Promise<{
  unnamed: string[];
  named: string[];
  violations: AxeRuleHit[];
}> {
  const workbench = distinctServedPages().find((surface) => surface.page === "workbench");
  if (workbench === undefined) throw new Error("SERVED_PATHS opened no Workbench");
  openAt(workbench.path);
  await waitUntilUp("workbench");
  stripCatalogLinkName();
  const scanned = await scanAxe();
  return {
    ...partitionViolations("workbench", scanned.violations, []),
    violations: scanned.violations
  };
}

beforeEach(() => {
  sessionStorage.clear();
  stampProductionDocumentShell();
});

afterEach(() => {
  vi.restoreAllMocks();
  cleanup();
  window.history.replaceState(null, "", "/atelier");
});

describe("core surfaces meet WCAG 2.2 AA, or the violation carries an item", () => {
  it("names the surface, the rule, and what a person should do for an unnamed violation", () => {
    expect(
      partitionViolations(
        "workbench",
        [{ id: "button-name", help: "Buttons must have discernible text" }],
        []
      ).unnamed
    ).toEqual([
      "workbench: button-name — Buttons must have discernible text. Fix this on workbench, or attach an atelier-2 issue URL (https://github.com/FlexOr2/atelier-2/issues/<n>) to the same axe rule id and the same surface."
    ]);
  });

  it("does not treat a row without an atelier-2 issue URL as an exception", () => {
    expect(
      partitionViolations(
        "workbench",
        [{ id: "button-name", help: "Buttons must have discernible text" }],
        [{ id: "button-name", surface: "workbench", item: "later" }]
      ).unnamed
    ).toHaveLength(1);
  });

  it("does not fail a violation tied to an atelier-2 issue URL, and names that URL", () => {
    const partitioned = partitionViolations(
      "workbench",
      [{ id: "button-name", help: "Buttons must have discernible text" }],
      [{ id: "button-name", surface: "workbench", item: NAMED_ITEM }]
    );
    expect(partitioned.unnamed).toEqual([]);
    expect(partitioned.named).toEqual([`workbench: button-name is named by ${NAMED_ITEM}`]);
  });

  it("does not treat a matching rule on a different surface as named", () => {
    expect(
      partitionViolations(
        "catalog",
        [{ id: "button-name", help: "Buttons must have discernible text" }],
        [{ id: "button-name", surface: "workbench", item: NAMED_ITEM }]
      ).unnamed
    ).toHaveLength(1);
  });

  it("does not treat an incomplete check as a violation", async () => {
    const workbench = distinctServedPages().find((surface) => surface.page === "workbench");
    if (workbench === undefined) throw new Error("SERVED_PATHS opened no Workbench");
    openAt(workbench.path);
    try {
      await waitUntilUp("workbench");
      const scanned = await scanAxe();
      const partitioned = partitionViolations("workbench", scanned.violations, []);
      expect(scanned.incomplete.map((hit) => hit.id)).toContain("color-contrast");
      expect(partitioned.unnamed.join("\n")).not.toContain("color-contrast");
    } finally {
      cleanup();
    }
  });

  it("scans each distinct served page once, including the four rail destinations", () => {
    const pages = distinctServedPages();
    const visited = pages.map((surface) => surface.page);
    expect(visited.filter((page) => page === "workbench")).toHaveLength(1);
    expect(
      SERVED_PATHS.filter((servedPath) => cockpitRoute(instantiate(servedPath)).page === "workbench").length
    ).toBeGreaterThan(1);
    expect(visited).toContain("workflow");
    for (const destination of Object.values(WORKSHOP_DESTINATION)) {
      const route = cockpitRoute(destination.path);
      expect(route.page).not.toBe("not-found");
      expect(visited).toContain(route.page);
    }
  });

  it("fails axe on a rendered room after a control loses its accessible name", async () => {
    try {
      const { unnamed } = await openWorkbenchWithNamelessCatalogLink();
      expect(unnamed).toEqual([
        "workbench: link-name — Links must have discernible text. Fix this on workbench, or attach an atelier-2 issue URL (https://github.com/FlexOr2/atelier-2/issues/<n>) to the same axe rule id and the same surface."
      ]);
    } finally {
      cleanup();
    }
  });

  it("names the item and does not fail when that same defect is tied to an atelier-2 issue URL", async () => {
    try {
      const { violations } = await openWorkbenchWithNamelessCatalogLink();
      const nameless = violations.find((violation) => violation.id === "link-name" || violation.id === "button-name");
      if (nameless === undefined) {
        throw new Error(
          `expected link-name or button-name, got ${violations.map((violation) => violation.id).join(", ") || "no violations"}`
        );
      }
      const partitioned = partitionViolations("workbench", violations, [
        { id: nameless.id, surface: "workbench", item: NAMED_ITEM }
      ]);
      expect(partitioned.unnamed).toEqual([]);
      expect(partitioned.named).toContain(`workbench: ${nameless.id} is named by ${NAMED_ITEM}`);
    } finally {
      cleanup();
    }
  });

  it("waits until Settings has reached Sources, not Settings unavailable", async () => {
    const settings = distinctServedPages().find((surface) => surface.page === "settings");
    if (settings === undefined) throw new Error("SERVED_PATHS opened no Settings");
    openAt(settings.path);
    try {
      await waitUntilUp("settings");
      expect(screen.getByRole("heading", { name: settingsPageCopy.sourcesTitle }).isConnected).toBe(
        true
      );
      expect(screen.queryByText(settingsPageCopy.unavailable)).toBeNull();
    } finally {
      cleanup();
    }
  });

  it("proves(core-surfaces-meet-wcag-or-name-the-item): each distinct served page has no unnamed WCAG 2.2 AA violation", async () => {
    const unnamed: string[] = [];
    const named: string[] = [];
    for (const surface of distinctServedPages()) {
      openAt(surface.path);
      try {
        await waitUntilUp(surface.page);
        const scanned = await scanAxe();
        const partitioned = partitionViolations(surface.page, scanned.violations, NAMED_EXCEPTIONS);
        unnamed.push(...partitioned.unnamed);
        named.push(...partitioned.named);
      } finally {
        cleanup();
      }
    }
    expect(
      named.every((line) => ISSUE_URL.test(line.slice(line.lastIndexOf("https://")))),
      named.join("\n")
    ).toBe(true);
    expect(unnamed, ["unnamed:", ...unnamed, "named:", ...named].join("\n")).toEqual([]);
  });
});
