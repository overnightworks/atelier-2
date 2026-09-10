# ADR 0019: Workshop target picture — rooms, blocks, rules

- Status: ACCEPTED 2026-08-25 — operator blessing of the picture
  [`0003-ziel-ui-mockup-v8.html`](../requirements/0003-ziel-ui-mockup-v8.html)
  (v8.13) on [#711](https://github.com/FlexOr2/atelier-2/issues/711). Accepting
  this record accepts the rooms, the four building blocks, the rules, the
  HEART amendments and the successor rules in §5; it claims nothing about what
  is built. The requirement document 0003 is pinned
  (`docs/requirements/README.md`, lifecycle): this record **records the
  decision**, and a successor revision of 0003 **carries it** through its own
  freeze/APPROVE step — no rule sentence changes in place. Rulings made after
  that date are not covered by this acceptance; §6 collects them, each with
  its own date, source and executing party.
- Date: 2026-08-25
- Requirement authority: [Requirement 0003](../requirements/0003-ziel-ui.md)
  (the target UI), [HEART](../HEART.md) (what it must feel like),
  [Issue #516](https://github.com/FlexOr2/atelier-2/issues/516) (the epic that
  ruled the four-surface rail this record replaces).
- Decision authority: operator rulings of 25.08.2026 on
  [#704](https://github.com/FlexOr2/atelier-2/issues/704) (starting lives in
  the catalog detail; no Workflows room) and the v8 review points (a)–(d)
  (conversation on the workbench, questions as stage and card, no "Done today"
  shelf, nothing twice on one page), plus the head's Board ruling of the same
  day; the operator rulings of 25.08.2026 on
  [#711](https://github.com/FlexOr2/atelier-2/issues/711) (the v8.9 review:
  no per-role exceptions, a Settings room, the queue as one unfolding line on
  the workbench, no "cast" in the copy; and the final ruling "model registry
  as configuration": exact model ids per provider, discovery through the
  pinned CLI, model defaults chosen per difficulty from that list — a model
  rating (0–10 with thresholds) was considered and rejected —
  the receipt holding requested and confirmed id); **all approved by
  approving this record**, not by the journal.
- Depends on: [ADR 0014](0014-in-graph-rounds.md) (the loop is drawn beside
  the edges); [ADR 0018](0018-plugin-intake-and-neutral-roles.md) (the role is
  neutral, the file provider-bound; plugin intake is atomic; hooks do not
  run; a missing executor is a state); [ADR 0007](0007-catalog-identity.md)
  (scan writes nothing, intake is a click); [ADR 0017](0017-account-credential-model.md)
  (a source names an Account reference, never material).
- Names, never decides: [#658](https://github.com/FlexOr2/atelier-2/issues/658)
  (the conversation is a run), [#79](https://github.com/FlexOr2/atelier-2/issues/79)
  (queue priority), [#567](https://github.com/FlexOr2/atelier-2/issues/567)
  (project connections), [#711](https://github.com/FlexOr2/atelier-2/issues/711)
  (who fills a role: difficulty against the project's model defaults — the
  ruling §3 records, and the owner of its grammar and schema follow-ups),
  [#434](https://github.com/FlexOr2/atelier-2/issues/434) (requested and
  confirmed model id in the receipt), [#724](https://github.com/FlexOr2/atelier-2/issues/724)
  (no inert settings/profile furniture — Settings is a room only because it
  holds sources and model defaults), [#82](https://github.com/FlexOr2/atelier-2/issues/82)
  / [#106](https://github.com/FlexOr2/atelier-2/issues/106) /
  [#557](https://github.com/FlexOr2/atelier-2/issues/557) (login, autonomy
  levels, settings layering — login and autonomy this picture deliberately
  leaves undrawn; #557's model tiers are answered by the Settings room).

## Context

The predecessor revision of Requirement 0003 names Mockup v5 as the gestalt
owner: a rail with four surfaces — Chat, Board, Workflows, History — under the
project as context.
Building against it produced a house whose first thing in the room is
navigation, whose finished runs stand in two places, whose starting has two
doors (a Workflows room and a catalog "Start"), and whose Chat is a page you
visit — the very thing HEART forbids. The operator's review of v8 named the
remaining fault plainly: the surfaces *restate* — a state shown by colour is
repeated as a sentence, a label explains a shelf that explains itself, a
count stands where an action should.

The picture is the answer: four rooms built from four blocks under one rule
against restating, every element carrying the user question it answers, and
every gate measured rather than asserted. It contradicts sixteen sentences
of the owner documents (§5). **No owner document, before this decision,
contains the word "Workbench" or "Werkbank"**: the room this picture is built
around exists nowhere in HEART or 0003 before this record, which introduces
it.

## Decision

### 1. Four rooms, and why there is no Board

The rail has three rooms — **Workbench · Catalog · History** — and at its
foot, set apart by a line, the fourth: **Settings**, the context above the
three. Its head is the project switcher; its name stands small under the
Settings entry in every room. Each room answers one question:

| Room | The one question | Never |
| --- | --- | --- |
| Workbench | What needs me now, what is moving, what did we say today, what do I say next? | a chronicle, a library, a shelf title, anything twice, a number without an action |
| Catalog | What can this house do, from where, how do I get more, how do I start one by hand? | editing, a hash on a card, the conductor |
| History | Which run was that — when, for what, with what result? | anything still moving or waiting; counting or grading |
| Settings | Which project am I in, what is it connected to, which models does each provider have, which one answers each difficulty? | a second door into a room, the queue, hard-coded provider lines, a secret in clear text |

The **Run** is a view, not a room: the same graph in three tenses (still,
live, frozen), reached from a row and trailing back to the room the run's
state belongs to.

**There is no Board, and there will not be one later.** A Board would be one
of two things: a copy of Workbench plus History (what waits, what moves, what
finished — all of which the workbench and the history already own, each
once), or a toy without an action (tiles that count). Both violate "nothing
twice" and "no number that does not lead to an action". What a Board was
meant to carry moves to owners that already exist: the decision is the first
thing on the workbench; the ochre count in the rail is the notification; scale
lives on the workbench's living shelf; the queue — imported items, priority,
admission — is one line on the workbench that unfolds in place into its list;
it lives nowhere else.

**There is no Workflows room and no Chat room.** Starting lives in the catalog
detail (one door, #704); speaking to an agent lives on the workbench, in the
terminal seat that stands where the conversation stood (amendment 06.09.2026,
below) — you do not visit a chat page. HEART lines 19–22
("Workflows owns starting … Catalog owns the library … admitted in the
Catalog and started in Workflows", landed 25.08.2026 by #684) are **retracted
by this record** (§5).

### 2. Four building blocks

Everything the workshop says is a question it holds out — a wait node of some
run — and everything the operator says is an answer. The decision with two
buttons and the "yes" on a start sheet are one mechanism in two sizes; what
the operator says to an agent is said in the terminal, which the workshop
frames and never reads (amendment 06.09.2026, below). Therefore every room is composed from exactly four blocks, each a
reused component (REQ-UIQ-07):

| Block | Question | Where |
| --- | --- | --- |
| **Stage** — sender line · question in serif · two honest buttons · one aside with a door | What is asked of me, what are my answers? | workbench top, waiting run, every dialog (a dialog is the stage in the operator's own hand: ink border, not ochre) |
| **Row** — name · work item · one sentence · clock; smallest form: name · work item · standing word | Which run, for what, how does it stand? | workbench, Recent runs, History |
| **Card** — the row in grid form: glyph · name · one sentence · pills | What is this, may I open it? | Catalog |
| **Sheet** — over the still page from the right at 1280, from the bottom at 390 (picture frame "Import", 390 shots), as tall as its content | What does this step need before it goes? | Start, Import, Connect a source |

plus the **graph with one node panel** (tabs Result · Input · Prompt · Log ·
Evidence) as the run's own body. A stage may take a written answer or more
than two answers; it never becomes a form, and a choice made once (an answer)
is buttons, never a dropdown. Dropdowns are for values chosen per difficulty,
per role on a start sheet, per work item or per account, which may be
re-chosen (model defaults, the start sheet's roles, picker, Account).

**Interactive Attach is an ephemeral exception surface above the graph, not a
room or fifth building block.**

### 3. State is shown, never restated

A state shows in the shape of the element itself — colour, placeholder, glyph,
breath — and is never repeated as a sentence. Text stands only where no
element shows it. Corollaries the picture applies everywhere:

- Origin and provenance stand only where they deviate from the default
  ("chosen now", "set on the start sheet", "group, 3 projects").
- Explanations come on request — tooltip, "Technical", "Raw" — never inline.
- Under a graph node stands the **role**, never the agent and never the
  difficulty; who fills it stands on the start sheet and in Settings, the
  difficulty in the node panel's Input tab ("Difficulty 2 · review"), the
  requested and the confirmed model id in its Evidence tab. The merge gate carries the person glyph, not
  the word "you". The loop carries data ("↻ until green · ≤3"); its condition
  is a tooltip.
- **The difficulty sits on the role, the model on the difficulty, and the
  models are configuration** (origin: the operator's tier ruling on
  [#557](https://github.com/FlexOr2/atelier-2/issues/557), 23.08.2026 — class
  = number per role, a project table of a few lines, fallback to the next
  higher class; sharpened on #711, 25.08.2026 — named classes rejected, tiers
  are numbers, no exceptions per role, no occupancy per lineage; final ruling
  on #711, 25.08.2026 ~23:35 — "model registry as configuration, not code").
  A workflow role declares `difficulty: 1 | 2 | 3` (absent → 2) and
  `kind: build | review`; it may add `family_differs_from: <role>` (the
  family rule is the workflow's, never an automatism of the house) and may
  **pin** one exact model id (`model: claude-opus-5`) — the head objected
  that a pin makes the workflow provider-bound; the operator allows it:
  optional, and visible as "pinned in workflow". The *definition* (the prompt
  file) stays neutral (ADR 0018 §1).
  **The registry**: one versioned configuration record per connected
  provider (a host-configuration record after the `OccupancyRevision`
  pattern; alternatively a file under `~/.config/atelier2/`) holding exact
  model ids (`claude-opus-5`, `claude-opus-4-8`, `grok-4.6`, `gpt-5.6-sol`,
  `gpt-5.5` …) with their Account — no rating, no rung: a numeric capability
  per model with thresholds per difficulty was considered on #711 and
  rejected by the operator (25.08.2026) — changed in Settings › Models and
  nowhere else. A new model is one line; the
  house is never rebuilt for it. **Discovery** per provider through the
  pinned CLI pre-fills the registry where the provider lists its models
  (Codex, Grok); where it does not (Claude), the operator adds the id and the
  house checks it by a dry run on first use, reporting "unknown at the
  provider" honestly — the line stays, nothing is cast to it. Provenance
  stands only where it deviates: "added by you".
  **Model defaults** per difficulty are the operator's choice: three rows,
  difficulty 1 · 2 · 3 → one model from the registry, as exact ids with
  their Account (`claude-opus-5 · Account leonardo`), each a dropdown over
  the Models list that writes on change; nothing is derived, no thresholds. Start override
  and pin take exact ids; aliases ("newest opus") are convenience only and are
  not drawn. Precedence per role, unchanged: **start override** (the start
  sheet's dropdown, for this run only) > **pinned in workflow** > **model default per difficulty**
  > **next higher difficulty** (#557: never silent, never weaker; the field
  says "(next higher)") > none (ochre "choose a configuration", only when no
  higher difficulty has one either or the family rule refuses them all; the
  conductor asks). A default row and a pin name a **model**, and the registry
  may hold several checked configurations of one model, one per capability: the
  configuration that fills a role is the one whose capability is the node's
  declared mode. A model without one leaves the role unoccupied — never a
  fallback to another capability, and never a step skipped for it. An exact
  configuration, named by a start override, wins as it was written.
  **There is no per-role exception and no durable override
  outside the workflow's pin**: what should hold for a project is changed in
  Settings, once, per difficulty. The word "cast" does not appear in the
  house's copy. **The receipt** holds both exact ids — the one the house
  requested and the one the provider confirmed — and a difference is visible
  at the run: brick on the confirmed id in the node's Evidence and on the
  back of the run (#434 owns the field). `cast_unbound_roles` stays the one
  owner of the resolution in code; the difficulty lookup is new beneath it.
  Reconciliation with the code (#711, 25.08.): the built **occupancy per
  (project, workflow lineage) is removed**, not kept as a layer — its records
  are not migrated, because nothing in the new model can hold a per-lineage
  value; a project that relied on one re-chooses in Model defaults (three
  rows), and the migration drops the table and its write path; the registry
  is a host-scoped record per provider, the model defaults a project-scoped
  record of three model references, both *beside* the agent configuration —
  never part of its hash; `difficulty` is a new role field and lands together
  with its grammar guard (the V3 node refuses unknown forms); the workflow pin
  is an explicit exception to ADR 0018 §1 and is to be written into ADR 0018.
- One glyph per room, per kind, per provider; no symbol exists that §01 of the
  picture does not explain.
- Hue belongs to state alone, and only to states that want something; done is
  ink. On any screen exactly one element moves. A state has **one carrier**: a
  waiting run's row shows the ochre node and no sentence.
- Framed is what lies on a living shelf and opens; ruled is the list of what is
  over. A room never mixes the two on one shelf.
- Empty is the shape without content where the absence is obvious (an empty
  workbench is only its terminal and the one next move; an empty history is
  only its column heads; an
  empty catalog shows no zero-count chips); a sentence stays only where it
  teaches the next move. Loading is a still skeleton. Error is brick with one
  sentence and one move — the failed run's move is "open the run", where what
  it did and where it stopped stand. Waiting is the stage.
- Anything wider than the screen — graph, log, table — fades at its right
  edge; a stack taller than its ceiling fades at the bottom. The fade is the
  one affordance, because overlay scrollbars cannot be promised.

### 4. What the picture draws that the house has today, and what it removes

Surfaces that exist in `frontend/src` today and are **drawn** in the picture:
the cancel dialog (two sentences: a hand at work vs. resting at a wait); the
wait answer as stage (boolean, enum, written; and an unconfirmed send with one
"Send again"); "open the run" from the stage; import by drop with its
recognition sheet and its two refusals (nothing recognised; a literal secret
in `.mcp.json`); the node panel with its five tabs and folded Raw; "Turn the
piece over" for who/cost/proof; the degraded start sheet ("no source" + door)
and the start error; catalog states "Newer revision · Pull", "Not
executable", "No executor runs X here yet"; connect a source and the question
before a disconnect; the empty catalog, the empty skills shelf, the empty
history, the empty workbench, not found, restarting; the failed run's one move.

Surfaces that exist today and are **removed** by this picture:

| Removed | Why / where it went |
| --- | --- |
| Board room, its "Needs you · N / Running · N" groups, the inline "Answer here ▾" disclosure, `BoardWaitingAnswer` | §1; the stage on the workbench is the one place to answer outside the run |
| Workflows room and its start cards; "Open Workflows" empty-state links | starting lives in the catalog detail; the rail is the only door between rooms |
| `NewRunPage`: "Saved workflow / Publish YAML" radios, both YAML textareas, "Edit", "Review publication", the publish dialog, "Expert fields" (profile, revision, provider, model, executor, auth mode), JSON order editor, "Change" | the start sheet is generated from the order schema; every order type has one rendering; casting is a dropdown per role; no editor (REQ-UI-22, non-goals) |
| `CatalogImportDoor` ×2 (paste textareas, per-kind "Import a workflow / an agent") | one "Import" button and the whole page as drop surface; a file is the unit |
| Catalog "Admit into catalog" button and its busy/error states | catalog admission happens inside the import transaction (ADR 0018 §2). The word **Admit** is not removed: it moves to the workbench's queue list, where "Admit" on a work item is both the not-yet state and the attributed click (#79) |
| Project page: count tiles, reference cards (Board/History/Workflows), workflow-occupancy `<select>` + "Save"/"✓ Saved", and the occupancy record behind it | the Project room becomes **Settings**: the switcher in its head, sources, the model registry, model defaults; every dropdown writes on change, no Save. The occupancy per lineage is retired (§3) |
| Rail slots "Profile", "switch project (Not built yet)" | #724: no inert furniture; Settings stays because it holds sources and model defaults; the switcher lives in its head; a Profile place arrives with #82/#106 |
| History "30 days" static note; "Name · Result · Duration" columns | chips Today · 7 days · 30 days · Range… with the default marked; columns When · Purpose · Work item · Result · Duration |
| "Retry / Discard" pairs on six cards | the error pattern: one sentence, one move; an answer once given is not discarded |
| Legacy `HumanActionCard` (integer wait), `NodeRail`, "Events N" raw disclosure | V1/V2 surfaces; exact bytes live under Evidence and Raw |
| `WorkflowGraphDrawing` legend "What the shapes mean" | vocabulary is learned once (§01 of the picture); a help place is a later decision, not a disclosure on every graph |
| `InfoHint` "Why …" buttons, `ProofAnchor` "Copy" buttons | tooltips and the hash itself as the copy target on the back |

Not drawn and, at the time of this decision, **not removed** — a technical
surface with no room: `ReconciliationActionCard` (found/absent determination
of an exact effect). It is operator-level repair; where it lives is an open
decision, see *Open*. (31.08.2026, #901 slice 4/#924: the card fell with the
V1/V2 run surface it lived on, so until the blessed V3 resolve surface exists
a run parked in WAITING_RECONCILIATION is resolvable only through the raw
API; the open decision below still owns where that surface goes.)

### 5. Owner sentences this picture contradicts, and their successors

Sixteen sentences change: four in HEART, nine rules, three passages; a fifth
HEART row records one sentence kept verbatim. Every rule gets a **new
identifier**; the old one is marked superseded-by and never reused. Old text
is quoted verbatim in its own language; the successor stays in that language,
so no requirement changes language. The 0003 successor revision is drafted
from this table and approved through the requirement lifecycle.

**HEART — amended in place**

| Today (verbatim) | Amended |
| --- | --- |
| "The Board owns what wants you now — what is still moving or waiting on you; History owns what already happened. A run lives in exactly one of the two at any moment, and it crosses from Board to History once, at the instant it turns terminal, never lingering in both." | "The Workbench owns what wants you now and what is still moving; History owns what already happened. A run lives in exactly one of the two at any moment and crosses from Workbench to History once, at the instant it turns terminal." |
| "Workflows owns starting — nothing lives there but what is already admitted and ready to run; Catalog owns the library — everything this workshop has ever been given, with where it came from and whether it may yet be started. A piece is admitted in the Catalog and started in Workflows, never the other way." (lines 19–22, #684) | **retracted**, replaced by: "The Catalog owns the library and starting: everything this workshop has ever been given, with where it came from, whether it may yet be started — and the one door to start it by hand. The terminal starts everything else." |
| "You speak to the workshop, you do not visit a chat page." | **kept verbatim** — where you speak is the workbench's own body, not a room. |
| "The composer is always within reach, and until the conductor is connected it says so honestly — in one sentence, without a button that duplicates a door." | "The terminal is always within reach on the workbench, and while it cannot be reached it says so in one sentence naming the way out — the agent CLI, or the HTTP API — without a button that duplicates a door." (amendment 06.09.2026) |
| "No naked numbers ("1 step"), no unlabeled sentences, no jargon ("took"), no word the operator has to guess ("ATELIER" floating over a title), no board numbers, no two ways to the same door." | "No naked numbers ("1 step"), no unlabeled sentences, no jargon ("took"), no word the operator has to guess ("ATELIER" floating over a title), no count that does not lead to an action, no two ways to the same door. A state is shown, never restated: where colour, shape or placeholder already says it, no sentence repeats it." |

**Requirement 0003 — identified rules: successor identifiers**

| Superseded (verbatim) | Successor |
| --- | --- |
| **REQ-UI-01**: „Die Werkstatt ordnet sich in eine Rail mit vier Flächen — Chat, Board, Workflows, History — samt Projekt-Umschalter und Settings/Profile-Slot; das Projekt ist der Kontext über allen vieren, kein fünfter Reiter." | **REQ-UI-20**: „Die Werkstatt ordnet sich in eine Rail mit drei Räumen — Workbench, Catalog, History — und am Fuß dem Raum Settings, dem Kontext über den dreien, mit dem Projektnamen klein darunter: Projekt-Umschalter im Kopf, verbundene Quellen, Modell-Registry je Provider, Model defaults je Stufe. Ein Profile-Platz kommt mit der Entscheidung von #82 und #106 (REQ-UI-15), nie vorher." REQ-UI-01 superseded-by REQ-UI-20. |
| **REQ-UI-02**: „Jeder Bildschirm beantwortet genau eine Frage." | **REQ-UI-21**: „Jeder Bildschirm beantwortet genau eine Frage; warten mehrere Fragen, ist eine als Bühne OFFEN und die anderen sind darunter in einem kompakten Stapel erreichbar — nie hinter einer Zahl verborgen." REQ-UI-02 superseded-by REQ-UI-21. |
| **REQ-UI-03**: „Workflows entstehen agentisch, nie in einem Baukasten oder Editor; ein neuer Entwurf erscheint als Karte auf dem Board, und der Operator segnet ihn dort ab." | **REQ-UI-22**: „Workflows entstehen agentisch, nie in einem Baukasten oder Editor; ein neuer Entwurf erscheint als Karte im Catalog, und der Operator segnet ihn dort ab." REQ-UI-03 superseded-by REQ-UI-22. |
| **REQ-UI-04**: „Woran die Flotte arbeitet, ist erstklassig: jede Fläche ist projekt-gescoped, und der Projekt-Umschalter in der Rail ist die eine Naht zum Projektwechsel." | **REQ-UI-23**: „Woran die Flotte arbeitet, ist erstklassig: jeder Raum ist projekt-gescoped, und der Projekt-Umschalter im Kopf von Settings ist die eine Naht: derselbe Klick wechselt das Projekt und landet in dessen Settings." REQ-UI-04 superseded-by REQ-UI-23. |
| **REQ-UI-16**: „Die Werkstatt nimmt Arbeit weg: lehrende Leerzustände, das Receipt als Schmuckstück, Rückgängig statt Nachfragen; Puls-Kopfzeile und Posteingang gehen im Board auf." | **REQ-UI-24**: „Die Werkstatt nimmt Arbeit weg: lehrende Leerzustände, das Receipt als Schmuckstück, Rückgängig statt Nachfragen; Puls-Kopfzeile und Posteingang gehen in der Workbench auf — die Bühne ist der Posteingang, die Ocker-Zahl in der Rail der Puls, die Queue eine aufklappbare Zeile." REQ-UI-16 superseded-by REQ-UI-24. |
| **REQ-UI-18**: „Mockups sind Entwurfs-Vorlagen; der aktuelle Stand der Vorlage ist [Mockup v5](0003-ziel-ui-mockup-v5.html), regeneriert vom lebenden Original des Operators, nie unabhängig editiert." | **REQ-UI-25**: „Gegen die gesegnete Vorlage wird gebaut, und ihre Tore werden gemessen statt behauptet; der aktuelle Stand ist [Mockup v8](0003-ziel-ui-mockup-v8.html), Owner-Record ADR 0019, wie Code per PR geändert; jede gesegnete Fassung wird eingefroren, die neueste gesegnete ist der Owner." REQ-UI-18 superseded-by REQ-UI-25. |
| **REQ-UIQ-04**: „Anzeige-Strings einer Fläche kommen aus ihrem Owner, und das Layout verträgt die längere Form; Kernflächen sind die vier Rail-Flächen und die Run-Sicht." | **REQ-UIQ-12**: „Anzeige-Strings eines Raums kommen aus ihrem Owner, und das Layout verträgt die längere Form; Kernflächen sind die drei Räume, Settings und die Run-Sicht." REQ-UIQ-04 superseded-by REQ-UIQ-12. |
| **REQ-UIQ-06**: „Leer, lädt, Fehler und wartet sind vier benannte Zustände." | **REQ-UIQ-10**: „Leer, lädt, Fehler und wartet sind vier gestaltete Zustände — benannt heißt gestaltet, nicht beschriftet: Leer ist die Form ohne Inhalt, Laden ein stilles Skelett, Fehler Brick mit einem Satz und einem Zug, Warten die Bühne." REQ-UIQ-06 superseded-by REQ-UIQ-10. |
| **REQ-UIQ-09**: „Die Fläche darf geil aussehen und Spaß machen; der Screenshot-Maßstab ist Mockup v5, und das letzte Wort hat der Operator." | **REQ-UIQ-11**: „Die Fläche darf geil aussehen und Spaß machen; der Screenshot-Maßstab ist Mockup v8, und das letzte Wort hat der Operator." REQ-UIQ-09 superseded-by REQ-UIQ-11. |

REQ-UI-04's substance is unchanged; it still receives a new identifier
because its wording changes ("Fläche" → "Raum", the switcher in Settings'
head, the double role of the click).

**Requirement 0003 — unnumbered passages (successor revision text)**

| Today (verbatim) | Successor text |
| --- | --- |
| Title: „# Requirement 0003: Ziel-UI — eine Werkstatt, vier Flächen, ein Graph" | „# Requirement 0003: Ziel-UI — eine Werkstatt, vier Räume, ein Graph" |
| Intent: „Um diesen Kern sitzt die Ziel-UI nach Mockup v5: eine Werkstatt, geordnet durch eine Rail mit vier Flächen — Chat, Board, Workflows, History — unter dem Projekt als Kontext (Operator-Ruling 22.08.2026, Epic #516). Mockup v5 ist der Gestalt-Owner." | „Um diesen Kern sitzt die Ziel-UI nach Mockup v8: eine Werkstatt, geordnet durch eine Rail mit drei Räumen — Workbench, Catalog, History — und Settings am Fuß als Kontext darüber, mit dem Projekt-Umschalter im Kopf (ADR 0019). Mockup v8 ist der Gestalt-Owner." |
| Non-goals: „Keine Dashboards und kein Benachrichtigungszentrum neben dem Board." | „Keine Dashboards, kein Board und kein Benachrichtigungszentrum: die Bühne auf der Workbench und die Ocker-Zahl in der Rail sind die Benachrichtigung." |

No other owner document may retain Board as a room or owner.

### 6. Later rulings recorded against this mapping

Not part of the 25.08.2026 blessing. §5 records that blessing and is not
amended; a supersession ruled after it is added here with its own date, its
source, and the party that executed the record — the requirement lifecycle's
approval line has a field for neither, so this table is where both are named.

**Requirement 0003 — identified rules: later retractions**

| Retracted (verbatim) | Superseding rule |
| --- | --- |
| **REQ-UIQ-01**: „Jedes Element beantwortet eine benennbare Nutzerfrage." | **retracted**, with no successor sentence: no verifier can bite on it — a list that counts its own entries refutes nothing. The named screenshot gate **REQ-UIQ-11** („Die Fläche darf geil aussehen und Spaß machen; der Screenshot-Maßstab ist Mockup v8, und das letzte Wort hat der Operator.") takes its place: there it is judged whether a surface answers its questions. REQ-UIQ-01 superseded-by REQ-UIQ-11. |

- Decision: the operator, 30.08.2026, recorded on
  [#435](https://github.com/FlexOr2/atelier-2/issues/435).
- Why it could be ruled: REQ-UIQ-01 has had no binding since 29.08.2026. Its
  proving sentence `studio-elements-answer-named-questions` left `acceptance/`
  with the landing of #897 (`50723cd3`), and the successor sentence
  `every-rendered-workbench-control-is-inventoried` states in writing that it
  does not claim the requirement.
- Execution: the head session, not the operator, removed the rule from the 0003
  successor revision, wrote this row, and prepared that revision's approval
  record — under the operator's spoken delegation of 30.08.2026 for the
  mechanical step („ich poste nichts! also du kannst das freigeben!").
  The operator ruled; he did not type the record.

## Consequences

- The rail loses two rooms and gains none. Inside a page, **one door per
  fact**: a fact that lives in another room gets exactly one door beside it
  (the degraded picker's "Connect one in Settings →"); the queue line is not
  a door but the fact itself, unfolding in place; every other cross-room link — empty-state buttons, reference
  cards, a second link to the same room — goes, because the rail is that door.
- Who fills a role has two homes only — the start sheet (this run) and
  Settings (this project: three model defaults
  chosen from the host's registry) plus the workflow's own optional pin —
  and the graph never names an agent or a difficulty; the catalog card names
  the provider by its mark and nothing of the project's choices. Which models
  exist is configuration (the registry); which one answered is evidence
  (requested + confirmed, #434 owns the field).
- **The V3 grammar must carry the role fields** — `difficulty` (closed enum
  `{1, 2, 3}`, default 2), `kind` (`build | review`), optional
  `family_differs_from` (a role name of the same workflow) and the optional
  pin — with a guard that refuses any other form; **the host gains a
  versioned model registry per provider (exact ids with discovery and the
  dry-run check), the project record gains three model
  defaults (difficulty → exact id · Account), and the occupancy per lineage
  is dropped** — a schema hop after #666 (hop 37); ADR 0006 (node
  vocabulary) and ADR 0018 (§1, the casting sentence) need an amendment
  naming them; #434 carries requested and confirmed id into the receipt. Those are follow-ups **owned by #711**, not decided here; this
  record only fixes what the picture shows.
- Answering a wait has one affordance (the stage) in two places (workbench,
  run); the inline board answer and the pinned card collapse into it.
- Restating becomes a measured defect (0 hits of the named patterns), not a
  taste.
- The frontend's three dialog implementations collapse into one stage-shaped
  dialog; the six Retry/Discard copies into one error pattern.
- Requirement 0003's successor carries §5; the predecessor remains in
  revision history.
- Nothing here is built by accepting it; PRODUCT.md owns what is.

## Open decisions for the operator

1. **The door from the workbench to a run's own view** stands on a decision's
   stage and on every row of the living shelf; the conversation that used to
   carry a second one is gone (amendment 06.09.2026, below).
2. **Where reconciliation lives.** `ReconciliationActionCard` had no room in
   this picture and fell with the V1/V2 surface (#924); it is repair, not
   work. Candidate: a stage raised by the run that owns the effect, with the
   written-answer form.
3. **Autonomy and login** (#82, #106) — the rail reserves no place until they
   are decided; #557's settings layer is the Settings room as far as model
   defaults go.

## Required proofs before the picture is accepted as the owner

All measured on the rendered picture at 1280 and 390, light and dark, with the
scripts in [`0003-ziel-ui-gates/`](../requirements/0003-ziel-ui-gates/)
(`shoot-v8.mjs`, `measure-v8.mjs`, `rm-check.mjs`, `measure-ear.mjs`,
`count-restating.mjs`, run with Playwright from `frontend/`); the same gates apply to every built
surface that claims this picture as its standard.

- **Contrast**: every text node and every input placeholder meets WCAG 2.2 AA
  (4.5:1; 3:1 for large text) in both schemes — 0 failures.
- **Targets**: every interactive element is at least 24 × 24 px (`--tap`) — 0
  below.
- **One moving element**: no frame has more than one animated element; frames
  without a working hand have none.
- **Reduced motion**: with `prefers-reduced-motion: reduce`, 0 animations.
- **Restating**: 0 hits of the named patterns — captions under controls,
  optionality as a word, purpose ledes, shelf titles above self-explaining
  elements, the state word beside its colour, an agent or a difficulty under a
  node, "you" at a gate, a loop bound as prose, a provider twice on a card, a dropdown where
  chips belong, a state pill beside the button that is the state, zero-count
  chips on an empty room. A new pattern found in review is added to the
  script, never argued away.
- **No page overflow**: `scrollWidth` equals the viewport at 1280 and 390;
  anything wider sits inside its own faded scroll container (graph, log,
  table).
- **The terminal stands on the workbench at both widths**, taking the room's
  full width where the room is narrow.
- **Click budgets** (picture §07): answer the open
  decision 1, any other 1 + 1; find the running run 0; open a finished result
  1 from the workbench, 2 from History; start by hand 4 (+1 for a work item);
  import 1 by drop, 3 by button; cancel 3; connect a source 3 + one address;
  see the queue 1, admit an item +1.
  A surface exceeding its budget is a defect (REQ-UIQ-08).
- **(2026-09-02 amendment, #1037)** The five `0003-ziel-ui-gates/` scripts
  named above (`shoot-v8.mjs`, `measure-v8.mjs`, `rm-check.mjs`,
  `measure-ear.mjs`, `count-restating.mjs`) are removed: each measured the
  static mockup file against itself and had zero references from any workflow,
  `package.json`, or `scripts/`. The enforcement of these required proofs
  lives in three CI-run tests instead: `frontend/tests/app/uiQuality.test.ts`
  (component-level: core surfaces render their owned display-copy strings,
  never a hardcoded string, through Testing Library); `frontend/tests/e2e/ui-quality.spec.ts`
  (e2e: axe-core accessibility violations, pseudo-locale copy survival, and
  the Workbench control inventory at desktop and 390px); and
  `frontend/tests/e2e/uiq-budget.spec.ts` (e2e, at 390 and 1280:
  answer-a-decision, find-the-running-run and read-run-standing in the
  Workbench/Run view; open-a-finished-run from History; open-node-log; find-by-
  search, open-tile and reach-Start — Catalog + card until Start is in view,
  not §07's 4-click start-by-hand / Start-run remainder — and import-via-
  button in Catalog; connect-a-source in Settings). §07 budgets this spec does
  not assert — start-by-hand's own remainder, import-by-drop, open-a-finished-
  run from the Workbench (only History's path is asserted), cancel, and
  see-the-queue / admit-an-item — join the same open question below. Whether
  the removed scripts measured anything beyond what these three tests now
  cover — contrast, tap-target size, one-moving-element, reduced motion, page
  overflow, and the §07 budgets just named — is an open question tracked on
  #435 (REQ-UIQ-05/08/13), not a gap filled by a new spec here.

## Out of scope and stop conditions

This record does not decide: the token skin (REQ-UI-14 owns the separation);
the catalog window's data contract (#659); login and autonomy (#82, #106);
which providers list their models through their CLI and how the dry-run
check is performed (the adapter's, not the picture's); the conductor's protocol (#658); the source
adapter's capabilities (#728); the exact bytes of the 0003 successor
revision (its own approval).

Stop implementation on: a Board or Workflows route; a second door to the same
fact inside a page; a sentence that restates a state an element already
shows; an agent name or a difficulty under a graph node; a role filled by
role name or by a named class instead of by its difficulty against the model
defaults; a per-role exception or any durable override outside the workflow's
pin; a model list hard-wired in code instead of the registry; a rating,
ladder or threshold that chooses a model for the operator; an alias where an exact id belongs; a receipt without the confirmed
id; the queue anywhere but the workbench's line; a family rule hard-wired in the house instead of declared
by the workflow; build/review flags on a configuration; buttons replaced by a dropdown for an
answer given once (dropdowns for values chosen per role, work item or account
are fine); a wait answered anywhere but a stage; a hash on a card.

## Amendment, 06.09.2026: the terminal seat replaces the conversation

The record above stands as decided; this amendment names what #1099 changed
about one of its blocks, so the two are read together rather than one silently
rewriting the other.

The web-chat conductor was a rebuilt agent CLI inside a browser page. The
agent CLIs already work, so the workbench now holds the operator's own
terminal — a tmux session with the CLI in it, reached over loopback through
ttyd — where the conversation and its ear used to be. The stage, the living
shelf, the queue line and every other block are untouched, and the picture
owner's own `#v8-22-seat` frames (blessed with PR #1272) are the drawing.

What this amendment retracts from the record above: the conversation as a
block of the workbench, the ear as the place an answer is typed, "the
conversation is a run" as a door, and the send-a-message click budget. What it
keeps: "you speak to the workshop, you do not visit a chat page" — truer of a
terminal than of a chat page — and every sentence about the stage, the rows,
the rail and the rooms.

One expectation line of #1099's list was overruled by the operator the day it
was built (06.09.2026, "Terminal geht nicht auf dem Phone? Das muss gehen"):
there is no width at which the workbench refuses the terminal. On a phone the
terminal takes the room's full width and asks its own client for smaller type;
it is never replaced by a refusal for being narrow.

## Supersedes

The four-surface rail of REQ-UI-01 as ruled on #516 (22.08.2026) and Mockup
v5 as gestalt owner (REQ-UI-18), through the successor identifiers in §5. It
amends HEART "The place" (two paragraphs, one of them the #684 text
retracted) and the second sentence of HEART "The ear" as quoted in §5; "you
do not visit a chat page" stands. The amendment of 06.09.2026 above replaces
the conversation and its ear with the terminal seat (#1099). It creates one explicit exception to ADR
0018 §1 — the workflow's optional model pin — to be written into ADR 0018 as
an amendment; otherwise ADR 0018 applies unchanged.
