from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects import sqlite as sqlite_dialect
from sqlalchemy.engine import Engine
from sqlalchemy.schema import CreateIndex, CreateTable

from atelier2.adapters.dbos.published_queue_shapes import (
    PUBLISHED_QUEUE_ITEMS_TRANSITION_BEFORE_RELEASE,
    PUBLISHED_QUEUE_LAUNCH_BINDINGS_NO_UPDATE_BEFORE_RELEASE,
)
from atelier2.adapters.dbos.published_schema_shapes import (
    PUBLISHED_QUEUE_ITEMS_STATE_TRANSITION_TRIGGER_BEFORE_OBSERVATION,
    PUBLISHED_TABLE_INDEXES,
    PUBLISHED_TABLE_SHAPES,
    PUBLISHED_WAIT_ANSWER_TRIGGERS,
)
from atelier2.adapters.dbos.queue_tables import (
    queue_dependency_edges,
    queue_items,
    queue_launch_bindings,
    queue_project_policy_revisions,
    queue_proposal_revisions,
)
from atelier2.adapters.dbos.table_vocabulary import (
    closed_vocabulary_sql,
    metadata,
    rfc3339_utc,
    rfc3339_utc_or_null,
)
from atelier2.adapters.github.composition import migrate_v44_github_source_location
from atelier2.contracts.agent_permissions import (
    PermissionAuthority,
    PermissionEffect,
    PermissionScopeKind,
)
from atelier2.contracts.agents import (
    MAXIMUM_AGENT_FIELD_CHARACTERS,
    MAXIMUM_PROVIDER_ID_CHARACTERS,
    MAXIMUM_SIGNED_INT64,
)
from atelier2.contracts.artifacts import MAXIMUM_ARTIFACT_BYTES
from atelier2.contracts.catalog_v3 import MAXIMUM_LINEAGE_DISPLAY_NAME_CHARACTERS
from atelier2.contracts.definition_sources import (
    MAXIMUM_DEFINITION_SOURCE_ACTOR_CHARACTERS,
    MAXIMUM_DEFINITION_SOURCE_SELECTIONS,
    MAXIMUM_GIT_OBJECT_NAME_CHARACTERS,
    MAXIMUM_REPOSITORY_LOCATION_CHARACTERS,
    MAXIMUM_REPOSITORY_PATH_CHARACTERS,
    MAXIMUM_REPOSITORY_REF_CHARACTERS,
    MAXIMUM_SELECTION_PATTERN_CHARACTERS,
    MINIMUM_GIT_OBJECT_NAME_CHARACTERS,
    DefinitionSourceAccess,
    DefinitionSourceKind,
)
from atelier2.contracts.executions import WaitAnswerAttributionKind
from atelier2.contracts.hashing import frame
from atelier2.contracts.host_configuration import (
    MAXIMUM_CONNECTION_ACTOR_CHARACTERS,
    MAXIMUM_CREDENTIAL_DIRECTORY_CHARACTERS,
    MAXIMUM_EXACT_MODEL_ID_CHARACTERS,
    MAXIMUM_PROJECT_ID_CHARACTERS,
    MAXIMUM_PROJECT_ROOT_PATH_CHARACTERS,
    MAXIMUM_SOURCE_ADDRESS_CHARACTERS,
    MAXIMUM_SOURCE_KIND_CHARACTERS,
    MAXIMUM_SOURCE_REFERENCE_CHARACTERS,
    ConnectionActor,
    ProjectId,
    ProjectSourceConnectionLifecycle,
    ProjectSourceConnectionRevision,
    ProjectSourceId,
    SourceAddress,
    SourceConnectionAuthMethod,
    SourceKind,
    SourceReference,
)
from atelier2.contracts.queue_projection import (
    QueueProposalSource,
)
from atelier2.contracts.revisions_v3 import RevisionKind
from atelier2.contracts.runs import FIRST_ROUND_ORDINAL
from atelier2.contracts.workflow_formats import WorkflowFormatVersion


@dataclass(frozen=True)
class ProductSchemaHandoff:
    version: int
    fingerprint_sha256: str


# Hop 54 gives a launch binding the ending of its run, the restart ordinal it
# was written at, and a key that admits its successor, so an item whose run
# failed or was cancelled is given back to the sweep rather than bound forever.
_HOP_PREDECESSOR_VERSION = 54
SCHEMA_VERSION = _HOP_PREDECESSOR_VERSION + 1
_VERSION_NINE = 9
_VERSION_TEN = 10
_VERSION_ELEVEN = 11
_VERSION_TWELVE = 12
_VERSION_THIRTEEN = 13
_VERSION_FOURTEEN = 14
_VERSION_FIFTEEN = 15
_VERSION_SIXTEEN = 16
_VERSION_SEVENTEEN = 17
_VERSION_EIGHTEEN = 18
_VERSION_NINETEEN = 19
_VERSION_TWENTY = 20
_VERSION_TWENTY_ONE = 21
_VERSION_TWENTY_TWO = 22
_VERSION_TWENTY_THREE = 23
_VERSION_TWENTY_FOUR = 24
_VERSION_TWENTY_FIVE = 25
_VERSION_TWENTY_SIX = 26
_VERSION_TWENTY_SEVEN = 27
_VERSION_TWENTY_EIGHT = 28
_VERSION_TWENTY_NINE = 29
_VERSION_THIRTY = 30
_VERSION_THIRTY_ONE = 31
_VERSION_THIRTY_TWO = 32
_VERSION_THIRTY_THREE = 33
_VERSION_THIRTY_FOUR = 34
_VERSION_THIRTY_FIVE = 35
_VERSION_THIRTY_SIX = 36
_VERSION_THIRTY_SEVEN = 37
_VERSION_THIRTY_EIGHT = 38
_VERSION_THIRTY_NINE = 39
_VERSION_FORTY = 40
_VERSION_FORTY_ONE = 41
_VERSION_FORTY_TWO = 42
_VERSION_FORTY_THREE = 43
_VERSION_FORTY_FOUR = 44
_VERSION_FORTY_FIVE = 45
_VERSION_FORTY_SIX = 46
_VERSION_FORTY_SEVEN = 47
_VERSION_FORTY_EIGHT = 48
_VERSION_FORTY_NINE = 49
_VERSION_FIFTY = 50
_VERSION_FIFTY_ONE = 51
_VERSION_FIFTY_TWO = 52
_VERSION_FIFTY_THREE = 53
_VERSION_FIFTY_FOUR = 54
_VERSION_FIFTY_FIVE = 55
# docs/PRODUCT.md "Stage: prototype": no store compatibility is owed.
# Every published prototype schema remains a predecessor; runtime never migrates it.
_OFFLINE_CUTOVER_VERSIONS = frozenset(range(1, SCHEMA_VERSION))
# V9 product tables equal V8. V10 adds the thin catalog/receipt foundation. V11
# closes the artifact/output and now-retired access store shape.
# V12 adds append-only catalog alias and retirement histories. V13 gives the
# context-package manifest, the node-execution-request preimage and the run
# configuration snapshot durable, immutable homes, and records the run
# configuration revision a supervised V3 run was started under. V14 gives the
# order a run was started with a durable, immutable home, so one published
# revision serves every order instead of one revision per distinct input. V15
# adds the immutable evidence of one redeemed tool grant: which command the
# attempt ran, how it ended, and what it wrote. V16 gives an agent completion a
# home for the receipt hash its event preimage now binds, so a recomputed
# terminal hash proves under which binding the attempt ran. V17 admits
# OUTPUT_SCHEMA_REFUSED as a second attempt failure code, so a schema-refused
# output ends its attempt under its own name instead of borrowing the process
# exit's or killing the driver. V18 admits FAILED as a run state, so a line
# whose open node paths have terminally failed ends under the node's own
# reason instead of standing STARTED with nothing to continue it. V19 gives
# content-addressed material a durable, immutable home, so an order larger than
# the inline bound travels as the address of bytes published once instead of not
# travelling at all. V20 gives the round a declared loop is turning a durable
# home on the run and on every event and agent receipt it writes, keys a node
# execution request by the execution rather than by the request it repeats, and
# drops the receipt key that said one agent receipt per node per run -- a
# sentence that stopped being true when a node could run twice. V21 admits
# headless_with_tools as a requested capability, so a configuration may durably
# ask for an executor whose invocation carries the provider's own tools. V22
# records when a run, attempt, or event was written, as RFC 3339 UTC.
# Predecessor rows stay NULL — no invented time. V23 admits AGENT_REFUSED as a
# third attempt failure code. V24 admits PROJECT_VERIFICATION_FAILED as a
# fourth, so a granted verification that exits nonzero ends under its own name.
# V25 gives the host its live-versioned configuration channel, first entry
# project id → root path, as append-only revisions. V26 adds recommended
# occupancy per workflow lineage as a second family on that channel: revision
# header and role bindings, append-only.
# V27 gives Core the exact Runner generation/invocation binding and the durable
# semantic evidence acceptance phase. V28 removes the receipt-access table that
# never acquired a writer. V29 gives the queue projection its durable admission
# row: one item identified by its project and tracker reference, CAS-guarded
# through OBSERVED to ADMITTED under a named catalog workflow binding. V30
# admits CANCELLED as a run ending, so an operator's run-cancel command (#439)
# ends the run under its own word instead of standing STARTED with nothing to
# continue it or borrowing FAILED for something the operator chose. V31 gives
# the webhook delivery decision (#433 phase 1) its one durable cursor: a
# singleton row on the attention feed, naming the last delivered event's
# identity and a CAS revision an advance must name to move it, guarded the
# same way the queue projection's admission row already is. No writer built
# above it yet -- phase 1 proves the cursor and the delivery decision behind
# a fake transport; phase 2 gives it a real network edge and a loop.
# V32 admits the never-launched runner-lease cancel terminal transition
# (#584): an operator run-cancel on a runner-lease-bound attempt leased but
# never launched (runner_manifest_id set, runner_invocation_id IS NULL) may
# end CANCELLED under disposition NEVER_LAUNCHED, its runner binding preserved
# and no evidence fabricated -- proved only by a won lease withdraw. It rewrites
# no row and moves no table shape; only the `agent_attempts_state_transition`
# trigger gains one branch, so the hop is a trigger swap.
# V33 gives the project-source connection record (#567, ADR 0010 decision 2)
# its durable home as a third append-only family on the host configuration
# channel: project id, source kind, an opaque source address only the
# connected platform adapter interprets, a credential-directory reference --
# never a credential value -- the chosen auth method, and the connecting
# actor.
# V34 keys a wait answer by the node execution it answers and records the round
# that execution stands in (#671). The predecessor key -- run and node -- said
# one answer per node per run forever, which stops being true the moment a
# declared loop turns a Wait a second time. Every stored answer already carries
# its execution id, and round one derives byte-identically to the roundless
# derivation, so the key moves losslessly and every carried row is filled with
# round one. It rewrites no bytes of an answer and reinterprets nothing; the
# cutover is offline like every other, so "preserved" means preserved by the
# migrate command and never by a running store.
# V35 admits WAIT_CANCELLED as a run event kind (#668). A run resting in
# WAITING_INPUT had no way to end: no attempt existed for an operator's cancel
# to stop, so the run stood owed an answer forever against ADR 0006's sentence
# that a run cancel drives it to one cancelled receipt. The kind is the
# cancellation's own attestation -- it carries the minted command id as its
# payload and is fenced by the node execution it names -- so the run's terminal
# hash folds over a real event instead of a lift inventing one. Only the
# vocabulary widens; every stored row keeps its bytes, its key and its meaning.
# V36 re-scopes one run-event key to the round (#658).
# `run_events_legacy_kind_unique` said one event of a kind per node per run for
# every event no agent attempt owns, which stopped being true the moment a
# declared loop could turn a Wait a second time: round two's WAITING_INPUT names
# the same node and kind as round one's. `run_events_round_kind_unique` says the
# same thing about one round, so every log the predecessor holds satisfies it.
# The round-scoped key is not redundant beside the execution-keyed one: an
# execution id is derived, and only a writer that derived it right lands under
# it, while `_existing_event` reads a round back by run, revision, node and
# round and expects at most one row. The hop is two DDL statements and the
# version CAS -- SQLite moves an index without reading a row, so no row is
# rewritten, no table is parked and the table text does not move.
# V37 gives an attempt the address of its transcript (#666).
# `transcript_artifact_hash` is one nullable content address: what the executor
# decoded of the provider's own stream, bounded and redacted before it was
# stored, kept as an artifact in the same write that ends the attempt. It is a
# pointer rather than the bytes because `artifacts` already owns material read
# whole, and it is a column on the attempt rather than a keyed row because one
# attempt has one transcript and no other coordinate identifies it. Every
# predecessor row carries NULL, which is the exact statement that no transcript
# was decoded for that attempt -- never that the attempt took no steps.
# Three guards make the pointer mean what it says rather than merely resemble
# it. The reference onto `artifacts` refuses an address no bytes answer, because
# a 64-hex column with nothing behind it points at evidence that may never have
# existed. The CHECK admits it only on a terminal row, because a transcript is
# what an attempt DID and a live attempt has not finished doing it. The
# state-transition trigger lets it go from absent to present exactly once, so
# the one column of a terminal row an update could still have swung at another
# artifact cannot be swung at all.
# V38 admits ABANDONED as an effect-intent ending (#705). A prepared intent moves
# because a workflow moves it, and when none will move it again the restart
# routes it to WAITING_RECONCILIATION -- a transition that lifts a STARTED run to
# the operator's door. A run that has already ended has nothing left to lift, so
# before this word such an intent stood PREPARED forever behind a door that
# refused it. ABANDONED says the run's own ending on the intent: reached only
# from PREPARED, never left, claiming neither a receipt nor an absence, keeping
# the prepared request bytes readable. `effect_intents_abandonment` admits
# exactly that one transition and refuses every other write touching the word, so
# no confirmed intent can be overwritten by it and no abandoned one revived, and
# `effect_intents_no_abandoned_insert` closes the other door: a row born
# ABANDONED would be an ending no run ever reached. Only the vocabulary widens;
# every stored row keeps its bytes, its key and its meaning.
# V39 admits CANDIDATE_CAPTURE_FAILED as an attempt failure code (#642). An
# attempt's work lives only in the directory it was made in, so it is kept as a
# candidate before the attempt is completed; a capture that fails leaves the work
# lost, and every code published before this one would have said something untrue
# about how -- that a process died, or that a form refused what no form saw. The
# widened CHECK admits the word on the table, and both FAILED transitions of
# `agent_attempts_state_transition` admit it: the armed local-process attempt
# that reaches this ending today, and the runner-evidence one beside it. Which
# carrier can reach an ending is the carrier's business, not the vocabulary's --
# today no runner-lease attempt captures a candidate, because it is refused a
# pinned project source before it starts, but a schema that encoded that refusal
# as a narrower word list would have to be migrated again the day the carrier
# changes, and until then it would hold two disagreeing answers to "which codes
# exist". Only the vocabulary widens; every stored row keeps its bytes, its key
# and its meaning.
# V40 removes occupancy per workflow lineage and adds two append-only host
# configuration families: exact model ids per provider and three optional
# project defaults keyed by difficulty. Occupancy rows are deliberately not
# migrated because the replacement has no lineage dimension.
# V41 gives a fork command its immutable lineage header, strict-prefix source
# references, and confirmed-effect replay fences. Effect receipts admit one
# additional confirmation source only when all four columns identify an existing
# source receipt from an earlier run.
# V42 persists the closed effect operation on intents and receipts. V43 gives
# schema-refused attempts their immutable evidence record. V44 gives the queue
# append-only policies, proposals, exact-revision dependency edges, and launch
# reservations while preserving the admission-only V43 rows as legacy review.
# V45 gives a source a durable identity and lifecycle, records the connection
# instant for new revisions, and preserves every legacy revision with an absent
# instant and one deterministic source identity per project history.
# V46 makes both sides of answer attribution explicit. Every WAITING_INPUT head
# records the closed actor it expects; every new answer records that actor, while
# predecessor answers carry the named LEGACY_UNATTRIBUTED kind and no invented
# actor.
# V48 gives the queue item the tracker title as it was last observed, the marker
# of when it was observed, and the marker of when import derived the item
# retired by set difference (ADR 0016, 2026-09-01 amendment). All three are
# dated observations, never core-asserted facts: a queue row keeps them NULL
# until an import writes them, and none of the three participates in the
# proposal or admission state machine.
# V49 adds the three tables a definition source needs and moves none: the
# registration and its ordered selections, and one provenance row per path a
# source has delivered. `catalog_source_intakes` carries the revision kind
# beside the hash because the published revision hash deliberately excludes the
# kind, so only the pair names a publication.
# V51 adds `permission_receipts`, the authorisation ledger of ADR 0020 §2: one
# append-only row per provider permission question, keyed by the attempt and the
# correlation id the question was minted with, bound to the policy revision its
# answer stands on.
# V52 gives a project policy the optional workflow lineage, priority rank and
# automation disposition a labelled item with no proposal is proposed under,
# and every proposal revision the source that wrote it. A stored proposal
# crosses as OPERATOR: the operator's own door is the only writer that existed
# before this hop.
# V55 gives a launch binding the ending of its run and the restart ordinal it
# was written at, keys it by the proposal revision so a restarted item keeps
# every binding it held, and admits one proposal revision advance per release.
# The hop number is movable: `_HOP_PREDECESSOR_VERSION` is the one
# constant to restack.
_PRODUCT_SCHEMA_FINGERPRINT_SHA256 = {
    7: "0bf32217a1254ee64d84c4ed629244600d542211ac655e4405a0df51f857081b",
    8: "6ba76214cb567ffcdab46e5a3ae00fc10824b962f16a8036ce90590be0b79b38",
    9: "6ba76214cb567ffcdab46e5a3ae00fc10824b962f16a8036ce90590be0b79b38",
    10: "4a7bbd9bf07880868aa2f7ddae3e7262eb270f711d4fdc420f902457817bfff7",
    11: "18dead2ab36c15bf61fa1b1bb5fed3b5a1075dc773d83d8b57c00c05c84178ef",
    12: "feef25b171e305bb9a3a9637cc4d0fb1c8dec4a4a7a9813e060ccf12598a5cc7",
    13: "5782fdc1331c52f3f04097f6a2a6d416ab528d6ee8a6546a7d6435ae9d11c175",
    14: "6cf56491322e716fce9be2310584ed2b92533961b8fda341bfcc317182432f0a",
    15: "375e81d1c8967053951d1be0cab19cee274e35272f364feae15ec3413eb3c9b9",
    16: "97605fb330cb6382d52a554d644015f631cccea3759c04c27de3ca5f1fea9c3a",
    17: "2f3a11d0b4d67e375259ca732c7243c95d19fa763e03785b0bd4a83c1b1359d2",
    18: "c60275544c9984adccff79e3a4f5ab6eeab5ea1683306adf1d2faa7dbb51e29d",
    19: "a861d9087da05c112f88ae8ec573f57338b5ef1d04f36553922c505127b34298",
    20: "09752981999444ee4129cfe29b7322b79d2ff378f91d1af5050342eff78b8637",
    21: "6c4705f2960d1669a596ae8f3c857dd0ac15c4c94b71b4bb5998d1bac672cefe",
    22: "72aa8f76942197b704f07c156adbb1e46c3b069ce16a53c6d95a067827966387",
    23: "6d8a3af85ecc40781c6eea454e33ae625de1cf6d8726ca5c502cdcc33eb2c124",
    24: "ba573ba80dbdbb5d9b2a93bc6958b7544838915be3e0f5fc816cacc718dfe9c8",
    25: "91d8889ce6239855c894b89ab658188d9b13927dedb1cc905dacdc151a485842",
    26: "0af3ca8bbbbe06a56c56bb0988de384fde2a807b1e409152a02e1e226e917ab8",
    27: "7f929ab33c6b8742ff24a301bb13cb1f49a4ced2d96b52b97dbb26196ebd2ac4",
    28: "8e15796b7361796fc5c70e9c1682ddf58b967dea7fb112127366cfca600c9b36",
    29: "06e1d67be5f39569e7661321c063f7ea84c95efba2906519e7473a6f2016b640",
    30: "1229c61ee62c20531cb31ed324a3b822646d56899f30be62ab1c6abebf325c3c",
    31: "60d98794edd55744b3ec2cc4f4d7b9bf7a23106b4b7f0d4b9a009042d054a419",
    32: "0cdbeaf303f2839661930234a508e141cd995b8552def9b426a52aaad1eab84e",
    33: "f634d04c6cc525147ead8aa0dad8ef728189a6ef9554049c8a2aad56f3caeea8",
    34: "28dab0f4a152d7be66fa699d1123fdd130a94fe80ad705c19330f075e4fdd85a",
    35: "29df9a195316ce94527be2c906e4dc4104e00b2cb16caa9bfada17fecb5a21d5",
    36: "c9f4b5d99a9ff8e33796e36151b66f00175eceaa797e30461bf6e01264266ce8",
    37: "e41cf318212e0a79d6605413b5818ef68d6245baaf05a53b888b8aac40131a13",
    38: "aebd8b6bad8a719864f0c02828db643dd3dcbe7c89198beb6a8c1c4c30100824",
    39: "3c0cc05dd977fd61d2c88d78ba7566fdc0146e2d7af27df61aea636a4ac2c4be",
    40: "d8d7b89cc0cacd15dfde84bf15f796f0e03d9b571c26be0309ed87a60960071d",
    41: "7c4bc13ceb1db7533bfdf9697c1e6b262032a516275b488eac73af9969446b68",
    42: "d2f874edd0dbbecb677b284db8e41cd3a681fae99703d126764bc90fa0cf7865",
    43: "f7d299ab865b87ca47a399d4897f8c7b273085c4d206fac9eb882d47198b9782",
    44: "b8a176e76092a24fa0c8ac1caafdd69e57f4ff404ecb5560a1dd426d32a3ee9b",
    45: "39d0811369f0b7a4b248448042623ecde0d290e95d191d75c32a9faf538fffa5",
    46: "1428683e38b4cce26b866e02ef1afa974a2c3208e26606f8792d0d48f0b1a43b",
    47: "d7987152a11a2702808b5fb1b71c0891e1c6724519435e87ee840cc235c00e39",
    48: "ecf4b2aba21f7225f121a3afc128d76e9ce10801c83121a93712f39320704653",
    49: "01930b9de9fc8804ed1be5ec34dc02df926373cb95f20319f6e38d92b1c39ea2",
    50: "bb34288b35fbf4fe059960323b7a92ee4e5473b5a945e697c0f4b9fe29c6d8a9",
    51: "2b0be085b59e160db8b9d925bbb889205b32a2bbd45fcad673277b2b229fd622",
    52: "6121453b26de9913e212d726b95d74def93c0a754e25eadfadbe77f7c7c432e2",
    53: "038b3e7f5ca011d78e6a1013d7b3fde96b8056165106a2c71898e3353e9da881",
    54: "13edd2cba8b5bca12e4c6c679aa7a5974d36693cd6b0e0e8da132736afe56aa4",
    55: "a015ba3b7fd7d3fc654eb5cfad1cd672748ccc65f7c0229480a74390eff56759",
}
V9_SCHEMA_HANDOFF = ProductSchemaHandoff(
    _VERSION_NINE,
    _PRODUCT_SCHEMA_FINGERPRINT_SHA256[_VERSION_NINE],
)
V10_SCHEMA_HANDOFF = ProductSchemaHandoff(
    _VERSION_TEN,
    _PRODUCT_SCHEMA_FINGERPRINT_SHA256[_VERSION_TEN],
)
V11_SCHEMA_HANDOFF = ProductSchemaHandoff(
    _VERSION_ELEVEN,
    _PRODUCT_SCHEMA_FINGERPRINT_SHA256[_VERSION_ELEVEN],
)
V12_SCHEMA_HANDOFF = ProductSchemaHandoff(
    _VERSION_TWELVE,
    _PRODUCT_SCHEMA_FINGERPRINT_SHA256[_VERSION_TWELVE],
)
V13_SCHEMA_HANDOFF = ProductSchemaHandoff(
    _VERSION_THIRTEEN,
    _PRODUCT_SCHEMA_FINGERPRINT_SHA256[_VERSION_THIRTEEN],
)
V14_SCHEMA_HANDOFF = ProductSchemaHandoff(
    _VERSION_FOURTEEN,
    _PRODUCT_SCHEMA_FINGERPRINT_SHA256[_VERSION_FOURTEEN],
)
V15_SCHEMA_HANDOFF = ProductSchemaHandoff(
    _VERSION_FIFTEEN,
    _PRODUCT_SCHEMA_FINGERPRINT_SHA256[_VERSION_FIFTEEN],
)
V16_SCHEMA_HANDOFF = ProductSchemaHandoff(
    _VERSION_SIXTEEN,
    _PRODUCT_SCHEMA_FINGERPRINT_SHA256[_VERSION_SIXTEEN],
)
V17_SCHEMA_HANDOFF = ProductSchemaHandoff(
    _VERSION_SEVENTEEN,
    _PRODUCT_SCHEMA_FINGERPRINT_SHA256[_VERSION_SEVENTEEN],
)
V18_SCHEMA_HANDOFF = ProductSchemaHandoff(
    _VERSION_EIGHTEEN,
    _PRODUCT_SCHEMA_FINGERPRINT_SHA256[_VERSION_EIGHTEEN],
)
V19_SCHEMA_HANDOFF = ProductSchemaHandoff(
    _VERSION_NINETEEN,
    _PRODUCT_SCHEMA_FINGERPRINT_SHA256[_VERSION_NINETEEN],
)
V20_SCHEMA_HANDOFF = ProductSchemaHandoff(
    _VERSION_TWENTY,
    _PRODUCT_SCHEMA_FINGERPRINT_SHA256[_VERSION_TWENTY],
)
V21_SCHEMA_HANDOFF = ProductSchemaHandoff(
    _VERSION_TWENTY_ONE,
    _PRODUCT_SCHEMA_FINGERPRINT_SHA256[_VERSION_TWENTY_ONE],
)
V22_SCHEMA_HANDOFF = ProductSchemaHandoff(
    _VERSION_TWENTY_TWO,
    _PRODUCT_SCHEMA_FINGERPRINT_SHA256[_VERSION_TWENTY_TWO],
)
V23_SCHEMA_HANDOFF = ProductSchemaHandoff(
    _VERSION_TWENTY_THREE,
    _PRODUCT_SCHEMA_FINGERPRINT_SHA256[_VERSION_TWENTY_THREE],
)
V24_SCHEMA_HANDOFF = ProductSchemaHandoff(
    _VERSION_TWENTY_FOUR,
    _PRODUCT_SCHEMA_FINGERPRINT_SHA256[_VERSION_TWENTY_FOUR],
)
V25_SCHEMA_HANDOFF = ProductSchemaHandoff(
    _VERSION_TWENTY_FIVE,
    _PRODUCT_SCHEMA_FINGERPRINT_SHA256[_VERSION_TWENTY_FIVE],
)
V26_SCHEMA_HANDOFF = ProductSchemaHandoff(
    _VERSION_TWENTY_SIX,
    _PRODUCT_SCHEMA_FINGERPRINT_SHA256[_VERSION_TWENTY_SIX],
)
V27_SCHEMA_HANDOFF = ProductSchemaHandoff(
    _VERSION_TWENTY_SEVEN,
    _PRODUCT_SCHEMA_FINGERPRINT_SHA256[_VERSION_TWENTY_SEVEN],
)
V28_SCHEMA_HANDOFF = ProductSchemaHandoff(
    _VERSION_TWENTY_EIGHT,
    _PRODUCT_SCHEMA_FINGERPRINT_SHA256[_VERSION_TWENTY_EIGHT],
)
V29_SCHEMA_HANDOFF = ProductSchemaHandoff(
    _VERSION_TWENTY_NINE,
    _PRODUCT_SCHEMA_FINGERPRINT_SHA256[_VERSION_TWENTY_NINE],
)
V30_SCHEMA_HANDOFF = ProductSchemaHandoff(
    _VERSION_THIRTY,
    _PRODUCT_SCHEMA_FINGERPRINT_SHA256[_VERSION_THIRTY],
)
V31_SCHEMA_HANDOFF = ProductSchemaHandoff(
    _VERSION_THIRTY_ONE,
    _PRODUCT_SCHEMA_FINGERPRINT_SHA256[_VERSION_THIRTY_ONE],
)
V32_SCHEMA_HANDOFF = ProductSchemaHandoff(
    _VERSION_THIRTY_TWO,
    _PRODUCT_SCHEMA_FINGERPRINT_SHA256[_VERSION_THIRTY_TWO],
)
V33_SCHEMA_HANDOFF = ProductSchemaHandoff(
    _VERSION_THIRTY_THREE,
    _PRODUCT_SCHEMA_FINGERPRINT_SHA256[_VERSION_THIRTY_THREE],
)
V34_SCHEMA_HANDOFF = ProductSchemaHandoff(
    _VERSION_THIRTY_FOUR,
    _PRODUCT_SCHEMA_FINGERPRINT_SHA256[_VERSION_THIRTY_FOUR],
)
V35_SCHEMA_HANDOFF = ProductSchemaHandoff(
    _VERSION_THIRTY_FIVE,
    _PRODUCT_SCHEMA_FINGERPRINT_SHA256[_VERSION_THIRTY_FIVE],
)
V36_SCHEMA_HANDOFF = ProductSchemaHandoff(
    _VERSION_THIRTY_SIX,
    _PRODUCT_SCHEMA_FINGERPRINT_SHA256[_VERSION_THIRTY_SIX],
)
V37_SCHEMA_HANDOFF = ProductSchemaHandoff(
    _VERSION_THIRTY_SEVEN,
    _PRODUCT_SCHEMA_FINGERPRINT_SHA256[_VERSION_THIRTY_SEVEN],
)
V38_SCHEMA_HANDOFF = ProductSchemaHandoff(
    _VERSION_THIRTY_EIGHT,
    _PRODUCT_SCHEMA_FINGERPRINT_SHA256[_VERSION_THIRTY_EIGHT],
)
V39_SCHEMA_HANDOFF = ProductSchemaHandoff(
    _VERSION_THIRTY_NINE,
    _PRODUCT_SCHEMA_FINGERPRINT_SHA256[_VERSION_THIRTY_NINE],
)
V40_SCHEMA_HANDOFF = ProductSchemaHandoff(
    _VERSION_FORTY,
    _PRODUCT_SCHEMA_FINGERPRINT_SHA256[_VERSION_FORTY],
)
V41_SCHEMA_HANDOFF = ProductSchemaHandoff(
    _VERSION_FORTY_ONE,
    _PRODUCT_SCHEMA_FINGERPRINT_SHA256[_VERSION_FORTY_ONE],
)
V42_SCHEMA_HANDOFF = ProductSchemaHandoff(
    _VERSION_FORTY_TWO,
    _PRODUCT_SCHEMA_FINGERPRINT_SHA256[_VERSION_FORTY_TWO],
)
V43_SCHEMA_HANDOFF = ProductSchemaHandoff(
    _VERSION_FORTY_THREE,
    _PRODUCT_SCHEMA_FINGERPRINT_SHA256[_VERSION_FORTY_THREE],
)
V44_SCHEMA_HANDOFF = ProductSchemaHandoff(
    _VERSION_FORTY_FOUR,
    _PRODUCT_SCHEMA_FINGERPRINT_SHA256[_VERSION_FORTY_FOUR],
)
V45_SCHEMA_HANDOFF = ProductSchemaHandoff(
    _VERSION_FORTY_FIVE,
    _PRODUCT_SCHEMA_FINGERPRINT_SHA256[_VERSION_FORTY_FIVE],
)
V46_SCHEMA_HANDOFF = ProductSchemaHandoff(
    _VERSION_FORTY_SIX,
    _PRODUCT_SCHEMA_FINGERPRINT_SHA256[_VERSION_FORTY_SIX],
)
V47_SCHEMA_HANDOFF = ProductSchemaHandoff(
    _VERSION_FORTY_SEVEN,
    _PRODUCT_SCHEMA_FINGERPRINT_SHA256[_VERSION_FORTY_SEVEN],
)
V48_SCHEMA_HANDOFF = ProductSchemaHandoff(
    _VERSION_FORTY_EIGHT,
    _PRODUCT_SCHEMA_FINGERPRINT_SHA256[_VERSION_FORTY_EIGHT],
)
V49_SCHEMA_HANDOFF = ProductSchemaHandoff(
    _VERSION_FORTY_NINE,
    _PRODUCT_SCHEMA_FINGERPRINT_SHA256[_VERSION_FORTY_NINE],
)
PRODUCT_SCHEMA_HANDOFF = ProductSchemaHandoff(
    SCHEMA_VERSION,
    _PRODUCT_SCHEMA_FINGERPRINT_SHA256[SCHEMA_VERSION],
)

# Every table below states its own hex-64 hash columns and simple non-empty
# columns with the same CHECK text; one constant per column name gives that
# repeated text a single owner instead of a copy at each table. Some are read
# by a table declared earlier in this module than the table they name (a
# SQLAlchemy string-form ForeignKey admits a forward reference), so they are
# declared once, here, before the table section starts.
_REVISION_HASH_HEX64_CHECK = (
    "length(revision_hash) = 64 AND revision_hash NOT GLOB '*[^0-9a-f]*'"
)
_WORKFLOW_REVISION_HASH_HEX64_CHECK = (
    "length(workflow_revision_hash) = 64 "
    "AND workflow_revision_hash NOT GLOB '*[^0-9a-f]*'"
)
_AGENT_CONFIGURATION_REVISION_HASH_HEX64_CHECK = (
    "length(agent_configuration_revision_hash) = 64 "
    "AND agent_configuration_revision_hash NOT GLOB '*[^0-9a-f]*'"
)
_REQUEST_HASH_HEX64_CHECK = (
    "length(request_hash) = 64 AND request_hash NOT GLOB '*[^0-9a-f]*'"
)
_NODE_EXECUTION_ID_HEX64_CHECK = (
    "length(node_execution_id) = 64 AND node_execution_id NOT GLOB '*[^0-9a-f]*'"
)
_RECEIPT_HASH_HEX64_CHECK = (
    "length(receipt_hash) = 64 AND receipt_hash NOT GLOB '*[^0-9a-f]*'"
)
_ATTEMPT_ID_HEX64_CHECK = (
    "length(attempt_id) = 64 AND attempt_id NOT GLOB '*[^0-9a-f]*'"
)
_VALUE_HASH_HEX64_CHECK = (
    "length(value_hash) = 64 AND value_hash NOT GLOB '*[^0-9a-f]*'"
)
_SCHEMA_REVISION_HASH_HEX64_CHECK = (
    "length(schema_revision_hash) = 64 AND schema_revision_hash NOT GLOB '*[^0-9a-f]*'"
)
_RUN_ID_NONEMPTY_CHECK = "length(run_id) > 0"
_LOGICAL_KEY_NONEMPTY_CHECK = "length(logical_key) > 0"
_ACTOR_NONEMPTY_CHECK = "length(actor) > 0"
_NODE_ID_NONEMPTY_CHECK = "length(node_id) > 0"
_ACTIVATED_AT_NONEMPTY_CHECK = "length(activated_at) > 0"
_PROVIDER_ID_LOWERCASE_START_CHECK = "provider_id GLOB '[a-z]*'"
_PROVIDER_ID_ALLOWED_CHARACTERS_CHECK = "provider_id NOT GLOB '*[^a-z0-9._-]*'"
_POSITION_NONNEGATIVE_CHECK = "position >= 0"

# Table-qualified column names this module's own tables use as string-form
# foreign key targets from more than one dependent table; one constant per
# name gives the reference a single owner instead of a copy at each user.
_WORKFLOW_REVISIONS_REVISION_HASH = "workflow_revisions.revision_hash"
_RUN_CONFIGURATION_REVISIONS_REVISION_HASH = "run_configuration_revisions.revision_hash"
_RUNS_REVISION_HASH = "runs.revision_hash"
_RUNS_RUN_ID = "runs.run_id"
_AGENT_CONFIGURATION_REVISIONS_REVISION_HASH = (
    "agent_configuration_revisions.revision_hash"
)
_EFFECT_INTENTS_LOGICAL_KEY = "effect_intents.logical_key"
_EFFECT_RECEIPTS_LOGICAL_KEY = "effect_receipts.logical_key"
_EFFECT_RECEIPTS_RUN_ID = "effect_receipts.run_id"
_EFFECT_RECEIPTS_WORKFLOW_REVISION_HASH = "effect_receipts.workflow_revision_hash"
_EFFECT_RECEIPTS_RESULT_HASH = "effect_receipts.result_hash"
_AGENT_ATTEMPTS_ATTEMPT_ID = "agent_attempts.attempt_id"
_CATALOG_LINEAGES_LINEAGE_ID = "catalog_lineages.lineage_id"
_CONTEXT_PACKAGES_V3_PACKAGE_HASH = "context_packages_v3.package_hash"

