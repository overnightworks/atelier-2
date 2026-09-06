import { CockpitRequestError, type Problem } from "../api/client";

const PROBLEM_TYPE_PREFIX = "urn:atelier2:problem:v1:";

const KNOWN_PROBLEM_NEXT_ACTION: Readonly<Record<string, string>> = {
  "durable-state-corrupt":
    "The workshop cannot read this stored work. Refresh the page; if it stays, an operator must inspect the durable store."
};

const OUTPUT_SHAPE =
  /^agent-output-shape-unavailable: (\d+) outputs on node '([^']+)'/;
const WAIT_INPUTS = /^inputs on wait node '([^']+)' that nothing composes/;
const TOOL_GRANTS = /^(\d+) tool grants on node '([^']+)', and one attempt redeems one grant$/;

/**
 * A start-refusal the wire already named, in a sentence a person can act on.
 *
 * Known tokens keep their machine form on the wire. This function is the UI
 * owner of the human reading. An unknown token stays raw, so a new refusal
 * is never dressed as something else.
 */
export function humanStartRefusal(reason: string): string {
  const shape = OUTPUT_SHAPE.exec(reason);
  if (shape !== null) {
    const count = shape[1] ?? "0";
    const node = shape[2] ?? "this node";
    if (count === "0") {
      return `This workflow declares no output on node '${node}'. Add one outputs: entry there and publish again.`;
    }
    return `Node '${node}' declares ${count} outputs; an agent node completes with exactly one. Keep one outputs: entry and publish again.`;
  }
  if (
    reason === "agent forms nothing binds yet: outputs" ||
    reason === "authored forms nothing binds yet: outputs"
  ) {
    return "This workflow declares no output. Add one outputs: entry on the agent node and publish again.";
  }
  if (reason.startsWith("authored forms nothing binds yet:")) {
    const forms = reason.slice("authored forms nothing binds yet:".length).trim();
    return `This workflow declares forms the runtime does not bind yet (${forms}). Remove them and publish again.`;
  }
  if (reason.startsWith("node kinds no runtime interprets:")) {
    const kinds = reason.slice("node kinds no runtime interprets:".length).trim();
    return `This workflow uses node kinds the runtime does not run (${kinds}). Replace them with agent or wait nodes and publish again.`;
  }
  if (reason.startsWith("graph outputs nothing carries out of a run:")) {
    return "This workflow declares graph outputs nothing carries out of a run. Remove graph_outputs and publish again.";
  }
  if (reason === "an output confirmed by an operator nothing asks") {
    return "An output asks an operator to confirm it, and nothing here asks anyone. Remove confirmed_by and publish again.";
  }
  if (reason.startsWith("input sources nothing binds yet:")) {
    const sources = reason.slice("input sources nothing binds yet:".length).trim();
    return `This workflow reads input sources the start cannot supply (${sources}). Use a graph input or a previous node's output and publish again.`;
  }
  if (reason.includes("do not form one line")) {
    return "This workflow is not one line. The runtime cannot choose between branches yet; keep a single chain and publish again.";
  }
  const waitInputs = WAIT_INPUTS.exec(reason);
  if (waitInputs !== null) {
    const node = waitInputs[1] ?? "this wait";
    return `Wait node '${node}' declares inputs nothing composes into the question. Remove those inputs and publish again.`;
  }
  const grants = TOOL_GRANTS.exec(reason);
  if (grants !== null) {
    const node = grants[2] ?? "this node";
    return `Node '${node}' pins more than one tool grant; one attempt redeems one. Keep a single grant and publish again.`;
  }
  return reason;
}

export function cannotBeStarted(reason: string | null): string {
  if (reason === null || reason === "") {
    return "Cannot be started: this workflow is not executable.";
  }
  return `Cannot be started: ${humanStartRefusal(reason)}`;
}

export function humanProblemDetail(problem: Pick<Problem, "type" | "detail">): string {
  if (!problem.type.startsWith(PROBLEM_TYPE_PREFIX)) {
    return problem.detail;
  }
  const code = problem.type.slice(PROBLEM_TYPE_PREFIX.length);
  if (code === "durable-projection-unrepresentable") {
    return "Open the node detail to inspect the stored value.";
  }
  return KNOWN_PROBLEM_NEXT_ACTION[code] ?? problem.detail;
}

export function humanErrorMessage(error: unknown, fallback: string): string {
  if (error instanceof CockpitRequestError) {
    // A round trip that never happened (#700) carries the browser's own raw
    // transport text ("Failed to fetch" and the like), never a sentence this
    // workshop owns -- the caller's fallback speaks instead. A contract
    // violation the API did answer with keeps its own specific message.
    if (error.transport_failure) return fallback;
    if (error.problem !== null) return humanProblemDetail(error.problem);
  }
  return error instanceof Error ? error.message : fallback;
}
