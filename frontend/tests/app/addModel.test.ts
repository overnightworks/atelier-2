import { describe, expect, it } from "vitest";

import type {
  AgentConfigurationRevisionListItem,
  AuthProfileRevision,
  ModelRegistryRevision,
  ProjectModelDefaultsRevision
} from "../../src/api/client";
import {
  MODEL_ID,
  defaultsAfterRemovingConfigurationHash,
  offeredAccounts,
  pickExecutorPin,
  planAddModel,
  rebasedRegistryWrite,
  rowPresentation,
  trimmedModelId
} from "../../src/lib/addModel";

const PROFILE_HASH = "b".repeat(64);
const OTHER_PROFILE_HASH = "c".repeat(64);
const PIN_HASH = "d".repeat(64);
const SMALLER_HASH = "a".repeat(64);
const LARGER_HASH = "f".repeat(64);
const REGISTRY_HASH = "e".repeat(64);
const NEW_HASH = "9".repeat(64);

function profile(overrides: Partial<AuthProfileRevision> = {}): AuthProfileRevision {
  return {
    profile_id: "operator-anthropic-subscription",
    revision_number: 1,
    provider_id: "anthropic",
    auth_mode: "subscription",
    auth_profile_revision_hash: PROFILE_HASH,
    ...overrides
  };
}

function configuration(
  overrides: Partial<AgentConfigurationRevisionListItem> = {}
): AgentConfigurationRevisionListItem {
  return {
    model: "claude-opus-4-6",
    auth_profile_revision_hash: PROFILE_HASH,
    executor_revision: "claude-atelier-doors/v1",
    requested_capability: "headless_with_tools",
    provider_id: "anthropic",
    auth_mode: "subscription",
    agent_configuration_revision_hash: PIN_HASH,
    startable: true,
    structurally_startable: true,
    not_startable_reason: null,
    provider_probe_problem_code: null,
    provider_probe_observed_at: null,
    ...overrides
  };
}

function entry(
  overrides: Partial<ModelRegistryRevision["entries"][number]> = {}
): ModelRegistryRevision["entries"][number] {
  return {
    model_id: "claude-opus-4-6",
    agent_configuration_revision_hash: PIN_HASH,
    source: "operator",
    provider_check: "checked",
    ...overrides
  };
}

function registry(
  overrides: Partial<ModelRegistryRevision> = {}
): ModelRegistryRevision {
  return {
    provider_id: "anthropic",
    revision_number: 1,
    model_registry_revision_hash: REGISTRY_HASH,
    entries: [entry()],
    ...overrides
  };
}

function hashFor(index: number): string {
  return index.toString(16).padStart(64, "0");
}

describe("the model id the add door accepts", () => {
  it("trims and accepts a non-whitespace id", () => {
    expect(trimmedModelId("  grok-4.6  ")).toBe("grok-4.6");
    expect(MODEL_ID.test(trimmedModelId("  grok-4.6  "))).toBe(true);
    expect(MODEL_ID.test(trimmedModelId("claude-opus-4-6"))).toBe(true);
  });

  it("refuses an empty, blank, or spaced id", () => {
    expect(MODEL_ID.test(trimmedModelId(""))).toBe(false);
    expect(MODEL_ID.test(trimmedModelId("   "))).toBe(false);
    expect(MODEL_ID.test(trimmedModelId("foo bar"))).toBe(false);
    expect(
      planAddModel({
        modelId: "  ",
        profile: profile(),
        configurations: [configuration()],
        registries: []
      })
    ).toEqual({ kind: "invalid-id" });
    expect(
      planAddModel({
        modelId: "not a model",
        profile: profile(),
        configurations: [configuration()],
        registries: []
      })
    ).toEqual({ kind: "invalid-id" });
  });
});

describe("which accounts the add door offers", () => {
  it("keeps profiles that share a provider with a configuration and sorts them", () => {
    const xai = profile({
      profile_id: "z-grok",
      provider_id: "xai",
      auth_profile_revision_hash: OTHER_PROFILE_HASH
    });
    const laterAnthropic = profile({ profile_id: "n-account" });
    const earlierAnthropic = profile({ profile_id: "a-account" });
    const unused = profile({
      profile_id: "no-config",
      provider_id: "openai",
      auth_profile_revision_hash: "1".repeat(64)
    });

    expect(
      offeredAccounts(
        [xai, laterAnthropic, unused, earlierAnthropic],
        [
          configuration({ provider_id: "xai", auth_profile_revision_hash: OTHER_PROFILE_HASH }),
          configuration()
        ]
      )
    ).toEqual([earlierAnthropic, laterAnthropic, xai]);
  });
});

