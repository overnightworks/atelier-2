# ADR 0014: A declared loop repeats a stretch of one graph, and the round is the fourth dimension of a node execution identity

- Status: ACCEPTED 2026-08-18 — implemented: the loop declaration, the round as
  the fourth dimension of node execution identity, and the bounded round-repeat
  runtime landed with this record and run a real loop to `COMPLETED`; see
  `tests/integration/test_v3_bounded_loop_run.py` and
  [docs/PRODUCT.md](../PRODUCT.md). [#449](https://github.com/FlexOr2/atelier-2/issues/449)
  withdrew only the Subworkflow `iterate` binder ADR 0013 authored — not this
  record's in-graph loop; see "What happens to ADR 0013" below. Amended
  2026-08-25 ([#658](https://github.com/FlexOr2/atelier-2/issues/658)): a
  repeated Wait is executable, replacing "What this build repeats, and what it
  refuses" below — the round identity this record decided already carries the
  answer path, so what the section called a missing identity is a landed one.
  Amended 2026-08-26 ([#751](https://github.com/FlexOr2/atelier-2/issues/751)):
  an Action node inside a declared loop stays refused, this time deliberately
  rather than by gap — see "What this build repeats, and what it refuses"
  below.
- Supersedes: the structural finding of
  [ADR 0013](0013-bounded-iteration.md) — "A loop inside one graph is therefore
  identity-impossible" — and, with it, ADR 0013's decision that a round is a child
  run of a `subworkflow` node. ADR 0013's `iterate` block stays declarable and
  stays unexecutable; see "What happens to ADR 0013" below
- Requirement authority: [Issue #1](https://github.com/FlexOr2/atelier-2/issues/1)
- Decision authority: [Issue #25](https://github.com/FlexOr2/atelier-2/issues/25),
  the operator rulings of 19.08. — the design direction at
  [comment 5318036090](https://github.com/FlexOr2/atelier-2/issues/25#issuecomment-5318036090)
  and the cut at
  [comment 5318394119](https://github.com/FlexOr2/atelier-2/issues/25#issuecomment-5318394119)
- Depends on: [ADR 0002](0002-exact-yaml-graph.md) (the acyclic graph, whose cycle
  refusal this record leaves unconditional), [ADR 0006](0006-node-vocabulary.md)
  (the node contract and the durable record family a round now writes per round)
- Names, never decides: [#26](https://github.com/FlexOr2/atelier-2/issues/26) (the
  budget a loop will also have to answer to)

## Context

The workshop's real cycle is build → review → fix → re-review until green, and the
operator's design direction of 19.08. put it where it belongs: **inside** one
workflow, as `build → verify → review → [back to build] → wait → merge`, with the
merger a bound agent node rather than an outside hand. That is a loop over a
stretch of one graph. It is not a chain of child runs.

ADR 0013 decided the opposite shape, and it decided it for a reason that has to be
answered rather than ignored. Its words: *"A loop inside one graph is therefore
identity-impossible, not merely difficult: the same `node_id` executed a second
time in the same run collides on `NodeExecutionId`."* That was an accurate reading
of the identity as it then stood — `node-execution-id/v1` binds run, revision and
node, and nothing else. The subworkflow boundary dissolved the collision by giving
each round its own `run_id`.

The operator's cut removes the premise instead of the conclusion: the round becomes
a dimension of the identity. What ADR 0013 called impossible was a property of one
preimage, not of the domain.

## Decision

### The loop is declared beside the edges, never inside them

```yaml
nodes:
  - id: implement
    type: agent
    …
  - id: review
    type: agent
    depends_on: [implement]
    …
loops:
  - id: until_reviewed
    body: [implement, review]
    maximum_rounds: 3
```

`depends_on` stays acyclic, and ADR 0002's cycle refusal keeps no exception: a
backwards control edge is refused exactly as before. The declaration is the only
legal way back. That is the whole reason it sits beside the edges rather than in
them — an unconditional prohibition with one exception in it is not one.

**`maximum_rounds` is mandatory and has no default.** A loop declared without it is
refused by name, under the same `unbounded_iteration` token ADR 0013 minted. An
unbounded loop reachable by omission is an unbounded bill.

**The body is one uninterrupted stretch of the order the edges already declare**:
entered at its head, left at its tail, each member ordered after the one before it
by a single declared edge. Repeating a scattered set of nodes would need a rule for
what happens between them, and no owner decides that. A body the edges cannot turn
is refused under `loop_body_not_one_line`.

**One node belongs to at most one loop**, and two loops may not share an id. Both
are refused under `duplicate_name`.

### The round is the fourth dimension of the node execution identity, carried by a nested family

```text
round 1  → frame("node-execution-id/v1", run, revision, node)          # unchanged
round K>1 → frame("node-execution-id/round/v1", <that digest>, K)
```

Round one is **byte-for-byte** the derivation that landed before loops existed, so
no stored execution, receipt, artifact, attempt or durable workflow id moves. This
is the same discipline `adapters/dbos/workflow_ids.py` already states from the
other side: three id schemas live there deliberately unmerged, because one shared
derivation would restart every durable workflow under an identity nothing recorded.
`tests/domain/test_durable_id_forms.py` pins both halves — that round one *is* the
landed vector, and what each later round is.

**No new hash domain for events.** The event hash already binds the node execution
id, so it carries the round transitively; a recomputed terminal hash is therefore a
statement about which rounds ran. The round is nevertheless a column of its own on
the event and on the run, because a store must be able to answer "which round" and
a digest cannot be inverted. Contract validation holds the pair together, exactly
as it already holds `node_id` and `node_execution_id` together.

**The agent attempt ordinal is untouched.** ADR 0013's three reasons for keeping
iteration out of that vocabulary are unaffected by this record and still hold:
round seven has its own attempt 1 and at most its own replacement 2.

### Reaching the bound is a way out, and the run ends where any node ends

The last node of a round hands back to the loop's head while rounds remain. When
the bound is reached, the loop is simply not taken and the ordinary rule decides —
the sink completes the run, anything else hands on to its declared heir. **No new
terminal state, no new failure word, no new vocabulary.** A run whose loop is
exhausted ends through the path that already exists.

A result that ends a loop early — a review verdict steering the next edge — is
**not** decided here. It is #25's second head, and
[ADR 0015](0015-verdict-steered-continuation.md) is where it was decided; the
bound this record made mandatory stays mandatory beside it.

### A round owes its own evidence

One `node-execution-request/v3`, one `node-receipt/v3`, one `node-artifact/v3` and
one durable node workflow per round of per node, each addressed by that round's
identity. The requests for every declared round are written at the start, before
anything runs, because ADR 0006 requires a receipt to bind a request that already
exists and the bound makes "how many" answerable in advance.

The rounds of one node are asked the *same* thing until a result differs between
them, so their request preimages are identical and their hashes are one value. The
store therefore keys a node execution request by the **execution**, not by the
request hash — otherwise the second round's row vanished into the first and its
receipt had nothing to bind. For the same reason the agent receipt's key is the
node execution alone; the second key over (run, revision, node) said "once per run"
about something that is now once per round.

### What this build repeats, and what it refuses

Agent and Wait nodes are both repeated. A repeated Wait once asked the same
person the same question under an identity the answer path did not carry; this
record's own round dimension closes that gap — a `WaitNodeBinding` carries the
round ordinal it was bound in, and an answer is keyed by execution and round,
so a repeated Wait's question and its answer both stand under the round that
asked it. [#658](https://github.com/FlexOr2/atelier-2/issues/658) is where the
executable door was amended to admit it, with the integration proof that a
loop starting on a Wait runs two rounds through the public start and answer
doors.

A value read *out of* a loop still names no round — the reader would have to
say which round wrote it, and choosing is the verdict-driven continuation this
record does not decide. That form stays refused by name at the executable
door rather than started and abandoned.

**Amendment 2026-08-26 ([#751](https://github.com/FlexOr2/atelier-2/issues/751)):
an Action node inside a declared loop's body is deliberately out of scope, not
a gap awaiting its turn.** #706 made the Action-node effect-key derivation
round-aware so the identity plumbing would already be honest the day this
closed, and named the open question this amendment answers: no caller
declares an Action inside a loop body today, and repeating an Action repeats
an *external* side effect — a PR reopened, a message resent — once per round.
That repetition has its own idempotency question the round dimension does not
answer by itself: an external effect needs its own per-round idempotency key
before "run it again" is safe, and no domain need has asked for one. Extending
`_unrepeatable_loop_forms` (`contracts/workflow_executability.py`) to admit Action nodes
without that key would let a published document declare a shape the runtime
cannot honestly repeat. The refusal therefore stays exactly where #706 left
it — enforced by `_unrepeatable_loop_forms` and `parse_executable_workflow_document`
at every load, not only at run start — until a real caller needs a repeated
external effect and can name its idempotency key. [#751](https://github.com/FlexOr2/atelier-2/issues/751)
closes `wontfix` on this reasoning.

### A data edge inside a loop that the edges cannot order reads the previous round

`from: {node, output}` stays one payload in the producing output's schema. When
the source is in the same loop as the reader and not in the reader's `depends_on`
closure, the edge is not a cycle: `depends_on` stays acyclic, unconditionally.
The value is the one that source wrote in the immediately previous round. Round
one has no previous round, so that input is honestly absent — not a refusal, not
a guessed seed. A sequence of every earlier round would make the input schema
disagree with the output schema; that form is not this one.

Issue #402 is the owning item for this edge. ADR 0015's verdict still steers
the back edge; this rule is what lets the next build *read* the verdict that
steered it.

## What happens to ADR 0013

ADR 0013's `iterate` block — a bounded repetition of a **child run** hanging off a
`subworkflow` node — stays declarable and unexecutable: no runtime interprets a
`subworkflow` node. #449 withdrew its tests-only composition binding; what has been
superseded is its claim that the in-graph shape *cannot exist*, and the conclusion
that shape forced.

**This leaves two declarable ways to say "repeat, at most N times", and that is a
named seam rather than a settled design.** They are not the same domain idea — one
repeats a child run, the other repeats a stretch of this graph — and they already
share one refusal vocabulary. Whether the child-run form earns a runtime or is
retired in favour of a loop over a `subworkflow` node is a question for usage
evidence, and it is recorded here so the next reader does not mistake the overlap
for an oversight.

## Consequences

- The store moves to schema version 20: a round on the run and on every event and
  agent receipt, the node execution request keyed by its execution, and the agent
  receipt's once-per-run key dropped. Every carried row is read as round one,
  which is what those rows are — not a default filled in to make a column fit.
- Schema version 36 ([#658](https://github.com/FlexOr2/atelier-2/issues/658))
  finishes that move for the pause: the event log's once-per-node key said one
  event of a kind per node per run, which a Wait a loop turns twice breaks by
  writing a second `WAITING_INPUT`. Its successor says the same thing about one
  round, so a round still holds one pause and the next round holds its own. The
  hop is two DDL statements and the version CAS in one transaction — an index
  moves without reading a row, so nothing stored is rewritten.
- The hop exposed a latent defect in the migration chain and fixes it: a rebuild
  step materialised its table from the *live* declaration, which is only ever the
  current shape, so the first hop to touch a table an earlier hop had already
  rebuilt broke the chain in the middle. Published predecessor shapes now have
  their own owner, and one shared rebuild carries every row by reading both
  tables rather than a hand-kept column list.
- `completion_after_node` gains the round, and every reader of a completion — the
  node workflow, the attempt store's recovery path, the replacement workflow —
  answers with the same round or refuses.
- A node's value is read from the producing **execution** rather than the producing
  node, so a store holding several rounds of one node has one answer to one
  question. That closes the `MultipleResultsFound` this construct would otherwise
  have turned into an unnamed 500.
- The published grammar grows the `loops` form from the same models the parser
  reads, so the description cannot promise a shape the door refuses.
