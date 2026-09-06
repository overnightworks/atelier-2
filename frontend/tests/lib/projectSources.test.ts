import { describe, expect, it } from "vitest";

import { CockpitRequestError, type Problem, type ProjectSourceResource } from "../../src/api/client";
import {
  canConnectSource,
  canRotateSourceToken,
  connectProjectSourceBody,
  connectedPhrase,
  disconnectFacts,
  presentProjectSource,
  rotateProjectSourceTokenBody,
  sourceHeadline,
  sourceKindLabel,
  sourceWriteFailure,
  takeActiveSourcesToday
} from "../../src/lib/projectSources";
import {
  disconnectTitle,
  settingsPageCopy,
  sourceAlreadyPresent
} from "../../src/lib/settingsPageCopy";

const now = new Date("2026-08-28T15:05:00Z");

function source(overrides: Partial<ProjectSourceResource> = {}): ProjectSourceResource {
  return {
    public_source_reference: "source1.MzgwZjI3YTEtNmRlMC01NjNkLTQwYWItYzg1MzBmOWMyNWNj",
    kind: "github",
    address: "FlexOr2/atelier-2",
    scope: "issues",
    connected_at: null,
    revision: 2,
    auth_method: "personal-access-token",
    ...overrides
  };
}

function problem(code: string, title: string, status: number): CockpitRequestError {
  return new CockpitRequestError("refused", {
    type: `urn:atelier2:problem:v1:${code}`,
    title,
    status,
    detail: "provider detail"
  } as Problem, true);
}

describe("project source request bodies", () => {
  it("composes connect from Where and Token without a kind", () => {
    expect(connectProjectSourceBody("  FlexOr2/atelier-2  ", "secret-token")).toEqual({
      address: "FlexOr2/atelier-2",
      token: "secret-token"
    });
    expect(Object.keys(connectProjectSourceBody("FlexOr2/atelier-2", "secret-token"))).toEqual([
      "address",
      "token"
    ]);
  });

  it("composes rotate from the token only", () => {
    expect(rotateProjectSourceTokenBody("next-token")).toEqual({ token: "next-token" });
  });

  it("lets Connect go only for Items with a Where and a Token", () => {
    expect(canConnectSource("items", "FlexOr2/atelier-2", "secret-token")).toBe(true);
    expect(canConnectSource("library", "FlexOr2/atelier-2", "secret-token")).toBe(false);
    expect(canConnectSource("items", "   ", "secret-token")).toBe(false);
    expect(canConnectSource("items", "FlexOr2/atelier-2", "")).toBe(false);
    expect(canRotateSourceToken("next-token")).toBe(true);
    expect(canRotateSourceToken("")).toBe(false);
  });
});

describe("project source row presentation", () => {
  it("labels the kind from the resource and never shows a branch, token, or auth method", () => {
    const row = presentProjectSource(source(), now);

    expect(row.chip).toBe(settingsPageCopy.items);
    expect(row.kindLabel).toBe(settingsPageCopy.github);
    expect(row.headline).toBe(`${settingsPageCopy.github} · FlexOr2/atelier-2`);
    expect(row.scope).toBe(settingsPageCopy.issues);
    expect(row.connected).toBe(settingsPageCopy.connectionTimeNotRecorded);
    expect(row.duplicate).toBe(false);
    expect(row.headline).not.toContain("@");
    expect(JSON.stringify(row)).not.toContain("personal-access-token");
    expect(JSON.stringify(row).toLowerCase()).not.toContain("secret");
    expect(sourceKindLabel("gitlab")).toBe(settingsPageCopy.gitlab);
    expect(sourceHeadline(source({ kind: "gitlab", address: "infra" }))).toBe(
      `${settingsPageCopy.gitlab} · infra`
    );
    expect(sourceKindLabel("gitea")).toBe(settingsPageCopy.source);
    expect(presentProjectSource(source({ kind: "gitea" }), now).kindLabel).toBe(
      settingsPageCopy.source
    );
    expect(presentProjectSource(source({ kind: "gitea" }), now).headline).not.toContain("gitea");
  });

  it("names a recorded connection with the shared age words", () => {
    expect(connectedPhrase("2026-08-28T15:00:00Z", now)).toBe(
      `${settingsPageCopy.connected} 5 min ago`
    );
    expect(connectedPhrase(null, now)).toBe(settingsPageCopy.connectionTimeNotRecorded);
  });

  it("marks a duplicate on the existing row", () => {
    expect(presentProjectSource(source(), now, true).duplicate).toBe(true);
  });
});

