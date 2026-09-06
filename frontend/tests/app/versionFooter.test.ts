import type * as SvelteTestingLibrary from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { CockpitApi } from "../../src/api/client";
import { exactLocal } from "../../src/lib/when";
import { railCopy } from "../../src/lib/railCopy";
import { healthResource } from "../support/cockpitApi";

vi.mock("../../src/lib/pageReload", () => ({ reloadPage: vi.fn() }));

let testingLibrary: typeof SvelteTestingLibrary;
let openWorkshop: (health: CockpitApi["health"]) => ReturnType<(typeof SvelteTestingLibrary)["render"]>;
let reportConnectionLost: () => void;
let reportConnectionRestored: () => void;
let reloadPage: () => void;

/**
 * `App` closes over `versionState.ts` and `connectionState.ts`'s
 * module-level stores, so a fresh render needs a fresh module graph rather
 * than a manual reset -- `vi.resetModules()` plus a fresh dynamic import of
 * testing-library alongside the app keeps every piece bound to the same
 * reloaded Svelte runtime; mixing a freshly reset component with a stale
 * `render` from a different runtime instance fails.
 */
beforeEach(async () => {
  vi.resetModules();
  const library = await import("@testing-library/svelte");
  const { default: App } = await import("../../src/App.svelte");
  const { MutationJournal } = await import("../../src/lib/mutationJournal");
  const { cockpitApiStub } = await import("../support/cockpitApi");
  const connection = await import("../../src/lib/connectionState");
  const pageReload = await import("../../src/lib/pageReload");

  testingLibrary = library;
  openWorkshop = (health) => {
    window.history.replaceState(null, "", "/atelier");
    return library.render(App, {
      props: {
        cockpitApi: cockpitApiStub({ health }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });
  };
  reportConnectionLost = connection.reportConnectionLost;
  reportConnectionRestored = connection.reportConnectionRestored;
  reloadPage = pageReload.reloadPage;
});

afterEach(() => {
  vi.restoreAllMocks();
  testingLibrary.cleanup();
  window.history.replaceState(null, "", "/atelier");
});

describe("the workshop shell's footer names the running serve (#1100)", () => {
  it("shows the short commit, its full hash on request, and the deploy time it loaded with", async () => {
    const { screen } = testingLibrary;
    const commit = "1234567890abcdef1234567890abcdef12345678";
    const health = vi.fn().mockResolvedValue(
      healthResource({ source_commit: commit, serve_started_at: "2026-08-31T08:00:00Z" })
    );
    openWorkshop(health);

    const footer = await screen.findByText(/12345678/, { exact: false });

    expect(footer.textContent).toContain(exactLocal("2026-08-31T08:00:00Z"));
    expect(footer.title).toBe(commit);
  });

  it("says nothing about the serve until the loaded version is known", () => {
    const { container } = openWorkshop(vi.fn(() => new Promise<never>(() => {})));

    expect(container.querySelector(".serve-footer")).toBeNull();
  });

  it("records the baseline once recovery's health succeeds after the mount read failed", async () => {
    const { screen } = testingLibrary;
    const commit = "d".repeat(40);
    const health = vi
      .fn()
      .mockRejectedValueOnce(new Error("restarting"))
      .mockResolvedValueOnce(healthResource({ source_commit: commit }));
    openWorkshop(health);

    reportConnectionLost();
    reportConnectionRestored();

    expect(await screen.findByTitle(commit)).not.toBeNull();
    expect(screen.queryByText(railCopy.newVersionAvailable)).toBeNull();
  });

  it("announces a new version once a reconnect's health answers a different commit, without reloading on its own", async () => {
    const { screen } = testingLibrary;
    const health = vi
      .fn()
      .mockResolvedValueOnce(healthResource({ source_commit: "a".repeat(40) }))
      .mockResolvedValueOnce(healthResource({ source_commit: "b".repeat(40) }));
    openWorkshop(health);
    await screen.findByTitle("a".repeat(40));

    reportConnectionLost();
    reportConnectionRestored();

    const notice = await screen.findByText(railCopy.newVersionAvailable);
    expect(notice.closest(".serve-footer")?.textContent).toContain(railCopy.newVersionAvailable);
    expect(reloadPage).not.toHaveBeenCalled();
    expect(screen.queryByTitle("a".repeat(40))).toBeNull();

    screen.getByRole("button", { name: railCopy.reload }).click();
    expect(reloadPage).toHaveBeenCalledOnce();
  });

  it("keeps naming the loaded commit across a reconnect that answers the same commit", async () => {
    const { screen } = testingLibrary;
    const sameCommit = "c".repeat(40);
    const health = vi.fn().mockResolvedValue(healthResource({ source_commit: sameCommit }));
    openWorkshop(health);
    await screen.findByTitle(sameCommit);

    reportConnectionLost();
    reportConnectionRestored();

    expect(await screen.findByTitle(sameCommit)).not.toBeNull();
    expect(screen.queryByText(railCopy.newVersionAvailable)).toBeNull();
  });
});
