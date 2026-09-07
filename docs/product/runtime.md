# Runtime status

## Current state

One callable product-core vertical executes an immutable YAML workflow revision
through `Agent → Action → Wait → Subworkflow`. The order of YAML entries is not
execution order; configured edges are. Each confirmed node writes one ordered,
hash-bound event and starts only its configured successor. A submitted Wait
answer resumes the same run, and the terminal event binds the completed history
to one terminal hash. Process restarts resume from durable checkpoints without
duplicating the Action effect.

Workflow format V2 adds provider-neutral Agent roles. Before a run starts, every
role is resolved to one immutable, secret-free agent-configuration revision and
authentication-profile revision; the complete matrix is frozen into that run.
That frozen chain currently ends at the `AgentConfigurationRevision`: it binds
model, authentication profile, executor and requested capability, but no Markdown
agent-definition revision or system prompt. A Markdown agent definition itself is
already publishable as an immutable `agent_definition` catalog revision over
`POST /agent-definition-revisions` — exact authored bytes in, hash out, with the
identical definition reconstructable from the stored bytes — but no
configuration references it yet. A format-3 node's executable job today
is its authored instruction composed with the exact named run material it reads.
The target configuration-to-definition link is owned by
[ADR 0007](../decisions/0007-catalog-identity.md), not inferred from matching model
fields.
Each configuration also binds a typed requested execution capability. Migrated
configuration revisions retain their original V1 hash and mean `headless`; new
API publications use the capability-aware V2 hash format and name the capability
they request, `headless`, `headless_with_tools`, or `interactive`. A caller that
omits it publishes `headless`, byte-identically to a publication made before the
field could be sent. Publication binds an executor key, not a capability, so a
configuration may request more than today's executors serve. Every executor
registry entry must attest at least one capability an unattended attempt can ask
for — `headless` or `headless_with_tools` — because the runtime drives every
attempt and stands at no terminal; a run requesting a capability absent from its
exact provider/executor entry is refused before any provider process starts,
whichever direction the mismatch runs. A
nonterminal run is refused on restart before its factory opens when that
attestation has disappeared.
Before invoking the exact configured provider/executor, the runtime persists one
ordinal-1 attempt and binds an in-memory invocation to a separately supervised
process generation. Only the live caller whose compare-and-set reaches
`LAUNCH_ARMED` may authorize that generation to launch; the exact cgroup is the
restart-visible cleanup witness, while PIDs, commands, environment, and provider
handles are never durable state. The API can durably request cancellation of the
current exact attempt before signalling it. Its workflow sends `TERM`, waits one
finite grace, escalates to `KILL`, reaps the process, and records the cleanup
disposition. Recovery continues that cleanup from the cgroup witness without
replaying the invocation, and reads an absent witness as the cleanup it attests
rather than as an answer it must wait for -- a host that restarts its serving
unit takes the whole cgroup subtree with it. A serve start also stops every
attempt no live workflow is driving any more: a workflow that ended without
moving the attempt it drove is not pending, so nothing replays it, and the
restart puts each such attempt under one durable `atelier2-driver-lost` command
and lets that same cleanup path end it as `INTERRUPTED`. The inventory is read
lazily in bounded attempt-identity keyset pages; scan progress is not durable,
so a restart begins again from current durable truth. An attempt whose driver
is merely waiting to be recovered is left alone. One explicit replacement
creates a distinct ordinal-2
attempt and workflow only after cleanup; ordinal 3 and automatic provider retry
do not exist. A known reaped unsuccessful child becomes `FAILED`; a success
records the non-secret operational identity and the exact output bytes in one
atomic attempt/receipt/event/run transition. Which bytes may become that success
is the declaring node's decision: a V2 node bounds them and nothing else, while a
V3 node's answer is read against the schema its own output pins before any of
that transition is written.
When a declared output schema refuses an agent's answer, the engine gives that
same agent one durable repair round carrying the refusal reason; a second refusal
ends the run under `OUTPUT_SCHEMA_REFUSED`.
Every locally dispatched execution carries one immutable permission policy
revision, bound by the composition root and handed down as a dispatch parameter
rather than folded into the request an attempt is identified by; it grants
nothing today, and no provider channel this runtime opens can ask yet, so a
question would be refused under that revision's hash
([ADR 0020](../decisions/0020-provider-boundary.md) §3). Every answer -- refusal
as much as grant -- becomes an append-only `permission_receipts` row bound to the
attempt, the question's correlation id and that revision hash before the
provider is told it, and an answer that cannot be kept is never given.

