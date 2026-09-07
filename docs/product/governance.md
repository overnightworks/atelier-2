# Governance and project status

A node can now say which tool it needs and have it redeemed. A `tools` entry is
a published tool grant the document pins by hash, exactly as an output pins its
schema, so what a node may do is byte-pinned like every other material it names;
the one capability a runtime here redeems is `run-project-verification`. A
client publishes those bytes through `POST /tool-grant-revisions`, the same
form as a schema: exact JSON in, the catalog's own write, hash out, refused by
the grant owner's own name before anything is stored. When
such a node runs, the command the project's own manifest declares under
`[tool.atelier2.verification]` is run in that attempt's own leased directory --
the project decides what verifies it, never the agent and never the atelier.
A command that exits zero leaves durable proof of exactly which command ran,
how it ended and the hash of what it wrote. That proof belongs to the attempt
that redeemed the grant, not to the attempt's success: it is kept whenever the
check passed, including where the attempt then failed for a reason of its own --
an answer the schema refused, a declared refusal, or work that could not be kept
-- so a run that verified clean is never indistinguishable from one whose check
was never satisfied. Only a passing check is ever recorded: the stored exit code
is fixed at zero, so a redemption cannot say a command failed. A command that exits nonzero ends the
attempt `FAILED` under `PROJECT_VERIFICATION_FAILED`, names how it ended on the
`failed` `node-receipt/v3`, and writes no agent receipt, no `AGENT_COMPLETED`,
and no `tool_redemptions` row -- there is nothing it redeemed. That receipt also
names what the check said no *to*: the schema revision and value hash of the
answer the provider gave, and the address of a bounded, redacted artifact
holding the attempt's own patch against the pinned tree. The work itself is
still not kept as a candidate; only the evidence is.
A grant is redeemed at all only where the attempt changed something: a lease
still holding exactly the pinned tree ends `FAILED` under `CANDIDATE_UNCHANGED`
before any command starts, with the pinned tree and the agent's own bounded
answer in the receipt. Only an attempt of a node that pinned a grant is asked
that question -- a node that redeems nothing may answer without touching a
file. A granted verification that exceeds its
declared `timeout_seconds` after the claim ends the same way: the attempt is
`FAILED` under `PROJECT_VERIFICATION_FAILED`, the `failed` `node-receipt/v3`
reason names the timeout, and the attempt is not left `LAUNCH_ARMED`. The
manifest that is read is the one the pinned commit carries, and the
directory it runs in is that same lease after the provider has worked there, so
what a project declared stays the pin's and where it was run is the mutated
lease rather than a rematerialized pin tree or a living checkout. Refusals are
named rather than worked around: a grant
naming a capability nothing here performs, or bytes that are no grant at all,
refuses the run at the reference that pinned it; a node pinning more grants than
one attempt redeems is refused by that count; a project stating no verification
at the pinned commit refuses the attempt in the words of the manifest that should
have stated it; and a root that is no repository of its own is refused before the
server exists -- each before any provider process starts. What this does not
claim is isolation: the leased directory is still honestly "not a sandbox", the
verification runs as the served process's own user, and enforcement at a boundary
that cannot be talked out of is not built. Neither is the static capability
attestation of a build -- declared, resolved, redeemed and proven is the whole of
the claim.

