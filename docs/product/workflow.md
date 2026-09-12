# Workflow status

Workflow format V3 is authored in full and executed in one shape. The parser accepts a
format-3 document into its own closed model — the five node kinds with the field
matrix each requires, refuses, or accepts, `depends_on` as the only control edge,
the join rule in its three arities, the input sources a node may read — another
node's output, that node's terminal receipt, a context entry, and the order the
graph itself was started with — the two context-edge kinds, graph-level inputs
and outputs, and the loops that repeat a stretch of the graph — and refuses every
forbidden form naming the node and the field it concerns, including each retired
V1 or V2 key with its replacement.

A document may declare that a stretch of its own graph repeats. The declaration
names the loop, the nodes it repeats and a maximum number of rounds that has no
default: an unbounded loop is refused by name, as is a body the declared edges do
not order in one uninterrupted stretch, a node two loops claim, and a loop
repeating a node nothing declares. A control edge pointing backwards stays refused
as the cycle it always was, so the declaration is the only legal way back. A run
whose document declares a loop runs the body again from its first node while
rounds remain and ends when the bound is reached, through the terminal path every
other node ends by. Every round of every looped node is its own node execution
with its own deterministic identity, durable request, receipt, produced value,
durable workflow and event in the chain the terminal hash recomputes over — while
the first round of a node keeps byte for byte the identity it had before any loop
existed.

A loop has a second, earlier way out: the answer a round produced. The
declaration may name the node whose verdict decides and the verdict that sends
the loop round again, and the engine reads that verdict out of the value the
round kept and chooses the edge — another round, or the way out — while the
declared bound stays the fallback no verdict gets past. The vocabulary is closed
and has one owner (`accepted` and `revise`), and the answer carrying it is judged
by a schema derived from that same vocabulary and published as an ordinary
revision: a deciding node's one output must pin exactly that revision, or the
document is refused by name, as is a verdict read from a node that does not close
the round. That is what makes a `revise` a *successful* node whose content chose
the next edge rather than a failure. What no verdict can say yet is the agent's
own named refusal — "the order is unclear because X" — because a run ends failed
only under an attempt failure code whose value list is a store contract, and a
refusal written under either existing code would name a schema or a dead process
that was never involved. A loop body may hold agent and wait nodes: a round is a
second execution of a node, and both kinds mint one, so a person may be asked
again every round and each pause is its own event. An action node is still
refused in a loop, because a repeated effect has no round an idempotent write
could lean on. A `from` edge whose source sits in the same loop and is not
ordered by `depends_on` reads that source's immediately previous round — one
payload, the producing output's schema — and is honestly empty in round one. A value read *out of* a loop is still
refused by name because no rule here says which round wrote it. Unsafe YAML is
refused by name too, before any vocabulary is read: an
anchor, an alias, an explicit tag, a merge key, a duplicate key, a second document,
a document that is not UTF-8 without a byte order mark.

