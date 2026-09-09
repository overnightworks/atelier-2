# ADR 0020: One session port carries every provider on the path that runs the live attempts; a permission is authorisation, the transcript its projection

- Status: ACCEPTED 2026-09-04; amended 2026-09-04 (§3, the policy is bound at
  dispatch), 2026-09-05 (issue #1252: the Agent Runner inventory named in §1
  -- `runner/session.py`, `runner/executors.py`, and the rest of the
  container-hosted cluster ADR 0009 describes -- was deleted for having no
  live caller; its code stays reachable in Git history for reference and is
  not to be revived), 2026-09-09 (issue #1430: §8, catalog knowledge belongs to
  the shared provider library, and §6's credential promise loses its visibility
  half by operator ruling of the same day) — step 1 carries the typed policy
  and the decider seam; steps 2-6 are not implemented
- Date: 2026-09-04
- Decision authority: the operator ruling of 2026-09-04 on proposal
  [#1177](https://github.com/FlexOr2/atelier-2/issues/1177), which owns the
  proposal history. Two independent counter-checks are recorded on that
  proposal — the first rejected the draft, the second accepted it with
  changes — and both sets of changes are carried into this record.
- Depends on: [ADR 0008](0008-budget-units.md) (turn limiter, meter, money
  absent), [ADR 0009](0009-runner-trust.md) (the trust boundary, provider
  containment, credential reference)
- Amends: [ADR 0009](0009-runner-trust.md) — the process watchdog is no longer a
  predecessor retained only for deletion but the first implementation of this
  record's session port, and the Agent Runner is deleted; the session port is
  the sole owner of live provider execution
- Feeds: [#1178](https://github.com/FlexOr2/atelier-2/issues/1178) (step 0),
  [#1174](https://github.com/FlexOr2/atelier-2/issues/1174) (output-seam schema
  discipline), [#943](https://github.com/FlexOr2/atelier-2/issues/943) and
  [#1099](https://github.com/FlexOr2/atelier-2/issues/1099) (the human terminal
  seat, which stays separate)

## Context

Six live runs in a row failed at the provider boundary, each a different symptom
of the same shape: three provider CLIs driven in headless print mode, their
standard output parsed by Atelier, schema flags that behave differently per
provider, scrub residue left in the candidate, and processes that die silently
(#1165, #1166, #1174, #943). This boundary is a small share of the product's
code and causes most of its outages, because a print-mode call is a one-way
process: a fixed payload goes to standard input, standard input closes, and
output is collected until end of file. A provider that wants to ask a question
mid-turn, or a caller that wants to cancel, has nowhere to speak. Meanwhile
every vendor maintains a structured duplex channel of its own, so a hand-written
parser per CLI buys each vendor's release cadence as a permanent bill.

The operator ruled on 2026-09-04: every model stays freely selectable, including
a privately hosted open model; take the simplest path and do not pay
thousand-fold maintenance because a CLI changed; a different mechanism per
provider is acceptable because an abstraction layer exists; the architecture
must be coherent. Billing runs through subscriptions.

A live-usage audit on 2026-09-04 answered where that boundary actually is. Of
485 live attempts in the preceding thirty days the Agent Runner executed none:
every attempt carries empty runner identity, no launch binding was ever
recorded, and no runner journal event exists. The Runner is a large body of code
with a larger body of tests that has never run in production, and the operator
ruled the same day that code built ahead of its caller is frozen — kept, not
hardened, and not treated as the foundation.

## Decision

### 1. One session port, owned by the path that carries the live attempts

The session port is `AgentSession` in `ports/agent_executions.py`, which today
arms an attempt, runs one invocation and gives supervision up. This record
grows it into a duplex conversation: `send(prompt)`, correlated events (tool
called, tool returned, permission requested), `decide(permission)`, `cancel`,
and one terminal result with its meter. A one-way process that writes a payload
and reads until end of file is the reason a provider cannot ask a question
mid-turn.

Its owner is the path that runs the live attempts:
`application/execute_agent_attempt.py`, with `AgentProcessSupervisor` — the
watchdog-backed implementation every live attempt has gone through — as the
first implementation of the port. The Agent Runner (`runner/session.py`,
`runner/executors.py`) was never the owner and is deleted for having had no
live caller. When a caller for it exists — isolation for foreign repositories,
or more than one user — the same port moves behind a Runner boundary built
fresh for that caller. That is the designed seam, and it is why the deletion
costs nothing: the port, not its host, is the contract.

In either placement a provider implementation is a contained child process or an
Atelier-owned bridge process, never code loaded into the driving process, and
ADR 0009 §1's containment requirements are what the port must satisfy when it
moves. An in-process vendor SDK is a different trust boundary and is not decided
here.

### 2. Three separated artefacts, never one

1. **Live session events** are the driver's contract. Each tool call carries an
   Atelier-owned typed correlation id; provider-side identifiers stay inside the
   driver and never reach a durable record. Without that id, two concurrent
   calls to the same tool cannot be told apart, which is what name matching
   silently lost.
2. **Permission receipts** are the authorisation ledger. A receipt is bound to
   the attempt, the policy revision, the correlation id of the call, and the
   effect: requested effect, offered scope, granted effect, authority. It
   carries enums, hashes and typed values, never raw provider arguments; a value
   that cannot be represented is refused, never truncated. This ledger does not
   replace `AgentReceiptV2`, which stays the receipt of an execution's outcome.
3. **The transcript** is a readable, redacted projection over the same
   correlation id — `attempt-transcript/v3`, with the v1 and v2 readers
   retained. Unknown provider output stays a bounded
   `UnrecognisedProviderOutput` step, and redaction stays solely in transcript
   construction. No session-opened step is added: provider, model and executor
   revision have owners in configuration and in the receipt, and a display
   derives them.

### 3. A permission is authorisation, decided by the session owner, fail-closed

A transcript step is evidence, not a control. The authorisation is an immutable
typed permission policy revision, bound to the execution at dispatch, and its
hash stands in every receipt the execution writes. A pure decider beside the
session owner answers each request from that policy and refuses anything it does
not recognise. It holds no deadline of its own; the attempt deadline bounds the
session. The driver transports the request and the decision and decides nothing.

**2026-09-04 amendment (head ruling, [#1198](https://github.com/FlexOr2/atelier-2/issues/1198)):
the policy is bound at dispatch, not carried in the request.** The draft put the
policy revision into `AgentConfigurationRevision` or `AgentExecutionRequestV2`.
Either would fold the authorisation into the identity an attempt is minted from,
so widening what a deployment permits would orphan every attempt in flight and
rewrite stored hashes. What a deployment permits is the deployment's fact: the
composition root binds one revision and hands it down as a dispatch parameter,
and the receipt names its hash. A per-executor grant reaches the request only
once it has a live caller.

### 4. Transport per provider is the vendor's own maintained structured channel

- **Grok**: the native agent-client-protocol mode, pin raised. It is the first
  vector because it removes the print-mode collapse without a third-party
  package.
- **Claude**: the agent-client-protocol adapter as a contained child process
  under a fixed pin. The vendor Python agent SDK is admissible only as an
  Atelier-owned bridge process, never in-process, and needs a permission hook
  there, because an earlier allow rule bypasses the SDK's own tool callback.
  Both paths remain subject to the vendor's subscription terms.
- **Codex**: the vendor SDK with plan login, with the documented JSON
  non-interactive mode as the fallback; the deprecated MCP-server path is not
  taken.
- **Open and self-hosted models**: an agent-client-protocol agent (Goose or
  OpenCode) in front of an OpenAI-compatible endpoint, behind the same port.

**The protocol itself is implemented once, provider-neutrally.** The
agent-client-protocol client is one bounded state machine over newline-delimited
JSON-RPC: it owns the framing and its bounds, the handshake, the session, the
single prompt, the correlation of every question to its answer, and the terminal
reading of how a conversation ended. It knows no vendor. What a vendor spells
differently reaches it through a narrow classifier that answers with typed values
or with "unrepresentable", never with a provider's own object; an unrepresentable
request that would have reached a file or a shell is refused closed. A second
vector therefore brings a classifier and its pin, not a second parser.

**The neutral ACP core is two adapter modules behind `ProviderConversation`.**
`adapters/newline_json_rpc.py` owns the bounded codec: every complete frame and
the exact unfinished remainder are measured against the port's own bounds
before anything is decoded, and an outgoing message is measured against the
caller's own bound before it is spelled. `adapters/agent_client_protocol.py`
owns the standard ACP state machine built on that codec — handshake, session,
permission and cancellation races, and the five terminal outcomes — translating
frames into `ProviderConversationAction`s and back. Neither module knows a
vendor's vocabulary; what a vendor spells outside ACP's standard fields reaches
the state machine only through the classifier seam above, never as a raw
provider dict.

A new provider is not configuration alone. Executors are selected by an exact
`(provider, executor revision)` key through the registry in
`ports/agent_executions.py`; every new vector needs a registered executor
revision, a pin and attestation, a policy, a meter and a conformance proof — but
no new protocol and no new parser once it speaks the protocol.

### 5. The output schema is judged at Atelier's own seam

Atelier's own output seam is the authority for every provider (#1174). Provider-
side schema enforcement may later be added as supplementary defence; it never
becomes the authority, because its behaviour differs per vendor and release.

### 6. Budgets, credentials and proof keep their existing owners

Budgets follow ADR 0008 unchanged: every multi-turn executor revision attests a
native turn limiter and an exact turn and token meter, and no money value enters
a receipt, a gate or a display. Credentials follow ADR 0009 §6 unchanged where
it applies: a logical reference is resolved by the side that executes, never
transported by value, and the provider's credential source is offered read-only,
so a writing token refresh fails visibly. That rule travels with the port when
it moves behind the Runner boundary.

Proof is per adapter and per pin, before a vector is armed: state-machine tests
for split and coalesced frames, unknown messages, permission and cancel races,
end of file without a terminal result, limit refusals, token refresh and
containment drift; a replay from a real capture; and a canary. One successful
transcript is not proof.

### 7. Each print-mode predecessor is deleted with its own proof

When a provider's new vector is proven, that provider's print-mode path is
deleted in the same slice — not collected for a cleanup at the end.

The process watchdog is not among those predecessors. ADR 0009 retained it only
for deletion because the Runner was expected to take its work; the audit above
shows the Runner never did, so this record amends that: the watchdog is the
first implementation of the session port and stays until the port moves behind a
Runner that a caller has pulled into life. Nothing else in ADR 0009's trust
boundary changes — it remains the target for isolation and multi-user
execution.

### 8. Which models exist is the shared library's answer, not a second one here

The boundary has two halves. A *turn* is §1's session port. A *catalog* — which
models a provider offers, and whether it is logged in at all — is a separate
question with a separate answer, and `overnightworks-agent-providers` owns it.
This repository holds one adapter over `agent_providers.catalog`
(`adapters/agent_provider_catalog.py`), an import contract that keeps the
library inside it, and no CLI vocabulary of its own; the host states its
deployment facts once, in `serve()`, and asks. Two truths about which models
exist may not coexist for a day, so the host-side discovery this replaced was
deleted in the same change.

Claude stays outside that answer. The library's Claude route lists the aliases
the CLI's `/model` prints (`opus`, `sonnet`), while a model registry here names
full model ids (`claude-opus-5`). Membership in an alias list is no evidence
about a registry id, so Claude reports no model list at all and its ids stay
unchecked until the validation door checks one — the honest answer, and the one
that keeps `checked` meaning what §5's seam and the start path read it as.

That validation door is untouched by this record. It runs the composed
executor's own prepared command and decoder, which is the only thing that knows
this deployment's configuration and executor revision; a catalog's membership
test cannot replace it and is not offered as one.

**The credential property, set here and inherited from ADR 0009 §6.** A
catalog probe never sees the operator's credential directory. It runs in a
private home holding one mode-0400 copy, that home is removed when the probe
ends, and a direct overwrite of the copy fails. ADR 0009 §6 is the standard
this meets; its own mechanism -- a read-only bind mount into a Runner
container -- is deleted with that container, so this record sets the property
positively rather than inheriting a live mount.

One half of §6's promise is given up here: a refresh that creates a new file
and renames it over the disposable copy succeeds **silently** and is discarded
with the private home, so the operator is never told it happened. That is an
operator ruling of 2026-09-09, recorded as a dated amendment beside §6 itself
in [ADR 0009](0009-runner-trust.md) -- the protection holds, no credential of
the operator's is written, lost, or made reachable, and the visibility is
released because nothing offers it today.

## Consequences

- What falls: the per-CLI stream parsers and schema-flag branches in the Claude
  and Grok subscription adapters, the Codex last-message file path, and the
  scrub sweep once measured.
- What stays: the durable core and its truth ownership, receipts, the candidate
  and its verification, and `CANDIDATE_UNCHANGED` as the honest failure of an
  attempt that changed nothing.
- Duplex is new surface in the session owner and each vendor channel is a pin
  that must be raised deliberately: permission and cancel can race, so that race
  is part of every adapter's proof.
- The Runner is deleted for having had no live caller.

## Named edges, not decided here

- A human or Core decision mid-turn needs its own authenticated duplex port with
  a deadline and a cancel-race contract.
- A writable per-attempt credential copy or a refresh broker waits for a
  measured refresh failure and a fresh operator ruling, per ADR 0009 §6.
- The registry cost of open models — one selection entry, pin and attestation
  per hosted model — is accepted, not abstracted away.
- The human terminal seat (#1099) and a real PTY for a child process (#943) stay
  separate from this boundary.
- Reviving the Runner has one trigger and no other: a named caller needing
  isolation for foreign repositories or more than one user; until then it
  stays deleted, and rebuilding it is a slice of its own.

## Order

Step 0 is #1178, cut to the live path: the existing provider child lifetime
moves behind the `AgentSession` port with the watchdog-backed supervisor as its
implementation, preserving bytes, cancel, reconnect and terminal evidence,
before any provider changes. Then: duplex events
with correlation, permission receipts and policy binding, proven against a fake
provider; Grok; Claude; Codex; an open model; transcript v3. A provider's live
proof is one real `issue-to-pr` run reaching its review node with that builder.

## Supersedes and amends

No ADR is superseded. This record **amends ADR 0009** in two places, each
dated and noted there. 2026-09-04: the process watchdog is no longer a
predecessor retained only for deletion but the first implementation of the
session port, and the Agent Runner is deleted; the session port is the owner of
live provider execution. 2026-09-09 (§8, operator ruling): §6's read-only
credential ingress keeps its protection but loses its promise that a writing
token refresh fails visibly, for the catalog path that meets its standard
without its deleted mount. ADR 0009's trust boundary, its containment rules
(§1), and the rest of §6 including the condition on a writable copy stand
unchanged as the target of that move, and ADR 0008's turn-limiter and
money-absent rules stand unchanged.

The print-mode invocation design being replaced was never a decision record: it
lives in the subscription adapters' own docstrings and in their executor
revision tokens. Removing it therefore supersedes no record and needs no
amendment anywhere.

## Out of scope and stop conditions

This record does not decide an in-process SDK, a mid-turn human approval port, a
writable credential path, terminal-seat design, or provider pricing. Stop
implementation if a driver starts deciding permissions,
if a transcript step is used as an authorisation, if a provider identifier
reaches a durable record instead of an Atelier correlation id, if provider-side
schema enforcement is treated as the authority, if a vector is armed without its
conformance proof and pin, if a replaced print-mode path is left alive after its
successor is proven, or if the deleted Runner is revived instead of building
the live path.
