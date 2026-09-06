/**
 * Topological layers of a V3 excerpt, in the order a drawing can place them.
 *
 * A node sits in the first layer after every dependency it named. Within a
 * layer the order is the UTF-8 identity of the node id, so the same excerpt
 * always draws the same picture. A cycle, or an edge that names nobody in
 * the excerpt, is a named refusal — the drawing does not invent a placement.
 */

export type GraphLayerNode = {
  id: string;
  depends_on: readonly string[];
};

export type GraphLayers<T extends GraphLayerNode = GraphLayerNode> =
  | { ok: true; layers: readonly (readonly T[])[] }
  | { ok: false; reason: string };

export function layerWorkflowGraph<T extends GraphLayerNode>(nodes: readonly T[]): GraphLayers<T> {
  const byId = new Map(nodes.map((node) => [node.id, node]));
  if (byId.size !== nodes.length) {
    return { ok: false, reason: "This graph could not be layered: a node id is repeated." };
  }
  for (const node of nodes) {
    if (node.depends_on.some((dependency) => !byId.has(dependency))) {
      return {
        ok: false,
        reason: "This graph could not be layered: a node names a dependency that is not among the previews."
      };
    }
  }

  const remaining = new Set(byId.keys());
  const layers: T[][] = [];
  while (remaining.size > 0) {
    const ready = [...remaining]
      .filter((id) => {
        const node = byId.get(id);
        return node?.depends_on.every((dependency) => !remaining.has(dependency)) ?? false;
      })
      .sort(compareUtf8Identities);
    if (ready.length === 0) {
      return { ok: false, reason: "This graph could not be layered: a cycle remains." };
    }
    layers.push(ready.map((id) => byId.get(id)).filter((node): node is T => node !== undefined));
    for (const id of ready) remaining.delete(id);
  }
  return { ok: true, layers };
}

function compareUtf8Identities(left: string, right: string): number {
  const encodedLeft = new TextEncoder().encode(left);
  const encodedRight = new TextEncoder().encode(right);
  const limit = Math.min(encodedLeft.length, encodedRight.length);
  for (let index = 0; index < limit; index += 1) {
    const delta = encodedLeft[index]! - encodedRight[index]!;
    if (delta !== 0) return delta;
  }
  return encodedLeft.length - encodedRight.length;
}