describe("the one-source deferral", () => {
  it("keeps an empty or single list and names extra items as not built", () => {
    const first = source();
    const second = source({
      public_source_reference: "source1.YWx0ZXJuYXRlLXNvdXJjZS1yZWZlcmVuY2UtYWFhYQ",
      address: "github.com/other/repo"
    });
    expect(takeActiveSourcesToday([])).toEqual({ items: [], severalNotBuilt: false });
    expect(takeActiveSourcesToday([first])).toEqual({ items: [first], severalNotBuilt: false });
    expect(takeActiveSourcesToday([first, second])).toEqual({
      items: [first],
      severalNotBuilt: true
    });
  });

  it("names an already-present source with its address", () => {
    expect(sourceAlreadyPresent("FlexOr2/atelier-2")).toBe(
      `FlexOr2/atelier-2 ${settingsPageCopy.alreadyPresent}`
    );
  });
});

describe("disconnect confirmation facts", () => {
  it("names what goes, what stays, and that Connect starts again", () => {
    const facts = disconnectFacts({
      address: "FlexOr2/atelier-2",
      projectName: "atelier",
      remainingSources: [source({ kind: "gitlab", address: "infra" })],
      modelsExist: true
    });

    expect(facts.title).toBe(disconnectTitle("FlexOr2/atelier-2"));
    expect(facts.goes).toBe(settingsPageCopy.thisConnection);
    expect(facts.stays).toBe(
      `atelier, ${settingsPageCopy.gitlab} · infra, ${settingsPageCopy.and} ${settingsPageCopy.theModels}`
    );
    expect(facts.again).toBe(settingsPageCopy.connectStartsNew);
  });

  it("keeps the project and models when no other source remains", () => {
    const facts = disconnectFacts({
      address: "FlexOr2/atelier-2",
      projectName: "atelier",
      remainingSources: [],
      modelsExist: true
    });
    expect(facts.stays).toBe(`atelier ${settingsPageCopy.and} ${settingsPageCopy.theModels}`);
  });

  it("keeps only the project when no other source and no models remain", () => {
    const facts = disconnectFacts({
      address: "FlexOr2/atelier-2",
      projectName: "atelier",
      remainingSources: [],
      modelsExist: false
    });
    expect(facts.stays).toBe("atelier");
  });
});

describe("source write failures", () => {
  it("maps refused, invalid, duplicate, disconnect, and generic connect failures through the copy owner", () => {
    expect(sourceWriteFailure(problem(
      "project-source-already-connected",
      "Project source already connected",
      409
    ), "connect")).toEqual({ kind: "duplicate" });
    expect(sourceWriteFailure(problem(
      "project-source-token-refused",
      "Project source token refused",
      422
    ), "connect")).toEqual({
      kind: "refused",
      sentence: settingsPageCopy.tokenRefused,
      nextStep: settingsPageCopy.renewToken
    });
    expect(sourceWriteFailure(problem(
      "project-source-token-refused",
      "Project source token refused",
      422
    ), "renew")).toEqual({
      kind: "refused",
      sentence: settingsPageCopy.tokenRefused,
      nextStep: settingsPageCopy.renewToken
    });
    expect(sourceWriteFailure(problem(
      "project-source-invalid",
      "Project source invalid",
      422
    ), "connect")).toEqual({
      kind: "refused",
      sentence: settingsPageCopy.sourceInvalid,
      nextStep: settingsPageCopy.renewToken
    });
    expect(sourceWriteFailure(new Error("transport"), "connect")).toEqual({
      kind: "refused",
      sentence: settingsPageCopy.sourceConnectRefused,
      nextStep: settingsPageCopy.renewToken
    });
    expect(sourceWriteFailure(new Error("transport"), "disconnect")).toEqual({
      kind: "refused",
      sentence: settingsPageCopy.sourceDisconnectRefused,
      nextStep: settingsPageCopy.retry
    });
  });
});