A node's `budget` is content now, not a word. A `budget_policy` revision is
published through `POST /budget-revisions` and carries exactly four bounds: the
hard `attempt_deadline_seconds` every budget states, an optional hard
`maximum_assistant_turns`, and the two `reported_*_token_threshold` values a
provider can only report after the work it measures. The names carry that
difference, so no surface can offer a post-hoc number as a maximum, a cap or a
ceiling. Every present value is a positive signed 64-bit integer, an absent
optional is not zero, and money is absent by decision: an authentication mode
selects a credential path and measures no charge. Bytes that bound nothing are
refused by their own name -- an unknown field such as a cost ceiling or a run
budget, an explicit null, a zero, a fraction, a boolean, a value past signed
int64, prose -- at the publication door and again at the reference that pins
them, so no run starts under a budget nobody could read. A budget revision is
identified twice, on purpose: the registry and the node pin the exact bytes,
while the four bounds have their own `budget-revision/v1` content identity, which
catalog lineage, display name and revision position never enter. A document that
writes `budget:` is executable: the start resolves that pin the same way it
resolves a schema or a tool grant, and the attempt reads the bound from those
published bytes. The hard turn bound now reaches both workspace-tool executors:
a node that pins a budget naming `maximum_assistant_turns` launches with that
value as `--max-turns`; a node that pins no budget, or a budget that names no
turn bound, keeps the executor's existing default. What this still does not
claim: the deadline does not run a clock, the reported token thresholds judge no
usage report, a tool-free attempt does not read the bound, and the executor-side
declaration of which dimensions a revision requires and what ceiling it attests
is not built.
The first fully budgeted V3 Agent attempt -- deadline clock, reported-token
thresholds, executor-attested refusal, usage and receipt binding -- belongs to
#455 after the durable Runner work in #15 and #301.

Whoever recomputes a finished run's terminal hash now also proves under which
binding it ran. The agent receipt already folded provider, auth mode, auth
profile revision, model, executor revision, configuration revision and request
hash into one value; that value is a named position in the `AGENT_COMPLETED`
event's own preimage, so the fold from receipt fields through event hashes to the
terminal hash misses under any other binding. Older events are untouched: the
`node-event-hash/v3` domain is chosen by content, so a completion that carries no
receipt binding keeps the hash it always had, and an event written before this
version carries no binding rather than an invented one. What is still not proven
is the request hash's own preimage: the job bytes it is taken over have no
durable home, so a verifier copies that hash rather than recomputing it.

The host keeps one live-versioned configuration channel. It is durable,
append-only-versioned in the `auth_profile_revisions` form, and readable at
runtime. The first entry is `project id → root path`. Provider-scoped model
registries append immutable revisions of exact model id,
agent-configuration revision, server-derived discovery or operator provenance,
and the provider check. The registry PUT accepts only the exact id and
configuration hash. A pinned CLI list marks discovered ids checked; a provider
without a list leaves an operator id not checked until the validation door runs
the composed executor's bounded dry run and appends the checked or
unknown-at-provider result as another registry revision. Only checked entries
may be referenced by defaults or role resolution.
Project-scoped model defaults append the operator's Difficulty 1/2/3
selections as exact registry tuples. Today's store is the
first project: that configuration entry, and the reads that treat it as a
project. CLI flags remain bootstrap of where the channel lives --
`--database` is the store, and `--project-id` with `--project-root` may write
the first mapping -- they are not a second copy of the map. After the mapping
exists, compose and the run path that needs a project root read it from the
channel for that project id, not from a second `--project-root` flag. A bad
project id -- including text that is not exact UTF-8 Unicode scalar text -- is
refused `project-unknown` before hashing or configuration. `GET
/atelier/api/v1/projects` answers zero or the one project this process opened,
and its delivered `project1.` reference addresses the identical detail
resource. That resource exposes neither the internal id nor the root path. A
different well-formed reference is `project-unknown`; a malformed reference is
`invalid-public-project-reference`. A configured id with no root is not an
empty collection, and unreadable or corrupt configuration stays visibly
unavailable or corrupt. There is no HTTP write to a project's identity or root,
second project, pagination, project editor, or store-per-project process. The
bounded configuration writes are `PUT /atelier/api/v1/model-registries/{provider_id}`
for a provider registry, `POST
/atelier/api/v1/model-registries/{provider_id}/validations` for the server-owned
first-use check, and `PUT
/atelier/api/v1/projects/{public_project_reference}/model-defaults` for one
project's defaults; the latter cannot create or alter a project. Every project
defaults and resolution operation first proves that the reference names the
project this process serves; a foreign configured project never reaches the
model-configuration channel.

