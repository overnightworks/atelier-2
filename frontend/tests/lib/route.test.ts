import { describe, expect, it } from "vitest";
import {
  cockpitRoute,
  runPath,
  workflowPath,
  type CockpitRoute
} from "../../src/lib/route";
import {
  PUBLIC_REFERENCE_PLACEHOLDER,
  SERVED_PATHS,
  WORKFLOW_NAME_PLACEHOLDER
} from "../support/servedPaths";

const SAMPLE_PUBLIC_REFERENCE = "run1.cnVu";
const SAMPLE_WORKFLOW_NAME = "iterate-code";

/**
 * Every page this router can open, and whether a served path must reach it on a
 * cold load. Total over the router's own union, so a page added there without a
 * decision here is a type error rather than a level that survives a click and
 * dies on a reload -- the defect this file exists for, one edge further out.
 */
const REACHED_COLD: Record<CockpitRoute["page"], boolean> = {
  workbench: true,
  settings: true,
  workflow: true,
  catalog: true,
  history: true,
  run: true,
  "not-found": false
};

function coldLoad(servedPath: string): CockpitRoute {
  return cockpitRoute(
    servedPath
      .replace(PUBLIC_REFERENCE_PLACEHOLDER, SAMPLE_PUBLIC_REFERENCE)
      .replace(WORKFLOW_NAME_PLACEHOLDER, SAMPLE_WORKFLOW_NAME)
  );
}

describe("the paths the server is asked to serve", () => {
  /**
   * The declaration is only worth reading if it cannot promise a path this
   * router would answer with "not found". Together with the host test that
   * serves exactly this file, a level is either reachable cold from both sides
   * or red on one of them.
   */
  it.each([...SERVED_PATHS])(
    "proves(a-level-opens-from-a-pasted-link-and-survives-a-reload): %s opens a page rather than nothing",
    (servedPath) => {
      expect(coldLoad(servedPath).page).not.toBe("not-found");
    }
  );

  it("declares a path for every page this router can open", () => {
    const opened = new Set(SERVED_PATHS.map((servedPath) => coldLoad(servedPath).page));

    expect([...opened].sort()).toEqual(
      Object.entries(REACHED_COLD)
        .filter(([, reached]) => reached)
        .map(([page]) => page)
        .sort()
    );
  });

  it("opens Settings on its canonical address", () => {
    expect(SERVED_PATHS).toContain("/atelier/settings");
    expect(cockpitRoute("/atelier/settings")).toEqual({ page: "settings" });
  });

  it("opens the Workbench on the workshop root, where the Board used to stand", () => {
    expect(SERVED_PATHS).toContain("/atelier");
    expect(cockpitRoute("/atelier")).toEqual({ page: "workbench" });
    expect(cockpitRoute("/atelier/chat")).toEqual({ page: "workbench" });
  });

  it("still answers an unknown path with not-found", () => {
    expect(cockpitRoute("/atelier/nowhere").page).toBe("not-found");
  });

  it("opens the Catalog only on its canonical address", () => {
    expect(SERVED_PATHS).toContain("/atelier/catalog");
    expect(cockpitRoute("/atelier/catalog")).toEqual({ page: "catalog" });
    expect(cockpitRoute("/atelier/workflows")).toEqual({ page: "not-found" });
  });

  it("opens History on its own path", () => {
    expect(SERVED_PATHS).toContain("/atelier/history");
    expect(cockpitRoute("/atelier/history")).toEqual({ page: "history" });
  });

  it("round-trips a workflow name a person actually publishes, spaces and a detail path included", () => {
    const name = "Probefahrt am frischen Haus/detail";

    expect(cockpitRoute(workflowPath(name))).toEqual({ page: "workflow", name });
  });

  it("keeps the remaining detail path in a pasted workflow address", () => {
    expect(cockpitRoute("/atelier/catalog/catalog/detail")).toEqual({
      page: "workflow",
      name: "catalog/detail"
    });
  });

  it("answers a malformed percent-encoding in a workflow path with not-found", () => {
    expect(cockpitRoute("/atelier/catalog/%").page).toBe("not-found");
  });
});

describe("a run's own address", () => {
  it("opens the run page from the link every room builds", () => {
    expect(cockpitRoute(runPath(SAMPLE_PUBLIC_REFERENCE))).toEqual({
      page: "run",
      publicReference: SAMPLE_PUBLIC_REFERENCE
    });
  });

  // Links carrying the old "?from=chat" marker are still in browser histories;
  // they open the same run rather than a broken page.
  it("ignores a query a bookmarked link still carries", () => {
    expect(cockpitRoute(`/atelier/runs/${SAMPLE_PUBLIC_REFERENCE}?from=chat`)).toEqual({
      page: "run",
      publicReference: SAMPLE_PUBLIC_REFERENCE
    });
  });
});
