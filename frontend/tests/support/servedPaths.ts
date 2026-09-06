import servedPaths from "../../src/lib/servedPaths.json";

/**
 * The frozen list `route.ts` and the server both read: exercised here as the
 * exhaustive set every cold-load test walks, never a second copy of it.
 */
export const SERVED_PATHS: readonly string[] = servedPaths;

/** Where a run's public reference stands in a served path. */
export const PUBLIC_REFERENCE_PLACEHOLDER = "{public_ref}";

/** Where a workflow's name stands in a served path. */
export const WORKFLOW_NAME_PLACEHOLDER = "{workflow_name:path}";
