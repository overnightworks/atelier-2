import type { ProjectSourceResource } from "../api/client";
import { problemCode } from "./catalogName";
import {
  disconnectTitle,
  settingsPageCopy
} from "./settingsPageCopy";
import { ageLabel } from "./when";

export type SourceContentKind = "items" | "library";

export type SourceDoorError = {
  sentence: string;
  nextStep: string | null;
};

export type SourceWriteDoor = "connect" | "disconnect" | "renew";

export type SourceWriteFailure =
  | { kind: "duplicate" }
  | { kind: "refused"; sentence: string; nextStep: string };

export interface ProjectSourceRowView {
  publicSourceReference: string;
  chip: string;
  kindLabel: string;
  address: string;
  headline: string;
  scope: string;
  connected: string;
  duplicate: boolean;
}

export interface DisconnectFacts {
  title: string;
  goesLabel: string;
  goes: string;
  staysLabel: string;
  stays: string;
  againLabel: string;
  again: string;
}

export function connectProjectSourceBody(
  address: string,
  token: string
): { address: string; token: string } {
  return { address: address.trim(), token };
}

export function rotateProjectSourceTokenBody(token: string): { token: string } {
  return { token };
}

export function canConnectSource(
  kind: SourceContentKind,
  address: string,
  token: string
): boolean {
  if (kind !== "items") return false;
  const trimmed = address.trim();
  return (
    trimmed.length >= 1
    && trimmed.length <= 1_024
    && token.length >= 1
    && token.length <= 4_096
  );
}

export function canRotateSourceToken(token: string): boolean {
  return token.length >= 1 && token.length <= 4_096;
}

export function sourceKindLabel(kind: string): string {
  if (kind === "github") return settingsPageCopy.github;
  if (kind === "gitlab") return settingsPageCopy.gitlab;
  return settingsPageCopy.source;
}

export function sourceHeadline(source: ProjectSourceResource): string {
  return `${sourceKindLabel(source.kind)} · ${source.address}`;
}

export function connectedPhrase(connectedAt: string | null, now: Date): string {
  if (connectedAt === null) return settingsPageCopy.connectionTimeNotRecorded;
  return `${settingsPageCopy.connected} ${ageLabel(connectedAt, now, "ago")}`;
}

export function takeActiveSourcesToday(
  items: readonly ProjectSourceResource[]
): { items: ProjectSourceResource[]; severalNotBuilt: boolean } {
  if (items.length <= 1) {
    return { items: [...items], severalNotBuilt: false };
  }
  return { items: items.slice(0, 1), severalNotBuilt: true };
}

export function presentProjectSource(
  source: ProjectSourceResource,
  now: Date,
  duplicate = false
): ProjectSourceRowView {
  const kindLabel = sourceKindLabel(source.kind);
  return {
    publicSourceReference: source.public_source_reference,
    chip: settingsPageCopy.items,
    kindLabel,
    address: source.address,
    headline: sourceHeadline(source),
    scope: settingsPageCopy.issues,
    connected: connectedPhrase(source.connected_at, now),
    duplicate
  };
}

export function disconnectFacts(args: {
  address: string;
  projectName: string;
  remainingSources: readonly ProjectSourceResource[];
  modelsExist: boolean;
}): DisconnectFacts {
  const staysParts: [string, ...string[]] = [
    args.projectName,
    ...args.remainingSources.map((source) => sourceHeadline(source))
  ];
  if (args.modelsExist) staysParts.push(settingsPageCopy.theModels);
  return {
    title: disconnectTitle(args.address),
    goesLabel: settingsPageCopy.goes,
    goes: settingsPageCopy.thisConnection,
    staysLabel: settingsPageCopy.stays,
    stays: joinWithAnd(staysParts),
    againLabel: settingsPageCopy.again,
    again: settingsPageCopy.connectStartsNew
  };
}

export function sourceWriteFailure(
  error: unknown,
  door: SourceWriteDoor
): SourceWriteFailure {
  const code = problemCode(error);
  if (code === "project-source-already-connected") return { kind: "duplicate" };
  if (door === "disconnect") {
    return {
      kind: "refused",
      sentence: settingsPageCopy.sourceDisconnectRefused,
      nextStep: settingsPageCopy.retry
    };
  }
  if (code === "project-source-token-refused") {
    return {
      kind: "refused",
      sentence: settingsPageCopy.tokenRefused,
      nextStep: settingsPageCopy.renewToken
    };
  }
  if (code === "project-source-invalid") {
    return {
      kind: "refused",
      sentence: settingsPageCopy.sourceInvalid,
      nextStep: settingsPageCopy.renewToken
    };
  }
  return {
    kind: "refused",
    sentence: settingsPageCopy.sourceConnectRefused,
    nextStep: settingsPageCopy.renewToken
  };
}

function joinWithAnd([first, ...rest]: readonly [string, ...string[]]): string {
  const last = rest.at(-1);
  if (last === undefined) return first;
  if (rest.length === 1) return `${first} ${settingsPageCopy.and} ${last}`;
  return `${[first, ...rest.slice(0, -1)].join(", ")}, ${settingsPageCopy.and} ${last}`;
}
