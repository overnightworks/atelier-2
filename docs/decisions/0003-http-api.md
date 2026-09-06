# ADR 0003: The HTTP API projects durable workflow truth

- Status: ACCEPTED 2026-08-11 — implemented: the projecting API landed with this
  record and serves under `/atelier/api/v1`
- Amended 2026-08-30 (#904, from #901): the V1 and V2 families this record froze
  are deleted. See "Amendment 2026-08-30" below.

## Context

The durable runtime can recover a workflow after process loss, but callers need
a stable control and observation boundary that preserves those guarantees. An
in-memory event broker, API-owned command state, or process-local retry answer
would create a second truth and make reconnect behavior depend on which server
process receives the request.

## Decision

FastAPI owns a thin, versioned adapter at `/atelier/api/v1`. The API publishes
secret-free auth-profile and agent-configuration revisions, exact safe-YAML
workflow bytes, and exact JSON Schema bytes, starts runs from published
revisions, projects revision and run pages, accepts Wait answers,
current-attempt cancellation commands, and reconciliation commands, and streams
the eleven implemented durable event kinds. A subscriber who does not already
know a run holds `GET /events`: opening that stream is the subscription. The
feed emits only `WAITING_INPUT` and `AGENT_FAILED`, in the same SSE envelope
and `VersionedRunEventResource` bytes as the per-run stream. `Last-Event-ID`
is the last emitted `event1` cursor; resume is same-instant identity exclusion
from that cursor's instant T:
`recorded_at > T OR (recorded_at == T AND (run_id, seq) not among identities
already emitted at T)`. Last-Event-ID seeds the set with that event1 only; a
live holder adds each identity it emits and resets the set when the second
advances. Lexicographic `(recorded_at, run_id, seq) > cursor` is not the
resume rule, because `recorded_at` is second-precision and two waits in one
second is the normal case. Events whose V22
instant is NULL stay off this feed; the feed does not invent a time. It does not accept credentials or own a parallel run, command, or event
state machine. Cancellation returns `202` while cleanup is pending and `200` for
an exact terminal retry. Stale, terminal, non-current, conflicting-command, and
forbidden-replacement requests are distinct closed problems.

A V3 run is cancelled through `POST /runs/{public_ref}/cancellations`, distinct
from the attempt door above. Its body carries only the operator's opaque
`idempotency_key` and D2's `expected_node_execution_id`; the durable command id
is minted server-side into a reserved namespace, so no request field can name a
command or force that namespace. It returns `202` while the run's cleanup is
pending and `200` for an exact terminal retry, on the same versioned run
resource the other doors serve. Beyond the pending/terminal pair it carries a
third exit the attempt door does not: when a concurrent success ended the
targeted attempt before the cancel landed, the run kept going, so naming it
terminal would lie -- that race answers `409
run-cancellation-overtaken-by-success` rather than a false `200`. A run that is
not cancellable and a conflicting-command retry are their own distinct closed
problems (`run-not-cancellable`, `run-cancellation-command-conflict`), and
whether a run can be cancelled at all is the server's own
`RunResourceV3.cancellation` predicate, not the cockpit's guess.

A terminal V3 run is forked through `POST /runs/{public_ref}/forks`. The closed
body carries only `idempotency_key` and `restart_from_node_id`; the command and
successor identities are server-owned derivations. The first accepted command
returns the successor `RunResourceV3` with `201`, and an exact retry returns the
same resource with `200`. A missing origin is `404 run-not-found`. Nonterminal
origins, missing restart nodes, looped workflows, a non-reusable prefix, a
different target under the same command, and unavailable executor or runtime
capability are closed `409` refusals. Admission failure remains `503`, while
corrupt durable truth and an unrepresentable durable projection remain closed
`500` problems. The API never edits the origin and never accepts a successor id
or replacement revision from the caller.

A value that crosses from an answer into the next request is named for what it
identifies, not for where it stands: a workflow's revision hash is
`workflow_revision_hash` and its format version `workflow_format_version` on
every body that carries them, a published schema or budget revision names its
own kind, and an artifact travels as `artifact_hash` from the publication that
minted it into the order that names it. No published body carries a bare
`revision_hash` or `format_version`, and no path parameter is named
`revision_hash`; only the grammar of the authored workflow document keeps the
bare words, because inside a document each means one thing. A declared order
answers the author's own `schema: {ref, revision}` hull rather than flattening
it under other names. Moving the
revision and catalog resources onto that language was a second explicit breaking
pre-release migration, taken for the same reason as the SSE envelope below and
under the same condition: no external consumer exists, and the cockpit, the
command, the MCP door and the frozen document migrate together.

The Wait-answer request is the third explicit breaking pre-release migration.
It carries an actor and the exact `expected_node_execution_id`; a V3 run serves
its current execution id from the run, revision, current node and round rather
than borrowing a cancellation target. The durable store validates the current
run head and any answer already bound to it under the write transaction before
inserting anything. The request actor is a closed wire value; an unknown actor
or malformed body is `422 invalid-request`, before the durable seam. The store
reads the actor recorded on the exact `WAITING_INPUT` head and a mismatch takes
that same named refusal. A proven prior waiting execution that was never
answered remains the definitive `409 answer-execution-stale`. The same answer
from the same actor is
idempotent: PENDING returns `202`, while an already APPLIED answer returns `200`.
Different bytes for an execution that already holds an answer, pending or
applied, are `409 answer-state-conflict`, and the first answer stands. A
missing or duplicate answer row for an answered execution, contradictory
bindings, or a WAITING_INPUT run pointing at a non-Wait node is
`500 durable-state-corrupt`. V3 `WAIT_ANSWERED` receipts
require a non-null actor attribution. V45-to-V46 migration names its distinct
historical case `legacy-unattributed` instead of making attribution optional or
inventing `operator`; new answers record `operator`. The cockpit, MCP
projection, request schema, problem
vocabulary, and frozen OpenAPI document migrate in the same head under the same
pre-release condition.

The SSE event resource is one exact closed union, and the workflow, start, and
run resources are each their own closed family. Start uses the closed shape
itself to select which start it is.
A run projection includes its immutable, public binding matrix. An
`AGENT_COMPLETED` event carries canonical Base64 plus the exact output hash so
arbitrary bytes never pass through UTF-8 decoding. The event family publishes
only the agent kinds a format-3 line actually writes.
The SSE envelope carries only `id` and `data`: omitting the transport
`event:` field makes every frame a default `message`, while `data.event` is the
sole domain discriminant. The per-run stream has exactly two frame shapes under that
one envelope: a durable event, which carries its cursor as `id`, and the
terminal failure frame `STREAM_FAILED`, which carries a problem body and no
`id`, because a resume cursor on a refusal would invite the browser to
reconnect into the same refusal forever. The attention feed `GET /events`
adds a third shape, `RUN_PROJECTION_CORRUPT`: it names one unprojectable run
with problem `durable-state-corrupt` and that run's `public_run_reference`,
carries the underlying attention event's cursor as `id` so resume continues
past it, and does not end the subscription. That run's own
`/runs/{public_ref}/events` stream still ends with `STREAM_FAILED`.

Every mutation delegates to the runtime owner and decides created-versus-
existing from the row written in that same transaction. Only a newly created
command schedules continuation. Starting a run verifies its published revision
inside the start transaction. Reads use short-lived SQLite connections behind
separate bounded admission for ordinary control work and event-page polling.
Admission has its own injected wait deadline; refusal is a pre-header 503 for
control work and ends an already-started stream regularly. Database timing has
three explicit owners rather than one misleading clock: the composed SQLAlchemy
engine bounds pool checkout, the query adapter bounds SQLite lock waiting, and
its progress deadline starts only after checkout. None is described as cancelling a
running thread. An idle stream backs off deterministically to a configured
ceiling and resets that delay after progress. Cancellation keeps the applicable
bound occupied until the underlying blocking durable call has actually returned.

Run references are canonical `run1` encodings of the domain's UTF-8 run ID.
Both V3 list and detail resources carry the same optional fork-origin record and
bounded successor lineage. Reused strict-prefix nodes remain ordinary succeeded
rail entries, augmented as one all-or-none group with the source run reference,
source event hash, source receipt hash, and source declared Context-Package hash.
The resource does not copy the origin receipt or output.
Event cursors canonically bind that reference to a positive durable sequence as
`event1`. Run pagination uses SQLite's `BINARY` ordering for `TEXT`, then refuses
the projection if that observed order or boundary disagrees with the exact UTF-8
byte order required by the API. `Last-Event-ID` is an exclusive acknowledgement:
the stream begins with the next durable event. Reusing an older cursor
intentionally replays unacknowledged events, and reconnecting to a new process
reads the same history from the durable store. A terminal stream ends only
after its durable tail has been delivered.

JSON resources and commands are closed typed models. Publications of exact
bytes rather than a typed model are workflow (`application/yaml`) and the
JSON documents whose hash is of those exact bytes: schema, budget, and
tool-grant (`application/json`). Taking bytes does not mean saying
nothing about them: the workflow publication body carries the shape of the
document, derived from the same models the publication reads it against rather
than written a second time. The shape decides the form; whether a named edge
resolves, whether a cycle closes and whether this build executes the result are
statements about the whole document and keep their named refusals. A refused
path names the location of the OpenAPI document, so a consumer holding only a
base URL reaches all of this without guessing. Each schema-profile refusal the
store already names is a distinct closed problem, not a generic invalid
request. Centrally injected limits reject declared
oversize bodies before route parsing and stop undeclared or chunked bodies while
they are received; they also bound individual fields, encoded and decoded payloads,
workflow graphs, response projections, and concurrent query work. Durable
control-read projections outside those limits have their encoded workflow bytes
refused before YAML parsing and are refused before serialization as the 500
`durable-projection-unrepresentable` problem; their durable rows are not changed.
After an SSE response has started, the same failures the REST surface names are
named in the stream: a corrupt or unprojectable durable row, a durable row
outside the configured projection limits, and a port that breaks its page
contract each end the stream with a terminal failure frame carrying the problem
body that surface would have answered. Only backpressure and transient store
unavailability end the stream regularly, because a client's own reconnect is
the correct answer to both, and a client that reconnects into a permanent
refusal would never be told. Errors are closed RFC 9457
`application/problem+json` variants on both paths. The generated OpenAPI 3.1
document is built and validated eagerly during application construction and adds
a documented extension naming both SSE frame shapes. The published failure frame
promises exactly the problems the stream can speak, not the open problem shape,
so it never offers a consumer a body the closed vocabulary would refuse.
Streaming uses FastAPI's public `EventSourceResponse` and `ServerSentEvent`
mechanisms.

This default-message envelope is an explicit breaking pre-release migration
from named SSE frames. Atelier 2 had no external consumer at the decision point;
the sole local cockpit and tests migrate together. A cockpit tab left open
across that deploy must reload. The durable payload, cursor, native reconnect,
and `Last-Event-ID` semantics do not change.

## Consequences

- HTTP retries and server restarts preserve the runtime's durable idempotency;
  the API adds no recovery log or broker.
- Stream delivery is replayable rather than exactly-once. Clients acknowledge
  progress by reconnecting with the last cursor they have durably consumed.
- Adding a new public event or problem kind changes a closed contract and must
  be treated as an API-version decision.
- Auth-profile and agent-configuration publication stores selection metadata,
  not credentials; credential lookup remains a future host/provider concern.
- This boundary supplies no authentication, browser CORS policy, provider or
  platform integration, cockpit, process supervision, or deployment. A host
  must supply any required access boundary before exposing it beyond a trusted
  environment.

## Amendment 2026-08-30 — the V1 and V2 families are deleted

This record originally froze two things that no longer exist: that "V1 and V2
workflow, start, and run resources remain their own closed families", and that
"the preexisting V1 raw JSON and named OpenAPI components are byte-frozen".
Both are withdrawn.

[docs/PRODUCT.md](../PRODUCT.md) § Stage: prototype owns why no fallback and no
backwards compatibility are owed here. #901 measured what that costs: 239 live
runs at `workflow_format_version` 3, all 26 stored workflow documents at
`format_version: 3`. No run or document of format 1 or 2 has ever existed, so
the frozen components described shapes nothing could produce.

The freeze was defensible while a consumer might hold those bytes. None does:
the only client is this repository's own cockpit, whose API client is
hand-written zod rather than a generated one; there is no codegen step; and
`serve` is loopback-only. Re-freezing the document therefore breaks no
external contract.

What the API serves after #904 is one family. The published document describes
only what the system can answer with — no resource for a run nobody can start.
Durable state naming any other format is refused as corrupt rather than
projected into a shape that would have to lie about it.

Unchanged by this amendment: every frame string remains a value and remains
frozen, including the eleven that hold hashes in the live store. A class name
is a name and is free; what stands in the first argument of a `frame(...)` call
is neither.

## Supersedes

None.
