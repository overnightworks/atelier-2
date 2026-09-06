import { vi, type Mock } from "vitest";

import type {
  CockpitApi,
  HealthResource,
  RunEventHandlers,
  RunPage,
  RunV3,
  SeatResource
} from "../../src/api/client";
import { runRow } from "./runV3";

/** A `GET /health` answer, its own owner so every test names only what it varies. */
export function healthResource(overrides: Partial<HealthResource> = {}): HealthResource {
  return {
    status: "serving",
    source_commit: "a".repeat(40),
    source_tree: "b".repeat(40),
    serve_started_at: "2026-08-31T08:00:00Z",
    ...overrides
  };
}

/** A `GET /seat` answer: a serve holding no seat, unless a test names one. */
export function seatResource(overrides: Partial<SeatResource> = {}): SeatResource {
  return { state: "MISSING", url: null, project_id: null, ...overrides };
}

/**
 * One owner for the CockpitApi test double: when the port grows a method, the
 * suite learns about it here instead of in every app test.
 *
 * Only reads every mount makes unconditionally carry a default -- the neutral
 * "nothing there" page for a list, a neutral `healthResource()` for `health`
 * (App's own mount reads it for the footer's baseline, #1100). Everything a
 * test depends on must be handed in, so no test passes because a shared
 * default happened to answer for it.
 */
export function cockpitApiStub(overrides: Partial<CockpitApi> = {}): CockpitApi {
  return {
    health: vi.fn(async () => healthResource()),
    getSeat: vi.fn(async () => seatResource()),
    listRuns: vi.fn(async () => ({ items: [], next_after: null })),
    listProjects: vi.fn(async () => ({ items: [] })),
    getProjectSourceConnection: vi.fn(),
    listProjectSources: vi.fn(async () => ({ items: [] })),
    connectProjectSource: vi.fn(),
    disconnectProjectSource: vi.fn(),
    rotateProjectSourceToken: vi.fn(),
    getModelRegistry: vi.fn(),
    putModelRegistry: vi.fn(),
    validateModelRegistryEntry: vi.fn(),
    getProjectModelDefaults: vi.fn(),
    putProjectModelDefaults: vi.fn(),
    resolveProjectModels: vi.fn(),
    listWorkflowRevisions: vi.fn(async () => ({ items: [], next_after_revision_hash: null })),
    listAgentConfigurationRevisions: vi.fn(async () => ({
      items: [],
      next_after_revision_hash: null
    })),
    listAuthProfileRevisions: vi.fn(async () => ({
      items: [],
      next_after_revision_hash: null
    })),
    listObservedQueueItems: vi.fn(async () => ({ items: [], next_after: null })),
    listAgentDefinitionRevisions: vi.fn(async () => ({
      items: [],
      next_after_revision_hash: null
    })),
    recognizeLibraryDocument: vi.fn(),
    addLibraryDocument: vi.fn(),
    publishAgentDefinition: vi.fn(),
    getAgentDefinitionRevision: vi.fn(),
    publishArtifact: vi.fn(),
    getRevisionByName: vi.fn(),
    foundCatalogLineage: vi.fn(),
    admitCatalogMember: vi.fn(),
    retireCatalogLineage: vi.fn(),
    publish: vi.fn(),
    publishAuthProfile: vi.fn(),
    publishAgentConfiguration: vi.fn(),
    start: vi.fn(),
    answer: vi.fn(),
    cancelRun: vi.fn(),
    forkRun: vi.fn(),
    getRun: vi.fn(),
    getNodeDetail: vi.fn(),
    getWorkflowRevision: vi.fn(),
    getSchemaRevision: vi.fn(),
    openRunEvents: vi.fn(() => ({ close: vi.fn() })),
    openAttentionEvents: vi.fn(() => ({ close: vi.fn() })),
    ...overrides
  };
}

/** A run event transport the test drives by hand, holding the handlers it was opened with. */
export class FakeRunEventFeed {
  handlers: RunEventHandlers | null = null;
  close = vi.fn();
  open: CockpitApi["openRunEvents"] = vi.fn((_publicReference: string, handlers: RunEventHandlers) => {
    this.handlers = handlers;
    return { close: this.close };
  });
  openAttention: CockpitApi["openAttentionEvents"] = vi.fn((handlers: RunEventHandlers) => {
    this.handlers = handlers;
    return { close: this.close };
  });
}

/** The cursors the paged doubles below hand out, one per page boundary. */
export const PAGE_CURSORS = ["run1.cDE", "run1.cDI", "run1.cDM"];

type ListRunsDouble = Mock<(after?: string) => Promise<RunPage>>;

/**
 * A listRuns double that serves the given pages, each answering the cursor
 * before it, exactly as the durable route does: the last page carries a null
 * cursor. `failFrom` makes that page index unreadable instead.
 */
export function pagedListRuns(
  pages: readonly (readonly RunV3[])[],
  failFrom = -1
): ListRunsDouble {
  return vi.fn(async (after?: string) => {
    const index = after === undefined ? 0 : PAGE_CURSORS.indexOf(after) + 1;
    if (index === failFrom) {
      throw new Error(`page ${index + 1} is unreadable`);
    }
    const page = pages[index] ?? [];
    const next = PAGE_CURSORS[index];
    return {
      items: page.map(runRow),
      next_after: index + 1 < pages.length && next !== undefined ? next : null
    };
  });
}

/** A listRuns double whose cursor never advances -- the durable defect a client must not spin on. */
export function repeatingCursorListRuns(page: readonly RunV3[]): ListRunsDouble {
  const repeated = PAGE_CURSORS[0] ?? null;
  return vi.fn(async () => ({ items: page.map(runRow), next_after: repeated }));
}