The channel also holds project-source connection revisions
([ADR 0010](../decisions/0010-github-platform-adapter.md) decision 2). Each
revision carries a stable source id, `CONNECTED` or `DISCONNECTED` lifecycle,
the connection instant, source kind, the adapter-owned opaque address, and a
nullable adapter-owned source-ref detail plus a credential-directory reference;
neither detail is connection identity, and the revision never carries the
credential value. Schema V45 rebuilds this family in one transaction under its
immutability triggers. It preserves every credential-directory string, assigns
one deterministic source id per `(project_id, source_kind)` history, sets
`connected_at` to null, and relocates a GitHub address's embedded branch into
the private `source_ref` detail while leaving other adapters' opaque addresses
unchanged. For each project, only the row with the unique project-wide maximum
revision remains `CONNECTED`; every earlier row is `DISCONNECTED`, meaning it
is preserved history rather than evidence that a DELETE was witnessed. Tied
project-wide maxima refuse before mutation. Replaying the completed hop yields
the same rows.

`atelier2 connect` remains the compatible offline operator door and cannot
retarget an active source. The bounded HTTP collection adds `GET` and `POST
/atelier/api/v1/projects/{public_project_reference}/sources`; `DELETE
/atelier/api/v1/projects/{public_project_reference}/sources/{public_source_reference}`
is idempotent disconnect, and `PUT` on that member's `/token` suffix validates
and rotates the stored credential reference without changing source identity or
connection instant. Connecting the same provider address keeps one source id;
a different address cannot take over while that source is active. Connect and
rotation reconcile the durable write before removing a credential deposit: a
validation refusal leaves the previous revision and credential reference
unchanged, and an unreadable write outcome retains any deposit a durable row
may name. Public problems give a fixed next step; corrupt state stops mutation
for inspection. A token entered at either HTTP door is never returned in a
response or error and never enters a log or event. Reads expose only the public
source reference, kind, provider-owned `owner/name` address, `issues` scope,
nullable legacy connection instant, revision and auth method. The singular `GET
/atelier/api/v1/projects/{public_project_reference}/source-connection` remains
as a residual until its frontend reader moves, but reads only the active
`CONNECTED` revision: after disconnect it answers not-connected. No public
projection carries the source ref. This first door deliberately allows zero or
one active source per project; multi-source queue identity stays with the later
queue phase.

Serve composes from the record: a served project whose latest connected
revision names a GitHub source gets the live `open-pr` adapter, and the github
adapter package alone composes the branchless `owner/name` address and
source-ref detail into its repository facts. V45 refuses a GitHub identity that
still embeds a branch; the V44 migration relocates that detail before the row
can be read. The source-ref detail returns through no public host surface (ADR
0010 decision 1). The temporary `--github-*` serve flags are gone; argparse
refuses them as unrecognized arguments. All
three live-GitHub guards stand on the record-composed path: a non-loopback bind
refuses to start, admission refuses an agent-authored `open-pr` grant, and a
start refuses while an earlier run still owes an agent `open-pr` redemption.

