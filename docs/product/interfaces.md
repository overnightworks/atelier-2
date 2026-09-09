# Interface status

An HTTP API now projects that durable state under `/atelier/api/v1`. It can
read the queue through one typed `GET /queue-items` projection across observed,
proposed, and admitted rows; read a project's queue capacity policy, every
field and its current revision, through
`GET /projects/{public_project_reference}/queue-policy`, and revise it with a
CAS-guarded `PUT` on that same path; write the priority, workflow lineage, and
prerequisites the operator will inspect through
`PUT /queue-proposals`; and confirm exactly that proposal through
`POST /queue-admissions`. The policy revision also carries the project's
`automation_label`: with one named, the sweep confirms every inspected
proposal whose tracker item carries that label under the `AUTOMATION_RULE`
authority, and with none named it admits nothing automatically. It may also
carry `default_workflow_lineage_id`, `default_priority_rank`, and
`automation_disposition_default` -- stated together or not at all -- and then
a labelled item that carries no proposal is proposed from them, with
`proposal.source` reading `POLICY_DEFAULT` instead of `OPERATOR`. The
disposition is `HUMAN_REQUIRED` unless the operator states otherwise, so the
defaults propose work without releasing it. The priority
wire shape is `{"rank": n}`. Queue
responses use the contract's typed state, authority, automation disposition,
and blocker values. A tracker read failure never drops a durable queue row: the
resource instead reports `ENRICHMENT_UNAVAILABLE` while the row still carries
the tracker title last observed at import, its observation timestamp, and,
for a retired item, when import saw it leave the tracker's open set.
It can
publish secret-free auth-profile and agent-configuration revisions and list
both (a listed configuration names its live `startable` answer and, when
false, the exact reason: no executor bound, the model registry pointing
elsewhere, or only its live receipt still missing); publish
exact JSON Schema revisions; publish and
inspect immutable workflow revisions; start, list, and inspect V1 or V2 runs
(the list accepts a `state` filter so a consumer can ask which runs wait;
a page is admitted by one `PageLimit`, not a restated 1-to-100;
a persisted run format is one `WorkflowFormatVersion`,
not a restated 1-2-3 CHECK;
a cancelled attempt's cleanup disposition is one
`AgentAttemptCancellationDisposition`, not restated tokens on query and SSE);
list and inspect a V3 run from the published document it was started
against, not today's executable parse;
read the agent receipts a run has written;
for a bounded loop, every run query selects the durable current round's exact
node execution, while the receipt list and event page retain every round and
stream preparation agrees with the page about the one terminal event;
an `invalid-request` names the field and reason the validator already knew;
answer a waiting node; cancel the current V2 Agent attempt with an optional
single replacement; cancel a V3 run through `POST /runs/{ref}/cancellations`,
which carries only the operator's opaque idempotency key and the node execution
its confirmation fenced on and answers the closed cancellation vocabulary
(accepted, terminal retry, overtaken by success, not cancellable, command
conflict); submit an accountable reconciliation; and follow the
closed durable event history as a resumable server-sent event stream.

The `{ref}` on `GET /runs/{ref}` and `/runs/{ref}/events` is the public run
reference: `run1.` followed by the base64url encoding of the durable `run_id`,
without padding. Every run door takes exactly that form, and the start
response supplies it. A `run_id` of `live-1123-pass3-20260904T105844Z` is
`run1.bGl2ZS0xMTIzLXBhc3MzLTIwMjYwOTA0VDEwNTg0NFo`.

