# ADR 0011: A project is a store root; the root bounds where a project exists, and destroying it is the only removal

- Status: PROPOSED 2026-08-15 — first project is the host-configuration
  mapping plus the runtime and zero-or-one HTTP reads that treat it as a
  project; store-per-project and a second project are not implemented;
  decision 2 and the catalog-export loss list amended 2026-08-25 (candidate
  store, operator ruling on issue #642) and again 2026-08-26 (#642 P2b, the
  order that carries the candidate invariant); the candidate store of decision 2
  is the one part now implemented — an attempt keeps its work in it before it is
  completed — while everything else here remains decision only
- Date: 2026-08-15
- Decision authority: [Issue #23](https://github.com/FlexOr2/atelier-2/issues/23),
  SHA-256 over the exact served UTF-8 body bytes with nothing appended — 847 bytes,
  no trailing newline —
  `f495eba44482cf5353f9363166856995a6af82f03fdf1c8da85b860e2be49c39`; widened by
  three directions on that issue, each hashed over its served body without the
  trailing newline that body carries:
  [comment 5302063026](https://github.com/FlexOr2/atelier-2/issues/23#issuecomment-5302063026)
  (project as a first-class configured bundle), 618 bytes,
  `700462d103e82d5cf3dd568696410e253c74ded96987a404bafb022877dd177d`,
  [comment 5302135318](https://github.com/FlexOr2/atelier-2/issues/23#issuecomment-5302135318)
  (retention and deletion assigned to this decision), 2009 bytes,
  `ccaab9374b66d0288d36d9fa2132e8aa8779ad68775a5fae6c2f9229d07e012f`, and
  [comment 5302776983](https://github.com/FlexOr2/atelier-2/issues/23#issuecomment-5302776983)
  (what is installation-level and what is the project's, with the library register
  as the pattern case), 1306 bytes,
  `354f6feca061962b4cc3939833b94c29a712964a77ff6790ab094c5d2af99006`
- Requirement authority: [Issue #1](https://github.com/FlexOr2/atelier-2/issues/1)
  (single-user V1, live-versioned configuration, secrets by reference),
  [#79](https://github.com/FlexOr2/atelier-2/issues/79) (the operator scenario this
  record serves: several external projects, each its own token, each its own queue).
  The landed requirement documents this record must not contradict — each `DRAFT`,
  and a requirement never outranks a landed decision record:
  [0001](../requirements/0001-queue-und-autonomie.md) REQ-QUEUE-09 (a project is a
  configured bundle) and its open retention question, which names this decision as
  the owner; [0002](../requirements/0002-teams-und-zugang.md) REQ-ZUGANG-08, -09 and -14 (the
  project is the sharing unit; sharing libraries and workflows means sharing git
  sources, and there is no second sharing channel; credentials stay references and
  are never transported); [0004](../requirements/0004-runner-und-remote.md) REQ-REMOTE-06
  and REQ-REMOTE-07 (`allowed-projects` is an enrolment fact placement matches on, never a claim
  a connecting process makes).
- Depends on: [ADR 0001](0001-durable-runtime.md) (the canonical store, the
  process-global DBOS binding, the append-only ledgers this record partitions),
  [ADR 0009](0009-runner-trust.md) (the one trust boundary, credentials by
  reference, and the bound on the same-UID acceptance this record consumes) — on
  `main` since PR #78 merged as `1a88dbdd`,
  [ADR 0007](0007-catalog-identity.md) (definition sources,
  store-stable lineage and measurement identity, and the catalog export this
  record's decision 6 builds on) — ACCEPTED 2026-08-16, document only and not
  implemented,
  [ADR 0008](0008-budget-units.md) (which sums are legal),
  [ADR 0010](0010-github-platform-adapter.md) (the tracker connection a project
  configures) — on `main` since PR #81 merged as `87cd5700`
- Hard predecessor of a second project:
  [#60](https://github.com/FlexOr2/atelier-2/issues/60) — the sandbox mechanism and
  its functional probe, the only thing that can confine a running attempt to its
  project (decision 2). This record decides that a second project waits for it and
  decides no sandbox mechanism.
- Feeds: [#16](https://github.com/FlexOr2/atelier-2/issues/16) Phase 2, which owns
  every schema transition and executes what decision 5 shapes;
  [#79](https://github.com/FlexOr2/atelier-2/issues/79) (per-project queue,
  pause and resume), [#24](https://github.com/FlexOr2/atelier-2/issues/24) (the
  adapter a project binds), [#58](https://github.com/FlexOr2/atelier-2/issues/58)
  (the workspace lease, whose root decision 2 fixes)
- Names, never decides: [#82](https://github.com/FlexOr2/atelier-2/issues/82)
  (login, roles, tenancy), [#9](https://github.com/FlexOr2/atelier-2/issues/9)
  part 3 (remote runners), [#16](https://github.com/FlexOr2/atelier-2/issues/16)
  (columns, versions, migration mechanics),
  [#60](https://github.com/FlexOr2/atelier-2/issues/60) (the sandbox mechanism
  itself, named above as a predecessor and designed there)
- Evidence: documentary and read-only, at `main` `78a3487`.
  `src/atelier2/adapters/dbos/schema.py` (`SCHEMA_VERSION = 8`, fourteen product
  tables, `_PRODUCT_SCHEMA_FINGERPRINT_SHA256`, twelve `*_no_delete` triggers —
  `runs` carries only `runs_binding_no_update` and `atelier_schema_versions` carries
  none), `src/atelier2/adapters/dbos/runtime.py` (`DbosRuntimeBinding`,
  `DbosRuntimeBindingConflict`, the process-global runtime, and the agent-control
  root resolved as `<database>.parent/.atelier2-agent-control`),
  `src/atelier2/host/serving.py` (`HostSettings.database_path`, `effect_store_path`,
  the loopback refusal). No code changed, no gate claim below; nothing here is
  implemented.

## Context

Atelier holds one canonical SQLite store per installation. It carries every product
table, the DBOS system tables and `datasource_outputs`, and a process binds exactly
one of them: a second, incompatible binding inside one process is refused by name
(`DbosRuntimeBindingConflict`). There is no project dimension anywhere.

#79 plans to point that store at other people's work: *"mehrere externe Projekte,
auch Arbeit, je eigenes Token, je eigene Queue."* Three measured facts make that a
decision rather than a feature.

**Nothing can be removed.** Twelve of the fourteen product tables refuse row
deletion by trigger; `runs` carries only `runs_binding_no_update` and
`atelier_schema_versions` carries none, so the ledger is *almost* uniformly
append-only and nowhere prunable. There is no TTL, no retention, no run export and
no project deletion. Today the only way to remove anything is to throw the whole
file away — which, with one file, means throwing every project away.

**Nothing separates one project's credential from another's.** ADR 0009 forbids a
runner reading "another project's" credential and hands the mechanism here. A
column is not that mechanism: it is a filter every future query must remember.

**Nothing confines a running attempt.** #60 measured this host read-only: `bwrap`
exists and fails, `kernel.apparmor_restrict_unprivileged_userns = 1`, and the
components a confined Claude attempt needs are missing — the host is `UNAVAILABLE`
for a confined attempt, and an executable check is not a sandbox proof. A provider
process the service starts therefore runs as the service's own OS user with the
whole filesystem in reach. This fact decides how much a directory can honestly be
asked to carry, and decision 2 carries it rather than papering over it.

The first two are one question. A store per project makes deletion the removal of a
directory; a store with a project column must answer deletion against `no_delete`,
against DBOS's own project-blind ledger, and against a separate effect-store file.
Deciding them apart loses the second to the first. The third is a different
question with the same due date, and it is answered by naming a mechanism and a
predecessor, never by renaming a directory an access boundary.

## Decision

### 1. A project is a configured bundle with a minted id and one root

A **project** is the operator's unit of work and of consequence: a repository the
work happens in, a tracker connection (ADR 0010), a credential reference (never
material), workflow assignment rules, an item filter, and its running state.

Its identity is a **minted opaque id**, assigned once. It is deliberately *not*
derived from the bundle, unlike ADR 0007's lineage ids: two projects may honestly
name one repository with different filters and different tokens, and a derived id
would silently merge them, while a rotated token or a corrected filter would
silently fork one. Convergence across stores is not wanted here — a project is a
deployment fact, not shared content.

**The bundle lives inside the project's own store**, as immutable hash-identified
revisions with an append-only activation history — the shape
`auth_profile_revisions` already landed for exactly this class of fact:
`(id, revision_number)` unique over immutable revisions, deliberately outside the
catalog because it carries a credential reference. Renaming appends an alias event
and retires nothing; pausing and resuming append lifecycle events, latest wins.
This is what makes #79's resume button honest: the project's state is durable and
in one place, so resuming rebuilds no context.

**Exactly one fact about a project lives outside it: `project id → root path`,**
in the same live-versioned configuration channel as every other host setting. It
carries no credential, no filter, and no name. The store cannot hold it, because it
is what tells the service which store to open.

**What is installation-level and what is the project's** (operator direction
5302776983). The rule is existence against choice: **the installation owns what
exists, the project owns what is chosen or overridden.**

- *Installation-level*: provider connections; the **register** of definition sources
  and libraries (ADR 0007 decision 2, which this record leaves exactly where it is);
  default rules such as the model per role class and the budget frame; the runner
  fleet and its enrolment register (requirement 0002 REQ-ZUGANG-10); appearance and
  language.
- *Project-level, and therefore a bundle revision*: repository, tracker connection
  and credential reference; the **selection** of registered sources and libraries;
  workflow assignment rules and item filter; overrides of the installation defaults;
  running state.

The register-and-selection split is the package-registry shape: registered once,
selected per project. **The resolution order is one sentence — the project's value
wins, absence in the project inherits the installation's, and an explicit
deselection is a deselection and is never re-inherited.** A new project starts from
the installation's default selection; a default added later reaches every project
that never spoke about it and never reaches one that deselected it. There is no
direction in which an installation-level change overrides a value a project set,
because that would let a global edit silently narrow a running project.

The split also keeps the bundle honest about lifetime: the register is an operator
fact that outlives every project, and the selection is a project fact that dies with
its root (decision 3).

This record adds no ADR 0007 kind token and needs no amendment to it.

### 2. One project, one root; the root bounds where a project exists

Each project owns a **project root**: a directory holding its canonical durable
store, its effect store, its agent-control root — which already resolves beside the
store as `<database>.parent/.atelier2-agent-control` — its candidate store, derived
the same way as `<database>.parent/.atelier2-candidates.git` (2026-08-25 amendment,
#642-Journal: the project-owned bare-repository home for an attempt's captured
candidate trees that ADR 0010 decision 5's `push-atelier-commit` pushes) — and its
workspace and clone tree.

**A candidate is kept before its attempt is completed, and the two writes do not
share a transaction** (2026-08-26 amendment, #642 P2b). They cannot: the candidate
is a git ref in the project's own bare repository and the attempt is a row in the
project's SQLite store, and no transaction spans both. The invariant is therefore
carried by the order — the ref is written first, and only then is the attempt
completed — so a process dying in the gap leaves work that is readable beside an
attempt that never succeeded, never the reverse. A capture that fails ends the
attempt `FAILED` under `CANDIDATE_CAPTURE_FAILED` rather than escaping, because an
escaped error would leave the attempt armed and unresolvable. What this record
claims about the candidate store is placement, not atomicity.

**Nothing of a project exists outside its root**, and that is the whole rule this
record decides. It is a **placement** rule about the product's own writes: every
path a project's runtime opens is derived from that project's root, so the
installation can be read whole and no byte of A found outside A. That is what makes
deletion complete (decision 3), what makes a bad store one project's outage instead
of the installation's (decision 5), and it is provable by a canary read rather than
argued.

**What the root is not.** The root is not an access-control boundary, and this
record refuses to claim it is one. A directory does not stop a process: an attempt
running in A, as the service's OS user, can name an absolute path into B's root and
can read the credential directory ADR 0009 deliberately keeps outside every store.
The root bounds what the *product* writes; it bounds nothing about what a *provider
process* reads. This record therefore claims no OS-enforced isolation anywhere, and
its consequences state what the root buys and what it does not.

**The #23 sentence is therefore decided with its mechanism and its predecessor.**
The binding widening (comment 5302135318) reads: *no project can resolve another's
token, and no attempt of one project sees another's workspace or clone.* This record
decides that sentence as binding and splits it into the two mechanisms that can
actually carry it, because one of them exists and the other does not:

- **Resolution belongs to the service, and it is structural today.** A run start
  resolves a credential reference only through the acting project's own bundle, in
  that project's own store; a reference not named there is refused before any
  provider process is started (`credential-outside-project`). A project runtime
  holds no other project's auth-profile rows at all, so this is a property of what
  the process can see rather than a predicate a query must remember. Placement
  carries the same scope on the runner side: `allowed-projects` is an attested
  enrolment fact (requirement 0004 REQ-REMOTE-06 and REQ-REMOTE-07), so an attempt of A is never
  placed on a runner that is not allowed for A.
- **Confinement belongs to the sandbox, and it does not exist yet.** Stopping the
  *placed process* from reading B's root or a credential directory is the OS
  mechanism #60 owns — its filesystem `denyRead`/`allowRead` policy, its network
  allowlist, and its functional probe. ADR 0009 already wrote the bound this record
  consumes: the same-UID acceptance holds only while the whole boundary is one
  machine under one OS user, and it **ends** at "a second OS user or a second
  project sharing the host (#23)". A second project is precisely the event that
  revokes it.

**That consequence is a hard predecessor, not a caveat.** A second project is not
connected before #60's probe attests confinement on the host that will run it and
the live A→B denial in the proof list below is observed. Opening a second project
root on a deployment whose sandbox probe does not attest confinement is refused
(`project-confinement-unattested`) rather than served with a directory and a
promise. Until that day this record's isolation claim is exactly: one project's data
has one place, its deletion is complete, and its store failure is local. It is not:
one project cannot read another.

The alternative — one store with a project column — is rejected on the deletion it
cannot prove, not on taste. A project-scoped erasure there would have to reach
fourteen product tables under triggers that refuse deletion, the DBOS system tables
that carry workflow inputs and have no project dimension at all, and a separate
effect-store file; a reviewer could never confirm it complete. Isolation would rest
on every query remembering a predicate forever. Its one real advantage — a cheap
cross-project `SELECT` — is answered in decision 4.

The cost is named and accepted: DBOS binds one canonical store per process, so an
active project is served by **its own durable runtime process**. That process is
part of the coordinating service, partitioned by project — it writes durable truth,
so it is not a runner and **mints no second trust boundary**; in V1 the service and
its project runtimes are one OS user on one host, the `same-host` tier ADR 0009
already names, under the bound that tier itself carries above. Pause and resume then
land where they belong: a paused project drains to its terminal receipts (#79) and
its runtime exits; resume starts it again. The supervision contract for those
processes — restart, reaping, and what is unavailable while a project runtime is
down — is not decided here, and it is the second hard predecessor of a second
project.

### 3. Deletion is a whole project; a run is never deletable

- **Project deletion is the destruction of its root**, an operator act at the
  filesystem. The product's obligation is decision 2's placement rule, which is what
  makes the act sufficient. What can be taken out first, and what can never be, is
  decision 6 — narrower than an earlier draft of this line claimed.
- **Run, receipt, attempt, event and revision deletion is refused, permanently**
  (`run-deletion-refused`). These rows are the evidence ADR 0001's exactly-once,
  `POSSIBLY_RAN` and `WAITING_RECONCILIATION` guarantees are read from; a deletable
  run makes an unresolved attempt unresolvable and the ledger unauditable. This
  record adds no `no_delete` trigger and removes none, and it adds no runtime delete
  path a bug could reach.
- **There is no TTL, no automatic pruning and no size-triggered deletion.** Unbounded
  growth per project is the accepted cost; the operator's lever is a whole project,
  and silent pruning would be evidence loss with no actor.
- **Finer-grained removal does not exist.** One issue's text cannot be withdrawn from
  a store that keeps it, and this record refuses to pretend otherwise: a requirement
  to remove content below project granularity is a different store shape and would
  supersede this record rather than extend it.

The consequence for the operator's scenario is exactly what he needs to know before
connecting his employer's repository: its issue text and its code live in a root he
can destroy whole, and nothing finer.

### 4. Cross-project: sources point in, reads merge, credentials never cross

- **Definition sources stay operator-level** (ADR 0007 decision 2), and the reference
  direction is **project → source**: the register is installation-level and the
  selection is the project's (decision 1); a project's bundle names the sources it
  takes in from, and a source names no project and learns nothing about which
  projects used it. Intake copies bytes into the taking project's store, and a source
  is never a runtime dependency, so a project keeps running when a source is gone.
- **Cross-project reading is a merge, not a join, and it converges.** ADR 0007 already
  derives a lineage id from its founding revision hash and a measurement id from
  `(kind, terminal run hash)`, precisely so two stores agree. The same definition taken
  into two projects therefore carries the same lineage id in both, and one fact
  observed in two stores does not double-count. Cross-project views are read-only
  projections over N stores; no cross-project write, lock or transaction exists, and
  no in-installation shortcut moves rows between two project stores — content crosses
  only by the file form of decision 6.
- **Queue caps are per project by construction** — one queue, one store, #79's
  denominations unchanged. The **installation-wide ceiling** is decided here because
  the subscription is one and no project can see another: it is enforced by the
  coordinating service, the only component that sees every project, and it is
  denominated **only** in started attempts and summed `attempt_deadline_seconds`.
  Those are Atelier-counted and meter-free, so summing them across projects is legal
  under ADR 0008. Token caps stay per project and per meter revision and are **never**
  summed across either (`installation-ceiling-exceeded` refuses; it never clamps).
- **Credentials are strictly per project.** An auth profile is a project-store row and
  is never shared; a credential reference not named by the acting project's bundle is
  refused before any provider process (`credential-outside-project`). This is the
  resolution half of the mechanism ADR 0009 §5 defers here. That section lists its
  obligations as "enforced by the service rather than trusted to the runner", and
  the bullet handed here — a runner reading credential material of another project
  — is the one the service cannot enforce alone: it can refuse to *hand over* a
  reference, and only the sandbox can stop a started process from *reading* the
  directory. The confinement half is decision 2's predecessor, and this record
  splits the bullet rather than reporting it as covered.

### 5. What V8→V9 carries: the store is the dimension, so no row widens

#16 owns every transition and executes this shape; the shape is:

- **No project column is added to any product table**, in V9 or later. The dimension
  is the store, so isolation costs the migration nothing but this rule.
- **Each project store carries its own `atelier_schema_versions` and its own exact
  fingerprint.** Migration runs per store, independently, and a partially migrated
  installation is a real, allowed state.
- **A store the running binary cannot open exactly refuses that project only**
  (`project-store-version-mismatch`), never the installation. One project's failed
  migration or corrupted store is not the workshop's outage — the second thing the
  root buys after deletion.
- **Today's store becomes the first project's root**, with a minted id and no row
  rewritten. There is no data migration for isolation, only a configuration entry.

### 6. Export and import: the catalog travels, the project does not

#23's body names export and import among the consequences this decision must carry,
and decision 3 leans on them, so they are decided here rather than assumed.

**Exactly two forms exist, and neither is a portable project.**

- **The catalog export of ADR 0007 decision 6**, taken from and imported into one
  project store: one file per revision plus one manifest, one read transaction,
  all-or-nothing, refusing rather than merging, with selective bundles already
  removed there. It carries content and catalog-relational truth — revisions,
  lineages, activations, intake provenance as evidence, and measurements whose ids
  are store-stable. It is a **store transport** and not the library-sharing channel:
  sharing a library or a workflow with another operator is sharing a git source
  (requirement 0002 REQ-ZUGANG-09), which decision 1 leaves exactly where it is, and this
  record adds no third channel.
- **A backup of the root**, byte for byte, taken while the project's runtime is
  stopped. That is the whole project, and it is deliberately not an interchange
  format: it is the same store, moved or restored, and it is the only thing that can
  bring a destroyed project back.

**A whole-project export bundle is refused, and refused as a decision rather than
deferred.** It would carry a tracker connection and a credential reference into
another installation — exactly the transport ADR 0009 and requirement 0002 REQ-ZUGANG-14
forbid — and it would carry an attempt ledger the receiving installation never ran,
turning evidence about processes into a claim a file can assert. ADR 0007 removed
selective bundles for the same reason one level down, and decision 3 refuses removal
finer than a project for the mirror reason: what cannot be taken out one row at a
time must not be handed over one row at a time either.

**What a catalog export deliberately loses, each with its reason.** None of these
losses is silent — an export names what it contains, and this record names what it
will never contain:

- the **run, receipt, attempt, event and reconcile ledger** — the measurement *facts*
  travel as ADR 0007 entries and converge on the terminal run hash, but the ledger
  itself does not, because it is evidence about processes this installation ran, and
  an importable ledger is an assertable one;
- the **effect store** — operational state of one bound runtime, meaningless beside
  any other store;
- the **candidate store** (`.atelier2-candidates.git`, decision 2) — an attempt's
  captured candidate trees, deliberately lost because no exported relation gives
  them meaning: which attempt captured a given tree and which push intent later
  consumed it lives only in the attempt and effect-store facts the two bullets
  above already exclude, so an exported tree would be content with no addressee
  (2026-08-25 amendment, #642-Journal);
- the **project bundle** — repository, tracker connection, credential reference,
  filters, workflow rules, running state: deployment facts of this installation, and
  a credential never travels at all;
- **workspaces and clones** — reproducible from git, and #58's lease is bound to this
  machine's device and inode;
- the **source configuration** and **`project id → root path`** — ADR 0007 decision 6
  already refuses the first as the receiving operator's business in neither
  direction, and the second is host configuration.

**A backup is taken from a stopped project, and restoring it is a configuration
act.** The canonical store and the effect store are two files with no shared
transaction, so a copy taken while a runtime is bound is not a backup: the project is
paused, drained to its terminal receipts and its runtime exited first (#79's pause is
that act), and only then is the root copied. Restoring is putting the root back and
pointing `project id → root path` at it.

**A backup is the operator's own copy and never a transport between installations.**
Nothing stops a person copying a directory, and this record does not pretend
otherwise; what it decides is that the product mints no operation to adopt another
installation's root. Such a root arrives carrying that installation's minted id, its
credential references and its ledger, so configuring it is either the collision below
or an installation holding runs it never performed — the two outcomes this decision
exists to prevent. Content reaches another installation through the catalog export and
through nothing else.

**Destroying a root is irreversible, and the product offers no restore.** This is the
sentence decision 3 owes the operator: after the destruction the only recovery is a
backup he took himself, and the atelier keeps no copy, no tombstone and no undo
window. That is not a gap to close later; it is the price of a deletion a reviewer
can confirm complete.

**Identity on copy: the minted id belongs to the store, and no remint exists.**

- A root **moved or restored keeps its id**. The id lives in the project's own bundle
  revisions and no product row carries it (decision 5), so relocation rewrites
  nothing and every reference stays true.
- Two configured roots whose stores carry the same minted id are **refused at
  configuration** (`project-id-collision`), before either is opened. `project id →
  root path` is a function or it is nothing: with two roots answering one id every
  refusal keyed by a project id becomes ambiguous, and the ambiguity would surface at
  run start instead of at the act that caused it.
- **No remint operation exists.** A copied root cannot become a second live project by
  being handed a new id: its ledger records runs that project never performed, and
  there is no honest value to rewrite them to. The supported way to get a second
  project holding the same content is the one the two forms already give — create an
  empty project and take its catalog across by ADR 0007 export and import, which
  carries exactly the content and none of the history that would be a lie there.
  Project templates and forks are therefore not deferred by this record; they are
  decided against, and a record that wants them supersedes this paragraph rather than
  extending it.

### 7. Not decided here

Multi-user identity, roles and tenancy (#82) — this record is single-operator and
must not be read as an authorization model. Remote runners and per-project runner
placement (ADR 0009's successor, #9 part 3). Every column, version number and
migration mechanic (#16). The queue's own rules, priority and ready model (#79). The
platform adapter (#24) and the catalog's internals (ADR 0007). The sandbox mechanism
(#60) — named above as a hard predecessor and designed there, never here. The
project-runtime supervision contract named in decision 2, which owns failure
isolation and is the other hard predecessor of a second project.

## Refusals

| Name | Raised when | Boundary |
| --- | --- | --- |
| `project-unknown` | an operation names a project with no configured root | service |
| `project-root-conflict` | two configured roots are equal, or one contains another | composition |
| `project-id-collision` | two configured project roots hold stores carrying the same minted project id | composition |
| `project-confinement-unattested` | a second project root is opened on a deployment whose sandbox probe does not attest project confinement | project open |
| `cross-project-reference` | a document, binding, command or report names a revision, lineage, run, attempt or workspace outside its project | service |
| `credential-outside-project` | a credential reference is not named by the acting project's bundle | run start |
| `project-store-version-mismatch` | a project store is not exactly the running schema version | project open |
| `run-deletion-refused` | any request to delete a run, receipt, attempt, event or revision | store |
| `installation-ceiling-exceeded` | a start would exceed the installation-wide attempt or attempt-second ceiling | run start |

Durable failure tokens, where any of these must become one, are minted by #16.

## Consequences

- Deletion becomes answerable for the first time, and it is provable rather than
  argued. That is what unblocks connecting a foreign repository at all.
- Placement is structural instead of predicate-enforced: one project's bytes have one
  home, and a credential resolves only out of the store of the project asking, rather
  than out of a query that remembers a filter.
- Confinement is not structural, and this record buys a predecessor instead of a
  claim. Until #60's probe attests it, a second project would share a filesystem with
  the first under one OS user — precisely the point at which ADR 0009 ends its
  same-UID acceptance. Waiting is the honest cost of decision 2.
- Failure isolation is bought but not proved here. That an active project costs a
  process is what makes one project's crash survivable, and what makes it *provable*
  is the supervision contract this record defers — so the process cost is paid before
  its benefit is demonstrable.
- An active project costs a process. The service becomes a supervisor of project
  runtimes, which is real work that did not exist before and has no owner yet.
- No cross-project SQL exists. Every cross-project view is N reads and a merge, and it
  depends on ADR 0007's accepted store-stable lineage and measurement identities,
  which are not implemented yet.
- Per-project growth is unbounded and the only lever is coarse. An operator who wants
  one item's text gone must delete its project.
- The atelier gains no undo. A destroyed project is gone and the operator's own backup
  is the only recovery — which is precisely what makes "remove this project"
  answerable at all.
- Nothing about a project is portable between installations except its catalog. An
  operator changing machines moves roots; he does not export projects.

## Required proofs before implementation is accepted

- After a full run in project A — intake, publication, run, receipt, workspace — no
  byte of A's content exists outside A's root, proved by a canary read of the whole
  installation (the shape #58 acceptance 8 already uses).
- Destroying A's root leaves B fully operable, and every operation naming A refuses
  `project-unknown` with nothing written.
- A document, binding, command or report naming another project's revision, lineage,
  run, attempt or workspace refuses whole and changes nothing durable.
- A credential reference outside the acting project's bundle refuses before any
  provider process, and a full durable and API projection contains no credential value.
- **Confinement, observed live, before a second project is connected**: an attempt
  running in A is denied a read of an absolute path inside B's root *and* a read of
  the credential directory, by the sandbox policy on the host that will run both,
  while the same attempt's out-of-project credential reference refuses at run start.
  Both halves are required — the service refusal alone is not confinement, and a
  sandbox alone is not resolution — and a deployment that cannot produce this proof
  refuses its second project root (`project-confinement-unattested`).
- A project store at the wrong schema version refuses that project only; the other
  projects open, run and terminate unaffected.
- A deletion request against any run, receipt, attempt, event or revision refuses, and
  the schema fingerprint proves every `no_delete` trigger still present.
- The same definition taken into two projects yields one lineage id in both stores, and
  a cross-project read of one run's measurements counts it once.
- A catalog export taken from A and imported into an empty project B reproduces every
  revision byte-identically and contains no credential value, no bundle revision and
  no attempt, receipt or event row — checked against the whole export, not a sampled
  field.
- A root copied and both copies configured refuses `project-id-collision` before
  either store is opened; the same root moved and reconfigured opens with its id and
  its references unchanged.
- A project that never spoke about a source inherits an installation default added
  afterwards; a project that deselected one still does not have it; and no
  installation-level change alters a value a project set.
- The installation ceiling refuses a start in attempts and attempt-seconds; no sum of
  tokens across projects or meter revisions exists anywhere in the implementation.

**What this list deliberately does not prove.** Failure isolation — that A's runtime
process dying leaves B running and terminating its own receipts — is the supervision
contract's proof, not this record's. The root-destruction proof above is a
*destruction* proof and must not be read as a crash proof. That contract is a hard
predecessor of a
second project on the same footing as the confinement proof, and it carries that
sentence when it is written.

## Out of scope and stop conditions

Stop implementation on: a project column added to any product table; one process
opening two canonical stores; a shared store for credentials, catalog rows,
measurements or workspaces; any run-, receipt-, event- or revision-level delete path
in the runtime; a project's data written outside its root; a token cap summed across
projects or meter revisions; a second trust boundary minted between the coordinating
service and a project runtime; an export form beside the catalog export and the
stopped-root backup of decision 6; a credential reference or tracker connection
present in any export; a project id reminted, or a copied root configured as a second
live project; an installation-level default overriding a value a project set; or a
second project connected before both hard predecessors are met — the sandbox probe
attesting confinement with the live A→B denial observed, and the project-runtime
supervision contract.

## Supersedes

None.
