import { expect, test, type Page } from "@playwright/test";

import { IMPORT_SHEET_KINDS } from "../../src/lib/catalogImport";
import { catalogPageCopy } from "../../src/lib/catalogPageCopy";

/**
 * The operator's own journey through the import doors (#659).
 *
 * "I have no workflow and cannot build one" was the report this room answers,
 * so the proof is the report's own path: open the Catalog, put a file in, see
 * it listed and read that it is startable. Everything below happens
 * through the browser — the only two API calls stage a schema the workflow
 * pins, which no surface publishes and no part of this journey is about.
 */

function scenarioName(stem: string, repetition: number): string {
  return `${stem}-${repetition}`;
}

function agentFile(name: string): string {
  return [
    "---",
    `name: ${name}`,
    "description: Proves an authored agent file reaches the catalog.",
    "model: sonnet",
    "tools: Read, Grep",
    "---",
    "",
    "You write down exactly what the stage asks for.",
    ""
  ].join("\n");
}

async function anyJsonSchema(page: Page): Promise<string> {
  const published = await page.request.post("/atelier/api/v1/schema-revisions", {
    headers: { "content-type": "application/json" },
    data: "true"
  });
  expect([200, 201]).toContain(published.status());
  return (await published.json()).schema_revision_hash as string;
}

function workflowFile(
  schemaHash: string,
  promptText: string | undefined,
  name: string
): string {
  return [
    "format_version: 3",
    `name: ${name}`,
    "nodes:",
    "  - id: ask",
    "    type: wait",
    `    prompt: ${promptText ?? "Is the import door open?"}`,
    "    outputs:",
    "      - name: answer",
    "        schema:",
    "          ref: answer-schema",
    `          revision: ${schemaHash}`,
    ""
  ].join("\n");
}

/** The entry the catalog draws for one published name. */
function entry(page: Page, name: string) {
  return page.getByRole("listitem").filter({ hasText: name });
}

async function importInto(
  page: Page,
  fileName: string,
  document: string,
  kind: string
): Promise<void> {
  const opener = page.getByRole("button", { name: catalogPageCopy.import });
  await opener.click();
  await expect(page.getByRole("dialog", { name: catalogPageCopy.import })).toHaveCount(0);
  const chooseFile = () =>
    page.getByLabel(catalogPageCopy.filePicker).setInputFiles({
      name: fileName,
      mimeType: "application/octet-stream",
      buffer: Buffer.from(document)
    });
  await chooseFile();
  const sheet = page.getByRole("dialog", { name: catalogPageCopy.import });
  await expect(sheet).toBeVisible();

  // Escape closes the sheet onto the door that opened it (REQ-UIQ-05), not
  // onto the hidden file input the click passed through.
  await page.keyboard.press("Escape");
  await expect(sheet).toHaveCount(0);
  await expect(opener).toBeFocused();

  await opener.click();
  await chooseFile();
  await expect(sheet).toBeVisible();
  await sheet.getByRole("button", { name: kind, exact: true }).click();
  await sheet.getByRole("button", { name: catalogPageCopy.addToCatalog }).click();
  await expect(sheet).toHaveCount(0);
}

