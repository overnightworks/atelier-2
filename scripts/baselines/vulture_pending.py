"""Names that wait for a decision an open item already owns.

Read as data by `scripts/check_dead_code.py`; never imported at runtime. Every
entry is `module/path.py:symbol`, relative to `src/atelier2`, exactly as
vulture reports it: qualifying by module is what stops excusing a name in one
module from vouching for a dead namesake in another. Every group carries the
day it expires, and the gate turns red once that day arrives: a parked
decision is allowed to be slow, not permanent. A name that turns out to be
built ahead of a caller rather than awaiting a decision moves to
`vulture_frozen.py`; a name the decision retires is deleted with its code.
"""

WAITING_FOR_A_DECISION = (
    {
        "names": (
            "adapters/dbos/schema.py:V9_SCHEMA_HANDOFF",
            "adapters/dbos/schema.py:V10_SCHEMA_HANDOFF",
            "adapters/dbos/schema.py:V11_SCHEMA_HANDOFF",
            "adapters/dbos/schema.py:V12_SCHEMA_HANDOFF",
            "adapters/dbos/schema.py:V13_SCHEMA_HANDOFF",
            "adapters/dbos/schema.py:V14_SCHEMA_HANDOFF",
            "adapters/dbos/schema.py:V15_SCHEMA_HANDOFF",
            "adapters/dbos/schema.py:V16_SCHEMA_HANDOFF",
            "adapters/dbos/schema.py:V17_SCHEMA_HANDOFF",
            "adapters/dbos/schema.py:V18_SCHEMA_HANDOFF",
            "adapters/dbos/schema.py:V19_SCHEMA_HANDOFF",
            "adapters/dbos/schema.py:V20_SCHEMA_HANDOFF",
            "adapters/dbos/schema.py:V21_SCHEMA_HANDOFF",
            "adapters/dbos/schema.py:V22_SCHEMA_HANDOFF",
            "adapters/dbos/schema.py:V23_SCHEMA_HANDOFF",
            "adapters/dbos/schema.py:V24_SCHEMA_HANDOFF",
            "adapters/dbos/schema.py:V25_SCHEMA_HANDOFF",
            "adapters/dbos/schema.py:V26_SCHEMA_HANDOFF",
            "adapters/dbos/schema.py:V27_SCHEMA_HANDOFF",
            "adapters/dbos/schema.py:V28_SCHEMA_HANDOFF",
            "adapters/dbos/schema.py:V29_SCHEMA_HANDOFF",
            "adapters/dbos/schema.py:V30_SCHEMA_HANDOFF",
            "adapters/dbos/schema.py:V31_SCHEMA_HANDOFF",
            "adapters/dbos/schema.py:V32_SCHEMA_HANDOFF",
            "adapters/dbos/schema.py:V33_SCHEMA_HANDOFF",
            "adapters/dbos/schema.py:V34_SCHEMA_HANDOFF",
            "adapters/dbos/schema.py:V35_SCHEMA_HANDOFF",
            "adapters/dbos/schema.py:V36_SCHEMA_HANDOFF",
            "adapters/dbos/schema.py:V37_SCHEMA_HANDOFF",
            "adapters/dbos/schema.py:V38_SCHEMA_HANDOFF",
            "adapters/dbos/schema.py:V39_SCHEMA_HANDOFF",
            "adapters/dbos/schema.py:V40_SCHEMA_HANDOFF",
            "adapters/dbos/schema.py:V41_SCHEMA_HANDOFF",
            "adapters/dbos/schema.py:V42_SCHEMA_HANDOFF",
            "adapters/dbos/schema.py:V43_SCHEMA_HANDOFF",
            "adapters/dbos/schema.py:V44_SCHEMA_HANDOFF",
            "adapters/dbos/schema.py:V45_SCHEMA_HANDOFF",
            "adapters/dbos/schema.py:V46_SCHEMA_HANDOFF",
            "adapters/dbos/schema.py:V47_SCHEMA_HANDOFF",
            "adapters/dbos/schema.py:V48_SCHEMA_HANDOFF",
            "adapters/dbos/schema.py:V49_SCHEMA_HANDOFF",
            "adapters/dbos/schema.py:PRODUCT_SCHEMA_HANDOFF",
        ),
        "why": (
            "#1168 finding 8: the V9..V49 schema-handoff ledger is alive only in "
            "tests/integration/test_store_migration.py -- production migrates one "
            "hop and refuses every other version. #1168 finding 2a decides whether "
            "the migration ladder below the live store's version is deleted, and "
            "the ledger goes with it."
        ),
        "expires_on": "2026-10-04",
    },
)