The canonical store is schema V46. A fresh store is created as exact V46 and
carries published revisions of the closed kind set, lineage membership bound
to those revisions, append-only alias and retirement histories, format-3
runs, immutable node artifact bytes, node receipts, their ordered output
bindings, and the immutable declared context packages, node-execution request
preimages and run configuration snapshots those receipts name, and the immutable
orders a run was started with, the immutable proof of every redeemed tool
grant, the receipt hash an agent completion binds, immutable content-addressed
artifacts an order may name instead of carrying their bytes, the round a
declared loop was turning when each run, event and agent receipt was written,
the exact node execution and immutable attribution fenced into every Wait
answer -- V46 records the closed actor expected by each WAITING_INPUT head,
records `operator` on new answers, and names migrated predecessor answers
`legacy-unattributed` without inventing an actor; the public current execution
and its node rail are derived together from one run projection --
the host configuration channel's project-root, project-source, provider model
registry, and project model-default revisions, and the queue projection's
admission row per work item. V40 retired lineage occupancy instead of carrying
both authorities. The catalog adapter founds a lineage
and admits members through a typed writer that derives `CatalogLineageId`
from kind and founding hash and refuses a mismatched id before mutation. An
admitted name or lineage id resolves to the exact published bytes; a missing
founding, unpublished member, wrong kind, or retired lineage is refused by
name. Measurements and policy activations are not in this profile. Every schema
from V9 up to the one just below current remains a published predecessor
object -- `schema.py` names each as its own `V*_SCHEMA_HANDOFF` constant -- and
an exact file at V7 through the version just below current is refused by
runtime without mutation, with no runtime migration or downgrade. An offline
`atelier2 migrate` command raises an exact store on any source version
`schema.py`'s `_SCHEMA_MIGRATION_STEPS` ladder still names to the current
schema, one published step at a time. Until a named maturity there is no
compatibility promise.
V28 removes the writerless receipt-Access table and triggers. Its offline V27
hop drops only an empty table and refuses any row without mutation; the
published V3 receipt hash retains its literal empty Access subframe as frozen
byte identity, not as a public input or writable store.
V29 established the queue projection's derived item identity. V44 extends that
owner for Phase D1 ([ADR 0016](../decisions/0016-queue-projection-identity.md)):
an observed item first receives an append-only proposal revision carrying a
typed priority rank, exact workflow lineage, project-local prerequisites,
automation disposition, and optional project-policy revision. The operator
then confirms the exact inspected proposal under the row's CAS revision. The
confirmation records typed `OPERATOR` authority; it does not choose or replace
the proposal's workflow. OBSERVED rows enter through the
operator's issue import: `POST /atelier/api/v1/project-sources/import` on a
served instance whose project-source connection record names a GitHub
repository observes every open issue as one OBSERVED row (reference grammar
`gh:<n>`, owned by the GitHub adapter), idempotent through the derived
identity and insert-or-ignore -- a repeated import adds nothing and never
rewinds a proposal or admission.

A project policy may state the workflow lineage, priority rank, and automation
disposition a labelled item with no proposal is proposed under (V52). The
label sweep then writes that proposal itself, recording `POLICY_DEFAULT` as
its source and the policy revision it came from, before the same admission CAS
runs; a policy without those defaults leaves such an item observed exactly as
before. What a person inspected there is the policy, once -- not the item: a
proposal carrying `POLICY_DEFAULT` was read by nobody before it was written,
which is exactly what that source keeps apart from `OPERATOR`. A policy whose
disposition is `AUTOMATION_AUTHORIZED` therefore makes adding the label the
whole human act that starts an agent run on that item and spends provider
money on it, with no per-item confirmation in between. The disposition is
`HUMAN_REQUIRED` unless the operator states otherwise, so an operator who
states no authorisation gets a proposal to release rather than a started run
(REQ-QUEUE-05).

`GET /atelier/api/v1/queue-items` is the one typed read across OBSERVED,
PROPOSED, and ADMITTED rows. Project policy is written with an expected
revision, proposals through `PUT /atelier/api/v1/queue-proposals`, and
`POST /atelier/api/v1/queue-admissions` remains the confirmation door. The
projection always retains the durable row when tracker enrichment is
unavailable and says so explicitly. A poll loop, a durable cursor
with conditional reads, rate-limit projection, and closed/label semantics are
named deferrals of the import's first slice. Nothing durable holds a tracker
item's title, description, or comments -- REQ-QUEUE-14 keeps those with the
tracker.

V44 also appends project policy revisions, proposal revisions, exact-proposal
dependency edges, and immutable launch bindings. Dependencies are project-local
and only a prerequisite run in `COMPLETED` satisfies one. Capacity inspection
and launch reservation share one immediate transaction: a project with a
published policy revision blocks reservation with `CAP_REACHED` at its
`maximum_active_runs` ceiling, and a project with no published revision has no
cap at all -- launch reservation skips the check rather than treating the
absence as corrupt durable state (operator ruling 28.08.2026). The binding pins
one derived run id and exact workflow revision, so restart and catalog-head
movement cannot launch a second run. The V43→V44 migration preserves every
earlier row, invents none of these decisions, and exposes any old admitted row
without a provable binding as `LEGACY_REVIEW_REQUIRED`.