A subscriber who does not already know a run holds `GET /events`; opening that
stream is the subscription. The Workbench holds that stream, so a wait that opens while the operator
watches appears without a reload and without `POST /subscriptions`. The feed is closed to
`WAITING_INPUT`, `AGENT_FAILED`, and `ACTION_RECONCILIATION_REQUIRED` — the
events that stop a run until an operator acts — in the same envelope and
`VersionedRunEventResource` the per-run stream emits. A run whose projection cannot be served is named on this feed as `RUN_PROJECTION_CORRUPT` (`durable-state-corrupt`, `public_run_reference`) and does not end the subscription; that run's own event stream still ends with `STREAM_FAILED`. `Last-Event-ID` resumes by same-instant identity
exclusion: from that event1's instant T,
`recorded_at > T OR (recorded_at == T AND (run_id, seq) not among identities
already emitted at T)`. Last-Event-ID seeds the set with that cursor only; a
live holder adds each identity it emits and resets the set when the second
advances. Second-precision instants make two waits in one second the normal
case, so lexicographic `(recorded_at, run_id, seq) > cursor` is not the resume
rule. Pre-V22 events whose instant is NULL
stay off the feed rather than inventing a time. A served V2
run also names the state of every node of the revision it is bound to, so a reader
is told where each node stands instead of computing it: one pure function in the
core derives that rail from the run snapshot, that revision, and the events since,
with the snapshot authoritative only until an event overtakes it. A failed
terminal snapshot names the failed node and the attempt that ended it, so a list
read matches the event stream. A node refused before any attempt existed -- an
executor nothing can bind, a work-item claim the ledger would not grant --
names its closed `reason` and, as `detail`, the sentence the refusing boundary
gave, so the node detail and the journal say why the door was shut and not only
that it was. Success carries exactly one name on the wire. Existing
V1 JSON and OpenAPI component bytes stay pinned so nothing widens them by
accident — they moved once, deliberately, when every body learned to name a
value the way the next request writes it — while exact V2 unions expose
the run's safe binding matrix and byte-safe Agent output, and the event stream
answers a format-3 agent or wait event as its own family rather than dressing it
as V1 — a format-3 pause naming no answer type, because that format's Wait node
declares a schema instead, and its answer travelling as bytes rather than as the
decimal text only an `integer` wait can honestly produce.
Public references are transport identifiers, not new domain identities, and
retries report whether a command was newly accepted or already existed without
duplicating its durable
write or wake-up. The API also describes the one body it takes as bytes: a
guessed path is refused with the exact location of the OpenAPI document, and the
workflow publication body there carries the shape of the document itself —
derived from the models the publication reads it against, so no second
description can drift. That shape decides the form; the rules only a whole
document answers keep their named refusals at publication. It also answers in
the words the next request is written with: a workflow's revision hash and its
format version are spelled the same on every body that carries them, the path
that reads one revision is `{workflow_revision_hash}`, a declared order answers
the author's own `schema: {ref, revision}` hull, a published schema or budget
revision names its own kind, and material published as an artifact is ordered
under the address the publication answered. A machine consumer assembles each
request out of fields the answers before it named, without a translation table
of its own. An order or wait answer whose schema's top-level `type` is
`string` carries its raw UTF-8 text as the value; every other schema carries a
JSON document.

The stdio MCP `start_run` tool accepts artifact and work-item orders only;
inline orders remain an HTTP-only form until their retirement is a later slice.
Publishing material and starting a run are two calls. If the start is refused or
fails, its already-published immutable artifact remains reusable and no run
exists.

`publish_artifact` names its material one of two ways. `content_base64` carries
bytes the caller is composing in the call itself and accepts at most 1,047,552
Base64 characters, or 785,664 decoded bytes: the artifact store permits
1,048,576 bytes, but Base64 and the JSON-RPC request envelope must fit the
1,048,576-byte MCP line cap (1,024 bytes are reserved for that envelope).
`path` names material that is already a file on the machine the stdio child runs
on -- the child has the same loopback trust as the browser there, so it reads
the file itself and posts those exact bytes, and nothing large has to be
reproduced inside a tool call. It carries the whole 1,048,576-byte store bound,
and a path that is not absolute, is missing, is not a regular file, cannot be
read, or exceeds that bound is refused by name before anything is sent. Naming
both sources, or neither, is refused the same way. `GET /artifacts/{hash}`
answers the exact bytes as `application/octet-stream`, with a typed refusal for
an address nobody published; MCP mirrors it as `read_artifact`, which answers
the address and the bytes as Base64 and refuses an answer that does not hash to
the address it asked for. A run's own result is readable the same way: the
terminal hash a run resource carries, and the `output_base64` its
`/runs/{ref}/events` stream carries, both name material this door reads back.

A described listing and a single revision read both name where a revision's
bytes came in from: `provenance` carries the source's public `source1.`
reference and the commit, path and instant of the *first* intake that delivered
those bytes, so a later delivery of the same bytes never rewrites their origin.
It carries only what was true at that intake — where the source points today is
configuration a later connect may change, so no location or ref travels here,
and a resource answering for the source itself is a later slice. A revision
published through the catalog's own door carries no `provenance` at all.
Definition sources themselves stay a command-line surface: connecting, scanning
and taking one in have no HTTP route.

[ADR 0003](../decisions/0003-http-api.md) owns the API and resume
contract.