test("proves(the-operator-imports-a-workflow-and-an-agent-and-starts-what-was-imported): the catalog import sheet carries a file from disk to startable", async ({
  page
}, testInfo) => {
  const workflowName = scenarioName("catalog-import-proof", testInfo.repeatEachIndex);
  const agentName = scenarioName("catalog-import-scribe", testInfo.repeatEachIndex);
  const schemaHash = await anyJsonSchema(page);

  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/atelier");
  const catalogLink = page.getByRole("navigation", { name: "Workshop" }).getByRole("link", { name: "Catalog" });
  await catalogLink.click();
  await expect(page.getByRole("heading", { name: catalogPageCopy.title })).toBeVisible();
  await expect(catalogLink).toBeInViewport();
  await expect(entry(page, workflowName)).toHaveCount(0);
  await expect(entry(page, agentName)).toHaveCount(0);

  await importInto(
    page,
    `${workflowName}.yaml`,
    workflowFile(schemaHash, undefined, workflowName),
    catalogPageCopy.kindWorkflow
  );
  await expect(entry(page, workflowName)).toBeVisible();
  // A file dropped in by hand carries no source, so its card wears no
  // provenance pill -- a chip every card wore would distinguish nothing.
  await expect(entry(page, workflowName).locator(".tile-pill:not(.tile-status)")).toHaveCount(0);

  await importInto(page, `${agentName}.agent.md`, agentFile(agentName), catalogPageCopy.kindAgent);
  await expect(entry(page, agentName)).toBeVisible();
  // An imported agent belongs to the provider whose format it arrived in, and
  // the row says so instead of implying it runs anywhere.
  await expect(entry(page, agentName).getByLabel(catalogPageCopy.agentProviderClaude)).toBeVisible();

  const catalogGroups = page.getByRole("group", { name: catalogPageCopy.catalogGroups });
  // The picture makes All the reset action, not a tally; only the three
  // concrete kinds carry their catalog counts.
  const filterChips = catalogGroups.locator(".filter-chip");
  await expect(filterChips.nth(0)).toHaveText(catalogPageCopy.all);
  const workflowTileCount = await page
    .getByRole("region", { name: catalogPageCopy.workflowsTitle })
    .getByRole("listitem")
    .count();
  const agentTileCount = await page
    .getByRole("region", { name: catalogPageCopy.agentsByProvider })
    .getByRole("listitem")
    .count();
  await expect(filterChips.nth(1)).toHaveText(
    `${catalogPageCopy.workflowsTitle}${workflowTileCount}`
  );
  await expect(filterChips.nth(2)).toHaveText(
    `${catalogPageCopy.agentsTitle}${agentTileCount}`
  );
  await expect(filterChips.nth(3)).toHaveText(new RegExp(`^${catalogPageCopy.skillsTitle}0$`));
  await catalogGroups.getByRole("button", { name: /^Agents/ }).click();
  await expect(entry(page, agentName)).toBeVisible();
  await expect(entry(page, workflowName)).toHaveCount(0);
  await page.getByLabel(catalogPageCopy.searchLabel).fill(agentName);
  await expect(entry(page, agentName)).toBeVisible();
  await catalogGroups.getByRole("button", { name: /^Skills/ }).click();
  await expect(page.getByText(catalogPageCopy.skillsNone)).toBeVisible();
  await catalogGroups.getByRole("button", { name: /^All/ }).click();
  await page.getByLabel(catalogPageCopy.searchLabel).fill("");

  // The admission is durable, not a screen state: a cold load of the room
  // reads the admitted entry back and offers its enabled manual start door.
  await page.reload();
  await expect(entry(page, workflowName).getByText(catalogPageCopy.newerRevision)).toHaveCount(0);
  await expect(page.getByText(catalogPageCopy.notAdmittedHint)).toHaveCount(0);
  await entry(page, workflowName).getByRole("link", { name: workflowName }).click();
  await expect(page.getByRole("heading", { name: workflowName })).toBeVisible();
  await expect(page.getByRole("button", { name: catalogPageCopy.start })).toBeEnabled();
});

test("an unadmitted sibling of an admitted name shows as a newer revision, not a second card", async ({
  page
}, testInfo) => {
  const workflowName = scenarioName("catalog-lineage-proof", testInfo.repeatEachIndex);
  const schemaHash = await anyJsonSchema(page);

  await page.goto("/atelier/catalog");
  await importInto(
    page,
    `${workflowName}.yaml`,
    workflowFile(schemaHash, undefined, workflowName),
    catalogPageCopy.kindWorkflow
  );

  // A second, unadmitted revision is published under the same name -- the
  // live duplicate-card finding (#659): the room must not draw a second
  // "catalog-lineage-proof" card for it.
  const published = await page.request.post("/atelier/api/v1/workflow-revisions", {
    headers: { "content-type": "application/yaml" },
    data: workflowFile(schemaHash, "Is the door still open?", workflowName)
  });
  expect([200, 201]).toContain(published.status());
  await page.reload();

  await expect(page.getByRole("listitem").filter({ hasText: workflowName })).toHaveCount(1);
  await expect(entry(page, workflowName).getByText(catalogPageCopy.newerRevision)).toBeVisible();
});

