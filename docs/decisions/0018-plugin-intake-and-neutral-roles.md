# ADR 0018: Provider-bound plugins, neutral roles — how agents, skills, MCP and workflows enter the catalog and reach a run

- Status: ACCEPTED 2026-08-25 — operator approval on issue #660 after two
  independent adversarial reviews (PASS on delta); see
  [PR #676](https://github.com/FlexOr2/atelier-2/pull/676). Acceptance decides the
  model, not its existence: decision 6 is the only place that says what is built,
  and everything else here is proposed and claims nothing about the tree. §6
  amended 2026-09-02 (issue #660, "was ein Agent mitbekommt"); §5's grant count
  amended 2026-09-03 (issue #1101, `MAXIMUM_REDEEMED_TOOL_GRANTS` 1 → 2).
- Date: 2026-08-25
- Requirement authority: [Issue #1](https://github.com/FlexOr2/atelier-2/issues/1)
  (files in git are the source of truth; a run configuration pins exactly; no
  credential value in a workflow, prompt, event, receipt, log or API resource)
- Decision authority: [Issue #660](https://github.com/FlexOr2/atelier-2/issues/660).
  The operator asked the questions and rejected the first answer; the model below
  was ruled by the head under those questions and **is approved by approving this
  record**, not by the journal. Its two carrying comments —
  [the neutrality correction](https://github.com/FlexOr2/atelier-2/issues/660#issuecomment-5409294913)
  and [the plugin-as-unit ruling](https://github.com/FlexOr2/atelier-2/issues/660#issuecomment-5409299513) —
  supersede the earlier ones in that thread.
- Depends on: [ADR 0007](0007-catalog-identity.md) (decision 2 — operator-owned
  sources, three acts, no auto-intake; decision 3 — publication and admission are
  two states; decision 4 — the bytes are what a definition says; decision 8 — the
  publish gate), which §5 **amends** by one kind token;
  [ADR 0006](0006-node-vocabulary.md) (`skills:`/`tools:` are `{ref, revision}`);
  [ADR 0009](0009-runner-trust.md) (§6 — never a secret-distribution channel; §7 —
  a binding needs a runner that attests it);
  [ADR 0011](0011-project-isolation.md) (the project store an admission is scoped
  by); [ADR 0017](0017-account-credential-model.md) (the Account a credential
  reference points at)
- Names, never decides: [#557](https://github.com/FlexOr2/atelier-2/issues/557)
  (casting), [#66](https://github.com/FlexOr2/atelier-2/issues/66) (agent as a
  Markdown file — and the record whose Phase D this model contradicts, §6),
  [#659](https://github.com/FlexOr2/atelier-2/issues/659) (the catalog window),
  [#6](https://github.com/FlexOr2/atelier-2/issues/6) (the publish gate),
  [#8](https://github.com/FlexOr2/atelier-2/issues/8) (the scorecard). Refusal
  vocabulary is owned by `contracts/`'s `*Refusal` / `*RefusalReason` `StrEnum`s.

## Context

The operator asked how the atelier recognizes an agent, a skill, an MCP server or
a hook in a foreign repository; whether a Codex agent can run on Claude; whether
the import unit is a file or a plugin; and whether the answer stays
low-maintenance. The first answer built a **translation layer** — both provider
formats parsed into one neutral agent contract, model names mapped to tier
intents, provider-only fields mapped onto our capability bounds. His own test
("low-maintenance, working, provider-neutral *and* simple — does the concept
carry?") broke it: every provider field a translation models is a field the
atelier must keep chasing, and the result would still be a guess about a prompt
written for another provider.

What made translation look necessary survives: both providers model an agent as a
**composition** — identity, model, tools, skills, MCP servers, bounds — so an
agent is a recipe whose skills and MCP servers are ingredients that must exist
wherever it runs. Only the conclusion changes: the composition need not be
*understood* by us, it must be *complete* where the provider reads it. ADR 0007
owns where authored content lives and that the bytes are the truth; ADR 0009 owns
the trust boundary. Nothing owns the unit that enters, what the atelier promises
about it, or where neutrality actually lives.

## Decision

### 1. The role is neutral, not the file

Neutrality lives in two places, and in neither is it a property of an imported
file: **in the workflow**, where a node declares a portable `role`, a
workflow-generic instruction, what the occurrence gets and what it may do
(ADR 0006; `docs/VISION.md`'s worker/occurrence sentence); and **in the
casting**, where run start binds that role to one `AgentConfigurationRevision`
naming provider, model, auth profile and executor (#711).

An imported agent or plugin is therefore **provider-bound, and that is
acceptable**: a Claude Markdown agent runs on Claude, a Codex agent on Codex.
**The atelier translates nothing** — no model-name mapping, no composition
parsing, no neutral mirror of a provider's agent schema. The intake reads only
the **frontmatter minimum** it needs for two jobs of its own: the catalog window
(name, description, provider kind, origin) and reference resolution inside the
package (§2). Everything else travels as bytes, read by the provider that wrote
the format.

**Two authored fields are proposals, not settings, and one contradicts landed
code.** A file may *propose* a model; the casting sets it. Today
`agent_configuration_revision_for` (`contracts/agent_definitions.py`) does the
opposite — `deployment.default_model if definition.model is None else
definition.model` lets the file **override** the deployment default — so this is a
**required change to the #66 Phase A→C seam**, not a later nicety. A file's
`tools:` is likewise **bounded by the node's grant: it may narrow within it,
never widen it**, because the workflow decides what an occurrence may do. The
catalog states both per entry, so an operator sees that the file's model and
tools are not what will run.

**Switching provider means casting the role with a different agent**, not porting
a file, and #8's scorecard measures which casting performs better: competition at
the role rather than asserted portability. This record makes no "runs anywhere"
claim. What the atelier writes neutrally is what it owns — the house's core
workflows in the catalog (`workflows/*.yaml`) and the seat.

**A casting may name a whole plugin, not only one agent** (PROPOSED, on §4's
unmeasured operation). The role says what the occurrence may do; the casting
supplies the provider *and* the plugin travelling with it, optionally naming one
of its agents as the entry agent. The provider then uses that plugin's own
sub-agents, skills and admitted MCP servers as it would at home, inside the
containment vector. A plugin cast this way is **one casting unit**: its
ingredients are not cast separately.

**2026-08-25 amendment (Operator-Ruling, #711): what fills a role is one
precedence, and the models are configuration.** A role in the workflow declares
`difficulty: 1 | 2 | 3` (missing defaults to 2) and may declare
`family_differs_from: <role>` (a review role kept off the build role's provider
family). A workflow may separately declare a fixed model pin — the one named
exception to this section's neutrality, since it binds that workflow to a
provider; the role declaration itself stays neutral. Precedence per role:
**start override** (for that run only) **> pin in the workflow** (an exact
model id) **> model default per difficulty** (a project setting resolved to
one exact model id for the role's difficulty) **> next higher difficulty**
(#557; never silent, never a weaker model) **> uncast**. Which models exist is
configuration, not code: the host configuration channel holds one versioned
registry of exact model ids and Accounts per connected provider — populated
through the pinned CLI where that adapter can list models, otherwise validated
when first used, no rebuild — carrying no rating. A separate versioned project
setting maps difficulty 1/2/3 to one registry entry each, the operator's own
choice. The receipt carries the requested and the provider-confirmed model id
(#434).
Occupancy per (project, workflow lineage) is **retired, not layered**: no
per-role exception survives outside the pin and the start override above.
This is decided; ADR 0018 is its owner. #711 implements the registry and project
records, the one resolution path, and the occupancy retirement; #434 owns the
receipt extension.

### 2. The plugin is the intake unit, and its intake is atomic

A **plugin** is a provider-bound package: agents, skills, MCP declaration and
manifest, in the layout its provider already expects. It is taken in **whole** and
rendered **whole** (§4); nothing is decomposed into atelier-shaped parts.

- **Reference resolution is package-internal.** An agent referring to a skill or
  an MCP server refers to something in the same package; the intake resolves it
  there and nowhere else, and a reference leaving the package is refused at
  intake rather than starting without the ingredient.
- **One click is one commit: all N revisions or none.** A partial intake would
  leave the store holding agent v2 beside skill v1 — a combination no author
  published and no receipt could explain — so a failure at any file leaves the
  store byte-identical to its pre-intake state. Admission follows publication as
  ADR 0007 decision 3 requires, and is equally all-or-nothing.
- **A single-file import is the exception path**: a loose agent file is a
  one-file plugin under the same rules.

**Catalog building blocks vs plugin baggage.** A building block gets its own
catalog entry because something references, casts or blesses it individually:

| Building block | Why it gets an entry |
| --- | --- |
| `workflow` | runs consume it; the only provider-neutral block, because the grammar is ours |
| `agent_definition` | cast onto a role (#711); provider-bound, shown with its provider mark |
| `mcp_server` | blessed individually, since starting a process is a trust boundary; its declaration form is de-facto shared, its execution provider-bound |
| `skill` | referenced by agents, so a reference resolves and the revision is provable; `SKILL.md` is de-facto shared, but no portability is claimed until an executor loads one |

Everything else is **baggage**: hooks (not executed; the notice is shown),
commands, settings, the plugin manifest, marketplace metadata and provider-only
fields get no entry, travel with the plugin, are rendered into the scratch root
as the provider expects, and are neither shown nor translated — whatever the
containment vector does not allow simply has no effect. Nothing is mapped to a
generic form: the catalog **knows** what a plugin contains and translates none of
it, because the role and the reference are neutral and the file is not.

### 3. Git is the only authoring truth; the catalog is a window

ADR 0007 decision 2 applied, not re-decided.

- A **source is installation configuration**, not a store shape:
  `(source id, kind=git, location, ref, credential reference, selections)`, a
  selection being `(path pattern, kind token)`. The credential is an ADR 0017
  Account reference; material never enters the configuration (ADR 0009 §6).
- **A file's kind is configured, never inferred.** The layouts below are what an
  operator's selections usually say — defaults he writes, never a guess — and a
  file matching two selections is refused naming both.

| Ingredient | Usual pattern | Marker | Kind token |
| --- | --- | --- | --- |
| Agent | `agents/*.md`, `.claude/agents/*.md` | frontmatter carrying name and description | `agent_definition` |
| Skill | `skills/<name>/SKILL.md` | the directory plus SKILL.md frontmatter | `skill` |
| Workflow | `workflows/*.yaml` | the V3 grammar | `workflow` |
| MCP server | `.mcp.json`, `mcp.json` | an `mcpServers` mapping | `mcp_server` (§5, new) |

- **Connecting a source is one attributed first intake** — the click is the
  actor. After that **scanning is automatic and writes nothing**, showing drift;
  **intake happens on click**. There is no auto-intake, for ADR 0007 decision 2's
  reason: content the operator has never read must not become the head his next
  authored binding resolves to.
- **Provenance is `catalog_source_intakes`** — ADR 0007's shape
  `(revision hash, source id, path, source position, actor, taken_at)`. No column
  beside the bytes restates what the bytes say (decision 4).
- **`published_revisions(kind, revision_hash, document)` is already the
  content-addressed evidence store**; the snapshot is that table plus the intake
  record, not a second copy. Bytes are held because a run must prove which exact
  bytes drove it after a force-push or a deleted repository — git objects cannot
  carry that proof, since the repository is what may vanish. A second reason is
  future and marked as such: a cage without egress cannot fetch from git, so Core
  hands it the bytes.

### 4. Rendering is provider-native — a fourth, not-yet-measured operation of the same CLI

The plugin is written into the **attempt's scratch root** in the layout its
provider expects, and **the provider loads its own agents and skills**. That is
the decision; its mechanism is stated as what it is — unmeasured — because the
built containment vector cannot simply be reused.

**The measured doors vector is a doors configuration, not a plugin
configuration.** `adapters/claude_subscription.py` measured `--tools=` (removes
every tool, so no subagent delegation either), `--allowedTools`,
`--strict-mcp-config` with an explicit `--mcp-config`, `--setting-sources=` and
`--disable-slash-commands` — which the same measurement records as **disabling
all skills**. A run that must load a plugin's skills and agents cannot carry that
vector unchanged, and this record does not pretend otherwise.

**The candidate mechanism, named and unmeasured.** Loading a package whole points
at `--plugin-dir` — which loads the package's `hooks.json` and `.mcp.json` along
with its agents and skills — plus `--agent <name>` or an `--agents` JSON naming
the agent to run. None is measured here. Rendering is therefore a **fourth CLI
operation** beside the three the adapter proves, and it earns its containment
vector by its own probe, at the same standard: what each flag admits and refuses,
measured, before a plugin runs anything. `--safe-mode` stays excluded by the
existing measurement — it prevents any `--mcp-config` server from spawning, so it
and a door cannot coexist.

**Hooks are not executed — a requirement, not a measured fact.** A hook is
arbitrary shell code inside the agent process with everything the agent can see,
invisible and receiptless, and `--plugin-dir` is exactly the flag that would load
one; whether the process runs no hook is a probe result *Required proofs* demands
and this record does not assert. A hook's intent has honest graph owners — a
verification node or the `run-project-verification` grant, a verdict steering the
loop's back edge (ADR 0015), an edge, a receipt. Intake does not refuse a
plugin for carrying hooks: the agent is taken in and the catalog shows the notice
that they will not run, never silently.

### 5. An MCP server is a new published kind; its blessing is admission plus attestation

**This is an amendment to ADR 0007**, stated as one: that record closed the kind
token set and said adding a token is an amendment. `mcp_server` is added for a
named need — an MCP declaration must be publishable by hash before anything may
reference or spawn it. `RevisionKind` carries no such member today.

**Blessing is not a new card** but the two gates that exist: an **admission**
under #6's publish gate, without which an MCP revision is never spawned, and an
**executor attestation** (ADR 0009 §7), whose refusal token
`no-runner-attests-binding` is that record's and is itself unbuilt.

**Admission is per project store** (ADR 0011): a plugin blessed in project A is
not blessed in project B, which takes the same bytes in again — identical
hashes — and records its own admission. Trust is a project's decision and does
not travel with a hash.

Two constraints already bind and must not be quietly widened:
`MAXIMUM_REDEEMED_TOOL_GRANTS = 1` (`contracts/workflow_executability.py`) — one node pins
one grant — and Claude Code's `mcp__<server>__<tool>` allowlist grammar, which is
how an admitted server's tools are named and bounded.

**Amendment 2026-09-03 (issue #1101, plan-reviewed on #819): a node pins up to
two grants, one of each shape.** `MAXIMUM_REDEEMED_TOOL_GRANTS` rises 1 → 2: an
Agent node may bind one **exec-shaped** grant, redeemed inside its own attempt,
and one **effect-shaped** grant, redeemed after it succeeds, together. Two
grants of the *same* shape stay refused — which shape a pin is is only known
once its published bytes are read, so the refusal is named there rather than
counted at the authored form: once at start, before the run exists, once its
published capabilities resolve (`application/evaluate_executability.py`), and
again at binding time as the redemption-time invariant
(`adapters/dbos/agent_effect_grants.py`). The widening is in count, not in what
one node may still contradict.

**`.mcp.json` environment values are reference-only, and the reference form is
defined rather than guessed.** A **reference** is a name whose value is fetched at
use: an `${…}` placeholder the provider or runner substitutes from its own
environment — `${CLAUDE_PLUGIN_ROOT}` and `${SOME_TOKEN}` alike — or an Account
reference. Those pass intake untouched; the atelier resolves nothing and learns
nothing. A **literal secret value** written out in the file is refused at intake,
not sanitized and not stored, because an intake that "cleans" one has already
written it (ADR 0009 §6, ADR 0017 invariant 1).

### 6. Order by consumer: build for a reader that exists

1. **`workflow` intake first**, because runs already consume workflow revisions.
2. **Agents and skills reach a run only through #66 Phase C/D** —
   configuration → definition → launch. `AgentConfigurationRevision` carries model,
   auth profile, executor and capability and no definition link, and a V3 document
   declaring `skills:` is refused at start because `skills` stands in
   `V3_UNBOUND_AUTHORED_FORMS`: nothing binds it.
3. **Plugin intake needs an executor that loads a plugin from the scratch root** —
   the same seam, and the next real one.

**A named conflict with #66, not resolved here.** #66's Phase D plans to freeze
the definition's `AgentToolDeclaration` at run start and have the adapter map
neutral tokens onto provider flags. That is a translation, and §1 refuses
translations. Both cannot stand: either Phase D is re-cut so the file's `tools:`
is a proposal the node grant overrides, or this record's neutrality rule falls.
The re-cut belongs to #66's body.

**Built today**, verifiable in the tree at this record's date: the doors vector
and the atelier doors (`adapters/claude_subscription.py`); the publish doors,
including the agent-definition route landed as
[PR #630](https://github.com/FlexOr2/atelier-2/pull/630) (#66 Phase A);
`published_revisions` and the catalog lineage tables. The catalog window is in
flight (#659). **Proposed, none of it built**: the git source configuration, scan
and click-intake; `catalog_source_intakes`, which ADR 0007 names as a store shape
and no schema carries yet; the `mcp_server` kind; §4's rendering operation; and
the executor that loads a plugin.

**Amendment 2026-09-02 (operator ruling, issue #660 — "was ein Agent mitbekommt";
four independent counter-checks): Pieces reach a run individually, never a plugin
whole.** `profile` travels inside the execution request ahead of the instruction
(ADR 0006 lines 604-606: profile bytes first, instruction second);
`application/compose_node_job.py` composes it once bound (today it composes the
instruction only). `skills` are written by the delivering adapter into its
private state directory. `required_context` is written into the lease by a new
step of `execute_agent_attempt` after materialization (today the invocation is
built right after `materialize`, with no context step). Hooks, settings,
commands and provider configuration files are never written from a piece — they
have no piece kind, so the allowlist holds by construction; `.mcp.json` is
recognised at intake as `mcp_server` (§5, lines 153/181) and enters
`RevisionKind` before any `tools:` grant references it. The document pins
`{ref, revision}`; `head` resolves only at authoring (ADR 0007). Content
refusals for provider-bound kinds belong to the delivering adapter and are
invoked at intake, never at run start (Refusals). This amends §1 (whole-plugin
casting), §2 (rendered whole) and §4 (`--plugin-dir`); intake stays atomic per
plugin. Deferred and named on #660: sub-agent placement (agents reach a run
only through casting, §6), a project-default profile, and the containment
vector after a carrier cage (#632 covers Runner attempts only; local attempts
keep today's vector).

## Refusals

An ingredient can be wrong in three ways, each with its own boundary, so a
failure is never discovered at run start and never silently: **parse** (the file
does not satisfy the kind its selection declared), **reference resolution** (the
package lacks something it refers to — "agent X needs skill Y — not in the
plugin"), and **admission** (not blessed for this project store).

**A missing executor is a state, not a refusal.** "No configured executor for
provider X" is what the catalog *shows* on an entry — an honest startability
state beside its kind, name, origin, hash and its model/tools proposals (§1) —
never a refusal raised at run start.

The names below are proposed with the model; each joins the snake_case `*Refusal`
/ `*RefusalReason` `StrEnum` family in `contracts/` when built.

| Name | Raised when | Boundary |
| --- | --- | --- |
| `plugin_ingredient_missing` | an agent references a skill or MCP server the package does not contain | reference resolution |
| `plugin_reference_escapes_package` | a reference points outside the intaken package | reference resolution |
| `mcp_declaration_carries_secret` | an `.mcp.json` `env` entry carries a literal value instead of a reference (§5) | intake |
| `mcp_server_not_admitted` | a binding names an MCP revision this project store never admitted | binding resolution |

## Threat model

| Threat | Covering control |
| --- | --- |
| A foreign repository plants an MCP server so a run spawns its process | inert until an attributed intake, unspawnable until admission and attestation (§5) |
| A foreign repository plants hooks to run shell code inside the agent process | required: §4's probe must show no hook executes — a demand, not yet a measurement |
| A literal secret is smuggled into the store through `.mcp.json` `env` | refused at intake, never stored and never sanitized (§5) |
| A plugin references material outside itself and pulls in unreviewed bytes | package-internal resolution only (§2) |
| A partial intake binds an agent to a skill version nobody published | intake is one transaction (§2) |
| An agent reaches tools or servers nobody granted | the node's grant decides, the file's `tools:` does not (§1); one node pins one grant (§5) |
| A moved branch, force-push or deleted repository destroys a run's proof | the bytes live in `published_revisions` by hash (§3) |
| Content arrives the operator never read and becomes the next binding's head | intake is a click; scanning writes nothing (§3) |

**Amendment 2026-09-03 (issue #1101):** the covering control for "An agent
reaches tools or servers nobody granted" is now: the node's grant decides, the
file's `tools:` does not (§1); a node pins at most one exec-shaped and one
effect-shaped grant, and the second grant of a shape is refused by resolved
capability, at start and again at binding (§5, amended above).

## Consequences

- **No translation to maintain**: provider-format drift is the provider's
  problem, and the price is honesty about portability — a Codex agent does not
  become a Claude agent, so comparison happens by casting and #8's scorecard.
- **Rendering costs a new probe, not a reused vector** (§4). The existing doors
  measurement covers a different configuration and does not transfer.
- **Two landed decisions must change**: `agent_configuration_revision_for` stops
  letting a file override the deployment model (§1), and #66's Phase D
  neutral-token mapping is re-cut (§6). Both are preconditions, not niceties.
- **The `agent_definition` frontmatter key set becomes too narrow.**
  `contracts/agent_definitions.py` admits exactly `name`, `description`, `model`
  and `tools` and refuses anything else with `field-unknown`, so a real provider
  agent file is refused today. Pass-through requires that door to carry unmodelled
  keys while the required fields stay required.
- **`RevisionKind` gains one member**, outside the revision hash
  (`contracts/revisions_v3.py`), so existing revisions keep their identities.

## Open decisions for the operator

1. **The auto-intake amendment path.** ADR 0007 decision 2 names the shape an
   automatic intake would need — an enrolled `agent` actor under a published
   intake-policy revision — and refuses it until then. This record keeps
   click-intake; whether that amendment is ever wanted is open.
2. **Who may bless an MCP server under multiple users.** Which principal may
   admit, and whether a tenant admin may bless for a project he does not own, is
   an access-control decision this record does not take.

## Required proofs before implementation is accepted

- **The rendering probe (§4), before any plugin runs work.** With the plugin in an
  attempt's scratch root under the chosen flags, measured on a real CLI: which
  skills and agents load; that no tool outside the node's grant is reachable; that
  no MCP server outside the explicit config is reachable; that no settings file
  outside the scratch root is read; and that **a hook declared in the package does
  not execute** — observed at the process, not inferred from a flag list. A flag
  that cannot be shown to bound what it claims does not enter the vector.
- Scanning a connected source leaves the store byte-identical; the first intake
  records an actor, and no path produces an intake record without one.
- A plugin whose agent references a skill the package lacks is refused naming
  both, and nothing of that plugin is published.
- A plugin intake failing at its last file leaves the store byte-identical to its
  pre-intake state, proven over the whole store root.
- An `.mcp.json` carrying a literal secret is refused; one carrying `${…}`
  placeholders is taken in unchanged, and no substitution happens at intake.
- An MCP revision admitted in one project store is not spawnable from another
  until that store admits it too, at the identical revision hash.
- A definition whose file names a model publishes a configuration carrying the
  **deployment's** model, and the catalog shows the file's model as a proposal.
- An imported agent file with frontmatter keys the atelier does not model
  publishes, reconstructs byte-identically and reaches its provider unchanged,
  while a file missing a required field is still refused.
- A catalog entry whose provider has no configured executor renders as an
  unstartable state and raises no run-start refusal.
- A cast plugin's sub-agent delegation works under the measured vector.
- Casting one role with a Claude agent and with a Codex agent produces two runs
  whose receipts name their own provider, and no run configuration contains a
  translated model name.

## Out of scope and stop conditions

This record does not decide: the catalog window's layout (#659, #9); the #66
Phase C/D contracts themselves, including the re-cut §6 names; how a git source
is read (implementation under ADR 0007); the scorecard (#8, ADR 0008); the
Account and secret-store model (ADR 0017); the runner cutover (ADR 0009, #540).

Stop implementation on: any translation of a provider agent format into a neutral
composition, including model-name mapping; a write back into a configured source;
an automatic intake without an attributed actor; a per-attribute column beside the
published bytes; an imported file widening what a node grant allows; or a plugin
rendered under a containment vector no probe measured.

## Supersedes

None. This record **amends ADR 0007** by adding the `mcp_server` kind token (§5)
and otherwise applies decisions 2, 3, 4 and 8 unchanged; ADR 0006's reference form
and ADR 0009's boundary are untouched. The mid-day translation idea on #660 is
withdrawn by the operator's own correction (see *Context*).
