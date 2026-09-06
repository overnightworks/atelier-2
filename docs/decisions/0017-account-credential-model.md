# ADR 0017: An installation-owned Account holds every credential; delegated grants and stored keys are peer auth modes, and the app holds only references

- Status: PROPOSED 2026-08-24 — draft awaiting operator approval. This record
  decides a model; only the slices §10 names as built exist. Everything else is
  proposed, and nothing here claims otherwise. Context's description of the
  Claude adapter amended 2026-09-02 (issue #993): the prior directory-sharing
  design it named is superseded by per-invocation isolation.
- Date: 2026-08-24
- Requirement authority: [Issue #1](https://github.com/FlexOr2/atelier-2/issues/1)
  (the secret rule: no credential value in a workflow, prompt, event, receipt,
  log or API resource)
- Decision authority: [Issue #557](https://github.com/FlexOr2/atelier-2/issues/557),
  which carries the operator rulings of 23.–24.08.2026 this record formalizes —
  the three-layer settings model, the generic grant vocabulary, the
  reference-not-vault strategy, both auth modes from the start, the app-DB-never-
  holds-the-value precision, and the *Account* vocabulary ruling. Body read under
  ADR 0010 §5's canonical rule — 8,604 UTF-8 bytes, ending in one LF byte —
  `15d3e0e25cb4a049798201961bdae8484e5be99e7c24b5c4873073079375d4b1`.
  The credential-source topology requirement (§4: server-held versus
  runner-local) and the effect-execution-locus ruling (§5: both loci
  first-class, X the default, Y an opt-in) of 24.08.2026 are further operator
  rulings not yet in that body; operator approval of this record is their
  durable record until #557's journal carries them.
- Depends on: [ADR 0009](0009-runner-trust.md) (§5–§6: credentials reach a
  worker by reference, never by value; a runner reads no credential outside its
  bound profile; the credential-channel canary shape), [ADR 0010](0010-github-platform-adapter.md)
  (§2–§3: PAT and GitHub App as operator-chosen methods, the connection record,
  installation tokens derived and never stored), [ADR 0011](0011-project-isolation.md)
  (the project boundary a per-source Account override is scoped by)
- Names, never decides, the dependencies owned elsewhere:
  [#82](https://github.com/FlexOr2/atelier-2/issues/82) (OIDC human login — the
  other identity axis, §9), [#23](https://github.com/FlexOr2/atelier-2/issues/23)
  (multi-project isolation), [#540](https://github.com/FlexOr2/atelier-2/issues/540)
  (runner cutover, where installation accounts become operable),
  [#567](https://github.com/FlexOr2/atelier-2/issues/567) (project links, the
  surface that occupies roles), [#16](https://github.com/FlexOr2/atelier-2/issues/16)
  (durable failure tokens, where any refusal here must become one)

## Context

Atelier acts toward two kinds of external party on a user's behalf: AI
providers (Claude, Codex, Grok) whose CLIs or APIs execute agent attempts, and
code platforms (GitHub, and later GitLab) where effects land and observations
originate. Every one of those actions needs a credential, and today every
credential enters the same way: **a file the operator placed on the host,
referenced by path**. The host CLI takes `--claude-credential-directory`,
`--grok-credential-directory` and `--codex-credential-directory`
(`atelier2.host`); the Claude adapter reads only the subscription type out of
the operator's own `CLAUDE_CONFIG_DIR` and hands the *path* to the provider
subprocess (`atelier2.adapters.claude_subscription`); the GitHub `open-pr`
adapter resolves a personal access token from a credential directory once at
`open()` (`atelier2.adapters.github.live_effects.GitHubTokenCredential`). The
credential-channel canary (`tests/domain/test_credential_channel.py`)
structurally refuses any API field or durable column named for a credential
channel. ADR 0009 §6 fixed the wire rule: a runner receives a typed non-secret
`AuthReference`, never a value and never a Serve-local path.

**2026-09-02 amendment (Issue #993): the Claude adapter no longer hands the
operator's own directory to the child process.** Issue #993 diagnosed the
paragraph above as this executor's production defect: `CLAUDE_CONFIG_DIR`
pointed every invocation at the operator's own live directory, which other
processes read and wrote concurrently, and the pinned CLI has a known lock
race on that directory's files (Anthropic GH #3117) — read as a live
production incident, not a theoretical risk. The declared credential
directory is now read as a SOURCE only, at composition and once per
invocation; the directory a launched process is actually handed is a private,
disposable copy minted for that one call, holding `.credentials.json` plus a
written onboarding stub, and removed on every lifecycle path — a normal
release, a refusal never decoded, an exception, or executor shutdown. The
paragraph above is left standing as the record of what this adapter did in
August and why; this amendment states what it does instead, not a second
telling of the same fact.

That bootstrap is clean, and it does not scale, and this record says so
plainly: **it presumes one human with shell access to the host.** A multi-user
server has users who are not that human. A user connects *their* repository
and deposits a token for *their* project through a UI; the server must durably
hold or durably be able to obtain that credential and use it unattended —
overnight runs, queue-driven work, no operator at a terminal — without the
operator placing a file per user, and without any user's secret being readable
by any other user. "A file the operator placed" answers none of that. The
operator's rulings on #557 (24.08.2026) pin how the answer must look:

- **Reference, not vault**: Atelier preferably stores no secret at all — it
  points at a login the operator or provider already performed. Where a secret
  must exist, it is held by reference and preferably short-lived.
- **Both modes from the start**: delegated OAuth-style grants *and* raw API
  keys/PATs are first-class peers, because some providers offer no delegated
  form and some operators choose keys deliberately. OAuth-only is not the
  product.
- **The app DB never holds the value**: the durable Atelier database — runs,
  decisions, events, graph — stores only a reference to an Account; the secret
  value lives in a dedicated secret channel, enforced structurally by the
  canary.
- **The unit is named *Account***, in UI, docs and code.

Two owners already hold pieces of this and must not be duplicated: ADR 0009
owns how a credential reaches a worker, ADR 0010 owns GitHub's two methods and
the connection record. Nothing owns what the installation durably *has* — the
Account itself, its storage, its tenancy, its modes across all providers and
platforms. That is the gap this record closes.

## Decision

### 1. The Account is the one installation-owned credential unit

An **Account** is the installation-owned record of one connected external
identity:

- the **provider or platform** it belongs to (`anthropic`, `openai`, `xai`,
  `github`, `gitlab` — the existing `ProviderId` slug shape);
- its **auth mode** (§2);
- its **credential source** (§4): server-held, or runner-local;
- exactly one credential anchor, by source and mode: for a server-held
  Account a **delegated-grant reference** (where the credential is a login
  session or grant, §3 tier A) or a **secret-store reference** (where the
  credential is a stored key, §3 tier B); for a runner-local Account only a
  **non-secret role reference** the runner resolves on its own host — the
  server holds no secret material for it at all;
- its **owning tenant** (§9) and a non-secret display identity so an operator
  can tell two Accounts apart without resolving either.

The Account subsumes what today exists three times — the per-provider
credential-directory flags, the GitHub credential directory, and ADR 0010's
per-connection credential reference — into one owner. ADR 0010's connection
record keeps everything else it binds (project, repository scope, method,
connecting actor) and its credential reference becomes a reference to an
Account; no second credential ownership survives beside it.

The durable app database stores the Account record — provider, mode,
reference, tenancy — and **never** a secret value. That is not a guideline but
the canary's contract, extended over the Account tables the moment they exist.

### 2. `AuthMode`: delegated and stored-key modes are first-class peers

The existing `AuthMode` (`atelier2.contracts.agents`) carries `SUBSCRIPTION`
and `API_KEY` and is already framed into auth-profile revision hashes,
requests and receipts. This record extends that enum — it does not replace it —
to four members, distinguished by **who holds the credential**:

| Mode | Holder of the credential | Status |
| --- | --- | --- |
| `subscription` | the provider's own CLI: a login session in a config directory Atelier references by path and never parses beyond a non-secret check | BUILT |
| `oauth` | the Atelier server: a delegated grant (refresh token) in the secret store, from which short-lived scoped access tokens are minted at use | PROPOSED |
| `api_key` | the Atelier server: a raw long-lived secret (API key or PAT) in the secret store, envelope-encrypted (§3) | member BUILT; live provider executors PROPOSED |
| `app_installation` | the Atelier server: a GitHub App private key plus an installation id, from which short-lived installation tokens are minted at use and never stored (ADR 0010 §3) | PROPOSED |

The holder column above describes the server-held source; under the
runner-local source (§4) the same modes exist with the runner host as holder,
and `subscription` is inherently runner-local — a CLI login lives where the
CLI runs.

`subscription` and `oauth` stay distinct members although both are delegated,
because they name different holders: a `subscription` login lives in the
provider CLI's own store and survives only on a host that carries it, while an
`oauth` grant is server-held state a multi-user deployment can obtain per user
through a browser flow. Collapsing them would erase exactly the fact a
deployment plans around.

**Neither family is a fallback for the other.** A binding to a mode the
resolved Account does not carry refuses — ADR 0009 §7's no-downgrade rule,
reused, not re-decided. Which modes each provider supports, and why both
families exist per provider:

| Provider / platform | Delegated modes | Stored-key mode | Why both are first-class |
| --- | --- | --- | --- |
| Claude / Anthropic | `subscription` — the CLI's OAuth login (`claudeAiOauth` record under `CLAUDE_CONFIG_DIR`), BUILT | Anthropic API key (`api_key`), PROPOSED | subscription carries an operator's own plan; a key carries org billing and a server tenant who never runs the CLI |
| Codex / OpenAI | `subscription` — the Codex CLI's ChatGPT login, BUILT (`adapters/codex_subscription.py`); a server-held `oauth` grant PROPOSED | OpenAI API key (`api_key`), PROPOSED | same split: personal plan vs. unattended, per-tenant, metered use |
| Grok / xAI | `subscription` — the Grok CLI's login session, BUILT (`adapters/grok_subscription.py`) | xAI API key (`api_key`), PROPOSED — the primary mode for direct API use | xAI's API access is key-centric; the CLI session covers the CLI path only |
| GitHub | `app_installation` (preferred for multi-user: own actor, per-installation scope and revocation) and `oauth` (device flow per user), both PROPOSED | PAT (`api_key`) — BUILT as file-by-reference; ADR 0010 §2 keeps it a first-class operator choice, never a concession | ADR 0010's table owns the full trade; the PAT is the low-friction first slice, not the target |
| GitLab | OAuth application / group or project access token, PROPOSED | PAT (`api_key`), PROPOSED | future adapter behind the same platform port; same mode taxonomy, no new model |

Which delegated form a provider actually offers is an external fact that
shifts; the table states it as of this record, and each adapter verifies it
against the provider's primary documentation when its mode is built, rather
than this record freezing a foreign product's roadmap.

### 3. Storage: two honest tiers, both clean

**Tier A — delegated grant, preferred.** The server stores only the grant
material: a refresh token, or an App private key plus installation id. From it
the adapter mints **short-lived, scoped access tokens at the moment of use**,
held in memory and persisted nowhere — ADR 0010 §3's "installation tokens are
derived, never stored", generalized to every delegated mode. The user (or
operator) revokes the grant at the provider, and revocation actually bites,
because nothing long-lived was ever copied. Atelier never holds the user's
password and never a long-lived broad token in this tier. The grant material
is itself a secret and gets no exemption: it lives in the secret store under
tier B's encryption discipline. Its advantage over a stored key is scope,
lifetime and revocability — not a lighter storage rule.

The `subscription` mode is the delegated idea in its already-built,
runner-local form (§4): the grant lives in the provider CLI's own store,
Atelier holds only the path, and stores nothing at all.

**Tier B — stored key, first-class.** Where a provider offers no delegated
form, or the operator chooses a key, the raw secret is stored with **envelope
encryption**: the ciphertext lives in a dedicated secret store; each secret is
encrypted under its own data key; the data key is wrapped by a
key-encryption-key held **outside** the store — a KMS, a Vault, an HSM, or a
host-provided key, the backend being an open operator decision (listed below). The
secret is decrypted **only at the point of use** — and under §4's coupling
that point is always in Core: the adapter making the outgoing platform call,
for the duration of that call, never written back anywhere. A provider
process in a cage authenticates with a runner-local credential (§4), never
with a server-held one. A dump of the secret store alone yields ciphertext
without its KEK; a dump of the app database alone yields references. Neither
alone leaks a secret — a sentence that bounds **offline** artifacts only: a
live, fully compromised server reaches the KEK exactly as its own
decrypt-at-use path does, so a live full compromise must be assumed to expose
every server-held tier-B plaintext, and recovery is rotating every tier-B
secret. That exposure is precisely why reference-before-vault and the
runner-local source exist.

**The KEK has a lifecycle of its own.** It is rotatable: rotation re-wraps
every data key under the new KEK, and no ciphertext remains reachable only
through the old one. A KEK compromise is therefore recoverable without
re-depositing every secret — rotate the KEK, re-wrap the data keys — because
the KEK wraps keys, never the secrets themselves.

**The secret store is a separate boundary from the app database.** Different
store, different access path, never joined in one query, one backup or one
volume. Today's built predecessor of that boundary is the operator-owned file
referenced by path; on the one-machine bootstrap the same file also stands in
for the runner-local mapping, mounted read-only into the cage (ADR 0009's
2026-08-22 credential-ingress amendment) — one file playing both roles on one
host is exactly what §4's two sources pull apart, and under §4's coupling the
grown server-held store never inherits that mount. The OS keyring and a
secrets manager are the
named backends the same reference shape grows into (#557). A UI that lets a
user deposit a token writes into the secret store through the credential
channel and never into the app database — depositing is the one operator/user
hand-action #557 reserves; secrets never travel through chat or agents. The
deposit route itself carries obligations: it is TLS-terminated end to end,
its request body is never logged, traced or captured by any middleware, and
the value it carries is written only into the credential channel — the one
moment a secret legitimately transits the server is granted no second
destination.

Both tiers describe the **server-held** source; §4 names the second source,
for which the server stores nothing at all.

### 4. Credential source: where the value lives is an axis orthogonal to the mode

The mode (§2) says what kind of credential authenticates; the **source** says
where its value physically lives and who resolves it. Two sources are
first-class, and every Account carries exactly one (operator requirement,
24.08.2026):

- **Server-held.** The Atelier server holds the Account under §3's storage
  tiers and resolves it in Core. This is the source for what the server
  itself performs — reading work items, publishing effects — and for nothing
  else: it feeds no cage (the coupling below).
- **Runner-local.** The credential value lives **only on the runner host** —
  a separate machine, typically inside a company's own network with its own
  egress — as a mounted secret file, a host keyring entry, or a provider-CLI
  login session. Mounted files and keyrings are preferred over raw
  environment variables, because a process environment is readable through
  `/proc` and inherited by every subprocess, while a file or keyring entry is
  read once at the point of use. The server sends only the generic role by
  its non-secret reference; a small runner-side mapping, set when the runner
  is installed, resolves role → local secret. The value never leaves the
  runner host and never touches the server's database, leases or logs.

Runner-local is not a new wire contract: it **is** ADR 0009 §6, taken
seriously — "Core transmits a logical credential reference; the Runner
resolves the reference from its own credential source" — extended from the
provider directories the launcher already mounts read-only (ADR 0009's
2026-08-22 amendment) to a declared per-role mapping. What this record adds is
that the source is part of the Account model, not an accident of deployment.

| | Server-held | Runner-local |
| --- | --- | --- |
| Who holds the value | the server's secret store (§3), encrypted at rest | only the runner host: mounted file, keyring, or CLI login |
| Who resolves it | the server, at the outgoing call in Core | the runner, from its install-time role mapping |
| What crosses the wire | nothing: the value is resolved and used in Core and crosses to no worker | the role reference only — never any secret material |
| Threat surface | server compromise reaches ciphertext and the KEK path (§3 bounds it) | server compromise reaches **nothing**: the server never had the value |
| Revocation | rotate or revoke in installation settings (§8 layering) | revoke on the runner host, where the company's access control already is |
| When to use | what Core itself performs: platform effects and reads for tenants who deposit keys | every runner-executed use; enterprise runners in a private network; any tenant unwilling to hand a token to an external server |

**The one coupling: the axes are selectable, but not fully independent, and
this record says so instead of faking it.** A server-held Account may
authenticate only Core-performed use — X-locus effects and server-side reads.
A runner-*executed* provider process, and a runner-executed effect or read
under locus Y (§5–§6), **requires a runner-local Account**: feeding a
server-held secret into a cage — an `api_key` handed to a runner, or any
server-held value or token minted from one crossing the service toward a
worker — would make Atelier exactly the secret-distribution channel ADR 0009
§6 forbids, and is refused by name (`credential-source-locus-mismatch`) at
binding resolution, before any lease or provider start. This coupling is what
*preserves* ADR 0009 §6 unchanged: under every admissible combination, no
secret crosses the service in either direction — Core-held values are used in
Core, runner-held values are used in the runner, and the wire carries
references only.

The coupling's named consequence for provider keys: agent attempts execute in
cages, so a provider `api_key` that authenticates attempts must be
runner-local — placed on the runner host, not deposited at the server. A
*server-held* provider key can serve only a Core-performed provider call, of
which none exists today; binding one to an attempt refuses
(`credential-source-locus-mismatch`) instead of the key leaking into the
cage.

**Runner-local is the preferred enterprise topology.** A company's sensitive
tokens stay on their runner, in their network, governed by access control
they already operate; the Atelier server — even an external SaaS one — never
sees them, and its run-execution path can stay credential-minimal. That
strengthens, not complicates, the multi-tenant story: the server-held store
holds only what tenants chose to deposit.

**How far runner-local reaches for platform effects is the third axis, §5:**
today it covers provider credentials fully — the provider process runs *in*
the runner, so the login or key lives on the runner host and the server never
sees it — while a platform-effect token follows the effect execution locus.

**Per-role scoping holds under both sources.** The cage gives a run only the
credential for **its** bound role — each run reads only its role, never
"every agent reads everything." Server-held: Core resolves exactly the one
referenced credential for its own call — it feeds no cage (the coupling
above). Runner-local: the runner's mapping resolves
exactly the one role the lease names, and ADR 0009 §5's prohibition — a
runner reads no credential outside its bound profile — is the enforcement,
here applied *inside* the runner host between its cage and its own mapping. A
role the mapping does not carry refuses (`auth-profile-unresolvable`,
ADR 0009), with no fallback to another source.

**Both axes compose with the layering (§8) and stay invisible to the
workflow.** A role can be occupied by a server-held Account or a runner-local
one, in any mode the provider supports and any use its source admits under
the coupling above; the workflow still declares only the generic role, blind
to source and mode alike.

### 5. Effect execution locus: the third selectable axis — Core performs by default, runner-performs is a verified opt-in

Two credential classes execute in different places, and the difference is
load-bearing for how far the runner-local source reaches:

- **Agent/LLM credentials (Claude, Codex, Grok)** authenticate a provider
  process that runs *in* the runner. Runner-local holds fully; that is the
  built shape.
- **Platform effect credentials (GitHub today)** authenticate an *effect* —
  and under ADR 0009 §1 Core is the sole writer of product truth, so today
  the effect is performed by Core: the agent produces an intent, and the
  server makes the real platform call and commits the receipt
  (`adapters/github/live_effects.py`, #430). That is exactly why the GitHub
  token sits on the server today — and why, for platform effects under X
  alone, "the token never reaches the server" does not hold.

**The ruled model (operator ruling, 24.08.2026): both loci are first-class
and selectable, exactly as `auth_mode` and the credential source are peers —
per effect binding and per deployment, not one product-wide either/or.** The
operator's rationale, kept here because it is the requirement: build the
concept so both are possible from the start — X is needed for the self-hosted
single-operator installation, Y for the enterprise "our secrets never leave
our network" topology — rather than forcing one.

- **X — Core performs** is the **safe default**: an effect binding that
  selects no locus executes in Core. Simplest form, one central auditable
  performer, one truth-writer.
- **Y — the runner performs, Core verifies** is an **explicit opt-in** per
  enterprise deployment or binding: the platform token stays runner-local,
  and Core's role shifts from performer to verifier.

| | X — Core performs (default; today) | Y — Runner performs, Core verifies (opt-in; PROPOSED) |
| --- | --- | --- |
| Who holds the platform token | the server (server-held source, §3) | the runner host (runner-local source) |
| Who performs | Core: one central, auditable performer | the runner, inside the tenant's own network |
| Who writes truth | Core performs *and* writes the receipt | Core only: it reads the platform back (ADR 0010's readback surface) and commits the receipt on what the readback proved — never on the runner's report |
| Trust implication | the platform token crosses into the server's trust domain | effect execution moves into the less-trusted worker (the boundary ADR 0009 exists for); a runner could falsify its own effect report, so Core's independent readback is the check, and Core still needs at least read reach for it |
| Satisfies "our secrets never leave our network" | no — the platform token is on the server | yes — this completes the enterprise requirement for effects |
| Cost | none beyond §3's storage discipline | building the Effect Worker lane ADR 0009 §1 already names — a separate worker role for one Core-prepared `EffectIntent`, with only Core committing the receipt — placed on the tenant's runner host under a runner-local grant; plus amendments to ADR 0009 (that placement and credential source) and ADR 0010 (execute at the worker, readback in Core), in their own record before Y is built |

**The guardrail that keeps "both possible" clean: Y is refused for any
effect Core cannot reliably verify by readback.** ADR 0010 §5 already
enumerates which operations' readback can prove what; an operation whose
declared readback cannot positively prove the claimed outcome — the class
that can only answer `EffectUnknownOutcome`, such as an eventually-consistent
search standing in for an inventory — may not opt into Y, and a binding that
tries refuses loudly at binding time (`effect-locus-unverifiable`). At run
time the same rule holds: a Y readback that answers `UNKNOWN` routes to the
reconciliation path `contracts.effects` already owns, and never becomes a
receipt on the runner's word. Y silently degrading to "Core trusts the
runner's receipt" is the one failure this axis must never have; the threat is
a compromised runner forging an effect nobody can check, and the invariant is
§7's number 8.

**The three axes together.** An Account and its bindings now select along
`auth_mode` (§2) × credential source (§4) × locus (§5 for writes, mirrored
for reads in §6) — each per account, role or binding, never product-wide by
accident. The safe default on every axis is the simplest built form:
`subscription` where a provider CLI login exists, server-held for a deposited
secret, X for an effect. Every other value is an explicit opt-in with a
fail-loud guardrail: no mode downgrade (§2), no source fallback (§4), no
unverifiable Y (§5). They carry exactly one coupling, stated in §4 rather
than hidden: a server-held Account authenticates only Core-performed use, so
every runner-executed use requires a runner-local Account
(`credential-source-locus-mismatch`). Y-support remains PROPOSED end to end;
nothing performs an effect outside Core today.

### 6. Display and platform reads: the same locus axis, mirrored

Reading has the same locus question as writing, and one distinction above it
that costs nothing and must never be blurred:

- **Displaying Atelier's own state — runs, graph, decisions, queue — needs no
  platform credential at all.** The cockpit and API project Atelier's own
  durable truth (ADR 0003, ADR 0004); that baseline works with or without any
  Account and is not part of this axis.
- **Reading the connected platform's state — work items, PR/MR status,
  repository state — needs a read credential**, and that read has exactly
  §5's locus choice:
  - **X-read — the server reads** (the default, and the only form anything
    does today): the readback that exists (`open-pr`, #430) runs server-side
    under a server-held credential, and ADR 0010 §4's polling observation is
    decided in the same place. Simplest.
  - **Y-read — a runner reads and reports** (opt-in, PROPOSED): the read
    credential stays runner-local; a runner inside the tenant's network
    fetches items and status, and the server stores and displays what the
    runner reported — it never holds the platform token.

The same guardrails carry over unchanged: fail loud (an unresolvable or
unconnected read refuses with ADR 0010's own refusals, never returns an
invented empty state), and no silent trust-downgrade — a runner-fetched
platform fact is recorded as ADR 0010 §6 already requires of every observed
fact, with its provenance, here including *who fetched it*, and is never
presented as server-verified when it is not. That provenance is load-bearing
beyond display: observed platform facts feed #79's ready model and #8's
measurements, so a fabricating runner under Y-read could steer automation,
not just a screen. The Y amendment must therefore name which automation may
consume facts whose only provenance is a runner's report — display is safe by
the labeling above; anything that *acts* on such a fact is a decision that
amendment owns.

**The honest consequence of running fully runner-local (Y everywhere): the
server never reads the platform directly.** A runner in the enterprise
network is the deployment's eyes — it fetches the items and status the
cockpit displays. One tension is named now so the Y amendment cannot miss it:
invariant 8 commits a Y-effect receipt only on Core's *own* readback, and a
server that never reads the platform cannot perform one. A fully runner-local
deployment therefore resolves that verification read inside the Y amendment's
scope — an independent reader distinct from the performing runner, or a
minimal server-side read credential as the one deliberate exception — and
this record refuses only the third option outright: the performer verifying
itself.

**Pointing at a platform is token-free.** Declaring a link — this project
tracks that GitHub or GitLab repository — is a declaration, not a use; the
platform adapter contract (#24, ADR 0010) is generic, and GitLab is not a
special case of it. The link *does* something — observe, import, write — only
once a credential for that operation exists; until then each operation
refuses with ADR 0010's `project-source-not-connected` /
`platform-credential-unresolvable` shapes rather than half-working.
Runner-read is a named seam, not built; it follows the same build-when-needed
discipline as locus Y.

### 7. Invariants, each with the threat it mitigates

1. **The secret value never enters the app run/event/decision database, any
   API projection, log, prompt, dossier, lease or receipt.** Only the secret
   store holds it, encrypted — and for a runner-local Account (§4) the value
   additionally never reaches the server at all, in any channel. *Threat:*
   database dump, log aggregation, prompt exfiltration, evidence-export leak.
   *Enforcement, stated precisely because the canary must evolve without
   weakening:* the name canaries
   (`tests/domain/test_credential_channel.py`) keep guarding every
   value-*leaving* surface — durable columns, API fields, wire models — with
   **no** exemption, extended over the Account tables and resources this
   record adds. The source-wide name scan gains exactly **one** enumerated
   owner: the secret-store adapter package becomes the sole module permitted
   to name `secret`/`credential`, because a store that may not say its own
   name cannot exist. And for the encrypted path the real enforcement is the
   value-level required proofs below — a dump without the KEK yields nothing,
   a projection after a run carries no value — with the name canary as the
   name-channel tripwire, not the whole control.
2. **Reference before vault.** Where a provider holds a login Atelier can
   point at, Atelier stores nothing; a secret is stored only where no
   delegated form exists or the operator chose a key. *Threat:* Atelier
   accreting into a high-value vault whose compromise is every tenant's
   compromise.
3. **Decrypt at use only.** Plaintext exists in the memory of the one calling
   adapter or cage for the duration of one use, never at rest outside the
   store, never re-persisted, never returned by any API. *Threat:* insider
   with store or backup access; a crashed process leaving plaintext behind.
4. **Per-tenant isolation and least handoff.** An Account belongs to exactly
   one tenant; a user reaches only Accounts their tenancy owns; and a cage
   receives exactly the one credential the one run's binding names — by
   reference, resolved at the trust boundary, and under §4's coupling never a
   server-held value — never the store, never a second Account (ADR 0009 §5:
   a runner reads no credential outside its bound profile). Layered
   resolution (§8) **never selects an Account outside the requesting
   project's owning tenancy**: in a multi-tenant deployment the installation
   default is per-tenant, and a fallthrough that would cross a tenancy
   boundary refuses (`account-tenant-mismatch`) instead of resolving.
   *Threat:* cross-tenant access, including by defaulting rather than by
   request; a compromised run harvesting the installation's whole credential
   set.
5. **Rotation and revocation are live paths, not documentation.** Delegated
   modes rotate themselves (short-lived tokens) and are revocable at the
   provider by the user. A stored key has a named rotation path: the ciphertext
   is replaced under the same Account identity, the old value is never
   journaled, and every later use resolves the new value. The KEK rotates too
   (§3): rotation re-wraps every data key, and no ciphertext survives
   reachable only through the old KEK. *Threat:* leaked key, lost device,
   user offboarding, a revoked grant that keeps working because a long-lived
   copy survived; a KEK compromise with no recovery path short of
   re-depositing everything.
6. **Store/DB separation with the KEK outside both.** Compromise of any single
   system — app DB, secret store, or backup of either — yields references or
   ciphertext, never plaintext. *Threat:* single-system compromise; a backup
   pipeline that quietly widens the trust boundary.
7. **Fail loud, never downgrade.** An Account that does not resolve, a mode
   the Account does not carry, a tenant mismatch — each refuses with a typed
   refusal before any provider or platform call, with no fallback to another
   mode or another Account. *Threat:* silent widening; a run that "worked"
   under a credential nobody chose.
8. **Locus Y only for effects Core can verify by readback.** An effect
   binding may opt into runner-performed execution only where its declared
   readback (ADR 0010 §5) can positively prove the claimed outcome; an
   unverifiable effect refuses Y at binding time, and a Y readback answering
   `UNKNOWN` routes to reconciliation instead of becoming a receipt.
   *Threat:* a compromised runner forging an effect nobody can check — Y
   silently degrading into Core trusting the runner's own report.

### 8. The three layers: installation owns, project occupies, workflow declares

The #557 settings model, restated once here because Accounts are its first
layer, and not re-decided:

- **The installation owns Accounts.** Depositing, rotating and revoking a
  credential happens in installation settings (or the bootstrap CLI flags that
  are their predecessor) — nowhere else. A runner-local Account (§4) is
  *declared* at the installation like any other — provider, mode, role
  reference, tenant — but its value is placed on the runner host at install
  and never deposited at the server.
- **The project, and each linked source, occupies roles with an Account.** The
  installation default is one-Account-for-all **within one tenancy** — in a
  multi-tenant deployment the installation default is per-tenant, never a
  deployment-wide pool. A project or a single linked source may override its
  tenant's default with a source-specific Account. Most specific wins:
  link > project default > installation (tenant) default — the same
  resolution shape as the casting table — and no step of that fallthrough may
  cross a tenancy boundary: a resolution that would refuses
  (`account-tenant-mismatch`, invariant 4) instead of selecting a neighbor's
  Account.
- **The workflow declares only generic account roles and enforcement
  classes** — secret reach, network egress, source checkout, later
  computer-use (#557's grant vocabulary) — never a token, a provider name or
  an auth mode. The token value never appears in a project document, a
  workflow or a lease.

A new enforcement class is new core code with its own item; a new HTTPS API
behind an existing class never is (#557 ruling).

### 9. Human identity is a separate axis

WHO may operate — the login of #82's OIDC principal — is a different fact from
WHAT machine credential an Account holds, and the two never merge. Access
control binds an Account to its owning tenant; an authenticated human
principal is authorized *against* that binding to view, deposit, rotate or
revoke. An Account never doubles as a login, a login never doubles as an
Account, and revoking a human's access revokes their reach to Accounts without
touching the Accounts themselves. ADR 0009 §9's typed actor carries who
commanded a run; the Account carries what the run authenticated as toward the
provider. Both appear in receipts; neither substitutes for the other.

### 10. Built today, proposed next — precisely

**Built** (verifiable in the tree at this record's date):

- `AuthMode.SUBSCRIPTION` and `AuthMode.API_KEY`, framed into auth-profile and
  receipt hashes (`contracts/agents.py`); `AuthReference`, the typed
  non-secret wire form (ADR 0009 §6).
- Subscription-by-reference for Claude, Codex and Grok: credential directory
  by path, non-secret subscription-type check, path handed to the subprocess,
  environment scrubbed, no token stored
  (`adapters/claude_subscription.py`, `codex_subscription.py`,
  `grok_subscription.py`; host flags in `atelier2.host`).
- PAT-by-reference for GitHub: `GitHubTokenCredential` resolves a token file
  once at `open()`, value never durable
  (`adapters/github/live_effects.py`, ADR 0010 §3).
- The credential-channel canary (`tests/domain/test_credential_channel.py`).
- `api_key` in live use only by the credential-free fake-free witness executor
  (`adapters/free_runner_executor.py`).

Today's provider credential directories, read-only mounted into the runner
container on the same host (ADR 0009's 2026-08-22 amendment), together with
the reference-resolve seam both wire ends already share
(`adapters/free_runner_executor.py`), are the built predecessor of §4's
runner-local source — on one machine, without the declared per-role mapping.

**Proposed** (this record's target, none of it built): the Account record and
its durable reference-only tables, carrying the source axis; the `oauth` and
`app_installation` modes; live `api_key` executors for the three providers;
the encrypted secret store with envelope encryption and an external KEK; the
runner-side role → local-secret mapping for the runner-local source; locus-Y
effect execution (the Effect Worker on the runner host under a runner-local
grant, after the ADR 0009/0010 amendment §5 names) and its read mirror, the
runner-read lane (§6); per-tenant access control; deposit/rotate/revoke
surfaces; GitLab.

**The path between them is subsumption, not a parallel mechanism**: each
bootstrap credential-directory flag becomes the first installation Account of
its provider, in the same Account store the UI later writes — #557 names this
seam, and no second store may appear beside it.

## Refusals

Proposed with the model; durable failure tokens, where any must become one,
stay #16's. `auth-profile-unresolvable` (ADR 0009) and
`platform-credential-unresolvable` (ADR 0010) remain the point-of-use
refusals and are not renamed.

| Name | Raised when | Boundary |
| --- | --- | --- |
| `account-unknown` | a role occupation names an Account no record carries | binding resolution |
| `account-tenant-mismatch` | a principal or project reaches an Account outside its owning tenant | access control |
| `account-mode-unsupported` | a binding requires a mode the resolved Account does not carry, with no downgrade | binding resolution |
| `account-secret-store-unavailable` | the secret store or its KEK cannot be reached at the point of use | point of use |
| `account-revoked` | a use resolves an Account whose grant or key was revoked; the refusal names revocation, not absence | point of use |
| `effect-locus-unverifiable` | an effect binding opts into locus Y while its declared readback cannot positively prove the claimed outcome (§5, invariant 8) | binding resolution |
| `credential-source-locus-mismatch` | a binding pairs a server-held Account with a runner-executed use — a provider process in a cage, or a Y-locus effect or read (§4's coupling) | binding resolution |

## Threat model

| Threat | Covering control |
| --- | --- |
| App-database dump | references only, never values (invariants 1, 6; canary) |
| Full server compromise (external/SaaS deployment) | runner-local source (§4): the value was never on the server — for provider credentials today, for platform-effect tokens under the Y opt-in (§5). For server-held Accounts the honest bound: a **live** compromise exposes every tier-B plaintext, because the running server legitimately reaches the KEK (§3 bounds offline dumps only) — recovery is rotating every tier-B secret, and delegated grants are additionally revoked at the provider |
| Secret-store dump or store backup leak | ciphertext only; KEK held outside the store (§3 tier B) |
| KEK compromise | rotate the KEK and re-wrap every data key; no ciphertext survives reachable only through the old KEK (§3, invariant 5) — the secrets themselves need no re-deposit |
| Log, prompt, event or dossier leak | value never enters any of them (invariant 1; canary; ADR 0009 §6) |
| Cross-tenant access | Account-to-tenant binding plus access control (invariant 4; §9) |
| Insider with app-DB access | sees references |
| Insider with store access | sees ciphertext; KEK access is a separate, separately controlled system |
| Compromised runner/cage — runner-local short-lived forms | the cage holds one runner-local credential for one run (§4's coupling: never a server-held value); a delegated runner-local form exposes at most a short-lived scoped token; revocation stops new bindings (ADR 0009 §4/§10) |
| Compromised runner/cage — `subscription`, the BUILT mode, and this slice's sharpest residual | the cage holds a **long-lived login**: the operator's CLI credential directory is mounted readable (ADR 0009's 2026-08-22 amendment), including the OAuth refresh token, and HTTPS egress is permitted (ADR 0009's 2026-08-23 egress amendment) — so a prompt-injected or otherwise compromised tool-using run can read and exfiltrate it, or print it into its own answer, which becomes a durable record the *name* canary cannot catch. Containments: the egress bounds, provider-side revocation of the login, and known-value scrubbing of run output before it becomes durable — scrubbing is a control this record requires or, until built, a **named gap** the operator accepts at approval |
| Compromised runner forging an effect (locus Y) | only readback-verifiable effects may opt into Y, and a receipt commits only on Core's own readback, never on the runner's report (invariant 8; §5) |
| Compromised runner abusing a runner-local platform token beyond its leased intent (locus Y) | a coarse token (a PAT) cannot be scoped per-intent, so a compromised runner holding one can perform real platform effects outside any lease; the control is grant and token scoping — an App installation token or fine-grained per-repository token over a broad PAT — and a coarse PAT remains a **named residual on the tenant's side**, bounded by the tenant's own rotation and audit |
| Lost device / user offboarding | delegated grant revoked at the provider; stored key rotated under the same Account identity (invariant 5) |
| Revoked credential mid-operation | fail loud with a named refusal; no downgrade, no silent retry (invariant 7) |

## Consequences

- A multi-user deployment has one honest place a tenant's credential lives and
  one honest way it is used, instead of the file-per-operator bootstrap
  stretched past its truth. The bootstrap stays valid as the smallest
  installation's form of the same model.
- Both auth families cost real implementation per provider — a delegated flow
  *and* a key path each — and that is the operator's explicit requirement, not
  scope creep. The mode taxonomy keeps it four members, not one per provider.
- Atelier stores more secrets than today (tier B) and answers for them with
  envelope encryption, store/DB separation and an external KEK — machinery the
  single-operator bootstrap never needed. The reference-not-vault preference
  and the runner-local source are what keep that set as small as providers
  and tenants allow.
- An enterprise can run Atelier's server outside its network and still never
  hand it a sensitive token: the runner-local source (§4) keeps the value on
  the company's own runner, and the server's run-execution path stays
  credential-minimal. Today that covers provider credentials; covering
  platform-effect tokens too is locus Y (§5) — an explicit opt-in whose build
  remains PROPOSED.
- The locus becomes a selectable third axis with X as the safe default, for
  writes and reads alike (§5, §6). A deployment that opts into Y pays for the
  Effect Worker build and the ADR 0009/0010 amendment, and buys the
  enterprise topology for effects — bounded by invariant 8 to the effects a
  readback can prove; fully runner-local display costs the runner-read lane,
  through which a runner becomes the deployment's eyes on the platform.
- The three axes are honest about their one coupling rather than faking full
  independence: a server-held Account never feeds a cage, which keeps ADR
  0009 §6 intact and costs the convenience of "deposit a provider key at the
  server and run attempts with it" — a provider key for attempts is placed
  runner-local instead.
- The built `subscription` slice keeps its sharpest residual visible instead
  of papered over: a cage holding a mounted long-lived login with permitted
  HTTPS egress is exfiltratable by a compromised run, contained by egress
  bounds, provider-side revocation and output scrubbing (or its named gap) —
  the operator approves this record seeing that row.
- `AuthMode` gains two members; every hash frame that carries a mode already
  carries it as a value, so existing revisions keep their identities and new
  modes produce new ones.
- ADR 0010's connection record loses nothing and owns one thing less: its
  credential reference points at an Account instead of a private copy of the
  same idea.

## Open decisions for the operator

Listed, deliberately not decided here:

1. **Locus Y enablement**: the locus model itself is ruled (§5 — both
   first-class, X the default, Y an opt-in bounded by the verifiability
   guardrail; §6 mirrors it for reads). Open are when Y and the runner-read
   lane are built, the scope of the ADR 0009/0010 amendment they require —
   including how a fully runner-local deployment performs the verification
   read §6 names — and which adapter operations are certified Y-eligible
   first.
2. **Secret-store and KEK backend**: operator file + host-provided key (the
   built seam grown), OS keyring, HashiCorp Vault, a cloud KMS, or an
   age/HSM-wrapped file — and which the self-hosted default is.
3. **Default method per platform**: GitHub App vs. OAuth device flow vs. PAT
   as the recommended (never forced, ADR 0010 §2) default; subscription vs.
   API key as the recommended default per AI provider.
4. **Rotation cadence for stored keys**, and whether it is enforced (refusal
   past age) or advisory (visible staleness).
5. **Deposit scope for provider API keys**: per-installation only, or also
   per-project — the layering (§8) supports both; the question is what the
   deposit surface offers first.

## Required proofs before implementation is accepted

- The canary extends over every Account table and resource: a full durable and
  API projection after a run under each mode contains no secret value, no
  plaintext grant material and no store-internal path.
- An app-DB dump plus a secret-store dump, without the KEK, yield no plaintext
  secret between them.
- After a completed run, no plaintext secret exists at rest anywhere —
  including crash and cancel paths of the using adapter and cage.
- A principal of one tenant reaching another tenant's Account refuses
  (`account-tenant-mismatch`) and resolves nothing.
- A cage receives exactly the one bound credential; a second Account of the
  same installation is not resolvable from inside it.
- Rotating a stored key replaces the ciphertext under the same Account
  identity, the old value is not recoverable from any journal or backup path
  Atelier writes, and the next use resolves the new value.
- Rotating the KEK re-wraps every data key: after rotation every stored
  secret still resolves, and no ciphertext remains decryptable only through
  the old KEK.
- Revoking a delegated grant stops the next use with `account-revoked`; no
  long-lived derivative of the grant keeps working.
- A binding to a mode the Account does not carry refuses without downgrade,
  per provider, both directions (delegated↔key).
- A binding pairing a server-held Account with a runner-executed use refuses
  (`credential-source-locus-mismatch`) before any lease or provider start,
  and no credential material — value, token, or derivative — crosses the
  service toward the worker on the refusal path or any admissible path.
- The source-wide name scan admits exactly one owner: outside the enumerated
  secret-store adapter package, no module names a credential channel — the
  canary's single exemption is itself asserted, so a second one fails the
  suite.
- The tenant fallthrough proof: a project whose own and whose tenant's
  defaults are unset does not resolve another tenant's installation Account —
  the resolution refuses (`account-tenant-mismatch`) rather than selecting
  it.
- Known-value scrubbing, when built: a run whose output embeds a bound
  credential value does not produce a durable record containing it; until
  built, this proof's absence is the named gap the threat table carries.
- For a runner-local Account, the lease and every server-side channel carry
  only the non-secret role reference; a full server-side dump — app DB, secret
  store, logs, events, API projection — after such a run contains no byte of
  the credential value.
- A runner-local runner resolves exactly the role its lease binds; a role
  outside its mapping refuses (`auth-profile-unresolvable`), and a second
  role's secret is not resolvable from inside the bound run's cage.
- When locus Y is built: an effect binding whose declared readback cannot
  prove its outcome refuses Y at binding time (`effect-locus-unverifiable`);
  a runner report claiming a performed effect yields no receipt until Core's
  own readback proves it, and a forged report over an unperformed effect
  yields none at all — the intent routes to reconciliation; an unspecified
  locus executes in Core.
- Each bootstrap credential-directory flag resolves as an installation Account
  through the same resolution path the UI-deposited Account uses — one store,
  proven by the absence of a second.

## Out of scope and stop conditions

This record does not decide: the ADR 0009/0010 amendment locus Y requires
(its own record before Y is built); the store/KEK backend and the per-platform
defaults (the operator's open decisions above); OIDC login and session shape
(#82); how a
credential reaches a worker (ADR 0009 §6, unchanged); GitHub operation
semantics and the App-vs-PAT trade table (ADR 0010); the settings UI surfaces
and decision cards (#557, #474); multi-project isolation (#23); billing, quota
and cost (ADR 0008); durable failure tokens (#16).

Stop implementation on: a secret value in the app database, an event, a lease,
a log, a prompt, a dossier or any API projection; a plaintext secret at rest
outside the secret store; the KEK stored beside the ciphertext it wraps; a
mode downgrade or cross-mode fallback; a credential resolved from ambient
environment or a CLI's configuration instead of the bound Account (ADR 0010
§2's inheritance rule, generalized); a second credential store beside the
Account store; a tenant resolving another tenant's Account; a secret traveling
through chat, an agent conversation or an agent-writable surface; a delegated
mode built by storing the access token instead of minting it; a server-held
secret, or any token minted from one, crossing the service toward a cage or
worker (§4's coupling); a runner-local
credential value crossing to the server in any channel, or a runner resolving
a role its lease did not bind; an effect receipt committed under locus Y from
the runner's report instead of Core's own readback, or Y bound to an effect
whose readback cannot prove its outcome; a layered resolution selecting an
Account across a tenancy boundary; a second module naming a credential
channel beyond the enumerated secret-store package; or an Account table that
stores the value "temporarily".

## Supersedes

None. This record's source/locus coupling (§4) **preserves ADR 0009 §6
unchanged** — under every admissible axis combination no secret crosses the
service, which is that rule kept, not amended — and ADR 0010 §3's discipline
holds identically for both sources. ADR 0010's connection record is extended
to name an Account as its credential reference, and everything else it binds
stands.