async function openUncertainSheet(page: Page, fileName: string, document: string): Promise<void> {
  await page.getByLabel(catalogPageCopy.filePicker).setInputFiles({
    name: fileName,
    mimeType: "application/octet-stream",
    buffer: Buffer.from(document)
  });
  const sheet = page.getByRole("dialog", { name: catalogPageCopy.import });
  await expect(sheet).toBeVisible();
  await expect(sheet.getByText(fileName)).toBeVisible();
  await expect(sheet.getByRole("button", { name: catalogPageCopy.cancel })).toBeVisible();
  await expect(sheet.getByRole("button", { name: catalogPageCopy.close })).toHaveCount(0);
  const add = sheet.getByRole("button", { name: catalogPageCopy.addToCatalog });
  await expect(add).toBeDisabled();
  await expect(sheet.getByText(catalogPageCopy.noKindDeclared)).toBeVisible();
  await expect(sheet.getByRole("button", { name: catalogPageCopy.kindWorkflow, exact: true })).toBeVisible();
  await expect(sheet.getByRole("button", { name: catalogPageCopy.kindAgent, exact: true })).toBeVisible();
  await expect(sheet.getByRole("group", { name: catalogPageCopy.kind }).getByRole("button")).toHaveCount(
    IMPORT_SHEET_KINDS.length
  );
}

test("an uncertain file asks for a kind instead of closing", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/atelier/catalog");
  await expect(page.getByRole("heading", { name: catalogPageCopy.title })).toBeVisible();
  await openUncertainSheet(page, "notes.md", "a note\n");
  await expect(page.getByText("Choose a file", { exact: true })).toHaveCount(0);
  const sheet = page.getByRole("dialog", { name: catalogPageCopy.import });
  await sheet.getByRole("button", { name: catalogPageCopy.kindWorkflow, exact: true }).click();
  await sheet.getByRole("button", { name: catalogPageCopy.addToCatalog }).click();
  await expect(sheet.getByText(catalogPageCopy.notAWorkflow)).toBeVisible();
});

test("a mistaken kind stays on the sheet and leaves the catalog empty", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto("/atelier/catalog");
  await expect(page.getByRole("heading", { name: catalogPageCopy.title })).toBeVisible();
  await openUncertainSheet(page, "notes.md", "a note\n");
  const sheet = page.getByRole("dialog", { name: catalogPageCopy.import });
  await sheet.getByRole("button", { name: catalogPageCopy.kindWorkflow, exact: true }).click();
  await sheet.getByRole("button", { name: catalogPageCopy.addToCatalog }).click();
  await expect(sheet.getByText(catalogPageCopy.notAWorkflow)).toBeVisible();
  await expect(sheet.getByRole("button", { name: catalogPageCopy.addToCatalog })).toBeVisible();
  await expect(sheet.getByRole("button", { name: catalogPageCopy.cancel })).toBeVisible();
  await expect(entry(page, "notes.md")).toHaveCount(0);
});

test("the catalog room is composed, not squeezed, at 390 pixels", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/atelier/catalog");
  await expect(page.getByRole("heading", { name: catalogPageCopy.title })).toBeVisible();

  const navigation = page.getByRole("navigation", { name: "Workshop" });
  await expect(navigation).toBeVisible();
  await expect(navigation.getByRole("link")).toHaveCount(4);
  await expect(page.getByRole("button", { name: catalogPageCopy.import })).toBeInViewport();

  // The phone rail puts every destination in its grid's first row. Prove that
  // layout contract, rather than measuring text-dependent pixel offsets.
  const railLayout = await navigation.evaluate((element) => ({
    display: getComputedStyle(element).display,
    clientWidth: element.clientWidth,
    scrollWidth: element.scrollWidth
  }));
  expect(railLayout.display).toBe("grid");
  expect(railLayout.scrollWidth).toBeLessThanOrEqual(railLayout.clientWidth);
  expect(
    await navigation.getByRole("link").evaluateAll((links) =>
      links.map((link) => getComputedStyle(link).gridRowStart)
    )
  ).toEqual(["1", "1", "1", "1"]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
});