A narrow local cockpit can list runs, publish and start a workflow from `/new`,
and project one durable run's bound revision, state, nodes, and resumable event
history. A V3 run, its list row, and a node that has run carry when they
started and ended: the store keeps UTC. The project list shows the local date
and time on the row, newest activity first, and names that sort; the run page
still keeps the exact stamp behind the info affordance. Predecessor rows that
never recorded an instant stay empty rather than inventing one. Each
project-list row also shows the one project and, when the published revision
answers a name, the workflow. The saved-workflow picker offers one row per authored name the described
listing already publishes, not one row per revision hash. Several revisions
that share a name collapse; the catalog head from
`GET /catalog-revisions/by-name/workflow/{name}` is the default when that name
resolves, and older members sit in a collapsed revision choice. A name with
one listed revision has no empty submenu. A published title the catalog does
not hold is named Unlisted when it is a legal catalog name and Unnamable when
the title cannot be one — the picker does not swallow that 404. Those
refusals, and a row that cannot be started, each have their own shape, so a
choice is not a muted twin of a refusal. After a choice the list collapses
onto that card with a Change path, and the start form sits directly under it.
Unnamed documents stay one row each, as they did. `POST /library/additions` is
a separate durable intake: it accepts opaque octet-stream bytes with the
required declared kind `agent`, `skill`, or `workflow`, and records that
declaration beside its attribution. The response and `GET /library/additions/{intake_id}`
read back the intake identity and the exact declared kind. The same bytes under
two declared kinds are two intakes; this door neither recognises nor validates
their content, and it neither publishes nor admits a revision.
The catalog Import sheet is the operator door that supplies that kind:
recognition reports what it saw, and the person chooses Workflow or Agent —
the kinds the catalog can hold as a tile; that choice travels with the
addition. An uncertain file still gets those chips instead of a Close-only
refusal. A mistaken kind is named on the sheet before anything stands in the
catalog. After the intake it publishes through the revision door of that
kind and admits through the one catalog-lineage family, so a workflow and an
agent alike get a named lineage with numbered revisions, all or nothing. An
agent's lineage name is the `name` its frontmatter authored, read back from the
published revision; a name the catalog grammar refuses leaves the revision
published but unnamed.
Separately, `POST /library/recognitions`
says what a loose document is without writing anything: opaque bytes plus an
optional `file_name`, answered as a recognized workflow (format, authored name
and description), a recognized agent definition (name, description, provider
mark), a kind the library recognises but does not hold yet (a `SKILL.md` with
a closed frontmatter block, or JSON with `mcpServers`, each with its reason), or
unrecognized with what every kind expected and why these bytes are not it. A
document two markers claim is refused naming both; the file name is a marker,
not a tie-breaker, so a `SKILL.md` whose frontmatter is a valid agent
definition is ambiguous and only a `SKILL.md` that is not a valid agent is a
skill. The document publishes the byte bound of the body and the character
bound of `file_name`. Recognition reuses the workflow and
agent-definition parsers publication already runs; no skill or MCP store
exists. Details repeats what the published graph already answers —
format, roles and node count where the V3 resource carries them, executability,
declared orders with the schema each pinned, the lineage's revision history,
and the graph miniature. A hash sits behind a proof affordance — hidden until
asked, copied by a click, naming what it seals. Edit shows the exact published YAML and
publishes a new revision through the same door; a legal catalog name then
joins the lineage. Per-node outputs stay in that document; the preview does
not copy them. A known start-refusal or problem token is shown as a sentence with
a next action; an unknown token stays raw. The V3 graph also answers an excerpt of each node — id, kind, role,
the bounded start of an agent instruction, and the authored `depends_on`
edges. A wait has a prompt, not an instruction, so that field is empty there.
An entry node answers an empty edge list. The authored node stays in the
document bytes. A V3 run page draws that excerpt as topological layers and
paints each node's state from the rail the server already walked — shape and
colour together, no zoom, no drag. The page leads with the published workflow
name and keeps the run id as identity. A click into a node speaks Prompt and
Output, never Asked or Answered. The run head is the one standing sentence;
the node's Result tab carries the decoded declared output with the Exact-text
fold — a declared object's own `answer` field as one sentence with its other
non-empty fields named after it, a declared array as its own items, an object
with no `answer` field as all of its fields, a bare string as itself — the
declared bytes kept behind a collapsed disclosure. The Who panel labels the receipt's model as
the declared configuration model and says a provider-resolved model is not
recorded — the same honest absence as usage. A hash leads with its human
name and is copied by a click on that named control — the hex is the proof
behind the name, not the reading title. The live event line names which node
finished and does not paste the output the node already holds. A STARTED run paints the working node
as live work, not as a finished card, and shows new events from the existing
SSE door as they arrive. Empty, connecting, and failed stream states are each
named as themselves. The process log is not on that door — it stays in the
lease (#104) — and the page says so rather than inventing a progress bar.
Node detail now serves the stored transcript of a finished attempt; the Log
tab that would render it is still not built. The
live event line stays open until the events it has applied match the latest
cursor the run itself names, so a run that has already ended still shows every
node that finished. Details on the
saved-workflow picker reuses the same drawing without run state. A chosen V3 revision that declares
orders shows one field per order, shaped by the schema the author pinned: a
string schema renders a text area and a file door, an object schema always
offers a Raw JSON door -- beside its field-by-field form when every field is a
scalar the form can type, alone when a field is not (an array, a bare `enum`,
a nested object) -- and a work-item order renders the tracker picker; a
revision that declares none shows no field. Before the start, the cockpit
publishes each declared order's exact bytes through `POST /artifacts` (#1089)
and sends `orders` as `{name, artifact_hash}` for every order but the work
item, which the start reads and sends as `{name, work_item}` itself -- the
same publish-then-name-the-hash door the CLI and MCP already use, never an
inline value for a published order. Role
bindings on the Catalog detail's start sheet offer eligible registered
configurations by provider, exact model id, and readable Account. There is no
remembered role choice. For an admitted V3 workflow, that sheet reads the same
model-resolution door as every other start path. Each role is resolved by the
fixed order: a run-local override, an
exact workflow pin, the configured model default for its declared difficulty,
then a configured higher difficulty.
Missing or ambiguous pins and unknown overrides are terminal; they never fall
through to a default. A `family_differs_from` declaration is checked against
the final provider assignments, including overrides. Any roles left without an
assignment are returned together in one typed refusal naming the role, the
reason, and the family relation where that caused the refusal. The same
decision runs inside the canonical start transaction against one
host-configuration snapshot, so a run never combines registries and defaults
from different instants. The agent list is empty until a configuration is
published, and says so. The Workbench is the one workshop surface for
work that needs a person or is moving. Its stage is the notification surface;
the rail's ochre count is the notification count. Catalog, History, and
Settings complete the rail described by the blessed Mockup v8 and ADR 0019.
History's finished-run row names when it ran, the work item or an em dash,
a derived half-sentence of what came of the run, and a short public run
reference so two otherwise identical finished runs stay distinguishable —
not only whether it ended, and never the raw result bytes.
Settings is the door for connecting, disconnecting, and renewing a project
source token against the source collection, showing the pictured source row;
the residual singular source-connection GET remains for other readers
(History). It is also the one editing surface for each provider's exact model
registry and the three difficulty defaults, in that order. The startable configuration list is
the owner of that provider-grouped rendering: a provider whose registry is
missing, or whose entry is not yet checked, still renders, marked unavailable,
with the Check action that publishes a missing registry if needed, then asks
the server to append its dry-run result.
Registry rows name the exact model
id, Account, provenance, and current provider check as separate facts; adding
or removing a row writes immediately. Only checked, startable registry entries
are selectable as defaults. Defaults are shown
as Difficulty 3, 2, and 1; selecting a model or clearing a row is an immediate
write that replaces only that difficulty. The other two saved rows are carried
byte-for-byte. A new choice is admitted when its provider, model, and
configuration are a checked, startable registry row; a carried row stays
admissible if that provider later stops reporting it. The retained model,
Account, and unavailable state wrap as one visible surface
until that row is changed or cleared. Neither operation renders a saving or saved caption, and an uncertain
write retries its identical bytes. Check is one operation; Retry of an
uncertain publish or validation resumes at that step and continues through
validation. Settings does not read or count runs; the
Workbench alone owns that live-work signal. The new-run trail names the project the same way the other
levels do. It can answer the exact integer requested by a Wait node and resolve an
unknown Action outcome as either an exact found effect or an accountable,
confirmed absence. For a V2 run it renders the node states the API names rather
than deriving them — the V2 event stream carries the rail with every event, so
nothing V2 is derived in the browser; the one named exception is the V1 half,
whose run resource is byte-frozen and which dies with the V3 cutover — and the
only state rule left in the browser is a client-owned interaction overlay that
lifts a node needing the operator while his form is open and stills it by that
open form alone. Its session-scoped mutation journal preserves exact retry
bytes without becoming a second durable truth. [ADR 0004](../decisions/0004-local-cockpit.md)
owns this browser boundary. The cockpit still provides no provider or platform
integration, authentication boundary, public deployment, or general-purpose
workflow editing. The graph, API, and local cockpit are a proven durable
vertical, not yet a general-purpose workflow engine or a deployed remote
product.