describe("the executor pin an add copies", () => {
  it("prefers a configuration on the same auth profile", () => {
    const matching = configuration({
      agent_configuration_revision_hash: LARGER_HASH,
      executor_revision: "matching/v1"
    });
    const smallerOtherProfile = configuration({
      auth_profile_revision_hash: OTHER_PROFILE_HASH,
      agent_configuration_revision_hash: SMALLER_HASH,
      executor_revision: "other/v1"
    });

    expect(
      pickExecutorPin("anthropic", PROFILE_HASH, [smallerOtherProfile, matching])
    ).toBe(matching);
  });

  it("falls back to the lexicographically smallest hash on the provider", () => {
    const larger = configuration({
      auth_profile_revision_hash: OTHER_PROFILE_HASH,
      agent_configuration_revision_hash: LARGER_HASH
    });
    const smaller = configuration({
      auth_profile_revision_hash: "8".repeat(64),
      agent_configuration_revision_hash: SMALLER_HASH,
      executor_revision: "smallest/v1"
    });

    expect(pickExecutorPin("anthropic", PROFILE_HASH, [larger, smaller])).toBe(smaller);
  });

  it("returns null when no configuration shares the provider", () => {
    expect(
      pickExecutorPin("anthropic", PROFILE_HASH, [
        configuration({ provider_id: "xai", auth_profile_revision_hash: OTHER_PROFILE_HASH })
      ])
    ).toBeNull();
  });

  it("returns null for an empty configuration list instead of throwing", () => {
    expect(pickExecutorPin("anthropic", PROFILE_HASH, [])).toBeNull();
  });
});

describe("planning an add", () => {
  it("names a duplicate on the same provider and id", () => {
    const existing = entry({ model_id: "grok-4.6" });
    const sameProvider = registry({ entries: [existing] });
    const plan = planAddModel({
      modelId: "  grok-4.6  ",
      profile: profile(),
      configurations: [configuration()],
      registries: [sameProvider]
    });

    expect(plan).toEqual({
      kind: "duplicate",
      entry: existing,
      registry: sameProvider
    });
  });

  it("does not treat the same id on another provider as a duplicate", () => {
    const plan = planAddModel({
      modelId: "grok-4.6",
      profile: profile(),
      configurations: [configuration()],
      registries: [
        registry({
          provider_id: "xai",
          entries: [entry({ model_id: "grok-4.6" })]
        })
      ]
    });

    expect(plan.kind).toBe("create");
  });

  it("refuses when no pin exists", () => {
    expect(
      planAddModel({
        modelId: "grok-4.6",
        profile: profile(),
        configurations: [configuration({ provider_id: "xai" })],
        registries: []
      })
    ).toEqual({ kind: "no-pin" });
  });

  it("copies the pin fields and writes the next registry revision with an appended entry", () => {
    const pin = configuration({
      executor_revision: "pin/v2",
      requested_capability: "interactive"
    });
    const existing = entry({
      model_id: "claude-opus-4-6",
      agent_configuration_revision_hash: PIN_HASH
    });
    const current = registry({ revision_number: 4, entries: [existing] });
    const plan = planAddModel({
      modelId: "  grok-4.6  ",
      profile: profile(),
      configurations: [pin],
      registries: [current]
    });

    expect(plan.kind).toBe("create");
    if (plan.kind !== "create") return;
    expect(plan.pin).toBe(pin);
    expect(plan.input).toEqual({
      model: "grok-4.6",
      auth_profile_revision_hash: PROFILE_HASH,
      executor_revision: "pin/v2",
      requested_capability: "interactive"
    });

    const write = plan.registryWrite(NEW_HASH);
    const input = {
      revision_number: 5,
      entries: [
        {
          model_id: "claude-opus-4-6",
          agent_configuration_revision_hash: PIN_HASH
        },
        {
          model_id: "grok-4.6",
          agent_configuration_revision_hash: NEW_HASH
        }
      ]
    };
    expect(write.input).toEqual(input);
    expect(write.body).toBe(JSON.stringify(input));
  });

  it("starts a missing registry at revision 1", () => {
    const plan = planAddModel({
      modelId: "grok-4.6",
      profile: profile(),
      configurations: [configuration()],
      registries: []
    });

    expect(plan.kind).toBe("create");
    if (plan.kind !== "create") return;
    const write = plan.registryWrite(NEW_HASH);
    expect(write.input.revision_number).toBe(1);
    expect(write.input.entries).toEqual([
      {
        model_id: "grok-4.6",
        agent_configuration_revision_hash: NEW_HASH
      }
    ]);
    expect(write.body).toBe(JSON.stringify(write.input));
  });

  it("keeps twenty ids when mapping registry entries with no extra cap", () => {
    const ids = Array.from({ length: 19 }, (_, index) => `model-${index + 1}`);
    const current = registry({
      entries: ids.map((modelId, index) =>
        entry({
          model_id: modelId,
          agent_configuration_revision_hash: hashFor(index)
        })
      )
    });
    const plan = planAddModel({
      modelId: "model-20",
      profile: profile(),
      configurations: [configuration()],
      registries: [current]
    });

    expect(plan.kind).toBe("create");
    if (plan.kind !== "create") return;
    const listed = plan.registryWrite(NEW_HASH).input.entries.map((item) => item.model_id);
    expect(listed).toHaveLength(20);
    expect(listed).toEqual([...ids, "model-20"]);
  });
});

