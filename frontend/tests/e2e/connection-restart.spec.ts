import { expect, test } from "@playwright/test";

import { restartNoticeCopy } from "../../src/lib/connectionState";

const widths = [
  { name: "desktop", width: 1280, height: 900 },
  { name: "390", width: 390, height: 844 }
] as const;

/**
 * The honest restart notice against a real host redeploy (#700 stage 1).
 *
 * `/__e2e/recompose` is the harness's own stand-in for the host process kill
 * a redeploy performs (`tests/e2e/serve_cockpit.py`): it stops the live
 * server and brings a fresh one up behind the same port, the same shape as a
 * production restart -- plain (no `?reset=true`), because this proof is
 * about the restart itself, not about the server's seeded state (#742: this
 * suite's server is shared across spec files that may run in any order).
 *
 * The proof stays off any page holding an open durable-event stream (a run
 * cockpit): the harness's graceful shutdown only disables
 * keep-alive on an in-flight connection, it never closes one still
 * streaming, so an open `EventSource` at the moment of the restart would
 * hang `/__e2e/recompose` forever. The restart itself is asked for over HTTP
 * and the harness drops its sockets after a one-second grace
 * (`RESTART_CONNECTION_GRACE_SECONDS`), so the Workbench's own attention
 * stream cannot wedge it (#1114).
 *
 * The Workbench no longer speaks its own connection state -- its ear became
 * the terminal seat (#1099) -- so the one restart line is the shell's own
 * banner, and what this test owns is that the banner appears on the open room
 * and clears itself once the connection recovers, with no reload. A full app
 * restart proven recoverable here leaves the server equally healthy for
 * whatever spec runs next, in whatever order that is.
 */
test("shows the calm restart line on the open workbench, and clears it on its own with no reload", async ({ page }) => {
  test.setTimeout(120_000);

  const reset = await page.request.post("/__e2e/recompose?reset=true");
  expect(reset.status()).toBe(202);
  const resetGeneration = await reset.text();
  await expect(async () => {
    expect(await (await page.request.get("/__e2e/generation")).text()).toBe(resetGeneration);
  }).toPass({ timeout: 20_000 });
  await page.goto("/atelier/chat");
  await expect(page.getByRole("heading", { name: "Workbench" })).toBeVisible();

  const restarted = await page.request.post("/__e2e/recompose");
  expect(restarted.status()).toBe(202);
  const expectedGeneration = await restarted.text();

  // Confirm the old listener is actually down before asking the SPA to do
  // anything -- otherwise the very next click could race a socket that has
  // not closed yet and land on the old server, proving nothing.
  await expect(async () => {
    await expect(page.request.get("/__e2e/generation")).rejects.toThrow();
  }).toPass({ timeout: 15_000 });

  // An in-app navigation (no reload) whose own mount read now fails for
  // real: the one round trip that discovers the outage.
  await page
    .getByRole("navigation", { name: "Workshop" })
    .getByRole("link", { name: "Catalog", exact: true })
    .click();

  // `status` computes its accessible name from an explicit label only, never
  // from its own content (ARIA's nameFrom:author for this role) -- the same
  // reason the rest of this suite matches a status region by its text, not
  // by name.
  const notice = page.getByRole("status").filter({ hasText: "restarting" });
  await expect(notice).toBeVisible({ timeout: 10_000 });
  await expect(notice).toContainText(restartNoticeCopy);

  // `position: sticky` never held here in the first place (broken by
  // `html,body{overflow-x:hidden}`); `position: fixed` does not scroll away
  // either, which sticky at least tries to promise -- proven against a real
  // scroll, not just a static screenshot. `.workshop-stage`, not the page,
  // is the room's own scroll container, so a tall spacer forces real
  // overflow there before scrolling it.
  await page.evaluate(() => {
    const spacer = document.createElement("div");
    spacer.style.height = "2000px";
    spacer.dataset.e2eScrollSpacer = "true";
    document.querySelector(".workshop-stage")?.appendChild(spacer);
  });
  await page.locator(".workshop-stage").evaluate((stage) => {
    stage.scrollTop = 800;
  });
  await expect
    .poll(() => page.locator(".workshop-stage").evaluate((stage) => stage.scrollTop))
    .toBeGreaterThan(0);
  const bannerRect = await notice.boundingBox();
  expect(bannerRect?.y).toBe(0);
  expect(bannerRect?.width).toBe(await page.evaluate(() => window.innerWidth));
  await page.locator("[data-e2e-scroll-spacer]").evaluate((spacer) => spacer.remove());
  await page.locator(".workshop-stage").evaluate((stage) => {
    stage.scrollTop = 0;
  });

  // The shell's own top banner, the one line every room shows: the fixed
  // evidence below covers it.
  for (const viewport of widths) {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await page.screenshot({
      path: test.info().outputPath(`connection-restart-banner-${viewport.name}.png`),
      fullPage: true
    });
  }
  await page.setViewportSize({ width: 1280, height: 900 });

  // Back on the workbench the issue names, with no network call of its own:
  // it already reads the one central store, and the one restart line stands
  // above it like above every other room.
  await page.getByRole("link", { name: "Workbench" }).click();
  await expect(page.getByRole("heading", { name: "Workbench" })).toBeVisible();
  await expect(notice).toContainText(restartNoticeCopy);

  for (const viewport of widths) {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await page.screenshot({
      path: test.info().outputPath(`connection-restart-notice-${viewport.name}.png`),
      fullPage: true
    });
  }
  await page.setViewportSize({ width: 1280, height: 900 });

  // The harness is fully back once its generation counter matches what the
  // restart promised -- the same proof `cockpit.spec.ts` already trusts for
  // this endpoint.
  await expect(async () => {
    expect(await (await page.request.get("/__e2e/generation")).text()).toBe(expectedGeneration);
  }).toPass({ timeout: 20_000 });

  // No page.reload() anywhere above: the notice clearing on its own is the
  // automatic recovery itself.
  await expect(notice).toBeHidden({ timeout: 20_000 });
});

/**
 * A redeploy takes its open sockets with it, and the harness must too (#1114).
 *
 * Every room that listens holds a server-sent stream: the Workbench keeps
 * `GET /events` open for as long as it is on screen (`lib/attentionHold.ts`),
 * and a stream by its nature never finishes. Uvicorn's graceful shutdown waits
 * out every live connection, so `/__e2e/recompose` used to park the whole
 * shared harness on "Waiting for connections to close" until some later step
 * happened to navigate the stream away -- a wedge every spec after it paid
 * for. Nothing here closes the stream on the server's behalf: no navigation,
 * no reload, no page close. The restart has to drop it itself.
 */
test("restarts under an open ear, with nothing but the restart to close the stream", async ({ page }) => {
  test.setTimeout(120_000);

  const attentionStreamOpened = page.waitForResponse(
    (response) => response.url().endsWith("/atelier/api/v1/events") && response.ok()
  );
  await page.goto("/atelier/chat");
  await expect(page.getByRole("heading", { name: "Workbench" })).toBeVisible();
  await attentionStreamOpened;

  const restarted = await page.request.post("/__e2e/recompose");
  expect(restarted.status()).toBe(202);
  const expectedGeneration = await restarted.text();

  await expect(async () => {
    expect(await (await page.request.get("/__e2e/generation")).text()).toBe(expectedGeneration);
  }).toPass({ timeout: 20_000 });
});