V3 subworkflow and iterate forms remain authored and readable, but unexecutable.
The real starter refuses them before a run, configuration or enqueue write; no child
closure, boundary binding, depth check, or child preview exists. The order a root
graph declares is readable by name, and a node reading an order its graph never
declared, or an unread declared order, is refused naming what it concerns.
Every non-workflow versioned reference of a root document — schema, deterministic
and adapter operation, context source, read operation, profile, skill, tool, and
the policy, budget, retry and cancellation policies — resolves against the registry of the kind
its authored position puts it in, by the exact revision hash it pins. A reference
whose revision is no pinned hash, that no publication of that kind carries, or that a registry answers
with a revision of another kind or another hash is refused naming the node, the
field, the declared entry, the chain it was reached through, and the reference
itself. A `schema` reference proves more than its hash: the revision it pins must be
a schema, under one closed profile of JSON Schema Draft 2020-12 whose bounds keep a
published schema cheap to read — bounded bytes, container depth and value count,
UTF-8 without a byte order mark, no duplicate keys and no non-canonical numbers,
`$id`, `$anchor`, `$dynamicAnchor` and `$dynamicRef` refused, every `$ref` local and
resolvable, `$schema` absent or exactly Draft 2020-12, and `format` left the draft's
annotation instead of an assertion. Retrieval is off by construction rather than by
trust: evaluation runs against a registry whose only retrieval path raises. The
profile checks a reference's target and not only its form: a local `$ref` naming an
anchor or a pointer the document does not carry is refused where the bytes are read,
over the whole document rather than only where an evaluator would trip. A reference
cycle no instance can break is refused too — the rule is whether the cycle passes
through an applicator that descends into the instance, so `{"$ref": "#"}` alone is
refused while a tree whose child is `{"$ref": "#"}` under `properties` stays legal,
because that recursion ends on any finite instance. So nothing this profile accepts
can fail at first evaluation for want of a target, which would be an outage rather
than a refusal. That profile is now applied to values as well as to schemas: an
agent's answer is read against the schema its node declared, by that one owner,
before the answer can become anything. Bytes that fall outside the profile are
refused by name, so the whole snapshot fails rather than binding a type nobody can
read.
A root run's non-workflow references are frozen into one run-configuration revision
with its role matrix, hash-framed as one immutable snapshot whose identity does not
depend on assembly order. Behind that binding, nothing: the registries are ports a
caller supplies. A durable catalog adapter now
publishes exact revision bytes, founds a named lineage through a typed writer
that derives the lineage id, and resolves an admitted name or lineage id to
those bytes. A document already published through the door of its kind
is named through `POST /catalog-lineages` from those same bytes and the same
hash; founding does not invent a second identity. That door family is
kind-generic: one founding, member and retirement door, and one
`GET /catalog-revisions/by-name/{kind}/{name}`, serve every published kind, and
the founding name comes from the document per kind -- a V3 workflow's `name`,
an agent definition's frontmatter `name` -- so a workflow and an agent may carry
the same name in two lineages. Run-configuration binding is
still lineage-free and a reference's `ref` is carried into that snapshot
without calling `resolve_reference`. A
64-hex query is a lineage id; anything else is a display name. A retired
lineage is refused by id or any alias. There is no capability attestation, and
no runtime executes a child.

A valid V3 document is publishable long before all of it is executable: it
becomes an immutable revision under the same exact-bytes hash identity as V1 and V2,
and the revision projection names its format and says what still has no owner -- an
authored form nothing binds, or a pinned reference no published revision answers -- by
the same two rules the start applies, so no reading promises a start the service then
refuses; an invalid one is refused at publication carrying that named node and field. One shape of
it runs: a single line of Agent, Wait and linear Action nodes, each entered by at most
one dependency and followed by at most one dependent, declaring no optional form the
runtime does not bind. `required_context` and `available_context` are parsed target
forms but remain in that refused set; neither is a template parameter and no start
substitutes its authored source or revision. Per-run work instead enters through a
declared `graph_input`, supplied as exact `RunInput` material. A document outside
that shape is refused at the start naming what it is waiting for — a node kind
nothing interprets, a branch nothing chooses between, an authored form nothing
binds — rather than naming its version, and writes no run. The V1 and V2 document
grammar is deleted: the parser admits format 3 only, and a document declaring
format 1 or 2 is refused rather than read.
[ADR 0006](../decisions/0006-node-vocabulary.md) owns this vocabulary and the staging
rule behind it.