describe("how a registry row presents", () => {
  it("names checking, unknown, added, and none", () => {
    expect(
      rowPresentation({ source: "operator", provider_check: "checked" }, true)
    ).toBe("checking");
    expect(
      rowPresentation({ source: "discovered", provider_check: "unknown-at-provider" }, false)
    ).toBe("unknown");
    expect(
      rowPresentation({ source: "operator", provider_check: "checked" }, false)
    ).toBe("added-checked");
    expect(
      rowPresentation({ source: "operator", provider_check: "not-checked" }, false)
    ).toBe("added-not-checked");
    expect(
      rowPresentation({ source: "discovered", provider_check: "checked" }, false)
    ).toBe("none");
    expect(
      rowPresentation({ source: "discovered", provider_check: "not-checked" }, false)
    ).toBe("none");
  });
});

function projectDefaults(
  items: ProjectModelDefaultsRevision["defaults"],
  revisionNumber = 1
): ProjectModelDefaultsRevision {
  return {
    project_id: "atelier",
    public_project_reference: "project1.dGVzdA",
    revision_number: revisionNumber,
    project_model_defaults_revision_hash: REGISTRY_HASH,
    defaults: items
  };
}

function defaultItem(
  difficulty: 1 | 2 | 3,
  configurationHash: string,
  modelId = "claude-opus-4-6"
): ProjectModelDefaultsRevision["defaults"][number] {
  return {
    difficulty,
    model_registry_revision_hash: REGISTRY_HASH,
    provider_id: "anthropic",
    model_id: modelId,
    agent_configuration_revision_hash: configurationHash
  };
}

describe("defaults after removing a configuration hash", () => {
  it("drops every default that names the hash and keeps siblings", () => {
    const sibling = defaultItem(1, NEW_HASH, "other-model");
    const current = projectDefaults(
      [defaultItem(3, PIN_HASH), defaultItem(2, PIN_HASH), sibling],
      4
    );
    const write = defaultsAfterRemovingConfigurationHash(current, PIN_HASH);
    const input = { revision_number: 5, defaults: [sibling] };
    expect(write?.input).toEqual(input);
    expect(write?.body).toBe(JSON.stringify(input));
  });

  it("returns null when no saved default names the hash", () => {
    expect(defaultsAfterRemovingConfigurationHash(null, PIN_HASH)).toBeNull();
    expect(
      defaultsAfterRemovingConfigurationHash(projectDefaults([defaultItem(3, NEW_HASH)]), PIN_HASH)
    ).toBeNull();
  });
});

describe("rebasing a registry write onto the current revision", () => {
  it("adds the intended model beside current siblings at the next revision", () => {
    const sibling = entry({
      model_id: "sibling-model",
      agent_configuration_revision_hash: SMALLER_HASH
    });
    const current = registry({ revision_number: 2, entries: [sibling] });
    const rebased = rebasedRegistryWrite(current, {
      kind: "add",
      modelId: "grok-4.6",
      configurationHash: NEW_HASH
    });
    expect(rebased.kind).toBe("write");
    if (rebased.kind !== "write") return;
    const input = {
      revision_number: 3,
      entries: [
        {
          model_id: "sibling-model",
          agent_configuration_revision_hash: SMALLER_HASH
        },
        {
          model_id: "grok-4.6",
          agent_configuration_revision_hash: NEW_HASH
        }
      ]
    };
    expect(rebased.write.input).toEqual(input);
    expect(rebased.write.body).toBe(JSON.stringify(input));
  });

  it("removes the intended hash and keeps siblings", () => {
    const sibling = entry({
      model_id: "sibling-model",
      agent_configuration_revision_hash: SMALLER_HASH
    });
    const current = registry({ revision_number: 2, entries: [entry(), sibling] });
    const rebased = rebasedRegistryWrite(current, {
      kind: "remove",
      configurationHash: PIN_HASH
    });
    expect(rebased.kind).toBe("write");
    if (rebased.kind !== "write") return;
    expect(rebased.write.input).toEqual({
      revision_number: 3,
      entries: [
        {
          model_id: "sibling-model",
          agent_configuration_revision_hash: SMALLER_HASH
        }
      ]
    });
  });

  it("treats an already added or already removed intent as current truth", () => {
    expect(
      rebasedRegistryWrite(registry(), {
        kind: "add",
        modelId: "claude-opus-4-6",
        configurationHash: PIN_HASH
      })
    ).toEqual({ kind: "already-true" });
    expect(
      rebasedRegistryWrite(registry({ entries: [] }), {
        kind: "remove",
        configurationHash: PIN_HASH
      })
    ).toEqual({ kind: "already-true" });
  });
});
