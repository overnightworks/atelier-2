# ADR 0010: One GitHub adapter observes, publishes and reads back; the core stays platform-blind

- Status: PROPOSED 2026-08-15 — decision only, most of this record unimplemented;
  §7's client choice ACCEPTED 2026-08-23 (operator ruling on issue #430) —
  `githubkit`, measured against the `open-pr` operation and recorded in §7;
  decisions 1 and 5 amended 2026-08-25 (`push-atelier-commit`, operator ruling
  on issue #642, gated REVISE then folded per that review) — decision only,
  unimplemented; decision 1 amended 2026-08-26 (`snapshot()` joins the
  observation port, head ruling on issue #732) — **built with its amendment**,
  in the change that adds this record's second reading operation (issue #712);
  decision 5's commit-identity rationale amended 2026-08-30 (operator ruling on
  issue #883, superseding the ruling on issue #32) — what the declared identity
  says, the rule that it is declared unchanged; decision 5's authoritative-negative
  rule amended 2026-09-05 (head ruling, usage evidence pass 9, issue #1210) — an
  `ls-remote` and a `pulls?head=…` search with nothing to read for the exact
  expected ref/head are authoritative absence before send; the post-send
  readback rule is unchanged, and which of the two a read is the intent's
  durable owner names (`ReadbackPhase`), never the adapter
- Date: 2026-08-15
- Requirement authority: [Issue #1](https://github.com/FlexOr2/atelier-2/issues/1),
  story 4, whose "GitHub landet einen nativen Flow" and provider/secret rules this
  record expresses and never re-decides — body read under decision 5's canonical
  rule, 25337 bytes, no trailing newline,
  `db17a54944498ec4e5f8c456b44771b8e1b9301b9b622d47ca11b1a830cf2b66`
- Decision authority: [Issue #24](https://github.com/FlexOr2/atelier-2/issues/24),
  SHA-256 over the exact served UTF-8 body bytes with nothing appended — 519 bytes,
  no trailing newline —
  `0104406ed3ecbee20caba9be436defd79b0e932e122075012b162dd44ca14087`, which poses
  the open decisions; the auth-method question is ruled by the operator in
  [comment 5302051551](https://github.com/FlexOr2/atelier-2/pull/81#issuecomment-5302051551),
  and the three token-method consequences of decisions 5 and 6 by the panel ruling
  in [#1 comment 5302114585](https://github.com/FlexOr2/atelier-2/issues/1#issuecomment-5302114585)
- Depends on: [ADR 0006](0006-node-vocabulary.md) (the adapter-operation contract,
  the core-derived idempotency key and the `external_effects` attestation this
  record fills in, and which routed every platform specific here),
  [ADR 0009](0009-runner-trust.md) (secrets by reference, the typed actor, the
  declared-exposure rule this record's observation choice follows) — on `main`
  via [PR #78](https://github.com/FlexOr2/atelier-2/pull/78), so this record's
  acceptance precondition is met,
  [ADR 0005](0005-enforced-package-boundaries.md) (the boundary gate that keeps
  the client inside the adapter)
- Feeds: [#79](https://github.com/FlexOr2/atelier-2/issues/79) (the queue, whose
  transport this is), [#8](https://github.com/FlexOr2/atelier-2/issues/8)
  (platform events as the hard measurements),
  [#7](https://github.com/FlexOr2/atelier-2/issues/7) (a readable actor)
- Names, never decides, the dependencies owned elsewhere:
  [#79](https://github.com/FlexOr2/atelier-2/issues/79) (queue, priority, ready
  model, automation filter), [#7](https://github.com/FlexOr2/atelier-2/issues/7)
  (conductor assignment), [#23](https://github.com/FlexOr2/atelier-2/issues/23)
  (multi-project isolation), [#16](https://github.com/FlexOr2/atelier-2/issues/16)
  (durable failure vocabulary), [#22](https://github.com/FlexOr2/atelier-2/issues/22)
  (catalog identity), `docs/requirements/README.md` (the requirement revision
  store and trace format)

## Context

Issue #1 story 4 wants one readable chain — requirement, bound head and tree,
pull request, check, review, native squash merge — with no Atelier-built landing
ceremony. Nothing owns how Atelier reaches GitHub: no auth mode, no observation,
no publication path, no readback contract.

The gap already costs. Every one of this repository's issues and every comment on
them, human and agent alike, is authored by `FlexOr2`, the operator's own account,
because the fleet publishes through the operator's `gh` credential and signs its
work in prose. That is not cosmetic: PR #72 was merged on a review verdict the
reviewing instance later disclaimed as not its own, and nothing in the platform
record could have separated the two. #7 needs an actor, #8's fourth anti-gaming
rule needs to tell an operator intervention from an agent's own run, and #79's
automation filter needs to know which items agents may take. All three read the
same missing fact.

Who Atelier acts as, however, is the operator's call rather than an architectural
one, and the operator ruled it on 2026-08-15: they configure the method when they
connect a project. So this record specifies both methods, states plainly what each
one can and cannot prove, and keeps the recognizability the finding above demands
in a place neither method can lose — the content Atelier writes and the receipts
Atelier keeps.

Requirement revisions are hand-rolled the same way: an agent copies an issue body,
computes its SHA-256 and pastes the digest into prose — issue #4's "Requirement
binding" block and this record's own decision-authority line above are both that
convention.

Two owners already exist and must not be duplicated. `contracts.effects` owns the
intent, readback and receipt discipline with its three outcomes, and
`ports.effects` owns the `EffectAdapter` protocol behind them. ADR 0006 owns the
Action node, derives the idempotency key in the core, enumerates proven adapter
operations under `external_effects`, and states outright that no platform
identifier belongs in the core because addressing, authorization and readback are
#24's. Observation has no owner at all.

One constraint is decisive for observation: under ADR 0009 §3 a deployment declares
its exposure rather than having it inferred from a bind address, and this one
declares `this-machine` — nothing outside the machine session may reach the API, so
there is no reachable inbound URL today.

## Decision

### 1. One adapter, three surfaces, and a platform-blind core

GitHub enters through exactly one adapter package with three surfaces:
**observation** (read), **publication** (perform an effect), **readback** (prove
what an effect did). No GitHub identifier — repository, issue, pull request,
label, check, review, installation — appears in `contracts`, `ports`,
`application`, `api` or `host`. The client library and every GitHub concept stay
inside the adapter under the same forbidden-import contract shape ADR 0005
already enforces for DBOS and SQLAlchemy; ADR 0005 owns that gate.

Publication and readback open **no new port**. The GitHub adapter is an
`EffectAdapterFactory`: its `EffectDestination` is the platform, its
`AdapterOperationalIdentity` is the exact repository and project connection a
durable effect targets, and its `readback`/`execute` are the ones `ports.effects`
already declares. Reusing that owner is what makes GitLab or a board backend a new
adapter and a new operation registry rather than a core change.

Observation opens **one** new port, the only one this record opens: a platform
observation source that takes a durable cursor and returns provider-neutral typed
observed facts plus the next cursor. Its facts name platform-neutral things — an
external item, its state, its labels, its actor, the commit an evidence object
speaks about — so a second platform implements the same port.

**2026-08-25 amendment (Operator-Ruling, #642-Journal): a forge-neutral
operation opens a second adapter package under the same port.**
`push-atelier-commit` (decision 5) is not a GitHub operation: its transport is
Git itself, never GitHub's Contents API, so it is composed from a separate,
forge-neutral adapter package (`atelier2.adapters.git_transport.**`) rather
than `atelier2.adapters.github.**`, reusing the same `EffectAdapterFactory` and
`ports.effects` this decision already names — exactly the "new adapter, no core
change" shape the paragraph above states for GitLab, now exercised by a
same-repository operation instead of a second platform. **The marker contract
decision 5 names moves with it**: because the marker's syntax and trailer key
must be identical whether the operation writing it is GitHub-specific or
forge-neutral, its one owner becomes a shared, provider-neutral contract module
neither package duplicates, and the GitHub adapter's existing copy is deleted
rather than kept as a second one. This amendment authorizes that split; it does
not build it, and it does not decide
[#728](https://github.com/FlexOr2/atelier-2/issues/728)'s proposed
`PlatformAdapter` port. The direction is compatible — a named per-package
capability surface behind one port — but #728 remains its own open question,
and this record neither adopts nor rules on its `capabilities()`/`snapshot()`
shape.

**2026-08-26 amendment (Head-Ruling, #732-Journal): the observation port grows
`snapshot()`, and grows nothing else.**
[#728](https://github.com/FlexOr2/atelier-2/issues/728) proposed widening this
record's one port in a single step into a five-operation `PlatformAdapter` —
`capabilities()`, `snapshot()` and `reference_grammar()` beside the two reads.
That is refused as a shape decided ahead of the callers that would prove it.
The rule this amendment sets instead, and the only general thing it decides:
**a read operation joins this port together with the first caller that needs
it, in the same change as the amendment naming it.** Under that rule the port
holds two reads and no catalogue — `open_items()`, which answers which items
the tracker holds, and `snapshot()`, which answers what one named item said.

**A snapshot is decision 5's observed revision, generalised — not a second
snapshot idea beside it.** Decision 5 already rules how a platform object
becomes reproducible material: the exact served UTF-8 bytes, hashed as they are
with nothing appended, carrying the object identity and the read's change
marker as provenance. That rule was written for the requirement issue and
nothing in it is specific to one kind of item, so `snapshot()` answers exactly
that revision for whatever work item the connected tracker holds. The core
keeps holding orchestration state by reference (REQ-QUEUE-14) and gains the one
thing a reference cannot carry: the bytes a run actually read, pinned, so a
later read of a moving object is never mistaken for them.

**Two kinds, and no platform noun in either.** A work item is an `issue` or a
`change_request`; a GitHub pull request and a GitLab merge request are the same
kind, and the mapping is the adapter's, exactly as the reference spelling
already is. The listing keeps refusing pull requests, because which items the
queue observes is #79's question; reading one by name is a different question
and answers the neutral kind.

**The caller of `snapshot()` is the start, and it pins what it read.** A start
door accepts an order that names an item instead of carrying bytes; the start
reads that item once and stores the observed revision as the order's own value,
which is why the reading is here rather than in a workflow node. Four
consequences are decided with it.

The value's schema is **not the author's choice**: a graph input carrying a work
item pins the exact published revision of the one document Atelier owns for it —
the neutral kind, the bytes, the digest, the change marker, the read time — and a
document pinning anything else, a permissive shape above all, refuses the start.
Without that rule a "work item" would mean whatever some schema happened to
admit.

The read happens **before any durable row exists**, so a start that cannot read
the item writes nothing and answers why.

**The durable answer comes before the read.** A start naming a work item asks
the store first, carrying the item's name rather than its bytes: a run of that
identity that already exists is answered from what it pinned, and only when
there is nothing to answer from does the start read the item and ask again. So a
retry neither re-reads a moving object nor turns an unreachable tracker into a
failed retry — and because the identity of a work-item order is the item it
names rather than the bytes of one read, a race between two starts of the same
run resolves to one run rather than to a conflict. Material beside it is
answered the same way without a read: an artifact resolves inside the store.
The reading stays outside the store's transaction, which is the other reason it
cannot be a single ask.

**That identity is only as honest as the row it is read from**, so a stored
value under this schema is verified before it decides anything: it must hash to
the hash stored beside it and be the complete document the one writer of that
row produces — the same completeness the start demands before writing one.
Neither can fail for a row this product wrote, so a failure is durable state
that lies, and the start stops on it rather than deciding "same run" from bytes
nobody can vouch for.

And a run's material is what the platform said *at that moment*: two starts
naming the same item across an edit are two runs with two different values,
which is the reproducibility the reference alone could never give.

**What this amendment leaves undecided, deliberately.** `capabilities()` and
`reference_grammar()` stay unruled until a caller lands — this record still
neither adopts nor refuses them as a design. A snapshot carries no title, no
item state and no linked items, because none of them has a caller yet, and no
discussion and no diff, because those are unbounded and belong in an artifact
addressed by hash rather than in a second byte budget inside an order value.
An item whose body exceeds the inline order bound is refused by that bound like
any other oversized value; the artifact path is what a later slice gives it.
A reference that is not in the composed adapter's own grammar earns no outcome
of its own here: it addresses no item in the connected tracker, which is what
the caller is told. And nothing here publishes the house schema for an
operator: a project that wants work-item orders publishes that document as a
schema revision like any other, and seeding it at serve time is a later
decision with its own owner.

### 2. Auth: the operator chooses the method when a project is connected

**The authentication method is the operator's choice at project-connect time, not
this record's mandate** (operator ruling on #24, 2026-08-15). V1 carries **both**
methods, each fully specified here — **with one dated, named exception:
`push-atelier-commit` is PAT-only until a future amendment designs its App
installation-token handoff (2026-08-25 amendment, #642-Journal; decision 5)** —
and a connected project binds exactly one of them as a published configuration
revision under #1's live-configuration rule.

- **Personal access token — the low-friction path, and V1 must support it.** The
  operator pastes a token when connecting the project and is done. A token scoped
  to the connected repository is the recommended form within this path, because
  narrowing the scope costs the operator nothing at the moment they paste it.
- **GitHub App — the second method, recommended, never forced.** It buys an actor
  identity of its own, per-repository and per-permission scope, short-lived
  installation tokens minted from a private key, and the identity a signed webhook
  delivery and a multi-user successor would need.

| | Personal access token | GitHub App |
| --- | --- | --- |
| Setup | paste a token when connecting the project | create the App, install it, place its key |
| Credential held | the token itself, long-lived until the operator rotates it | a private key, from which short-lived installation tokens are minted |
| Scope | the granting user's reach, narrowable per repository and permission | per installation, per repository, per permission |
| Actor the platform records | the granting user — the operator | the App's own actor, distinct from every human |
| How an Atelier action is recognizable | on the platform, only where the operation has a content slot for a marker (decision 5); everywhere else, only from Atelier's own receipts | by the account that acted, on every operation, plus the same marker |
| Multi-user successor | none: the connection is one human's reach | user-to-server tokens per operator under the same App |
| Revoking one connection | rotate the token: everything else that token drives breaks with it | uninstall or suspend that installation alone; other installations are untouched |
| If the held credential leaks | the granting user's reach, until the operator rotates it | **wider, not narrower**: the private key authenticates the App itself, so it can mint an installation token for *every* installation that trusts that App. Recovery is a key rotation across the App, not a local revocation |

**Atelier's own receipts are where actor truth lives, in both methods.** The
authoritative answer to "did Atelier do this, under which run and node" is
Atelier's receipt ledger, which carries the typed actor of ADR 0009 §9 whatever the
platform recorded. Where an operation has a content slot, Atelier additionally
writes a marker so a human reading the platform sees the same thing (decision 5
enumerates the slots, because not every operation has one). The platform's actor
field is therefore corroboration in the App method and, in the token method, no
evidence about agency at all.

**The honest weakening this record names rather than forbids:** in the token method
the platform records the operator for every operation, so on the platform side an
agent's action and the operator's own hand differ only by a marker that any holder
of the account could also write — honest labeling, not proof — and for contentless
operations not even by that. Three consequences follow, and none blocks the method:

- the account-level separation #8's fourth anti-gaming rule and #79's automation
  filter would like exists only in the App method; in the token method those owners
  read Atelier's receipts for what Atelier did, and the content marker as a *claim*
  with its provenance rather than as platform-proven attribution (decision 6);
- **a label is never a write operation Atelier performs for authorization
  purposes.** Labels are an observed input to #79's filter, never an authorization
  Atelier grants itself; no adapter operation writes a label that any authorization
  or automation filter reads. Without this rule the token method has an obvious
  loop — Atelier labels an item as permitted and the platform records the operator
  as having permitted it — and closing that loop is cheaper than detecting it;
- **merge and close are bounded in the operation registry, never as adapter-wide
  powers.** Which state-changing operations exist, what each may transition and
  what its readback proves are declared per adapter-operation revision under ADR
  0006, so "Atelier can merge" is never a general truth about the connection: it is
  true of exactly the published operation revisions the run binds and its
  `external_effects` attestation covers.

That is the tradeoff the operator is choosing, stated where they can see it.

- **Connecting a project is an explicit operator act**, with a durable record
  binding the project, the chosen method, the reference to its credential, the
  repository scope, the connecting actor, and — in the App method — the App
  identity and the installation identity the credential is used against, so an
  incident starts from named identities rather than from a search. The record holds
  those identities and never the credential, and it is a local subset of what the
  App's key reaches, never the inventory of it (decision 3). It is the same shape as ADR
  0009 §4's runner enrolment, and deliberately not a second ceremony. An unconnected project
  performs no operation and yields no observation
  (`project-source-not-connected`); a revoked connection likewise
  (`project-source-not-connected`).
- **Scope is requested per named operation**, least privilege in both methods: read
  access for the objects observation names, write access only for the objects a
  published Action operation revision creates or updates. A permission with no
  operation behind it is not requested, and in the PAT path this is a
  recommendation the connect surface presents rather than a rule the platform can
  enforce for the operator.
- **The multi-user successor exists only on the App path** — user-to-server tokens
  per operator, so each human acts as themselves while agent actions keep the App
  identity. A PAT-connected project is single-operator by construction. V1 builds
  neither.
- **An ambient credential is never an auth method.** The adapter resolves the
  reference its project connection names, and never a token that merely happens to
  be in the environment or in a CLI's configuration — including the `gh` credential
  the bootstrap harness uses today. Connecting is a decision, not an inheritance.

### 3. Credentials reach the adapter by reference, never by value

ADR 0009 §6 holds here unchanged and **identically for both methods**; this record
only names what the references are.

- The durable secret is the connection's credential — the **token** in the PAT
  method, the App **private key** in the App method. The adapter's host resolves it
  from a credential reference at composition, exactly as
  `ClaudeSubscriptionSettings.credential_directory` already does for the Claude
  adapter. Neither ever appears in a workflow document, prompt, context package,
  event, receipt, log, database row, API resource, crash evidence or test fixture.
  The operator pastes a token into the credential channel, never into a project
  record, an issue, or a workflow.
- **Installation tokens are derived, never stored.** In the App method they are
  minted in memory, refreshed by the client, and persisted nowhere. A durable store
  of an installation token is refused as a design, because it converts a
  short-lived credential into a long-lived one and gives the leak a place to live.
- **A long-lived token is the PAT method's accepted cost**, and it is bounded where
  Atelier can bound it: the credential channel holds it, rotation and expiry stay
  the operator's, and revoking it is one operator act on the platform. Atelier
  neither copies it nor extends its life.
- **The App method's long-lived secret is the private key, and its blast radius is
  the App, not the installation.** Disconnecting or suspending one installation
  stops that connection; it does nothing about a leaked key, which can still mint
  an installation token wherever that App is installed. Recovery is therefore a key
  rotation at the App. Neither method's credential is stored, logged or projected,
  so this is a recovery contract, not a second secret rule.
- **The affected set is asked of the platform, never inferred from Atelier's own
  records.** Two rules make that answerable. Atelier uses a **dedicated App**, one
  the deployment owns and shares with nothing else, so the key's reach and the
  product's reach are the same set by construction. And at rotation the authoritative
  inventory is **the platform's own list of that App's installations**, not the
  connection records: a connection record is a *local subset* — the installations
  Atelier was told to use — and an incident must assume the key reached every
  installation trusting the App, including ones no connection record names. Anything
  else inventories the attacker's target from the victim's notes.
- A bound credential reference that does not resolve refuses at run start
  (`platform-credential-unresolvable`), with no fallback to another auth mode.
- **Raw platform responses do not land durably.** ADR 0006 already has the rule:
  the operation revision declares its typed readback projection and the core
  carries that projection as an opaque hashed payload. The adapter never writes a
  raw provider frame into a receipt, per #1's frame rule.

### 4. Item observation: polling in V1, a webhook as a hint later

**V1 polls.** The reason is not effort: this deployment declares `this-machine`
exposure under ADR 0009 §3, and under that declaration nothing outside the machine
session may reach the API, so there is no address a delivery could reach.
Webhooks are unreachable today, and a decision record that chose them would be
choosing an architecture the deployment cannot run.

- **The interval is configuration, not a constant this record mints.** It is
  live-versioned per project under #1's configuration rule, and the need that
  bounds it is the queue's ready latency — so #79 sets it with its own acceptance,
  where the need is stated.
- **A durable observation cursor per observed collection** makes restart honest: a
  restart neither re-observes from the beginning nor skips. Observation is
  at-least-once, and an observed fact is identified by the platform object and its
  own revision marker, so a repeated observation is the same fact rather than a
  second one.
- **Conditional reads.** The adapter uses the platform's own change markers —
  entity tags and update cursors — so a poll that learns nothing costs nothing it
  does not have to.
- **Rate limiting is visible, never silent.** Exhaustion degrades observation with
  a named projected reason and loses no cursor
  (`platform-observation-rate-limited`). It never fabricates an absence and never
  quietly stops.
- **The successor is a hint, not a second truth.** When ADR 0009's remote tier and
  authenticated bind exist, a webhook delivery is accepted only after signature
  verification and only as a *hint*: the adapter reads the object back before
  anything durable is written. Truth therefore stays on the one readback path,
  which is also why missed, duplicated or replayed deliveries need no replay
  machinery.
- **Observation starts nothing.** It produces observed facts. Which item becomes
  ready, and what runs, is #79's and the scheduler's.

### 5. Publication: only through an Action node, and always readback-then-create

- **Every create or update on the platform is an Action node** bound to a versioned
  adapter-operation revision (ADR 0006) — the GitHub adapter's for a GitHub-facing
  operation, and, since decision 1's 2026-08-25 amendment, the forge-neutral
  git-transport adapter's for `push-atelier-commit`. An agent shell reaching `gh`
  is not a publication path. This is the line the fleet crosses today, and it is
  enforced by the boundary rather than by instructions, per #1's rule that prompts
  are not controls.
- **The core derives the idempotency key** (ADR 0006: run, node, operation revision
  and intent hash); an author never writes one. Neither GitHub's API nor a bare git
  push offers an idempotency key of its own, so where an operation creates a
  content-bearing object the adapter **carries the effect's request hash in that
  object's own content**, and `execute` is always readback-then-create. The marker
  is what lets a re-attempt after a crash find the first effect instead of creating
  its twin, and it is the same marker that carries decision 2's attribution.

**Where the marker lives, per operation — enumerated, never assumed.** Attribution
and deduplication are properties of an operation, not of the adapter, so each
published operation revision declares which row it is:

| Operation kind | Marker slot | Deduplication | Attribution on the platform |
| --- | --- | --- | --- |
| Create an issue, a pull request, or a comment | the object's own body, which Atelier authors in full | readback by marker | the marker, plus the account in the App method |
| Push a commit Atelier authors | a commit trailer, plus the commit's author and committer identity — both owned by the operation revision, see below | readback by trailer | the trailer and that identity |
| Edit an object Atelier itself created | the same body slot, rewritten whole | the target's content hash | unchanged from its creation |
| Write into a body a human owns — a requirement issue above all | **none: Atelier does not write it** | not applicable | not applicable |
| A contentless change: close, reopen, merge, apply a label, request a review | **none exists** | the target's own state, read back by id | **only Atelier's receipts.** In the token method the platform attributes it to the operator, and this record says so rather than implying otherwise |

Three rules fall out and are binding. **A human-owned body is never mutated to
carry a marker**, because a requirement issue is #1's editable source of truth and
a marker written into it corrupts exactly the bytes the revision hashes. **A
companion comment is never used to mark a contentless change**, because it is a
second effect with its own failure window and would have to be reconciled against
the first. And **an operation whose declared row is "none exists" is honest about
it**: its receipt carries the attribution, no marker is claimed, and no consumer is
told the platform proved something it did not.

**The marker syntax and the commit identity have an owner here, because nothing
else in this repository owns them.** Repository policy binds no trailer key and no
per-invocation commit identity, so a commit operation would otherwise inherit
whatever ambient git configuration the host happens to carry — which is how a
commit ends up wearing an identity nobody chose. Therefore: **one marker contract
owns the marker's exact syntax and its trailer key.** Since decision 1's
2026-08-25 amendment its owner is a shared, provider-neutral contract module,
not the GitHub adapter alone — the GitHub adapter's `marker.py` is that
contract's GitHub implementation, not a second copy of it — shared by
every operation revision so two operations cannot drift into two dialects, and
**each commit operation revision declares the author and committer identity its
commits carry**, per invocation and never read from ambient git configuration.
Both are proven with the operation revision: a commit produced by that operation
carries exactly the declared identity and trailer, and a run with a differently
configured host produces the same two. Should repository-wide authorship policy
later acquire an owner, this record defers to it rather than keeping a second copy.

**Amendment 2026-08-30 (operator ruling, issue #883): what that declared identity
says.** That policy now has an owner, and this record defers to it as promised. An
Atelier commit carries the **operator as author** and the **model of the node that
performed the push as committer** — `Claude Opus 5`, `grok-4.6`, whichever held the
`push-atelier-commit` grant for that run — with both addresses derived from the
GitHub account the project is connected with, never from the model's provider. This
supersedes the ruling on [#32](https://github.com/FlexOr2/atelier-2/issues/32), that
an agent's commit must never claim to be the operator.

The two are not in conflict once git's two identity fields are held apart, which #32
did not do: it saw one field wearing the wrong name. **Author** names whose change it
is — the operator's intent, his authorization, his accountability. **Committer** names
what produced and placed the bytes — the model. #32's concern was an agent passing
itself off as a human, and this ruling honours that concern rather than overriding it:
the committer line names the machine in every commit, unmissably. The sentence above
keeps its full force — the identity is declared, never inherited from the host.

The derivation is a default, not a fixture: each derived part is overridable where
models are already maintained. A published `push-atelier-commit` revision still
declares one literal author and committer pair, so a receipt continues to prove which
identity was authorized for that commit — which means one such revision per model,
because this contract admits concrete identities and no derivation rule.

**2026-08-25 amendment (Operator-Ruling, #642-Journal): `push-atelier-commit`
names the git-transport push operation.** The independent architecture ruling
on issue #642
([comment 5400317551](https://github.com/FlexOr2/atelier-2/issues/642#issuecomment-5400317551))
decides `push-atelier-commit` as its own versioned adapter operation, transported
over forge-neutral Git rather than the GitHub Contents API, and this amendment is
the decision-5 row that ruling requires so a builder starts from this record
rather than the issue journal. It fills the marker table's existing "Push a
commit Atelier authors" row with a name: the marker slot is the commit trailer
plus the author and committer identity the operation revision declares, exactly
as the general commit rule above already requires, never read from ambient git
configuration.

**The push is create-only, never an update.** Every send names its target
branch ref's expected old value as absent, in Git's own compare-and-swap form
(the zero OID as the ref's prior value), and `execute` never updates or
force-pushes a ref that already names a commit — a second commit on an
existing Atelier branch is a second Action node's own push, never a rewrite of
this one's. A non-fast-forward or lease rejection from the remote is not a
refusal in its own right: it is exactly the trigger that sends the intent to
readback, never to a second send — **and no retry happens inside this
operation**, whatever readback then finds. Readback reads one of two ways.
The ref may be **present and name this push's own commit** — the earlier
attempt's own race — which resolves to a receipt, not a second send. Or the
ref may be **absent, or present and name a different commit**: both are
`UNKNOWN`, and both are treated exactly alike by the no-resend rule below —
**the ambiguous-retry rule is unconditional, so an absent ref after a
rejection is never distinguished from a genuine divergence and never
auto-retried as if it were a fresh intent.** A present-but-different ref
resolves through `platform-push-ref-diverged`; an absent ref resolves through
the ordinary "Create a content-bearing object" row below, whose unmatched scan
is already `UNKNOWN` by that row's own rule. Either way `UNKNOWN` advances to
`WAITING_RECONCILIATION`, and a fresh attempt is the operator's reconciliation
decision, never the adapter's own choice to resend. **A divergence carries no
typed sub-reason beyond `UNKNOWN`**, because this operation owns no evidence
at its boundary that would let it tell one divergent cause from another; an
owner that later proves a distinguishing reason may add one, but this
amendment does not invent one on the chance it might be useful.

**Its authoritative negative is `none`, unconditionally** — not
only by default but by declaration, because the object it addresses is a ref
under Atelier's own future control: a ref this operation expects and does not
find is exactly as consistent with "never pushed" as with "pushed, then deleted
or force-updated", so readback never returns `AUTHORITATIVE_NOT_FOUND` for it and
`proves_absence` stays `False` for the operation, matching the general
"Create a content-bearing object" row of the table below rather than opening a
second one.

**2026-09-05 amendment (head ruling, usage evidence pass 9, issue #1210):
overturned for the pre-send check that decides whether to send at all.** Live
pass 9 produced two `WAITING_RECONCILIATION` results in one run where the
remote held nothing at all — not the divergence this decision's `UNKNOWN`
rule protects against, but an ordinary first attempt reading its own read as
unresolved. A `git ls-remote` that exits 0 with no line for exactly the
expected ref, taken *before* any send, is now authoritative absence: the
operation proceeds to send rather than routing to reconciliation. This does
not touch the paragraph above, which still governs the *readback taken after a
send* — a rejected push's ref is still read as `UNKNOWN`, never as this
pre-send absence, because only the pre-send read has nothing else it could
mean. The zero-OID lease on the push itself, unchanged, remains the protection
against a double send: an authoritative pre-send absence only clears the
operation to attempt its one create-only, compare-and-swapped send.
**Named residual gap, accepted for this slice (head ruling, issue #1210):**
DBOS may replay `durable_effect`'s resolving step after a crash using the
`OBSERVE` step's already-memoized pre-send absence, and if the ref was removed
between the crash and the replay, the replayed `execute` finds absence again
and pushes once more — fenced only by the zero-OID lease admitting solely the
same deterministic commit (proven at the workflow level by
`tests/crash/test_git_transport_effect_recovery.py::test_a_replayed_resolve_after_ref_removal_pushes_the_same_commit_once_more`),
never a durable "send attempted" claim; closing it needs a schema hop on
`effect_intents` and is its own slice, serial after #1216.

**2026-09-05 amendment (head ruling, usage evidence pass 10, issue #1224): a
branch nobody reviews any more is replaced under a lease on what was read.**
This overturns "never an update" above for exactly one pre-send case, and
leaves it standing everywhere else. Every second `issue-to-pr` run on the same
work item found its item branch carrying the first run's commit, whose pull
request was closed, and read that foreign commit as the divergence above — so
each second run cost the operator a hand determination and a branch deletion.
The pre-send read now asks the tracker what stands on that branch
(`ports.effects.HeadBranchPullRequests`, answered for GitHub by the same
head-branch listing the amendment above already reads): with **no pull request
open** on it — every one closed or merged, or none at all — the branch carries
work nobody reviews, this request holds nothing there, and the send proceeds as
`--force-with-lease=<ref>:<the commit that was read>`; with **one open**, the
outcome stays `UNKNOWN` naming that pull request's number, and reconciliation
is unchanged. The lease is the whole fence: it names the exact value this
attempt observed, so a branch that moves between the read and the send is
refused by the remote rather than overwritten. The replaced oid is evidence for
an operator, not part of the receipt's identity — a structured log line records
it at the send, the same convention
`effect_unknown_outcome_entered_reconciliation` uses, so the same accepted push
produces byte-identical result bytes whether read from the send itself or
reconstructed by a crash readback that never observed what the branch stood at
before. A tracker that cannot answer licenses nothing. This changes only the
*pre-send* decision, exactly as the amendment above did: a foreign commit read
*after* a send is still the divergence this decision keeps `UNKNOWN`, because
there a branch someone else moved and one this send lost a race for read alike.

Which of the two a read is, the adapter cannot know and does not decide: a
process that died between its push and its receipt leaves the remote looking
exactly like one that was never asked. The durable owner of the intent names
it, as `ReadbackPhase` on the port's `readback`, and the effect contract
refuses an absence answered for `AFTER_SEND` outright. So the amendment holds
only where the phase says it may: the observing step of `durable_effect`,
which runs before that workflow's resolving step ever reaches an `execute` and
is replayed rather than read again after a recovery. The reconciliation path
reads `AFTER_SEND`, because an intent reaches that door only behind an unknown
outcome and an ambiguous send is one of the two ways to get one; there the
operator's own determination, never the read, licenses the execution.

**The credential handoff is normative, not left to whatever a subprocess call
happens to do.** The credential — the token in the PAT method — never reaches
the git subprocess as a literal value in its argument vector, per decision 3's
`/proc/<pid>/cmdline` rule, **and never as a literal value in that subprocess's
environment either**: `/proc/<pid>/environ` is exactly as readable a leak
surface as `argv` to a process sharing the host, so neither carries the token's
own bytes. The subprocess instead receives a **reference** in its constructed
environment — the same credential-file path decision 3 already resolves the
token from — and a git credential helper or `GIT_ASKPASS` script git itself
invokes reads that file and answers git's credential prompt; the pushing
process's own environment never holds the secret, only the path to it.
**Stated exactly: the git subprocess's environment may legitimately carry the
credential-file path — that path is a reference, not the secret — and must
never carry the credential's own bytes; the two are not the same claim, and
this amendment makes only the second one.** This amendment adds no second
credential rule: it states decision 3's existing
by-reference discipline precisely for the one transport in this record that
shells out to a subprocess instead of calling an HTTP client library directly.
**This amendment specifies `push-atelier-commit` for the PAT credential file
only.** The App method's installation-token handoff into a git subprocess is a
named gap, not decided here: minting, scoping and refreshing an installation
token for a `git push` invocation is a different shape from handing `githubkit`
a bearer value, and this record does not design it. Until a future amendment
does, `push-atelier-commit` is PAT-only and the App method stays `open-pr`-only
for this operation.

**Its request presupposes a candidate-tree invariant this record does not
own, named rather than left vague.** The operation's tree object is the
immutable, content-addressed candidate that ADR 0011 decision 2's project-owned
candidate store (`.atelier2-candidates.git`) holds, captured and anchored to
the pinned base commit before the attempt that produced it is allowed to reach
a terminal successful state — an invariant the attempt lifecycle enforces
(`application/execute_agent_attempt.py`'s success path), never this adapter.
This amendment only names the dependency because the operation's request
bytes — tree OID, base commit, branch, identity — do not exist without it.

- **What counts as an authoritative negative is declared per operation, and for a
  create there is none.** The three outcomes of `contracts.effects` are only as
  honest as the read behind them, so:

| Operation kind | Can readback prove `FOUND`? | Can it prove `AUTHORITATIVE_NOT_FOUND`? |
| --- | --- | --- |
| Create a content-bearing object | yes — the marker identifies it | **no.** The platform assigns the identity, so the request has no address to read; a listing is bounded and a search index is eventually consistent. An unmatched scan is `UNKNOWN` |
| A **reversible** state change on an object addressed by id — close, reopen, apply a label, edit, request a review | yes when the intended state is present: for a state-setting effect the intended state *is* the effect | **no by default.** A mismatch is equally explained by "never performed" and by "performed, then reversed or edited by a human", so it is `UNKNOWN` |
| A **monotonic** transition — merge above all | yes — the merged flag and the merge commit | yes. A merge cannot be undone into an unmerged pull request, so not-merged at read time does exclude a merge by this intent, provided the head the intent named is still the one read |

  A reversible operation may reach an authoritative negative only where it declares
  the evidence that proves non-performance: a precondition the platform enforces at
  write time, or an event identity on the target's own timeline that would exist if
  the effect had run. That declaration is part of the operation revision and is
  proven with it; absent one, the row above stands and the negative is `UNKNOWN`. An
  operation revision offering a negative its read cannot support is refused
  (`platform-absence-unprovable`), and an empty search never becomes an absence.
  This is where the third outcome earns its keep: `UNKNOWN` routes to the operator
  reconciliation command `contracts.effects` already owns.

  **2026-09-05 amendment (head ruling, usage evidence pass 9, issue #1210):
  overturned for the pre-send check that decides whether to send at all, for
  `open-pr`.** The same live pass 9 that overturned decision 5's push paragraph
  above hit its twin on this operation: a first attempt whose PR search found
  nothing read that as `UNKNOWN` and routed to reconciliation before ever
  sending. A `GET /repos/{owner}/{repo}/pulls?head=owner:branch&state=all`
  answering 200 with an empty list, taken *before* any send, is now
  authoritative absence for that exact head — the operation proceeds to open
  the pull request rather than routing to reconciliation. The row above is
  unchanged for readback taken *after* a send: an unmatched scan there is still
  `UNKNOWN`, because only the pre-send search has nothing else it could mean.
  GitHub's own head+base uniqueness constraint (422 on a second `POST`) remains
  the protection against a double send; an authoritative pre-send absence only
  clears the operation to attempt its one create, and a create that constraint
  refuses whose winner the listing still does not name reports `UNKNOWN` with
  what GitHub said rather than raising over a request that was sent.
  Authoritative here means exactly the answer the ruling names: HTTP `200`
  with an empty list, read across every page of the listing, with the marker
  deciding which of a branch's pull requests is this request's. Any other
  status, an unreadable body, or a branch naming only foreign pull requests is
  `UNKNOWN`. As for the push above, the phase is named by the intent's durable
  owner (`ReadbackPhase`), never decided by the adapter.
- **The ambiguous retry needs no new state, because a durable one already precedes
  the send.** `EffectIntentState.PREPARED` is written durably before any request
  leaves the adapter, so a crash between send and receipt always leaves a prepared
  intent with its exact request bytes; readback then resolves it to a receipt, an
  authoritative absence, or `UNKNOWN` — and `UNKNOWN` advances to
  `WAITING_RECONCILIATION` rather than being retried blind. **No operation is ever
  re-sent on an unresolved outcome**, whatever its kind: a create because its
  negative is unprovable, a reversible change because a mismatch may mean a human
  undid it and re-sending would overrule them. That is the case the operator
  resolves, and it is the honest price of a platform with no idempotency key.
- **An update is idempotent by content.** A target that already carries the intended
  content hash, or already stands in the intended state, is `FOUND`, not a second
  write.
- **Scope is bound.** An operation addressing an object outside the connected
  project's repository scope refuses (`platform-object-out-of-scope`), whichever
  method holds the credential; multi-project isolation stays #23's.
- **Requirement-revision publication stops being hand-rolled.** The adapter observes
  the requirement issue and publishes an immutable revision from the exact served
  UTF-8 body bytes and their SHA-256 — the same canonical rule the fleet applies by
  hand, computed once by the adapter instead of pasted into prose, and carrying the
  object identity and the read's change marker as provenance. **The canonical rule
  is exact: the bytes the API serves as the body, hashed as they are, with nothing
  appended** — no trailing newline, no re-encoding, no normalization. The issue stays
  the human's: publication is a read, Atelier never writes that body, and the bytes
  it hashes are the bytes a human last wrote. This record decides what is observed,
  what is hashed and what identity it carries; the durable revision store and the
  trace format stay with `docs/requirements/README.md`.

**One recipe, one owner, and the divergence named.** That precision is not pedantry,
it is the reason to mechanize: two operative recipes stand in the landed tree today,
and a hash a human types is a hash nobody re-derives. Ruled here so that no builder
and no distiller has to choose between them:

- **The owner stays `docs/requirements/README.md`.** It owns the issue-body citation
  convention, and this record does not take that ownership. What this record settles
  is the one rule the adapter will mechanize, which is therefore the rule a human
  should already be writing.
- **The canonical form is the exact served bytes**, reproduced with

  ```console
  $ gh api repos/FlexOr2/atelier-2/issues/82 --template '{{.body}}' | sha256sum
  ```

  A Go template writes the field and appends nothing. The recipe
  `docs/requirements/README.md` carried until this correction landed,
  `--jq '.body' | sha256sum`, digests the body plus the newline `gh`'s raw-string
  output adds — an identity over bytes the object does not contain, and one no
  reader can re-derive from the object alone. The newline cannot be suppressed
  inside `--jq`, which takes the filter as
  its only argument and rejects a `jq` flag; `gh api … | jq -j '.body'` is the
  equivalent two-tool form.
- **The divergence, exactly.** All three landed requirement citations were computed
  under the README's recipe and carry the appended newline: `#79 body @ 9d781a3c`,
  `#82 body @ fe6fd31f`, `#9 body @ 36800d6e`. Every decision-authority digest was
  computed under the exact rule: ADR 0008's `#26` (`69a3f021…`), this record's `#24`
  (`0104406e…`), and ADR 0009's `#21` (`5c03ceb1…`). The split runs between the two
  directories, not inside either.
- **Landed citations are provenance and are not rewritten.** Each records what its
  author read under the recipe its document named at the time, and each stays valid
  under that recipe. Restating a provenance record to match a later convention
  destroys the only thing it was for.
- **The correction was owed, and it was not made here.**
  `docs/requirements/README.md` now names the exact form as its recipe and records
  that citations predating that change carry the appended-newline form. That step
  belonged to the requirements-document owner and landed through
  [#93](https://github.com/FlexOr2/atelier-2/issues/93); this record does not edit
  another owner's convention from inside a decision.

### 6. Readback semantics: what merged, closed and labeled mean

One rule governs all of them, and it is ADR 0009 §5's rule rather than a second
one: **an observed platform fact is evidence** carrying its provenance — the object
identity, the exact observed fields, the read time and the platform's change
marker. Only the core writes a verdict.

- **`merged` is not `closed`.** Merged is the pull request's merged flag together
  with its merge commit. A closed-unmerged pull request is a distinct terminal fact
  and is never read as success. Reading these states is unconditional; *performing*
  a merge or a close is not, and stays bounded to the published operation revisions
  decision 2 names.
- **A landing receipt binds both objects.** Native squash merge is authoritative
  (#1), so the reviewed head and tree are not the landed commit. The receipt names
  the reviewed head and tree *and* the resulting merge commit, because #1 requires
  a receipt to say which object was checked and which commit landed — which is
  exactly what the fleet writes by hand in a PR body today.
- **`closed` on an item carries no reason alone.** The observed fact is the pair of
  state and state reason. What that pair means for a queue outcome is #79's.
- **`labeled` is an observed set at a read, and this record mints no label name.**
  The repository carries only the platform's default labels today; the automation
  filter's vocabulary belongs to #79, and inventing one here would be a constant
  with no named need. Labels are read in one direction only: per decision 2 no
  Atelier operation writes one that an authorization or filter reads, so a label
  the filter trusts was always set by someone other than Atelier — and in the token
  method, where the account cannot show that, Atelier's own receipts are what say
  so.
- **A check or a review is evidence about exactly one commit.** A check result or an
  approval observed against a different head is not evidence about this candidate,
  because an approval goes stale the moment a new commit is pushed.
- **Every observed action carries its actor, and how strongly it is proven.** In the
  App method the platform's own actor maps onto ADR 0009 §9's typed actor: the
  installation is an `agent`, recorded with the published revision it acted under,
  and a user is an `operator`. In the PAT method the platform records the operator
  for both, so the adapter reads decision 2's content marker as an `agent` **claim**
  and records it as such — a claim with its provenance, never platform-proven
  attribution. The observed fact therefore carries the actor *and* whether the
  account or only the content established it, so #8's fourth rule and #79's filter
  can decide what they trust instead of being told a claim is a proof. An action
  that maps to neither, and carries no marker, is recorded as unattributed and
  counted as neither (`platform-actor-unattributable`).
- **For #8 the adapter supplies facts, not metrics.** Merged or not, the check
  outcome per head, the review rounds, the platform timestamps between opening and
  merging. It computes no rate, no duration aggregate and no cost; token and cost
  truth stays with the receipts ADR 0008 owns, and the subscription mode reports
  consumption rather than money.

### 7. The client is chosen, not written

A hand-rolled client would own four jobs: authenticating both methods — a pasted
token, and an App assertion whose installation tokens must be minted and refreshed
— pagination, conditional requests and rate-limit headers, and webhook signature
verification. A maintained client owns them, so hand-rolling the REST client is
refused as a design.

**`githubkit` is the client** (operator ruling on issue #430, 2026-08-23):
typed models generated from GitHub's own OpenAPI description, both token and
App authentication strategies, and webhook verification in one library, on the
Pydantic this project already depends on; `PyGithub` was the alternative and is
not carried. The measurement below is from the `open-pr` operation's live
adapter (`atelier2.adapters.github.live_effects`), the first slice composed
against it.

**What `githubkit` deletes for this operation.** Typed request construction —
the create-pull-request and create-ref bodies are generated `TypedDict`s
(`ReposOwnerRepoPullsPostBodyType`, `ReposOwnerRepoGitRefsPostBodyType`)
this adapter fills in rather than hand-shaping JSON. An injectable transport —
`GitHubCore` accepts an `httpx.BaseTransport`, which is what lets this
operation's tests exercise the real request and error-handling path against an
in-memory server rather than the network (`tests/integration/test_github_open_pr_live.py`),
with no second test-only client to maintain. Retry — `auto_retry` is on by
default, so a transient failure is the library's job rather than a call site's
loop. TLS and connection handling — `httpx` owns both, and this adapter never
touches a socket. A typed exception — `RequestFailed` carries the response, which
is what lets `execute` tell "this branch ref already exists" (422, the earlier
attempt's own race) from every other creation failure without parsing status
codes and bodies by hand. Both the token and the App auth strategies exist in
the one library, so the second method (not composed by this slice) is a
configuration choice later rather than a second client to write.

**What this operation still owns, and one caching feature it explicitly
refuses.** This operation's marker placement in a pull request's own body, and
its readback-then-create idempotency decision (decision 5), are this adapter's
own choice, not the client's: `githubkit` has no notion of "the same logical
effect" — that is exactly the gap ADR 0010 exists to close. The marker's
syntax itself is the shared, provider-neutral contract decision 1's amendment
names, with this adapter's `marker.py` as that contract's GitHub
implementation, never a second copy of the decision. So are branch naming,
deriving a title from the predecessor agent's output, and the
credential-by-reference boundary (decision 3) — the adapter hands `githubkit`
a token resolved from a credential directory at `open()`, never a value from
anywhere else. `githubkit` bundles `hishel` for HTTP-level response caching by
default (`http_cache=True`); this operation turns it off, because a cached
"not found" answering a retry's search is exactly the twin the
readback-then-create rule exists to prevent — the library's caching applies
here and is a hazard for this specific read, not a feature this operation
declines out of caution. And this adapter deliberately does not consume
`githubkit`'s generated response models for `pulls.list`, `pulls.create` or
`repos.get_branch`: those models require every field GitHub's schema declares,
including a fully populated nested repository object neither this operation
nor its readback needs, so this adapter reads the two fields it actually acts
on (`number`, `body`) from the raw JSON response instead and fails loud on a
response shaped unlike what a real GitHub answer carries.

**(2026-08-27 increment, #162 B4) A documentation release is a distinct,
operator-authorized run, never the automatic successor of `revise`.** Its
version-2 `open-pr` request is closed: it names the base revision, candidate
digest, approved verdict digest, every path/current digest/replacement UTF-8
byte sequence, title, body and `draft: true`. The Action refuses every other
verdict or candidate-digest pairing, and recomputes that digest over the
canonical `base_revision`, `changes`, `title`, and `body` projection, before
either the candidate tree is pushed or a pull request is opened. The platform
adapter materializes those exact replacement bytes and delegates their commit
to the existing `push-atelier-commit` fence before `open-pr`; the request-hash
marker and deterministic push receipt remain the idempotency owners, so recovery
after either write converges rather than making a second commit or pull request.
The connected base branch must still name the request's exact base revision.
Credentials remain adapter-local references: neither the release order nor its
lease carries a token.

Whichever client is chosen stays inside the adapter under the boundary
contract of decision 1; `githubkit`'s own vocabulary — its models, its
exception types, `httpx` — does not appear outside
`atelier2.adapters.github.**`, the same gate ADR 0005 already enforces for
DBOS and SQLAlchemy.

## Refusals

| Name | Raised when | Boundary |
| --- | --- | --- |
| `project-source-not-connected` | an operation or observation names a project with no connection record | adapter composition |
| `project-source-not-connected` | the connection record was removed | adapter composition |
| `platform-credential-unresolvable` | the bound credential reference does not resolve on the adapter's host | run start |
| `platform-object-out-of-scope` | an operation addresses an object outside the connected repository scope | operation binding |
| `platform-marker-slot-unavailable` | an operation revision declares a marker slot the object kind it writes does not have | operation binding |
| `platform-absence-unprovable` | an operation declares an authoritative negative its read cannot support — a create, a reversible change with no declared non-performance evidence, or an absence derived from an eventually consistent search | operation binding, and again at readback |
| `platform-actor-unattributable` | an observed action maps to no known actor and carries no Atelier marker | observation |
| `platform-observation-rate-limited` | the platform's limit stops a poll; visible, cursor preserved | observation |
| `candidate-tree-unrepresentable` | `push-atelier-commit`'s source lease contains a symlink, a submodule, a nested `.git`, or a size the candidate store refuses (2026-08-25 amendment, #642-Journal) | candidate capture; the attempt ends FAILED, never SUCCEEDED with the capture skipped |
| `candidate-capture-unavailable` | the project's Git object boundary does not answer during candidate capture (2026-08-25 amendment, #642-Journal) | candidate capture; the attempt ends FAILED with this code in its receipt, its workspace **is** released (the `AgentAttemptPossiblyRan` rule of `application/execute_agent_attempt.py` wins over retaining it — a retained workspace is a leak, not a store), the receipt names the loss, and a retry re-runs the attempt |
| `candidate-tree-missing` | a `push-atelier-commit` intent finds no candidate-tree ref for its node execution, or one present but naming a tree other than the one its pin recorded (2026-08-25 amendment, #642-Journal) | operation binding |
| `candidate-base-not-on-remote` | `push-atelier-commit`'s pinned base commit is not reachable at the target remote, or the candidate's recorded base no longer matches the base commit the intent names (2026-08-25 amendment, #642-Journal) | execute |
| `platform-push-ref-diverged` | the target branch ref exists but names a commit other than the one this push's readback expects (2026-08-25 amendment, #642-Journal) | readback; resolves to `UNKNOWN`, never `AUTHORITATIVE_NOT_FOUND`, and carries no typed sub-reason for the divergence |
| `platform-push-authentication-invalid` | the remote rejects the resolved credential itself — expired, revoked, or out of scope (2026-08-25 amendment, #642-Journal) | execute |
| `platform-push-authorization-rejected` | the remote accepts the credential but refuses the push under its own policy — branch protection, a ruleset, a server-side hook (2026-08-25 amendment, #642-Journal) | execute |

Durable failure tokens, where any of these must become one, are minted by #16;
this record borrows that owner rather than opening a second vocabulary.

## Consequences

- The operator connects a project in the way they choose, and the low-friction path
  is a first-class one rather than a concession. Neither method is a different
  product: the same operations, receipts, refusals and secret rules hold on both,
  **except `push-atelier-commit`, PAT-only until its App handoff is designed
  (2026-08-25 amendment, #642-Journal)**.
- An Atelier action stops being recognizable only by a prose signature. Its typed
  actor is in Atelier's receipts under both methods, a content marker adds a
  platform-visible copy wherever an operation has a slot for one, and in the App
  method the account proves it outright. The record says which of the three a
  consumer is looking at instead of flattening them.
- The disowned-verdict failure is therefore mitigated in both methods and closed
  only in the App one — and for a contentless change in the token method it is
  mitigated only in Atelier's own ledger. Naming that honestly is the point: a
  consumer that needs account-level proof — #8's fourth rule, a future multi-user
  path — knows it must ask for the App method rather than discovering the gap later.
- An effect whose outcome no read can settle waits for the operator instead of being
  retried into a duplicate or over a human's correction. That is a real cost of a
  platform without idempotency keys, paid on the reconciliation path the effect
  contract already has, rather than hidden behind a read that cannot prove what it
  is asked to prove.
- V1 gains a poll loop and one new port. It gains no inbound surface, no new
  process and no new trust boundary; ADR 0009's remains the only one.
- The fleet's hand-rolled patterns become machine truth: a claim or verdict comment
  becomes a published effect with a receipt, and a requirement digest becomes an
  observed revision instead of a pasted string.
- #79 can be built on observed facts without inventing a queue-shaped platform
  contract, and #8 gets its hard measurements with provenance rather than
  self-reports.
- Polling bounds how fresh the queue can be, and that bound is configuration
  answering to #79's acceptance rather than a number chosen here.

## Required proofs before implementation is accepted

- A full durable and API projection after a fake run contains no token, no private
  key, no installation token and no credential path — the canary shape ADR 0009
  already uses, run once per auth method.
- Both auth methods perform the same operations, produce the same receipt shape and
  raise the same refusals; no operation is available in one method only —
  **`push-atelier-commit` is this record's one named, dated exception, PAT-only
  until its App handoff is designed (2026-08-25 amendment, #642-Journal)**.
- The same Action node re-executed after a crash between send and receipt leaves
  exactly one platform object, found by readback, never a twin.
- A crash before the request leaves the adapter still finds the intent durably
  `PREPARED` with its exact request bytes, and an effect whose readback cannot
  settle reaches `WAITING_RECONCILIATION` without a second send.
- A readback that can only search returns `UNKNOWN` and routes to reconciliation;
  no path returns `AUTHORITATIVE_NOT_FOUND` from a search, and no create operation
  declares an authoritative negative at all.
- A reversible change whose target does not stand in the intended state returns
  `UNKNOWN`, not an absence — proven with the human-reversal case: Atelier closes an
  item, a human reopens it, and the readback does not report that Atelier never
  closed it.
- A merge readback proves both outcomes against the head the intent named, and a
  head that moved since is not answered as if it had not.
- A commit an operation produces carries exactly the identity and trailer that
  operation revision declares, on a host whose ambient git configuration says
  something else.
- An unconnected or revoked project performs no operation and yields no
  observation, and no durable row is written on the refusal path.
- Every operation revision declares its marker slot and its authoritative-negative
  row, and one declaring a slot its object does not have is refused at binding.
- A contentless change writes no companion object, and its attribution is readable
  from Atelier's receipts alone.
- No operation writes a body Atelier does not own; a requirement issue's bytes are
  byte-identical before and after a run that observed it.
- No published operation writes a label that any authorization or automation filter
  reads.
- A restart mid-observation resumes from the durable cursor, producing the same
  fact set once, with no gap and no replay.
- A closed-unmerged pull request never reads as merged, and a landing receipt names
  reviewed head, reviewed tree and merge commit.
- A check result observed against another head is refused as evidence for this
  candidate.
- In the App method an action by the installation and one by the operator yield
  different typed actors from the same observation path; in the PAT method the same
  path records the marker-derived actor as a claim and never as platform-proven.
- No module outside the GitHub adapter imports the client library or names a GitHub
  concept, proven by the ADR 0005 boundary gate.
- A rate-limit exhaustion is visible in the projection and loses no cursor.
- A published requirement revision's digest matches the platform's served bytes
  recomputed independently, with no added newline and no normalization.
- **(2026-08-25 amendment, #642-Journal, `push-atelier-commit`)** A crash after
  the commit is pushed but before its receipt is written leaves readback finding
  the pushed commit; a re-attempt after that crash creates no second commit.
- **(2026-08-25 amendment, #642-Journal)** A racing identical push and a racing
  divergent push both resolve without a duplicate commit; the divergent race
  resolves through `platform-push-ref-diverged` to `UNKNOWN`, never a silent
  overwrite of the other push.
- **(2026-08-25 amendment, #642-Journal)** A force-reset of the target branch
  between push and readback resolves to `UNKNOWN`, never an authoritative
  absence.
- **(2026-08-25 amendment, #642-Journal)** A push whose target branch already
  exists — standing at the intent's own base commit or at a later ancestor of
  it — is refused rather than updated, and the existing ref's content is
  unchanged after the refusal: the create-only invariant holds even where a
  fast-forward would have been technically possible.
- **(2026-08-25 amendment, #642-Journal)** On a host whose ambient
  `user.name`/`user.email` contradict the operation revision's declared
  identity, the pushed commit still carries exactly that declared identity.
- **(2026-08-25 amendment, #642-Journal)** One compound proof stands for the
  candidate-tree data-loss class as a whole: the captured candidate matches the
  pinned tree exactly for every path the lease still carries, including a
  tracked-but-ignored file; the candidate is anchored in ADR 0011's
  project-owned candidate store before the attempt reaches a terminal
  successful state and before its workspace is released; a re-attempt after a
  crash between anchoring and success recovers idempotently, finding the same
  ref rather than writing a second one; the ref survives `git gc --prune=now`;
  and a capture failure ends the attempt FAILED under its own named failure
  code with the loss named in its receipt, its workspace released rather than
  retained, with no `AgentAttemptPossiblyRan` report on replay, and a retry
  re-runs the attempt from scratch.
- **(2026-08-26 amendment, #712)** A snapshot answers the exact bytes the
  platform served as the item's body and a digest a reader recomputes from them
  alone: a body carrying a carriage return, a non-ASCII character and no
  trailing newline is neither normalized nor re-encoded, and appending a
  newline is a different revision. A read whose change marker is missing is
  refused rather than answered with an invented one, and an item the tracker
  does not hold — or a reference in another adapter's grammar — is answered as
  unknown rather than as a read that may yet succeed.
- **(2026-08-26 amendment, #712)** A pull request read by name answers the
  neutral `change_request` kind, and no GitHub noun travels with it past the
  adapter boundary. A read answering about a different item than the one asked
  for is refused rather than pinned under the asked-for name, at the adapter and
  again above it.
- **(2026-08-26 amendment, #712)** A start naming a work-item order stores the
  observed revision the start read, under the house schema the workflow pinned;
  a document pinning any other schema for that order refuses the start with
  nothing written. The same item read across an edit yields two different stored
  values, and a start that cannot read the item — no connection, no such item,
  an unreachable platform, a refused payload — writes no run row and answers
  which of the four it was. An item whose read exceeds the inline order bound is
  refused by that bound, again with nothing written.
- **(2026-08-26 amendment, #712)** A retry of a run that already exists answers
  from what that run pinned without reading the tracker at all — proven with an
  edited item, an unreachable platform, a deleted item, and a start that names
  an artifact beside the item — while a retry naming a different item is a
  conflict rather than that run; and a read whose write failed leaves nothing
  behind, so the next start reads again.
- **(2026-08-26 amendment, #712)** A stored order under the work-item schema
  that does not hash to the hash beside it, or is not the complete document its
  writer produces, stops the next start as corrupt durable state rather than
  being read as an identity.
- **(2026-08-25 amendment, #642-Journal)** A durable projection, event, receipt,
  and log all show no credential or secret material for a `push-atelier-commit`
  run under whichever credential handoff it is specified for — the PAT file
  today. **Stated exactly, to match the credential-handoff paragraph above: the
  argument vector and the environment of the git subprocess, and of any
  credential helper or `GIT_ASKPASS` process it invokes, may legitimately show
  the credential-file path — that is the reference decision 3 and the
  paragraph above name — but never the credential's own bytes; the canary
  asserts the absence of the secret's bytes, not the absence of the reference
  to it.** This is the same canary standard the auth-method proof above already
  uses, extended past the raw command line to the process environment and to
  every helper process the git subprocess itself starts. A future amendment
  specifying the App method's installation-token handoff proves the same
  canary against that handoff before it is accepted.

## Out of scope and stop conditions

This record does not decide: the queue, priority, ready model, automation filter
and its label vocabulary (#79); conductor assignment (#7); any non-GitHub tracker,
for which decision 1's port shape is the seam and nothing more; multi-project
isolation (#23); the durable requirement-revision store and trace format
(`docs/requirements/README.md`); durable failure tokens (#16); catalog identity
(#22); cost, pricing and quota (ADR 0008); the remote transport and inbound surface
a webhook needs (ADR 0009 and #9 part 3).

Stop implementation on: an auth method hardcoded instead of chosen per project
connection, or either method built as a second-class path with fewer operations
beyond `push-atelier-commit`'s one named, dated PAT-only exception (2026-08-25
amendment, #642-Journal, pending its App handoff design); a
marker-derived actor presented as platform-proven attribution; a content-bearing
object created without its marker, or a marker slot assumed for an operation that
has none; a marker written into a body a human owns; a companion object created to
mark a contentless change; a label written for an authorization or filter to read;
an authoritative negative claimed for a create or for a reversible change without
its declared non-performance evidence, or any absence derived from search; any
effect re-sent on an unresolved outcome; a commit identity read from ambient git
configuration instead of its operation revision; a shared App, or an incident that
inventories only Atelier's own connection records; a stored token or installation
token; a
secret value in a workflow, prompt, context package, event, receipt, log or API
resource; a create without a prior readback; a GitHub identifier outside the
adapter; an agent shell publishing through `gh` instead of an Action node; a
webhook accepted as truth without a readback; a poll interval hardcoded instead of
configured; a label name minted here; a reading operation added to the
observation port ahead of the caller that needs it (2026-08-26 amendment,
#732-Journal); or a snapshot that carries a platform's own noun for an item
instead of the neutral kind.

## Supersedes

None.
