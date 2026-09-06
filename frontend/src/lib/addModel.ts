import type {
  AgentConfigurationInput,
  AgentConfigurationRevisionListItem,
  AuthProfileRevision,
  ExactModelRegistryRevisionWrite,
  ExactProjectModelDefaultsRevisionWrite,
  ModelRegistryRevision,
  ProjectModelDefaultsRevision
} from "../api/client";

export const MODEL_ID = /^\S+$/;

export function trimmedModelId(raw: string): string {
  return raw.trim();
}

export function offeredAccounts(
  profiles: AuthProfileRevision[],
  configurations: AgentConfigurationRevisionListItem[]
): AuthProfileRevision[] {
  return profiles
    .filter((profile) =>
      configurations.some((item) => item.provider_id === profile.provider_id)
    )
    .sort((left, right) => {
      if (left.provider_id !== right.provider_id) {
        return left.provider_id < right.provider_id ? -1 : 1;
      }
      if (left.profile_id !== right.profile_id) {
        return left.profile_id < right.profile_id ? -1 : 1;
      }
      return 0;
    });
}

export function pickExecutorPin(
  providerId: string,
  authProfileRevisionHash: string,
  configurations: AgentConfigurationRevisionListItem[]
): AgentConfigurationRevisionListItem | null {
  const sameProvider = configurations.filter((item) => item.provider_id === providerId);
  if (sameProvider.length === 0) return null;
  const matchingProfile = sameProvider.filter(
    (item) => item.auth_profile_revision_hash === authProfileRevisionHash
  );
  const [firstCandidate, ...remainingCandidates] =
    matchingProfile.length > 0 ? matchingProfile : sameProvider;
  if (firstCandidate === undefined) return null;
  return remainingCandidates.reduce(
    (best, item) =>
      item.agent_configuration_revision_hash < best.agent_configuration_revision_hash
        ? item
        : best,
    firstCandidate
  );
}

export type AddModelPlan =
  | { kind: "invalid-id" }
  | { kind: "no-pin" }
  | {
      kind: "duplicate";
      entry: ModelRegistryRevision["entries"][number];
      registry: ModelRegistryRevision;
    }
  | {
      kind: "create";
      input: AgentConfigurationInput;
      pin: AgentConfigurationRevisionListItem;
      registryWrite: (newHash: string) => ExactModelRegistryRevisionWrite;
    };

export function planAddModel(args: {
  modelId: string;
  profile: AuthProfileRevision;
  configurations: AgentConfigurationRevisionListItem[];
  registries: ModelRegistryRevision[];
}): AddModelPlan {
  const modelId = trimmedModelId(args.modelId);
  if (!MODEL_ID.test(modelId)) {
    return { kind: "invalid-id" };
  }
  for (const registry of args.registries) {
    if (registry.provider_id !== args.profile.provider_id) continue;
    const entry = registry.entries.find((candidate) => candidate.model_id === modelId);
    if (entry !== undefined) {
      return { kind: "duplicate", entry, registry };
    }
  }
  const pin = pickExecutorPin(
    args.profile.provider_id,
    args.profile.auth_profile_revision_hash,
    args.configurations
  );
  if (pin === null) {
    return { kind: "no-pin" };
  }
  const registry = args.registries.find(
    (candidate) => candidate.provider_id === args.profile.provider_id
  );
  const input: AgentConfigurationInput = {
    model: modelId,
    auth_profile_revision_hash: args.profile.auth_profile_revision_hash,
    executor_revision: pin.executor_revision,
    requested_capability: pin.requested_capability
  };
  return {
    kind: "create",
    input,
    pin,
    registryWrite: (newHash) => {
      const entries = [
        ...(registry?.entries ?? []).map((entry) => ({
          model_id: entry.model_id,
          agent_configuration_revision_hash: entry.agent_configuration_revision_hash
        })),
        {
          model_id: modelId,
          agent_configuration_revision_hash: newHash
        }
      ];
      return exactRegistryWrite((registry?.revision_number ?? 0) + 1, entries);
    }
  };
}

export type RowPresentation =
  | "checking"
  | "unknown"
  | "added-checked"
  | "added-not-checked"
  | "none";

export function rowPresentation(
  entry: { source: string; provider_check: string },
  checking: boolean
): RowPresentation {
  if (checking) return "checking";
  if (entry.provider_check === "unknown-at-provider") return "unknown";
  if (entry.source === "operator" && entry.provider_check === "checked") {
    return "added-checked";
  }
  if (entry.source === "operator" && entry.provider_check === "not-checked") {
    return "added-not-checked";
  }
  return "none";
}

function exactRegistryWrite(
  revisionNumber: number,
  entries: ExactModelRegistryRevisionWrite["input"]["entries"]
): ExactModelRegistryRevisionWrite {
  const input = { revision_number: revisionNumber, entries };
  return { input, body: JSON.stringify(input) };
}

export function defaultsAfterRemovingConfigurationHash(
  current: ProjectModelDefaultsRevision | null,
  configurationHash: string
): ExactProjectModelDefaultsRevisionWrite | null {
  if (current === null) return null;
  const remaining = current.defaults.filter(
    (item) => item.agent_configuration_revision_hash !== configurationHash
  );
  if (remaining.length === current.defaults.length) return null;
  const input = { revision_number: current.revision_number + 1, defaults: remaining };
  return { input, body: JSON.stringify(input) };
}

export type RegistryIntent =
  | { kind: "add"; modelId: string; configurationHash: string }
  | { kind: "remove"; configurationHash: string };

export type RebasedRegistryWrite =
  | { kind: "already-true" }
  | { kind: "write"; write: ExactModelRegistryRevisionWrite };

export function rebasedRegistryWrite(
  current: ModelRegistryRevision,
  intent: RegistryIntent
): RebasedRegistryWrite {
  const alreadyPresent = current.entries.some((entry) => {
    if (intent.kind === "add") {
      return (
        entry.model_id === intent.modelId
        && entry.agent_configuration_revision_hash === intent.configurationHash
      );
    }
    return entry.agent_configuration_revision_hash === intent.configurationHash;
  });
  if (intent.kind === "add" && alreadyPresent) return { kind: "already-true" };
  if (intent.kind === "remove" && !alreadyPresent) return { kind: "already-true" };

  const kept = current.entries.map((entry) => ({
    model_id: entry.model_id,
    agent_configuration_revision_hash: entry.agent_configuration_revision_hash
  }));
  const entries = intent.kind === "remove"
    ? kept.filter((entry) => entry.agent_configuration_revision_hash !== intent.configurationHash)
    : [
        ...kept,
        {
          model_id: intent.modelId,
          agent_configuration_revision_hash: intent.configurationHash
        }
      ];
  return { kind: "write", write: exactRegistryWrite(current.revision_number + 1, entries) };
}