Inside that shape the runtime drives the line its author wrote. Each Agent node runs
its attempt through the same durable path a V2 node uses, and the heir its author
declared starts when its predecessor completes. A linear Action node pins a published
adapter-operation revision; the runtime dispatches each durable intent by its persisted
operation and exact adapter binding, and its closed registry performs `open-pr` and
`push-atelier-commit`. An Agent reaches the push only through a tool grant that pins
that exact published operation revision; a node may pin at most one exec-shaped grant,
redeemed inside its own attempt, and at most one effect-shaped grant, redeemed after it
succeeds -- two pins of the same shape are refused once their published capabilities
say so.
`POST /adapter-operation-revisions` is the publication door (bytes in, hash out,
idempotent), and a start whose `operation.revision` is that hash gets past the
reference that used to refuse as unpublished. A project-bound open-PR Action declares
one `body` input read from a builder Agent's own output through the dependency
closure that already orders them, so review and a Wait may stand between the two; the
request carries that output and the run's derived work-item branch, and the same
Agent owns the confirmed push receipt the branch is read against. A
documentation-release Action instead binds its declared candidate and independently
approved verdict orders into ADR 0010's closed draft-release request. That is a
separate operator-authorized work-item run: the candidate digest covers the
canonical base/change/title/body projection, the platform publishes those exact
replacement bytes through the existing push fence before opening the draft PR,
and `revise` never reaches either effect. A
V3 Agent request hashes the current job composition:
declared root-string orders appear as their text while every other order keeps its
JSON representation. Readers first recompute that composition, then prove a
pre-change request against the legacy all-JSON composition when necessary. The
attempt record does not yet persist its composition version; that unambiguous
future choice requires the next schema hop. Tests inject a fake GitHub
`EffectAdapterFactory` that records a branch and pull-request number, writes the request hash into the pull
request body, and answers a replay by readback rather than creating a twin. The
served host composes the loopback adapter unless the served project's
source-connection record names a GitHub source, in which case serve composes the
live githubkit adapter from that record. The token never enters the lease,
a receipt, an event or an API projection, and the lease listing has no `.git`.
ADR 0010 stays PROPOSED. A Wait node holds the run in
`WAITING_INPUT` as a durable state rather than as work in progress: nothing is queued
behind it, a restart finds it still waiting, and it moves only when a person answers.

A terminal, non-looping V3 run can be forked from one node of that same revision.
The successor does not edit or copy the origin: every successful node strictly before
the restart node is an immutable reference to the origin execution and its receipt,
event and declared Context-Package evidence. The successor creates node requests and
declared packages only from the restart node onward. Confirmed effects at or after that
node are fenced across the two runs: an identical request reuses the confirmed result
without calling the adapter, while different bytes stop at reconciliation rather than
replaying an already-opened pull request. The run list and detail expose the same
origin/successor lineage and mark referenced nodes on the rail. Forking does not change
the workflow revision, configuration, orders, budget accounting, or queue policy;
nonterminal origins and looped workflows are refused.
The V3 run page offers retry-from-node with a confirmation of the carried prefix
versus the nodes that run again; it does not change model, workflow revision, or
budget, because the served fork body has no such fields.
The V3 run page shows that wait as an answer card. A Wait with no inputs shows
the authored prompt unchanged; a Wait with inputs shows that prompt composed with
each declared graph input or named predecessor output. The exact composed question
is the durable pause payload and its payload hash is the integrity check; restart
readback serves that same pause rather than recomposing it. The card sends typed
bytes through the same `POST /runs/{ref}/answers` door the API already proved.
What that answer may be is the node's own declaration — a V1 or V2 Wait names an
`answer_type` and admits the canonical text of an integer, while a V3 Wait declares one
output with a schema and admits exactly what that schema admits, judged by the same
profile owner that reads every other value the run produces. An answer the schema
refuses is named as no answer at all and leaves the run waiting for another; an answer
to a run that is not waiting is refused as the state conflict it is. The admitted
answer is kept as the event's own bytes and carries the run to the next node, or, where
the Wait node is the line's sink, to the run's own terminal hash.

