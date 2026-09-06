import { get } from "svelte/store";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type * as VersionState from "../../src/lib/versionState";

const LOADED = { commit: "a".repeat(40), deployedAt: "2026-08-31T08:00:00Z" };

let versionState: typeof VersionState;

beforeEach(async () => {
  vi.resetModules();
  versionState = await import("../../src/lib/versionState");
});

describe("the loaded serve version, compared against later health answers (#1100)", () => {
  it("names the version the page loaded with", () => {
    versionState.recordLoadedVersion(LOADED);

    expect(get(versionState.loadedVersion)).toEqual(LOADED);
    expect(get(versionState.newVersionAvailable)).toBe(false);
  });

  it("flags a mismatch only once a different commit is observed", () => {
    versionState.recordLoadedVersion(LOADED);

    versionState.noteObservedVersion(LOADED);
    expect(get(versionState.newVersionAvailable)).toBe(false);

    versionState.noteObservedVersion({ commit: "b".repeat(40), deployedAt: "2026-09-01T08:00:00Z" });
    expect(get(versionState.newVersionAvailable)).toBe(true);
  });

  it("resets the mismatch on the next fresh load", () => {
    versionState.recordLoadedVersion(LOADED);
    versionState.noteObservedVersion({ commit: "b".repeat(40), deployedAt: "2026-09-01T08:00:00Z" });
    expect(get(versionState.newVersionAvailable)).toBe(true);

    versionState.recordLoadedVersion({ commit: "b".repeat(40), deployedAt: "2026-09-01T08:00:00Z" });

    expect(get(versionState.newVersionAvailable)).toBe(false);
  });

  it("adopts the first observed version as the baseline when the mount read never landed one", () => {
    versionState.noteObservedVersion(LOADED);

    expect(get(versionState.loadedVersion)).toEqual(LOADED);
    expect(get(versionState.newVersionAvailable)).toBe(false);
  });
});
