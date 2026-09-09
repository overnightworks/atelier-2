# ADR 0009: One trust boundary separates the coordinating service from every worker

- Status: PROPOSED 2026-08-15; amended 2026-08-21, 2026-08-22, 2026-08-23, 2026-08-24, 2026-08-25, 2026-08-26, 2026-09-04, 2026-09-05 (see [ADR 0020](0020-provider-boundary.md): the watchdog is the first session implementation; the container-hosted Runner this record describes -- the candidate image, `atelier2-runner-launcher`, and the disposable #301-A harness -- was deleted 2026-09-05, issue #1252, for having no live caller in 485 live attempts, and lives on in Git history for whoever names a caller next), 2026-09-09 (see the §6 amendment below: the provider catalog path inherits this record's credential standard and gives up its visibility half); disposable #301-A candidate 2026-08-22 — no live Runner availability
- Date: 2026-08-15
- Requirement authority: [Issue #1](https://github.com/FlexOr2/atelier-2/issues/1)
- Decision authority: [#21](https://github.com/FlexOr2/atelier-2/issues/21) owns
  the item; this record owns the decision. A live issue body is not a
  freezable byte object — the prior byte-pinned candidate drifted stale a
  third time — so this record no longer pins #21's body by digest; the prior
  rebind and derived-document debt are recorded in
  [#21 comment 5354779824](https://github.com/FlexOr2/atelier-2/issues/21#issuecomment-5354779824).
  The operator-owned architecture ruling is
  [#5 comment 5354196886](https://github.com/FlexOr2/atelier-2/issues/5#issuecomment-5354196886);
  the canonical terminology and owner map are
  [#9 comment 5354522420](https://github.com/FlexOr2/atelier-2/issues/9#issuecomment-5354522420)
  and the amended [#9 body](https://github.com/FlexOr2/atelier-2/issues/9), whose
  rebind is recorded in
  [comment 5354786342](https://github.com/FlexOr2/atelier-2/issues/9#issuecomment-5354786342).
  This record owns #21's trust mandate and **does not close #21**: the local
  carrier decision is bounded below; remote/CI remains its separate stop-gate.
- Depends on: [ADR 0001](0001-durable-runtime.md) (process ownership, attempt
  states), [ADR 0003](0003-http-api.md) (the control surface this record
  authenticates), [ADR 0004](0004-local-cockpit.md) (the local-only boundary this
  record replaces), [ADR 0006](0006-node-vocabulary.md) (attestation vocabulary,
  reused and never duplicated), [ADR 0008](0008-budget-units.md) (the attempt
  deadline this record reuses as a lifetime bound)
- Feeds: [#7](https://github.com/FlexOr2/atelier-2/issues/7) (actor attribution),
  [#9](https://github.com/FlexOr2/atelier-2/issues/9) (operator-facing epic;
  remote execution, Effect Worker and Remote Attach remain separate deliveries),
  [#60](https://github.com/FlexOr2/atelier-2/issues/60) (sandbox probe as
  attested state)
- Names, never decides, the dependencies owned elsewhere:
  [#16](https://github.com/FlexOr2/atelier-2/issues/16) (durable failure
  vocabulary), [#23](https://github.com/FlexOr2/atelier-2/issues/23)
  (multi-project isolation), [#58](https://github.com/FlexOr2/atelier-2/issues/58)
  (workspace lease), [#15](https://github.com/FlexOr2/atelier-2/issues/15)
  (attempt state, fencing, terminal-evidence acceptance and reconciliation),
  [#301](https://github.com/FlexOr2/atelier-2/issues/301) (the deployable Atelier
  Runner, executor adapters and containment), and
  [#312](https://github.com/FlexOr2/atelier-2/issues/312) (separate Serve/Runner
  artifacts, deployment, migration and cutover)

## Context

Issue #1 wants the cockpit reachable at `hallucinai.de/atelier`. Today the API
has no operator identity: any client that reaches the port may publish, start,
answer, cancel, and reconcile, and the `actor` field on a reconciliation command
is a string the caller asserts about itself. The only control standing between a
stranger and the operator's billed subscription is `atelier2.host`, which refuses
composition when a Claude subscription executor meets a bind that is not a
literal loopback address, with the reason written into the refusal: the billed
boundary stays on this machine until an authenticated boundary exists.

That refusal is correct and it is a placeholder. Nothing owns what happens when
execution moves off this machine — who a runner is, how it proves that, what it
may do with an attempt, and how the terminal channel that carries human
keystrokes into a credential-bearing process is gated. #9's remote surfaces are
blocked on this record, #7 needs an actor before commands can be attributed, and two
vision-panel lenses marked the gap CRITICAL with no owner.

The operator directive of 2026-08-15 (#1) sharpens the shape: agents are
default-capable and restricted by their definition, and fail-closed staging
stays exactly where the trust concerns are — credentials, billing, sandbox. This
record is that boundary, so strictness here follows the directive rather than
contradicting it.

The 2026-08-20 ruling resolved a later contradiction in the planned local
carrier. The live watchdog/exec-guard path and the subsequently planned direct-
systemd manager are predecessors, and a one-container Serve-plus-execution
deployment is rejected rather than selectable. Their landed history remains in
the owning issues; this record retains only the deletion fact needed to prevent
any of them from returning as a fallback.

**2026-09-04 amendment ([ADR 0020](0020-provider-boundary.md), operator
ruling):** the watchdog half of that sentence no longer holds. A live-usage
audit found that the Agent Runner executed none of the 485 live attempts of the
preceding thirty days, so the watchdog is not a predecessor waiting for deletion
but the first implementation of ADR 0020's `AgentSession` port, and this
record's Runner is frozen inventory until a caller — isolation for foreign
repositories, or more than one user — pulls it into life. The direct-systemd
manager and the one-container deployment remain rejected as written. Everything
else this record decides, including the trust boundary the port later moves
behind, stands.

**2026-08-26 amendment: why this record stopped pinning #21's body by
digest.** An earlier revision bound `#21 body @ 3c1f663c…` — 7,961 UTF-8
bytes, ending in one LF byte — and it drifted stale a third time as the item
kept moving. The canonical digest rule is exact — the bytes the API serves as
the issue body, hashed as they are, with nothing appended, re-encoded or
normalized — and this record's first revision even got that wrong once, when
a shell pipeline (`gh ... --jq .body`) appended a newline before hashing and
bound bytes GitHub never served. A live issue body is not a freezable byte
object, so a per-record human-pasted pin will always chase it; a document
that must freeze exact bytes belongs on the requirements registry in
`docs/requirements/README.md`, as REQ documents already do.

## Decision

### 1. Core owns truth; workers own one bounded operation

**Atelier Core / Serve** owns scheduling, ready-set computation, attempt and
generation CAS, canonical artifacts, events, dispositions, receipts, retry,
resume, cancel and reconciliation. It is the only writer of product truth.

The **Atelier Runner** is a deployable Agent worker. One Runner invocation
executes exactly one leased `AgentAttempt`, identified by its attempt, request,
generation/invocation and pinned Runner-manifest identity. It hosts the Agent
Executor Adapters for Claude, Claude-tools, Codex, Grok and Grok-tools, and owns
provider CLI and local credential resolution, ephemeral workspace
materialization and containment, bounded collection and terminal evidence. A
provider process is its child, not a Runner; the existing `AgentSession`
port is a lower, Serve-local predecessor, not this boundary. (2026-09-04
amendment, [ADR 0020](0020-provider-boundary.md): that Serve-local path is where
every live attempt actually runs, and it owns the session port until this
boundary has a caller; the containment rules of this section are what the port
must satisfy when it moves.)

An **Effect Worker** is a separate worker role for one prepared `EffectIntent`.
It hosts the existing Effect Adapter contract under an operation-scoped grant
and returns effect evidence; only Core commits an `EffectReceipt` or
`WAITING_RECONCILIATION`. It never shares the Agent Runner's identity,
credential, environment or privilege lane. Wait, Join, Resume and deterministic
or subworkflow scheduling remain in Core and are not workers.

The disposable candidate's Core-restart leg is deliberately a live Docker
witness, not a deterministic test. Its deterministic crash harness proves the
binding and same-child rules without Docker. In the live leg a pre-opened host
observer receives Core's `STARTED` cut event, lowers the Runner container to its
current cgroup-v2 `pids.max`, records container/PID/start-tick identity, and
only then acknowledges Core so it can write the cut record and exit. The shell
reads that STARTED-bound identity from the cut record and requires the same
single child after Core exits and after reconnect `STARTED`, while the monotonic
`pids.events:max` counter remains unchanged.

Serve and Runner are separate OCI images and release artifacts under #312.
Serve contains no provider CLI or provider credential value and receives no raw
carrier or OCI lifecycle authority. The Runner writes evidence, never product
truth; native carrier logs or artifacts may transport evidence or provenance
but never become the canonical store.

### 2. Every carrier enforces the same identity boundary; the first local form is decided

A **carrier / host** supplies the execution environment in which a worker runs:
local OCI, a remote machine, GitHub Actions or GitLab CI. It is neither an
Atelier Runner adapter nor a second scheduler or store of record. The selected
path is deployment state; work or evidence arriving by another path is refused
(`runner-transport-mismatch`). For remote/CI, who holds launch, cleanup and
lifecycle authority remains the separate open decision below.

Before Core binds work or accepts evidence, it establishes the exact worker
invocation it authorized. The worker authenticates Core in the same act. The
transport must authenticate both peers, authorize each operation for exactly its
attempt/generation and worker role, and make replay and idempotency explicit. A
reused name, path, job label or carrier identity never substitutes for the
per-invocation identity (`runner-peer-unverified`).

For the first local Agent Runner, rootful Docker Engine/Compose is the carrier.
The host launcher alone owns Runner-container launch, stop and cleanup; Core owns
none of that authority. A Runner owns only its provider child, including start,
TERM→KILL, reap and journal. Each Attempt gets one hardened, non-privileged
Runner container and one internal private network. Core may join the needed
Attempt networks; Runner containers from different Attempts may not reach one
another. The exact proof surface is read-only root, `cap-drop=ALL`,
`no-new-privileges`, an unprivileged user, a PID limit, no published port, no
Docker socket and no Docker, project, home, workspace or general host mount. The
host launcher may inject only exact per-invocation identity material read-only.
The read-only harness-code bind mounts in the disposable witness are witness-only
test plumbing, not a production mount form. It proves only the local carrier
boundary, not provider egress, `LAUNCH_ARMED`, cancellation, crash/host-loss or
remote/CI.

Core and Runner mutually authenticate with X.509. The Runner client leaf carries
one exact URI-SAN binding of Attempt, Request, Generation, Invocation and
Runner-manifest identity; both peers check their expected peer, CA, EKU and the
complete binding before an operation. Server and client EKUs differ. This is an
X.509-SAN contract; no identity-framework literal is part of it. Host-managed
keys are mode 0600 and read-only-mounted only into their matching disposable
container; Core has only its own leaf/key and the CA. Issuance, rotation,
revocation and retention remain external operator CA responsibilities, not an
Atelier PKI.

Rootful Docker daemon, host launcher, host and operator CA are the local TCB.
Remote/CI carrier, lifecycle authority and mutual authentication remain **OPEN
on #21**. They stay refused until #15-B proves carrier-neutral crash, cancel and
readback behavior; then #9 owns GitHub first and GitLab parity. Mounting a
Docker/OCI socket, systemd or DBus into Serve, introducing a privileged broker,
or running privileged systemd in a container is not an admissible placeholder.
The first CI proof hosts one Atelier Runner job for one AgentAttempt; it does not
compile the Atelier DAG into native CI jobs. A later Effect Worker remains a
separately authorized job.

**2026-08-23 amendment (Operator-Ruling, #301-Journal): controlled egress.**
The per-Attempt private network gains bounded outbound Internet reach —
outgoing HTTPS and DNS only — so a provider CLI reaches its API in the
invocation form §7's observed-version-pin proof already measures. Inbound
connections into a Runner container stay blocked, and so does Runner-to-Runner
traffic across Attempts; the cross-Attempt unreachability proof stays a
required witness case unchanged. A forward proxy was considered and rejected:
it would mutate the measured Claude environment vector, which carries no
`HTTPS_PROXY` entry, and would reopen an already-measured conformance surface
rather than reuse it. Traffic outside HTTPS/DNS, and any inbound attempt, must
be refused at the network boundary as a loud, immediate connection failure the
provider CLI's own error handling surfaces — never a silent stall the operator
has to diagnose by timeout; B-2's required proof includes demonstrating this
failure shape for whatever concrete mechanism it selects, exactly as the
disposable-witness paragraph above already withholds a provider-egress proof
until then.

**2026-08-22 amendment (Operator-Ruling, #301-Journal): read-only credential
ingress.** The host launcher's per-invocation-identity-only mount rule above
gains one exact second permitted surface: each provider's credential
directory may be bind-mounted into its Runner container **read-only**. This is
the only host surface added beyond that identity material; no other host,
project, home, or workspace path is admitted. Read-write access to the
original credential directory stays forbidden, because a live operator
session may hold that directory open and a Runner-side write risks corrupting
it underneath the operator. A write-capable form — a per-Attempt copy,
destroyed after RELEASE — is not decided here; it waits on a fresh
operator-ruling question, raised only once a measured token-refresh failure
under read-only actually occurs. Until then, a provider CLI whose token
refresh needs to write its credential store fails loud and visibly under the
read-only mount: the run breaks with a named, observable error rather than
hanging silently, and no credential data is lost, because nothing was ever
writable to lose.

**2026-09-09 amendment ([ADR 0020 §8](0020-provider-boundary.md), operator
ruling 2026-09-09, issue #1430): the provider catalog path inherits this
standard and gives up its visibility half.** The read-only ingress above is a
Runner-container surface, and that container is deleted (see the status line);
no catalog probe ever ran beneath it. ADR 0020 §8 therefore sets the same
protection positively as its own: a probe never sees the operator's credential
directory at all, it runs in a private home holding one mode-0400 copy, that
home is removed when the probe ends, and a direct overwrite of the copy fails.
What is given up there is the sentence above it: a refresh that creates a new
file and renames it over the copy succeeds **silently**, against the disposable
copy alone, and is discarded with the private home, so the operator is never
told it happened. The operator ruled that acceptable on 2026-09-09 -- the
protection this rule exists for holds, since no credential of the operator's is
written, lost, or made reachable, while no mechanism offers the visibility
today and its absence destroys nothing. The read-only ingress rule itself
stands unchanged for whatever revives the Runner.

**2026-08-23 amendment (Operator-Ruling B, #540-Journal): a lease is a request,
not an authorization.** The local carrier form is a host launcher process
beside the console. It alone holds engine authority; Serve receives no socket,
no broker, and no privileged helper, and asks for an Attempt by publishing a
Runner lease. That lease is therefore a **trusted input to a privileged
process**: it names host directories the launcher will mount into a container
and the container it will attach to an Attempt network — a lease free to name
anything would be a mount of the host's own filesystem into an Attempt, and
`CAP_NET_ADMIN` inside a namespace nobody chose. The launcher consequently
validates source and target itself, against what the operator declared when it
was started, and never against what the document claims: every path a lease
names must resolve inside the operator's declared attempt root, and the console
container it names must be the one declared at start. This holds now, while an
operator script writes the leases, and it is what makes the seam safe when
Serve becomes the writer — Serve is precisely the component this record refuses
to trust with the carrier, and being able to ask must never become being able
to command.

**2026-08-23 amendment (Operator-Ruling, `#540` C-3.3): the launcher protects
the host, and the console is long-lived.** Three sentences, each carrying a
consequence this record's implementation must show.

*(a) The boundary is the host, not the Attempt.* Serve writes into the attempt
root the launcher mounts into Runner containers, so a compromised Serve chooses
the resource limits and mount targets of **its own** Attempts. No launcher can
prevent that, and Core-side checks are worthless against a compromised Serve
because Serve *is* Core — Core-side checks protect against drift and mistakes,
never against Serve itself. What the launcher's fences do protect is this host:
the manifest a lease names must be the manifest identity Core bound, and every
resource number it carries — memory, process limit, CPU quota, scratch size,
the sum of the tmpfs grants that are host memory, and the journal capacity that
is host disk — must stay inside bounds the operator declared at start. Both
fences are therefore **launcher-side**, applied before the first engine call of
an Attempt, together with the image the lease may start as root over that
Attempt's volumes.

With them, what a *lease* may ask of this host is bounded in every dimension it
names. The journal is the one the engine does not enforce: it has to be a
durable volume so a Runner's own restart finds it, and the local volume driver
gives such a volume no size, so that capacity is kept by the Runner against its
own manifest and bounded here by declaration instead. What stays outside this
sentence is a **Runner image** that ignores its manifest — which is the image
the operator declared, not one a lease chose.

*(b) "Privately created, therefore policy before reachability" no longer holds
for the console, and is replaced positively.* The console is started by the
deployment, on its own base network, long before any Attempt exists; requiring
a container created reachable by nothing would mean it could never be attached
to an Attempt at all. The guarantee is restated as two positive sentences that
are read back out of the engine: an Attempt's own packet-filter chain is
installed **before** `network connect` puts that Attempt's network into the
container, and the attachment attestation says the container is attached to the
declared base network and to exactly one Attempt network — and to nothing else.
A container the launcher itself creates is still created private and still
reaches nothing until its Attempt exists; that is now a property of how Runner
containers are started, not the fence itself. Each Attempt's rules live in a
chain named after that Attempt, reached from a dispatch chain the base policy
installs once, and removed whole at release — so a console carries exactly the
Attempts it currently has, and a second Attempt neither accumulates behind the
first nor is refused.

*(c) Rejected, with reason.* A per-Attempt **published port** instead of
attaching the console: it would put Core on an address every one of the host's
own network neighbours can reach, moving a promise this record makes into the
host's firewall configuration. A **fully private console behind a proxy**: a
stronger promise, but one deployment component more, and the operator chose the
declared base network plus the per-Attempt fence instead. IPv6 gets a blanket
reject in the base policy and no per-Attempt chain, because Attempt networks
are IPv4 and no Attempt ever opens an IPv6 path to widen.

**2026-08-24 amendment (Operator-Ruling A, `#540` Kind #585): the launcher
retains one terminal fact for Serve, one way, as a file.** A Runner journals
its terminal fact and then tries to deliver it to Core; a Serve restarted
mid-session never receives that delivery, and the fact lives only in the
per-Attempt journal volume, which no process but the launcher may read. So the
launcher copies that record — verbatim, the Runner's own canonical bytes — out
of the journal volume into the Attempt's handoff directory, which Serve already
wrote and can read, and Serve converges the Attempt over it on its own restart.
This does not widen the launcher's authority: reading a durable volume it owns
and writing the handoff directory it already writes the attestation into are
both authority the launcher already holds; the copy fires only for a Runner
container that has **exited** and only once its terminal record is **present**,
never before the launcher's single resume nor while the container still runs, so
it can neither copy a fact the Runner has not finished nor race a delivery the
Runner is about to make. The direction is the only new fact, and it is
deliberately narrow: this is a launcher→handoff **file copy**, not a second
reader of the journal volume. Serve still never touches the journal, holds no
carrier authority, and reads only a plain host file — the trust boundary of
sec. 2 is unchanged, gaining one one-way seam the compromised-Serve model
already covers (Serve can distort a file under its own attempt root, which
corrupts only its own Attempt's convergence, exactly as amendment (a) already
says of everything else under that root).

**Identity validity is bounded by what it identifies (`#540` C-3.3).** The
installation's authority stands for about a year, the console's own leaf for
about a quarter and is renewed by re-running the launcher's
`issue-console-identity` command, and a Runner's leaf is minted for the attempt
span the manifest Core selected declares — the same span the Runner's own
session deadline runs on, so an invocation that is over holds a key that opens
nothing. A launcher refuses to serve at all when the console identity it is
pointed at has already expired, because that failure would otherwise appear
inside every Attempt as an unreadable handshake.

**2026-08-26 amendment (Operator-Ruling 24.08., #632): no new self-built
isolation cage.** What is built and works here — rootful Docker hardening,
the per-Attempt packet-filter chains, Landlock on the provider child — stays
unchanged; this amendment orders no rebuild and #632 stays parked rather than
blocking. It records only the doctrine the ruling states for what comes
next: from this date, no new self-built isolation slice (packet-filter,
network or jail mechanics) is begun; a coming Runner slice is measured
through an adopt lens first — does a maintained sandbox runtime already meet
the need? Considered, ascending: gVisor (`runsc`) as the Docker runtime under
the existing carrier, a deployment detail costing little code and the first
candidate to try; Firecracker or Kata self-hosted as a new carrier adapter
fulfilling this same lease/evidence protocol, matching the direction that a
credential never leaves the network; E2B or another cloud sandbox, rejected
for that path because code and credentials would cross a foreign cloud, left
open only as a later option for non-sensitive projects. This item names no
`Done when` this record owns; it waits for real Runner usage to demand
isolation work before the first adopt candidate is tried.

### 3. Operator authentication gates every exposure beyond this machine

The API gains an authenticated **operator principal** before any exposure that
reaches past the machine it runs on. Until that authenticator exists, the
composition refusal in `atelier2.host` generalizes from "a Claude subscription
executor is composed" to the whole API: an exposed deployment with no composed
operator authenticator refuses at composition (`unauthenticated-exposure`), in
the same loud shape as today, never as a warning or a default-open mode.

**Exposure is declared, never inferred from the bind address.** A loopback bind
does not prove local reach: a reverse proxy, an SSH port forward, a container
port publication, or any other fronting layer leaves Atelier bound to `127.0.0.1`
while the world reaches it — and an unauthenticated API behind such a layer is
exactly the hole this section exists to close. So the deployment declares its
exposure explicitly, and the two facts are checked against each other:

- `this-machine`: nothing outside the machine session may reach the API. A
  non-loopback bind contradicts this declaration and refuses at composition.
- `reachable`: something in front of the service may carry remote callers. An
  operator authenticator is **mandatory**, whatever the bind address is.

An undeclared exposure is not a default; composition refuses until the
deployment states one. Where a fronting proxy is the exposure, the operator
declares that trust relationship together with the authentication it terminates —
and forwarded request headers (`X-Forwarded-For`, `Forwarded`, and their kin) are
**never** read as identity and never as evidence about exposure, because a caller
can write them.

Named mechanism per declared exposure, single-user V1 (#1):

- `this-machine`: the operator is whoever holds the machine session; no
  credential, and the declared exposure plus the loopback bind together carry
  the boundary.
- `reachable`: one credential per operator client, verified by the service over
  TLS the deployment terminates. The verifier material is a path the host reads at
  composition — never a value in a workflow, prompt, event, receipt, log, or API
  resource (#1's secret rule). Session lifetime is configured per deployment
  against a named need (a stolen cockpit session must not outlive the operator's
  own session) and is live-versioned configuration under #1, not a constant this
  record invents.

Rejected alternatives, because neither yields an actor for #7: a shared secret
carried in a URL, and an address allowlist used as the whole authentication.

### 4. Long-lived Runners enrol; ephemeral CI jobs use a CI TrustPolicy

Every long-lived remote Runner holds **its own** credential. A shared fleet
secret is refused: it cannot be revoked for one host and names no actor.
Enrolment is an explicit operator act with a durable record binding the Runner
id, carrier tier, credential-verifier reference, enrolling actor and Runner
attestation (§7). An unenrolled Runner receives no attempt binding or attach
ticket (`runner-unknown`); a revoked one likewise (`runner-revoked`). Core and
Runner mutually authenticate in the same handshake.

An ephemeral CI job is not manually enrolled as a long-lived Runner. Instead,
the operator enrols one narrow **CI TrustPolicy**: pinned OIDC issuer, immutable
repository or project identity, exact workflow or configuration identity, and
protected ref or environment. One unique workflow-run/job assertion may be
exchanged only once for a short-lived credential bound to one attempt,
generation and worker role. A differing claim or replay is refused. This policy
admits the job identity; it does not make CI a scheduler, truth owner or
capability author.

Agent Runner and Effect Worker credentials are role-separated and mutually
unusable. A CI Agent proof therefore grants no Effect operation and carries no
ambient repository-mutation credential; a later Effect Worker proof receives
only its prepared intent and operation-scoped grant.

**Revocation marks the enrolment record revoked; it never deletes it.** A
deleted record makes a revoked runner indistinguishable from one that was never
enrolled, which would collapse `runner-revoked` into `runner-unknown` and erase
the fact an incident needs most: that this runner id was trusted, and when it
stopped being. So enrolment state is durable and three-valued — absent, enrolled,
revoked — carrying the revoking actor and the revocation time. Re-enrolling a
revoked runner id is an explicit operator act with a fresh credential and a fresh
attestation (§7), never a silent return to service. Revocation stops new
bindings; it asserts nothing about an attempt already in flight (§10).

### 5. What an Agent Runner may do, and what no worker may do

An Agent Runner **may** accept exactly one lease bound to immutable attempt,
request, generation/invocation, executor and Runner-manifest identities after
Core's compare-and-set to `LAUNCH_ARMED` (ADR 0001; Core arms, never the Runner).
It may launch, supervise, cancel and reap that provider process, resolve the
credential reference locally (§6), and report bounded observations and terminal
evidence. Identical delivery returns the same invocation; a different binding
conflicts.

No worker may do any of the following; Core enforces each prohibition rather
than trusting the worker:

- mint a verdict. Receipts, dispositions, budget judgements, and the durable
  event sequence are written by the core (ADR 0001, 0006, 0008). A report
  carrying one is refused whole (`runner-report-out-of-scope`).
- publish or alter catalog content — workflow, agent-configuration, auth-profile,
  budget, retry, skill, tool, or capability revisions (`runner-not-authorized`).
- execute an attempt it was not bound (`attempt-binding-unknown`), or anything
  under an attempt id that already reached a terminal state
  (`attempt-binding-terminal`).
- read credential material outside its bound auth profile — another profile's,
  another runner's, or another project's, whose isolation #23 owns.
- widen its own capability set. ADR 0006's rule holds unchanged at this boundary:
  capabilities are attested, never claimed.

### 6. Provider credentials reach a runner by reference, never by value

Core transmits the auth-profile revision and a logical credential *reference*;
it transmits neither the value nor a Serve-local host path. The Runner resolves
the reference from its own credential source. The current prepared-path seam is
predecessor implementation owned for deletion by #301, not the target contract.
A Runner that cannot resolve the bound reference refuses before provider start
(`auth-profile-unresolvable`) with no fallback to another auth mode. Atelier is
never a secret-distribution channel.

### 7. A runner is attested, and a binding needs a runner that attests it

This record mints no parallel capability vocabulary, and it does not widen ADR
0006's. That distinction is the whole of this section, because the two kinds of
fact involved have different producers:

- ADR 0006's **runtime capability revision** is immutable and produced by the
  build and adapter layer: "not authored and not editable", no deployment writes
  one and no document grants an entry. What a build can prove belongs here, and
  nothing else may enter.
- A **host-and-deployment fact** — whether this machine's sandbox actually
  confines, which provider binary versions this host has, whether the operator
  enabled attach for this deployment — is produced by a probe on one host or by
  an operator's choice. None of that is a build product, so writing it into
  0006's manifest would make the manifest authorable, which 0006 forbids.

So a runner presents a **typed runner attestation**: a wrapper that *references*
its runtime capability revision by id and never restates its entries, plus the
host-and-deployment facts above, signed by the runner's own credential (§4) and
valid for one host. The capability revision stays exactly what 0006 made it; the
wrapper is where per-host truth lives.

At enrolment and at every connection a runner presents that attestation,
carrying:

- **the referenced runtime capability revision id**, whose `agent_execution`
  entries already carry executor identity, provider mode, build identity, gate
  run and evidence reference — so auth-mode enforcement needs no new capability
  name and no new manifest entry here;
- its **sandbox probe state**: the functional probe #60 defines, executed on that
  host. An executable check is not a sandbox proof, and a probe result from
  another host is not this host's;
- its **observed version pins**: the provider CLI versions actually present on
  this host, checked against the versions its referenced executor revisions
  attest, as ADR 0008's meter revision already pins Claude's measured CLI
  versions. A host whose observed version is outside the attested ones has a
  changed attestation, not a usable capability;
- its **attach channel state**: whether the deployment enabled the terminal
  channel on this runner (§8). This is a wrapper field, not a capability entry —
  a deployment toggle is exactly the authored grant 0006 refuses, and it never
  restates `mode`, whose sole declarer stays the bound agent-configuration
  revision (#9 Rev. 4).

The service stores the presented attestation with the enrolment and compares it
at each connection. An attestation differing from the enrolled one — a different
capability revision id, a different probe result, a different observed version, a
changed attach state — is a new attestation requiring a new operator enrolment,
visible as a diff, never a silent widening (`runner-attestation-changed`).

For an ephemeral CI Runner, the CI TrustPolicy and unique job assertion replace
the long-lived enrolment record, not the attestation. The bound Runner manifest,
executor identity and measured carrier facts still accompany the one-attempt
credential; the assertion cannot author or widen ADR 0006 capabilities.

**Placement is the half this record adds.** ADR 0006 refuses a capability the
bound runtime capability revision does not attest. Run start now also refuses a
binding that **no connected, authorized Runner** attests — authorization is a
long-lived enrolment or an ephemeral CI TrustPolicy exchange —
(`no-runner-attests-binding`), naming the node, the binding, and the missing
attestation, before any durable run, binding, attempt, or provider process — the
409 shape #60 already uses. An unplaceable run is refused, never queued in the
hope a runner appears, because queueing it turns fail-closed into a hang. The
same rule carries provider auth modes, as #1 requires: a runner declares the
modes it can *enforce* on its host, and a binding to a mode it does not attest
refuses rather than downgrading.

### 8. The terminal channel is a separately gated, default-off capability

Attach is the one channel that lets a human's keystrokes into a
credential-bearing process, so execution attestation never implies it. It is
gated separately from execution, and every gate below is load-bearing on its
own:

- **Default off.** A deployment enables it explicitly, and the runner carries
  that state as the `attach_channel` field of its runner attestation (§7) — a
  wrapper field, never an entry in ADR 0006's immutable manifest, because a
  deployment toggle is an authored grant and 0006 admits none. It does not
  restate `mode`, whose sole declarer stays the bound agent-configuration
  revision (#9 Rev. 4).
- **Per-attach step-up.** Enabling the deployment is not consent for one attach.
  Each attach is a distinct operator authorization producing a single-use ticket
  bound to exactly one `(attempt id, runner id, operator actor)`.
- **A ticket is a credential and is handled as one.** Its value crosses to the
  authorized operator client and nowhere else. Durable state holds the ticket's
  **opaque id and a digest of its value** — never the value itself, in any
  record, log, event, receipt or API resource, under the same secret rule §6
  applies to every other credential here.
- **The bearer is unguessable, and the id is not authority.** Storing a digest
  protects a *strong* bearer and publishes a brute-force target for a weak one:
  a sequential counter, a UUID scheme, a timestamp, or any value derivable from
  the attempt, runner or actor identity the record already publishes satisfies
  every other sentence here and is recoverable offline from the durable digest by
  anyone who reads it. So the bearer is drawn from a cryptographically secure
  random source with **at least 256 bits of entropy** — the named need being
  exactly that stored digest, since a guess costs one hash — and from no other
  source. Because the bearer carries full entropy, a single SHA-256 is a
  sufficient digest and no password-derivation function is wanted; that
  sufficiency is a consequence of the entropy floor, so the two rules travel
  together and neither is weakened alone. The opaque id **identifies** a ticket
  and never authorizes one: redemption requires the bearer whose digest matches,
  and presenting the id alone is refused like presenting nothing.
- **Verification is constant-time, and the bearer is confined.** The stored
  digest is compared against the presented bearer's digest with a constant-time
  comparison, because a byte-wise compare over a stored digest leaks it one byte
  at a time. Raw bearer bytes exist only at issue and at redemption: they cross
  once to the authorized operator client, are compared and discarded, and are
  never re-displayed or recoverable afterwards — an operator who loses a ticket
  authorizes a new attach rather than retrieving the old one.
- **Consumed exactly once, atomically.** Redemption is a compare-and-consume
  against durable state: the first redemption wins and marks the ticket spent in
  the same operation that authorizes the attach; a concurrent second redemption
  loses and refuses (`attach-ticket-consumed`). A read-then-mark sequence is not
  acceptable, because that race is exactly one unauthorized attach.
- **Short lifetime with a named bound.** A ticket's lifetime is deployment
  configuration against a named need (a ticket must not outlive the operator's
  presence at the terminal), and it is **capped** at the earlier of the attach
  session's end and the bound attempt's `attempt_deadline_seconds` (ADR 0008).
  The deadline is the ceiling, not the lifetime: an attempt may legitimately be
  allowed to run for hours, and a ticket valid for hours is a standing key. No
  new constant is minted here, because the duration is configured, not decided.
- **Audit.** Every attach writes a durable record — actor, attempt, runner,
  ticket id, start and end. An attach whose audit record cannot be written does
  not start. The attempt's receipt carries #9's operator-influenced marking as
  that record already requires.

An ephemeral CI TrustPolicy grants no attach capability. V1 attach stays local
(#9 part 2). This record does not open remote attach; it states what remote
attach must present when its epic runs.

### 9. Every command carries a typed, authenticated actor

An actor is a typed identity, not a free string, with exactly three kinds:

- `operator` — an authenticated operator principal (§3);
- `agent` — the conductor (#7) or any client acting under a published
  configuration or policy revision, recorded together with that revision id and
  the operator who enrolled it;
- `worker` — an authenticated Agent Runner or Effect Worker together with its
  exact role; it reports evidence only and never issues a command that changes
  catalog or verdict.

Durable command records bind the actor identity and, for an `agent`, the exact
published revision it acted under, so "who started this, under which published
policy" is answerable from the record. `ReconcileActor`'s caller-asserted string
is superseded by that authenticated identity in the same change that lands the
operator authenticator; until then it is a self-declared label and no document,
API description, or cockpit surface may call it attribution.

**An `agent` actor authenticates with a credential, and a revision id is not
one.** The published revision an agent acted under answers *under which policy*;
it can be read by anyone who can read the catalog, so it proves nothing about
*who called*. An `agent` client therefore holds its own credential under §4's
enrolment shape — one credential per client, a durable enrolment record naming
the enrolling operator, the revisions it may act under, and its revocation state,
with the same three-valued lifecycle. The record binds both facts and never
conflates them: the credential establishes the identity, the revision id records
the policy.

**Delegation is bounded and does not chain.** An `agent` actor never exceeds the
authority of the operator who enrolled it: a command the enrolling operator may
not issue is refused for the agent too, and revoking that operator revokes every
agent enrolled under them. An agent cannot enrol another actor, cannot mint a
credential, and cannot act as an `operator`; an agent that presents an operator
actor is refused (`actor-kind-not-permitted`) rather than downgraded. A run
started by an agent that itself starts runs carries the originating actor
unchanged through the chain, so depth never launders identity.

### 10. Failure semantics: loud, and never a widened blind spot

- Every refusal named here is typed and terminal. None degrades to a weaker auth
  mode, a longer timeout, an unauthenticated retry, or a clamped bound.
- Authentication, enrolment, or attestation that cannot be verified refuses
  before any durable run, binding, attempt, receipt, or provider start.
- Before `LAUNCH_ARMED`, a lost lease may be assigned again only when
  authoritative no-launch evidence proves that no provider or Effect operation
  began. At or after `LAUNCH_ARMED`, silence is not evidence about the external
  operation: the attempt remains `POSSIBLY_RAN` (ADR 0001), and Core never
  replaces, replays or re-places it. Revocation stops new bindings and resolves
  nothing already in flight.
- The lease, launch fence, terminal-evidence acknowledgement and reconciliation
  protocol belong to #15. Until that protocol and the #21 carrier decision are
  implemented, remote and CI bindings are represented but refused as
  unavailable rather than advertised as working.

**2026-08-25 amendment (Independent review, #672): the session wire's own
revision is explicit, not inferred from a field count.** `PREPARE` widened
from 19 to 21 fields to carry an Attempt's declared output schema and pinned
turn limit alongside what it already carried — the same `AgentExecutionRequestV2`
content `#663` gave every `LOCAL_PROCESS` Attempt, now reaching a
`RUNNER_LEASE` carrier too. A payload's field count is an implementation
detail of one message, not a fact a peer can safely infer a whole protocol
generation from, so the session wire now names its own generation in the
frame domain a peer must match exactly (`runner-session/v2`, up from
`runner-session/v1`). A frame built to that superseded domain is a real,
well-formed peer speaking a revision this decoder no longer serves — decode
answers it by name, `runner-session-incompatible-revision`, not by folding it
into the generic `runner-session-noncanonical` malformed-bytes bucket. This
keeps §10's own rule for every other boundary here: a stale peer fails loud
and named, before anything is armed, rather than surfacing as a decode error
indistinguishable from corruption. The manifest a Runner is selected under
attests the same domain (`contracts/runner_manifests.py` reuses the wire
codec's own constant rather than a second copy of the literal), so a
manifest naming a retired revision is refused the same way a live frame is.

This compatibility is asymmetric, and deliberately not smoothed over. A
current decoder recognizes the retired `v1` domain by name because that
literal is fixed and known today; an already-deployed pre-#672 decoder was
never taught the current `v2` domain or the `runner-session-incompatible-
revision` vocabulary, so it cannot read a new peer's revision, or a new
peer's REFUSE naming that code, by name at all — it only fails at the same
generic domain-mismatch check every other unrecognized domain hits. Forward
compatibility (an old peer reading new traffic) is therefore strictly weaker
than the backward compatibility proven above (a new peer reading old
traffic): both directions fail safely, before anything is armed, but only
one direction fails by name.

**2026-08-29 amendment (#889): terminal evidence has one canonical V2
record.** `runner-terminal-evidence-exchange/v2` carries all six terminal
variants and the payload-free ACK tombstone. A decoded provider result carries
its bounded output and optional canonical `AttemptTranscript`. A decoded
provider failure carries exactly one of `AGENT_REFUSED` and
`PROCESS_EXITED_UNSUCCESSFULLY`, its physical signed-int64 exit code and bounded
standard error, and its optional canonical transcript. Reserved fields are
empty, so one byte record has one meaning. Its semantic identity uses the
separate `runner-terminal-evidence/v2` hash domain and binds transcript presence
apart from transcript bytes; for a provider failure it also binds the named
failure code and the complete exit signature.

The codec's exact syntactic maximum remains 1,106,413 bytes: the longer admitted
failure code, a fixed-width exit code, 49,152 bytes of standard error, a
1,048,576-byte canonical transcript, and maximum-width UTF-8 generation and
invocation identities. The candidate journal grants 2,097,152 bytes, so
retained evidence keeps the transcript in that one record under one ACK
protocol and one durable owner; no second transcript file, evidence channel, or
acknowledgement protocol is introduced. The V2 decoder recognizes an Exchange
V1 domain only to return a typed refusal naming V1 and explaining that only V2
is supported; it never decodes a V1 payload beside V2.

**2026-08-30 amendment (#900): the session bound is derived from the record
bound.** Measured before moved, because a record that cannot exist is not a
maximum. `RunnerSessionFrame` pins a session's generation and invocation tokens
to exactly 43 base64url characters, and the Runner builds its evidence envelope
from the same binding and invocation the frame carries. Under those real
identities the widest record is a provider failure with the longer admitted
failure code, a fixed-width exit code, 49,152 bytes of standard error and a full
1,048,576-byte transcript document: 1,098,307 bytes. That is the one number the
transport is sized against, and it is the largest record a session can ever be
asked to carry.

Today's Runner does not originate it, and today's Runner is not what sizes the
transport. `runner/session.py` refuses to publish a provider failure carrying a
transcript, and the one registered runner-side executor
(`FreeRunnerCandidateExecutor`) returns no transcript under any job while
reading its child's answer under a 49,152-byte bound, so the widest record a
live provider child can cause today is 49,691 bytes. Sizing session transport
at that number would reopen the same silent gap from the other side the moment
a second executor lands: what a session must deliver is what may be durably
stored, and the record codec and `RunnerJournal` own that, not the current
executor catalogue.

The codec's 1,106,413-byte maximum stands 8,106 bytes above that, and the whole
gap is identity width: `RunnerGenerationId` and `RunnerInvocationId` bound
themselves to 1,024 *characters*, which is 4,096 bytes each in UTF-8, where a
session frame admits 43. Those 8,106 bytes are records the journal may hold and
a session can never carry.

Which bound moves is readable in the code rather than a matter of taste.
`TERMINAL_RECORD` hands the journal's canonical record over verbatim as its
single payload field: `RunnerJournal.publish` stores exactly the bytes the
session later sends. The two numbers are therefore not two budgets but one plus
a fixed envelope, and the storable maximum had already crossed the old one. That
envelope is 404 bytes, fixed because every identity a session frame carries has
a pinned width, so delivering that maximum needed a 1,098,711-byte body
against a 1,078,291-byte limit -- short by 20,420 bytes, and short in the way
that surfaces only on the rarest run, as `runner-session-oversized`: a transport
word for a contract fault.

So the transport side moves, and it moves by construction rather than by choice.
`MAXIMUM_RUNNER_SESSION_BODY_BYTES` is now
`MAXIMUM_RUNNER_TERMINAL_EVIDENCE_RECORD_BYTES + TERMINAL_RECORD_ENVELOPE_BYTES`
(1,106,817 bytes), and `MAXIMUM_RUNNER_SESSION_WIRE_FRAME_BYTES` adds the
four-byte length prefix (1,106,821 bytes). Raising the record bound now raises
the transport bound with it; a second literal beside the first is what let these
two disagree at all. The terminal-record codec and the session identity contract
are unchanged.

Where each number comes from, so the next reader need not re-derive it:

- 1,106,413 bytes --
  `test_largest_admissible_v2_record_equals_its_codec_bound_and_fits_journal`
  builds the widest encodable record and asserts the constant is exactly it.
- 404 bytes -- `test_a_terminal_record_body_spends_one_fixed_width_on_its_envelope`
  measures the envelope from the production encoder.
- both session bounds --
  `test_a_session_body_carries_the_largest_record_the_journal_may_hold` shows the
  largest journal-legal record filling a `TERMINAL_RECORD` body exactly.
- 1,098,307 bytes and the full path --
  `test_the_largest_record_the_journal_may_hold_reaches_core_over_tls` builds
  that record under a session's own 43-character identities, publishes it
  through the real `RunnerJournal` under the production journal bound, resumes
  it in the production candidate session, and carries it over genuine TLS
  through the production transport and `CoreRunnerSession` to commit, ACK
  tombstone, RELEASE and journal removal.
- 49,691 bytes, what a Runner originates today --
  `test_a_real_runner_originates_its_widest_terminal_record_and_delivers_it_over_tls`
  plants nothing: a real candidate subprocess answers at its executor's own
  bound, the session decodes that child's exit into the evidence envelope
  itself, journals it, and the same production path delivers it.

The 8,106 bytes of transport headroom are what one identity contract admitting
widths another refuses costs. Narrowing `RunnerGenerationId` and
`RunnerInvocationId` to the session's 43-character token would collapse both
numbers onto one width and remove the headroom; that touches every minting
caller and is not this slice.

The wire proof does not cover a live Core store, the Docker/launcher host path,
or a real crash and restart. A crash after provider start but before terminal
publication remains an open #301 gap; this slice adds no schema and no second
durable owner.

`RunnerProviderFailure` still defaults its omitted failure code to
`PROCESS_EXITED_UNSUCCESSFULLY`: the current session and other pre-integration
callers construct only the old one-argument physical-failure form. That default
is a compatibility seam with a named removal path, not an Exchange V2
ambiguity. A later #301 integration must pass the decoder's actual
`AGENT_REFUSED` or `PROCESS_EXITED_UNSUCCESSFULLY` code and transcript, then
delete the default and `RunnerEvidenceCannotCarryTranscript`; until then the
current Runner does not originate decoded `AGENT_REFUSED` or failure
transcripts.

## Refusals

| Name | Raised when | Boundary |
| --- | --- | --- |
| `unauthenticated-exposure` | a deployment declaring `reachable` exposure, or declaring none, with no composed operator authenticator | host composition |
| `exposure-bind-contradiction` | a deployment declaring `this-machine` exposure bound to a non-loopback address | host composition |
| `runner-transport-mismatch` | a connection whose transport does not match the declared tier | runner connection |
| `runner-peer-unverified` | the runner's per-invocation identity is not established, or the service does not authenticate back, where §2's identity invariant is required | runner binding |
| `runner-unknown` | a runner with no enrolment record requests work or a ticket | runner connection |
| `runner-revoked` | the enrolment record is marked revoked | runner connection |
| `runner-attestation-changed` | the presented runner attestation differs from the enrolled one | runner connection |
| `no-runner-attests-binding` | no connected authorized Runner attests the bound capability, executor, provider mode or auth mode | run start |
| `auth-profile-unresolvable` | the bound credential reference does not resolve on the runner's host | run start |
| `attempt-binding-unknown` | a runner acts on an attempt it was not bound | attempt handoff |
| `attempt-binding-terminal` | a runner acts under a terminal attempt id | attempt handoff |
| `attach-ticket-consumed` | a ticket is redeemed a second time, including concurrently | attach |
| `attach-ticket-invalid` | a redemption presents no bearer, an unissued bearer, or a bearer whose digest does not match the named ticket | attach |
| `actor-kind-not-permitted` | an `agent` actor presents an operator actor, or enrols or delegates | service authorization |
| `runner-report-out-of-scope` | a report carries a disposition, receipt, or catalog mutation | runner report |
| `runner-not-authorized` | a runner attempts a catalog or command operation | service authorization |

Durable failure tokens, where any of these must become one, are minted by #16;
this record borrows that owner rather than opening a second vocabulary.

## Consequences

- The loopback rule stops being a Claude-specific special case and becomes the
  product's general rule: no exposure beyond this machine without an
  authenticated operator. ADR 0004's "safe only on the trusted local boundary"
  gets its named successor, and #7 gets the identity it needs before the
  conductor issues commands.
- A deployment must now state its exposure, and stating none refuses. That is a
  new obligation on every deployment including today's local one, and it is the
  price of not inferring reach from a bind address a proxy can front.
- Serve and Runner ship as separate artifacts. Provider tools and credentials
  leave Serve; raw carrier lifecycle authority never enters it. #312 proves the
  exact artifacts and cutover, rather than this record duplicating that plan.
- The identity invariant is carrier-neutral. #21 decides the first local form:
  rootful Docker Engine/Compose, host-launcher container lifecycle, per-Attempt
  hardened containers and private networks, and X.509 mutual authentication.
  Rootful Docker, host launcher, host and operator CA remain the local TCB.
  Remote/CI stays an explicit later decision rather than inheriting this form.
- Long-lived Runner credentials cost an enrolment ceremony. Ephemeral CI jobs
  instead cost one narrow TrustPolicy and one short-lived, one-attempt credential
  per unique job. Neither path accepts a shared fleet secret.
- Agent Runner and Effect Worker are separate privilege lanes. CI may carry
  either one as a job, but never turns the Atelier DAG, artifacts or receipts
  into CI-owned truth.
- The phased implementation and deletion ledger live on #15, #301 and #312:
  `#15-A → #301-A → #15-B → #301-B → #312 → Deletion`. This ADR owns the
  invariant and links the plan rather than copying it.
- The static one-network form in #301-A is disposable test composition only.
  #312 owns dynamic per-Attempt network creation, drift refusal and cutover; no
  local live installation changes before that owner reaches its own gate.

## Required proofs before implementation is accepted

- Composition refuses a deployment declaring `reachable` exposure with no
  operator authenticator, refuses one declaring `this-machine` while bound to a
  non-loopback address, refuses one declaring no exposure at all — and the
  existing `this-machine` loopback composition still succeeds unchanged.
- A forwarded header naming another address changes nothing: it is neither read
  as identity nor as exposure, and the same request is authorized identically
  with and without it.
- An unknown, revoked, or attestation-changed runner receives no attempt binding
  and no attach ticket, and no durable row is written for the refusal path.
- A revoked runner and a never-enrolled one produce **different** refusals from
  durable state, and re-enrolling a revoked id requires an explicit operator act.
- Where remote/CI needs §2's identity invariant, a runner whose per-invocation
  identity is not established is refused before any attempt binding, and so is a
  service that does not authenticate back — proven against the mechanism of that
  remote/CI tier.
- The bounded current local live-host witness records the two expected Core/Runner
  peers authorizing, exact expected client URI-SAN binding, same-CA wrong-URI
  identity refusal before an operation, wrong-EKU TLS refusal, cross-Attempt
  network probes unreachable, and removal of one Attempt's Runner while Core and
  the other Runner keep running. It does not execute a wrong-CA or wrong-server-
  identity case. Its `result.md` SHA-256 is
  `9c4d962b2bb1dfb3c1dc152979998b4c5297e102d8fedc8416e4c1c787d39da5`
  with successful transcript SHA-256
  `9cc5704b8273c431879695735a38045d8893adcd5ca4f6c015a1f6deadfbac04`,
  manifest SHA-256
  `952eb84623cd20fcbb1dc555a255689f020d7644bd952f1872675c16ac3c73a9`
  and external cleanup proof SHA-256
  `7eaf668be6129fcf78fe46eb25d58e18e14a3b0df14cc0f83cb73edc842eef0f`.
- A disposable #301-A candidate (`scripts/runner_candidate.sh`) on this host
  proved one success Attempt `SUCCEEDED`/`ACKNOWLEDGED` with a real manifest
  content identity, launcher inspect equality, measured READY matching that
  manifest, and Landlock on the free child exec; and one cancel Attempt
  `CANCELLED`/`ACKNOWLEDGED` with `replacement=NONE` and `REAPED_AFTER_KILL`.
  Witness directories `/var/tmp/atelier2-301a-runner-witness.6Bj1kk` (success
  Core store SHA-256
  `9c5c8548e593dd79e123a1b9cdf9343dbc67ec11936f6a66c07f1711aa69efe1`) and
  `/var/tmp/atelier2-301a-runner-witness.0WqeSL` (cancel Core store SHA-256
  `7b84cc59cc65bcb51c31ee4fb3996dde73353dd6872d82efe182dc4bff9ee901`). After
  receiver success the host private keys were unlinked through held directory
  FDs; the retained trees hold public certificate metadata only. A retained
  Core-restart leg at
  `/var/tmp/atelier2-301a-runner-witness.jZs0NF` proved one provider child
  (PID `2372845`, start tick `24859444`) in Runner container
  `ba621229c2e449e2cd5782eb0f934e1cada22f21673da49eaf00ffe4f2b436a0`
  from the fenced `STARTED` cut through Core death and reconnect `STARTED`,
  with `pids.current == pids.max == 2`, unchanged `pids.events:max == 0`, and
  one `FAILED`/`ACKNOWLEDGED` terminal record. Its Core store SHA-256 is
  `a9b14d78b632804ea82bb36d780b0ea4cdc4033be46f497c230de3106d8869a7`;
  its cut, child-survival and terminal-proof records have SHA-256 values
  `969170f5341ceb750fc44a59ee48c19dc1df20b9dd13a9fa6614b96fad487787`,
  `2abe74e56683a17c8b596a08dba2a2f268d3856ae5326bf49b75a5c83172590f`
  and
  `e5e742c1786860d67fecb217f0bc00d93900b18d983c9ab2a8a6ead62dadb707`.
  Exact labelled Docker objects were empty after `RELEASED`. Together these
  legs do not prove live A.1 availability, cancel races, replacement `ONE`, a
  wrong-CA live refusal, or packaged cutover. They also do not close the
  STARTED-fence TOCTOU window: from Core writing its STARTED FIFO request until
  the host completes the `pids.max` reduction, a provider crash and respawn can
  make the replacement the identity that the fence records. Focused tests cover
  peer EKU/SAN/CA refusal, Landlock identity denial, journal ACK/RELEASE order,
  and the A request-subset/refusal vocabulary.
- A runner identity is not satisfied by a reused name: an identifier that
  outlives the runner it named never binds a later attempt.
- A CI assertion with the wrong issuer, repository/project, workflow/config,
  ref/environment or unique job identity receives no credential or work. The
  same assertion replayed after exchange also receives neither.
- A short-lived credential is usable only for its exact attempt, generation and
  worker role. Agent Runner and Effect Worker credentials are mutually unusable,
  and neither carries ambient repository-mutation authority.
- The inspected Serve artifact contains no provider CLI or credential and has no
  Docker/OCI socket, systemd/DBus or other raw carrier authority. The separately
  identified Runner artifact is the only Agent-execution image.
- Run start refuses a binding no connected runner attests, naming node, binding
  and missing attestation, with no run, binding, attempt, receipt or process.
- A runner report carrying a disposition, receipt, or catalog mutation is
  refused whole and changes nothing durable.
- A full durable and API projection after a fake run contains no credential
  value and no verifier path — the canary shape #58 acceptance 8 already uses.
- Attach without a valid ticket refuses; a failed audit write prevents the
  attach; an attach past the bound attempt's deadline refuses; and a ticket
  redeemed **twice concurrently** succeeds exactly once, the loser refusing with
  `attach-ticket-consumed`.
- No ticket value appears in any durable record, log, event, receipt or API
  resource — the same canary shape the credential proof above uses — and it is
  not retrievable after issue by any surface.
- A redemption presenting the ticket's opaque id with no bearer, with an unissued
  bearer, or with another ticket's bearer refuses (`attach-ticket-invalid`) and
  consumes nothing, proving the id is an identifier and not authority.
- An `agent` actor with no credential is refused; one presenting an `operator`
  actor is refused rather than downgraded; and a command its enrolling operator
  may not issue is refused for it too.
- Before `LAUNCH_ARMED`, reassignment succeeds only with authoritative no-launch
  evidence. At or after `LAUNCH_ARMED`, a disappearing Runner leaves the attempt
  `POSSIBLY_RAN`, and no second Runner ever receives it.
- Two enrolled runners with different attestations place only the bindings each
  attests, proving placement is per runner and not per deployment.
- Deleting or expiring native CI logs and artifacts removes no canonical
  artifact, receipt or reconciliation evidence from Core.

## Out of scope and stop conditions

This record decides only the local rootful Docker form described in §2. It leaves
**OPEN on #21** the first remote/CI carrier, its launch/cleanup authority and
its mutual-authentication mechanism. Beyond the versioned session and retained
terminal-evidence records above, it does not decide carrier transport framing;
the environment-requirements vocabulary; multi-project or multi-tenant isolation
(#23); the operator-credential storage backend or cockpit login surface; the
provider-side sandbox mechanism (#60); durable failure token names (#16); rate
limiting or quota. #15 owns lease/fencing/evidence acknowledgement and
reconciliation; #301 owns the Agent worker; #312 owns dynamic per-Attempt
networks, packaging and cutover; #9 owns the operator-facing epic and remote
attach.

Stop implementation on: a shared runner secret; a runner writing a receipt,
disposition, or catalog revision; a provider credential value crossing the
service in either direction; a worker-identity credential written into durable
state, logs or carrier artifacts; an attach path without a per-attach ticket and
an audit record; a ticket value written into durable state, or redeemed by a
read-then-mark sequence instead of an atomic compare-and-consume; a ticket bearer
from anything but a cryptographically secure random source, or below the entropy
floor, or verified by a non-constant-time comparison, or treated as authorized on
its opaque id alone; a worker bound by a reusable name instead of a
per-invocation identity; a shared Agent Runner/Effect Worker credential or
privilege lane; an ephemeral CI job manually enrolled as a long-lived Runner; a
carrier assertion accepted outside its pinned TrustPolicy; raw Docker/OCI,
systemd/DBus or privileged-broker authority entering Serve; a
deployment-authored or probe-derived entry written into ADR 0006's immutable
capability manifest; exposure inferred from the bind address, or a forwarded
header read as identity; a revocation that deletes the enrolment record instead
of marking it; an `agent` actor authorized by a revision id alone; an unplaceable
run that is queued instead of refused; a remote binding published as available
before the ownership contract exists; an actor field described as attribution
while it is still caller-asserted; local #301-A mutating a live installation;
or remote/CI carrier-bound implementation beginning before #21 records its
separate open decision.

## Supersedes

No other ADR. This record's 2026-08-20 amendment supersedes its own watchdog and
direct-systemd target descriptions and records rejection of the one-container
target; the compact context note above is retained only as migration history.