atelier_schema_versions = sa.Table(
    "atelier_schema_versions",
    metadata,
    sa.Column("version", sa.Integer, primary_key=True),
)
workflow_revisions = sa.Table(
    "workflow_revisions",
    metadata,
    sa.Column("revision_hash", sa.Text, primary_key=True),
    sa.Column("document", sa.LargeBinary, nullable=False),
    sa.CheckConstraint(_REVISION_HASH_HEX64_CHECK),
)
runs = sa.Table(
    "runs",
    metadata,
    sa.Column("run_id", sa.Text, primary_key=True),
    sa.Column("bootstrap_workflow_id", sa.Text, unique=True, nullable=False),
    sa.Column(
        "revision_hash",
        sa.Text,
        sa.ForeignKey(_WORKFLOW_REVISIONS_REVISION_HASH),
        nullable=False,
    ),
    sa.Column("workflow_format_version", sa.Integer, nullable=False),
    sa.Column("agent_binding_set_hash", sa.Text, nullable=True),
    sa.Column("current_node_id", sa.Text, nullable=False),
    sa.Column("current_round_ordinal", sa.Integer, nullable=False),
    sa.Column("state", sa.Text, nullable=False),
    sa.Column("state_version", sa.Integer, nullable=False),
    sa.Column("last_event_sequence", sa.Integer, nullable=False),
    sa.Column("terminal_hash", sa.Text, nullable=True),
    sa.Column(
        "run_configuration_revision_hash",
        sa.Text,
        sa.ForeignKey(_RUN_CONFIGURATION_REVISIONS_REVISION_HASH),
        nullable=True,
    ),
    sa.UniqueConstraint("run_id", "revision_hash"),
    sa.UniqueConstraint("run_id", "revision_hash", "agent_binding_set_hash"),
    sa.CheckConstraint(_RUN_ID_NONEMPTY_CHECK),
    sa.CheckConstraint("length(current_node_id) > 0"),
    sa.CheckConstraint(f"current_round_ordinal >= {FIRST_ROUND_ORDINAL}"),
    sa.CheckConstraint(
        "workflow_format_version IN ("
        + ", ".join(str(int(member)) for member in WorkflowFormatVersion)
        + ")"
    ),
    sa.CheckConstraint(
        f"(workflow_format_version = {int(WorkflowFormatVersion.V1)} AND "
        "agent_binding_set_hash IS NULL) OR "
        f"(workflow_format_version = {int(WorkflowFormatVersion.V2)} AND "
        "agent_binding_set_hash IS NOT NULL "
        "AND length(agent_binding_set_hash) = 64 "
        "AND agent_binding_set_hash NOT GLOB '*[^0-9a-f]*') OR "
        f"(workflow_format_version = {int(WorkflowFormatVersion.V3)} AND "
        "(agent_binding_set_hash IS NULL OR "
        "(length(agent_binding_set_hash) = 64 "
        "AND agent_binding_set_hash NOT GLOB '*[^0-9a-f]*')))"
    ),
    sa.CheckConstraint(
        "state IN ('STARTED', 'WAITING_RECONCILIATION', 'WAITING_INPUT', "
        "'COMPLETED', 'FAILED', 'CANCELLED')"
    ),
    sa.CheckConstraint("state_version >= 0"),
    sa.CheckConstraint("last_event_sequence >= 0"),
    sa.CheckConstraint(
        "(state IN ('COMPLETED', 'FAILED', 'CANCELLED') AND terminal_hash IS NOT NULL "
        "AND length(terminal_hash) = 64 AND terminal_hash NOT GLOB '*[^0-9a-f]*') "
        "OR (state NOT IN ('COMPLETED', 'FAILED', 'CANCELLED') AND terminal_hash IS NULL)"
    ),
    sa.CheckConstraint(
        f"(workflow_format_version = {int(WorkflowFormatVersion.V3)} "
        "AND run_configuration_revision_hash IS NOT NULL "
        "AND length(run_configuration_revision_hash) = 64 "
        "AND run_configuration_revision_hash NOT GLOB '*[^0-9a-f]*') "
        f"OR (workflow_format_version <> {int(WorkflowFormatVersion.V3)} "
        "AND run_configuration_revision_hash IS NULL)"
    ),
)
auth_profile_revisions = sa.Table(
    "auth_profile_revisions",
    metadata,
    sa.Column("revision_hash", sa.Text, primary_key=True),
    sa.Column("profile_id", sa.Text, nullable=False),
    sa.Column("revision_number", sa.Integer, nullable=False),
    sa.Column("provider_id", sa.Text, nullable=False),
    sa.Column("auth_mode", sa.Text, nullable=False),
    sa.UniqueConstraint("profile_id", "revision_number"),
    sa.UniqueConstraint(
        "revision_hash",
        "profile_id",
        "revision_number",
        "provider_id",
        "auth_mode",
    ),
    sa.CheckConstraint(_REVISION_HASH_HEX64_CHECK),
    sa.CheckConstraint(
        f"length(profile_id) BETWEEN 1 AND {MAXIMUM_AGENT_FIELD_CHARACTERS}"
    ),
    sa.CheckConstraint(f"revision_number BETWEEN 1 AND {MAXIMUM_SIGNED_INT64}"),
    sa.CheckConstraint(
        f"length(provider_id) BETWEEN 1 AND {MAXIMUM_PROVIDER_ID_CHARACTERS}"
    ),
    sa.CheckConstraint(_PROVIDER_ID_LOWERCASE_START_CHECK),
    sa.CheckConstraint(_PROVIDER_ID_ALLOWED_CHARACTERS_CHECK),
    sa.CheckConstraint("auth_mode IN ('subscription', 'api_key')"),
)
agent_configuration_revisions = sa.Table(
    "agent_configuration_revisions",
    metadata,
    sa.Column("revision_hash", sa.Text, primary_key=True),
    sa.Column("model", sa.Text, nullable=False),
    sa.Column(
        "auth_profile_revision_hash",
        sa.Text,
        sa.ForeignKey("auth_profile_revisions.revision_hash"),
        nullable=False,
    ),
    sa.Column("executor_revision", sa.Text, nullable=False),
    sa.Column("revision_format_version", sa.Integer, nullable=False),
    sa.Column("requested_capability", sa.Text, nullable=False),
    sa.UniqueConstraint(
        "revision_hash",
        "auth_profile_revision_hash",
        "model",
        "executor_revision",
    ),
    sa.UniqueConstraint(
        "revision_hash",
        "auth_profile_revision_hash",
        "model",
        "executor_revision",
        "revision_format_version",
        "requested_capability",
    ),
    sa.CheckConstraint(_REVISION_HASH_HEX64_CHECK),
    sa.CheckConstraint(f"length(model) BETWEEN 1 AND {MAXIMUM_AGENT_FIELD_CHARACTERS}"),
    sa.CheckConstraint(
        "length(auth_profile_revision_hash) = 64 "
        "AND auth_profile_revision_hash NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint(
        f"length(executor_revision) BETWEEN 1 AND {MAXIMUM_AGENT_FIELD_CHARACTERS}"
    ),
    sa.CheckConstraint("revision_format_version IN (1, 2)"),
    sa.CheckConstraint(
        "requested_capability IN ('headless', 'headless_with_tools', 'interactive')"
    ),
    sa.CheckConstraint(
        "revision_format_version = 2 OR requested_capability = 'headless'"
    ),
)