The same V3 run page lets the operator stop an honestly-cancellable run. Whether a
run can be cancelled is the server's word, published on the run resource as a closed
predicate rather than guessed from the rail: a run is cancellable while it is `STARTED`
on an agent node whose live attempt this cancel could stop, and while it rests at a
pause nobody has answered, and every other standing carries its own operator sentence —
between two nodes, waiting for a person to resolve an Action, a node that runs no agent,
already cancelling, already ended, an answer still being applied — instead of a grey
disabled button. Cancelling is a staged decision: a real question naming the consequence
this run would actually pay — a working agent stopped, or an answer nobody will now
give — confirmed before the irreversible boundary, whose command travels the same audited
pending/durably-accepted/uncertain/retry path an answer does, keyed by one idempotency
key so a lost reply is resent as the same command and never a second cancellation. One
durable winner is projected honestly. When a concurrent success finished before the
cancel reached it, the run keeps going and the cockpit says so rather than reporting a
false cancellation; when the cancel wins, the attempt's cleanup ends the run under its
own terminal word `CANCELLED` — not `FAILED` — with a `cancelled-by-operator` receipt on
the stopped node, and a server restart mid-cancel still lifts the run `CANCELLED` under
the same command identity. A reload during an unconfirmed cancel stays honest, offering
Retry and Discard rather than claiming the run is stopping, while a reload during an
accepted cancel keeps reading "Stopping this run".

A run resting in `WAITING_INPUT` ends differently, because it holds no attempt to stop.
The cancel writes its own attestation — a `WAIT_CANCELLED` event carrying the minted
command id, fenced by the node execution the confirmation named — and the run reaches
`CANCELLED` inside that one transaction, with that event folded into its terminal hash.
Nothing converges afterwards, so the door answers with the ended run rather than with a
cancellation still to come, and a retry of the same idempotency key reads that event
back instead of minting a second command. One refusal outranks the cancel: an answer
already accepted and not yet applied leaves the run waiting under the name
`answer-in-flight`, because a message the product has told a person it took is never
dropped to make room for a later command. A pause held open for an Action's
reconciliation stays non-cancellable — a live intent stands behind it.

What makes a V3 agent node executable now includes the shape of its answer. The
one enforced shape is `single-json-output/v1`: exactly one declared output, whose
whole decoded bytes are its value. A node declaring none — bytes no schema could
judge — or several — one value answered by another — is refused under the name
`agent-output-shape-unavailable`, before the run is written and before any
provider process starts, while the document itself stays publishable. What comes
back from the provider is then read against that schema by the profile owner
above, inside the transaction that would have written the success and before its
first row: an answer the schema refuses leaves no agent receipt, no completion
event and no advanced run, so a run can no longer end successfully on work its
own contract rejects. That one value is the provider's answer and, for a node
that kept a candidate, the atelier's own patch of the tree behind it: where the
node's published output schema declares a `candidate_diff` property, the runtime
writes the credential-scrubbed diff from the pinned tree to the candidate into
the value under it — never the provider, whose own bytes stay exactly what the
agent receipt keeps — so the node that reads the value next judges the change
rather than an account of it. The patch is cut, with a marker saying so, where it
would push the value past what a produced value carries, and the value is read
against that same declared schema once the patch stands in it -- a node whose
author declared the property as something a patch cannot be, or whose answer
fills the produced value so completely that not even the marker follows it, ends
`FAILED` under `PRODUCED_VALUE_REFUSED` with the `produced-value-refused`
receipt, rather than succeeding with the property quietly absent. That is a
separate word from the provider's `OUTPUT_SCHEMA_REFUSED` because the refused
bytes have a separate author: the provider wrote none of them, so it is neither
named for them nor asked for a repair round about them. The catalog
`code-review` and `plan-review` result schemas refuse a `revise` with no
finding or risk and admit `cannot-judge` only with a
reason, so a reviewer that cannot judge the evidence names that instead of
emitting an empty revise. Both catalog review workflows take a required `context` order carrying the owner-document excerpt the reviewer must judge against; a head that has no excerpt passes the explicit word none. The refusal is durable and named. The record family ADR
0006 declared has its production writer: the public start persists each node's
`node-execution-request/v3` and `context-package/v3` inside the start
transaction -- an order the run carries binds into that package as a material
member under its content hash -- and the terminal write ends the execution in
the same transaction as the agent receipt. A refused answer ends its attempt
`FAILED` under `OUTPUT_SCHEMA_REFUSED` with an `AGENT_FAILED` event, and the
`failed` `node-receipt/v3` carries a compact, bounded schema-refusal diagnosis as its reason:
it still names the violated place and rule, but never embeds the rejected
bytes. On that same family, it keeps the schema revision and the hash of the
exact decoded bytes the judgment used; the run itself ends
`FAILED` under that same reason — the node's ending lifted one level, so the
studio no longer lists it as Running. A success additionally keeps the exact
produced bytes as `node-artifact/v3` beside its `succeeded` receipt, and that
receipt names the same identity. An older receipt written before those fields
existed stays readable: the identity is honestly absent, not a refusal and not
corruption. Claude, Codex and Grok each take a decoded answer through that
same success-write seam; bytes the schema refuses end under the same token
for all three. The node detail reads the stored reason back, and a run started
before this writer existed stays honestly absent in those tables. A store that
still holds the old STARTED-after-failure shape is ended the same way at the
next serve start. A leftover whose last attempt on the current node is already
`INTERRUPTED` under the durable `atelier2-driver-lost` command, with no
replacement still in flight, ends the same way: the run becomes `FAILED` and
that existing interruption event is the named reason — no new event, no
attempt rewrite. A V1 run cannot take that lift, because the frozen V1 wire
refuses `FAILED`. A run that advanced past a succeeded predecessor onto a
node that never prepared an attempt is leftover only when the durable node
workflow that would start that attempt is missing or belongs to an
application version the running executor will not recover: the failed
`node-receipt/v3` on the current node names `atelier2-driver-lost`, and the
run becomes `FAILED` without a new event. A silent successor whose node
workflow is still pending under the running application version is left
standing.

