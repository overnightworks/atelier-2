import { describe, expect, it } from "vitest";

import { layerWorkflowGraph } from "../../src/lib/workflowGraph";

describe("topological layers of a V3 excerpt", () => {
  it("places a chain in authored-edge order even when the excerpt is reversed", () => {
    const layered = layerWorkflowGraph([
      { id: "review", depends_on: ["implement"] },
      { id: "implement", depends_on: [] }
    ]);

    expect(layered).toEqual({
      ok: true,
      layers: [[{ id: "implement", depends_on: [] }], [{ id: "review", depends_on: ["implement"] }]]
    });
  });

  it("sorts siblings in one layer by UTF-8 identity, not authored order", () => {
    const layered = layerWorkflowGraph([
      { id: "zeta", depends_on: [] },
      { id: "alpha", depends_on: [] },
      { id: "join", depends_on: ["zeta", "alpha"] }
    ]);

    expect(layered).toEqual({
      ok: true,
      layers: [
        [
          { id: "alpha", depends_on: [] },
          { id: "zeta", depends_on: [] }
        ],
        [{ id: "join", depends_on: ["zeta", "alpha"] }]
      ]
    });
  });

  it("lays a diamond as start, then both sides, then the join", () => {
    const layered = layerWorkflowGraph([
      { id: "end", depends_on: ["left", "right"] },
      { id: "right", depends_on: ["start"] },
      { id: "left", depends_on: ["start"] },
      { id: "start", depends_on: [] }
    ]);

    expect(layered).toEqual({
      ok: true,
      layers: [
        [{ id: "start", depends_on: [] }],
        [
          { id: "left", depends_on: ["start"] },
          { id: "right", depends_on: ["start"] }
        ],
        [{ id: "end", depends_on: ["left", "right"] }]
      ]
    });
  });

  it("still layers a single node that names no edge", () => {
    const layered = layerWorkflowGraph([{ id: "only", depends_on: [] }]);

    expect(layered).toEqual({ ok: true, layers: [[{ id: "only", depends_on: [] }]] });
  });

  it("refuses an edge that names a node outside the excerpt", () => {
    const layered = layerWorkflowGraph([{ id: "review", depends_on: ["implement"] }]);

    expect(layered.ok).toBe(false);
    if (layered.ok) return;
    expect(layered.reason).toMatch(/not among/);
  });

  it("refuses a cycle instead of inventing a placement", () => {
    const layered = layerWorkflowGraph([
      { id: "a", depends_on: ["b"] },
      { id: "b", depends_on: ["a"] }
    ]);

    expect(layered.ok).toBe(false);
    if (layered.ok) return;
    expect(layered.reason).toMatch(/cycle/i);
  });
});