Schema V27 stages the Core half of an external Runner handoff: one attempt can
bind one manifest and Runner generation, arm one invocation, and accept one of
six typed terminal evidence variants under a semantic hash, with idempotent
retries and colliding evidence refused. That schema, and the `LOCAL_PROCESS`
carrier every live attempt actually runs under, remain; the application layer
that would have driven a Runner-lease Attempt over it -- the `RUNNER_LEASE`
carrier and its dispatch, the host launcher, the disposable #301-A witness
candidate, and the packaged `--runner-*` flags and `ATELIER2_RUNNER_*`
container variables that reached them -- was deleted 2026-09-05 (issue #1252)
for having no live caller in 485 live attempts, and lives on in Git history
for whoever names a caller next.

Every attempt is started in a scratch working directory of its own. The operator
declares one provider-neutral scratch root, and the runtime leases from it a
directory named after the exact attempt identity, so an attempt and its
deliberate replacement never share one. The root is bound to a held directory
descriptor and refused before any provider starts when it is shared, belongs to
another user, is reached through a symbolic link, lies inside a git worktree, or
holds anything that is not an attempt workspace; an attempt whose directory
already exists is refused with that directory untouched, and no provider runs.
A provider may write whatever it likes inside its own directory: the measured
Claude CLI materializes a set of empty configuration and lock files there even
tool-free, which is why a provider is never handed the operator's own checkout.
What it is handed instead, where a project is declared, is that project's source
at one commit: the source is resolved to a commit when the node's durable binding
is composed and never again, and the tree that commit names is unpacked into the
leased directory before the provider starts. The tree travels without its
repository, so nothing in the lease can commit, fetch or push, and an attempt
whose pinned commit the source can no longer answer for is refused in that
source's own words before the attempt is claimed. The directory is removed once
the process and its descendants are proven gone and the attempt is durably
terminal -- after attested cleanup for a cancelled one -- and a restart removes
what terminal attempts left behind while preserving every nonterminal one.
Removal never follows a symbolic link out and never touches the root itself. This
is a directory holding pinned material, not an operating-system sandbox: the
process still runs as the serving user and can name other paths.

What an attempt made now outlives the directory it was made in. Its finished
work is captured into the project's own candidate store
(`.atelier2-candidates.git`, ADR 0011 decision 2) after any granted check has
run and before the attempt is completed, anchored under the attempt that made
it. The two writes share no transaction -- one is a git ref, the other a durable
row -- so the order carries the promise: no attempt is ever read as succeeded
without its work kept, and a capture that fails ends the attempt `FAILED` under
`CANDIDATE_CAPTURE_FAILED` rather than letting a run claim work it cannot show.
A crash between the two leaves the work readable beside an attempt that reports
it possibly ran. Nothing deletes a candidate yet: the store grows until the
project root is destroyed.

The same store answers one question earlier, before any granted check runs:
which tree does this lease hold now. It is asked only of an attempt that is
about to redeem a grant, because that is the only attempt for which changing
nothing is a failure rather than an honest answer. A tree equal to the pin means
the attempt changed nothing, and it ends `FAILED` under `CANDIDATE_UNCHANGED` in seconds
rather than paying a project's whole verification to learn that the pinned tree
still passes -- with the pinned tree and the agent's own bounded, redacted answer
in the receipt, because an answer describing work that is not there is the fact
worth keeping. That reading anchors nothing: naming a tree is not keeping one.
The same reading is taken again after a passing check, because a verification
command runs in that same workspace and what it writes belongs to the candidate;
that second reading, and the patch derived from it, both happen before anything
is anchored, so a store that cannot answer either of them leaves the attempt
under `CANDIDATE_CAPTURE_FAILED` with nothing kept.

An Agent whose exact grant pins `push-atelier-commit` now publishes that kept
candidate before the following open-PR Action. From the run's single issue order,
pinned project source and operation identities, the runtime derives one
`atelier2/work-item/<id>` branch and one deterministic commit over the candidate
tree, persists the operation with the intent and receipt, and admits success only
when the configured remote reads back that exact commit; the PR uses the same
branch as its head. A destination that answers and holds nothing, asked before
anything was sent, has said so: a remote read that succeeds and advertises no
such ref, and a pull request listing for that exact head branch answered with
an empty list, are absences the send proceeds on, so a first publication no
longer waits for a person to confirm what nobody sent. Read after a send, the
same silence says nothing -- a ref someone removed and a listing that has not
caught up look exactly like a request that never left -- so it keeps waiting
for a person, and the runtime, not the adapter, is what knows which of the two
a read is. What still enters or remains in durable reconciliation instead of
permitting another push is a read that failed, a listing naming only pull
requests that are not this request's, and any post-send readback that did not
find the exact thing that was sent. The adapter
now reports why -- the code the destination answered, how long it took, and the
last of its own words with every credential shape taken out. The effect store
logs that account, structured, the moment the outcome enters reconciliation;
carrying it into the durable record an operator reads without the log is a
named follow-up (#1210). An operator-authorized send is still fenced by a
create-only lease. The live-GitHub `open-pr` adapter renders that PR's own
title and body from the candidate's summary and changed paths rather than its
raw report, stating the default `Literal acceptance sentence(s): none`
exemption the repository's own acceptance gate reads, never the report's bare
JSON.

An attempt now leaves behind what it did, not only what it answered. The
executor decodes its provider's own structured stream into one neutral shape --
a tool call with its arguments, the result that came back, an assistant turn,
and what the call spent -- and every step is scanned for credential shapes and
cut to a readable width before anything durable sees it, so a token a tool
happened to print is replaced and marked rather than dropped or kept whole. A
line this vocabulary cannot read, including whatever a call that produced no
stream printed instead, is kept as the provider's own output under its own
name; a transcript with more steps than one artifact may hold loses its oldest
and says how many. The whole is one content-addressed artifact, and the attempt
names its address. Every ending keeps one — a success, a refusal, a nonzero
exit, and a granted check that never answered — because an exit code beside an
empty standard error was the entire account of a real failed run, which is no
account at all, and because an absence meaning two different things could not be
read at all. An attempt whose executor publishes no structured stream names
nothing, which is the honest absence rather than an invented shape.

Three guards make the address mean what it says: it must resolve to bytes this
store holds, it may stand only on an ended attempt, and once written it can
never be moved or cleared. Redaction is not a door a caller may walk past
either — building a transcript at all is what makes its steps safe. The Runner
that would have carried a bounded terminal-evidence record here was deleted
2026-09-05 (issue #1252) and lives on in Git history. Node detail serves the
decoded, redacted events of a stored transcript; the node panel's Log tab is
still not built.

The first real provider now sits behind that durable contract. When the operator
declares a Claude executable and a credential directory, the host composes one
Claude subscription executor. It decides no working directory. It runs the bound
model headless through the CLI's print stream, whose last line is the same
answer envelope its predecessor read alone and whose earlier lines are the
attempt's steps; that changed wire shape is a changed operation, so each of the
three Claude vectors carries its own new operational identity rather than
widening the one it had. It hands the job to the
process over standard input rather than its command line, and grants it one
private, disposable per-invocation `CLAUDE_CONFIG_DIR` plus the serving
host's executable search path; nothing else of the server's environment is
inherited. That directory holds a copy of exactly the declared credential
directory's `.credentials.json` and a written onboarding stub, and is removed
on every lifecycle path -- release, refusal, or executor shutdown -- without
touching another invocation. The declared credential directory is read as a
source at composition and per invocation; it is never itself the directory a
process runs against, so no launched process shares it with another. A
configuration binding a non-subscription profile to this executor is refused
before any process is prepared, and V1 admits only a personal `max` or `pro`
credential, refusing a Team, Enterprise, absent or unreadable one, because an
account outside that set can be handed administrator-managed settings when the
CLI starts. No output schema a node declared reaches Anthropic on any of the
three Claude vectors: the API refuses a schema whose root is an `allOf`,
`anyOf` or `oneOf` as a tool's input schema, and the review contract of
`issue-to-pr` is exactly such a document, so every Claude reviewer died on an
HTTP 400 before a model started. The declared schema closes the job instead —
its exact published document bytes, the same ones the output seam judges the
answer against — and the answer is the terminal `result` text the CLI names
itself. Which bytes of that text are the declared value is the schema house's
own question, not each adapter's: the whole answer, or the one JSON document it
carries, each judged by the same rule that judges any value, because one brief
really came back bare, fenced and introduced by a sentence. Narrowing never
repairs, so an answer carrying no value the schema admits travels on unchanged
and the seam refuses it as `OUTPUT_SCHEMA_REFUSED`. It attests exactly one
execution capability,
`headless`, because a print call is the only shape it can serve, so a node
demanding an interactive one is refused before that run exists at all. An
unsuccessful exit, an unreadable envelope, an envelope declaring a provider
error, and an answer larger than the durable output bound all fail the attempt
instead of recording invented output — each of them keeping the transcript of
what the call did write, so a refusal names more than its own code. Undeclared, the host composes no V2
provider factory and behaves exactly as before. When the
operator also declares a Grok executable, workspace, and credential directory,
the host composes one Grok subscription executor beside Claude. It runs the
bound model headless through `grok --output-format json`, decodes every
concatenated JSON value the CLI writes, and takes the last one's answer to the
output seam. Measured on grok 1.0.5, that format writes exactly one value even
across several model calls, so a tool-free attempt that answered carries no
transcript at all: a value standing before that envelope is a shape this reader
keeps as evidence of an ending rather than reads as progress. It names an
ending with no final message, rather than recording progress as an answer.
Every output schema the node declared travels as `--json-schema` on this
tool-free vector, in the same published document bytes the seam later judges;
the seam remains the last instance if the provider ignores the flag. A schema
whose root is a bare `type: string` is no exception — the CLI accepts that
form, and what it refuses is a schema that is no JSON object at all, such as
`true` — and this seam rewrites neither. The job travels inline through `-p`;
one larger than the measured 30,000-byte transport bound is refused before a
process exists. The same vector pins a turn ceiling so a Diff-Review-sized
order cannot run an unbounded loop, and the child inherits only the serving
host's search path plus one disposable invocation-private `HOME`/`GROK_HOME`.
That home receives a private copy of the source `auth.json`; provider sessions
and responses stay there and the entire home is removed after success, known
failure, cancellation, retry refusal, or process error without touching
another invocation. Before launch, `grok inspect`
must report that exact home/configuration as its only configuration source,
all external-compatibility imports disabled, and no ambient trust surface.

An agent node may also use tools itself through a second Grok executor of the
same deployment rather than a widening of the first. It is armed separately
by `--grok-workspace-tools`, because it is a separate grant: naming a Grok
executable still serves only the tool-free call. Grok splits the two
switches Claude combines: `--tools` names the built-in IDs the model may see
(`read_file`, `list_dir`, `grep`, `search_replace`, `run_terminal_cmd` — the
Headless-documented shell ID; parse does not check names), and `--allow`
names the five permission classes it may run without asking (`Read`, `Edit`,
`Write`, `Grep`, `Bash`) under `--permission-mode bypassPermissions`, which is
what a headless caller that can answer no confirmation needs; `--deny` restores
the protected-edit floors that mode would otherwise approve and keeps MCP
meta-tools from remaining visible. Every other containment switch and the
private `HOME` of the tool-free call stay. Unlike that call, this one asks for
`--output-format streaming-messages-json`, so it publishes whole assistant and
tool messages as they happen: the attempt keeps the turns, the thinking, the
doors it opened and what the call spent as named steps of its transcript, and
the answer is read from the terminal line the CLI names itself. The stream's
opening session header has no step of its own: it stays the provider's own
output, reduced to the session, the model, the doors granted and the regime
they were granted under, because a named step for it is a transcript-vocabulary
change the shared stream reader owns (`#892`). It carries no `--json-schema` —
the pinned CLI applies a declared output schema to every assistant message and
ends the session at the first message without a tool call, so a model that
narrates before acting would end its attempt on that narration. The declared
schema closes the job in words instead, as the exact published document bytes
the output seam judges the answer against, and an answer that is no such
document is refused there rather than accepted here. A session whose stream
shows no tool call at all is still refused as a provider failure instead of
published, so the node ends FAILED and only an explicit replacement runs it
again. It attests exactly one capability, `headless_with_tools`, and no other;
a node reaches it only where its own durable binding asked for that capability,
and a binding that asks the tool-free executor for tools, or this one for a
tool-free call, is refused before the run exists. Because a version answer is
not startability, and because a Clap refusal without an isolated home can exit
0, the deployment starts this exact argument vector once at composition with no
credentials and an empty inline prompt in a private `HOME`, and reads that
the CLI did not refuse an argument, with an unknown flag beside it as the
control; the marker is `unexpected argument`. Neither call reaches a model.
The executor claims no operating-system isolation, does not use
`--always-approve`, and does not pretend parse-time ID validation: the process
runs as the serving user and its tools reach what that user reaches. What a
real answer may reach with those tools is still the half a billed secret-file
probe has to establish under the operator's gate, which is why nothing composes
this executor unless an operator armed it by name.

Codex sits behind the same boundary, declared the same way and composed
alongside Claude rather than instead of it. Its CLI has no prompt-file flag, so
the node's job travels on standard input and never on the command line, and its
durable answer is read from the last-message file the CLI writes rather than
from an event stream this contract has not measured — an answer that never
arrives is a failed attempt, not an invented one. What differs is that a Codex
binding also carries the sandbox policy its attempts run under. That policy is
part of the prepared command, and the deployment proves at startup that the
CLI's own sandbox can actually start on this host, because a sandbox that
cannot start contains nothing and discovering that at the first run costs a
run. The same startup proves the composed profile resolves the credential
directory it was given, loads no user configuration, and configures no MCP
server, so the operator's own Codex trust — including the per-project trust its
configuration records — is never inherited by a served agent. For Claude,
Grok, and Codex alike, pin and attest still run. A failed deployment remains a
declared registry entry without a factory, so Serve stays up and the current
configuration list answers the closed startability pair without a host
diagnostic. The Catalog detail's start sheet shows a registry entry that is
unavailable to start as `◇ Unavailable`, retains the resolved choice so the
project setting remains visible, and blocks start until the operator chooses a
startable agent. An
operator may choose an eligible registry configuration as a start override for that run only; the
workflow pin and project model defaults remain unchanged. When an already
bound, unstarted run reaches an unavailable entry, it writes one attempt-less
`agent-executor-binding-unavailable` terminal refusal before a provider process
starts. A missing declaration or capability mismatch remains the loud runtime
binding conflict, and every attempt that may have crossed its launch fence
remains #15 recovery work.

The raw frame a provider writes has its own bound, distinct from the durable
output bound, because the durable answer travels inside a JSON envelope. The
executor declares that bound on the invocation it prepares, since the frame is a
property of its own wire format, and supervision holds the process to exactly
that declaration: a frame past it is terminated and refused before the executor
ever decodes it, so no answer is recorded. It is sized so no answer the durable
contract accepts can be refused as a frame, and where the wire format carries
the attempt's steps ahead of that answer it is sized for a whole transcript's
worth of them too. Each provider states its own number; the port's ceiling is
deliberately nobody's, so one operation's measurement never becomes another's
allowance. The job those processes receive —
stdin for Claude and Codex, a job file for Grok — is held to the process-input
bound, a separate decision from the durable answer bound. After a chain the job
can carry the instruction, the run's orders, and earlier results; a composition
past that bound is refused by name.

An executor whose provider speaks rather than prints opens a conversation for
its invocation after the workspace is leased and before anything starts. The
same supervised child then keeps its standard input open: supervision hands
every output frame to that conversation, puts each permission question to the
authority the dispatch bound and each file request to the access its binding
names, writes the answers back as standard input, and holds every buffer to the
bounds the executor declared. The conversation itself reaches nothing — it
reads bytes and asks — and nothing of it reaches the watchdog, which keeps
servicing pipes, deadlines and signals alone. A stop therefore still writes the
conversation's prepared stop frame once and signals in the same breath, and it
carries why it was asked for — an operator, a budget, a policy — because a
signal cannot say that afterwards. A stop is also the conversation's own to
ask for, with the same causes, when it reaches a ceiling nobody outside can
see. The conversation speaks first -- a protocol whose opening frame is the
caller's spells it itself, so no command payload carries a lifecycle it cannot
number -- it is told when its bytes physically reached the child, and it can
declare itself finished, which closes that child's input once the last answer
is written so a server that waits for more leaves on end of file rather than on
a signal. The conversation ends by saying in its own typed terms how the
attempt really finished, beside the bytes and the steps it leaves. An
invocation that opens no conversation is the print-mode call above, unchanged
in every byte.

The call itself is deliberately the barest one its authentication allows: no
tools, no hooks, no MCP servers, no plugins or skills, no project configuration
discovery, no session or prompt history surviving the invocation, no retries,
and a bounded turn count, so the credentials it is handed answer text and do
nothing else.
It also asks the CLI to strip provider credentials from every subprocess
environment, as defence in depth against a child this invocation does not
expect to have; that hardening needs bubblewrap on the search path the launched
process receives, so a deployment without it is refused at startup rather than
at the first run.

Every one of those switches was measured to be there and to really parse against
each exact Claude release the executor admits, and one contained call on each
shows what they leave standing when they act together; what that one call cannot
separate — which single switch stops what, and the controls that make such an
attribution mean anything — stands from the release it was first measured
against. So the deployment reads the declared executable's version before it
composes anything and admits only a release such a conformance run covers — not
merely a new enough one, and not one that merely sits between two covered ones,
because a later CLI can change a control the containment depends on, and
admitting it costs a rerun of that one-call matrix and a decision about this
executor's operational identity. The same startup attests that this host
carries no administrator-managed policy: policy is outside every switch above by
design and can still start a child beside the credential directory. Reading the
declared executable's version is itself the first execution of an external
binary, so it is run with no environment at all, in an empty private directory,
under a byte bound on both streams and a deadline that kills the whole probe
session. That is containment, not isolation — the process still runs as the
serving user — so this executor may only be declared for a loopback bind.
Serving it on a reachable address is refused at startup, because starting a
billed provider is unauthenticated on this API. OS-enforced isolation remains
the planned stronger boundary; until it lands, these refusals are what keep the
tool-free premise true.

An agent node may also use tools itself, through a second Claude executor of the
same deployment rather than a widening of the first. It is armed separately,
because it is a separate grant: naming a Claude executable still serves only the
tool-free call. Its invocation drops the tool ban and names one closed set of the
CLI's built-in tools twice — as the tools it offers and as the tools it
pre-approves, which is the only grant the CLI honours once this deployment asks
it to scrub subprocess environments — and keeps every other switch and the whole
environment of the tool-free call. It attests exactly one capability,
`headless_with_tools`, and no other; a node reaches it only where its own durable
binding asked for that capability, and a binding that asks the tool-free executor
for tools, or this one for a tool-free call, is refused before the run exists.
Because a version answer is not startability, the deployment starts this exact
argument vector once at composition with no job and reads the CLI's own refusal
of a missing argument, with an unknown flag beside it as the control; neither
call reaches a model. The executor claims no operating-system isolation and does
not pretend the lease is one: the process runs as the serving user and its tools
reach what that user reaches, the workspace is where it is started rather than a
boundary it is held inside, and what it writes there falls with the attempt.
Unlike the tool-free call, it has no one-call conformance matrix against a real
subscription answer yet: that a real answer uses exactly these tools and brings
no customization back with them is the half one billed call still has to
establish, which is why nothing composes this executor unless an operator armed
it by name and the packaged deployment does not.