A run reads one of those items as its own material through the start door: a V3
start order may name a work item (`{"name": ..., "work_item": "gh:<n>"}` on
`POST /atelier/api/v1/runs` and on the MCP `start_run` tool) instead of
carrying bytes, and the start reads that item from the served project's
connected tracker before any durable row exists. What the run stores is the
observed revision of ADR 0010 §5 -- the exact served body bytes, their
SHA-256, the neutral kind (`issue` or `change_request`, so a GitHub pull
request carries no GitHub noun into the core), the read's entity tag and its
read time -- serialized under the house schema `contracts.work_items` owns. A
workflow declares a work item by pinning that schema's published revision and
no other: a graph input pinning a different, in particular a permissive, schema
for a work-item order refuses the start rather than storing a value nothing
checked. The value is pinned, not re-read: the same item started across an edit
is two runs with two different values, and a retry of a run that already exists
is answered from what that run pinned without reaching the tracker at all --
the store is asked before the item is read, and only a start with nothing to
answer from reads. A start that cannot read the item answers which of the four
ways it failed -- no connected project, no such item in the tracker, an
unreachable platform, a payload its adapter refused -- and writes nothing;
those three connection answers are published on the start door's own OpenAPI
operation. Publishing the house schema is still the operator's own act, an item
whose read is larger than the inline order bound is refused by that bound
rather than published as an artifact. The catalog start sheet offers those
observed items as a picker; a run started there carries the observed revision
as the order value.

The queue sweep (`application/advance_queue.py`, `#1145`, #79 slice A2) starts
an admitted item through that same door rather than empty: when the bound
revision declares exactly one `graph_input` pinned to the work-item schema,
the sweep starts with `WorkItemOrderValue` naming the item's own tracker
reference and `bindings=()`, never `None` -- a bare request refuses an order.
A revision with no `graph_inputs` still starts exactly as before. A revision
that declares anything else -- more than one input, or one pinned to a
different schema -- is material the sweep has no way to fill; that one pass
answers `REQUIRED_ORDER_UNAVAILABLE` for the item and moves on to the rest of
the sweep -- the same transient, per-sweep answer an unreachable or
unconnected tracker already gets, not a durable state the item carries
between sweeps. Nothing renders that answer to the operator yet. An admitted
item naming a project other than the one this process serves -- reachable
through `PUT /queue-proposals`, or left behind by a changed served project --
is never this instance's item either: the sweep leaves it untouched and moves
on, rather than treating it as corrupt state. The sweep fires at process
launch (`DbosRuntime.launch()`), then on the runtime's own tick every
`QUEUE_SWEEP_INTERVAL_SECONDS`, and again the moment an admission commits. An
item whose run ended `FAILED` or `CANCELLED` is given back: the sweep writes
that ending onto the binding, carries the item's proposal forward one revision
with its prerequisites, and the next sweep starts it as a differently
identified run, up to `MAXIMUM_QUEUE_LAUNCH_RESTARTS` times (two, because every
restart is paid for). At the cap the item stays bound to its last ending, and a
`COMPLETED` run binds for good. Rendering a refused start at its item is open
#79 work.

On 2026-08-19 at `ed6376b` this landing measured how many concurrent
fake-executor runs one SQLite instance carries. The harness is in-process ASGI on one event loop,
production query-admission bounds, a V3 one-agent document, and
`RecordingAgentExecutorFactoryV2` — not Claude, Grok, or Codex. It carried 96
concurrent runs without a named HTTP or stream refusal. The first observed
pressure was event-write latency: 0.42s at the CI n=2, 12.3s at n=96. The start
door crossed the instance's 1s query-admission wait from n=16 (1.22s) and still
answered 201. The 30s SQLite writer-lock timeout, process-spawn, watchdog
cgroup, and memory failures were not observed. That is a measurement, not a
capacity promise and not a Postgres or #312 decision. The 503 knee is leftover;
[OPERATIONS.md](../OPERATIONS.md) names the operator command that raises n.