The other way an attempt ends badly now says as much. A provider process that
leaves no usable answer ends `FAILED` under `PROCESS_EXITED_UNSUCCESSFULLY` on
that same seam, and its `failed` receipt carries what the supervision saw --
how the child ended (an exit code, a signal, or a clean exit whose answer no
executor could read) and a bounded tail of its standard error, under the token
`process-exited-unsuccessfully`. The node detail and the `run` command read that
reason back, and an ending nothing recorded is reported as exactly that rather
than as an empty one. Standard error stops at the receipt: the `AGENT_FAILED`
event keeps carrying the bare failure code, so the event stream stays a bounded
surface anybody may subscribe to. The bounded vocabulary deliberately not
written here is now only the blocked receipt disposition; the cancelled receipt
disposition on the running node and the run's own terminal word have their
writer. #439 named both tokens durably --
`NodeReceiptReason.CANCELLED_BY_OPERATOR` and `RunState.CANCELLED` -- and an
operator's V3 run-cancel command constructs them: its attempt's cleanup writes
the `cancelled-by-operator` receipt and lifts the run `CANCELLED` under the same
command identity, on either carrier and across a restart taken mid-cancel.

An agent is authored as one markdown file. Its frontmatter is a closed set of
`name`, `description`, an optional `model`, and an optional `tools` declaration;
the body is the system prompt, kept byte-exact. An absent `tools` field leaves
the agent able to use every tool its executor offers, because a restriction is
only ever explicit, and a present one declares exactly that closed set. Every
other key, missing required field, unreadable value, duplicated key or tool, and
empty prompt is refused by its own name. A definition renders back to a canonical
document that reads as the same definition — the declared tool set always as a
sequence, so a tool name that itself spells the comma an author may separate by
survives the round trip — and the authoring helper maps it deterministically to an
existing agent-configuration revision, with the deployment rather than the file
owning the authentication profile, the executor, and the model an unspelled one
falls back to. That mapping is not a binding to the definition revision. This is
the authoring format alone: nothing enforces a tool declaration yet,
no serving surface publishes a definition yet, and today's configuration revision
carries no field for a name, description, tool declaration, or system prompt, so
the published revision alone cannot reconstruct the definition it came from.
Where an authored definition durably binds is decided by
[ADR 0007](../decisions/0007-catalog-identity.md). Its exact bytes can be reconstructed
as an `agent_definition` revision today, but no serving surface publishes that
definition and no agent configuration references it; the round trip therefore
does not yet prove the configuration-to-definition chain.