run_agent_bindings = sa.Table(
    "run_agent_bindings",
    metadata,
    sa.Column("run_id", sa.Text, nullable=False),
    sa.Column("revision_hash", sa.Text, nullable=False),
    sa.Column("binding_set_hash", sa.Text, nullable=False),
    sa.Column("role", sa.Text, nullable=False),
    sa.Column("agent_configuration_revision_hash", sa.Text, nullable=False),
    sa.PrimaryKeyConstraint("run_id", "role"),
    sa.ForeignKeyConstraint(
        ("run_id", "revision_hash", "binding_set_hash"),
        (_RUNS_RUN_ID, _RUNS_REVISION_HASH, "runs.agent_binding_set_hash"),
    ),
    sa.ForeignKeyConstraint(
        ("agent_configuration_revision_hash",),
        (_AGENT_CONFIGURATION_REVISIONS_REVISION_HASH,),
    ),
    sa.UniqueConstraint(
        "run_id",
        "revision_hash",
        "binding_set_hash",
        "role",
        "agent_configuration_revision_hash",
    ),
    sa.CheckConstraint(_RUN_ID_NONEMPTY_CHECK),
    sa.CheckConstraint(_REVISION_HASH_HEX64_CHECK),
    sa.CheckConstraint(
        "length(binding_set_hash) = 64 AND binding_set_hash NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint(f"length(role) BETWEEN 1 AND {MAXIMUM_AGENT_FIELD_CHARACTERS}"),
    sa.CheckConstraint(_AGENT_CONFIGURATION_REVISION_HASH_HEX64_CHECK),
)
effect_intents = sa.Table(
    "effect_intents",
    metadata,
    sa.Column("logical_key", sa.Text, primary_key=True),
    sa.Column("run_id", sa.Text, sa.ForeignKey(_RUNS_RUN_ID), nullable=False),
    sa.Column("canonical_request", sa.LargeBinary, nullable=False),
    sa.Column("request_hash", sa.Text, nullable=False),
    sa.Column(
        "workflow_revision_hash",
        sa.Text,
        sa.ForeignKey(_WORKFLOW_REVISIONS_REVISION_HASH),
        nullable=False,
    ),
    sa.Column("adapter_revision", sa.Text, nullable=False),
    sa.Column("destination_identity", sa.Text, nullable=False),
    sa.Column("adapter_operational_identity", sa.Text, nullable=False),
    sa.Column("operation_name", sa.Text, nullable=False),
    sa.Column("state", sa.Text, nullable=False),
    sa.Column("state_version", sa.Integer, nullable=False),
    sa.Column(
        "reconciliation_owner_command_id",
        sa.Text,
        sa.ForeignKey("reconcile_commands.command_id", ondelete="RESTRICT"),
        nullable=True,
    ),
    sa.UniqueConstraint("logical_key", "run_id", "workflow_revision_hash"),
    sa.ForeignKeyConstraint(
        ("run_id", "workflow_revision_hash"),
        (_RUNS_RUN_ID, _RUNS_REVISION_HASH),
    ),
    sa.CheckConstraint(_LOGICAL_KEY_NONEMPTY_CHECK),
    sa.CheckConstraint(_RUN_ID_NONEMPTY_CHECK),
    sa.CheckConstraint(_REQUEST_HASH_HEX64_CHECK),
    sa.CheckConstraint(_WORKFLOW_REVISION_HASH_HEX64_CHECK),
    sa.CheckConstraint("length(adapter_revision) > 0"),
    sa.CheckConstraint("length(destination_identity) > 0"),
    sa.CheckConstraint("length(adapter_operational_identity) > 0"),
    sa.CheckConstraint(
        "operation_name IN ('open-pr', 'push-atelier-commit', 'claim-work-item')"
    ),
    sa.CheckConstraint(
        "state IN ('PREPARED', 'WAITING_RECONCILIATION', 'RECONCILING', "
        "'CONFIRMED', 'ABANDONED')"
    ),
    sa.CheckConstraint("state_version >= 0"),
    sa.CheckConstraint(
        "(state = 'RECONCILING' "
        "AND reconciliation_owner_command_id IS NOT NULL "
        "AND length(reconciliation_owner_command_id) > 0) "
        "OR (state <> 'RECONCILING' "
        "AND reconciliation_owner_command_id IS NULL)"
    ),
)
reconcile_commands = sa.Table(
    "reconcile_commands",
    metadata,
    sa.Column("command_id", sa.Text, primary_key=True),
    sa.Column(
        "logical_key",
        sa.Text,
        sa.ForeignKey(_EFFECT_INTENTS_LOGICAL_KEY),
        nullable=False,
    ),
    sa.Column("expected_intent_version", sa.Integer, nullable=False),
    sa.Column("determination", sa.Text, nullable=False),
    sa.Column("actor", sa.Text, nullable=False),
    sa.Column("evidence", sa.Text, nullable=False),
    sa.Column("found_effect_id", sa.Text, nullable=True),
    sa.Column("found_result", sa.LargeBinary, nullable=True),
    sa.Column("found_result_hash", sa.Text, nullable=True),
    sa.Column("state", sa.Text, nullable=False),
    sa.CheckConstraint("length(command_id) > 0"),
    sa.CheckConstraint(_LOGICAL_KEY_NONEMPTY_CHECK),
    sa.CheckConstraint("expected_intent_version >= 0"),
    sa.CheckConstraint("determination IN ('FOUND', 'AUTHORITATIVE_NOT_FOUND')"),
    sa.CheckConstraint(_ACTOR_NONEMPTY_CHECK),
    sa.CheckConstraint("length(evidence) > 0"),
    sa.CheckConstraint(
        "(determination = 'FOUND' "
        "AND found_effect_id IS NOT NULL AND length(found_effect_id) > 0 "
        "AND found_result IS NOT NULL "
        "AND found_result_hash IS NOT NULL AND length(found_result_hash) = 64 "
        "AND found_result_hash NOT GLOB '*[^0-9a-f]*') "
        "OR (determination = 'AUTHORITATIVE_NOT_FOUND' "
        "AND found_effect_id IS NULL "
        "AND found_result IS NULL "
        "AND found_result_hash IS NULL)"
    ),
    sa.CheckConstraint("state IN ('PENDING', 'APPLIED', 'REJECTED_CONFLICT')"),
)
effect_receipts = sa.Table(
    "effect_receipts",
    metadata,
    sa.Column(
        "logical_key",
        sa.Text,
        sa.ForeignKey(_EFFECT_INTENTS_LOGICAL_KEY),
        primary_key=True,
    ),
    sa.Column("run_id", sa.Text, sa.ForeignKey(_RUNS_RUN_ID), nullable=False),
    sa.Column("canonical_request", sa.LargeBinary, nullable=False),
    sa.Column("request_hash", sa.Text, nullable=False),
    sa.Column(
        "workflow_revision_hash",
        sa.Text,
        sa.ForeignKey(_WORKFLOW_REVISIONS_REVISION_HASH),
        nullable=False,
    ),
    sa.Column("adapter_revision", sa.Text, nullable=False),
    sa.Column("destination_identity", sa.Text, nullable=False),
    sa.Column("adapter_operational_identity", sa.Text, nullable=False),
    sa.Column("operation_name", sa.Text, nullable=False),
    sa.Column("effect_id", sa.Text, nullable=False),
    sa.Column("result", sa.LargeBinary, nullable=False),
    sa.Column("result_hash", sa.Text, nullable=False),
    sa.Column("confirmation_source", sa.Text, nullable=False),
    sa.Column(
        "reconcile_command_id",
        sa.Text,
        sa.ForeignKey("reconcile_commands.command_id"),
        nullable=True,
    ),
    sa.Column("fork_source_logical_key", sa.Text, nullable=True),
    sa.Column("fork_source_run_id", sa.Text, nullable=True),
    sa.Column("fork_source_workflow_revision_hash", sa.Text, nullable=True),
    sa.Column("fork_source_result_hash", sa.Text, nullable=True),
    sa.UniqueConstraint(
        "logical_key", "run_id", "workflow_revision_hash", "result_hash"
    ),
    sa.ForeignKeyConstraint(
        ("logical_key", "run_id", "workflow_revision_hash"),
        (
            _EFFECT_INTENTS_LOGICAL_KEY,
            "effect_intents.run_id",
            "effect_intents.workflow_revision_hash",
        ),
    ),
    sa.ForeignKeyConstraint(
        (
            "fork_source_logical_key",
            "fork_source_run_id",
            "fork_source_workflow_revision_hash",
            "fork_source_result_hash",
        ),
        (
            _EFFECT_RECEIPTS_LOGICAL_KEY,
            _EFFECT_RECEIPTS_RUN_ID,
            _EFFECT_RECEIPTS_WORKFLOW_REVISION_HASH,
            _EFFECT_RECEIPTS_RESULT_HASH,
        ),
    ),
    sa.CheckConstraint(_LOGICAL_KEY_NONEMPTY_CHECK),
    sa.CheckConstraint(_RUN_ID_NONEMPTY_CHECK),
    sa.CheckConstraint(_REQUEST_HASH_HEX64_CHECK),
    sa.CheckConstraint(_WORKFLOW_REVISION_HASH_HEX64_CHECK),
    sa.CheckConstraint("length(adapter_revision) > 0"),
    sa.CheckConstraint("length(destination_identity) > 0"),
    sa.CheckConstraint("length(adapter_operational_identity) > 0"),
    sa.CheckConstraint(
        "operation_name IN ('open-pr', 'push-atelier-commit', 'claim-work-item')"
    ),
    sa.CheckConstraint("length(effect_id) > 0"),
    sa.CheckConstraint(
        "length(result_hash) = 64 AND result_hash NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint(
        "confirmation_source IN "
        "('ADAPTER_READBACK', 'ADAPTER_EXECUTION', "
        "'OPERATOR_FOUND', 'OPERATOR_AUTHORIZED_EXECUTION', 'FORK_REFERENCE')"
    ),
    sa.CheckConstraint(
        "(confirmation_source IN ('ADAPTER_READBACK', 'ADAPTER_EXECUTION') "
        "AND reconcile_command_id IS NULL) "
        "OR (confirmation_source IN "
        "('OPERATOR_FOUND', 'OPERATOR_AUTHORIZED_EXECUTION') "
        "AND reconcile_command_id IS NOT NULL "
        "AND length(reconcile_command_id) > 0) "
        "OR (confirmation_source = 'FORK_REFERENCE' "
        "AND reconcile_command_id IS NULL)"
    ),
    sa.CheckConstraint(
        "(confirmation_source = 'FORK_REFERENCE' "
        "AND fork_source_logical_key IS NOT NULL "
        "AND fork_source_run_id IS NOT NULL "
        "AND fork_source_workflow_revision_hash IS NOT NULL "
        "AND fork_source_result_hash IS NOT NULL "
        "AND fork_source_result_hash = result_hash) "
        "OR (confirmation_source <> 'FORK_REFERENCE' "
        "AND fork_source_logical_key IS NULL "
        "AND fork_source_run_id IS NULL "
        "AND fork_source_workflow_revision_hash IS NULL "
        "AND fork_source_result_hash IS NULL)"
    ),
)
agent_receipts = sa.Table(
    "agent_receipts",
    metadata,
    sa.Column("node_execution_id", sa.Text, primary_key=True),
    sa.Column("request_hash", sa.Text, nullable=False),
    sa.Column("run_id", sa.Text, nullable=False),
    sa.Column("workflow_revision_hash", sa.Text, nullable=False),
    sa.Column("node_id", sa.Text, nullable=False),
    sa.Column("executor_adapter_revision", sa.Text, nullable=False),
    sa.Column("executor_operational_identity", sa.Text, nullable=False),
    sa.Column("output_bytes", sa.LargeBinary, nullable=False),
    sa.Column("output_hash", sa.Text, nullable=False),
    sa.Column("receipt_hash", sa.Text, nullable=False, unique=True),
    sa.UniqueConstraint("run_id", "workflow_revision_hash", "node_id"),
    sa.ForeignKeyConstraint(
        ("run_id", "workflow_revision_hash"),
        (_RUNS_RUN_ID, _RUNS_REVISION_HASH),
    ),
    sa.CheckConstraint(_NODE_EXECUTION_ID_HEX64_CHECK),
    sa.CheckConstraint(_REQUEST_HASH_HEX64_CHECK),
    sa.CheckConstraint(_RUN_ID_NONEMPTY_CHECK),
    sa.CheckConstraint(_WORKFLOW_REVISION_HASH_HEX64_CHECK),
    sa.CheckConstraint(_NODE_ID_NONEMPTY_CHECK),
    sa.CheckConstraint("length(executor_adapter_revision) > 0"),
    sa.CheckConstraint("length(executor_operational_identity) > 0"),
    sa.CheckConstraint(
        "length(output_hash) = 64 AND output_hash NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint(_RECEIPT_HASH_HEX64_CHECK),
)
agent_receipts_v2 = sa.Table(
    "agent_receipts_v2",
    metadata,
    sa.Column("node_execution_id", sa.Text, primary_key=True),
    sa.Column("request_hash", sa.Text, nullable=False),
    sa.Column("run_id", sa.Text, nullable=False),
    sa.Column("workflow_revision_hash", sa.Text, nullable=False),
    sa.Column("node_id", sa.Text, nullable=False),
    sa.Column("role", sa.Text, nullable=False),
    sa.Column("binding_set_hash", sa.Text, nullable=False),
    sa.Column("agent_configuration_revision_hash", sa.Text, nullable=False),
    sa.Column("auth_profile_revision_hash", sa.Text, nullable=False),
    sa.Column("profile_id", sa.Text, nullable=False),
    sa.Column("revision_number", sa.Integer, nullable=False),
    sa.Column("provider_id", sa.Text, nullable=False),
    sa.Column("auth_mode", sa.Text, nullable=False),
    sa.Column("model", sa.Text, nullable=False),
    sa.Column("executor_revision", sa.Text, nullable=False),
    sa.Column("executor_operational_identity", sa.Text, nullable=False),
    sa.Column("output_bytes", sa.LargeBinary, nullable=False),
    sa.Column("output_hash", sa.Text, nullable=False),
    sa.Column("receipt_hash", sa.Text, nullable=False, unique=True),
    sa.Column("round_ordinal", sa.Integer, nullable=False),
    # One receipt per node *execution* -- the primary key above says it, and it
    # says it exactly. A second key over (run, revision, node) said the same
    # thing while a node ran once per run, and said something false the moment a
    # declared loop ran it again.
    sa.ForeignKeyConstraint(
        (
            "run_id",
            "workflow_revision_hash",
            "binding_set_hash",
            "role",
            "agent_configuration_revision_hash",
        ),
        (
            "run_agent_bindings.run_id",
            "run_agent_bindings.revision_hash",
            "run_agent_bindings.binding_set_hash",
            "run_agent_bindings.role",
            "run_agent_bindings.agent_configuration_revision_hash",
        ),
    ),
    sa.ForeignKeyConstraint(
        (
            "agent_configuration_revision_hash",
            "auth_profile_revision_hash",
            "model",
            "executor_revision",
        ),
        (
            _AGENT_CONFIGURATION_REVISIONS_REVISION_HASH,
            "agent_configuration_revisions.auth_profile_revision_hash",
            "agent_configuration_revisions.model",
            "agent_configuration_revisions.executor_revision",
        ),
    ),
    sa.ForeignKeyConstraint(
        (
            "auth_profile_revision_hash",
            "profile_id",
            "revision_number",
            "provider_id",
            "auth_mode",
        ),
        (
            "auth_profile_revisions.revision_hash",
            "auth_profile_revisions.profile_id",
            "auth_profile_revisions.revision_number",
            "auth_profile_revisions.provider_id",
            "auth_profile_revisions.auth_mode",
        ),
    ),
    sa.CheckConstraint(_NODE_EXECUTION_ID_HEX64_CHECK),
    sa.CheckConstraint(_REQUEST_HASH_HEX64_CHECK),
    sa.CheckConstraint(_RUN_ID_NONEMPTY_CHECK),
    sa.CheckConstraint(_WORKFLOW_REVISION_HASH_HEX64_CHECK),
    sa.CheckConstraint(
        f"length(node_id) BETWEEN 1 AND {MAXIMUM_AGENT_FIELD_CHARACTERS}"
    ),
    sa.CheckConstraint(f"length(role) BETWEEN 1 AND {MAXIMUM_AGENT_FIELD_CHARACTERS}"),
    sa.CheckConstraint(
        "length(binding_set_hash) = 64 AND binding_set_hash NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint(_AGENT_CONFIGURATION_REVISION_HASH_HEX64_CHECK),
    sa.CheckConstraint(
        "length(auth_profile_revision_hash) = 64 "
        "AND auth_profile_revision_hash NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint(
        f"length(profile_id) BETWEEN 1 AND {MAXIMUM_AGENT_FIELD_CHARACTERS}"
    ),
    sa.CheckConstraint(f"revision_number BETWEEN 1 AND {MAXIMUM_SIGNED_INT64}"),
    sa.CheckConstraint(
        f"length(provider_id) BETWEEN 1 AND {MAXIMUM_PROVIDER_ID_CHARACTERS}"
    ),
    sa.CheckConstraint(_PROVIDER_ID_LOWERCASE_START_CHECK),
    sa.CheckConstraint(_PROVIDER_ID_ALLOWED_CHARACTERS_CHECK),
    sa.CheckConstraint("auth_mode IN ('subscription', 'api_key')"),
    sa.CheckConstraint(f"length(model) BETWEEN 1 AND {MAXIMUM_AGENT_FIELD_CHARACTERS}"),
    sa.CheckConstraint(
        f"length(executor_revision) BETWEEN 1 AND {MAXIMUM_AGENT_FIELD_CHARACTERS}"
    ),
    sa.CheckConstraint(
        f"length(executor_operational_identity) BETWEEN 1 AND {MAXIMUM_AGENT_FIELD_CHARACTERS}"
    ),
    sa.CheckConstraint(
        "typeof(output_bytes) = 'blob' AND length(output_bytes) <= 49152"
    ),
    sa.CheckConstraint(
        "length(output_hash) = 64 AND output_hash NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint(_RECEIPT_HASH_HEX64_CHECK),
    sa.CheckConstraint(f"round_ordinal >= {FIRST_ROUND_ORDINAL}"),
)
tool_redemptions = sa.Table(
    "tool_redemptions",
    metadata,
    # Keyed by the attempt, and bound to the attempt alone (V39, #642). What a
    # redemption records is what *one attempt's* grant ran, and an attempt exists
    # whichever way it ends -- where the agent receipt this row used to hang from
    # is written only for a success. Anchored there, a verification that exited
    # zero and was then followed by a capture that could not keep the work had
    # nowhere to be written, so the proof of a check that really passed was
    # discarded with the ending. Keyed by the attempt rather than by the node
    # execution for the same reason: a replacement attempt of that node redeems
    # its own grant, and two attempts of one node are two redemptions, not a
    # collision.
    sa.Column(
        "attempt_id",
        sa.Text,
        sa.ForeignKey(_AGENT_ATTEMPTS_ATTEMPT_ID),
        primary_key=True,
    ),
    sa.Column("node_execution_id", sa.Text, nullable=False),
    sa.Column("run_id", sa.Text, nullable=False),
    sa.Column("workflow_revision_hash", sa.Text, nullable=False),
    sa.Column("node_id", sa.Text, nullable=False),
    sa.Column("tool_revision_hash", sa.Text, nullable=False),
    sa.Column("capability", sa.Text, nullable=False),
    sa.Column("command", sa.Text, nullable=False),
    sa.Column("exit_code", sa.Integer, nullable=False),
    sa.Column("standard_output_hash", sa.Text, nullable=False),
    sa.Column("receipt_hash", sa.Text, nullable=False, unique=True),
    sa.ForeignKeyConstraint(
        ("run_id", "workflow_revision_hash"),
        (_RUNS_RUN_ID, _RUNS_REVISION_HASH),
    ),
    sa.CheckConstraint(_NODE_EXECUTION_ID_HEX64_CHECK),
    sa.CheckConstraint(_RUN_ID_NONEMPTY_CHECK),
    sa.CheckConstraint(_WORKFLOW_REVISION_HASH_HEX64_CHECK),
    sa.CheckConstraint(
        f"length(node_id) BETWEEN 1 AND {MAXIMUM_AGENT_FIELD_CHARACTERS}"
    ),
    sa.CheckConstraint(_ATTEMPT_ID_HEX64_CHECK),
    sa.CheckConstraint(
        "length(tool_revision_hash) = 64 AND tool_revision_hash NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint("capability IN ('run-project-verification')"),
    # The exact argv, as the adapter writes one immutable value. Its length is
    # the record's own bound, not a second one spelled here: what a store may
    # hold and what a receipt may carry would be two numbers for one limit.
    sa.CheckConstraint("length(command) > 0"),
    # A stored redemption is the record of a command that was *satisfied* (V39,
    # #642). A nonzero exit is the opposite fact -- it ends the attempt under
    # PROJECT_VERIFICATION_FAILED and redeems nothing -- so the column is not
    # bounded here but fixed: with the row now able to outlive its attempt's
    # success, "exit code any integer" would have made "the check passed" a
    # thing a reader had to re-derive from every row instead of a thing this
    # table means.
    sa.CheckConstraint("exit_code = 0"),
    sa.CheckConstraint(
        "length(standard_output_hash) = 64 "
        "AND standard_output_hash NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint(_RECEIPT_HASH_HEX64_CHECK),
)
agent_attempts = sa.Table(
    "agent_attempts",
    metadata,
    sa.Column("attempt_id", sa.Text, primary_key=True),
    sa.Column("node_execution_id", sa.Text, nullable=False),
    sa.Column("request_hash", sa.Text, nullable=False),
    sa.Column("executor_operational_identity", sa.Text, nullable=False),
    sa.Column("run_id", sa.Text, nullable=False),
    sa.Column("workflow_revision_hash", sa.Text, nullable=False),
    sa.Column("node_id", sa.Text, nullable=False),
    sa.Column("attempt_ordinal", sa.Integer, nullable=False),
    sa.Column("state", sa.Text, nullable=False),
    sa.Column("state_version", sa.Integer, nullable=False),
    sa.Column("process_phase", sa.Text, nullable=False),
    sa.Column("process_owner_id", sa.Text, nullable=True),
    sa.Column("watchdog_generation_id", sa.Text, nullable=True),
    sa.Column("cancellation_command_id", sa.Text, nullable=True),
    sa.Column("cancellation_expected_state_version", sa.Integer, nullable=True),
    sa.Column("replacement", sa.Text, nullable=True),
    sa.Column("redrive_state", sa.Text, nullable=True),
    sa.Column("cancellation_disposition", sa.Text, nullable=True),
    sa.Column("cancellation_workflow_id", sa.Text, unique=True, nullable=True),
    sa.Column("failure_code", sa.Text, nullable=True),
    sa.Column(
        "receipt_hash",
        sa.Text,
        sa.ForeignKey("agent_receipts_v2.receipt_hash", ondelete="RESTRICT"),
        unique=True,
        nullable=True,
    ),
    sa.Column("runner_manifest_id", sa.Text, nullable=True),
    sa.Column("runner_generation_id", sa.Text, nullable=True),
    sa.Column("runner_invocation_id", sa.Text, nullable=True),
    sa.Column("runner_terminal_evidence_hash", sa.Text, nullable=True),
    sa.Column("runner_evidence_acceptance_phase", sa.Text, nullable=False),
    # The reference is declared, not assumed. The store writes the artifact and
    # the pointer in one transaction, so a row can never precede its bytes --
    # but "the writer is careful" is not a guarantee the store makes, and a
    # 64-hex column with nothing behind it is a pointer at evidence that may
    # never have existed. RESTRICT is what the receipt reference already says:
    # material an attempt names is not removable while the attempt names it.
    sa.Column(
        "transcript_artifact_hash",
        sa.Text,
        sa.ForeignKey("artifacts.artifact_hash", ondelete="RESTRICT"),
        nullable=True,
    ),
    sa.UniqueConstraint("node_execution_id", "attempt_ordinal"),
    sa.ForeignKeyConstraint(
        ("run_id", "workflow_revision_hash"),
        (_RUNS_RUN_ID, _RUNS_REVISION_HASH),
    ),
    sa.CheckConstraint(_ATTEMPT_ID_HEX64_CHECK),
    sa.CheckConstraint(_NODE_EXECUTION_ID_HEX64_CHECK),
    sa.CheckConstraint(_REQUEST_HASH_HEX64_CHECK),
    sa.CheckConstraint(
        f"length(executor_operational_identity) BETWEEN 1 AND {MAXIMUM_AGENT_FIELD_CHARACTERS}"
    ),
    sa.CheckConstraint(_RUN_ID_NONEMPTY_CHECK),
    sa.CheckConstraint(_WORKFLOW_REVISION_HASH_HEX64_CHECK),
    sa.CheckConstraint(
        f"length(node_id) BETWEEN 1 AND {MAXIMUM_AGENT_FIELD_CHARACTERS}"
    ),
    sa.CheckConstraint("attempt_ordinal IN (1, 2)"),
    # A transcript is what an attempt DID, so it exists only once the attempt is
    # over. Every writer sets it in the same statement that turns the row
    # terminal; saying so here is what stops a live attempt from carrying a
    # pointer whose bytes the run may still be adding to.
    sa.CheckConstraint(
        "transcript_artifact_hash IS NULL OR "
        "(length(transcript_artifact_hash) = 64 "
        "AND transcript_artifact_hash NOT GLOB '*[^0-9a-f]*' "
        "AND state IN ('SUCCEEDED', 'FAILED'))"
    ),
    sa.CheckConstraint(
        "process_phase IN ('NONE', 'WATCHDOG_READY', 'LAUNCH_AUTHORIZED', "
        "'PROCESS_OBSERVED', 'CLEANUP_ATTESTED')"
    ),
    sa.CheckConstraint(
        "(process_phase = 'NONE' AND process_owner_id IS NULL "
        "AND watchdog_generation_id IS NULL) OR "
        "(process_phase = 'CLEANUP_ATTESTED' "
        "AND cancellation_disposition = 'NEVER_LAUNCHED' "
        "AND process_owner_id IS NULL AND watchdog_generation_id IS NULL) OR "
        f"(process_phase <> 'NONE' AND length(process_owner_id) BETWEEN 1 AND {MAXIMUM_AGENT_FIELD_CHARACTERS} "
        f"AND length(watchdog_generation_id) BETWEEN 1 AND {MAXIMUM_AGENT_FIELD_CHARACTERS})"
    ),
    sa.CheckConstraint(
        "(runner_manifest_id IS NULL AND runner_generation_id IS NULL) OR "
        "(length(runner_manifest_id) = 64 "
        "AND runner_manifest_id NOT GLOB '*[^0-9a-f]*' "
        f"AND length(runner_generation_id) BETWEEN 1 AND {MAXIMUM_AGENT_FIELD_CHARACTERS})"
    ),
    sa.CheckConstraint(
        "runner_invocation_id IS NULL OR "
        "(runner_manifest_id IS NOT NULL "
        f"AND length(runner_invocation_id) BETWEEN 1 AND {MAXIMUM_AGENT_FIELD_CHARACTERS})"
    ),
    sa.CheckConstraint(
        "(runner_evidence_acceptance_phase = 'NONE' "
        "AND runner_terminal_evidence_hash IS NULL) OR "
        "(runner_evidence_acceptance_phase IN ('CORE_COMMITTED', 'ACKNOWLEDGED') "
        "AND length(runner_terminal_evidence_hash) = 64 "
        "AND runner_terminal_evidence_hash NOT GLOB '*[^0-9a-f]*')"
    ),
    sa.CheckConstraint(
        "runner_evidence_acceptance_phase = 'NONE' "
        "OR runner_invocation_id IS NOT NULL OR state = 'PREPARED'"
    ),
    sa.CheckConstraint(
        "runner_manifest_id IS NULL OR "
        "(process_phase = 'NONE' AND process_owner_id IS NULL "
        "AND watchdog_generation_id IS NULL)"
    ),
    sa.CheckConstraint(
        "(cancellation_command_id IS NULL "
        "AND cancellation_expected_state_version IS NULL "
        "AND replacement IS NULL AND redrive_state IS NULL "
        "AND cancellation_disposition IS NULL AND cancellation_workflow_id IS NULL) "
        f"OR (length(cancellation_command_id) BETWEEN 1 AND {MAXIMUM_AGENT_FIELD_CHARACTERS} "
        "AND cancellation_expected_state_version >= 0 "
        "AND replacement IN ('NONE', 'ONE') "
        "AND redrive_state IN ('PENDING', 'OWNER_NOT_LOCAL', 'CLEANUP_ATTESTED') "
        "AND length(cancellation_workflow_id) > 0 "
        "AND ((redrive_state = 'CLEANUP_ATTESTED' "
        "AND cancellation_disposition IN ('NEVER_LAUNCHED', 'EXITED_BEFORE_SIGNAL', "
        "'REAPED_AFTER_TERM', 'REAPED_AFTER_KILL', "
        "'OWNER_LOST_AFTER_PARENT_DEATH')) OR "
        "(redrive_state <> 'CLEANUP_ATTESTED' "
        "AND cancellation_disposition IS NULL)))"
    ),
    sa.CheckConstraint(
        "(state = 'PREPARED' AND state_version = 0 "
        "AND process_phase = 'NONE' AND runner_manifest_id IS NULL "
        "AND cancellation_command_id IS NULL "
        "AND failure_code IS NULL AND receipt_hash IS NULL) OR "
        "(state = 'PREPARED' AND state_version = 1 "
        "AND process_phase = 'WATCHDOG_READY' AND cancellation_command_id IS NULL "
        "AND failure_code IS NULL AND receipt_hash IS NULL) OR "
        "(state = 'PREPARED' AND state_version >= 1 "
        "AND process_phase = 'NONE' AND runner_manifest_id IS NOT NULL "
        "AND cancellation_command_id IS NULL "
        "AND failure_code IS NULL AND receipt_hash IS NULL) OR "
        "(state = 'LAUNCH_ARMED' AND state_version = 1 "
        "AND process_phase IN ('NONE', 'LAUNCH_AUTHORIZED') "
        "AND cancellation_command_id IS NULL "
        "AND failure_code IS NULL AND receipt_hash IS NULL) OR "
        "(state = 'LAUNCH_ARMED' AND state_version >= 2 "
        "AND process_phase IN ('NONE', 'LAUNCH_AUTHORIZED', 'PROCESS_OBSERVED') "
        "AND cancellation_command_id IS NULL "
        "AND failure_code IS NULL AND receipt_hash IS NULL) OR "
        "(state = 'CANCEL_REQUESTED' AND state_version >= 1 "
        "AND cancellation_command_id IS NOT NULL "
        "AND cancellation_disposition IS NULL "
        "AND failure_code IS NULL AND receipt_hash IS NULL) OR "
        "(state IN ('CANCELLED', 'INTERRUPTED') AND state_version >= 2 "
        "AND (process_phase = 'CLEANUP_ATTESTED' OR "
        "(process_phase = 'NONE' AND runner_manifest_id IS NOT NULL)) "
        "AND cancellation_command_id IS NOT NULL "
        "AND cancellation_disposition IS NOT NULL "
        "AND failure_code IS NULL AND receipt_hash IS NULL) OR "
        "(state = 'SUCCEEDED' AND state_version >= 2 "
        "AND cancellation_command_id IS NULL "
        "AND failure_code IS NULL AND receipt_hash IS NOT NULL) OR "
        "(state = 'FAILED' AND state_version >= 2 "
        "AND cancellation_command_id IS NULL "
        "AND failure_code IN "
        "('PROCESS_EXITED_UNSUCCESSFULLY', 'PROCESS_OUTPUT_LIMIT_EXCEEDED', "
        "'PROCESS_SUPERVISION_FAILED', 'OUTPUT_SCHEMA_REFUSED', "
        "'AGENT_REFUSED', 'PROJECT_VERIFICATION_FAILED', "
        "'CANDIDATE_CAPTURE_FAILED', 'CANDIDATE_UNCHANGED', "
        "'PRODUCED_VALUE_REFUSED') "
        "AND receipt_hash IS NULL)"
    ),
)
agent_attempt_receipts_v3 = sa.Table(
    "agent_attempt_receipts_v3",
    metadata,
    sa.Column(
        "attempt_id",
        sa.Text,
        sa.ForeignKey(_AGENT_ATTEMPTS_ATTEMPT_ID, ondelete="RESTRICT"),
        primary_key=True,
    ),
    sa.Column("reason", sa.Text, nullable=False),
    sa.Column("schema_revision_hash", sa.Text, nullable=False),
    sa.Column("value_hash", sa.Text, nullable=False),
    sa.Column(
        "artifact_hash",
        sa.Text,
        sa.ForeignKey("artifacts.artifact_hash", ondelete="RESTRICT"),
        nullable=True,
    ),
    sa.Column("receipt_hash", sa.Text, unique=True, nullable=False),
    sa.CheckConstraint("length(reason) > 0"),
    sa.CheckConstraint(_SCHEMA_REVISION_HASH_HEX64_CHECK),
    sa.CheckConstraint(_VALUE_HASH_HEX64_CHECK),
    sa.CheckConstraint(
        "artifact_hash IS NULL OR (length(artifact_hash) = 64 "
        "AND artifact_hash NOT GLOB '*[^0-9a-f]*')"
    ),
    sa.CheckConstraint(_RECEIPT_HASH_HEX64_CHECK),
)
permission_receipts = sa.Table(
    "permission_receipts",
    metadata,
    # The authorisation ledger of ADR 0020 §2, keyed by the question rather than
    # by the attempt: one attempt answers many, and the correlation id is minted
    # from the attempt and the call ordinal, so the pair is the question's own
    # identity and a second answer to one question is a key collision rather
    # than a second row. The foreign key is what makes an answer inseparable
    # from the execution it authorised -- a receipt without its attempt would be
    # an authorisation nobody can trace to what it permitted.
    sa.Column(
        "attempt_id",
        sa.Text,
        sa.ForeignKey(_AGENT_ATTEMPTS_ATTEMPT_ID),
        primary_key=True,
    ),
    sa.Column("correlation_id", sa.Text, primary_key=True),
    sa.Column("effect", sa.Text, nullable=False),
    sa.Column("scope_kind", sa.Text, nullable=False),
    sa.Column("scope_value", sa.Text, nullable=False),
    sa.Column("granted", sa.Integer, nullable=False),
    sa.Column("policy_revision_hash", sa.Text, nullable=False),
    sa.Column("authority", sa.Text, nullable=False),
    sa.Column("decided_at", sa.Text, nullable=False),
    sa.Column("receipt_hash", sa.Text, nullable=False, unique=True),
    sa.CheckConstraint(_ATTEMPT_ID_HEX64_CHECK),
    sa.CheckConstraint(
        "length(correlation_id) = 64 AND correlation_id NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint(closed_vocabulary_sql("effect", PermissionEffect)),
    sa.CheckConstraint(closed_vocabulary_sql("scope_kind", PermissionScopeKind)),
    sa.CheckConstraint(
        f"length(scope_value) BETWEEN 1 AND {MAXIMUM_AGENT_FIELD_CHARACTERS}"
    ),
    sa.CheckConstraint("granted IN (0, 1)"),
    sa.CheckConstraint(
        "length(policy_revision_hash) = 64 "
        "AND policy_revision_hash NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint(closed_vocabulary_sql("authority", PermissionAuthority)),
    sa.CheckConstraint(rfc3339_utc("decided_at")),
    sa.CheckConstraint(_RECEIPT_HASH_HEX64_CHECK),
)
run_events = sa.Table(
    "run_events",
    metadata,
    sa.Column("run_id", sa.Text, nullable=False),
    sa.Column("revision_hash", sa.Text, nullable=False),
    sa.Column("event_sequence", sa.Integer, nullable=False),
    sa.Column("node_id", sa.Text, nullable=False),
    sa.Column("node_execution_id", sa.Text, nullable=False),
    sa.Column("round_ordinal", sa.Integer, nullable=False),
    sa.Column("event_kind", sa.Text, nullable=False),
    sa.Column("wait_answer_actor", sa.Text, nullable=True),
    sa.Column("payload", sa.LargeBinary, nullable=False),
    sa.Column("payload_hash", sa.Text, nullable=False),
    sa.Column("receipt_logical_key", sa.Text, nullable=True),
    sa.Column("receipt_result_hash", sa.Text, nullable=True),
    sa.Column("event_hash", sa.Text, nullable=False),
    sa.Column("agent_attempt_id", sa.Text, nullable=True),
    sa.Column("attempt_ordinal", sa.Integer, nullable=True),
    sa.Column("cancellation_command_id", sa.Text, nullable=True),
    sa.Column("replacement", sa.Text, nullable=True),
    sa.Column("cancellation_disposition", sa.Text, nullable=True),
    sa.Column("replacement_attempt_id", sa.Text, nullable=True),
    sa.Column("agent_receipt_hash", sa.Text, nullable=True),
    sa.PrimaryKeyConstraint("run_id", "event_sequence"),
    sa.ForeignKeyConstraint(
        ("run_id", "revision_hash"), (_RUNS_RUN_ID, _RUNS_REVISION_HASH)
    ),
    sa.ForeignKeyConstraint(
        (
            "receipt_logical_key",
            "run_id",
            "revision_hash",
            "receipt_result_hash",
        ),
        (
            _EFFECT_RECEIPTS_LOGICAL_KEY,
            _EFFECT_RECEIPTS_RUN_ID,
            _EFFECT_RECEIPTS_WORKFLOW_REVISION_HASH,
            _EFFECT_RECEIPTS_RESULT_HASH,
        ),
    ),
    sa.CheckConstraint("event_sequence > 0"),
    sa.CheckConstraint(_NODE_ID_NONEMPTY_CHECK),
    sa.CheckConstraint(_NODE_EXECUTION_ID_HEX64_CHECK),
    sa.CheckConstraint(f"round_ordinal >= {FIRST_ROUND_ORDINAL}"),
    sa.CheckConstraint(
        "event_kind IN ('AGENT_COMPLETED', 'AGENT_FAILED', "
        "'AGENT_CANCEL_REQUESTED', 'AGENT_CANCELLED', 'AGENT_INTERRUPTED', "
        "'ACTION_RECONCILIATION_REQUIRED', "
        "'ACTION_RECONCILIATION_RESOLVED', 'ACTION_COMPLETED', 'WAITING_INPUT', "
        "'WAIT_ANSWERED', 'WAIT_CANCELLED', 'SUBWORKFLOW_COMPLETED')"
    ),
    sa.CheckConstraint(
        "(event_kind = 'WAITING_INPUT' AND wait_answer_actor IN ('operator')) "
        "OR (event_kind <> 'WAITING_INPUT' AND wait_answer_actor IS NULL)"
    ),
    sa.CheckConstraint(
        "length(payload_hash) = 64 AND payload_hash NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint("length(event_hash) = 64 AND event_hash NOT GLOB '*[^0-9a-f]*'"),
    sa.CheckConstraint(
        "(event_kind IN ('ACTION_RECONCILIATION_RESOLVED', 'ACTION_COMPLETED') "
        "AND receipt_logical_key IS NOT NULL "
        "AND length(receipt_logical_key) > 0 "
        "AND receipt_result_hash IS NOT NULL "
        "AND length(receipt_result_hash) = 64 "
        "AND receipt_result_hash NOT GLOB '*[^0-9a-f]*' "
        "AND receipt_result_hash = payload_hash) "
        "OR (event_kind NOT IN "
        "('ACTION_RECONCILIATION_RESOLVED', 'ACTION_COMPLETED') "
        "AND receipt_logical_key IS NULL AND receipt_result_hash IS NULL)"
    ),
    sa.CheckConstraint(
        "(agent_attempt_id IS NULL AND attempt_ordinal IS NULL "
        "AND cancellation_command_id IS NULL AND replacement IS NULL "
        "AND cancellation_disposition IS NULL AND replacement_attempt_id IS NULL) "
        "OR (length(agent_attempt_id) = 64 "
        "AND agent_attempt_id NOT GLOB '*[^0-9a-f]*' "
        "AND attempt_ordinal IN (1, 2) "
        "AND ((event_kind IN ('AGENT_COMPLETED', 'AGENT_FAILED') "
        "AND cancellation_command_id IS NULL AND replacement IS NULL "
        "AND cancellation_disposition IS NULL AND replacement_attempt_id IS NULL) "
        "OR (event_kind = 'AGENT_CANCEL_REQUESTED' "
        f"AND length(cancellation_command_id) BETWEEN 1 AND {MAXIMUM_AGENT_FIELD_CHARACTERS} "
        "AND replacement IN ('NONE', 'ONE') "
        "AND cancellation_disposition IS NULL AND replacement_attempt_id IS NULL) "
        "OR (event_kind IN ('AGENT_CANCELLED', 'AGENT_INTERRUPTED') "
        f"AND length(cancellation_command_id) BETWEEN 1 AND {MAXIMUM_AGENT_FIELD_CHARACTERS} "
        "AND replacement IN ('NONE', 'ONE') "
        "AND cancellation_disposition IS NOT NULL)))"
    ),
    # The mirror of the contract's admission rule (contracts/executions.py):
    # only a completion has an agent receipt, and it stays nullable because a
    # run written before v3 of the event hash carries none.
    sa.CheckConstraint(
        "(event_kind = 'AGENT_COMPLETED' AND (agent_receipt_hash IS NULL "
        "OR (length(agent_receipt_hash) = 64 "
        "AND agent_receipt_hash NOT GLOB '*[^0-9a-f]*'))) "
        "OR (event_kind <> 'AGENT_COMPLETED' AND agent_receipt_hash IS NULL)"
    ),
)
sa.Index(
    "run_events_legacy_execution_kind_unique",
    run_events.c.node_execution_id,
    run_events.c.event_kind,
    unique=True,
    sqlite_where=run_events.c.agent_attempt_id.is_(None),
)
# The same sentence in the coordinates a reader asks in. An execution id is
# derived from run, revision, node and round, so the index above already says
# one event of a kind per round -- but only to a writer that derived the id
# correctly, and the store cannot recompute a hash to check. `_existing_event`
# reads a round back by those four coordinates and expects at most one row, so
# two rows disagreeing about the execution of one round would turn a retry into
# an unnamed failure instead of the exact event it replays.
sa.Index(
    "run_events_round_kind_unique",
    run_events.c.run_id,
    run_events.c.revision_hash,
    run_events.c.node_id,
    run_events.c.round_ordinal,
    run_events.c.event_kind,
    unique=True,
    sqlite_where=run_events.c.agent_attempt_id.is_(None),
)
sa.Index(
    "run_events_attempt_kind_unique",
    run_events.c.agent_attempt_id,
    run_events.c.event_kind,
    unique=True,
    sqlite_where=run_events.c.agent_attempt_id.is_not(None),
)
run_instants = sa.Table(
    "run_instants",
    metadata,
    sa.Column("run_id", sa.Text, primary_key=True),
    sa.Column("started_at", sa.Text, nullable=False),
    sa.Column("ended_at", sa.Text, nullable=True),
    sa.CheckConstraint(_RUN_ID_NONEMPTY_CHECK),
    sa.CheckConstraint(rfc3339_utc("started_at")),
    sa.CheckConstraint(rfc3339_utc_or_null("ended_at")),
)
attempt_instants = sa.Table(
    "attempt_instants",
    metadata,
    sa.Column("attempt_id", sa.Text, primary_key=True),
    sa.Column("started_at", sa.Text, nullable=False),
    sa.Column("ended_at", sa.Text, nullable=True),
    sa.CheckConstraint(_ATTEMPT_ID_HEX64_CHECK),
    sa.CheckConstraint(rfc3339_utc("started_at")),
    sa.CheckConstraint(rfc3339_utc_or_null("ended_at")),
)
event_instants = sa.Table(
    "event_instants",
    metadata,
    sa.Column("run_id", sa.Text, nullable=False),
    sa.Column("event_sequence", sa.Integer, nullable=False),
    sa.Column("recorded_at", sa.Text, nullable=False),
    sa.PrimaryKeyConstraint("run_id", "event_sequence"),
    sa.CheckConstraint(_RUN_ID_NONEMPTY_CHECK),
    sa.CheckConstraint("event_sequence > 0"),
    sa.CheckConstraint(rfc3339_utc("recorded_at")),
)
wait_answers = sa.Table(
    "wait_answers",
    metadata,
    sa.Column("run_id", sa.Text, nullable=False),
    sa.Column("revision_hash", sa.Text, nullable=False),
    sa.Column("node_id", sa.Text, nullable=False),
    sa.Column("node_execution_id", sa.Text, nullable=False),
    sa.Column("round_ordinal", sa.Integer, nullable=False),
    sa.Column("actor", sa.Text, nullable=True),
    sa.Column("actor_attribution_kind", sa.Text, nullable=False),
    sa.Column("answer_bytes", sa.LargeBinary, nullable=False),
    sa.Column("answer_hash", sa.Text, nullable=False),
    sa.Column("answer_workflow_id", sa.Text, nullable=False, unique=True),
    sa.Column("state", sa.Text, nullable=False),
    sa.Column("state_version", sa.Integer, nullable=False),
    # The execution is the key, not the node: a node a declared loop turns
    # pauses once per round, and run-and-node would say one answer per node
    # per run forever.
    sa.PrimaryKeyConstraint("node_execution_id"),
    sa.ForeignKeyConstraint(
        ("run_id", "revision_hash"), (_RUNS_RUN_ID, _RUNS_REVISION_HASH)
    ),
    sa.CheckConstraint(_NODE_ID_NONEMPTY_CHECK),
    sa.CheckConstraint(f"round_ordinal >= {FIRST_ROUND_ORDINAL}"),
    sa.CheckConstraint(
        "(actor_attribution_kind = 'RECORDED' AND actor IN ('operator')) "
        "OR (actor_attribution_kind = 'LEGACY_UNATTRIBUTED' AND actor IS NULL)"
    ),
    sa.CheckConstraint(_NODE_EXECUTION_ID_HEX64_CHECK),
    sa.CheckConstraint(
        "length(answer_hash) = 64 AND answer_hash NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint("length(answer_workflow_id) > 0"),
    sa.CheckConstraint("state IN ('PENDING', 'APPLIED')"),
    sa.CheckConstraint("state_version IN (0, 1)"),
    sa.CheckConstraint(
        "(state = 'PENDING' AND state_version = 0) "
        "OR (state = 'APPLIED' AND state_version = 1)"
    ),
)


def _revision_kind_sql(column: str) -> str:
    """The closed published-kind vocabulary, asserted of one named column."""

    return closed_vocabulary_sql(column, RevisionKind)


_PUBLISHED_REVISION_KIND_SQL = _revision_kind_sql("kind")
published_revisions = sa.Table(
    "published_revisions",
    metadata,
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("revision_hash", sa.Text, nullable=False),
    sa.Column("document", sa.LargeBinary, nullable=False),
    sa.PrimaryKeyConstraint("kind", "revision_hash"),
    sa.CheckConstraint("length(kind) BETWEEN 1 AND 64"),
    sa.CheckConstraint(_PUBLISHED_REVISION_KIND_SQL),
    sa.CheckConstraint(_REVISION_HASH_HEX64_CHECK),
)
catalog_lineages = sa.Table(
    "catalog_lineages",
    metadata,
    sa.Column("lineage_id", sa.Text, primary_key=True),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("founding_revision_hash", sa.Text, nullable=False),
    sa.UniqueConstraint("kind", "founding_revision_hash"),
    sa.ForeignKeyConstraint(
        ("kind", "founding_revision_hash"),
        ("published_revisions.kind", "published_revisions.revision_hash"),
    ),
    sa.CheckConstraint("length(lineage_id) = 64 AND lineage_id NOT GLOB '*[^0-9a-f]*'"),
    sa.CheckConstraint("length(kind) BETWEEN 1 AND 64"),
    sa.CheckConstraint(_PUBLISHED_REVISION_KIND_SQL),
    sa.CheckConstraint(
        "length(founding_revision_hash) = 64 "
        "AND founding_revision_hash NOT GLOB '*[^0-9a-f]*'"
    ),
)
catalog_lineage_members = sa.Table(
    "catalog_lineage_members",
    metadata,
    sa.Column(
        "lineage_id",
        sa.Text,
        sa.ForeignKey(_CATALOG_LINEAGES_LINEAGE_ID),
        nullable=False,
    ),
    sa.Column("revision_number", sa.Integer, nullable=False),
    sa.Column("revision_hash", sa.Text, nullable=False),
    sa.PrimaryKeyConstraint("lineage_id", "revision_number"),
    sa.UniqueConstraint("lineage_id", "revision_hash"),
    sa.CheckConstraint("revision_number >= 1"),
    sa.CheckConstraint(_REVISION_HASH_HEX64_CHECK),
)
catalog_lineage_aliases = sa.Table(
    "catalog_lineage_aliases",
    metadata,
    sa.Column(
        "lineage_id",
        sa.Text,
        sa.ForeignKey(_CATALOG_LINEAGES_LINEAGE_ID),
        nullable=False,
    ),
    sa.Column("activation_number", sa.Integer, nullable=False),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("actor", sa.Text, nullable=False),
    sa.Column("activated_at", sa.Text, nullable=False),
    sa.PrimaryKeyConstraint("lineage_id", "activation_number"),
    sa.CheckConstraint("activation_number >= 1"),
    sa.CheckConstraint(
        f"length(name) BETWEEN 1 AND {MAXIMUM_LINEAGE_DISPLAY_NAME_CHARACTERS}"
    ),
    sa.CheckConstraint("name GLOB '[a-z]*' AND name NOT GLOB '*[^a-z0-9._-]*'"),
    sa.CheckConstraint("length(name) <> 64 OR name GLOB '*[^0-9a-f]*'"),
    sa.CheckConstraint(_ACTOR_NONEMPTY_CHECK),
    sa.CheckConstraint(_ACTIVATED_AT_NONEMPTY_CHECK),
)
catalog_lineage_retirements = sa.Table(
    "catalog_lineage_retirements",
    metadata,
    sa.Column(
        "lineage_id",
        sa.Text,
        sa.ForeignKey(_CATALOG_LINEAGES_LINEAGE_ID),
        nullable=False,
    ),
    sa.Column("activation_number", sa.Integer, nullable=False),
    sa.Column("state", sa.Text, nullable=False),
    sa.Column("actor", sa.Text, nullable=False),
    sa.Column("activated_at", sa.Text, nullable=False),
    sa.PrimaryKeyConstraint("lineage_id", "activation_number"),
    sa.CheckConstraint("activation_number >= 1"),
    sa.CheckConstraint("state IN ('retired')"),
    sa.CheckConstraint(_ACTOR_NONEMPTY_CHECK),
    sa.CheckConstraint(_ACTIVATED_AT_NONEMPTY_CHECK),
)
catalog_intakes = sa.Table(
    "catalog_intakes",
    metadata,
    sa.Column("intake_id", sa.Text, primary_key=True),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("document", sa.LargeBinary, nullable=False),
    sa.Column("actor", sa.Text, nullable=False),
    sa.Column("activated_at", sa.Text, nullable=False),
    sa.CheckConstraint("length(intake_id) = 64 AND intake_id NOT GLOB '*[^0-9a-f]*'"),
    sa.CheckConstraint("kind IN ('agent', 'skill', 'workflow')"),
    sa.CheckConstraint(_ACTOR_NONEMPTY_CHECK),
    sa.CheckConstraint(_ACTIVATED_AT_NONEMPTY_CHECK),
)
node_artifacts_v3 = sa.Table(
    "node_artifacts_v3",
    metadata,
    sa.Column(
        "run_id",
        sa.Text,
        sa.ForeignKey(_RUNS_RUN_ID),
        nullable=False,
    ),
    sa.Column("node_id", sa.Text, nullable=False),
    sa.Column("node_execution_id", sa.Text, nullable=False),
    sa.Column("output_name", sa.Text, nullable=False),
    sa.Column("schema_revision_hash", sa.Text, nullable=False),
    sa.Column("value", sa.LargeBinary, nullable=False),
    sa.Column("value_hash", sa.Text, nullable=False),
    sa.Column("artifact_hash", sa.Text, unique=True, nullable=False),
    sa.PrimaryKeyConstraint("run_id", "node_id", "node_execution_id", "output_name"),
    sa.UniqueConstraint(
        "node_execution_id",
        "output_name",
        "schema_revision_hash",
        "value_hash",
    ),
    sa.CheckConstraint(_NODE_ID_NONEMPTY_CHECK),
    sa.CheckConstraint("length(output_name) > 0"),
    sa.CheckConstraint(_NODE_EXECUTION_ID_HEX64_CHECK),
    sa.CheckConstraint(_SCHEMA_REVISION_HASH_HEX64_CHECK),
    sa.CheckConstraint(_VALUE_HASH_HEX64_CHECK),
    sa.CheckConstraint(
        "length(artifact_hash) = 64 AND artifact_hash NOT GLOB '*[^0-9a-f]*'"
    ),
)
node_receipts_v3 = sa.Table(
    "node_receipts_v3",
    metadata,
    sa.Column("node_execution_id", sa.Text, primary_key=True),
    sa.Column("disposition", sa.Text, nullable=False),
    sa.Column("reason", sa.Text, nullable=False),
    sa.Column("request_hash", sa.Text, nullable=False),
    sa.Column(
        "context_package_hash",
        sa.Text,
        sa.ForeignKey(_CONTEXT_PACKAGES_V3_PACKAGE_HASH),
        nullable=False,
    ),
    sa.Column("receipt_hash", sa.Text, unique=True, nullable=False),
    sa.CheckConstraint(_NODE_EXECUTION_ID_HEX64_CHECK),
    sa.CheckConstraint(
        "disposition IN ('succeeded', 'failed', 'cancelled', 'blocked')"
    ),
    sa.CheckConstraint("length(reason) > 0"),
    sa.CheckConstraint(_REQUEST_HASH_HEX64_CHECK),
    sa.CheckConstraint(
        "length(context_package_hash) = 64 "
        "AND context_package_hash NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint(_RECEIPT_HASH_HEX64_CHECK),
    # The pair is the binding. Each hash alone can name a record that exists
    # while the two together describe a node execution nobody ran -- this
    # execution's receipt pointing at another execution's request -- so the key
    # is composite and a single-column one would not see it.
    sa.ForeignKeyConstraint(
        ("node_execution_id", "request_hash"),
        (
            "node_execution_requests_v3.node_execution_id",
            "node_execution_requests_v3.request_hash",
        ),
    ),
)
artifacts = sa.Table(
    "artifacts",
    metadata,
    sa.Column("artifact_hash", sa.Text, primary_key=True),
    sa.Column("content", sa.LargeBinary, nullable=False),
    sa.CheckConstraint(
        "length(artifact_hash) = 64 AND artifact_hash NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint(
        f"length(content) BETWEEN 1 AND {MAXIMUM_ARTIFACT_BYTES}",
    ),
)
run_inputs_v3 = sa.Table(
    "run_inputs_v3",
    metadata,
    sa.Column("run_id", sa.Text, sa.ForeignKey(_RUNS_RUN_ID), nullable=False),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("schema_revision_hash", sa.Text, nullable=False),
    sa.Column("value", sa.LargeBinary, nullable=False),
    sa.Column("value_hash", sa.Text, nullable=False),
    sa.PrimaryKeyConstraint("run_id", "name"),
    sa.CheckConstraint("length(name) > 0"),
    sa.CheckConstraint(_SCHEMA_REVISION_HASH_HEX64_CHECK),
    sa.CheckConstraint(_VALUE_HASH_HEX64_CHECK),
)
run_configuration_revisions = sa.Table(
    "run_configuration_revisions",
    metadata,
    sa.Column("revision_hash", sa.Text, primary_key=True),
    sa.Column("preimage", sa.LargeBinary, nullable=False),
    sa.CheckConstraint(_REVISION_HASH_HEX64_CHECK),
)
node_execution_requests_v3 = sa.Table(
    "node_execution_requests_v3",
    metadata,
    # The execution is the key, not the request. Two rounds of one looped node
    # are asked the same thing until a result differs between them, so their
    # request preimages are identical and their hashes are one value -- while
    # the executions are two, and each owes its own receipt. Keying by the hash
    # made the second round's row vanish into the first and left its receipt
    # with nothing to bind.
    sa.Column("request_hash", sa.Text, nullable=False),
    sa.Column("node_execution_id", sa.Text, primary_key=True),
    sa.Column(
        "run_configuration_revision_hash",
        sa.Text,
        sa.ForeignKey(_RUN_CONFIGURATION_REVISIONS_REVISION_HASH),
        nullable=False,
    ),
    sa.Column("context_package_hash", sa.Text, nullable=False),
    sa.Column("preimage", sa.LargeBinary, nullable=False),
    sa.UniqueConstraint("node_execution_id", "request_hash"),
    sa.CheckConstraint(_REQUEST_HASH_HEX64_CHECK),
    sa.CheckConstraint(_NODE_EXECUTION_ID_HEX64_CHECK),
    sa.CheckConstraint(
        "length(context_package_hash) = 64 "
        "AND context_package_hash NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.ForeignKeyConstraint(
        ("context_package_hash",), (_CONTEXT_PACKAGES_V3_PACKAGE_HASH,)
    ),
)
context_packages_v3 = sa.Table(
    "context_packages_v3",
    metadata,
    sa.Column("package_hash", sa.Text, primary_key=True),
    sa.Column("manifest", sa.LargeBinary, nullable=False),
    sa.CheckConstraint(
        "length(package_hash) = 64 AND package_hash NOT GLOB '*[^0-9a-f]*'"
    ),
)
run_forks = sa.Table(
    "run_forks",
    metadata,
    sa.Column("command_id", sa.Text, primary_key=True),
    sa.Column("origin_run_id", sa.Text, sa.ForeignKey(_RUNS_RUN_ID), nullable=False),
    sa.Column("origin_terminal_hash", sa.Text, nullable=False),
    sa.Column(
        "successor_run_id",
        sa.Text,
        sa.ForeignKey(_RUNS_RUN_ID),
        unique=True,
        nullable=False,
    ),
    sa.Column(
        "workflow_revision_hash",
        sa.Text,
        sa.ForeignKey(_WORKFLOW_REVISIONS_REVISION_HASH),
        nullable=False,
    ),
    sa.Column(
        "run_configuration_revision_hash",
        sa.Text,
        sa.ForeignKey(_RUN_CONFIGURATION_REVISIONS_REVISION_HASH),
        nullable=False,
    ),
    sa.Column("restart_from_node_id", sa.Text, nullable=False),
    sa.Column("fork_hash", sa.Text, unique=True, nullable=False),
    sa.ForeignKeyConstraint(
        ("origin_run_id", "workflow_revision_hash"),
        (_RUNS_RUN_ID, _RUNS_REVISION_HASH),
    ),
    sa.ForeignKeyConstraint(
        ("successor_run_id", "workflow_revision_hash"),
        (_RUNS_RUN_ID, _RUNS_REVISION_HASH),
    ),
    sa.CheckConstraint("length(command_id) = 64 AND command_id NOT GLOB '*[^0-9a-f]*'"),
    sa.CheckConstraint("length(origin_run_id) > 0"),
    sa.CheckConstraint(
        "length(origin_terminal_hash) = 64 "
        "AND origin_terminal_hash NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint("length(successor_run_id) > 0"),
    sa.CheckConstraint(_WORKFLOW_REVISION_HASH_HEX64_CHECK),
    sa.CheckConstraint(
        "length(run_configuration_revision_hash) = 64 "
        "AND run_configuration_revision_hash NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint("length(restart_from_node_id) > 0"),
    sa.CheckConstraint("length(fork_hash) = 64 AND fork_hash NOT GLOB '*[^0-9a-f]*'"),
)
run_fork_reused_nodes = sa.Table(
    "run_fork_reused_nodes",
    metadata,
    sa.Column("successor_run_id", sa.Text, nullable=False),
    sa.Column("position", sa.Integer, nullable=False),
    sa.Column("node_id", sa.Text, nullable=False),
    sa.Column("round_ordinal", sa.Integer, nullable=False),
    sa.Column("source_run_id", sa.Text, nullable=False),
    sa.Column("source_workflow_revision_hash", sa.Text, nullable=False),
    sa.Column("source_node_execution_id", sa.Text, nullable=False),
    sa.Column("source_event_hash", sa.Text, nullable=False),
    sa.Column("source_receipt_hash", sa.Text, nullable=False),
    sa.Column("source_declared_context_package_hash", sa.Text, nullable=False),
    sa.Column(
        "source_agent_receipt_hash",
        sa.Text,
        sa.ForeignKey("agent_receipts_v2.receipt_hash"),
        nullable=True,
    ),
    sa.PrimaryKeyConstraint("successor_run_id", "position"),
    sa.UniqueConstraint("successor_run_id", "node_id", "round_ordinal"),
    sa.ForeignKeyConstraint(("successor_run_id",), ("run_forks.successor_run_id",)),
    sa.ForeignKeyConstraint(
        ("source_run_id", "source_workflow_revision_hash"),
        (_RUNS_RUN_ID, _RUNS_REVISION_HASH),
    ),
    sa.ForeignKeyConstraint(
        ("source_node_execution_id",),
        ("node_execution_requests_v3.node_execution_id",),
    ),
    sa.ForeignKeyConstraint(
        ("source_receipt_hash",),
        ("node_receipts_v3.receipt_hash",),
    ),
    sa.ForeignKeyConstraint(
        ("source_declared_context_package_hash",),
        (_CONTEXT_PACKAGES_V3_PACKAGE_HASH,),
    ),
    sa.CheckConstraint(_POSITION_NONNEGATIVE_CHECK),
    sa.CheckConstraint(_NODE_ID_NONEMPTY_CHECK),
    sa.CheckConstraint(f"round_ordinal >= {FIRST_ROUND_ORDINAL}"),
    sa.CheckConstraint("length(source_run_id) > 0"),
    sa.CheckConstraint(
        "length(source_workflow_revision_hash) = 64 "
        "AND source_workflow_revision_hash NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint(
        "length(source_node_execution_id) = 64 "
        "AND source_node_execution_id NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint(
        "length(source_event_hash) = 64 AND source_event_hash NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint(
        "length(source_receipt_hash) = 64 "
        "AND source_receipt_hash NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint(
        "length(source_declared_context_package_hash) = 64 "
        "AND source_declared_context_package_hash NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint(
        "source_agent_receipt_hash IS NULL OR "
        "(length(source_agent_receipt_hash) = 64 "
        "AND source_agent_receipt_hash NOT GLOB '*[^0-9a-f]*')"
    ),
)
run_fork_effect_fences = sa.Table(
    "run_fork_effect_fences",
    metadata,
    sa.Column("successor_run_id", sa.Text, nullable=False),
    sa.Column("position", sa.Integer, nullable=False),
    sa.Column("node_id", sa.Text, nullable=False),
    sa.Column("round_ordinal", sa.Integer, nullable=False),
    sa.Column("source_logical_key", sa.Text, nullable=False),
    sa.Column("source_run_id", sa.Text, nullable=False),
    sa.Column("source_workflow_revision_hash", sa.Text, nullable=False),
    sa.Column("source_result_hash", sa.Text, nullable=False),
    sa.PrimaryKeyConstraint("successor_run_id", "position"),
    sa.UniqueConstraint("successor_run_id", "node_id", "round_ordinal"),
    sa.ForeignKeyConstraint(("successor_run_id",), ("run_forks.successor_run_id",)),
    sa.ForeignKeyConstraint(
        (
            "source_logical_key",
            "source_run_id",
            "source_workflow_revision_hash",
            "source_result_hash",
        ),
        (
            _EFFECT_RECEIPTS_LOGICAL_KEY,
            _EFFECT_RECEIPTS_RUN_ID,
            _EFFECT_RECEIPTS_WORKFLOW_REVISION_HASH,
            _EFFECT_RECEIPTS_RESULT_HASH,
        ),
    ),
    sa.CheckConstraint(_POSITION_NONNEGATIVE_CHECK),
    sa.CheckConstraint(_NODE_ID_NONEMPTY_CHECK),
    sa.CheckConstraint(f"round_ordinal >= {FIRST_ROUND_ORDINAL}"),
    sa.CheckConstraint("length(source_logical_key) > 0"),
    sa.CheckConstraint("length(source_run_id) > 0"),
    sa.CheckConstraint(
        "length(source_workflow_revision_hash) = 64 "
        "AND source_workflow_revision_hash NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint(
        "length(source_result_hash) = 64 AND source_result_hash NOT GLOB '*[^0-9a-f]*'"
    ),
)
node_receipt_outputs_v3 = sa.Table(
    "node_receipt_outputs_v3",
    metadata,
    sa.Column(
        "node_execution_id",
        sa.Text,
        sa.ForeignKey("node_receipts_v3.node_execution_id"),
        nullable=False,
    ),
    sa.Column("position", sa.Integer, nullable=False),
    sa.Column("output_name", sa.Text, nullable=False),
    sa.Column("schema_revision_hash", sa.Text, nullable=False),
    sa.Column("value_hash", sa.Text, nullable=False),
    sa.PrimaryKeyConstraint("node_execution_id", "position"),
    sa.UniqueConstraint("node_execution_id", "output_name"),
    sa.ForeignKeyConstraint(
        (
            "node_execution_id",
            "output_name",
            "schema_revision_hash",
            "value_hash",
        ),
        (
            "node_artifacts_v3.node_execution_id",
            "node_artifacts_v3.output_name",
            "node_artifacts_v3.schema_revision_hash",
            "node_artifacts_v3.value_hash",
        ),
    ),
    sa.CheckConstraint(_POSITION_NONNEGATIVE_CHECK),
    sa.CheckConstraint("length(output_name) > 0"),
    sa.CheckConstraint(_NODE_EXECUTION_ID_HEX64_CHECK),
    sa.CheckConstraint(_SCHEMA_REVISION_HASH_HEX64_CHECK),
    sa.CheckConstraint(_VALUE_HASH_HEX64_CHECK),
)
host_project_root_revisions = sa.Table(
    "host_project_root_revisions",
    metadata,
    sa.Column("revision_hash", sa.Text, primary_key=True),
    sa.Column("project_id", sa.Text, nullable=False),
    sa.Column("revision_number", sa.Integer, nullable=False),
    sa.Column("root_path", sa.Text, nullable=False),
    sa.UniqueConstraint("project_id", "revision_number"),
    sa.UniqueConstraint(
        "revision_hash",
        "project_id",
        "revision_number",
        "root_path",
    ),
    sa.CheckConstraint(_REVISION_HASH_HEX64_CHECK),
    sa.CheckConstraint(
        f"length(project_id) BETWEEN 1 AND {MAXIMUM_PROJECT_ID_CHARACTERS}"
    ),
    sa.CheckConstraint(f"revision_number BETWEEN 1 AND {MAXIMUM_SIGNED_INT64}"),
    sa.CheckConstraint(
        f"length(root_path) BETWEEN 1 AND {MAXIMUM_PROJECT_ROOT_PATH_CHARACTERS}"
    ),
)
host_model_registry_revisions = sa.Table(
    "host_model_registry_revisions",
    metadata,
    sa.Column("revision_hash", sa.Text, primary_key=True),
    sa.Column("provider_id", sa.Text, nullable=False),
    sa.Column("revision_number", sa.Integer, nullable=False),
    sa.UniqueConstraint("provider_id", "revision_number"),
    sa.UniqueConstraint(
        "revision_hash",
        "provider_id",
        "revision_number",
    ),
    sa.UniqueConstraint("revision_hash", "provider_id"),
    sa.CheckConstraint(_REVISION_HASH_HEX64_CHECK),
    sa.CheckConstraint(
        f"length(provider_id) BETWEEN 1 AND {MAXIMUM_PROVIDER_ID_CHARACTERS}"
    ),
    sa.CheckConstraint(_PROVIDER_ID_LOWERCASE_START_CHECK),
    sa.CheckConstraint(_PROVIDER_ID_ALLOWED_CHARACTERS_CHECK),
    sa.CheckConstraint(f"revision_number BETWEEN 1 AND {MAXIMUM_SIGNED_INT64}"),
)
host_model_registry_entries = sa.Table(
    "host_model_registry_entries",
    metadata,
    sa.Column("revision_hash", sa.Text, nullable=False),
    sa.Column("provider_id", sa.Text, nullable=False),
    sa.Column("model_id", sa.Text, nullable=False),
    sa.Column("agent_configuration_revision_hash", sa.Text, nullable=False),
    sa.Column("source", sa.Text, nullable=False),
    sa.Column("provider_check", sa.Text, nullable=False),
    sa.PrimaryKeyConstraint("revision_hash", "model_id"),
    sa.UniqueConstraint(
        "revision_hash",
        "provider_id",
        "model_id",
        "agent_configuration_revision_hash",
    ),
    sa.ForeignKeyConstraint(
        ("revision_hash", "provider_id"),
        (
            "host_model_registry_revisions.revision_hash",
            "host_model_registry_revisions.provider_id",
        ),
    ),
    sa.ForeignKeyConstraint(
        ("agent_configuration_revision_hash",),
        (_AGENT_CONFIGURATION_REVISIONS_REVISION_HASH,),
    ),
    sa.CheckConstraint(_REVISION_HASH_HEX64_CHECK),
    sa.CheckConstraint(
        f"length(provider_id) BETWEEN 1 AND {MAXIMUM_PROVIDER_ID_CHARACTERS}"
    ),
    sa.CheckConstraint(_PROVIDER_ID_LOWERCASE_START_CHECK),
    sa.CheckConstraint(_PROVIDER_ID_ALLOWED_CHARACTERS_CHECK),
    sa.CheckConstraint(
        f"length(model_id) BETWEEN 1 AND {MAXIMUM_EXACT_MODEL_ID_CHARACTERS}"
    ),
    sa.CheckConstraint(
        "instr(model_id, ' ') = 0 AND instr(model_id, char(9)) = 0 "
        "AND instr(model_id, char(10)) = 0 AND instr(model_id, char(13)) = 0"
    ),
    sa.CheckConstraint(_AGENT_CONFIGURATION_REVISION_HASH_HEX64_CHECK),
    sa.CheckConstraint("source IN ('discovered', 'operator')"),
    sa.CheckConstraint(
        "provider_check IN ('not-checked', 'checked', 'unknown-at-provider')"
    ),
)
host_project_model_defaults_revisions = sa.Table(
    "host_project_model_defaults_revisions",
    metadata,
    sa.Column("revision_hash", sa.Text, primary_key=True),
    sa.Column("project_id", sa.Text, nullable=False),
    sa.Column("revision_number", sa.Integer, nullable=False),
    sa.UniqueConstraint("project_id", "revision_number"),
    sa.UniqueConstraint(
        "revision_hash",
        "project_id",
        "revision_number",
    ),
    sa.CheckConstraint(_REVISION_HASH_HEX64_CHECK),
    sa.CheckConstraint(
        f"length(project_id) BETWEEN 1 AND {MAXIMUM_PROJECT_ID_CHARACTERS}"
    ),
    sa.CheckConstraint(f"revision_number BETWEEN 1 AND {MAXIMUM_SIGNED_INT64}"),
)
host_project_model_defaults = sa.Table(
    "host_project_model_defaults",
    metadata,
    sa.Column(
        "revision_hash",
        sa.Text,
        sa.ForeignKey("host_project_model_defaults_revisions.revision_hash"),
        nullable=False,
    ),
    sa.Column("difficulty", sa.Integer, nullable=False),
    sa.Column("model_registry_revision_hash", sa.Text, nullable=False),
    sa.Column("provider_id", sa.Text, nullable=False),
    sa.Column("model_id", sa.Text, nullable=False),
    sa.Column("agent_configuration_revision_hash", sa.Text, nullable=False),
    sa.PrimaryKeyConstraint("revision_hash", "difficulty"),
    sa.ForeignKeyConstraint(
        (
            "model_registry_revision_hash",
            "provider_id",
            "model_id",
            "agent_configuration_revision_hash",
        ),
        (
            "host_model_registry_entries.revision_hash",
            "host_model_registry_entries.provider_id",
            "host_model_registry_entries.model_id",
            "host_model_registry_entries.agent_configuration_revision_hash",
        ),
    ),
    sa.CheckConstraint(_REVISION_HASH_HEX64_CHECK),
    sa.CheckConstraint("difficulty IN (1, 2, 3)"),
    sa.CheckConstraint(
        "length(model_registry_revision_hash) = 64 "
        "AND model_registry_revision_hash NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint(
        f"length(provider_id) BETWEEN 1 AND {MAXIMUM_PROVIDER_ID_CHARACTERS}"
    ),
    sa.CheckConstraint(
        f"length(model_id) BETWEEN 1 AND {MAXIMUM_EXACT_MODEL_ID_CHARACTERS}"
    ),
    sa.CheckConstraint(
        "instr(model_id, ' ') = 0 AND instr(model_id, char(9)) = 0 "
        "AND instr(model_id, char(10)) = 0 AND instr(model_id, char(13)) = 0"
    ),
    sa.CheckConstraint(_AGENT_CONFIGURATION_REVISION_HASH_HEX64_CHECK),
)
webhook_delivery_cursor = sa.Table(
    "webhook_delivery_cursor",
    metadata,
    sa.Column("cursor_id", sa.Text, primary_key=True),
    sa.Column(
        "run_id",
        sa.Text,
        sa.ForeignKey(_RUNS_RUN_ID),
        nullable=True,
    ),
    sa.Column("event_sequence", sa.Integer, nullable=True),
    sa.Column("revision", sa.Integer, nullable=False),
    sa.CheckConstraint("cursor_id = 'attention-events'"),
    sa.CheckConstraint("revision >= 0"),
    sa.CheckConstraint("(run_id IS NULL) = (event_sequence IS NULL)"),
    sa.CheckConstraint("event_sequence IS NULL OR event_sequence > 0"),
)
host_project_source_connection_revisions = sa.Table(
    "host_project_source_connection_revisions",
    metadata,
    sa.Column("revision_hash", sa.Text, primary_key=True),
    sa.Column("project_id", sa.Text, nullable=False),
    sa.Column("source_id", sa.Text, nullable=False),
    sa.Column("source_kind", sa.Text, nullable=False),
    sa.Column("revision_number", sa.Integer, nullable=False),
    sa.Column("source_address", sa.Text, nullable=False),
    sa.Column("source_ref", sa.Text, nullable=True),
    sa.Column("credential_directory", sa.Text, nullable=False),
    sa.Column("auth_method", sa.Text, nullable=False),
    sa.Column("connected_by", sa.Text, nullable=False),
    sa.Column("lifecycle", sa.Text, nullable=False),
    sa.Column("connected_at", sa.Text, nullable=True),
    sa.UniqueConstraint("project_id", "source_id", "revision_number"),
    sa.UniqueConstraint(
        "revision_hash",
        "project_id",
        "source_id",
        "revision_number",
    ),
    sa.CheckConstraint(_REVISION_HASH_HEX64_CHECK),
    sa.CheckConstraint(
        f"length(project_id) BETWEEN 1 AND {MAXIMUM_PROJECT_ID_CHARACTERS}"
    ),
    sa.CheckConstraint(
        "length(source_id) BETWEEN 36 AND 36 AND source_id GLOB "
        "'[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-"
        "[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-"
        "[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-"
        "[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-"
        "[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]"
        "[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'"
    ),
    sa.CheckConstraint(
        f"length(source_kind) BETWEEN 1 AND {MAXIMUM_SOURCE_KIND_CHARACTERS}"
    ),
    sa.CheckConstraint(f"revision_number BETWEEN 1 AND {MAXIMUM_SIGNED_INT64}"),
    sa.CheckConstraint(
        f"length(source_address) BETWEEN 1 AND {MAXIMUM_SOURCE_ADDRESS_CHARACTERS}"
    ),
    sa.CheckConstraint(
        "source_ref IS NULL OR length(source_ref) BETWEEN 1 AND "
        f"{MAXIMUM_SOURCE_REFERENCE_CHARACTERS}"
    ),
    sa.CheckConstraint(
        "length(credential_directory) BETWEEN 1 AND "
        f"{MAXIMUM_CREDENTIAL_DIRECTORY_CHARACTERS}"
    ),
    sa.CheckConstraint(
        f"auth_method IN ('{SourceConnectionAuthMethod.PERSONAL_ACCESS_TOKEN.value}')"
    ),
    sa.CheckConstraint(
        f"length(connected_by) BETWEEN 1 AND {MAXIMUM_CONNECTION_ACTOR_CHARACTERS}"
    ),
    sa.CheckConstraint("lifecycle IN ('CONNECTED', 'DISCONNECTED')"),
    sa.CheckConstraint(rfc3339_utc_or_null("connected_at")),
)

host_definition_source_revisions = sa.Table(
    "host_definition_source_revisions",
    metadata,
    sa.Column("revision_hash", sa.Text, primary_key=True),
    sa.Column("source_id", sa.Text, nullable=False),
    sa.Column("revision_number", sa.Integer, nullable=False),
    sa.Column("source_kind", sa.Text, nullable=False),
    sa.Column("repository_location", sa.Text, nullable=False),
    sa.Column("repository_ref", sa.Text, nullable=False),
    sa.Column("access", sa.Text, nullable=False),
    sa.Column("connected_by", sa.Text, nullable=False),
    sa.UniqueConstraint("source_id", "revision_number"),
    sa.CheckConstraint(_REVISION_HASH_HEX64_CHECK),
    sa.CheckConstraint("length(source_id) = 64 AND source_id NOT GLOB '*[^0-9a-f]*'"),
    sa.CheckConstraint(f"revision_number BETWEEN 1 AND {MAXIMUM_SIGNED_INT64}"),
    sa.CheckConstraint(f"source_kind IN ('{DefinitionSourceKind.GIT.value}')"),
    sa.CheckConstraint(
        "length(repository_location) BETWEEN 1 AND "
        f"{MAXIMUM_REPOSITORY_LOCATION_CHARACTERS}"
    ),
    sa.CheckConstraint(
        f"length(repository_ref) BETWEEN 1 AND {MAXIMUM_REPOSITORY_REF_CHARACTERS}"
    ),
    sa.CheckConstraint(f"access IN ('{DefinitionSourceAccess.ANONYMOUS.value}')"),
    sa.CheckConstraint(
        "length(connected_by) BETWEEN 1 AND "
        f"{MAXIMUM_DEFINITION_SOURCE_ACTOR_CHARACTERS}"
    ),
)
host_definition_source_selections = sa.Table(
    "host_definition_source_selections",
    metadata,
    sa.Column(
        "revision_hash",
        sa.Text,
        sa.ForeignKey("host_definition_source_revisions.revision_hash"),
        nullable=False,
    ),
    sa.Column("selection_ordinal", sa.Integer, nullable=False),
    sa.Column("path_pattern", sa.Text, nullable=False),
    sa.Column("revision_kind", sa.Text, nullable=False),
    sa.PrimaryKeyConstraint("revision_hash", "selection_ordinal"),
    sa.UniqueConstraint("revision_hash", "path_pattern"),
    sa.CheckConstraint(_REVISION_HASH_HEX64_CHECK),
    sa.CheckConstraint(
        f"selection_ordinal BETWEEN 1 AND {MAXIMUM_DEFINITION_SOURCE_SELECTIONS}"
    ),
    sa.CheckConstraint(
        f"length(path_pattern) BETWEEN 1 AND {MAXIMUM_SELECTION_PATTERN_CHARACTERS}"
    ),
    sa.CheckConstraint(_revision_kind_sql("revision_kind")),
)
catalog_source_intakes = sa.Table(
    "catalog_source_intakes",
    metadata,
    sa.Column("source_id", sa.Text, nullable=False),
    sa.Column("source_path", sa.Text, nullable=False),
    sa.Column("intake_number", sa.Integer, nullable=False),
    sa.Column("revision_kind", sa.Text, nullable=False),
    sa.Column("revision_hash", sa.Text, nullable=False),
    sa.Column("source_commit", sa.Text, nullable=False),
    sa.Column("intaken_by", sa.Text, nullable=False),
    sa.Column("intaken_at", sa.Text, nullable=False),
    sa.PrimaryKeyConstraint("source_id", "source_path", "intake_number"),
    sa.ForeignKeyConstraint(
        ("revision_kind", "revision_hash"),
        ("published_revisions.kind", "published_revisions.revision_hash"),
    ),
    sa.CheckConstraint("length(source_id) = 64 AND source_id NOT GLOB '*[^0-9a-f]*'"),
    sa.CheckConstraint(
        f"length(source_path) BETWEEN 1 AND {MAXIMUM_REPOSITORY_PATH_CHARACTERS}"
    ),
    sa.CheckConstraint(f"intake_number BETWEEN 1 AND {MAXIMUM_SIGNED_INT64}"),
    sa.CheckConstraint(_revision_kind_sql("revision_kind")),
    sa.CheckConstraint(_REVISION_HASH_HEX64_CHECK),
    sa.CheckConstraint(
        f"length(source_commit) BETWEEN {MINIMUM_GIT_OBJECT_NAME_CHARACTERS} AND "
        f"{MAXIMUM_GIT_OBJECT_NAME_CHARACTERS} "
        "AND source_commit NOT GLOB '*[^0-9a-f]*'"
    ),
    sa.CheckConstraint(
        f"length(intaken_by) BETWEEN 1 AND {MAXIMUM_DEFINITION_SOURCE_ACTOR_CHARACTERS}"
    ),
    sa.CheckConstraint(rfc3339_utc("intaken_at")),
)

PRODUCT_TABLE_NAMES = frozenset(metadata.tables)

_PRODUCT_TRIGGERS = {
    "workflow_revisions_no_update": """
        CREATE TRIGGER workflow_revisions_no_update
        BEFORE UPDATE ON workflow_revisions BEGIN
          SELECT RAISE(ABORT, 'workflow revisions are immutable');
        END
    """,
    "workflow_revisions_no_delete": """
        CREATE TRIGGER workflow_revisions_no_delete
        BEFORE DELETE ON workflow_revisions BEGIN
          SELECT RAISE(ABORT, 'workflow revisions are immutable');
        END
    """,
    "runs_binding_no_update": """
        CREATE TRIGGER runs_binding_no_update
        BEFORE UPDATE OF run_id, bootstrap_workflow_id, revision_hash,
                         workflow_format_version, agent_binding_set_hash,
                         run_configuration_revision_hash
        ON runs BEGIN
          SELECT RAISE(ABORT, 'run bindings are immutable');
        END
    """,
    "artifacts_no_update": """
        CREATE TRIGGER artifacts_no_update
        BEFORE UPDATE ON artifacts BEGIN
          SELECT RAISE(ABORT, 'artifacts are immutable');
        END
    """,
    "artifacts_no_delete": """
        CREATE TRIGGER artifacts_no_delete
        BEFORE DELETE ON artifacts BEGIN
          SELECT RAISE(ABORT, 'artifacts are immutable');
        END
    """,
    "run_inputs_v3_no_update": """
        CREATE TRIGGER run_inputs_v3_no_update
        BEFORE UPDATE ON run_inputs_v3 BEGIN
          SELECT RAISE(ABORT, 'run inputs are immutable');
        END
    """,
    "run_inputs_v3_no_delete": """
        CREATE TRIGGER run_inputs_v3_no_delete
        BEFORE DELETE ON run_inputs_v3 BEGIN
          SELECT RAISE(ABORT, 'run inputs are immutable');
        END
    """,
    "run_configuration_revisions_no_update": """
        CREATE TRIGGER run_configuration_revisions_no_update
        BEFORE UPDATE ON run_configuration_revisions BEGIN
          SELECT RAISE(ABORT, 'run configuration revisions are immutable');
        END
    """,
    "run_configuration_revisions_no_delete": """
        CREATE TRIGGER run_configuration_revisions_no_delete
        BEFORE DELETE ON run_configuration_revisions BEGIN
          SELECT RAISE(ABORT, 'run configuration revisions are immutable');
        END
    """,
    "node_execution_requests_v3_no_update": """
        CREATE TRIGGER node_execution_requests_v3_no_update
        BEFORE UPDATE ON node_execution_requests_v3 BEGIN
          SELECT RAISE(ABORT, 'node execution requests are immutable');
        END
    """,
    "node_execution_requests_v3_no_delete": """
        CREATE TRIGGER node_execution_requests_v3_no_delete
        BEFORE DELETE ON node_execution_requests_v3 BEGIN
          SELECT RAISE(ABORT, 'node execution requests are immutable');
        END
    """,
    "context_packages_v3_no_update": """
        CREATE TRIGGER context_packages_v3_no_update
        BEFORE UPDATE ON context_packages_v3 BEGIN
          SELECT RAISE(ABORT, 'context packages are immutable');
        END
    """,
    "context_packages_v3_no_delete": """
        CREATE TRIGGER context_packages_v3_no_delete
        BEFORE DELETE ON context_packages_v3 BEGIN
          SELECT RAISE(ABORT, 'context packages are immutable');
        END
    """,
    "auth_profile_revisions_no_update": """
        CREATE TRIGGER auth_profile_revisions_no_update
        BEFORE UPDATE ON auth_profile_revisions BEGIN
          SELECT RAISE(ABORT, 'auth profile revisions are immutable');
        END
    """,
    "auth_profile_revisions_no_delete": """
        CREATE TRIGGER auth_profile_revisions_no_delete
        BEFORE DELETE ON auth_profile_revisions BEGIN
          SELECT RAISE(ABORT, 'auth profile revisions are immutable');
        END
    """,
    "agent_configuration_revisions_no_update": """
        CREATE TRIGGER agent_configuration_revisions_no_update
        BEFORE UPDATE ON agent_configuration_revisions BEGIN
          SELECT RAISE(ABORT, 'agent configuration revisions are immutable');
        END
    """,
    "agent_configuration_revisions_no_delete": """
        CREATE TRIGGER agent_configuration_revisions_no_delete
        BEFORE DELETE ON agent_configuration_revisions BEGIN
          SELECT RAISE(ABORT, 'agent configuration revisions are immutable');
        END
    """,
    "run_agent_bindings_no_update": """
        CREATE TRIGGER run_agent_bindings_no_update
        BEFORE UPDATE ON run_agent_bindings BEGIN
          SELECT RAISE(ABORT, 'run agent bindings are immutable');
        END
    """,
    "run_agent_bindings_no_delete": """
        CREATE TRIGGER run_agent_bindings_no_delete
        BEFORE DELETE ON run_agent_bindings BEGIN
          SELECT RAISE(ABORT, 'run agent bindings are immutable');
        END
    """,
    "effect_intents_binding_no_update": """
        CREATE TRIGGER effect_intents_binding_no_update
        BEFORE UPDATE OF logical_key, run_id, canonical_request, request_hash,
                         workflow_revision_hash, adapter_revision, destination_identity,
                         adapter_operational_identity, operation_name
        ON effect_intents BEGIN
          SELECT RAISE(ABORT, 'effect intent bindings are immutable');
        END
    """,
    "effect_intents_no_delete": """
        CREATE TRIGGER effect_intents_no_delete
        BEFORE DELETE ON effect_intents BEGIN
          SELECT RAISE(ABORT, 'effect intents are immutable');
        END
    """,
    "effect_intents_abandonment": """
        CREATE TRIGGER effect_intents_abandonment
        BEFORE UPDATE OF state, state_version ON effect_intents
        WHEN (NEW.state = 'ABANDONED' OR OLD.state = 'ABANDONED')
          AND NOT (OLD.state = 'PREPARED' AND OLD.state_version = 0
                   AND NEW.state = 'ABANDONED' AND NEW.state_version = 1
                   AND NEW.reconciliation_owner_command_id IS NULL)
        BEGIN
          SELECT RAISE(ABORT, 'invalid effect intent abandonment');
        END
    """,
    "effect_intents_no_abandoned_insert": """
        CREATE TRIGGER effect_intents_no_abandoned_insert
        BEFORE INSERT ON effect_intents
        WHEN NEW.state = 'ABANDONED' BEGIN
          SELECT RAISE(ABORT, 'effect intents are not born abandoned');
        END
    """,
    "effect_receipts_no_update": """
        CREATE TRIGGER effect_receipts_no_update
        BEFORE UPDATE ON effect_receipts BEGIN
          SELECT RAISE(ABORT, 'effect receipts are immutable');
        END
    """,
    "effect_receipts_no_delete": """
        CREATE TRIGGER effect_receipts_no_delete
        BEFORE DELETE ON effect_receipts BEGIN
          SELECT RAISE(ABORT, 'effect receipts are immutable');
        END
    """,
    "run_forks_no_update": """
        CREATE TRIGGER run_forks_no_update
        BEFORE UPDATE ON run_forks BEGIN
          SELECT RAISE(ABORT, 'run forks are immutable');
        END
    """,
    "run_forks_no_delete": """
        CREATE TRIGGER run_forks_no_delete
        BEFORE DELETE ON run_forks BEGIN
          SELECT RAISE(ABORT, 'run forks are immutable');
        END
    """,
    "run_fork_reused_nodes_no_update": """
        CREATE TRIGGER run_fork_reused_nodes_no_update
        BEFORE UPDATE ON run_fork_reused_nodes BEGIN
          SELECT RAISE(ABORT, 'run fork reused nodes are immutable');
        END
    """,
    "run_fork_reused_nodes_no_delete": """
        CREATE TRIGGER run_fork_reused_nodes_no_delete
        BEFORE DELETE ON run_fork_reused_nodes BEGIN
          SELECT RAISE(ABORT, 'run fork reused nodes are immutable');
        END
    """,
    "run_fork_effect_fences_no_update": """
        CREATE TRIGGER run_fork_effect_fences_no_update
        BEFORE UPDATE ON run_fork_effect_fences BEGIN
          SELECT RAISE(ABORT, 'run fork effect fences are immutable');
        END
    """,
    "run_fork_effect_fences_no_delete": """
        CREATE TRIGGER run_fork_effect_fences_no_delete
        BEFORE DELETE ON run_fork_effect_fences BEGIN
          SELECT RAISE(ABORT, 'run fork effect fences are immutable');
        END
    """,
    "agent_receipts_no_update": """
        CREATE TRIGGER agent_receipts_no_update
        BEFORE UPDATE ON agent_receipts BEGIN
          SELECT RAISE(ABORT, 'agent receipts are immutable');
        END
    """,
    "agent_receipts_no_delete": """
        CREATE TRIGGER agent_receipts_no_delete
        BEFORE DELETE ON agent_receipts BEGIN
          SELECT RAISE(ABORT, 'agent receipts are immutable');
        END
    """,
    "agent_receipts_v2_no_update": """
        CREATE TRIGGER agent_receipts_v2_no_update
        BEFORE UPDATE ON agent_receipts_v2 BEGIN
          SELECT RAISE(ABORT, 'v2 agent receipts are immutable');
        END
    """,
    "agent_receipts_v2_no_delete": """
        CREATE TRIGGER agent_receipts_v2_no_delete
        BEFORE DELETE ON agent_receipts_v2 BEGIN
          SELECT RAISE(ABORT, 'v2 agent receipts are immutable');
        END
    """,
    "agent_attempt_receipts_v3_no_update": """
        CREATE TRIGGER agent_attempt_receipts_v3_no_update
        BEFORE UPDATE ON agent_attempt_receipts_v3 BEGIN
          SELECT RAISE(ABORT, 'agent attempt receipts are immutable');
        END
    """,
    "agent_attempt_receipts_v3_no_delete": """
        CREATE TRIGGER agent_attempt_receipts_v3_no_delete
        BEFORE DELETE ON agent_attempt_receipts_v3 BEGIN
          SELECT RAISE(ABORT, 'agent attempt receipts are immutable');
        END
    """,
    "tool_redemptions_no_update": """
        CREATE TRIGGER tool_redemptions_no_update
        BEFORE UPDATE ON tool_redemptions BEGIN
          SELECT RAISE(ABORT, 'tool redemptions are immutable');
        END
    """,
    "tool_redemptions_no_delete": """
        CREATE TRIGGER tool_redemptions_no_delete
        BEFORE DELETE ON tool_redemptions BEGIN
          SELECT RAISE(ABORT, 'tool redemptions are immutable');
        END
    """,
    "permission_receipts_no_update": """
        CREATE TRIGGER permission_receipts_no_update
        BEFORE UPDATE ON permission_receipts BEGIN
          SELECT RAISE(ABORT, 'permission receipts are immutable');
        END
    """,
    "permission_receipts_no_delete": """
        CREATE TRIGGER permission_receipts_no_delete
        BEFORE DELETE ON permission_receipts BEGIN
          SELECT RAISE(ABORT, 'permission receipts are immutable');
        END
    """,
    "reconcile_commands_payload_no_update": """
        CREATE TRIGGER reconcile_commands_payload_no_update
        BEFORE UPDATE OF command_id, logical_key, expected_intent_version,
                         determination, actor, evidence, found_effect_id,
                         found_result, found_result_hash
        ON reconcile_commands BEGIN
          SELECT RAISE(ABORT, 'reconcile command payloads are immutable');
        END
    """,
    "reconcile_commands_no_delete": """
        CREATE TRIGGER reconcile_commands_no_delete
        BEFORE DELETE ON reconcile_commands BEGIN
          SELECT RAISE(ABORT, 'reconcile commands are immutable');
        END
    """,
    "run_events_no_update": """
        CREATE TRIGGER run_events_no_update
        BEFORE UPDATE ON run_events BEGIN
          SELECT RAISE(ABORT, 'run events are immutable');
        END
    """,
    "run_events_no_delete": """
        CREATE TRIGGER run_events_no_delete
        BEFORE DELETE ON run_events BEGIN
          SELECT RAISE(ABORT, 'run events are immutable');
        END
    """,
    "agent_attempts_state_transition": """
        CREATE TRIGGER agent_attempts_state_transition
        BEFORE UPDATE ON agent_attempts
        WHEN NOT (
          OLD.attempt_id = NEW.attempt_id
          AND OLD.node_execution_id = NEW.node_execution_id
          AND OLD.request_hash = NEW.request_hash
          AND OLD.executor_operational_identity = NEW.executor_operational_identity
          AND OLD.run_id = NEW.run_id
          AND OLD.workflow_revision_hash = NEW.workflow_revision_hash
          AND OLD.node_id = NEW.node_id
          AND OLD.attempt_ordinal = NEW.attempt_ordinal
          AND (OLD.transcript_artifact_hash IS NULL
               OR NEW.transcript_artifact_hash = OLD.transcript_artifact_hash)
          AND NEW.state_version > OLD.state_version
          AND (
            (OLD.state = 'PREPARED' AND OLD.state_version = 0
             AND OLD.runner_manifest_id IS NULL
             AND OLD.failure_code IS NULL AND OLD.receipt_hash IS NULL
             AND NEW.state = 'PREPARED' AND NEW.state_version = 1
             AND NEW.process_phase = 'WATCHDOG_READY'
             AND NEW.runner_manifest_id IS NULL
             AND NEW.failure_code IS NULL AND NEW.receipt_hash IS NULL
             AND NEW.cancellation_command_id IS NULL)
            OR
            (OLD.state = 'PREPARED'
             AND OLD.runner_manifest_id IS NULL
             AND NEW.state = 'LAUNCH_ARMED'
             AND NEW.process_phase IN ('NONE', 'LAUNCH_AUTHORIZED')
             AND NEW.runner_manifest_id IS NULL
             AND NEW.failure_code IS NULL AND NEW.receipt_hash IS NULL
             AND NEW.cancellation_command_id IS NULL)
            OR
            (OLD.state = 'LAUNCH_ARMED'
             AND OLD.runner_manifest_id IS NULL
             AND OLD.process_phase = 'LAUNCH_AUTHORIZED'
             AND NEW.state = 'LAUNCH_ARMED'
             AND NEW.process_phase = 'PROCESS_OBSERVED'
             AND NEW.runner_manifest_id IS NULL
             AND NEW.failure_code IS NULL AND NEW.receipt_hash IS NULL
             AND NEW.cancellation_command_id IS NULL)
            OR
            (OLD.state = 'LAUNCH_ARMED'
             AND OLD.runner_manifest_id IS NULL
             AND OLD.failure_code IS NULL AND OLD.receipt_hash IS NULL
             AND NEW.state = 'SUCCEEDED'
             AND NEW.runner_manifest_id IS NULL
             AND NEW.failure_code IS NULL AND NEW.receipt_hash IS NOT NULL
             AND NEW.cancellation_command_id IS NULL
             AND EXISTS (
               SELECT 1 FROM agent_receipts_v2 AS receipt
               WHERE receipt.receipt_hash = NEW.receipt_hash
                 AND receipt.request_hash = NEW.request_hash
                 AND receipt.executor_operational_identity = NEW.executor_operational_identity
                 AND receipt.node_execution_id = NEW.node_execution_id
                 AND receipt.run_id = NEW.run_id
                 AND receipt.workflow_revision_hash = NEW.workflow_revision_hash
                 AND receipt.node_id = NEW.node_id
             ))
            OR
            (OLD.state = 'LAUNCH_ARMED'
             AND OLD.runner_manifest_id IS NULL
             AND OLD.failure_code IS NULL AND OLD.receipt_hash IS NULL
             AND NEW.state = 'FAILED'
             AND NEW.failure_code IN
               ('PROCESS_EXITED_UNSUCCESSFULLY', 'PROCESS_OUTPUT_LIMIT_EXCEEDED',
                'PROCESS_SUPERVISION_FAILED', 'OUTPUT_SCHEMA_REFUSED',
                'AGENT_REFUSED', 'PROJECT_VERIFICATION_FAILED',
                'CANDIDATE_CAPTURE_FAILED', 'CANDIDATE_UNCHANGED',
                'PRODUCED_VALUE_REFUSED')
             AND NEW.runner_manifest_id IS NULL
             AND NEW.receipt_hash IS NULL
             AND NEW.cancellation_command_id IS NULL)
            OR
            (OLD.state IN ('PREPARED', 'LAUNCH_ARMED')
             AND OLD.cancellation_command_id IS NULL
             AND OLD.runner_evidence_acceptance_phase = 'NONE'
             AND NEW.state = 'CANCEL_REQUESTED'
             AND NEW.cancellation_command_id IS NOT NULL
             AND NEW.cancellation_expected_state_version = OLD.state_version
             AND (OLD.runner_manifest_id IS NULL OR NEW.replacement = 'NONE')
             AND OLD.runner_manifest_id IS NEW.runner_manifest_id
             AND OLD.runner_generation_id IS NEW.runner_generation_id
             AND OLD.runner_invocation_id IS NEW.runner_invocation_id
             AND OLD.runner_terminal_evidence_hash IS NEW.runner_terminal_evidence_hash
             AND OLD.runner_evidence_acceptance_phase = NEW.runner_evidence_acceptance_phase
             AND NEW.failure_code IS NULL AND NEW.receipt_hash IS NULL)
            OR
            (OLD.state = 'CANCEL_REQUESTED'
             AND OLD.runner_manifest_id IS NULL
             AND NEW.state = 'CANCEL_REQUESTED'
             AND OLD.cancellation_command_id = NEW.cancellation_command_id
             AND NEW.redrive_state = 'OWNER_NOT_LOCAL'
             AND NEW.runner_manifest_id IS NULL
             AND NEW.failure_code IS NULL AND NEW.receipt_hash IS NULL)
            OR
            (OLD.state = 'CANCEL_REQUESTED'
             AND OLD.runner_manifest_id IS NULL
             AND NEW.state IN ('CANCELLED', 'INTERRUPTED')
             AND OLD.cancellation_command_id = NEW.cancellation_command_id
             AND NEW.process_phase = 'CLEANUP_ATTESTED'
             AND NEW.redrive_state = 'CLEANUP_ATTESTED'
             AND NEW.cancellation_disposition IS NOT NULL
             AND NEW.runner_manifest_id IS NULL
             AND NEW.failure_code IS NULL AND NEW.receipt_hash IS NULL)
            OR
            (OLD.state = 'PREPARED' AND OLD.process_phase = 'NONE'
             AND OLD.runner_manifest_id IS NULL
             AND OLD.runner_generation_id IS NULL
             AND OLD.runner_invocation_id IS NULL
             AND OLD.runner_evidence_acceptance_phase = 'NONE'
             AND NEW.state = 'PREPARED' AND NEW.process_phase = 'NONE'
             AND NEW.runner_manifest_id IS NOT NULL
             AND NEW.runner_generation_id IS NOT NULL
             AND NEW.runner_invocation_id IS NULL
             AND NEW.runner_evidence_acceptance_phase = 'NONE'
             AND NEW.cancellation_command_id IS NULL)
            OR
            (OLD.state = 'PREPARED' AND OLD.process_phase = 'NONE'
             AND OLD.runner_manifest_id = NEW.runner_manifest_id
             AND OLD.runner_generation_id = NEW.runner_generation_id
             AND OLD.runner_invocation_id IS NULL
             AND OLD.runner_evidence_acceptance_phase = 'NONE'
             AND NEW.state = 'LAUNCH_ARMED' AND NEW.process_phase = 'NONE'
             AND NEW.runner_invocation_id IS NOT NULL
             AND NEW.runner_evidence_acceptance_phase = 'NONE'
             AND NEW.cancellation_command_id IS NULL)
            OR
            (OLD.state = 'PREPARED' AND OLD.process_phase = 'NONE'
             AND OLD.runner_manifest_id = NEW.runner_manifest_id
             AND OLD.runner_generation_id = NEW.runner_generation_id
             AND OLD.runner_invocation_id IS NULL
             AND OLD.runner_evidence_acceptance_phase = 'NONE'
             AND NEW.state = 'PREPARED' AND NEW.process_phase = 'NONE'
             AND NEW.runner_terminal_evidence_hash IS NOT NULL
             AND NEW.runner_evidence_acceptance_phase = 'CORE_COMMITTED'
             AND NEW.cancellation_command_id IS NULL)
            OR
            (OLD.state = 'LAUNCH_ARMED'
             AND OLD.runner_manifest_id = NEW.runner_manifest_id
             AND OLD.runner_generation_id = NEW.runner_generation_id
             AND OLD.runner_invocation_id = NEW.runner_invocation_id
             AND OLD.runner_evidence_acceptance_phase = 'NONE'
             AND NEW.state = 'LAUNCH_ARMED'
             AND NEW.runner_terminal_evidence_hash IS NOT NULL
             AND NEW.runner_evidence_acceptance_phase = 'CORE_COMMITTED'
             AND NEW.cancellation_command_id IS NULL)
            OR
            (OLD.state IN ('LAUNCH_ARMED', 'CANCEL_REQUESTED')
             AND OLD.runner_manifest_id = NEW.runner_manifest_id
             AND OLD.runner_generation_id = NEW.runner_generation_id
             AND OLD.runner_invocation_id = NEW.runner_invocation_id
             AND OLD.runner_evidence_acceptance_phase = 'NONE'
             AND (OLD.state <> 'CANCEL_REQUESTED' OR OLD.replacement = 'NONE')
             AND NEW.state = 'SUCCEEDED'
             AND NEW.runner_terminal_evidence_hash IS NOT NULL
             AND NEW.runner_evidence_acceptance_phase = 'CORE_COMMITTED'
             AND NEW.cancellation_command_id IS NULL
             AND NEW.failure_code IS NULL AND NEW.receipt_hash IS NOT NULL
             AND EXISTS (
               SELECT 1 FROM agent_receipts_v2 AS receipt
               WHERE receipt.receipt_hash = NEW.receipt_hash
                 AND receipt.request_hash = NEW.request_hash
                 AND receipt.executor_operational_identity = NEW.executor_operational_identity
                 AND receipt.node_execution_id = NEW.node_execution_id
                 AND receipt.run_id = NEW.run_id
                 AND receipt.workflow_revision_hash = NEW.workflow_revision_hash
                 AND receipt.node_id = NEW.node_id
             ))
            OR
            (OLD.state IN ('LAUNCH_ARMED', 'CANCEL_REQUESTED')
             AND OLD.runner_manifest_id = NEW.runner_manifest_id
             AND OLD.runner_generation_id = NEW.runner_generation_id
             AND OLD.runner_invocation_id = NEW.runner_invocation_id
             AND OLD.runner_evidence_acceptance_phase = 'NONE'
             AND (OLD.state <> 'CANCEL_REQUESTED' OR OLD.replacement = 'NONE')
             AND NEW.state = 'FAILED'
             AND NEW.runner_terminal_evidence_hash IS NOT NULL
             AND NEW.runner_evidence_acceptance_phase = 'CORE_COMMITTED'
             AND NEW.cancellation_command_id IS NULL
             AND NEW.failure_code IN
               ('PROCESS_EXITED_UNSUCCESSFULLY', 'PROCESS_OUTPUT_LIMIT_EXCEEDED',
                'PROCESS_SUPERVISION_FAILED', 'OUTPUT_SCHEMA_REFUSED',
                'AGENT_REFUSED', 'PROJECT_VERIFICATION_FAILED',
                'CANDIDATE_CAPTURE_FAILED', 'CANDIDATE_UNCHANGED',
                'PRODUCED_VALUE_REFUSED')
             AND NEW.receipt_hash IS NULL)
            OR
            (OLD.state = 'CANCEL_REQUESTED'
             AND OLD.runner_manifest_id = NEW.runner_manifest_id
             AND OLD.runner_generation_id = NEW.runner_generation_id
             AND OLD.runner_invocation_id = NEW.runner_invocation_id
             AND OLD.runner_evidence_acceptance_phase = 'NONE'
             AND OLD.replacement = 'NONE'
             AND NEW.state = 'CANCELLED' AND NEW.process_phase = 'NONE'
             AND OLD.cancellation_command_id = NEW.cancellation_command_id
             AND NEW.redrive_state = 'CLEANUP_ATTESTED'
             AND NEW.cancellation_disposition IN
               ('EXITED_BEFORE_SIGNAL', 'REAPED_AFTER_TERM', 'REAPED_AFTER_KILL')
             AND NEW.runner_terminal_evidence_hash IS NOT NULL
             AND NEW.runner_evidence_acceptance_phase = 'CORE_COMMITTED'
             AND NEW.failure_code IS NULL AND NEW.receipt_hash IS NULL)
            OR
            (OLD.state = 'CANCEL_REQUESTED'
             AND OLD.runner_manifest_id IS NOT NULL
             AND OLD.runner_manifest_id = NEW.runner_manifest_id
             AND OLD.runner_generation_id = NEW.runner_generation_id
             AND OLD.runner_invocation_id IS NULL
             AND NEW.runner_invocation_id IS NULL
             AND OLD.runner_evidence_acceptance_phase = 'NONE'
             AND NEW.runner_evidence_acceptance_phase = 'NONE'
             AND OLD.runner_terminal_evidence_hash IS NULL
             AND NEW.runner_terminal_evidence_hash IS NULL
             AND OLD.replacement = 'NONE'
             AND NEW.state = 'CANCELLED' AND NEW.process_phase = 'NONE'
             AND OLD.cancellation_command_id = NEW.cancellation_command_id
             AND NEW.redrive_state = 'CLEANUP_ATTESTED'
             AND NEW.cancellation_disposition = 'NEVER_LAUNCHED'
             AND NEW.failure_code IS NULL AND NEW.receipt_hash IS NULL)
            OR
            (OLD.state = NEW.state
             AND OLD.process_phase = NEW.process_phase
             AND OLD.process_owner_id IS NEW.process_owner_id
             AND OLD.watchdog_generation_id IS NEW.watchdog_generation_id
             AND OLD.cancellation_command_id IS NEW.cancellation_command_id
             AND OLD.cancellation_expected_state_version IS NEW.cancellation_expected_state_version
             AND OLD.replacement IS NEW.replacement
             AND OLD.redrive_state IS NEW.redrive_state
             AND OLD.cancellation_disposition IS NEW.cancellation_disposition
             AND OLD.cancellation_workflow_id IS NEW.cancellation_workflow_id
             AND OLD.failure_code IS NEW.failure_code
             AND OLD.receipt_hash IS NEW.receipt_hash
             AND OLD.runner_manifest_id = NEW.runner_manifest_id
             AND OLD.runner_generation_id = NEW.runner_generation_id
             AND OLD.runner_invocation_id IS NEW.runner_invocation_id
             AND OLD.runner_terminal_evidence_hash = NEW.runner_terminal_evidence_hash
             AND OLD.runner_evidence_acceptance_phase = 'CORE_COMMITTED'
             AND NEW.runner_evidence_acceptance_phase = 'ACKNOWLEDGED')
            OR
            (OLD.state = 'PREPARED' AND NEW.state = 'PREPARED'
             AND OLD.process_phase = 'NONE' AND NEW.process_phase = 'NONE'
             AND OLD.runner_manifest_id IS NOT NULL
             AND OLD.runner_generation_id IS NOT NULL
             AND OLD.runner_evidence_acceptance_phase = 'ACKNOWLEDGED'
             AND NEW.runner_manifest_id IS NOT NULL
             AND NEW.runner_generation_id IS NOT NULL
             AND NEW.runner_generation_id <> OLD.runner_generation_id
             AND NEW.runner_invocation_id IS NULL
             AND NEW.runner_terminal_evidence_hash IS NULL
             AND NEW.runner_evidence_acceptance_phase = 'NONE'
             AND NEW.cancellation_command_id IS NULL
             AND NEW.failure_code IS NULL AND NEW.receipt_hash IS NULL)
          )
        ) BEGIN
          SELECT RAISE(ABORT, 'invalid agent attempt transition');
        END
    """,
    "agent_attempts_no_delete": """
        CREATE TRIGGER agent_attempts_no_delete
        BEFORE DELETE ON agent_attempts BEGIN
          SELECT RAISE(ABORT, 'agent attempts are immutable');
        END
    """,
    "wait_answers_payload_no_update": """
        CREATE TRIGGER wait_answers_payload_no_update
        BEFORE UPDATE OF run_id, revision_hash, node_id, node_execution_id,
                         round_ordinal, actor, actor_attribution_kind,
                         answer_bytes, answer_hash,
                         answer_workflow_id
        ON wait_answers BEGIN
          SELECT RAISE(ABORT, 'wait answer bindings are immutable');
        END
    """,
    "wait_answers_state_transition": """
        CREATE TRIGGER wait_answers_state_transition
        BEFORE UPDATE OF state, state_version ON wait_answers
        WHEN NOT (OLD.state = 'PENDING' AND OLD.state_version = 0
                  AND NEW.state = 'APPLIED' AND NEW.state_version = 1)
        BEGIN
          SELECT RAISE(ABORT, 'invalid wait answer transition');
        END
    """,
    "wait_answers_no_delete": """
        CREATE TRIGGER wait_answers_no_delete
        BEFORE DELETE ON wait_answers BEGIN
          SELECT RAISE(ABORT, 'wait answers are immutable');
        END
    """,
    "published_revisions_no_update": """
        CREATE TRIGGER published_revisions_no_update
        BEFORE UPDATE ON published_revisions BEGIN
          SELECT RAISE(ABORT, 'published revisions are immutable');
        END
    """,
    "published_revisions_no_delete": """
        CREATE TRIGGER published_revisions_no_delete
        BEFORE DELETE ON published_revisions BEGIN
          SELECT RAISE(ABORT, 'published revisions are immutable');
        END
    """,
    "catalog_lineages_no_update": """
        CREATE TRIGGER catalog_lineages_no_update
        BEFORE UPDATE ON catalog_lineages BEGIN
          SELECT RAISE(ABORT, 'catalog lineages are immutable');
        END
    """,
    "catalog_lineages_no_delete": """
        CREATE TRIGGER catalog_lineages_no_delete
        BEFORE DELETE ON catalog_lineages BEGIN
          SELECT RAISE(ABORT, 'catalog lineages are immutable');
        END
    """,
    "catalog_lineage_members_must_be_published": """
        CREATE TRIGGER catalog_lineage_members_must_be_published
        BEFORE INSERT ON catalog_lineage_members
        WHEN NOT EXISTS (
          SELECT 1
          FROM catalog_lineages AS lineage
          JOIN published_revisions AS revision
            ON revision.kind = lineage.kind
           AND revision.revision_hash = NEW.revision_hash
          WHERE lineage.lineage_id = NEW.lineage_id
        )
        BEGIN
          SELECT RAISE(
            ABORT,
            'catalog lineage members must name a published revision of the lineage kind'
          );
        END
    """,
    "catalog_lineage_members_no_update": """
        CREATE TRIGGER catalog_lineage_members_no_update
        BEFORE UPDATE ON catalog_lineage_members BEGIN
          SELECT RAISE(ABORT, 'catalog lineage members are immutable');
        END
    """,
    "catalog_lineage_members_no_delete": """
        CREATE TRIGGER catalog_lineage_members_no_delete
        BEFORE DELETE ON catalog_lineage_members BEGIN
          SELECT RAISE(ABORT, 'catalog lineage members are immutable');
        END
    """,
    "catalog_lineage_members_unique_per_kind": """
        CREATE TRIGGER catalog_lineage_members_unique_per_kind
        BEFORE INSERT ON catalog_lineage_members
        WHEN EXISTS (
          SELECT 1
          FROM catalog_lineage_members AS existing
          JOIN catalog_lineages AS existing_lineage
            ON existing_lineage.lineage_id = existing.lineage_id
          JOIN catalog_lineages AS new_lineage
            ON new_lineage.lineage_id = NEW.lineage_id
          WHERE existing.revision_hash = NEW.revision_hash
            AND existing_lineage.kind = new_lineage.kind
            AND existing.lineage_id <> NEW.lineage_id
        )
        BEGIN
          SELECT RAISE(
            ABORT,
            'catalog lineage members of one kind cannot share a revision'
          );
        END
    """,
    "catalog_lineage_aliases_name_unique_per_kind": """
        CREATE TRIGGER catalog_lineage_aliases_name_unique_per_kind
        BEFORE INSERT ON catalog_lineage_aliases
        WHEN EXISTS (
          SELECT 1
          FROM catalog_lineage_aliases AS existing
          JOIN catalog_lineages AS existing_lineage
            ON existing_lineage.lineage_id = existing.lineage_id
          JOIN catalog_lineages AS new_lineage
            ON new_lineage.lineage_id = NEW.lineage_id
          WHERE existing.name = NEW.name
            AND existing_lineage.kind = new_lineage.kind
            AND existing.lineage_id <> NEW.lineage_id
        )
        BEGIN
          SELECT RAISE(
            ABORT,
            'catalog lineage names are never reused across lineages of one kind'
          );
        END
    """,
    "catalog_lineage_aliases_no_update": """
        CREATE TRIGGER catalog_lineage_aliases_no_update
        BEFORE UPDATE ON catalog_lineage_aliases BEGIN
          SELECT RAISE(ABORT, 'catalog lineage aliases are immutable');
        END
    """,
    "catalog_lineage_aliases_no_delete": """
        CREATE TRIGGER catalog_lineage_aliases_no_delete
        BEFORE DELETE ON catalog_lineage_aliases BEGIN
          SELECT RAISE(ABORT, 'catalog lineage aliases are immutable');
        END
    """,
    "catalog_lineage_retirements_no_update": """
        CREATE TRIGGER catalog_lineage_retirements_no_update
        BEFORE UPDATE ON catalog_lineage_retirements BEGIN
          SELECT RAISE(ABORT, 'catalog lineage retirements are immutable');
        END
    """,
    "catalog_lineage_retirements_no_delete": """
        CREATE TRIGGER catalog_lineage_retirements_no_delete
        BEFORE DELETE ON catalog_lineage_retirements BEGIN
          SELECT RAISE(ABORT, 'catalog lineage retirements are immutable');
        END
    """,
    "host_definition_source_revisions_no_update": """
        CREATE TRIGGER host_definition_source_revisions_no_update
        BEFORE UPDATE ON host_definition_source_revisions BEGIN
          SELECT RAISE(ABORT, 'definition source revisions are immutable');
        END
    """,
    "host_definition_source_revisions_no_delete": """
        CREATE TRIGGER host_definition_source_revisions_no_delete
        BEFORE DELETE ON host_definition_source_revisions BEGIN
          SELECT RAISE(ABORT, 'definition source revisions are immutable');
        END
    """,
    "host_definition_source_selections_no_update": """
        CREATE TRIGGER host_definition_source_selections_no_update
        BEFORE UPDATE ON host_definition_source_selections BEGIN
          SELECT RAISE(ABORT, 'definition source selections are immutable');
        END
    """,
    "host_definition_source_selections_no_delete": """
        CREATE TRIGGER host_definition_source_selections_no_delete
        BEFORE DELETE ON host_definition_source_selections BEGIN
          SELECT RAISE(ABORT, 'definition source selections are immutable');
        END
    """,
    "catalog_source_intakes_no_update": """
        CREATE TRIGGER catalog_source_intakes_no_update
        BEFORE UPDATE ON catalog_source_intakes BEGIN
          SELECT RAISE(ABORT, 'catalog source intakes are immutable');
        END
    """,
    "catalog_source_intakes_no_delete": """
        CREATE TRIGGER catalog_source_intakes_no_delete
        BEFORE DELETE ON catalog_source_intakes BEGIN
          SELECT RAISE(ABORT, 'catalog source intakes are immutable');
        END
    """,
    "catalog_intakes_no_update": """
        CREATE TRIGGER catalog_intakes_no_update
        BEFORE UPDATE ON catalog_intakes BEGIN
          SELECT RAISE(ABORT, 'catalog intakes are immutable');
        END
    """,
    "catalog_intakes_no_delete": """
        CREATE TRIGGER catalog_intakes_no_delete
        BEFORE DELETE ON catalog_intakes BEGIN
          SELECT RAISE(ABORT, 'catalog intakes are immutable');
        END
    """,
    "node_artifacts_v3_no_update": """
        CREATE TRIGGER node_artifacts_v3_no_update
        BEFORE UPDATE ON node_artifacts_v3 BEGIN
          SELECT RAISE(ABORT, 'v3 node artifacts are immutable');
        END
    """,
    "node_artifacts_v3_no_delete": """
        CREATE TRIGGER node_artifacts_v3_no_delete
        BEFORE DELETE ON node_artifacts_v3 BEGIN
          SELECT RAISE(ABORT, 'v3 node artifacts are immutable');
        END
    """,
    "node_receipts_v3_no_update": """
        CREATE TRIGGER node_receipts_v3_no_update
        BEFORE UPDATE ON node_receipts_v3 BEGIN
          SELECT RAISE(ABORT, 'v3 node receipts are immutable');
        END
    """,
    "node_receipts_v3_no_delete": """
        CREATE TRIGGER node_receipts_v3_no_delete
        BEFORE DELETE ON node_receipts_v3 BEGIN
          SELECT RAISE(ABORT, 'v3 node receipts are immutable');
        END
    """,
    "node_receipt_outputs_v3_no_update": """
        CREATE TRIGGER node_receipt_outputs_v3_no_update
        BEFORE UPDATE ON node_receipt_outputs_v3 BEGIN
          SELECT RAISE(ABORT, 'v3 node receipt outputs are immutable');
        END
    """,
    "node_receipt_outputs_v3_no_delete": """
        CREATE TRIGGER node_receipt_outputs_v3_no_delete
        BEFORE DELETE ON node_receipt_outputs_v3 BEGIN
          SELECT RAISE(ABORT, 'v3 node receipt outputs are immutable');
        END
    """,
    "run_instants_start_no_update": """
        CREATE TRIGGER run_instants_start_no_update
        BEFORE UPDATE OF run_id, started_at ON run_instants BEGIN
          SELECT RAISE(ABORT, 'run start instant is immutable');
        END
    """,
    "run_instants_end_once": """
        CREATE TRIGGER run_instants_end_once
        BEFORE UPDATE OF ended_at ON run_instants
        WHEN OLD.ended_at IS NOT NULL OR NEW.ended_at IS NULL BEGIN
          SELECT RAISE(ABORT, 'run end instant is written once');
        END
    """,
    "run_instants_no_delete": """
        CREATE TRIGGER run_instants_no_delete
        BEFORE DELETE ON run_instants BEGIN
          SELECT RAISE(ABORT, 'run instants are immutable');
        END
    """,
    "attempt_instants_start_no_update": """
        CREATE TRIGGER attempt_instants_start_no_update
        BEFORE UPDATE OF attempt_id, started_at ON attempt_instants BEGIN
          SELECT RAISE(ABORT, 'attempt start instant is immutable');
        END
    """,
    "attempt_instants_end_once": """
        CREATE TRIGGER attempt_instants_end_once
        BEFORE UPDATE OF ended_at ON attempt_instants
        WHEN OLD.ended_at IS NOT NULL OR NEW.ended_at IS NULL BEGIN
          SELECT RAISE(ABORT, 'attempt end instant is written once');
        END
    """,
    "attempt_instants_no_delete": """
        CREATE TRIGGER attempt_instants_no_delete
        BEFORE DELETE ON attempt_instants BEGIN
          SELECT RAISE(ABORT, 'attempt instants are immutable');
        END
    """,
    "event_instants_no_update": """
        CREATE TRIGGER event_instants_no_update
        BEFORE UPDATE ON event_instants BEGIN
          SELECT RAISE(ABORT, 'event instants are immutable');
        END
    """,
    "event_instants_no_delete": """
        CREATE TRIGGER event_instants_no_delete
        BEFORE DELETE ON event_instants BEGIN
          SELECT RAISE(ABORT, 'event instants are immutable');
        END
    """,
    "host_project_root_revisions_no_update": """
        CREATE TRIGGER host_project_root_revisions_no_update
        BEFORE UPDATE ON host_project_root_revisions BEGIN
          SELECT RAISE(ABORT, 'host project-root revisions are immutable');
        END
    """,
    "host_project_root_revisions_no_delete": """
        CREATE TRIGGER host_project_root_revisions_no_delete
        BEFORE DELETE ON host_project_root_revisions BEGIN
          SELECT RAISE(ABORT, 'host project-root revisions are immutable');
        END
    """,
    "host_model_registry_revisions_no_update": """
        CREATE TRIGGER host_model_registry_revisions_no_update
        BEFORE UPDATE ON host_model_registry_revisions BEGIN
          SELECT RAISE(ABORT, 'host model registry revisions are immutable');
        END
    """,
    "host_model_registry_revisions_no_delete": """
        CREATE TRIGGER host_model_registry_revisions_no_delete
        BEFORE DELETE ON host_model_registry_revisions BEGIN
          SELECT RAISE(ABORT, 'host model registry revisions are immutable');
        END
    """,
    "host_model_registry_entries_no_update": """
        CREATE TRIGGER host_model_registry_entries_no_update
        BEFORE UPDATE ON host_model_registry_entries BEGIN
          SELECT RAISE(ABORT, 'host model registry entries are immutable');
        END
    """,
    "host_model_registry_entries_no_delete": """
        CREATE TRIGGER host_model_registry_entries_no_delete
        BEFORE DELETE ON host_model_registry_entries BEGIN
          SELECT RAISE(ABORT, 'host model registry entries are immutable');
        END
    """,
    "host_project_model_defaults_revisions_no_update": """
        CREATE TRIGGER host_project_model_defaults_revisions_no_update
        BEFORE UPDATE ON host_project_model_defaults_revisions BEGIN
          SELECT RAISE(ABORT, 'host project model-default revisions are immutable');
        END
    """,
    "host_project_model_defaults_revisions_no_delete": """
        CREATE TRIGGER host_project_model_defaults_revisions_no_delete
        BEFORE DELETE ON host_project_model_defaults_revisions BEGIN
          SELECT RAISE(ABORT, 'host project model-default revisions are immutable');
        END
    """,
    "host_project_model_defaults_no_update": """
        CREATE TRIGGER host_project_model_defaults_no_update
        BEFORE UPDATE ON host_project_model_defaults BEGIN
          SELECT RAISE(ABORT, 'host project model defaults are immutable');
        END
    """,
    "host_project_model_defaults_no_delete": """
        CREATE TRIGGER host_project_model_defaults_no_delete
        BEFORE DELETE ON host_project_model_defaults BEGIN
          SELECT RAISE(ABORT, 'host project model defaults are immutable');
        END
    """,
    "host_project_source_connection_revisions_no_update": """
        CREATE TRIGGER host_project_source_connection_revisions_no_update
        BEFORE UPDATE ON host_project_source_connection_revisions BEGIN
          SELECT RAISE(ABORT, 'project-source connection revisions are immutable');
        END
    """,
    "host_project_source_connection_revisions_no_delete": """
        CREATE TRIGGER host_project_source_connection_revisions_no_delete
        BEFORE DELETE ON host_project_source_connection_revisions BEGIN
          SELECT RAISE(ABORT, 'project-source connection revisions are immutable');
        END
    """,
    "queue_items_identity_no_update": """
        CREATE TRIGGER queue_items_identity_no_update
        BEFORE UPDATE OF item_id, project_id, tracker_item_reference
        ON queue_items BEGIN
          SELECT RAISE(ABORT, 'queue item identity is immutable');
        END
    """,
    "queue_items_no_delete": """
        CREATE TRIGGER queue_items_no_delete
        BEFORE DELETE ON queue_items BEGIN
          SELECT RAISE(ABORT, 'queue items are immutable');
        END
    """,
    "queue_items_no_nonobserved_insert": """
        CREATE TRIGGER queue_items_no_nonobserved_insert
        BEFORE INSERT ON queue_items
        WHEN NEW.state <> 'OBSERVED' OR NEW.state_version <> 0
          OR NEW.workflow_lineage_id IS NOT NULL
          OR NEW.admission_rationale IS NOT NULL
          OR NEW.current_proposal_revision IS NOT NULL
          OR NEW.decision_authority IS NOT NULL
        BEGIN
          SELECT RAISE(ABORT, 'queue items begin observed without a decision');
        END
    """,
    "queue_items_state_transition": """
        CREATE TRIGGER queue_items_state_transition
        BEFORE UPDATE ON queue_items
        WHEN NOT (
          (OLD.state = 'OBSERVED'
           AND NEW.state = 'PROPOSED'
           AND NEW.state_version = OLD.state_version + 1
           AND NEW.current_proposal_revision = NEW.state_version
           AND NEW.workflow_lineage_id IS NULL
           AND NEW.admission_rationale IS NULL
           AND NEW.decision_authority IS NULL
           AND EXISTS (
             SELECT 1 FROM queue_proposal_revisions AS proposal
             WHERE proposal.item_id = OLD.item_id
               AND proposal.proposal_revision = NEW.current_proposal_revision
           ))
          OR
          (OLD.state = 'PROPOSED'
           AND NEW.state = 'ADMITTED'
           AND NEW.state_version = OLD.state_version + 1
           AND NEW.current_proposal_revision = OLD.current_proposal_revision
           AND NEW.workflow_lineage_id = (
             SELECT proposal.workflow_lineage_id
             FROM queue_proposal_revisions AS proposal
             WHERE proposal.item_id = OLD.item_id
               AND proposal.proposal_revision = OLD.current_proposal_revision
           )
           AND NEW.admission_rationale IS NOT NULL
           AND NEW.decision_authority IN ('OPERATOR', 'AUTOMATION_RULE')
           AND (NEW.decision_authority = 'OPERATOR' OR EXISTS (
             SELECT 1 FROM queue_proposal_revisions AS proposal
             WHERE proposal.item_id = OLD.item_id
               AND proposal.proposal_revision = OLD.current_proposal_revision
               AND proposal.automation_disposition = 'AUTOMATION_AUTHORIZED'
           )))
          OR
          (OLD.state = 'ADMITTED' AND NEW.state = 'ADMITTED'
           AND NEW.state_version = OLD.state_version + 1
           AND NEW.current_proposal_revision = OLD.current_proposal_revision + 1
           AND NEW.workflow_lineage_id IS OLD.workflow_lineage_id
           AND NEW.admission_rationale IS OLD.admission_rationale
           AND NEW.decision_authority IS OLD.decision_authority
           AND EXISTS (
             SELECT 1 FROM queue_launch_bindings AS binding
             WHERE binding.item_id = OLD.item_id
               AND binding.proposal_revision = OLD.current_proposal_revision
               AND binding.ended_run_state IS NOT NULL))
          OR
          (NEW.state = OLD.state
           AND NEW.state_version = OLD.state_version
           AND NEW.workflow_lineage_id IS OLD.workflow_lineage_id
           AND NEW.admission_rationale IS OLD.admission_rationale
           AND NEW.current_proposal_revision IS OLD.current_proposal_revision
           AND NEW.decision_authority IS OLD.decision_authority)
        ) BEGIN
          SELECT RAISE(ABORT, 'invalid queue item transition');
        END
    """,
    "queue_project_policy_revisions_no_update": """
        CREATE TRIGGER queue_project_policy_revisions_no_update
        BEFORE UPDATE ON queue_project_policy_revisions BEGIN
          SELECT RAISE(ABORT, 'queue policy revisions are immutable');
        END
    """,
    "queue_project_policy_revisions_no_delete": """
        CREATE TRIGGER queue_project_policy_revisions_no_delete
        BEFORE DELETE ON queue_project_policy_revisions BEGIN
          SELECT RAISE(ABORT, 'queue policy revisions are immutable');
        END
    """,
    "queue_proposal_revisions_no_update": """
        CREATE TRIGGER queue_proposal_revisions_no_update
        BEFORE UPDATE ON queue_proposal_revisions BEGIN
          SELECT RAISE(ABORT, 'queue proposal revisions are immutable');
        END
    """,
    "queue_proposal_revisions_no_delete": """
        CREATE TRIGGER queue_proposal_revisions_no_delete
        BEFORE DELETE ON queue_proposal_revisions BEGIN
          SELECT RAISE(ABORT, 'queue proposal revisions are immutable');
        END
    """,
    "queue_dependency_edges_no_update": """
        CREATE TRIGGER queue_dependency_edges_no_update
        BEFORE UPDATE ON queue_dependency_edges BEGIN
          SELECT RAISE(ABORT, 'queue dependency edges are immutable');
        END
    """,
    "queue_dependency_edges_no_delete": """
        CREATE TRIGGER queue_dependency_edges_no_delete
        BEFORE DELETE ON queue_dependency_edges BEGIN
          SELECT RAISE(ABORT, 'queue dependency edges are immutable');
        END
    """,
    "queue_launch_bindings_release_only": """
        CREATE TRIGGER queue_launch_bindings_release_only
        BEFORE UPDATE ON queue_launch_bindings
        WHEN OLD.ended_run_state IS NOT NULL
          OR NEW.ended_run_state IS NULL
          OR NEW.item_id <> OLD.item_id
          OR NEW.proposal_revision <> OLD.proposal_revision
          OR NEW.project_id <> OLD.project_id
          OR NEW.run_id <> OLD.run_id
          OR NEW.workflow_revision_hash <> OLD.workflow_revision_hash
          OR NEW.restart_ordinal <> OLD.restart_ordinal
        BEGIN
          SELECT RAISE(ABORT, 'a queue launch binding only ever takes its ending');
        END
    """,
    "queue_launch_bindings_no_delete": """
        CREATE TRIGGER queue_launch_bindings_no_delete
        BEFORE DELETE ON queue_launch_bindings BEGIN
          SELECT RAISE(ABORT, 'queue launch bindings are immutable');
        END
    """,
    "webhook_delivery_cursor_identity_no_update": """
        CREATE TRIGGER webhook_delivery_cursor_identity_no_update
        BEFORE UPDATE OF cursor_id
        ON webhook_delivery_cursor BEGIN
          SELECT RAISE(ABORT, 'webhook delivery cursor identity is immutable');
        END
    """,
    "webhook_delivery_cursor_no_delete": """
        CREATE TRIGGER webhook_delivery_cursor_no_delete
        BEFORE DELETE ON webhook_delivery_cursor BEGIN
          SELECT RAISE(ABORT, 'the webhook delivery cursor is never deleted');
        END
    """,
}


class UnsupportedSchemaVersion(RuntimeError):
    def __init__(self, actual: object) -> None:
        super().__init__(
            f"Atelier schema version {actual!r} is unsupported; expected {SCHEMA_VERSION}"
        )


class MigrationRequired(UnsupportedSchemaVersion):
    def __init__(self, actual: int = 2) -> None:
        RuntimeError.__init__(
            self,
            f"Atelier schema version {actual} requires an explicit offline migration; "
            "runtime startup will not alter it",
        )


class StoreMigrationRefused(RuntimeError):
    """The offline migrate command will not alter this store."""


class StoreInUse(StoreMigrationRefused):
    def __init__(self) -> None:
        super().__init__(
            "the database is in use; stop the process that holds it and retry"
        )


@dataclass(frozen=True)
class StoreMigrationReport:
    source_version: int
    target_version: int
    fingerprint_sha256: str
    already_current: bool
    steps: tuple[tuple[int, int, str], ...]


def _require_supported_versions(versions: Sequence[int]) -> int:
    normalized = tuple(versions)
    if len(normalized) == 1 and normalized[0] in _OFFLINE_CUTOVER_VERSIONS:
        raise MigrationRequired(normalized[0])
    if len(normalized) != 1 or normalized[0] != SCHEMA_VERSION:
        raise UnsupportedSchemaVersion(normalized)
    return normalized[0]


@dataclass(frozen=True)
class _TableSchemaFingerprint:
    name: str
    create_sql: str
    columns: tuple[tuple[object, ...], ...]
    indexes: tuple[tuple[object, ...], ...]
    foreign_keys: tuple[tuple[object, ...], ...]


@dataclass(frozen=True)
class _ProductSchemaFingerprint:
    tables: tuple[_TableSchemaFingerprint, ...]
    triggers: tuple[tuple[str, str, str], ...]


def _quoted_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


_SQL_QUOTE_CLOSERS = {"'": "'", '"': '"', "`": "`", "[": "]"}


def _copy_quoted_span(
    source: str, start: int, closing_quote: str, target: list[str]
) -> int:
    """Copy the quoted span opening at `start` verbatim; return the index after it."""
    target.append(source[start])
    index = start + 1
    while index < len(source):
        character = source[index]
        target.append(character)
        index += 1
        if character != closing_quote:
            continue
        if index < len(source) and source[index] == closing_quote:
            target.append(closing_quote)
            index += 1
            continue
        return index
    return index


def _normalized_sql(value: object) -> str:
    if value is None:
        return ""
    source = str(value)
    normalized: list[str] = []
    pending_space = False
    index = 0
    while index < len(source):
        character = source[index]
        if character.isspace():
            pending_space = bool(normalized)
            index += 1
            continue
        if pending_space:
            normalized.append(" ")
            pending_space = False
        closing_quote = _SQL_QUOTE_CLOSERS.get(character)
        if closing_quote is None:
            normalized.append(character)
            index += 1
            continue
        index = _copy_quoted_span(source, index, closing_quote, normalized)
    return "".join(normalized)


def _table_fingerprint(
    connection: sqlite3.Connection,
    table_name: str,
    *,
    version: int = SCHEMA_VERSION,
) -> _TableSchemaFingerprint:
    create_record = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
    ).fetchone()
    if create_record is None or create_record[0] is None:
        raise UnsupportedSchemaVersion(
            f"malformed v{version} product table {table_name}"
        )
    quoted_table = _quoted_identifier(table_name)
    columns = tuple(
        tuple(record)
        for record in connection.execute(f"PRAGMA table_xinfo({quoted_table})")
    )
    indexes: list[tuple[object, ...]] = []
    for record in connection.execute(f"PRAGMA index_list({quoted_table})"):
        index_name = str(record[1])
        quoted_index = _quoted_identifier(index_name)
        index_sql_record = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
            (index_name,),
        ).fetchone()
        index_columns = tuple(
            tuple(column)
            for column in connection.execute(f"PRAGMA index_xinfo({quoted_index})")
        )
        indexes.append(
            (
                index_name,
                int(record[2]),
                str(record[3]),
                int(record[4]),
                _normalized_sql(
                    None if index_sql_record is None else index_sql_record[0]
                ),
                index_columns,
            )
        )
    foreign_keys = tuple(
        tuple(record)
        for record in connection.execute(f"PRAGMA foreign_key_list({quoted_table})")
    )
    return _TableSchemaFingerprint(
        table_name,
        _normalized_sql(create_record[0]),
        columns,
        tuple(sorted(indexes, key=lambda value: str(value[0]))),
        foreign_keys,
    )


_V27_ACCESS_TABLE_NAME = "node_receipt_access_v3"
_V27_ACCESS_TRIGGER_NAMES = (
    "node_receipt_access_v3_no_update",
    "node_receipt_access_v3_no_delete",
)


def _table_names_for_version(version: int) -> frozenset[str]:
    definition_source_tables = {
        host_definition_source_revisions.name,
        host_definition_source_selections.name,
        catalog_source_intakes.name,
    }
    before_permission_receipts = PRODUCT_TABLE_NAMES - {permission_receipts.name}
    before_definition_sources = before_permission_receipts - definition_source_tables
    predecessor_product_tables = before_definition_sources - {catalog_intakes.name}
    later = {run_instants.name, attempt_instants.name, event_instants.name}
    host_channel = {host_project_root_revisions.name}
    occupancy = {"host_occupancy_revisions", "host_occupancy_bindings"}
    model_configuration = {
        host_model_registry_revisions.name,
        host_model_registry_entries.name,
        host_project_model_defaults_revisions.name,
        host_project_model_defaults.name,
    }
    connections = {host_project_source_connection_revisions.name}
    fork_tables = {
        run_forks.name,
        run_fork_reused_nodes.name,
        run_fork_effect_fences.name,
    }
    attempt_receipt_tables = {agent_attempt_receipts_v3.name}
    phase_d_queue_tables = {
        queue_project_policy_revisions.name,
        queue_proposal_revisions.name,
        queue_dependency_edges.name,
        queue_launch_bindings.name,
    }
    before_phase_d = predecessor_product_tables - phase_d_queue_tables
    before_forks = before_phase_d - fork_tables - attempt_receipt_tables
    before_model_configuration = (before_forks - model_configuration) | occupancy
    predecessor_tables = (
        before_model_configuration
        - {queue_items.name, webhook_delivery_cursor.name}
        - connections
    ) | {_V27_ACCESS_TABLE_NAME}
    # A hop that adds or drops no table keeps its predecessor's set, so each
    # set holds from the version that shaped it up to the next one that did.
    table_names_by_floor_version = (
        (_VERSION_FIFTY_ONE, PRODUCT_TABLE_NAMES),
        (_VERSION_FORTY_NINE, before_permission_receipts),
        (_VERSION_FORTY_SEVEN, before_definition_sources),
        (_VERSION_FORTY_FOUR, predecessor_product_tables),
        (_VERSION_FORTY_THREE, before_phase_d),
        (_VERSION_FORTY_ONE, before_phase_d - attempt_receipt_tables),
        (_VERSION_FORTY, before_forks),
        (_VERSION_THIRTY_THREE, before_model_configuration),
        (_VERSION_THIRTY_ONE, before_model_configuration - connections),
        (
            _VERSION_TWENTY_NINE,
            before_model_configuration - connections - {webhook_delivery_cursor.name},
        ),
        (_VERSION_TWENTY_EIGHT, predecessor_tables - {_V27_ACCESS_TABLE_NAME}),
        (_VERSION_TWENTY_SIX, predecessor_tables),
        (_VERSION_TWENTY_FIVE, predecessor_tables - occupancy),
        (_VERSION_TWENTY_TWO, predecessor_tables - occupancy - host_channel),
        (_VERSION_NINETEEN, predecessor_tables - later - occupancy - host_channel),
        (
            _VERSION_FIFTEEN,
            predecessor_tables - {artifacts.name} - later - occupancy - host_channel,
        ),
        (
            _VERSION_FOURTEEN,
            predecessor_tables
            - {artifacts.name, tool_redemptions.name}
            - later
            - occupancy
            - host_channel,
        ),
        (
            _VERSION_THIRTEEN,
            predecessor_tables
            - {artifacts.name, run_inputs_v3.name, tool_redemptions.name}
            - later
            - occupancy
            - host_channel,
        ),
    )
    if version > SCHEMA_VERSION:
        raise UnsupportedSchemaVersion(version)
    for floor_version, table_names in table_names_by_floor_version:
        if version >= floor_version:
            return table_names
    raise UnsupportedSchemaVersion(version)


def _product_schema_fingerprint(
    connection: sqlite3.Connection,
    table_names: frozenset[str] | None = None,
    *,
    version: int = SCHEMA_VERSION,
) -> _ProductSchemaFingerprint:
    names = PRODUCT_TABLE_NAMES if table_names is None else table_names
    tables = tuple(
        _table_fingerprint(connection, table_name, version=version)
        for table_name in sorted(names)
    )
    placeholders = ",".join("?" for _ in names)
    triggers = tuple(
        (str(record[0]), str(record[1]), _normalized_sql(record[2]))
        for record in connection.execute(
            "SELECT name,tbl_name,sql FROM sqlite_master "
            f"WHERE type='trigger' AND tbl_name IN ({placeholders}) ORDER BY name",
            tuple(sorted(names)),
        )
    )
    return _ProductSchemaFingerprint(tables, triggers)


def _sqlite_connection(connection: sa.Connection) -> sqlite3.Connection:
    raw_connection = connection.connection.driver_connection
    if not isinstance(raw_connection, sqlite3.Connection):
        raise UnsupportedSchemaVersion(f"Atelier v{SCHEMA_VERSION} requires SQLite")
    return raw_connection


def _product_schema_fingerprint_sha256(
    fingerprint: _ProductSchemaFingerprint,
) -> str:
    encoded = json.dumps(
        asdict(fingerprint),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _require_product_shape(connection: sqlite3.Connection, version: int) -> None:
    try:
        observed = _product_schema_fingerprint(
            connection, _table_names_for_version(version), version=version
        )
    except UnsupportedSchemaVersion as error:
        raise UnsupportedSchemaVersion(
            f"malformed v{version} product schema fingerprint"
        ) from error
    expected = _PRODUCT_SCHEMA_FINGERPRINT_SHA256.get(version)
    if expected is None or _product_schema_fingerprint_sha256(observed) != expected:
        raise UnsupportedSchemaVersion(
            f"malformed v{version} product schema fingerprint"
        )


def _preflight_existing_schema(engine: Engine) -> int | None:
    raw_database_path = engine.url.database
    if engine.url.get_backend_name() != "sqlite" or raw_database_path is None:
        return None
    if raw_database_path in {"", ":memory:"}:
        return None
    database_path = Path(raw_database_path).resolve()
    if not database_path.is_file() or database_path.stat().st_size == 0:
        return None
    try:
        with sqlite3.connect(
            f"{database_path.as_uri()}?mode=ro", uri=True
        ) as connection:
            table_names = {
                str(record[0])
                for record in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if not table_names:
                return None
            if atelier_schema_versions.name not in table_names:
                raise UnsupportedSchemaVersion(
                    f"missing version owner beside tables {tuple(sorted(table_names))!r}"
                )
            versions: list[int] = []
            for record in connection.execute(
                "SELECT version FROM atelier_schema_versions"
            ):
                version = record[0]
                if not isinstance(version, int):
                    raise TypeError("schema version must be stored as an integer")
                versions.append(version)
            version = _require_supported_versions(versions)
            _require_product_shape(connection, version)
            return version
    except UnsupportedSchemaVersion:
        raise
    except (sqlite3.DatabaseError, TypeError, ValueError) as error:
        raise UnsupportedSchemaVersion("unreadable schema version owner") from error


def _create_triggers(connection: sa.Connection, statements: Iterable[str]) -> None:
    for statement in statements:
        connection.execute(sa.text(statement))


def _schema_version_from_connection(connection: sa.Connection) -> int | None:
    inspector = sa.inspect(connection)
    if not inspector.has_table(atelier_schema_versions.name):
        return None
    versions = connection.execute(
        sa.select(atelier_schema_versions.c.version)
    ).scalars()
    normalized: list[int] = []
    for version in versions:
        if not isinstance(version, int):
            raise UnsupportedSchemaVersion("schema version must be an integer")
        normalized.append(version)
    return _require_supported_versions(normalized)


def initialize_schema(engine: Engine) -> None:
    if engine.url.get_backend_name() != "sqlite":
        raise UnsupportedSchemaVersion(f"Atelier v{SCHEMA_VERSION} requires SQLite")
    _preflight_existing_schema(engine)
    with engine.connect() as connection:
        _schema_version_from_connection(connection)
        connection.commit()
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            inspector = sa.inspect(connection)
            if not inspector.has_table(atelier_schema_versions.name):
                existing_tables = set(inspector.get_table_names())
                if existing_tables:
                    raise UnsupportedSchemaVersion(
                        "missing version owner beside tables "
                        f"{tuple(sorted(existing_tables))!r}"
                    )
                metadata.create_all(connection)
                connection.execute(
                    atelier_schema_versions.insert().values(version=SCHEMA_VERSION)
                )
                _create_triggers(connection, _PRODUCT_TRIGGERS.values())

            locked_version = _schema_version_from_connection(connection)
            if locked_version != SCHEMA_VERSION:
                raise UnsupportedSchemaVersion(locked_version)
            _require_product_shape(_sqlite_connection(connection), SCHEMA_VERSION)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise


def _read_declared_schema_version(connection: sqlite3.Connection) -> int:
    table_names = {
        str(record[0])
        for record in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    if atelier_schema_versions.name not in table_names:
        raise StoreMigrationRefused(
            "missing version owner; this command will not alter it"
        )
    rows = connection.execute(
        f"SELECT version FROM {atelier_schema_versions.name}"
    ).fetchall()
    if len(rows) != 1 or not isinstance(rows[0][0], int):
        raise StoreMigrationRefused(
            f"schema version {tuple(row[0] for row in rows)!r} is unreadable; "
            "this command will not alter it"
        )
    return int(rows[0][0])


def _is_sqlite_lock(error: BaseException) -> bool:
    text = str(error).lower()
    return "locked" in text or "busy" in text


def _raise_declared_version(
    connection: sqlite3.Connection, source: int, target: int
) -> None:
    changed = connection.execute(
        f"UPDATE {atelier_schema_versions.name} SET version = ? WHERE version = ?",
        (target, source),
    ).rowcount
    if changed != 1:
        raise StoreMigrationRefused(
            f"schema version CAS {source} -> {target} changed nothing; "
            "this command will not alter it"
        )


# Every migration hop below asks sqlite_master the same three questions
# (does a table exist, does a named object of any kind exist, what kind is a
# named object) before it changes anything; one constant per question gives
# that repeated query text a single owner instead of a copy at each hop.
_SQLITE_MASTER_TABLE_EXISTS_QUERY = (
    "SELECT name FROM sqlite_master WHERE type='table' AND name=?"
)
_SQLITE_MASTER_NAME_EXISTS_QUERY = "SELECT name FROM sqlite_master WHERE name=?"
_SQLITE_MASTER_OBJECT_TYPE_QUERY = "SELECT type FROM sqlite_master WHERE name=?"


def _added_table_step(
    table: sa.Table,
    triggers: tuple[str, ...],
    source: int,
    target: int,
    *,
    allow_empty_prepared_table: bool = False,
) -> Callable[[sqlite3.Connection], None]:
    """One additive hop: a table this version introduces, its triggers, the CAS.

    Five published steps add exactly one immutable table, so the hop is written
    once rather than copied per version; what differs between them is only the
    table, its triggers, and the two version numbers.

    The table is created in the shape its *own* version published, which is
    today's declaration only while no later hop has moved it. A step that
    introduced a table and then built today's shape for it would leave a store
    that skipped every change since, and the next fingerprint on the way up
    would refuse it -- correctly, and far from the line that caused it.
    """

    def apply(connection: sqlite3.Connection) -> None:
        existing = connection.execute(
            _SQLITE_MASTER_TABLE_EXISTS_QUERY,
            (table.name,),
        ).fetchone()
        if existing is None:
            connection.execute(
                PUBLISHED_TABLE_SHAPES.get(
                    (target, table.name),
                    str(CreateTable(table).compile(dialect=sqlite_dialect.dialect())),
                )
            )
            for trigger in triggers:
                connection.execute(_PRODUCT_TRIGGERS[trigger])
        elif allow_empty_prepared_table and _table_is_empty(connection, table.name):
            _create_missing_triggers(connection, triggers)
        else:
            raise StoreMigrationRefused(
                f"schema version {source} already has {table.name}; "
                "this command will not alter it"
            )
        _raise_declared_version(connection, source, target)

    return apply


def _table_is_empty(connection: sqlite3.Connection, table_name: str) -> bool:
    return connection.execute(f"SELECT count(*) FROM {table_name}").fetchone() == (0,)


def _create_missing_triggers(
    connection: sqlite3.Connection, triggers: tuple[str, ...]
) -> None:
    for trigger in triggers:
        trigger_exists = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name=?",
            (trigger,),
        ).fetchone()
        if trigger_exists is None:
            connection.execute(_PRODUCT_TRIGGERS[trigger])


def _declared_indexes(table: sa.Table) -> Mapping[str, str]:
    """The `CREATE INDEX` text the declaration gives each of this table's indexes."""

    return {
        str(index.name): str(
            CreateIndex(index).compile(dialect=sqlite_dialect.dialect())
        )
        for index in sorted(table.indexes, key=lambda index: index.name or "")
    }


def _table_indexes_at(version: int, table: sa.Table) -> tuple[str, ...]:
    """The `CREATE INDEX` texts this table carries at one published version.

    An index set no hop has moved is exactly the declaration, which is why most
    published versions record none; a version a later hop moved an index of is a
    record, for the reason `published_schema_shapes` gives.
    """
    if version == SCHEMA_VERSION:
        return tuple(_declared_indexes(table).values())
    recorded = PUBLISHED_TABLE_INDEXES.get((version, table.name))
    return tuple(_declared_indexes(table).values()) if recorded is None else recorded


def _table_shape_at(version: int, table: sa.Table) -> str:
    """The `CREATE TABLE` text this table has at one published schema version.

    The current version is the declaration; every earlier one is a record, and
    `published_schema_shapes` says why it may not be derived.
    """
    if version == SCHEMA_VERSION:
        return str(CreateTable(table).compile(dialect=sqlite_dialect.dialect()))
    frozen_shape = PUBLISHED_TABLE_SHAPES.get((version, table.name))
    if frozen_shape is not None:
        return frozen_shape
    raise StoreMigrationRefused(
        f"no published shape of {table.name} at schema version {version} is "
        "recorded, so this hop cannot rebuild it"
    )


def _column_names(connection: sqlite3.Connection, table_name: str) -> tuple[str, ...]:
    return tuple(
        str(record[1])
        for record in connection.execute(f"PRAGMA table_info({table_name})")
    )


def _columns_a_row_must_carry(
    connection: sqlite3.Connection, table_name: str
) -> frozenset[str]:
    """The columns of this table no stored row may leave empty.

    Read from the table SQLite actually holds rather than from the declaration:
    a rebuild materialises the shape of its own target version, and only the
    newest of those is the declaration.
    """
    return frozenset(
        str(record[1])
        for record in connection.execute(f"PRAGMA table_info({table_name})")
        if int(record[3]) == 1 and record[4] is None
    )


def _created_index_names(
    connection: sqlite3.Connection, table_name: str
) -> tuple[str, ...]:
    """The named indexes on this table, without the ones a key implies.

    An index SQLite creates for a UNIQUE or PRIMARY KEY declaration has no
    `CREATE INDEX` text of its own and cannot be dropped; it goes and comes with
    the table shape that declares it.
    """
    return tuple(
        str(record[0])
        for record in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=? "
            "AND sql IS NOT NULL ORDER BY name",
            (table_name,),
        )
    )


def _rebuild_product_table(
    connection: sqlite3.Connection,
    table: sa.Table,
    parked_name: str,
    triggers: tuple[str, ...],
    source_version: int,
    target_version: int,
    filled_columns: Mapping[str, str] = {},
    trigger_source: Mapping[str, str] | None = None,
) -> None:
    """Republish one table in its target shape and carry every stored row over.

    SQLite changes neither a key nor a constraint in place, so every shape hop is
    this same rebuild: park the predecessor, create the target shape, copy by
    column name, drop the predecessor. Which columns are carried is read from the
    two tables themselves rather than hand-kept per step, so a column a hop adds
    is simply not carried and a column it drops simply stops being.

    `filled_columns` names the value each carried row gets in a column the
    predecessor does not have. A column that needs one and has none is refused
    here by name -- the store would otherwise answer with an integrity error
    about a column the operator never heard of.

    `trigger_source` is the trigger text to install after the rebuild. Earlier
    hops that rebuild this table must reinstall the trigger of *their* target,
    not today's, or an intermediate fingerprint breaks. The indexes are read the
    same way, by target version, so a hop that takes an index away leaves every
    earlier hop rebuilding the index set its own target published.
    """

    trigger_sql = _PRODUCT_TRIGGERS if trigger_source is None else trigger_source

    # The fingerprint this store was checked against says nothing about objects
    # outside the product schema, so any object holding the parking name is
    # refused before the first statement rather than overwritten.
    if connection.execute(_SQLITE_MASTER_NAME_EXISTS_QUERY, (parked_name,)).fetchone():
        raise StoreMigrationRefused(
            f"schema version {source_version} already has {parked_name}; "
            "this command will not alter it"
        )
    for trigger in triggers:
        connection.execute(f"DROP TRIGGER {trigger}")
    # Read from the store rather than from the declaration: an index name is
    # global in SQLite, so what has to go before the target shape is created is
    # every index this store actually holds -- including one a later hop has
    # already taken out of the declaration.
    for index_name in _created_index_names(connection, table.name):
        connection.execute(f"DROP INDEX {index_name}")
    # Children declare their foreign keys on this table by name, and a plain
    # rename would rewrite them to point at the predecessor this hop drops.
    connection.execute("PRAGMA legacy_alter_table=ON")
    try:
        connection.execute(f"ALTER TABLE {table.name} RENAME TO {parked_name}")
    finally:
        connection.execute("PRAGMA legacy_alter_table=OFF")
    connection.execute(_table_shape_at(target_version, table))
    parked_columns = set(_column_names(connection, parked_name))
    carried = [
        name for name in _column_names(connection, table.name) if name in parked_columns
    ]
    unfillable = (
        _columns_a_row_must_carry(connection, table.name)
        - parked_columns
        - set(filled_columns)
    )
    if unfillable:
        raise StoreMigrationRefused(
            f"schema version {target_version} adds {', '.join(sorted(unfillable))} to "
            f"{table.name} and no value is declared for the rows already stored"
        )
    written = ", ".join(carried + list(filled_columns))
    read = ", ".join(carried + list(filled_columns.values()))
    connection.execute(
        f"INSERT INTO {table.name} ({written}) SELECT {read} FROM {parked_name}"
    )
    connection.execute(f"DROP TABLE {parked_name}")
    for index_statement in _table_indexes_at(target_version, table):
        connection.execute(index_statement)
    for trigger in triggers:
        connection.execute(trigger_sql[trigger])


_RUN_EVENTS_TRIGGERS = ("run_events_no_update", "run_events_no_delete")
_PREDECESSOR_RUN_EVENTS = "run_events_before_the_receipt_column"


def _apply_v15_to_v16(connection: sqlite3.Connection) -> None:
    """Give an event the column v3 of its hash binds, and keep every stored row.

    Nothing already written is reinterpreted: an event from before this version
    carries NULL, which is what "this attempt recorded no receipt binding"
    means, and never an invented hash.
    """

    _rebuild_product_table(
        connection,
        run_events,
        _PREDECESSOR_RUN_EVENTS,
        _RUN_EVENTS_TRIGGERS,
        _VERSION_FIFTEEN,
        _VERSION_SIXTEEN,
    )
    _raise_declared_version(connection, _VERSION_FIFTEEN, _VERSION_SIXTEEN)


_AGENT_ATTEMPTS_TRIGGERS = (
    "agent_attempts_state_transition",
    "agent_attempts_no_delete",
)
_AGENT_RECEIPTS_V2_TRIGGERS = (
    "agent_receipts_v2_no_update",
    "agent_receipts_v2_no_delete",
)
_NODE_EXECUTION_REQUESTS_TRIGGERS = (
    "node_execution_requests_v3_no_update",
    "node_execution_requests_v3_no_delete",
)
_PREDECESSOR_AGENT_ATTEMPTS = "agent_attempts_before_the_refusal_code"
_FOUR_FAILURE_CODES = (
    "('PROCESS_EXITED_UNSUCCESSFULLY', 'OUTPUT_SCHEMA_REFUSED',\n"
    "                'AGENT_REFUSED', 'PROJECT_VERIFICATION_FAILED')"
)
_THREE_FAILURE_CODES = (
    "('PROCESS_EXITED_UNSUCCESSFULLY', 'OUTPUT_SCHEMA_REFUSED',\n"
    "                'AGENT_REFUSED')"
)
_TWO_FAILURE_CODES = "('PROCESS_EXITED_UNSUCCESSFULLY', 'OUTPUT_SCHEMA_REFUSED')"
_V24_AGENT_ATTEMPT_STATE_TRANSITION = """
        CREATE TRIGGER agent_attempts_state_transition
        BEFORE UPDATE ON agent_attempts
        WHEN NOT (
          OLD.attempt_id = NEW.attempt_id
          AND OLD.node_execution_id = NEW.node_execution_id
          AND OLD.request_hash = NEW.request_hash
          AND OLD.executor_operational_identity = NEW.executor_operational_identity
          AND OLD.run_id = NEW.run_id
          AND OLD.workflow_revision_hash = NEW.workflow_revision_hash
          AND OLD.node_id = NEW.node_id
          AND OLD.attempt_ordinal = NEW.attempt_ordinal
          AND NEW.state_version > OLD.state_version
          AND (
            (OLD.state = 'PREPARED' AND OLD.state_version = 0
             AND OLD.failure_code IS NULL AND OLD.receipt_hash IS NULL
             AND NEW.state = 'PREPARED' AND NEW.state_version = 1
             AND NEW.process_phase = 'WATCHDOG_READY'
             AND NEW.failure_code IS NULL AND NEW.receipt_hash IS NULL
             AND NEW.cancellation_command_id IS NULL)
            OR
            (OLD.state = 'PREPARED'
             AND NEW.state = 'LAUNCH_ARMED'
             AND NEW.process_phase IN ('NONE', 'LAUNCH_AUTHORIZED')
             AND NEW.failure_code IS NULL AND NEW.receipt_hash IS NULL
             AND NEW.cancellation_command_id IS NULL)
            OR
            (OLD.state = 'LAUNCH_ARMED'
             AND OLD.process_phase = 'LAUNCH_AUTHORIZED'
             AND NEW.state = 'LAUNCH_ARMED'
             AND NEW.process_phase = 'PROCESS_OBSERVED'
             AND NEW.failure_code IS NULL AND NEW.receipt_hash IS NULL
             AND NEW.cancellation_command_id IS NULL)
            OR
            (OLD.state = 'LAUNCH_ARMED'
             AND OLD.failure_code IS NULL AND OLD.receipt_hash IS NULL
             AND NEW.state = 'SUCCEEDED'
             AND NEW.failure_code IS NULL AND NEW.receipt_hash IS NOT NULL
             AND NEW.cancellation_command_id IS NULL
             AND EXISTS (
               SELECT 1 FROM agent_receipts_v2 AS receipt
               WHERE receipt.receipt_hash = NEW.receipt_hash
                 AND receipt.request_hash = NEW.request_hash
                 AND receipt.executor_operational_identity = NEW.executor_operational_identity
                 AND receipt.node_execution_id = NEW.node_execution_id
                 AND receipt.run_id = NEW.run_id
                 AND receipt.workflow_revision_hash = NEW.workflow_revision_hash
                 AND receipt.node_id = NEW.node_id
             ))
            OR
            (OLD.state = 'LAUNCH_ARMED'
             AND OLD.failure_code IS NULL AND OLD.receipt_hash IS NULL
             AND NEW.state = 'FAILED'
             AND NEW.failure_code IN
               ('PROCESS_EXITED_UNSUCCESSFULLY', 'OUTPUT_SCHEMA_REFUSED',
                'AGENT_REFUSED', 'PROJECT_VERIFICATION_FAILED')
             AND NEW.receipt_hash IS NULL
             AND NEW.cancellation_command_id IS NULL)
            OR
            (OLD.state IN ('PREPARED', 'LAUNCH_ARMED')
             AND OLD.cancellation_command_id IS NULL
             AND NEW.state = 'CANCEL_REQUESTED'
             AND NEW.cancellation_command_id IS NOT NULL
             AND NEW.cancellation_expected_state_version = OLD.state_version
             AND NEW.failure_code IS NULL AND NEW.receipt_hash IS NULL)
            OR
            (OLD.state = 'CANCEL_REQUESTED'
             AND NEW.state = 'CANCEL_REQUESTED'
             AND OLD.cancellation_command_id = NEW.cancellation_command_id
             AND NEW.redrive_state = 'OWNER_NOT_LOCAL'
             AND NEW.failure_code IS NULL AND NEW.receipt_hash IS NULL)
            OR
            (OLD.state = 'CANCEL_REQUESTED'
             AND NEW.state IN ('CANCELLED', 'INTERRUPTED')
             AND OLD.cancellation_command_id = NEW.cancellation_command_id
             AND NEW.process_phase = 'CLEANUP_ATTESTED'
             AND NEW.redrive_state = 'CLEANUP_ATTESTED'
             AND NEW.cancellation_disposition IS NOT NULL
             AND NEW.failure_code IS NULL AND NEW.receipt_hash IS NULL)
          )
        ) BEGIN
          SELECT RAISE(ABORT, 'invalid agent attempt transition');
        END
    """
_V17_AGENT_ATTEMPT_TRIGGERS = {
    "agent_attempts_state_transition": _V24_AGENT_ATTEMPT_STATE_TRANSITION.replace(
        _FOUR_FAILURE_CODES, _TWO_FAILURE_CODES
    ),
    "agent_attempts_no_delete": _PRODUCT_TRIGGERS["agent_attempts_no_delete"],
}
_V23_AGENT_ATTEMPT_TRIGGERS = {
    "agent_attempts_state_transition": _V24_AGENT_ATTEMPT_STATE_TRANSITION.replace(
        _FOUR_FAILURE_CODES, _THREE_FAILURE_CODES
    ),
    "agent_attempts_no_delete": _PRODUCT_TRIGGERS["agent_attempts_no_delete"],
}
_V24_AGENT_ATTEMPT_TRIGGERS = {
    "agent_attempts_state_transition": _V24_AGENT_ATTEMPT_STATE_TRANSITION,
    "agent_attempts_no_delete": _PRODUCT_TRIGGERS["agent_attempts_no_delete"],
}


_V27_AGENT_ATTEMPT_STATE_TRANSITION = """
        CREATE TRIGGER agent_attempts_state_transition
        BEFORE UPDATE ON agent_attempts
        WHEN NOT (
          OLD.attempt_id = NEW.attempt_id
          AND OLD.node_execution_id = NEW.node_execution_id
          AND OLD.request_hash = NEW.request_hash
          AND OLD.executor_operational_identity = NEW.executor_operational_identity
          AND OLD.run_id = NEW.run_id
          AND OLD.workflow_revision_hash = NEW.workflow_revision_hash
          AND OLD.node_id = NEW.node_id
          AND OLD.attempt_ordinal = NEW.attempt_ordinal
          AND NEW.state_version > OLD.state_version
          AND (
            (OLD.state = 'PREPARED' AND OLD.state_version = 0
             AND OLD.runner_manifest_id IS NULL
             AND OLD.failure_code IS NULL AND OLD.receipt_hash IS NULL
             AND NEW.state = 'PREPARED' AND NEW.state_version = 1
             AND NEW.process_phase = 'WATCHDOG_READY'
             AND NEW.runner_manifest_id IS NULL
             AND NEW.failure_code IS NULL AND NEW.receipt_hash IS NULL
             AND NEW.cancellation_command_id IS NULL)
            OR
            (OLD.state = 'PREPARED'
             AND OLD.runner_manifest_id IS NULL
             AND NEW.state = 'LAUNCH_ARMED'
             AND NEW.process_phase IN ('NONE', 'LAUNCH_AUTHORIZED')
             AND NEW.runner_manifest_id IS NULL
             AND NEW.failure_code IS NULL AND NEW.receipt_hash IS NULL
             AND NEW.cancellation_command_id IS NULL)
            OR
            (OLD.state = 'LAUNCH_ARMED'
             AND OLD.runner_manifest_id IS NULL
             AND OLD.process_phase = 'LAUNCH_AUTHORIZED'
             AND NEW.state = 'LAUNCH_ARMED'
             AND NEW.process_phase = 'PROCESS_OBSERVED'
             AND NEW.runner_manifest_id IS NULL
             AND NEW.failure_code IS NULL AND NEW.receipt_hash IS NULL
             AND NEW.cancellation_command_id IS NULL)
            OR
            (OLD.state = 'LAUNCH_ARMED'
             AND OLD.runner_manifest_id IS NULL
             AND OLD.failure_code IS NULL AND OLD.receipt_hash IS NULL
             AND NEW.state = 'SUCCEEDED'
             AND NEW.runner_manifest_id IS NULL
             AND NEW.failure_code IS NULL AND NEW.receipt_hash IS NOT NULL
             AND NEW.cancellation_command_id IS NULL
             AND EXISTS (
               SELECT 1 FROM agent_receipts_v2 AS receipt
               WHERE receipt.receipt_hash = NEW.receipt_hash
                 AND receipt.request_hash = NEW.request_hash
                 AND receipt.executor_operational_identity = NEW.executor_operational_identity
                 AND receipt.node_execution_id = NEW.node_execution_id
                 AND receipt.run_id = NEW.run_id
                 AND receipt.workflow_revision_hash = NEW.workflow_revision_hash
                 AND receipt.node_id = NEW.node_id
             ))
            OR
            (OLD.state = 'LAUNCH_ARMED'
             AND OLD.runner_manifest_id IS NULL
             AND OLD.failure_code IS NULL AND OLD.receipt_hash IS NULL
             AND NEW.state = 'FAILED'
             AND NEW.failure_code IN
               ('PROCESS_EXITED_UNSUCCESSFULLY', 'PROCESS_OUTPUT_LIMIT_EXCEEDED',
                'PROCESS_SUPERVISION_FAILED', 'OUTPUT_SCHEMA_REFUSED',
                'AGENT_REFUSED', 'PROJECT_VERIFICATION_FAILED')
             AND NEW.runner_manifest_id IS NULL
             AND NEW.receipt_hash IS NULL
             AND NEW.cancellation_command_id IS NULL)
            OR
            (OLD.state IN ('PREPARED', 'LAUNCH_ARMED')
             AND OLD.cancellation_command_id IS NULL
             AND OLD.runner_evidence_acceptance_phase = 'NONE'
             AND NEW.state = 'CANCEL_REQUESTED'
             AND NEW.cancellation_command_id IS NOT NULL
             AND NEW.cancellation_expected_state_version = OLD.state_version
             AND (OLD.runner_manifest_id IS NULL OR NEW.replacement = 'NONE')
             AND OLD.runner_manifest_id IS NEW.runner_manifest_id
             AND OLD.runner_generation_id IS NEW.runner_generation_id
             AND OLD.runner_invocation_id IS NEW.runner_invocation_id
             AND OLD.runner_terminal_evidence_hash IS NEW.runner_terminal_evidence_hash
             AND OLD.runner_evidence_acceptance_phase = NEW.runner_evidence_acceptance_phase
             AND NEW.failure_code IS NULL AND NEW.receipt_hash IS NULL)
            OR
            (OLD.state = 'CANCEL_REQUESTED'
             AND OLD.runner_manifest_id IS NULL
             AND NEW.state = 'CANCEL_REQUESTED'
             AND OLD.cancellation_command_id = NEW.cancellation_command_id
             AND NEW.redrive_state = 'OWNER_NOT_LOCAL'
             AND NEW.runner_manifest_id IS NULL
             AND NEW.failure_code IS NULL AND NEW.receipt_hash IS NULL)
            OR
            (OLD.state = 'CANCEL_REQUESTED'
             AND OLD.runner_manifest_id IS NULL
             AND NEW.state IN ('CANCELLED', 'INTERRUPTED')
             AND OLD.cancellation_command_id = NEW.cancellation_command_id
             AND NEW.process_phase = 'CLEANUP_ATTESTED'
             AND NEW.redrive_state = 'CLEANUP_ATTESTED'
             AND NEW.cancellation_disposition IS NOT NULL
             AND NEW.runner_manifest_id IS NULL
             AND NEW.failure_code IS NULL AND NEW.receipt_hash IS NULL)
            OR
            (OLD.state = 'PREPARED' AND OLD.process_phase = 'NONE'
             AND OLD.runner_manifest_id IS NULL
             AND OLD.runner_generation_id IS NULL
             AND OLD.runner_invocation_id IS NULL
             AND OLD.runner_evidence_acceptance_phase = 'NONE'
             AND NEW.state = 'PREPARED' AND NEW.process_phase = 'NONE'
             AND NEW.runner_manifest_id IS NOT NULL
             AND NEW.runner_generation_id IS NOT NULL
             AND NEW.runner_invocation_id IS NULL
             AND NEW.runner_evidence_acceptance_phase = 'NONE'
             AND NEW.cancellation_command_id IS NULL)
            OR
            (OLD.state = 'PREPARED' AND OLD.process_phase = 'NONE'
             AND OLD.runner_manifest_id = NEW.runner_manifest_id
             AND OLD.runner_generation_id = NEW.runner_generation_id
             AND OLD.runner_invocation_id IS NULL
             AND OLD.runner_evidence_acceptance_phase = 'NONE'
             AND NEW.state = 'LAUNCH_ARMED' AND NEW.process_phase = 'NONE'
             AND NEW.runner_invocation_id IS NOT NULL
             AND NEW.runner_evidence_acceptance_phase = 'NONE'
             AND NEW.cancellation_command_id IS NULL)
            OR
            (OLD.state = 'PREPARED' AND OLD.process_phase = 'NONE'
             AND OLD.runner_manifest_id = NEW.runner_manifest_id
             AND OLD.runner_generation_id = NEW.runner_generation_id
             AND OLD.runner_invocation_id IS NULL
             AND OLD.runner_evidence_acceptance_phase = 'NONE'
             AND NEW.state = 'PREPARED' AND NEW.process_phase = 'NONE'
             AND NEW.runner_terminal_evidence_hash IS NOT NULL
             AND NEW.runner_evidence_acceptance_phase = 'CORE_COMMITTED'
             AND NEW.cancellation_command_id IS NULL)
            OR
            (OLD.state = 'LAUNCH_ARMED'
             AND OLD.runner_manifest_id = NEW.runner_manifest_id
             AND OLD.runner_generation_id = NEW.runner_generation_id
             AND OLD.runner_invocation_id = NEW.runner_invocation_id
             AND OLD.runner_evidence_acceptance_phase = 'NONE'
             AND NEW.state = 'LAUNCH_ARMED'
             AND NEW.runner_terminal_evidence_hash IS NOT NULL
             AND NEW.runner_evidence_acceptance_phase = 'CORE_COMMITTED'
             AND NEW.cancellation_command_id IS NULL)
            OR
            (OLD.state IN ('LAUNCH_ARMED', 'CANCEL_REQUESTED')
             AND OLD.runner_manifest_id = NEW.runner_manifest_id
             AND OLD.runner_generation_id = NEW.runner_generation_id
             AND OLD.runner_invocation_id = NEW.runner_invocation_id
             AND OLD.runner_evidence_acceptance_phase = 'NONE'
             AND (OLD.state <> 'CANCEL_REQUESTED' OR OLD.replacement = 'NONE')
             AND NEW.state = 'SUCCEEDED'
             AND NEW.runner_terminal_evidence_hash IS NOT NULL
             AND NEW.runner_evidence_acceptance_phase = 'CORE_COMMITTED'
             AND NEW.cancellation_command_id IS NULL
             AND NEW.failure_code IS NULL AND NEW.receipt_hash IS NOT NULL
             AND EXISTS (
               SELECT 1 FROM agent_receipts_v2 AS receipt
               WHERE receipt.receipt_hash = NEW.receipt_hash
                 AND receipt.request_hash = NEW.request_hash
                 AND receipt.executor_operational_identity = NEW.executor_operational_identity
                 AND receipt.node_execution_id = NEW.node_execution_id
                 AND receipt.run_id = NEW.run_id
                 AND receipt.workflow_revision_hash = NEW.workflow_revision_hash
                 AND receipt.node_id = NEW.node_id
             ))
            OR
            (OLD.state IN ('LAUNCH_ARMED', 'CANCEL_REQUESTED')
             AND OLD.runner_manifest_id = NEW.runner_manifest_id
             AND OLD.runner_generation_id = NEW.runner_generation_id
             AND OLD.runner_invocation_id = NEW.runner_invocation_id
             AND OLD.runner_evidence_acceptance_phase = 'NONE'
             AND (OLD.state <> 'CANCEL_REQUESTED' OR OLD.replacement = 'NONE')
             AND NEW.state = 'FAILED'
             AND NEW.runner_terminal_evidence_hash IS NOT NULL
             AND NEW.runner_evidence_acceptance_phase = 'CORE_COMMITTED'
             AND NEW.cancellation_command_id IS NULL
             AND NEW.failure_code IN
               ('PROCESS_EXITED_UNSUCCESSFULLY', 'PROCESS_OUTPUT_LIMIT_EXCEEDED',
                'PROCESS_SUPERVISION_FAILED', 'OUTPUT_SCHEMA_REFUSED',
                'AGENT_REFUSED', 'PROJECT_VERIFICATION_FAILED')
             AND NEW.receipt_hash IS NULL)
            OR
            (OLD.state = 'CANCEL_REQUESTED'
             AND OLD.runner_manifest_id = NEW.runner_manifest_id
             AND OLD.runner_generation_id = NEW.runner_generation_id
             AND OLD.runner_invocation_id = NEW.runner_invocation_id
             AND OLD.runner_evidence_acceptance_phase = 'NONE'
             AND OLD.replacement = 'NONE'
             AND NEW.state = 'CANCELLED' AND NEW.process_phase = 'NONE'
             AND OLD.cancellation_command_id = NEW.cancellation_command_id
             AND NEW.redrive_state = 'CLEANUP_ATTESTED'
             AND NEW.cancellation_disposition IN
               ('EXITED_BEFORE_SIGNAL', 'REAPED_AFTER_TERM', 'REAPED_AFTER_KILL')
             AND NEW.runner_terminal_evidence_hash IS NOT NULL
             AND NEW.runner_evidence_acceptance_phase = 'CORE_COMMITTED'
             AND NEW.failure_code IS NULL AND NEW.receipt_hash IS NULL)
            OR
            (OLD.state = NEW.state
             AND OLD.process_phase = NEW.process_phase
             AND OLD.process_owner_id IS NEW.process_owner_id
             AND OLD.watchdog_generation_id IS NEW.watchdog_generation_id
             AND OLD.cancellation_command_id IS NEW.cancellation_command_id
             AND OLD.cancellation_expected_state_version IS NEW.cancellation_expected_state_version
             AND OLD.replacement IS NEW.replacement
             AND OLD.redrive_state IS NEW.redrive_state
             AND OLD.cancellation_disposition IS NEW.cancellation_disposition
             AND OLD.cancellation_workflow_id IS NEW.cancellation_workflow_id
             AND OLD.failure_code IS NEW.failure_code
             AND OLD.receipt_hash IS NEW.receipt_hash
             AND OLD.runner_manifest_id = NEW.runner_manifest_id
             AND OLD.runner_generation_id = NEW.runner_generation_id
             AND OLD.runner_invocation_id IS NEW.runner_invocation_id
             AND OLD.runner_terminal_evidence_hash = NEW.runner_terminal_evidence_hash
             AND OLD.runner_evidence_acceptance_phase = 'CORE_COMMITTED'
             AND NEW.runner_evidence_acceptance_phase = 'ACKNOWLEDGED')
            OR
            (OLD.state = 'PREPARED' AND NEW.state = 'PREPARED'
             AND OLD.process_phase = 'NONE' AND NEW.process_phase = 'NONE'
             AND OLD.runner_manifest_id IS NOT NULL
             AND OLD.runner_generation_id IS NOT NULL
             AND OLD.runner_evidence_acceptance_phase = 'ACKNOWLEDGED'
             AND NEW.runner_manifest_id IS NOT NULL
             AND NEW.runner_generation_id IS NOT NULL
             AND NEW.runner_generation_id <> OLD.runner_generation_id
             AND NEW.runner_invocation_id IS NULL
             AND NEW.runner_terminal_evidence_hash IS NULL
             AND NEW.runner_evidence_acceptance_phase = 'NONE'
             AND NEW.cancellation_command_id IS NULL
             AND NEW.failure_code IS NULL AND NEW.receipt_hash IS NULL)
          )
        ) BEGIN
          SELECT RAISE(ABORT, 'invalid agent attempt transition');
        END
    """
_V27_AGENT_ATTEMPT_TRIGGERS = {
    "agent_attempts_state_transition": _V27_AGENT_ATTEMPT_STATE_TRANSITION,
    "agent_attempts_no_delete": _PRODUCT_TRIGGERS["agent_attempts_no_delete"],
}


# The attempt transition trigger every schema from V32 to V36 published,
# recorded rather than derived for the reason `published_schema_shapes` gives
# about table text: a hop must install the trigger of its OWN target, and a
# record that followed the declaration would go on installing whatever the
# newest hop last changed. The V37 hop added the transcript-pointer clause to
# the live one, which names a column no V32 store has, so a V31 -> V32 step
# reinstalling today's text could not even create the trigger.
_V32_AGENT_ATTEMPT_STATE_TRANSITION = """
        CREATE TRIGGER agent_attempts_state_transition
        BEFORE UPDATE ON agent_attempts
        WHEN NOT (
          OLD.attempt_id = NEW.attempt_id
          AND OLD.node_execution_id = NEW.node_execution_id
          AND OLD.request_hash = NEW.request_hash
          AND OLD.executor_operational_identity = NEW.executor_operational_identity
          AND OLD.run_id = NEW.run_id
          AND OLD.workflow_revision_hash = NEW.workflow_revision_hash
          AND OLD.node_id = NEW.node_id
          AND OLD.attempt_ordinal = NEW.attempt_ordinal
          AND NEW.state_version > OLD.state_version
          AND (
            (OLD.state = 'PREPARED' AND OLD.state_version = 0
             AND OLD.runner_manifest_id IS NULL
             AND OLD.failure_code IS NULL AND OLD.receipt_hash IS NULL
             AND NEW.state = 'PREPARED' AND NEW.state_version = 1
             AND NEW.process_phase = 'WATCHDOG_READY'
             AND NEW.runner_manifest_id IS NULL
             AND NEW.failure_code IS NULL AND NEW.receipt_hash IS NULL
             AND NEW.cancellation_command_id IS NULL)
            OR
            (OLD.state = 'PREPARED'
             AND OLD.runner_manifest_id IS NULL
             AND NEW.state = 'LAUNCH_ARMED'
             AND NEW.process_phase IN ('NONE', 'LAUNCH_AUTHORIZED')
             AND NEW.runner_manifest_id IS NULL
             AND NEW.failure_code IS NULL AND NEW.receipt_hash IS NULL
             AND NEW.cancellation_command_id IS NULL)
            OR
            (OLD.state = 'LAUNCH_ARMED'
             AND OLD.runner_manifest_id IS NULL
             AND OLD.process_phase = 'LAUNCH_AUTHORIZED'
             AND NEW.state = 'LAUNCH_ARMED'
             AND NEW.process_phase = 'PROCESS_OBSERVED'
             AND NEW.runner_manifest_id IS NULL
             AND NEW.failure_code IS NULL AND NEW.receipt_hash IS NULL
             AND NEW.cancellation_command_id IS NULL)
            OR
            (OLD.state = 'LAUNCH_ARMED'
             AND OLD.runner_manifest_id IS NULL
             AND OLD.failure_code IS NULL AND OLD.receipt_hash IS NULL
             AND NEW.state = 'SUCCEEDED'
             AND NEW.runner_manifest_id IS NULL
             AND NEW.failure_code IS NULL AND NEW.receipt_hash IS NOT NULL
             AND NEW.cancellation_command_id IS NULL
             AND EXISTS (
               SELECT 1 FROM agent_receipts_v2 AS receipt
               WHERE receipt.receipt_hash = NEW.receipt_hash
                 AND receipt.request_hash = NEW.request_hash
                 AND receipt.executor_operational_identity = NEW.executor_operational_identity
                 AND receipt.node_execution_id = NEW.node_execution_id
                 AND receipt.run_id = NEW.run_id
                 AND receipt.workflow_revision_hash = NEW.workflow_revision_hash
                 AND receipt.node_id = NEW.node_id
             ))
            OR
            (OLD.state = 'LAUNCH_ARMED'
             AND OLD.runner_manifest_id IS NULL
             AND OLD.failure_code IS NULL AND OLD.receipt_hash IS NULL
             AND NEW.state = 'FAILED'
             AND NEW.failure_code IN
               ('PROCESS_EXITED_UNSUCCESSFULLY', 'PROCESS_OUTPUT_LIMIT_EXCEEDED',
                'PROCESS_SUPERVISION_FAILED', 'OUTPUT_SCHEMA_REFUSED',
                'AGENT_REFUSED', 'PROJECT_VERIFICATION_FAILED')
             AND NEW.runner_manifest_id IS NULL
             AND NEW.receipt_hash IS NULL
             AND NEW.cancellation_command_id IS NULL)
            OR
            (OLD.state IN ('PREPARED', 'LAUNCH_ARMED')
             AND OLD.cancellation_command_id IS NULL
             AND OLD.runner_evidence_acceptance_phase = 'NONE'
             AND NEW.state = 'CANCEL_REQUESTED'
             AND NEW.cancellation_command_id IS NOT NULL
             AND NEW.cancellation_expected_state_version = OLD.state_version
             AND (OLD.runner_manifest_id IS NULL OR NEW.replacement = 'NONE')
             AND OLD.runner_manifest_id IS NEW.runner_manifest_id
             AND OLD.runner_generation_id IS NEW.runner_generation_id
             AND OLD.runner_invocation_id IS NEW.runner_invocation_id
             AND OLD.runner_terminal_evidence_hash IS NEW.runner_terminal_evidence_hash
             AND OLD.runner_evidence_acceptance_phase = NEW.runner_evidence_acceptance_phase
             AND NEW.failure_code IS NULL AND NEW.receipt_hash IS NULL)
            OR
            (OLD.state = 'CANCEL_REQUESTED'
             AND OLD.runner_manifest_id IS NULL
             AND NEW.state = 'CANCEL_REQUESTED'
             AND OLD.cancellation_command_id = NEW.cancellation_command_id
             AND NEW.redrive_state = 'OWNER_NOT_LOCAL'
             AND NEW.runner_manifest_id IS NULL
             AND NEW.failure_code IS NULL AND NEW.receipt_hash IS NULL)
            OR
            (OLD.state = 'CANCEL_REQUESTED'
             AND OLD.runner_manifest_id IS NULL
             AND NEW.state IN ('CANCELLED', 'INTERRUPTED')
             AND OLD.cancellation_command_id = NEW.cancellation_command_id
             AND NEW.process_phase = 'CLEANUP_ATTESTED'
             AND NEW.redrive_state = 'CLEANUP_ATTESTED'
             AND NEW.cancellation_disposition IS NOT NULL
             AND NEW.runner_manifest_id IS NULL
             AND NEW.failure_code IS NULL AND NEW.receipt_hash IS NULL)
            OR
            (OLD.state = 'PREPARED' AND OLD.process_phase = 'NONE'
             AND OLD.runner_manifest_id IS NULL
             AND OLD.runner_generation_id IS NULL
             AND OLD.runner_invocation_id IS NULL
             AND OLD.runner_evidence_acceptance_phase = 'NONE'
             AND NEW.state = 'PREPARED' AND NEW.process_phase = 'NONE'
             AND NEW.runner_manifest_id IS NOT NULL
             AND NEW.runner_generation_id IS NOT NULL
             AND NEW.runner_invocation_id IS NULL
             AND NEW.runner_evidence_acceptance_phase = 'NONE'
             AND NEW.cancellation_command_id IS NULL)
            OR
            (OLD.state = 'PREPARED' AND OLD.process_phase = 'NONE'
             AND OLD.runner_manifest_id = NEW.runner_manifest_id
             AND OLD.runner_generation_id = NEW.runner_generation_id
             AND OLD.runner_invocation_id IS NULL
             AND OLD.runner_evidence_acceptance_phase = 'NONE'
             AND NEW.state = 'LAUNCH_ARMED' AND NEW.process_phase = 'NONE'
             AND NEW.runner_invocation_id IS NOT NULL
             AND NEW.runner_evidence_acceptance_phase = 'NONE'
             AND NEW.cancellation_command_id IS NULL)
            OR
            (OLD.state = 'PREPARED' AND OLD.process_phase = 'NONE'
             AND OLD.runner_manifest_id = NEW.runner_manifest_id
             AND OLD.runner_generation_id = NEW.runner_generation_id
             AND OLD.runner_invocation_id IS NULL
             AND OLD.runner_evidence_acceptance_phase = 'NONE'
             AND NEW.state = 'PREPARED' AND NEW.process_phase = 'NONE'
             AND NEW.runner_terminal_evidence_hash IS NOT NULL
             AND NEW.runner_evidence_acceptance_phase = 'CORE_COMMITTED'
             AND NEW.cancellation_command_id IS NULL)
            OR
            (OLD.state = 'LAUNCH_ARMED'
             AND OLD.runner_manifest_id = NEW.runner_manifest_id
             AND OLD.runner_generation_id = NEW.runner_generation_id
             AND OLD.runner_invocation_id = NEW.runner_invocation_id
             AND OLD.runner_evidence_acceptance_phase = 'NONE'
             AND NEW.state = 'LAUNCH_ARMED'
             AND NEW.runner_terminal_evidence_hash IS NOT NULL
             AND NEW.runner_evidence_acceptance_phase = 'CORE_COMMITTED'
             AND NEW.cancellation_command_id IS NULL)
            OR
            (OLD.state IN ('LAUNCH_ARMED', 'CANCEL_REQUESTED')
             AND OLD.runner_manifest_id = NEW.runner_manifest_id
             AND OLD.runner_generation_id = NEW.runner_generation_id
             AND OLD.runner_invocation_id = NEW.runner_invocation_id
             AND OLD.runner_evidence_acceptance_phase = 'NONE'
             AND (OLD.state <> 'CANCEL_REQUESTED' OR OLD.replacement = 'NONE')
             AND NEW.state = 'SUCCEEDED'
             AND NEW.runner_terminal_evidence_hash IS NOT NULL
             AND NEW.runner_evidence_acceptance_phase = 'CORE_COMMITTED'
             AND NEW.cancellation_command_id IS NULL
             AND NEW.failure_code IS NULL AND NEW.receipt_hash IS NOT NULL
             AND EXISTS (
               SELECT 1 FROM agent_receipts_v2 AS receipt
               WHERE receipt.receipt_hash = NEW.receipt_hash
                 AND receipt.request_hash = NEW.request_hash
                 AND receipt.executor_operational_identity = NEW.executor_operational_identity
                 AND receipt.node_execution_id = NEW.node_execution_id
                 AND receipt.run_id = NEW.run_id
                 AND receipt.workflow_revision_hash = NEW.workflow_revision_hash
                 AND receipt.node_id = NEW.node_id
             ))
            OR
            (OLD.state IN ('LAUNCH_ARMED', 'CANCEL_REQUESTED')
             AND OLD.runner_manifest_id = NEW.runner_manifest_id
             AND OLD.runner_generation_id = NEW.runner_generation_id
             AND OLD.runner_invocation_id = NEW.runner_invocation_id
             AND OLD.runner_evidence_acceptance_phase = 'NONE'
             AND (OLD.state <> 'CANCEL_REQUESTED' OR OLD.replacement = 'NONE')
             AND NEW.state = 'FAILED'
             AND NEW.runner_terminal_evidence_hash IS NOT NULL
             AND NEW.runner_evidence_acceptance_phase = 'CORE_COMMITTED'
             AND NEW.cancellation_command_id IS NULL
             AND NEW.failure_code IN
               ('PROCESS_EXITED_UNSUCCESSFULLY', 'PROCESS_OUTPUT_LIMIT_EXCEEDED',
                'PROCESS_SUPERVISION_FAILED', 'OUTPUT_SCHEMA_REFUSED',
                'AGENT_REFUSED', 'PROJECT_VERIFICATION_FAILED')
             AND NEW.receipt_hash IS NULL)
            OR
            (OLD.state = 'CANCEL_REQUESTED'
             AND OLD.runner_manifest_id = NEW.runner_manifest_id
             AND OLD.runner_generation_id = NEW.runner_generation_id
             AND OLD.runner_invocation_id = NEW.runner_invocation_id
             AND OLD.runner_evidence_acceptance_phase = 'NONE'
             AND OLD.replacement = 'NONE'
             AND NEW.state = 'CANCELLED' AND NEW.process_phase = 'NONE'
             AND OLD.cancellation_command_id = NEW.cancellation_command_id
             AND NEW.redrive_state = 'CLEANUP_ATTESTED'
             AND NEW.cancellation_disposition IN
               ('EXITED_BEFORE_SIGNAL', 'REAPED_AFTER_TERM', 'REAPED_AFTER_KILL')
             AND NEW.runner_terminal_evidence_hash IS NOT NULL
             AND NEW.runner_evidence_acceptance_phase = 'CORE_COMMITTED'
             AND NEW.failure_code IS NULL AND NEW.receipt_hash IS NULL)
            OR
            (OLD.state = 'CANCEL_REQUESTED'
             AND OLD.runner_manifest_id IS NOT NULL
             AND OLD.runner_manifest_id = NEW.runner_manifest_id
             AND OLD.runner_generation_id = NEW.runner_generation_id
             AND OLD.runner_invocation_id IS NULL
             AND NEW.runner_invocation_id IS NULL
             AND OLD.runner_evidence_acceptance_phase = 'NONE'
             AND NEW.runner_evidence_acceptance_phase = 'NONE'
             AND OLD.runner_terminal_evidence_hash IS NULL
             AND NEW.runner_terminal_evidence_hash IS NULL
             AND OLD.replacement = 'NONE'
             AND NEW.state = 'CANCELLED' AND NEW.process_phase = 'NONE'
             AND OLD.cancellation_command_id = NEW.cancellation_command_id
             AND NEW.redrive_state = 'CLEANUP_ATTESTED'
             AND NEW.cancellation_disposition = 'NEVER_LAUNCHED'
             AND NEW.failure_code IS NULL AND NEW.receipt_hash IS NULL)
            OR
            (OLD.state = NEW.state
             AND OLD.process_phase = NEW.process_phase
             AND OLD.process_owner_id IS NEW.process_owner_id
             AND OLD.watchdog_generation_id IS NEW.watchdog_generation_id
             AND OLD.cancellation_command_id IS NEW.cancellation_command_id
             AND OLD.cancellation_expected_state_version IS NEW.cancellation_expected_state_version
             AND OLD.replacement IS NEW.replacement
             AND OLD.redrive_state IS NEW.redrive_state
             AND OLD.cancellation_disposition IS NEW.cancellation_disposition
             AND OLD.cancellation_workflow_id IS NEW.cancellation_workflow_id
             AND OLD.failure_code IS NEW.failure_code
             AND OLD.receipt_hash IS NEW.receipt_hash
             AND OLD.runner_manifest_id = NEW.runner_manifest_id
             AND OLD.runner_generation_id = NEW.runner_generation_id
             AND OLD.runner_invocation_id IS NEW.runner_invocation_id
             AND OLD.runner_terminal_evidence_hash = NEW.runner_terminal_evidence_hash
             AND OLD.runner_evidence_acceptance_phase = 'CORE_COMMITTED'
             AND NEW.runner_evidence_acceptance_phase = 'ACKNOWLEDGED')
            OR
            (OLD.state = 'PREPARED' AND NEW.state = 'PREPARED'
             AND OLD.process_phase = 'NONE' AND NEW.process_phase = 'NONE'
             AND OLD.runner_manifest_id IS NOT NULL
             AND OLD.runner_generation_id IS NOT NULL
             AND OLD.runner_evidence_acceptance_phase = 'ACKNOWLEDGED'
             AND NEW.runner_manifest_id IS NOT NULL
             AND NEW.runner_generation_id IS NOT NULL
             AND NEW.runner_generation_id <> OLD.runner_generation_id
             AND NEW.runner_invocation_id IS NULL
             AND NEW.runner_terminal_evidence_hash IS NULL
             AND NEW.runner_evidence_acceptance_phase = 'NONE'
             AND NEW.cancellation_command_id IS NULL
             AND NEW.failure_code IS NULL AND NEW.receipt_hash IS NULL)
          )
        ) BEGIN
          SELECT RAISE(ABORT, 'invalid agent attempt transition');
        END
"""
_V32_AGENT_ATTEMPT_TRIGGERS = {
    "agent_attempts_state_transition": _V32_AGENT_ATTEMPT_STATE_TRANSITION,
    "agent_attempts_no_delete": _PRODUCT_TRIGGERS["agent_attempts_no_delete"],
}
_NINE_FAILURE_CODES = (
    "('PROCESS_EXITED_UNSUCCESSFULLY', 'PROCESS_OUTPUT_LIMIT_EXCEEDED',\n"
    "                'PROCESS_SUPERVISION_FAILED', 'OUTPUT_SCHEMA_REFUSED',\n"
    "                'AGENT_REFUSED', 'PROJECT_VERIFICATION_FAILED',\n"
    "                'CANDIDATE_CAPTURE_FAILED', 'CANDIDATE_UNCHANGED',\n"
    "                'PRODUCED_VALUE_REFUSED')"
)
_EIGHT_FAILURE_CODES = (
    "('PROCESS_EXITED_UNSUCCESSFULLY', 'PROCESS_OUTPUT_LIMIT_EXCEEDED',\n"
    "                'PROCESS_SUPERVISION_FAILED', 'OUTPUT_SCHEMA_REFUSED',\n"
    "                'AGENT_REFUSED', 'PROJECT_VERIFICATION_FAILED',\n"
    "                'CANDIDATE_CAPTURE_FAILED', 'CANDIDATE_UNCHANGED')"
)
_SEVEN_FAILURE_CODES = (
    "('PROCESS_EXITED_UNSUCCESSFULLY', 'PROCESS_OUTPUT_LIMIT_EXCEEDED',\n"
    "                'PROCESS_SUPERVISION_FAILED', 'OUTPUT_SCHEMA_REFUSED',\n"
    "                'AGENT_REFUSED', 'PROJECT_VERIFICATION_FAILED',\n"
    "                'CANDIDATE_CAPTURE_FAILED')"
)
_SIX_FAILURE_CODES = (
    "('PROCESS_EXITED_UNSUCCESSFULLY', 'PROCESS_OUTPUT_LIMIT_EXCEEDED',\n"
    "                'PROCESS_SUPERVISION_FAILED', 'OUTPUT_SCHEMA_REFUSED',\n"
    "                'AGENT_REFUSED', 'PROJECT_VERIFICATION_FAILED')"
)
_V38_AGENT_ATTEMPT_TRIGGERS = {
    "agent_attempts_state_transition": _PRODUCT_TRIGGERS[
        "agent_attempts_state_transition"
    ].replace(_NINE_FAILURE_CODES, _SIX_FAILURE_CODES),
    "agent_attempts_no_delete": _PRODUCT_TRIGGERS["agent_attempts_no_delete"],
}
"""What V33 to V38 published: today's trigger without three words.

Derived rather than copied out, the way every earlier vocabulary hop here is
derived: the whole difference *is* the failure-code list, so writing the other
250 lines again would be 250 more lines able to drift from the ones they have to
match. The replacement reaches **both** places the list appears, because the
vocabulary is one set: a code this schema admits at all is admitted wherever an
attempt may end FAILED, and a trigger naming a narrower subset would be a
second, quieter definition of what a failure code is."""

_V49_AGENT_ATTEMPT_TRIGGERS = {
    "agent_attempts_state_transition": _PRODUCT_TRIGGERS[
        "agent_attempts_state_transition"
    ].replace(_NINE_FAILURE_CODES, _SEVEN_FAILURE_CODES),
    "agent_attempts_no_delete": _PRODUCT_TRIGGERS["agent_attempts_no_delete"],
}
"""What V39 to V49 published, derived from today's trigger for the same reason."""

_V52_AGENT_ATTEMPT_TRIGGERS = {
    "agent_attempts_state_transition": _PRODUCT_TRIGGERS[
        "agent_attempts_state_transition"
    ].replace(_NINE_FAILURE_CODES, _EIGHT_FAILURE_CODES),
    "agent_attempts_no_delete": _PRODUCT_TRIGGERS["agent_attempts_no_delete"],
}
"""What V50 to V52 published, derived from today's trigger for the same reason."""


def _apply_v16_to_v17(connection: sqlite3.Connection) -> None:
    """Admit the refusal's own failure code, and keep every stored row.

    Every stored FAILED attempt already carries `PROCESS_EXITED_UNSUCCESSFULLY`,
    which the widened constraint still admits, so nothing is reinterpreted.
    """

    _rebuild_product_table(
        connection,
        agent_attempts,
        _PREDECESSOR_AGENT_ATTEMPTS,
        _AGENT_ATTEMPTS_TRIGGERS,
        _VERSION_SIXTEEN,
        _VERSION_SEVENTEEN,
        trigger_source=_V17_AGENT_ATTEMPT_TRIGGERS,
    )
    _raise_declared_version(connection, _VERSION_SIXTEEN, _VERSION_SEVENTEEN)


_PREDECESSOR_RUNS = "runs_before_failed_state"


def _apply_v17_to_v18(connection: sqlite3.Connection) -> None:
    """Admit FAILED as a run ending, and keep every stored row.

    Every stored run is still STARTED, waiting, or COMPLETED, which the widened
    constraint still admits, and nothing is reinterpreted. Inventory that should
    have ended is a serve-start convergence, not this hop's job.
    """

    _rebuild_product_table(
        connection,
        runs,
        _PREDECESSOR_RUNS,
        ("runs_binding_no_update",),
        _VERSION_SEVENTEEN,
        _VERSION_EIGHTEEN,
    )
    _raise_declared_version(connection, _VERSION_SEVENTEEN, _VERSION_EIGHTEEN)


_PREDECESSOR_ROUNDLESS_RUNS = "runs_before_the_round_column"
_PREDECESSOR_ROUNDLESS_RUN_EVENTS = "run_events_before_the_round_column"
_PREDECESSOR_REQUEST_KEYED_REQUESTS = (
    "node_execution_requests_v3_before_the_execution_key"
)
_PREDECESSOR_ONCE_PER_RUN_AGENT_RECEIPTS = "agent_receipts_v2_before_the_round"


def _apply_v19_to_v20(connection: sqlite3.Connection) -> None:
    """Give the round a durable home, and read every stored row as round one.

    Every run, event and agent receipt this store already holds was written
    before a document could declare a loop, so each of them stands in the first
    round -- that is a fact about them, not a default filled in to make a column
    fit.

    Two keys go with it, because both said "once per run" about something that
    is now once per round. A node execution request keyed by the request hash
    made the second round of a node vanish into the first, and an agent receipt
    keyed by (run, revision, node) refused the second round outright; each is
    replaced by the node execution key that says the same thing exactly.
    """

    _rebuild_product_table(
        connection,
        runs,
        _PREDECESSOR_ROUNDLESS_RUNS,
        ("runs_binding_no_update",),
        _VERSION_NINETEEN,
        _VERSION_TWENTY,
        {runs.c.current_round_ordinal.name: str(FIRST_ROUND_ORDINAL)},
    )
    _rebuild_product_table(
        connection,
        run_events,
        _PREDECESSOR_ROUNDLESS_RUN_EVENTS,
        _RUN_EVENTS_TRIGGERS,
        _VERSION_NINETEEN,
        _VERSION_TWENTY,
        {run_events.c.round_ordinal.name: str(FIRST_ROUND_ORDINAL)},
    )
    _rebuild_product_table(
        connection,
        node_execution_requests_v3,
        _PREDECESSOR_REQUEST_KEYED_REQUESTS,
        _NODE_EXECUTION_REQUESTS_TRIGGERS,
        _VERSION_NINETEEN,
        _VERSION_TWENTY,
    )
    _rebuild_product_table(
        connection,
        agent_receipts_v2,
        _PREDECESSOR_ONCE_PER_RUN_AGENT_RECEIPTS,
        _AGENT_RECEIPTS_V2_TRIGGERS,
        _VERSION_NINETEEN,
        _VERSION_TWENTY,
        {agent_receipts_v2.c.round_ordinal.name: str(FIRST_ROUND_ORDINAL)},
    )
    _raise_declared_version(connection, _VERSION_NINETEEN, _VERSION_TWENTY)


_AGENT_CONFIGURATION_REVISIONS_TRIGGERS = (
    "agent_configuration_revisions_no_update",
    "agent_configuration_revisions_no_delete",
)
_PREDECESSOR_TOOL_FREE_CONFIGURATIONS = (
    "agent_configuration_revisions_before_workspace_tools"
)


def _apply_v20_to_v21(connection: sqlite3.Connection) -> None:
    """Admit headless_with_tools as a requested capability, and keep every row.

    SQLite cannot widen a table CHECK in place, so this is the same rebuild every
    shape hop is. Every stored configuration requests `headless` or
    `interactive`, which the widened constraint still admits, and no stored one
    can name the tool executor either, because no predecessor store could publish
    it -- so no row is reinterpreted and none needs a value filled in.
    """

    _rebuild_product_table(
        connection,
        agent_configuration_revisions,
        _PREDECESSOR_TOOL_FREE_CONFIGURATIONS,
        _AGENT_CONFIGURATION_REVISIONS_TRIGGERS,
        _VERSION_TWENTY,
        _VERSION_TWENTY_ONE,
    )
    _raise_declared_version(connection, _VERSION_TWENTY, _VERSION_TWENTY_ONE)


_INSTANT_TABLES = (run_instants, attempt_instants, event_instants)
_INSTANT_TRIGGERS = (
    "run_instants_start_no_update",
    "run_instants_end_once",
    "run_instants_no_delete",
    "attempt_instants_start_no_update",
    "attempt_instants_end_once",
    "attempt_instants_no_delete",
    "event_instants_no_update",
    "event_instants_no_delete",
)


def _apply_v21_to_v22(connection: sqlite3.Connection) -> None:
    """Give runs, attempts, and events a home for the instant they were written.

    Three additive tables, no reinterpretation of a predecessor row: a run that
    already existed has no instant, which is what "this store never recorded
    when" means, and never an invented clock.
    """

    for table in _INSTANT_TABLES:
        existing = connection.execute(
            _SQLITE_MASTER_TABLE_EXISTS_QUERY,
            (table.name,),
        ).fetchone()
        if existing is not None:
            raise StoreMigrationRefused(
                f"schema version {_VERSION_TWENTY_ONE} already has {table.name}; "
                "this command will not alter it"
            )
        connection.execute(
            str(CreateTable(table).compile(dialect=sqlite_dialect.dialect()))
        )
    for trigger in _INSTANT_TRIGGERS:
        connection.execute(_PRODUCT_TRIGGERS[trigger])
    _raise_declared_version(connection, _VERSION_TWENTY_ONE, _VERSION_TWENTY_TWO)


_PREDECESSOR_ATTEMPTS_BEFORE_AGENT_REFUSED = "agent_attempts_before_agent_refused"


def _apply_v22_to_v23(connection: sqlite3.Connection) -> None:
    """Admit AGENT_REFUSED as a failure code, and keep every stored row.

    Every stored FAILED attempt already carries PROCESS_EXITED_UNSUCCESSFULLY
    or OUTPUT_SCHEMA_REFUSED, which the widened constraint still admits, so
    nothing is reinterpreted.
    """

    _rebuild_product_table(
        connection,
        agent_attempts,
        _PREDECESSOR_ATTEMPTS_BEFORE_AGENT_REFUSED,
        _AGENT_ATTEMPTS_TRIGGERS,
        _VERSION_TWENTY_TWO,
        _VERSION_TWENTY_THREE,
        trigger_source=_V23_AGENT_ATTEMPT_TRIGGERS,
    )
    _raise_declared_version(connection, _VERSION_TWENTY_TWO, _VERSION_TWENTY_THREE)


_PREDECESSOR_ATTEMPTS_BEFORE_PROJECT_VERIFICATION_FAILED = (
    "agent_attempts_before_project_verification_failed"
)


def _apply_v23_to_v24(connection: sqlite3.Connection) -> None:
    """Admit PROJECT_VERIFICATION_FAILED as a failure code, and keep every row.

    Stored FAILED rows already carry one of the three old codes, which the
    widened constraint still admits, so nothing is reinterpreted.
    """

    _rebuild_product_table(
        connection,
        agent_attempts,
        _PREDECESSOR_ATTEMPTS_BEFORE_PROJECT_VERIFICATION_FAILED,
        _AGENT_ATTEMPTS_TRIGGERS,
        _VERSION_TWENTY_THREE,
        _VERSION_TWENTY_FOUR,
        trigger_source=_V24_AGENT_ATTEMPT_TRIGGERS,
    )
    _raise_declared_version(connection, _VERSION_TWENTY_THREE, _VERSION_TWENTY_FOUR)


_OCCUPANCY_TABLE_NAMES = (
    "host_occupancy_revisions",
    "host_occupancy_bindings",
)
_OCCUPANCY_TRIGGER_STATEMENTS = {
    "host_occupancy_revisions_no_update": """
        CREATE TRIGGER host_occupancy_revisions_no_update
        BEFORE UPDATE ON host_occupancy_revisions BEGIN
          SELECT RAISE(ABORT, 'host occupancy revisions are immutable');
        END
    """,
    "host_occupancy_revisions_no_delete": """
        CREATE TRIGGER host_occupancy_revisions_no_delete
        BEFORE DELETE ON host_occupancy_revisions BEGIN
          SELECT RAISE(ABORT, 'host occupancy revisions are immutable');
        END
    """,
    "host_occupancy_bindings_no_update": """
        CREATE TRIGGER host_occupancy_bindings_no_update
        BEFORE UPDATE ON host_occupancy_bindings BEGIN
          SELECT RAISE(ABORT, 'host occupancy bindings are immutable');
        END
    """,
    "host_occupancy_bindings_no_delete": """
        CREATE TRIGGER host_occupancy_bindings_no_delete
        BEFORE DELETE ON host_occupancy_bindings BEGIN
          SELECT RAISE(ABORT, 'host occupancy bindings are immutable');
        END
    """,
}


def _apply_v25_to_v26(connection: sqlite3.Connection) -> None:
    """Give occupancy a durable home beside the project-root channel.

    Two additive tables, no reinterpretation of a predecessor row: a store that
    already existed has no occupancy, which is what "this project has not
    recommended a binding" means.
    """

    for table_name in _OCCUPANCY_TABLE_NAMES:
        existing = connection.execute(
            _SQLITE_MASTER_NAME_EXISTS_QUERY,
            (table_name,),
        ).fetchone()
        if existing is not None:
            raise StoreMigrationRefused(
                f"schema version {_VERSION_TWENTY_FIVE} already has {table_name}; "
                "this command will not alter it"
            )
        connection.execute(PUBLISHED_TABLE_SHAPES[(_VERSION_TWENTY_SIX, table_name)])
    for statement in _OCCUPANCY_TRIGGER_STATEMENTS.values():
        connection.execute(statement)
    _raise_declared_version(connection, _VERSION_TWENTY_FIVE, _VERSION_TWENTY_SIX)


_PREDECESSOR_ATTEMPTS_BEFORE_RUNNER_EVIDENCE = "agent_attempts_before_runner_evidence"


def _apply_v26_to_v27(connection: sqlite3.Connection) -> None:
    """Bind Runner evidence without reinterpreting one predecessor attempt.

    The four optional identities/evidence hashes stay absent on every V26 row;
    `NONE` is the exact statement that Core has accepted no Runner evidence.
    No other product table is rebuilt.
    """

    _rebuild_product_table(
        connection,
        agent_attempts,
        _PREDECESSOR_ATTEMPTS_BEFORE_RUNNER_EVIDENCE,
        _AGENT_ATTEMPTS_TRIGGERS,
        _VERSION_TWENTY_SIX,
        _VERSION_TWENTY_SEVEN,
        {agent_attempts.c.runner_evidence_acceptance_phase.name: "'NONE'"},
        trigger_source=_V27_AGENT_ATTEMPT_TRIGGERS,
    )
    _raise_declared_version(connection, _VERSION_TWENTY_SIX, _VERSION_TWENTY_SEVEN)


def _apply_v27_to_v28(connection: sqlite3.Connection) -> None:
    """Remove the unused Access store only when it holds no unowned truth."""

    access_row = connection.execute(
        f"SELECT 1 FROM {_V27_ACCESS_TABLE_NAME} LIMIT 1"
    ).fetchone()
    if access_row is not None:
        raise StoreMigrationRefused(
            f"schema version {_VERSION_TWENTY_SEVEN} has rows in "
            f"{_V27_ACCESS_TABLE_NAME}, but no production owner can translate "
            "them; this command will not alter it"
        )
    for trigger_name in _V27_ACCESS_TRIGGER_NAMES:
        connection.execute(f"DROP TRIGGER {trigger_name}")
    connection.execute(f"DROP TABLE {_V27_ACCESS_TABLE_NAME}")
    _raise_declared_version(connection, _VERSION_TWENTY_SEVEN, _VERSION_TWENTY_EIGHT)


_PREDECESSOR_RUNS_BEFORE_CANCELLED = "runs_before_cancelled_state"


def _apply_v29_to_v30(connection: sqlite3.Connection) -> None:
    """Admit CANCELLED as a run ending, and keep every stored row.

    Every stored run is still STARTED, waiting, COMPLETED, or FAILED, which
    the widened constraint still admits, and nothing is reinterpreted. #439
    P1 gives the word its durable home only; no writer constructs it and no
    serve-start inventory lifts a run onto it until #439 P3 gives it one.
    """

    _rebuild_product_table(
        connection,
        runs,
        _PREDECESSOR_RUNS_BEFORE_CANCELLED,
        ("runs_binding_no_update",),
        _VERSION_TWENTY_NINE,
        _VERSION_THIRTY,
    )
    _raise_declared_version(connection, _VERSION_TWENTY_NINE, _VERSION_THIRTY)


_AGENT_ATTEMPTS_STATE_TRANSITION_TRIGGER = "agent_attempts_state_transition"


def _apply_v31_to_v32(connection: sqlite3.Connection) -> None:
    """Admit the never-launched runner-lease cancel terminal transition (#584).

    No table shape moves and no row is rewritten: the only change is one added
    branch on the `agent_attempts_state_transition` trigger, so the hop drops
    that trigger and installs the text V32 itself published. That text is a
    record, not the current declaration: the V37 hop added a clause naming a
    column no V32 store has, so a step reinstalling today's trigger could not
    create it at all. Every stored attempt row is already legal under the
    unchanged CHECK constraints; the fingerprint the migration runner takes
    after this step is what proves the swapped trigger matches a freshly built
    v32 store, byte for byte.
    """

    connection.execute(f"DROP TRIGGER {_AGENT_ATTEMPTS_STATE_TRANSITION_TRIGGER}")
    connection.execute(
        _V32_AGENT_ATTEMPT_TRIGGERS[_AGENT_ATTEMPTS_STATE_TRANSITION_TRIGGER]
    )
    _raise_declared_version(connection, _VERSION_THIRTY_ONE, _VERSION_THIRTY_TWO)


_WAIT_ANSWERS_TRIGGERS = (
    "wait_answers_payload_no_update",
    "wait_answers_state_transition",
    "wait_answers_no_delete",
)
_PREDECESSOR_WAIT_ANSWERS = "wait_answers_before_the_execution_key"


def _apply_v33_to_v34(connection: sqlite3.Connection) -> None:
    """Key a wait answer by its execution and give it a round, keeping every row.

    Nothing already written is reinterpreted. A stored answer's own
    `node_execution_id` is already unique and is already the identity of the
    node's first round -- round one derives byte-identically to the roundless
    derivation -- so making it the key renames nothing and loses nothing. The
    round a carried row is filled with is that same first round, stated rather
    than inferred, because an execution hash cannot be read backwards to
    recover which round produced it.
    """

    _rebuild_product_table(
        connection,
        wait_answers,
        _PREDECESSOR_WAIT_ANSWERS,
        _WAIT_ANSWERS_TRIGGERS,
        _VERSION_THIRTY_THREE,
        _VERSION_THIRTY_FOUR,
        {wait_answers.c.round_ordinal.name: str(FIRST_ROUND_ORDINAL)},
        trigger_source=PUBLISHED_WAIT_ANSWER_TRIGGERS[_VERSION_THIRTY_FOUR],
    )
    _raise_declared_version(connection, _VERSION_THIRTY_THREE, _VERSION_THIRTY_FOUR)


_PREDECESSOR_WAIT_UNCANCELLABLE_RUN_EVENTS = "run_events_before_the_cancelled_wait"


def _apply_v34_to_v35(connection: sqlite3.Connection) -> None:
    """Admit WAIT_CANCELLED as an event kind, and keep every stored row.

    SQLite changes no CHECK in place, so widening the kind vocabulary is the
    same rebuild every shape hop is. Nothing already written is reinterpreted:
    no stored event carries the new kind, none of the carried columns move, and
    every row's own hash still frames the exact bytes it framed before. The two
    append-only triggers and the three partial unique indexes are reinstalled
    by the rebuild, because a run event log that could be updated or deleted
    for the length of one hop would be no evidence at all.
    """

    _rebuild_product_table(
        connection,
        run_events,
        _PREDECESSOR_WAIT_UNCANCELLABLE_RUN_EVENTS,
        _RUN_EVENTS_TRIGGERS,
        _VERSION_THIRTY_FOUR,
        _VERSION_THIRTY_FIVE,
    )
    _raise_declared_version(connection, _VERSION_THIRTY_FOUR, _VERSION_THIRTY_FIVE)


_ONCE_PER_NODE_EVENT_INDEX = "run_events_legacy_kind_unique"
_ROUND_SCOPED_EVENT_INDEX = "run_events_round_kind_unique"


def _apply_v35_to_v36(connection: sqlite3.Connection) -> None:
    """Re-scope the once-per-node event key to the round, touching no row.

    The predecessor's key spanned every round of a node at once, which a loop
    turning a Wait a second time breaks by writing a second `WAITING_INPUT`.
    Its successor says the same thing about one round, so every log the
    predecessor holds satisfies it: a store that admitted at most one such event
    per node admitted at most one per round of that node.

    Both statements are indexes, and SQLite adds and removes an index without
    reading or writing a row, so this hop is two DDL statements and the version
    CAS inside the migration's own transaction -- no table is parked, no row is
    copied, and the table text does not move. Should the second statement fail,
    the transaction takes the first one back with it.
    """

    connection.execute(f"DROP INDEX {_ONCE_PER_NODE_EVENT_INDEX}")
    connection.execute(_declared_indexes(run_events)[_ROUND_SCOPED_EVENT_INDEX])
    _raise_declared_version(connection, _VERSION_THIRTY_FIVE, _VERSION_THIRTY_SIX)


_PREDECESSOR_ATTEMPTS_BEFORE_THE_TRANSCRIPT = "agent_attempts_before_the_transcript"


def _apply_v36_to_v37(connection: sqlite3.Connection) -> None:
    """Give an attempt the address of its transcript, and keep every stored row.

    Nothing already written is reinterpreted: a predecessor attempt carries
    NULL, which is what "no transcript was decoded for this attempt" means, and
    never an invented address. The column is nullable, so no carried row needs a
    value declared for it.

    SQLite changes no CHECK in place and would append an added column after this
    table's own constraints, which is not where the declaration puts it, so this
    hop is the same rebuild every shape hop is rather than an `ALTER TABLE ADD
    COLUMN`. The attempt table's state-transition and no-delete triggers are
    reinstalled by the rebuild, because an attempt row that could take an
    unguarded transition for the length of one hop would be no ledger at all.
    The triggers it reinstalls are V37's own, not today's: a hop must leave the
    store standing at exactly the version it published, and the V39 word this
    version has no CHECK for would break its own fingerprint one step later.
    """

    _rebuild_product_table(
        connection,
        agent_attempts,
        _PREDECESSOR_ATTEMPTS_BEFORE_THE_TRANSCRIPT,
        _AGENT_ATTEMPTS_TRIGGERS,
        _VERSION_THIRTY_SIX,
        _VERSION_THIRTY_SEVEN,
        trigger_source=_V38_AGENT_ATTEMPT_TRIGGERS,
    )
    _raise_declared_version(connection, _VERSION_THIRTY_SIX, _VERSION_THIRTY_SEVEN)


_EFFECT_INTENTS_TRIGGERS = (
    "effect_intents_binding_no_update",
    "effect_intents_no_delete",
)
_V41_EFFECT_INTENT_TRIGGERS = {
    **_PRODUCT_TRIGGERS,
    "effect_intents_binding_no_update": _PRODUCT_TRIGGERS[
        "effect_intents_binding_no_update"
    ].replace(", operation_name", ""),
}
"""The intent triggers published before V42 made the operation immutable."""
_EFFECT_INTENTS_ABANDONMENT_TRIGGERS = (
    "effect_intents_abandonment",
    "effect_intents_no_abandoned_insert",
)
"""Both doors onto the word this hop admits: the transition that may reach it,
and the insert that may not. A CHECK admits a vocabulary; only these say which
writes are allowed to use it, and an ending that could be written straight into
a fresh row would be an abandonment no run ever ended."""
_PREDECESSOR_INTENTS_BEFORE_ABANDONMENT = "effect_intents_before_abandonment"


def _apply_v37_to_v38(connection: sqlite3.Connection) -> None:
    """Admit ABANDONED as an intent ending, and keep every stored row.

    Every stored intent is PREPARED, waiting, reconciling or CONFIRMED, which
    the widened CHECK still admits, so nothing is reinterpreted: a prepared
    intent this store already holds keeps standing prepared until the sweep that
    owns the word decides about it. The abandonment triggers are installed after
    the rebuild rather than carried through it, because they do not exist at the
    predecessor and the rebuild drops the triggers it is given before it parks
    the table.
    """

    _rebuild_product_table(
        connection,
        effect_intents,
        _PREDECESSOR_INTENTS_BEFORE_ABANDONMENT,
        _EFFECT_INTENTS_TRIGGERS,
        _VERSION_THIRTY_SEVEN,
        _VERSION_THIRTY_EIGHT,
        trigger_source=_V41_EFFECT_INTENT_TRIGGERS,
    )
    for trigger in _EFFECT_INTENTS_ABANDONMENT_TRIGGERS:
        connection.execute(_PRODUCT_TRIGGERS[trigger])
    _raise_declared_version(connection, _VERSION_THIRTY_SEVEN, _VERSION_THIRTY_EIGHT)


_PREDECESSOR_ATTEMPTS_BEFORE_CANDIDATE_CAPTURE_FAILED = (
    "agent_attempts_before_candidate_capture_failed"
)
_PREDECESSOR_REDEMPTIONS_BOUND_TO_THE_RECEIPT = (
    "tool_redemptions_bound_to_the_agent_receipt"
)
_TOOL_REDEMPTIONS_TRIGGERS = (
    "tool_redemptions_no_update",
    "tool_redemptions_no_delete",
)


def _refuse_redemptions_that_cannot_be_re_owned(
    connection: sqlite3.Connection,
) -> None:
    """Read every V38 redemption against the attempt it will be keyed by.

    A redemption, the attempt it names and the receipt it hangs from are three
    rows that must describe *one* execution. V38 never made them: its two
    foreign keys point at different tables and constrain each other not at all,
    so all three can name different work and still pass `foreign_key_check`.
    Carrying such a row would move the proof of a check onto an execution that
    never ran it -- quietly, and past every guard the store has.

    So the three are read against each other in full: the same run, the same
    workflow revision, the same node, the same node execution, the same request
    hash and the same executor identity, and the attempt's own receipt hash
    naming the very receipt found. Those last three are not extra caution --
    they are exactly what V38's own success trigger required before an attempt
    could reach SUCCEEDED, so a store where they disagree is one that reached
    that state without passing through it. Beside that, a row is refused where
    two claim one attempt (the new key cannot hold both), where the attempt
    never succeeded, and where the command exited nonzero -- which was never a
    redemption at all under the meaning this version fixes.

    The check reads and refuses. Nothing is repaired, because every one of these
    is a store this product did not write, and guessing which half of a
    contradiction to keep is how evidence gets quietly rewritten.
    """

    unsatisfied = connection.execute(
        "SELECT attempt_id FROM tool_redemptions WHERE exit_code <> 0"
    ).fetchall()
    if unsatisfied:
        raise StoreMigrationRefused(
            f"{len(unsatisfied)} tool redemptions record a command that exited "
            "nonzero, which this version does not admit as a redemption; this "
            "command will not alter it"
        )

    duplicated = connection.execute(
        "SELECT attempt_id FROM tool_redemptions "
        "GROUP BY attempt_id HAVING count(*) > 1"
    ).fetchall()
    if duplicated:
        raise StoreMigrationRefused(
            f"{len(duplicated)} attempts each hold more than one tool redemption, "
            "which the attempt-owned shape cannot represent; this command will "
            "not alter it"
        )
    unowned = connection.execute(
        "SELECT redemption.attempt_id FROM tool_redemptions AS redemption "
        "LEFT JOIN agent_attempts AS attempt "
        "  ON attempt.attempt_id = redemption.attempt_id "
        "WHERE attempt.attempt_id IS NULL "
        "   OR attempt.state <> 'SUCCEEDED' "
        "   OR attempt.node_execution_id <> redemption.node_execution_id "
        "   OR attempt.run_id <> redemption.run_id "
        "   OR attempt.workflow_revision_hash <> redemption.workflow_revision_hash "
        "   OR attempt.node_id <> redemption.node_id"
    ).fetchall()
    if unowned:
        raise StoreMigrationRefused(
            f"{len(unowned)} tool redemptions do not belong to a succeeded attempt "
            "of their own execution; this command will not alter it"
        )
    orphaned = connection.execute(
        "SELECT redemption.attempt_id FROM tool_redemptions AS redemption "
        "JOIN agent_attempts AS attempt "
        "  ON attempt.attempt_id = redemption.attempt_id "
        "LEFT JOIN agent_receipts_v2 AS receipt "
        "  ON receipt.node_execution_id = redemption.node_execution_id "
        " AND receipt.run_id = redemption.run_id "
        " AND receipt.workflow_revision_hash = redemption.workflow_revision_hash "
        " AND receipt.node_id = redemption.node_id "
        " AND receipt.receipt_hash = attempt.receipt_hash "
        " AND receipt.request_hash = attempt.request_hash "
        " AND receipt.executor_operational_identity "
        "     = attempt.executor_operational_identity "
        "WHERE receipt.node_execution_id IS NULL"
    ).fetchall()
    if orphaned:
        raise StoreMigrationRefused(
            f"{len(orphaned)} tool redemptions hang from no agent receipt of "
            "their own attempt's execution; this command will not alter it"
        )


def _apply_v38_to_v39(connection: sqlite3.Connection) -> None:
    """Admit CANDIDATE_CAPTURE_FAILED, and re-own a redemption to its attempt.

    Every stored FAILED attempt carries one of the six older codes, which the
    widened constraint still admits, so no ending changes meaning. Nothing is
    backfilled either: an attempt that ended before this word could not have
    failed for this reason, and saying it did would invent a loss that never
    happened.

    The redemption table is rebuilt in the same hop because the new ending needs
    it: a redemption hung from the success-only agent receipt cannot be written
    beside an attempt that failed, so a check that really passed would be thrown
    away with the loss of the work. Its key moves to the attempt for the same
    reason a replacement attempt is its own attempt -- two attempts of one node
    redeem two grants.

    What every stored row must be for that carry to be honest is *checked*, not
    assumed. V38's two foreign keys point at different tables and constrain each
    other not at all: nothing there forbids two rows naming one attempt, an
    attempt that never succeeded, or a receipt and an attempt describing
    different executions. Those stores are not ones this product wrote, but a
    hop that silently collided their rows -- or moved one project's proof onto
    another attempt -- would corrupt the evidence it exists to preserve. So each
    row is read against the attempt it names and the receipt it hangs from
    first, and a store that fails is refused whole and left exactly as it was.
    """

    _refuse_redemptions_that_cannot_be_re_owned(connection)
    _rebuild_product_table(
        connection,
        agent_attempts,
        _PREDECESSOR_ATTEMPTS_BEFORE_CANDIDATE_CAPTURE_FAILED,
        _AGENT_ATTEMPTS_TRIGGERS,
        _VERSION_THIRTY_EIGHT,
        _VERSION_THIRTY_NINE,
        trigger_source=_V49_AGENT_ATTEMPT_TRIGGERS,
    )
    _rebuild_product_table(
        connection,
        tool_redemptions,
        _PREDECESSOR_REDEMPTIONS_BOUND_TO_THE_RECEIPT,
        _TOOL_REDEMPTIONS_TRIGGERS,
        _VERSION_THIRTY_EIGHT,
        _VERSION_THIRTY_NINE,
    )
    _raise_declared_version(connection, _VERSION_THIRTY_EIGHT, _VERSION_THIRTY_NINE)


_MODEL_CONFIGURATION_TABLES = (
    host_model_registry_revisions,
    host_model_registry_entries,
    host_project_model_defaults_revisions,
    host_project_model_defaults,
)
_MODEL_CONFIGURATION_TRIGGERS = (
    "host_model_registry_revisions_no_update",
    "host_model_registry_revisions_no_delete",
    "host_model_registry_entries_no_update",
    "host_model_registry_entries_no_delete",
    "host_project_model_defaults_revisions_no_update",
    "host_project_model_defaults_revisions_no_delete",
    "host_project_model_defaults_no_update",
    "host_project_model_defaults_no_delete",
)


def _apply_v39_to_v40(connection: sqlite3.Connection) -> None:
    """Replace lineage occupancy with registry and difficulty configuration.

    No occupancy row is carried: its project-and-lineage key has no equivalent
    in either replacement record, so any mapping would silently invent which
    difficulty a role meant. The preflight happens before the first drop, and
    the migration transaction keeps every old row intact if any replacement
    name is already occupied.
    """

    for name in (
        *(table.name for table in _MODEL_CONFIGURATION_TABLES),
        *_MODEL_CONFIGURATION_TRIGGERS,
    ):
        existing = connection.execute(
            _SQLITE_MASTER_OBJECT_TYPE_QUERY, (name,)
        ).fetchone()
        if existing is not None:
            raise StoreMigrationRefused(
                f"schema version {_VERSION_THIRTY_NINE} already has {name}; "
                "this command will not alter it"
            )
    for trigger_name in _OCCUPANCY_TRIGGER_STATEMENTS:
        connection.execute(f"DROP TRIGGER {trigger_name}")
    connection.execute("DROP TABLE host_occupancy_bindings")
    connection.execute("DROP TABLE host_occupancy_revisions")
    for table in _MODEL_CONFIGURATION_TABLES:
        connection.execute(
            str(CreateTable(table).compile(dialect=sqlite_dialect.dialect()))
        )
    for trigger_name in _MODEL_CONFIGURATION_TRIGGERS:
        connection.execute(_PRODUCT_TRIGGERS[trigger_name])
    _raise_declared_version(connection, _VERSION_THIRTY_NINE, _VERSION_FORTY)


_RUN_FORK_TABLES = (run_forks, run_fork_reused_nodes, run_fork_effect_fences)
_RUN_FORK_TRIGGERS = (
    "run_forks_no_update",
    "run_forks_no_delete",
    "run_fork_reused_nodes_no_update",
    "run_fork_reused_nodes_no_delete",
    "run_fork_effect_fences_no_update",
    "run_fork_effect_fences_no_delete",
)
_EFFECT_RECEIPTS_TRIGGERS = (
    "effect_receipts_no_update",
    "effect_receipts_no_delete",
)
_PREDECESSOR_EFFECT_RECEIPTS_BEFORE_FORK_REFERENCE = (
    "effect_receipts_before_fork_reference"
)


def _apply_v40_to_v41(connection: sqlite3.Connection) -> None:
    """Add immutable fork evidence and admit a receipt's exact source reference."""

    for name in (
        *(table.name for table in _RUN_FORK_TABLES),
        *_RUN_FORK_TRIGGERS,
        _PREDECESSOR_EFFECT_RECEIPTS_BEFORE_FORK_REFERENCE,
    ):
        if (
            connection.execute(_SQLITE_MASTER_OBJECT_TYPE_QUERY, (name,)).fetchone()
            is not None
        ):
            raise StoreMigrationRefused(
                f"schema version {_VERSION_FORTY} already has {name}; "
                "this command will not alter it"
            )
    _rebuild_product_table(
        connection,
        effect_receipts,
        _PREDECESSOR_EFFECT_RECEIPTS_BEFORE_FORK_REFERENCE,
        _EFFECT_RECEIPTS_TRIGGERS,
        _VERSION_FORTY,
        _VERSION_FORTY_ONE,
    )
    for table in _RUN_FORK_TABLES:
        connection.execute(
            str(CreateTable(table).compile(dialect=sqlite_dialect.dialect()))
        )
    for trigger_name in _RUN_FORK_TRIGGERS:
        connection.execute(_PRODUCT_TRIGGERS[trigger_name])
    _raise_declared_version(connection, _VERSION_FORTY, _VERSION_FORTY_ONE)


_V41_EFFECT_INTENTS = "effect_intents_before_operation_name"
_V41_EFFECT_RECEIPTS = "effect_receipts_before_operation_name"


def _apply_v41_to_v42(connection: sqlite3.Connection) -> None:
    """Persist the closed effect operation on every intent and receipt."""

    mismatched = connection.execute(
        "SELECT r.logical_key FROM effect_receipts AS r "
        "JOIN effect_intents AS i ON i.logical_key = r.logical_key "
        "WHERE r.run_id <> i.run_id "
        "OR r.workflow_revision_hash <> i.workflow_revision_hash "
        "OR r.request_hash <> i.request_hash "
        "OR r.adapter_revision <> i.adapter_revision "
        "OR r.destination_identity <> i.destination_identity "
        "OR r.adapter_operational_identity <> i.adapter_operational_identity "
        "LIMIT 1"
    ).fetchone()
    if mismatched is not None:
        raise StoreMigrationRefused(
            "schema version 41 has an effect receipt whose durable binding differs "
            "from its intent; this command will not alter it"
        )
    _rebuild_product_table(
        connection,
        effect_receipts,
        _V41_EFFECT_RECEIPTS,
        _EFFECT_RECEIPTS_TRIGGERS,
        _VERSION_FORTY_ONE,
        _VERSION_FORTY_TWO,
        {"operation_name": "'open-pr'"},
    )
    _rebuild_product_table(
        connection,
        effect_intents,
        _V41_EFFECT_INTENTS,
        (
            "effect_intents_binding_no_update",
            "effect_intents_no_delete",
            "effect_intents_abandonment",
            "effect_intents_no_abandoned_insert",
        ),
        _VERSION_FORTY_ONE,
        _VERSION_FORTY_TWO,
        {"operation_name": "'open-pr'"},
    )
    _raise_declared_version(connection, _VERSION_FORTY_ONE, _VERSION_FORTY_TWO)


def _apply_v42_to_v43(connection: sqlite3.Connection) -> None:
    """Give a schema refusal its immutable per-attempt evidence record."""

    apply = _added_table_step(
        agent_attempt_receipts_v3,
        ("agent_attempt_receipts_v3_no_update", "agent_attempt_receipts_v3_no_delete"),
        _VERSION_FORTY_TWO,
        _VERSION_FORTY_THREE,
        allow_empty_prepared_table=True,
    )
    apply(connection)


_V43_QUEUE_ITEMS = "queue_items_before_phase_d"
_PHASE_D_QUEUE_TABLES = (
    queue_project_policy_revisions,
    queue_proposal_revisions,
    queue_dependency_edges,
    queue_launch_bindings,
)
_PHASE_D_QUEUE_IMMUTABILITY_TRIGGERS = (
    "queue_project_policy_revisions_no_update",
    "queue_project_policy_revisions_no_delete",
    "queue_proposal_revisions_no_update",
    "queue_proposal_revisions_no_delete",
    "queue_dependency_edges_no_update",
    "queue_dependency_edges_no_delete",
    "queue_launch_bindings_no_delete",
)


def _apply_v43_to_v44(connection: sqlite3.Connection) -> None:
    """Separate proposal, admission, and one exact launch without inventing either.

    The four new histories begin empty. The queue row gains nullable decision
    pointers, so every V43 row crosses byte-for-byte in its existing columns;
    in particular, an admitted row receives no proposal, authority, dependency,
    policy, or launch binding and is therefore read as LEGACY_REVIEW_REQUIRED.
    """

    for table in _PHASE_D_QUEUE_TABLES:
        existing = connection.execute(
            _SQLITE_MASTER_TABLE_EXISTS_QUERY,
            (table.name,),
        ).fetchone()
        if existing is not None:
            raise StoreMigrationRefused(
                f"schema version 43 already has {table.name}; "
                "this command will not alter it"
            )
        connection.execute(_table_shape_at(_VERSION_FORTY_FOUR, table))
    for trigger_name in _PHASE_D_QUEUE_IMMUTABILITY_TRIGGERS:
        connection.execute(_PRODUCT_TRIGGERS[trigger_name])
    _rebuild_product_table(
        connection,
        queue_items,
        _V43_QUEUE_ITEMS,
        (
            "queue_items_identity_no_update",
            "queue_items_no_delete",
        ),
        _VERSION_FORTY_THREE,
        _VERSION_FORTY_FOUR,
    )
    connection.execute(PUBLISHED_QUEUE_LAUNCH_BINDINGS_NO_UPDATE_BEFORE_RELEASE)
    connection.execute(_PRODUCT_TRIGGERS["queue_items_no_nonobserved_insert"])
    connection.execute(
        PUBLISHED_QUEUE_ITEMS_STATE_TRANSITION_TRIGGER_BEFORE_OBSERVATION
    )
    _raise_declared_version(connection, _VERSION_FORTY_THREE, _VERSION_FORTY_FOUR)


_V44_PROJECT_SOURCE_CONNECTIONS = "project_source_connections_before_identity"


def _legacy_project_source_id(project_id: str, source_kind: str) -> ProjectSourceId:
    digest = hashlib.sha256(
        frame(
            "legacy-project-source-id/v1",
            project_id.encode("utf-8"),
            source_kind.encode("utf-8"),
        )
    ).hexdigest()
    value = digest[:32]
    return ProjectSourceId(
        f"{value[:8]}-{value[8:12]}-{value[12:16]}-{value[16:20]}-{value[20:]}"
    )


def _v44_project_source_connection_revision_hash(
    project_id: ProjectId,
    revision_number: int,
    source_kind: SourceKind,
    source_address: SourceAddress,
    credential_directory: str | Path,
    auth_method: SourceConnectionAuthMethod,
    connected_by: ConnectionActor,
) -> str:
    """Rebuild the hash exactly as the published V44 contract framed it."""

    return hashlib.sha256(
        frame(
            "host-project-source-connection-revision/v1",
            project_id.value.encode("utf-8"),
            revision_number.to_bytes(8, byteorder="big"),
            source_kind.value.encode("utf-8"),
            source_address.value.encode("utf-8"),
            str(credential_directory).encode("utf-8"),
            auth_method.value.encode("ascii"),
            connected_by.value.encode("utf-8"),
        )
    ).hexdigest()


def _v44_connected_revision_keys(
    records: tuple[sqlite3.Row, ...],
) -> frozenset[tuple[str, str, int]]:
    """The one revision per project that stays connected after the hop.

    The V44 history carried no lifecycle: the kind whose latest revision is the
    project's newest is the connected one, and every other row is history.
    """

    latest_by_history: dict[tuple[str, str], int] = {}
    for record in records:
        history = (str(record["project_id"]), str(record["source_kind"]))
        latest_by_history[history] = max(
            latest_by_history.get(history, 0), int(record["revision_number"])
        )
    connected: set[tuple[str, str, int]] = set()
    for project_id in sorted({project_id for project_id, _ in latest_by_history}):
        latest_by_kind = {
            source_kind: revision_number
            for (owner, source_kind), revision_number in latest_by_history.items()
            if owner == project_id
        }
        project_maximum = max(latest_by_kind.values())
        current_kinds = tuple(
            source_kind
            for source_kind, revision_number in latest_by_kind.items()
            if revision_number == project_maximum
        )
        if len(current_kinds) != 1:
            raise StoreMigrationRefused(
                "schema version 44 contains durable project-source corruption: "
                f"project {project_id!r} has {len(current_kinds)} current kinds; "
                "expected exactly one; this command will not alter it"
            )
        connected.add((project_id, current_kinds[0], project_maximum))
    return frozenset(connected)


def _apply_v44_to_v45(connection: sqlite3.Connection) -> None:
    """Give the existing connection history identity and lifecycle without loss."""

    if connection.execute(
        _SQLITE_MASTER_NAME_EXISTS_QUERY,
        (_V44_PROJECT_SOURCE_CONNECTIONS,),
    ).fetchone():
        raise StoreMigrationRefused(
            f"schema version 44 already has {_V44_PROJECT_SOURCE_CONNECTIONS}; "
            "this command will not alter it"
        )
    cursor = connection.cursor()
    cursor.row_factory = sqlite3.Row
    records = tuple(
        cursor.execute(
            "SELECT * FROM host_project_source_connection_revisions "
            "ORDER BY project_id, source_kind, revision_number"
        ).fetchall()
    )
    connected_keys = _v44_connected_revision_keys(records)
    migrated_location_by_revision: dict[
        tuple[str, str, int], tuple[SourceAddress, SourceReference | None]
    ] = {}
    for record in records:
        project_id = str(record["project_id"])
        source_kind = SourceKind(str(record["source_kind"]))
        revision_number = int(record["revision_number"])
        source_address = SourceAddress(str(record["source_address"]))
        expected_hash = _v44_project_source_connection_revision_hash(
            ProjectId(project_id),
            revision_number,
            source_kind,
            source_address,
            str(record["credential_directory"]),
            SourceConnectionAuthMethod(str(record["auth_method"])),
            ConnectionActor(str(record["connected_by"])),
        )
        if record["revision_hash"] != expected_hash:
            raise StoreMigrationRefused(
                "schema version 44 project-source connection hash does not "
                "match its fields; this command will not alter it"
            )
        try:
            migrated_location_by_revision[
                (
                    project_id,
                    source_kind.value,
                    revision_number,
                )
            ] = migrate_v44_github_source_location(source_kind, source_address)
        except (TypeError, ValueError) as error:
            raise StoreMigrationRefused(
                "schema version 44 has a malformed GitHub project-source "
                "location; this command will not alter it"
            ) from error
    trigger_names = (
        "host_project_source_connection_revisions_no_update",
        "host_project_source_connection_revisions_no_delete",
    )
    for trigger_name in trigger_names:
        connection.execute(f"DROP TRIGGER {trigger_name}")
    connection.execute("PRAGMA legacy_alter_table=ON")
    try:
        connection.execute(
            "ALTER TABLE host_project_source_connection_revisions "
            f"RENAME TO {_V44_PROJECT_SOURCE_CONNECTIONS}"
        )
    finally:
        connection.execute("PRAGMA legacy_alter_table=OFF")
    connection.execute(
        str(
            CreateTable(host_project_source_connection_revisions).compile(
                dialect=sqlite_dialect.dialect()
            )
        )
    )
    for record in records:
        project_id = str(record["project_id"])
        source_kind = str(record["source_kind"])
        key = (project_id, source_kind, int(record["revision_number"]))
        source_address, source_ref = migrated_location_by_revision[key]
        revision = ProjectSourceConnectionRevision(
            ProjectId(project_id),
            _legacy_project_source_id(project_id, source_kind),
            int(record["revision_number"]),
            SourceKind(source_kind),
            source_address,
            Path(str(record["credential_directory"])),
            SourceConnectionAuthMethod(str(record["auth_method"])),
            ConnectionActor(str(record["connected_by"])),
            ProjectSourceConnectionLifecycle.CONNECTED
            if key in connected_keys
            else ProjectSourceConnectionLifecycle.DISCONNECTED,
            None,
            source_ref,
        )
        connection.execute(
            "INSERT INTO host_project_source_connection_revisions "
            "(revision_hash, project_id, source_id, source_kind, revision_number, "
            "source_address, source_ref, credential_directory, auth_method, "
            "connected_by, lifecycle, connected_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                revision.revision_hash.value,
                revision.project_id.value,
                revision.source_id.value,
                revision.source_kind.value,
                revision.revision_number,
                revision.source_address.value,
                None if revision.source_ref is None else revision.source_ref.value,
                str(revision.credential_directory),
                revision.auth_method.value,
                revision.connected_by.value,
                revision.lifecycle.value,
                None,
            ),
        )
    connection.execute(f"DROP TABLE {_V44_PROJECT_SOURCE_CONNECTIONS}")
    for trigger_name in trigger_names:
        connection.execute(_PRODUCT_TRIGGERS[trigger_name])
    _raise_declared_version(connection, _VERSION_FORTY_FOUR, _VERSION_FORTY_FIVE)


_PREDECESSOR_WAIT_ANSWERS_WITHOUT_ACTOR = "wait_answers_before_actor"
_PREDECESSOR_RUN_EVENTS_WITHOUT_WAIT_ACTOR = "run_events_before_wait_actor"


def _apply_v45_to_v46(connection: sqlite3.Connection) -> None:
    """Persist expected actors and name predecessor answers as unattributed."""

    _rebuild_product_table(
        connection,
        run_events,
        _PREDECESSOR_RUN_EVENTS_WITHOUT_WAIT_ACTOR,
        _RUN_EVENTS_TRIGGERS,
        _VERSION_FORTY_FIVE,
        _VERSION_FORTY_SIX,
        {
            run_events.c.wait_answer_actor.name: (
                "CASE WHEN event_kind = 'WAITING_INPUT' THEN 'operator' ELSE NULL END"
            )
        },
    )

    _rebuild_product_table(
        connection,
        wait_answers,
        _PREDECESSOR_WAIT_ANSWERS_WITHOUT_ACTOR,
        _WAIT_ANSWERS_TRIGGERS,
        _VERSION_FORTY_FIVE,
        _VERSION_FORTY_SIX,
        {
            wait_answers.c.actor.name: "NULL",
            wait_answers.c.actor_attribution_kind.name: (
                f"'{WaitAnswerAttributionKind.LEGACY_UNATTRIBUTED.value}'"
            ),
        },
    )
    _raise_declared_version(connection, _VERSION_FORTY_FIVE, _VERSION_FORTY_SIX)


def _apply_v46_to_v47(connection: sqlite3.Connection) -> None:
    _added_table_step(
        catalog_intakes,
        ("catalog_intakes_no_update", "catalog_intakes_no_delete"),
        _VERSION_FORTY_SIX,
        _VERSION_FORTY_SEVEN,
    )(connection)


_V47_QUEUE_ITEMS = "queue_items_before_tracker_observation"


def _apply_v47_to_v48(connection: sqlite3.Connection) -> None:
    """Give the queue item its last-observed title and two observation markers.

    Every stored row crosses byte-for-byte in its existing columns; the three
    new columns are nullable and carry no invented value (ADR 0016, 2026-09-01
    amendment) -- a predecessor row simply has no observation until an import
    writes one.
    """

    connection.execute("DROP TRIGGER queue_items_state_transition")
    _rebuild_product_table(
        connection,
        queue_items,
        _V47_QUEUE_ITEMS,
        (
            "queue_items_identity_no_update",
            "queue_items_no_delete",
            "queue_items_no_nonobserved_insert",
        ),
        _VERSION_FORTY_SEVEN,
        _VERSION_FORTY_EIGHT,
    )
    connection.execute(PUBLISHED_QUEUE_ITEMS_TRANSITION_BEFORE_RELEASE)
    _raise_declared_version(connection, _VERSION_FORTY_SEVEN, _VERSION_FORTY_EIGHT)


_DEFINITION_SOURCE_TABLES = (
    host_definition_source_revisions,
    host_definition_source_selections,
    catalog_source_intakes,
)
_DEFINITION_SOURCE_TRIGGERS = (
    "host_definition_source_revisions_no_update",
    "host_definition_source_revisions_no_delete",
    "host_definition_source_selections_no_update",
    "host_definition_source_selections_no_delete",
    "catalog_source_intakes_no_update",
    "catalog_source_intakes_no_delete",
)


def _apply_v48_to_v49(connection: sqlite3.Connection) -> None:
    """Give the store the git definition sources the catalog takes content from.

    Purely additive: three immutable tables and their triggers arrive, and not
    one stored row or existing table is touched. A store that already carries
    any of the three is refused whole rather than altered, because the rows in
    it were written by something this hop did not put there.
    """

    for name in (
        *(table.name for table in _DEFINITION_SOURCE_TABLES),
        *_DEFINITION_SOURCE_TRIGGERS,
    ):
        if (
            connection.execute(_SQLITE_MASTER_OBJECT_TYPE_QUERY, (name,)).fetchone()
            is not None
        ):
            raise StoreMigrationRefused(
                f"schema version {_VERSION_FORTY_EIGHT} already has {name}; "
                "this command will not alter it"
            )
    for table in _DEFINITION_SOURCE_TABLES:
        connection.execute(
            str(CreateTable(table).compile(dialect=sqlite_dialect.dialect()))
        )
    for trigger_name in _DEFINITION_SOURCE_TRIGGERS:
        connection.execute(_PRODUCT_TRIGGERS[trigger_name])
    _raise_declared_version(connection, _VERSION_FORTY_EIGHT, _VERSION_FORTY_NINE)


_PREDECESSOR_ATTEMPTS_BEFORE_CANDIDATE_UNCHANGED = (
    "agent_attempts_before_candidate_unchanged"
)


def _apply_v49_to_v50(connection: sqlite3.Connection) -> None:
    """Admit CANDIDATE_UNCHANGED, and keep every stored row exactly as it is.

    A vocabulary hop and nothing else: every stored FAILED attempt carries one
    of the seven older codes, which the widened constraint still admits, so no
    ending changes meaning. Nothing is backfilled -- an attempt that ended
    before this word existed ran its verification, whatever its tree was, and
    saying it had ended for this reason would invent a history it never had.
    """

    _rebuild_product_table(
        connection,
        agent_attempts,
        _PREDECESSOR_ATTEMPTS_BEFORE_CANDIDATE_UNCHANGED,
        _AGENT_ATTEMPTS_TRIGGERS,
        _VERSION_FORTY_NINE,
        _VERSION_FIFTY,
        trigger_source=_V52_AGENT_ATTEMPT_TRIGGERS,
    )
    _raise_declared_version(connection, _VERSION_FORTY_NINE, _VERSION_FIFTY)


_PERMISSION_RECEIPT_TRIGGERS = (
    "permission_receipts_no_update",
    "permission_receipts_no_delete",
)


def _apply_v50_to_v51(connection: sqlite3.Connection) -> None:
    """Give the store the ledger every answered permission question is kept in.

    Purely additive: one immutable table and its triggers arrive, and not one
    stored row or existing table is touched. Nothing is backfilled -- an attempt
    that ran before this ledger existed answered its questions under a policy
    whose decisions nobody wrote down, and inventing rows for them would put
    authorisations into the record that never authorised anything.
    """

    _added_table_step(
        permission_receipts,
        _PERMISSION_RECEIPT_TRIGGERS,
        _VERSION_FIFTY,
        _VERSION_FIFTY_ONE,
    )(connection)


_QUEUE_POLICY_TRIGGERS = (
    "queue_project_policy_revisions_no_update",
    "queue_project_policy_revisions_no_delete",
)
_QUEUE_PROPOSAL_TRIGGERS = (
    "queue_proposal_revisions_no_update",
    "queue_proposal_revisions_no_delete",
)
_V51_QUEUE_PROJECT_POLICY_REVISIONS = "queue_project_policy_revisions_v51"
_V51_QUEUE_PROPOSAL_REVISIONS = "queue_proposal_revisions_v51"


def _apply_v51_to_v52(connection: sqlite3.Connection) -> None:
    """Give the policy its proposal defaults, and every proposal its source.

    Both tables keep every stored row. The policy's three default columns are
    nullable and stay empty: a policy published before this hop named no
    defaults, and filling them would put a workflow choice into the record
    that no operator made. A proposal's source is not nullable and every
    carried row takes `OPERATOR`, which is what the record says -- the
    operator's own `PUT /queue-proposals` was the only writer that existed.
    """

    _rebuild_product_table(
        connection,
        queue_project_policy_revisions,
        _V51_QUEUE_PROJECT_POLICY_REVISIONS,
        _QUEUE_POLICY_TRIGGERS,
        _VERSION_FIFTY_ONE,
        _VERSION_FIFTY_TWO,
    )
    _rebuild_product_table(
        connection,
        queue_proposal_revisions,
        _V51_QUEUE_PROPOSAL_REVISIONS,
        _QUEUE_PROPOSAL_TRIGGERS,
        _VERSION_FIFTY_ONE,
        _VERSION_FIFTY_TWO,
        filled_columns={"source": f"'{QueueProposalSource.OPERATOR.value}'"},
    )
    _raise_declared_version(connection, _VERSION_FIFTY_ONE, _VERSION_FIFTY_TWO)


_PREDECESSOR_ATTEMPTS_BEFORE_PRODUCED_VALUE_REFUSED = (
    "agent_attempts_before_produced_value_refused"
)


def _apply_v52_to_v53(connection: sqlite3.Connection) -> None:
    """Admit PRODUCED_VALUE_REFUSED, and keep every stored row exactly as it is.

    A vocabulary hop and nothing else: every stored FAILED attempt carries one
    of the eight older codes, which the widened constraint still admits, so no
    ending changes meaning. Nothing is backfilled -- an attempt refused before
    this word existed was refused for bytes a provider wrote, which is what
    `OUTPUT_SCHEMA_REFUSED` says of it, and renaming it now would move an
    authorship this product never recorded.
    """

    _rebuild_product_table(
        connection,
        agent_attempts,
        _PREDECESSOR_ATTEMPTS_BEFORE_PRODUCED_VALUE_REFUSED,
        _AGENT_ATTEMPTS_TRIGGERS,
        _VERSION_FIFTY_TWO,
        _VERSION_FIFTY_THREE,
    )
    _raise_declared_version(connection, _VERSION_FIFTY_TWO, _VERSION_FIFTY_THREE)


_V53_EFFECT_INTENTS = "effect_intents_before_claim_work_item"
_V53_EFFECT_RECEIPTS = "effect_receipts_before_claim_work_item"


def _apply_v53_to_v54(connection: sqlite3.Connection) -> None:
    """Admit `claim-work-item`, and keep every stored intent and receipt.

    A vocabulary hop and nothing else: every stored effect names `open-pr` or
    `push-atelier-commit`, which the widened constraint still admits, so no
    recorded effect changes meaning. Nothing is backfilled -- a run that worked
    before this word existed held no claim, and writing one now would put a
    coordination fact into the record that never happened. The receipts are
    rebuilt before the intents they hang from, so the child's foreign key is
    written against the parent it will keep.
    """

    _rebuild_product_table(
        connection,
        effect_receipts,
        _V53_EFFECT_RECEIPTS,
        _EFFECT_RECEIPTS_TRIGGERS,
        _VERSION_FIFTY_THREE,
        _VERSION_FIFTY_FOUR,
    )
    _rebuild_product_table(
        connection,
        effect_intents,
        _V53_EFFECT_INTENTS,
        (
            "effect_intents_binding_no_update",
            "effect_intents_no_delete",
            "effect_intents_abandonment",
            "effect_intents_no_abandoned_insert",
        ),
        _VERSION_FIFTY_THREE,
        _VERSION_FIFTY_FOUR,
    )
    _raise_declared_version(connection, _VERSION_FIFTY_THREE, _VERSION_FIFTY_FOUR)


_V54_QUEUE_LAUNCH_BINDINGS = "queue_launch_bindings_before_release"


def _apply_v54_to_v55(connection: sqlite3.Connection) -> None:
    """Let a launch binding take the ending of its run, and keep every one.

    Every stored binding crosses byte-for-byte as the first launch of its item,
    at restart ordinal zero and without an ending: writing one now would say
    the sweep had given that item back under a rule that released nothing.
    """

    connection.execute("DROP TRIGGER queue_launch_bindings_no_update")
    _rebuild_product_table(
        connection,
        queue_launch_bindings,
        _V54_QUEUE_LAUNCH_BINDINGS,
        ("queue_launch_bindings_no_delete",),
        _VERSION_FIFTY_FOUR,
        _VERSION_FIFTY_FIVE,
        filled_columns={"restart_ordinal": "0"},
    )
    connection.execute(_PRODUCT_TRIGGERS["queue_launch_bindings_release_only"])
    connection.execute("DROP TRIGGER queue_items_state_transition")
    connection.execute(_PRODUCT_TRIGGERS["queue_items_state_transition"])
    _raise_declared_version(connection, _VERSION_FIFTY_FOUR, _VERSION_FIFTY_FIVE)


@dataclass(frozen=True)
class _SchemaMigrationStep:
    source_version: int
    target_version: int
    apply: Callable[[sqlite3.Connection], None]


_SCHEMA_MIGRATION_STEPS: tuple[_SchemaMigrationStep, ...] = (
    _SchemaMigrationStep(
        _VERSION_THIRTEEN,
        _VERSION_FOURTEEN,
        _added_table_step(
            run_inputs_v3,
            ("run_inputs_v3_no_update", "run_inputs_v3_no_delete"),
            _VERSION_THIRTEEN,
            _VERSION_FOURTEEN,
        ),
    ),
    _SchemaMigrationStep(
        _VERSION_FOURTEEN,
        _VERSION_FIFTEEN,
        _added_table_step(
            tool_redemptions,
            ("tool_redemptions_no_update", "tool_redemptions_no_delete"),
            _VERSION_FOURTEEN,
            _VERSION_FIFTEEN,
        ),
    ),
    _SchemaMigrationStep(_VERSION_FIFTEEN, _VERSION_SIXTEEN, _apply_v15_to_v16),
    _SchemaMigrationStep(_VERSION_SIXTEEN, _VERSION_SEVENTEEN, _apply_v16_to_v17),
    _SchemaMigrationStep(_VERSION_SEVENTEEN, _VERSION_EIGHTEEN, _apply_v17_to_v18),
    _SchemaMigrationStep(
        _VERSION_EIGHTEEN,
        _VERSION_NINETEEN,
        _added_table_step(
            artifacts,
            ("artifacts_no_update", "artifacts_no_delete"),
            _VERSION_EIGHTEEN,
            _VERSION_NINETEEN,
        ),
    ),
    _SchemaMigrationStep(_VERSION_NINETEEN, _VERSION_TWENTY, _apply_v19_to_v20),
    _SchemaMigrationStep(_VERSION_TWENTY, _VERSION_TWENTY_ONE, _apply_v20_to_v21),
    _SchemaMigrationStep(_VERSION_TWENTY_ONE, _VERSION_TWENTY_TWO, _apply_v21_to_v22),
    _SchemaMigrationStep(_VERSION_TWENTY_TWO, _VERSION_TWENTY_THREE, _apply_v22_to_v23),
    _SchemaMigrationStep(
        _VERSION_TWENTY_THREE, _VERSION_TWENTY_FOUR, _apply_v23_to_v24
    ),
    _SchemaMigrationStep(
        _VERSION_TWENTY_FOUR,
        _VERSION_TWENTY_FIVE,
        _added_table_step(
            host_project_root_revisions,
            (
                "host_project_root_revisions_no_update",
                "host_project_root_revisions_no_delete",
            ),
            _VERSION_TWENTY_FOUR,
            _VERSION_TWENTY_FIVE,
        ),
    ),
    _SchemaMigrationStep(_VERSION_TWENTY_FIVE, _VERSION_TWENTY_SIX, _apply_v25_to_v26),
    _SchemaMigrationStep(_VERSION_TWENTY_SIX, _VERSION_TWENTY_SEVEN, _apply_v26_to_v27),
    _SchemaMigrationStep(
        _VERSION_TWENTY_SEVEN, _VERSION_TWENTY_EIGHT, _apply_v27_to_v28
    ),
    _SchemaMigrationStep(
        _VERSION_TWENTY_EIGHT,
        _VERSION_TWENTY_NINE,
        _added_table_step(
            queue_items,
            ("queue_items_identity_no_update", "queue_items_no_delete"),
            _VERSION_TWENTY_EIGHT,
            _VERSION_TWENTY_NINE,
        ),
    ),
    _SchemaMigrationStep(_VERSION_TWENTY_NINE, _VERSION_THIRTY, _apply_v29_to_v30),
    _SchemaMigrationStep(
        _VERSION_THIRTY,
        _VERSION_THIRTY_ONE,
        _added_table_step(
            webhook_delivery_cursor,
            (
                "webhook_delivery_cursor_identity_no_update",
                "webhook_delivery_cursor_no_delete",
            ),
            _VERSION_THIRTY,
            _VERSION_THIRTY_ONE,
        ),
    ),
    _SchemaMigrationStep(
        _VERSION_THIRTY_ONE,
        _VERSION_THIRTY_TWO,
        _apply_v31_to_v32,
    ),
    _SchemaMigrationStep(
        _VERSION_THIRTY_TWO,
        _VERSION_THIRTY_THREE,
        _added_table_step(
            host_project_source_connection_revisions,
            (
                "host_project_source_connection_revisions_no_update",
                "host_project_source_connection_revisions_no_delete",
            ),
            _VERSION_THIRTY_TWO,
            _VERSION_THIRTY_THREE,
        ),
    ),
    _SchemaMigrationStep(
        _VERSION_THIRTY_THREE,
        _VERSION_THIRTY_FOUR,
        _apply_v33_to_v34,
    ),
    _SchemaMigrationStep(
        _VERSION_THIRTY_FOUR,
        _VERSION_THIRTY_FIVE,
        _apply_v34_to_v35,
    ),
    _SchemaMigrationStep(
        _VERSION_THIRTY_FIVE,
        _VERSION_THIRTY_SIX,
        _apply_v35_to_v36,
    ),
    _SchemaMigrationStep(
        _VERSION_THIRTY_SIX,
        _VERSION_THIRTY_SEVEN,
        _apply_v36_to_v37,
    ),
    _SchemaMigrationStep(
        _VERSION_THIRTY_SEVEN,
        _VERSION_THIRTY_EIGHT,
        _apply_v37_to_v38,
    ),
    _SchemaMigrationStep(
        _VERSION_THIRTY_EIGHT,
        _VERSION_THIRTY_NINE,
        _apply_v38_to_v39,
    ),
    _SchemaMigrationStep(
        _VERSION_THIRTY_NINE,
        _VERSION_FORTY,
        _apply_v39_to_v40,
    ),
    _SchemaMigrationStep(
        _VERSION_FORTY,
        _VERSION_FORTY_ONE,
        _apply_v40_to_v41,
    ),
    _SchemaMigrationStep(
        _VERSION_FORTY_ONE,
        _VERSION_FORTY_TWO,
        _apply_v41_to_v42,
    ),
    _SchemaMigrationStep(
        _VERSION_FORTY_TWO,
        _VERSION_FORTY_THREE,
        _apply_v42_to_v43,
    ),
    _SchemaMigrationStep(
        _VERSION_FORTY_THREE,
        _VERSION_FORTY_FOUR,
        _apply_v43_to_v44,
    ),
    _SchemaMigrationStep(
        _VERSION_FORTY_FOUR,
        _VERSION_FORTY_FIVE,
        _apply_v44_to_v45,
    ),
    _SchemaMigrationStep(
        _VERSION_FORTY_FIVE,
        _VERSION_FORTY_SIX,
        _apply_v45_to_v46,
    ),
    _SchemaMigrationStep(_VERSION_FORTY_SIX, _VERSION_FORTY_SEVEN, _apply_v46_to_v47),
    _SchemaMigrationStep(_VERSION_FORTY_SEVEN, _VERSION_FORTY_EIGHT, _apply_v47_to_v48),
    _SchemaMigrationStep(_VERSION_FORTY_EIGHT, _VERSION_FORTY_NINE, _apply_v48_to_v49),
    _SchemaMigrationStep(_VERSION_FORTY_NINE, _VERSION_FIFTY, _apply_v49_to_v50),
    _SchemaMigrationStep(_VERSION_FIFTY, _VERSION_FIFTY_ONE, _apply_v50_to_v51),
    _SchemaMigrationStep(_VERSION_FIFTY_ONE, _VERSION_FIFTY_TWO, _apply_v51_to_v52),
    _SchemaMigrationStep(_VERSION_FIFTY_TWO, _VERSION_FIFTY_THREE, _apply_v52_to_v53),
    _SchemaMigrationStep(_VERSION_FIFTY_THREE, _VERSION_FIFTY_FOUR, _apply_v53_to_v54),
    _SchemaMigrationStep(_VERSION_FIFTY_FOUR, _VERSION_FIFTY_FIVE, _apply_v54_to_v55),
)
_SCHEMA_MIGRATION_BY_SOURCE = {
    step.source_version: step for step in _SCHEMA_MIGRATION_STEPS
}


def _fingerprint_for_version(connection: sqlite3.Connection, version: int) -> str:
    _require_product_shape(connection, version)
    return _product_schema_fingerprint_sha256(
        _product_schema_fingerprint(
            connection, _table_names_for_version(version), version=version
        )
    )


def _validate_v44_queue_rows(connection: sqlite3.Connection) -> None:
    """Refuse queue rows that SQL NULL could otherwise disguise as valid."""

    invalid = connection.execute(
        """
        SELECT item.item_id
        FROM queue_items AS item
        LEFT JOIN queue_proposal_revisions AS proposal
          ON proposal.item_id = item.item_id
         AND proposal.proposal_revision = item.current_proposal_revision
        WHERE NOT (
          (item.state = 'OBSERVED'
           AND item.state_version = 0
           AND item.workflow_lineage_id IS NULL
           AND item.admission_rationale IS NULL
           AND item.current_proposal_revision IS NULL
           AND item.decision_authority IS NULL)
          OR
          (item.state = 'PROPOSED'
           AND item.current_proposal_revision IS NOT NULL
           AND item.current_proposal_revision >= 1
           AND item.state_version = item.current_proposal_revision
           AND item.workflow_lineage_id IS NULL
           AND item.admission_rationale IS NULL
           AND item.decision_authority IS NULL
           AND proposal.item_id IS NOT NULL)
          OR
          (item.state = 'ADMITTED'
           AND item.workflow_lineage_id IS NOT NULL
           AND item.admission_rationale IS NOT NULL
           AND item.current_proposal_revision IS NULL
           AND item.decision_authority IS NULL)
          OR
          (item.state = 'ADMITTED'
           AND item.workflow_lineage_id IS NOT NULL
           AND item.admission_rationale IS NOT NULL
           AND item.current_proposal_revision IS NOT NULL
           AND item.current_proposal_revision >= 1
           AND item.state_version = item.current_proposal_revision + 1
           AND item.decision_authority IS NOT NULL
           AND item.decision_authority IN ('OPERATOR', 'AUTOMATION_RULE')
           AND proposal.item_id IS NOT NULL
           AND proposal.workflow_lineage_id = item.workflow_lineage_id)
        )
        LIMIT 1
        """
    ).fetchone()
    if invalid is not None:
        raise StoreMigrationRefused(
            f"queue item {invalid[0]} is a partial or inconsistent V44 decision; "
            "this command will not alter it"
        )


def _inspect_store_readonly(database_path: Path) -> tuple[int, str | None]:
    """Read the version, and the fingerprint when this command can honour it.

    A refuse path must not open the file for write: converting journal mode or
    taking a write lock would mutate a store we then claim we left alone.
    """

    try:
        with sqlite3.connect(
            f"{database_path.resolve().as_uri()}?mode=ro", uri=True
        ) as connection:
            version = _read_declared_schema_version(connection)
            if version == SCHEMA_VERSION or version in _SCHEMA_MIGRATION_BY_SOURCE:
                try:
                    if version in {_VERSION_FORTY_FOUR, _VERSION_FORTY_FIVE}:
                        _validate_v44_queue_rows(connection)
                    return version, _fingerprint_for_version(connection, version)
                except UnsupportedSchemaVersion as error:
                    raise StoreMigrationRefused(str(error)) from error
            return version, None
    except StoreMigrationRefused:
        raise
    except sqlite3.DatabaseError as error:
        if _is_sqlite_lock(error):
            raise StoreInUse() from error
        raise StoreMigrationRefused(
            "the database is unreadable; this command will not alter it"
        ) from error


def _refusable_fingerprint(connection: sqlite3.Connection, version: int) -> str:
    try:
        return _fingerprint_for_version(connection, version)
    except UnsupportedSchemaVersion as error:
        raise StoreMigrationRefused(str(error)) from error


def _begin_immediate(connection: sqlite3.Connection) -> None:
    try:
        connection.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as error:
        if _is_sqlite_lock(error):
            raise StoreInUse() from error
        raise StoreMigrationRefused(
            "the database could not be locked; this command will not alter it"
        ) from error


def _migration_hops(
    connection: sqlite3.Connection, source_version: int
) -> list[tuple[int, int, str]]:
    """Apply every step from `source_version` up, proving each hop's fingerprint."""

    completed: list[tuple[int, int, str]] = []
    current = source_version
    while current != SCHEMA_VERSION:
        current_step = _SCHEMA_MIGRATION_BY_SOURCE.get(current)
        if current_step is None:
            raise StoreMigrationRefused(
                f"schema version {current} has no migration step; "
                "this command will not alter it"
            )
        try:
            current_step.apply(connection)
        except sqlite3.DatabaseError as error:
            raise StoreMigrationRefused(
                f"migration from schema version {current} failed: {error}; "
                "this command will not alter it"
            ) from error
        fingerprint = _refusable_fingerprint(connection, current_step.target_version)
        completed.append(
            (current_step.source_version, current_step.target_version, fingerprint)
        )
        current = current_step.target_version
    return completed


def migrate_store(database_path: Path) -> StoreMigrationReport:
    """Raise one existing store to SCHEMA_VERSION, or refuse it unaltered.

    The hop is a SQLite transaction on the named file: additive DDL and a
    version CAS, then the published fingerprint. A copy-then-swap would have
    to checkpoint WAL, copy the sidecar files, and still risk a torn rename;
    the object's native atomicity is the transaction. Each committed step is
    a complete published schema, never a half-written one.
    """

    if database_path.is_dir():
        raise StoreMigrationRefused(
            f"{database_path} is a directory, not a database file"
        )
    if not database_path.is_file() or database_path.stat().st_size == 0:
        raise StoreMigrationRefused(
            f"{database_path} is not a database file; "
            "this command does not create a store"
        )

    source_version, preview_fingerprint = _inspect_store_readonly(database_path)
    if source_version == SCHEMA_VERSION:
        if preview_fingerprint is None:
            raise StoreMigrationRefused(
                f"schema version {SCHEMA_VERSION} fingerprint could not be read; "
                "this command will not alter it"
            )
        return StoreMigrationReport(
            source_version,
            SCHEMA_VERSION,
            preview_fingerprint,
            True,
            (),
        )
    if source_version not in _SCHEMA_MIGRATION_BY_SOURCE:
        if source_version in _OFFLINE_CUTOVER_VERSIONS:
            raisable = ", ".join(
                str(version) for version in sorted(_SCHEMA_MIGRATION_BY_SOURCE)
            )
            raise StoreMigrationRefused(
                f"schema version {source_version} has no migration step; "
                f"only version {raisable} can be raised to {SCHEMA_VERSION}. "
                "runtime startup still refuses it without mutation"
            )
        raise StoreMigrationRefused(
            f"schema version {source_version} is unknown; "
            "this command will not alter it"
        )

    connection = sqlite3.connect(str(database_path.resolve()), timeout=0)
    try:
        connection.execute("PRAGMA busy_timeout=0")
        # Deliberately OFF for the hop, per SQLite's own table-rebuild recipe:
        # with enforcement on, renaming a table out rewrites every child
        # declaration to follow the parked name, which the rebuild then drops.
        # Row-level integrity is not waived -- the explicit foreign_key_check
        # before the commit refuses the whole hop on any violation.
        connection.execute("PRAGMA foreign_keys=OFF")
        _begin_immediate(connection)
        try:
            locked_version = _read_declared_schema_version(connection)
            if locked_version != source_version:
                raise StoreMigrationRefused(
                    f"schema version changed from {source_version} to "
                    f"{locked_version} before the hop; this command will not alter it"
                )
            _refusable_fingerprint(connection, locked_version)
            completed = _migration_hops(connection, locked_version)
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                tables = ", ".join(sorted({str(row[0]) for row in violations}))
                raise StoreMigrationRefused(
                    f"the migrated store violates foreign keys in {tables}; "
                    "this command will not alter it"
                )
            _validate_v44_queue_rows(connection)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        try:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.DatabaseError:
            pass
        return StoreMigrationReport(
            source_version,
            SCHEMA_VERSION,
            completed[-1][2],
            False,
            tuple(completed),
        )
    finally:
        connection.close()