The catalog carries the planner that cuts an item into buildable slices.
`workflows/breakdown.yaml` is executable at revision `69b1b419`: one headless
planner node reads the item body, its owner documents and the named
accepted-sentences form it was handed, and answers with slices that each carry a
typed priority — one positive integer rank, not a prose priority — and sort every
handed sentence into exactly one slice, under `proves` where that slice proves
it or under `defers` beside the one owner sentence naming who will. A slice that
leaves a handed sentence unassigned is not an answer its schema takes, and a
call handed no sentences says exactly that instead of reporting an empty
assignment as a complete one (PR #960). What the planner does not do is turn its
slices into work items: the authorised creation, with the readback and receipt
that would make it durable, is not built (#80). A computed schedule is not part
of it and is not owed — the operator ruled that out on 01.09.2026, and the wish
stands as an idea of its own (#969). The form between an idea and a cuttable
item, where a refine step proposes expectation lines until the item is regulated
enough to cut, has its own owner (#843).

The catalog also carries the circle the head walks for one work item.
`workflows/head-loop.yaml` is one line of seven nodes: a headless planner
re-questions whether the item's problem still exists or a landing, a ruling or
the vision has overtaken it; a wait asks the operator to pull the item, which is
the only question before anything is built; a reviewer judges the plan the item
carries against its owner documents; then the builder, review, release and
open-PR nodes of `issue-to-pr` follow, under the same tool grants, budget,
schemas and adapter operations. Two things about the builder differ: it is
handed the reviewed plan under `result of plan_review: result`, the label a
composed job gives an earlier node's work, and follows the item body where the
two disagree, and it pins no model, so the cast rather than the document decides
which provider builds. Plan review and candidate review are one role, filled
once, in a provider family different from the builder's. Its two waits are
answer-or-cancel doors: their schemas admit one word
each, so refusing means cancelling the run. What the document is not: there is
no fix round, so a `revise` still ends at the release wait exactly as in
`issue-to-pr`; and there is no landing door, so nothing here merges a pull
request, and the board projection after a landing stays the head's own work.

V1's graph is intentionally narrow: Agent delegates its configured job and exact
output contract through an injected provider-neutral executor and atomically
records a distinct success receipt with its existing event and successor. Action
owns the existing exact effect and reconciliation contract, Wait accepts one exact
integer answer, and the terminal Subworkflow adds two configured integers. The
document is a closed safe-YAML contract; unknown fields, unsafe YAML features,
cycles, unreachable nodes, changed retry identities, and contradictory answers
fail without mutating durable state. [ADR 0002](../decisions/0002-exact-yaml-graph.md)
owns that graph contract.

The executor still performs an Action only after authoritative absence. An
unknown outcome becomes `WAITING_RECONCILIATION` with a durable reason; one
accountable command may resolve it, after which initial and reconciled Actions
share the same continuation path. A raised adapter error is that same unknown
one process later: it ends the effect or reconcile workflow in a terminal
error status nothing replays, so a serve start routes every intent such a dead
workflow still owed — a `PREPARED` one under the exact transition an in-band
unknown takes, a `RECONCILING` one by closing its dead command and reopening
the door — to `WAITING_RECONCILIATION`, never to an invented absence. That door
lifts a live run, so an intent whose run has already ended takes no door at all:
it becomes `ABANDONED`, the run's own ending said on the intent, claiming
neither a receipt nor an absence and keeping the prepared request bytes
readable. Initial receipt creation commits atomically with intent confirmation. Reconciliation resolution separately commits its
receipt, intent, command, run, and resolved event. The later `ACTION_COMPLETED`
transition is another crash-safe transaction.
[ADR 0001](../decisions/0001-durable-runtime.md) owns the runtime and recovery
boundary.
