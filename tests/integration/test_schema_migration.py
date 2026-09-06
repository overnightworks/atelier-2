from __future__ import annotations

import ast
import hashlib
import sqlite3
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import exc
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from atelier2.adapters.dbos import schema as schema_module
from atelier2.adapters.dbos.published_schema_shapes import (
    PUBLISHED_QUEUE_ITEMS_STATE_TRANSITION_TRIGGER_BEFORE_OBSERVATION,
)
from atelier2.adapters.dbos.runtime import create_canonical_engine
from atelier2.adapters.dbos.schema import (
    _PRODUCT_SCHEMA_FINGERPRINT_SHA256,
    PRODUCT_SCHEMA_HANDOFF,
    SCHEMA_VERSION,
    V9_SCHEMA_HANDOFF,
    V10_SCHEMA_HANDOFF,
    V11_SCHEMA_HANDOFF,
    V12_SCHEMA_HANDOFF,
    V13_SCHEMA_HANDOFF,
    V14_SCHEMA_HANDOFF,
    V15_SCHEMA_HANDOFF,
    V16_SCHEMA_HANDOFF,
    V17_SCHEMA_HANDOFF,
    V18_SCHEMA_HANDOFF,
    V19_SCHEMA_HANDOFF,
    V20_SCHEMA_HANDOFF,
    V21_SCHEMA_HANDOFF,
    V22_SCHEMA_HANDOFF,
    V23_SCHEMA_HANDOFF,
    V24_SCHEMA_HANDOFF,
    V25_SCHEMA_HANDOFF,
    V26_SCHEMA_HANDOFF,
    V27_SCHEMA_HANDOFF,
    V28_SCHEMA_HANDOFF,
    V29_SCHEMA_HANDOFF,
    V30_SCHEMA_HANDOFF,
    V31_SCHEMA_HANDOFF,
    V32_SCHEMA_HANDOFF,
    V33_SCHEMA_HANDOFF,
    V34_SCHEMA_HANDOFF,
    V35_SCHEMA_HANDOFF,
    V36_SCHEMA_HANDOFF,
    V37_SCHEMA_HANDOFF,
    V38_SCHEMA_HANDOFF,
    V39_SCHEMA_HANDOFF,
    V40_SCHEMA_HANDOFF,
    V41_SCHEMA_HANDOFF,
    V42_SCHEMA_HANDOFF,
    V43_SCHEMA_HANDOFF,
    V44_SCHEMA_HANDOFF,
    V45_SCHEMA_HANDOFF,
    V46_SCHEMA_HANDOFF,
    V47_SCHEMA_HANDOFF,
    V48_SCHEMA_HANDOFF,
    V49_SCHEMA_HANDOFF,
    MigrationRequired,
    StoreMigrationRefused,
    UnsupportedSchemaVersion,
    _product_schema_fingerprint,
    _product_schema_fingerprint_sha256,
    _require_product_shape,
    catalog_lineage_aliases,
    catalog_lineage_members,
    catalog_lineage_retirements,
    catalog_lineages,
    context_packages_v3,
    effect_intents,
    effect_receipts,
    initialize_schema,
    migrate_store,
    node_artifacts_v3,
    node_execution_requests_v3,
    node_receipt_outputs_v3,
    node_receipts_v3,
    published_revisions,
    queue_items,
    reconcile_commands,
    run_configuration_revisions,
    run_events,
    run_fork_effect_fences,
    run_fork_reused_nodes,
    run_forks,
    runs,
    workflow_revisions,
)
from atelier2.contracts.catalog_v3 import CatalogLineage
from atelier2.contracts.host_configuration import ProjectId
from atelier2.contracts.queue_projection import (
    QueueItemState,
    TrackerItemReference,
    WorkItemReference,
)
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.contracts.runs import FIRST_ROUND_ORDINAL
from tests.integration.test_store_migration import (
    _create_populated_v41_store,
    _restore_v48_definition_source_predecessor,
)


def _create_populated_version_one_database(database_path: Path) -> None:
    document = b"workflow-v1"
    revision_hash = hashlib.sha256(document).hexdigest()
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE atelier_schema_versions(version INTEGER PRIMARY KEY);
            CREATE TABLE workflow_revisions(
              revision_hash TEXT PRIMARY KEY,
              document BLOB NOT NULL
            );
            CREATE TABLE runs(
              run_id TEXT PRIMARY KEY,
              dbos_workflow_id TEXT UNIQUE NOT NULL,
              revision_hash TEXT NOT NULL REFERENCES workflow_revisions(revision_hash),
              state TEXT NOT NULL CHECK(state IN ('STARTED', 'COMPLETED'))
            );
            INSERT INTO atelier_schema_versions VALUES(1);
            """
        )
        connection.execute(
            "INSERT INTO workflow_revisions VALUES(?, ?)",
            (revision_hash, document),
        )
        connection.execute(
            "INSERT INTO runs VALUES(?, ?, ?, ?)",
            ("run-1", "workflow-1", revision_hash, "COMPLETED"),
        )


def _logical_dump(database_path: Path) -> tuple[str, ...]:
    with sqlite3.connect(database_path) as connection:
        return tuple(connection.iterdump())


def _file_snapshot(root: Path) -> dict[str, bytes]:
    return {
        entry.name: entry.read_bytes() for entry in root.iterdir() if entry.is_file()
    }


def test_populated_version_one_is_refused_without_logical_mutation(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    _create_populated_version_one_database(database_path)
    before_files = _file_snapshot(tmp_path)
    before_logical = _logical_dump(database_path)
    engine = create_canonical_engine(database_path)

    with pytest.raises(MigrationRequired, match="explicit offline migration"):
        initialize_schema(engine)

    engine.dispose()
    assert _file_snapshot(tmp_path) == before_files
    assert _logical_dump(database_path) == before_logical


def test_version_four_is_refused_without_changing_any_file_byte(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE atelier_schema_versions(version INTEGER PRIMARY KEY);
            CREATE TABLE v4_durable_state(value BLOB NOT NULL);
            INSERT INTO atelier_schema_versions VALUES(4);
            INSERT INTO v4_durable_state VALUES(X'00FF');
            """
        )
    before_files = _file_snapshot(tmp_path)
    engine = create_canonical_engine(database_path)

    with pytest.raises(MigrationRequired, match="schema version 4"):
        initialize_schema(engine)

    engine.dispose()
    assert _file_snapshot(tmp_path) == before_files


@pytest.mark.parametrize(
    "schema_sql",
    [
        "CREATE TABLE unowned_state(value TEXT NOT NULL);",
        """
        CREATE TABLE atelier_schema_versions(version INTEGER NOT NULL);
        INSERT INTO atelier_schema_versions VALUES(1);
        INSERT INTO atelier_schema_versions VALUES(2);
        """,
        """
        CREATE TABLE atelier_schema_versions(version INTEGER PRIMARY KEY);
        INSERT INTO atelier_schema_versions VALUES(3);
        """,
        """
        CREATE TABLE atelier_schema_versions(version TEXT NOT NULL);
        INSERT INTO atelier_schema_versions VALUES('2');
        """,
    ],
    ids=[
        "missing-version-owner",
        "multiple-versions",
        "future-version",
        "malformed-version",
    ],
)
def test_unknown_schema_is_refused_without_logical_mutation(
    tmp_path: Path, schema_sql: str
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(schema_sql)
    before_files = _file_snapshot(tmp_path)
    before_logical = _logical_dump(database_path)
    engine = create_canonical_engine(database_path)

    with pytest.raises(UnsupportedSchemaVersion):
        initialize_schema(engine)

    engine.dispose()
    assert _file_snapshot(tmp_path) == before_files
    assert _logical_dump(database_path) == before_logical


@pytest.mark.proves("fresh-store-has-no-writerless-access-storage")
def test_empty_database_creates_the_exact_current_schema_and_reopens(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    engine = create_canonical_engine(database_path)

    initialize_schema(engine)

    with sqlite3.connect(database_path) as connection:
        assert (
            _product_schema_fingerprint_sha256(_product_schema_fingerprint(connection))
            == _PRODUCT_SCHEMA_FINGERPRINT_SHA256[SCHEMA_VERSION]
        )
    with engine.connect() as connection:
        assert (
            connection.scalar(sa.text("SELECT version FROM atelier_schema_versions"))
            == SCHEMA_VERSION
        )
        assert set(sa.inspect(connection).get_table_names()) >= {
            effect_intents.name,
            effect_receipts.name,
            reconcile_commands.name,
            published_revisions.name,
            catalog_lineages.name,
            catalog_lineage_members.name,
            catalog_lineage_aliases.name,
            catalog_lineage_retirements.name,
            node_artifacts_v3.name,
            node_receipts_v3.name,
            node_receipt_outputs_v3.name,
            run_forks.name,
            run_fork_reused_nodes.name,
            run_fork_effect_fences.name,
        }
        access_objects = connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE name LIKE 'node_receipt_access_v3%'"
        ).scalars()
        assert tuple(access_objects) == ()
        assert {
            column["name"] for column in sa.inspect(connection).get_columns("runs")
        } >= {
            "run_id",
            "bootstrap_workflow_id",
            "revision_hash",
            "workflow_format_version",
            "agent_binding_set_hash",
            "current_node_id",
            "state",
            "state_version",
            "last_event_sequence",
            "terminal_hash",
        }
        assert "dbos_workflow_id" not in {
            column["name"] for column in sa.inspect(connection).get_columns("runs")
        }
    first_dump = _logical_dump(database_path)

    initialize_schema(engine)

    assert _logical_dump(database_path) == first_dump
    engine.dispose()


def test_published_handoffs_pin_every_predecessor_and_the_current_schema() -> None:
    assert V9_SCHEMA_HANDOFF.version == 9
    assert (
        V9_SCHEMA_HANDOFF.fingerprint_sha256
        == _PRODUCT_SCHEMA_FINGERPRINT_SHA256[8]
        == _PRODUCT_SCHEMA_FINGERPRINT_SHA256[9]
        == "6ba76214cb567ffcdab46e5a3ae00fc10824b962f16a8036ce90590be0b79b38"
    )
    assert V10_SCHEMA_HANDOFF.version == 10
    assert (
        V10_SCHEMA_HANDOFF.fingerprint_sha256
        == _PRODUCT_SCHEMA_FINGERPRINT_SHA256[10]
        == "4a7bbd9bf07880868aa2f7ddae3e7262eb270f711d4fdc420f902457817bfff7"
    )
    assert V11_SCHEMA_HANDOFF.version == 11
    assert (
        V11_SCHEMA_HANDOFF.fingerprint_sha256
        == _PRODUCT_SCHEMA_FINGERPRINT_SHA256[11]
        == "18dead2ab36c15bf61fa1b1bb5fed3b5a1075dc773d83d8b57c00c05c84178ef"
    )
    assert V12_SCHEMA_HANDOFF.version == 12
    assert (
        V12_SCHEMA_HANDOFF.fingerprint_sha256
        == _PRODUCT_SCHEMA_FINGERPRINT_SHA256[12]
        == "feef25b171e305bb9a3a9637cc4d0fb1c8dec4a4a7a9813e060ccf12598a5cc7"
    )
    assert V13_SCHEMA_HANDOFF.version == 13
    assert (
        V13_SCHEMA_HANDOFF.fingerprint_sha256
        == _PRODUCT_SCHEMA_FINGERPRINT_SHA256[13]
        == "5782fdc1331c52f3f04097f6a2a6d416ab528d6ee8a6546a7d6435ae9d11c175"
    )
    assert V14_SCHEMA_HANDOFF.version == 14
    assert (
        V14_SCHEMA_HANDOFF.fingerprint_sha256
        == _PRODUCT_SCHEMA_FINGERPRINT_SHA256[14]
        == "6cf56491322e716fce9be2310584ed2b92533961b8fda341bfcc317182432f0a"
    )
    assert V15_SCHEMA_HANDOFF.version == 15
    assert (
        V15_SCHEMA_HANDOFF.fingerprint_sha256
        == _PRODUCT_SCHEMA_FINGERPRINT_SHA256[15]
        == "375e81d1c8967053951d1be0cab19cee274e35272f364feae15ec3413eb3c9b9"
    )
    assert V16_SCHEMA_HANDOFF.version == 16
    assert (
        V16_SCHEMA_HANDOFF.fingerprint_sha256
        == _PRODUCT_SCHEMA_FINGERPRINT_SHA256[16]
        == "97605fb330cb6382d52a554d644015f631cccea3759c04c27de3ca5f1fea9c3a"
    )
    assert V17_SCHEMA_HANDOFF.version == 17
    assert (
        V17_SCHEMA_HANDOFF.fingerprint_sha256
        == _PRODUCT_SCHEMA_FINGERPRINT_SHA256[17]
        == "2f3a11d0b4d67e375259ca732c7243c95d19fa763e03785b0bd4a83c1b1359d2"
    )
    assert V18_SCHEMA_HANDOFF.version == 18
    assert (
        V18_SCHEMA_HANDOFF.fingerprint_sha256
        == _PRODUCT_SCHEMA_FINGERPRINT_SHA256[18]
        == "c60275544c9984adccff79e3a4f5ab6eeab5ea1683306adf1d2faa7dbb51e29d"
    )
    assert V19_SCHEMA_HANDOFF.version == 19
    assert (
        V19_SCHEMA_HANDOFF.fingerprint_sha256
        == _PRODUCT_SCHEMA_FINGERPRINT_SHA256[19]
        == "a861d9087da05c112f88ae8ec573f57338b5ef1d04f36553922c505127b34298"
    )
    assert V20_SCHEMA_HANDOFF.version == 20
    assert (
        V20_SCHEMA_HANDOFF.fingerprint_sha256
        == _PRODUCT_SCHEMA_FINGERPRINT_SHA256[20]
        == "09752981999444ee4129cfe29b7322b79d2ff378f91d1af5050342eff78b8637"
    )
    assert V21_SCHEMA_HANDOFF.version == 21
    assert (
        V21_SCHEMA_HANDOFF.fingerprint_sha256
        == _PRODUCT_SCHEMA_FINGERPRINT_SHA256[21]
        == "6c4705f2960d1669a596ae8f3c857dd0ac15c4c94b71b4bb5998d1bac672cefe"
    )
    assert V22_SCHEMA_HANDOFF.version == 22
    assert (
        V22_SCHEMA_HANDOFF.fingerprint_sha256
        == _PRODUCT_SCHEMA_FINGERPRINT_SHA256[22]
        == "72aa8f76942197b704f07c156adbb1e46c3b069ce16a53c6d95a067827966387"
    )
    assert V23_SCHEMA_HANDOFF.version == 23
    assert (
        V23_SCHEMA_HANDOFF.fingerprint_sha256
        == _PRODUCT_SCHEMA_FINGERPRINT_SHA256[23]
        == "6d8a3af85ecc40781c6eea454e33ae625de1cf6d8726ca5c502cdcc33eb2c124"
    )
    assert V24_SCHEMA_HANDOFF.version == 24
    assert (
        V24_SCHEMA_HANDOFF.fingerprint_sha256
        == _PRODUCT_SCHEMA_FINGERPRINT_SHA256[24]
        == "ba573ba80dbdbb5d9b2a93bc6958b7544838915be3e0f5fc816cacc718dfe9c8"
    )
    assert V25_SCHEMA_HANDOFF.version == 25
    assert (
        V25_SCHEMA_HANDOFF.fingerprint_sha256
        == _PRODUCT_SCHEMA_FINGERPRINT_SHA256[25]
        == "91d8889ce6239855c894b89ab658188d9b13927dedb1cc905dacdc151a485842"
    )
    assert V26_SCHEMA_HANDOFF.version == 26
    assert (
        V26_SCHEMA_HANDOFF.fingerprint_sha256
        == _PRODUCT_SCHEMA_FINGERPRINT_SHA256[26]
        == "0af3ca8bbbbe06a56c56bb0988de384fde2a807b1e409152a02e1e226e917ab8"
    )
    assert V27_SCHEMA_HANDOFF.version == 27
    assert (
        V27_SCHEMA_HANDOFF.fingerprint_sha256
        == _PRODUCT_SCHEMA_FINGERPRINT_SHA256[27]
        == "7f929ab33c6b8742ff24a301bb13cb1f49a4ced2d96b52b97dbb26196ebd2ac4"
    )
    assert V28_SCHEMA_HANDOFF.version == 28
    assert (
        V28_SCHEMA_HANDOFF.fingerprint_sha256
        == _PRODUCT_SCHEMA_FINGERPRINT_SHA256[28]
        == "8e15796b7361796fc5c70e9c1682ddf58b967dea7fb112127366cfca600c9b36"
    )
    assert V29_SCHEMA_HANDOFF.version == 29
    assert (
        V29_SCHEMA_HANDOFF.fingerprint_sha256
        == _PRODUCT_SCHEMA_FINGERPRINT_SHA256[29]
        == "06e1d67be5f39569e7661321c063f7ea84c95efba2906519e7473a6f2016b640"
    )
    assert V30_SCHEMA_HANDOFF.version == 30
    assert (
        V30_SCHEMA_HANDOFF.fingerprint_sha256
        == _PRODUCT_SCHEMA_FINGERPRINT_SHA256[30]
        == "1229c61ee62c20531cb31ed324a3b822646d56899f30be62ab1c6abebf325c3c"
    )
    assert V31_SCHEMA_HANDOFF.version == 31
    assert (
        V31_SCHEMA_HANDOFF.fingerprint_sha256
        == _PRODUCT_SCHEMA_FINGERPRINT_SHA256[31]
        == "60d98794edd55744b3ec2cc4f4d7b9bf7a23106b4b7f0d4b9a009042d054a419"
    )
    assert V32_SCHEMA_HANDOFF.version == 32
    assert (
        V32_SCHEMA_HANDOFF.fingerprint_sha256
        == _PRODUCT_SCHEMA_FINGERPRINT_SHA256[32]
        == "0cdbeaf303f2839661930234a508e141cd995b8552def9b426a52aaad1eab84e"
    )
    assert V33_SCHEMA_HANDOFF.version == 33
    assert (
        V33_SCHEMA_HANDOFF.fingerprint_sha256
        == _PRODUCT_SCHEMA_FINGERPRINT_SHA256[33]
        == "f634d04c6cc525147ead8aa0dad8ef728189a6ef9554049c8a2aad56f3caeea8"
    )
    assert V34_SCHEMA_HANDOFF.version == 34
    assert (
        V34_SCHEMA_HANDOFF.fingerprint_sha256
        == _PRODUCT_SCHEMA_FINGERPRINT_SHA256[34]
        == "28dab0f4a152d7be66fa699d1123fdd130a94fe80ad705c19330f075e4fdd85a"
    )
    assert V35_SCHEMA_HANDOFF.version == 35
    assert (
        V35_SCHEMA_HANDOFF.fingerprint_sha256
        == _PRODUCT_SCHEMA_FINGERPRINT_SHA256[35]
        == "29df9a195316ce94527be2c906e4dc4104e00b2cb16caa9bfada17fecb5a21d5"
    )
    assert V36_SCHEMA_HANDOFF.version == 36
    assert (
        V36_SCHEMA_HANDOFF.fingerprint_sha256
        == _PRODUCT_SCHEMA_FINGERPRINT_SHA256[36]
        == "c9f4b5d99a9ff8e33796e36151b66f00175eceaa797e30461bf6e01264266ce8"
    )
    assert V37_SCHEMA_HANDOFF.version == 37
    assert (
        V37_SCHEMA_HANDOFF.fingerprint_sha256
        == _PRODUCT_SCHEMA_FINGERPRINT_SHA256[37]
        == "e41cf318212e0a79d6605413b5818ef68d6245baaf05a53b888b8aac40131a13"
    )
    assert V38_SCHEMA_HANDOFF.version == 38
    assert (
        V38_SCHEMA_HANDOFF.fingerprint_sha256
        == _PRODUCT_SCHEMA_FINGERPRINT_SHA256[38]
        == "aebd8b6bad8a719864f0c02828db643dd3dcbe7c89198beb6a8c1c4c30100824"
    )
    assert V39_SCHEMA_HANDOFF.version == 39
    assert (
        V39_SCHEMA_HANDOFF.fingerprint_sha256
        == _PRODUCT_SCHEMA_FINGERPRINT_SHA256[39]
        == "3c0cc05dd977fd61d2c88d78ba7566fdc0146e2d7af27df61aea636a4ac2c4be"
    )
    assert V40_SCHEMA_HANDOFF.version == 40
    assert (
        V40_SCHEMA_HANDOFF.fingerprint_sha256
        == _PRODUCT_SCHEMA_FINGERPRINT_SHA256[40]
        == "d8d7b89cc0cacd15dfde84bf15f796f0e03d9b571c26be0309ed87a60960071d"
    )
    assert V41_SCHEMA_HANDOFF.version == 41
    assert (
        V41_SCHEMA_HANDOFF.fingerprint_sha256
        == _PRODUCT_SCHEMA_FINGERPRINT_SHA256[41]
        == "7c4bc13ceb1db7533bfdf9697c1e6b262032a516275b488eac73af9969446b68"
    )
    assert V42_SCHEMA_HANDOFF.version == 42
    assert (
        V42_SCHEMA_HANDOFF.fingerprint_sha256
        == _PRODUCT_SCHEMA_FINGERPRINT_SHA256[42]
        == "d2f874edd0dbbecb677b284db8e41cd3a681fae99703d126764bc90fa0cf7865"
    )
    assert V43_SCHEMA_HANDOFF.version == 43
    assert (
        V43_SCHEMA_HANDOFF.fingerprint_sha256
        == _PRODUCT_SCHEMA_FINGERPRINT_SHA256[43]
        == "f7d299ab865b87ca47a399d4897f8c7b273085c4d206fac9eb882d47198b9782"
    )
    assert V44_SCHEMA_HANDOFF.version == 44
    assert (
        V44_SCHEMA_HANDOFF.fingerprint_sha256
        == _PRODUCT_SCHEMA_FINGERPRINT_SHA256[44]
        == "b8a176e76092a24fa0c8ac1caafdd69e57f4ff404ecb5560a1dd426d32a3ee9b"
    )
    assert V45_SCHEMA_HANDOFF.version == 45
    assert (
        V45_SCHEMA_HANDOFF.fingerprint_sha256
        == _PRODUCT_SCHEMA_FINGERPRINT_SHA256[45]
        == "39d0811369f0b7a4b248448042623ecde0d290e95d191d75c32a9faf538fffa5"
    )
    assert V46_SCHEMA_HANDOFF.version == 46
    assert (
        V46_SCHEMA_HANDOFF.fingerprint_sha256
        == _PRODUCT_SCHEMA_FINGERPRINT_SHA256[46]
        == "1428683e38b4cce26b866e02ef1afa974a2c3208e26606f8792d0d48f0b1a43b"
    )
    assert V47_SCHEMA_HANDOFF.version == 47
    assert (
        V47_SCHEMA_HANDOFF.fingerprint_sha256
        == _PRODUCT_SCHEMA_FINGERPRINT_SHA256[47]
        == "d7987152a11a2702808b5fb1b71c0891e1c6724519435e87ee840cc235c00e39"
    )
    assert V48_SCHEMA_HANDOFF.version == 48
    assert (
        V48_SCHEMA_HANDOFF.fingerprint_sha256
        == _PRODUCT_SCHEMA_FINGERPRINT_SHA256[48]
        == "ecf4b2aba21f7225f121a3afc128d76e9ce10801c83121a93712f39320704653"
    )
    assert V49_SCHEMA_HANDOFF.version == 49
    assert (
        V49_SCHEMA_HANDOFF.fingerprint_sha256
        == _PRODUCT_SCHEMA_FINGERPRINT_SHA256[49]
        == "01930b9de9fc8804ed1be5ec34dc02df926373cb95f20319f6e38d92b1c39ea2"
    )
    # V50 to V52 published no handoff of their own: those fingerprints are
    # alive only here (#1168 finding 8), so they are pinned without adding to
    # the handoff list.
    assert (
        _PRODUCT_SCHEMA_FINGERPRINT_SHA256[50]
        == "bb34288b35fbf4fe059960323b7a92ee4e5473b5a945e697c0f4b9fe29c6d8a9"
    )
    assert (
        _PRODUCT_SCHEMA_FINGERPRINT_SHA256[51]
        == "2b0be085b59e160db8b9d925bbb889205b32a2bbd45fcad673277b2b229fd622"
    )
    assert (
        _PRODUCT_SCHEMA_FINGERPRINT_SHA256[52]
        == "6121453b26de9913e212d726b95d74def93c0a754e25eadfadbe77f7c7c432e2"
    )
    assert (
        _PRODUCT_SCHEMA_FINGERPRINT_SHA256[53]
        == "038b3e7f5ca011d78e6a1013d7b3fde96b8056165106a2c71898e3353e9da881"
    )
    assert (
        _PRODUCT_SCHEMA_FINGERPRINT_SHA256[54]
        == "13edd2cba8b5bca12e4c6c679aa7a5974d36693cd6b0e0e8da132736afe56aa4"
    )
    assert PRODUCT_SCHEMA_HANDOFF.version == SCHEMA_VERSION == 55
    assert (
        PRODUCT_SCHEMA_HANDOFF.fingerprint_sha256
        == _PRODUCT_SCHEMA_FINGERPRINT_SHA256[55]
        == "51111cb385fa429bd596df41e633c8f3eb15e81be8874b950de4e3c896b9f1b2"
    )


def test_v47_store_migrates_onwards_inventing_no_observation_or_retirement(
    tmp_path: Path,
) -> None:
    """A real V47 queue row crosses the hop with its new columns NULL.

    The store is raised to today's declaration, then `queue_items` and its
    transition trigger are rebuilt down to the exact bytes V47 published and
    every table a later hop added is taken back off -- proving the migration on
    a real predecessor row, not on a freshly created database.
    """

    database_path = tmp_path / "atelier.sqlite"
    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    with engine.begin() as connection:
        connection.execute(
            workflow_revisions.insert().values(revision_hash="a" * 64, document=b"kept")
        )
    engine.dispose()
    reference = WorkItemReference(ProjectId("project1"), TrackerItemReference("gh:79"))
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("BEGIN IMMEDIATE")
        _restore_v48_definition_source_predecessor(connection)
        schema_module._rebuild_product_table(
            connection,
            queue_items,
            "queue_items_v48",
            (
                "queue_items_identity_no_update",
                "queue_items_no_delete",
                "queue_items_no_nonobserved_insert",
                "queue_items_state_transition",
            ),
            48,
            47,
            trigger_source={
                **schema_module._PRODUCT_TRIGGERS,
                "queue_items_state_transition": (
                    PUBLISHED_QUEUE_ITEMS_STATE_TRANSITION_TRIGGER_BEFORE_OBSERVATION
                ),
            },
        )
        connection.execute(
            f"INSERT INTO queue_items VALUES "
            f"(?, ?, ?, '{QueueItemState.OBSERVED.value}', 0, NULL, NULL, NULL, NULL)",
            (
                reference.item_id.value,
                reference.project.value,
                reference.tracker_item.value,
            ),
        )
        connection.execute("UPDATE atelier_schema_versions SET version = 47")
        connection.commit()
        _require_product_shape(connection, 47)

    report = migrate_store(database_path)

    assert (report.source_version, report.target_version) == (47, SCHEMA_VERSION)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT document FROM workflow_revisions"
        ).fetchone() == (b"kept",)
        row = connection.execute(
            "SELECT state, observed_title, title_observed_at, retired_at "
            "FROM queue_items WHERE item_id = ?",
            (reference.item_id.value,),
        ).fetchone()
        assert row == (QueueItemState.OBSERVED.value, None, None, None)
        _require_product_shape(connection, SCHEMA_VERSION)


def _schema_object_names(connection: sqlite3.Connection) -> frozenset[str]:
    """Every object this product declared, without SQLite's own bookkeeping.

    A `sqlite_autoindex_*` is not a schema object anybody wrote: SQLite mints
    one per UNIQUE and primary key inside a CREATE TABLE this product already
    names, so counting them would be counting the same declaration twice.
    """

    return frozenset(
        str(record[0])
        for record in connection.execute("SELECT name FROM sqlite_master")
        if not str(record[0]).startswith("sqlite_")
    )


_REPUBLISHED_BY_A_LATER_HOP = (
    "agent_attempts",
    "effect_intents",
    "effect_receipts",
    "queue_launch_bindings",
    "queue_project_policy_revisions",
    "queue_proposal_revisions",
)
"""The tables a hop after the one under test republishes for reasons of its own.

A store migrated to today crosses every remaining hop, not only the one a test
is about: V50 and V53 rebuild the attempt table to widen its failure-code
vocabulary, V52 gives the queue policy its proposal defaults and every proposal
the source that wrote it, V54 widens the effect tables' operation vocabulary,
and V55 gives a launch binding its ending and its key. Their declarations are
therefore expected to differ afterwards; every row in them, and every other
statement, is not.
"""

_RETRIGGERED_BY_A_LATER_HOP = ("queue_items_state_transition",)
"""The guards a later hop rewrites without moving the table they stand on."""


def _rows_beside_the_version_owner(connection: sqlite3.Connection) -> frozenset[str]:
    """Every dumped statement except the ones a hop is expected to change."""

    republished = tuple(
        prefix
        for name in _REPUBLISHED_BY_A_LATER_HOP
        for prefix in (f"CREATE TABLE {name} (", f"CREATE TRIGGER {name}_")
    ) + tuple(f"CREATE TRIGGER {name}" for name in _RETRIGGERED_BY_A_LATER_HOP)
    return frozenset(
        statement
        for statement in connection.iterdump()
        if "atelier_schema_versions" not in statement
        and not statement.startswith(republished)
    )


def test_a_v48_store_gains_the_definition_source_tables_and_keeps_its_rows(
    tmp_path: Path,
) -> None:
    """The additive hop adds its empty tables and touches nothing else.

    A V48 store crosses every remaining hop, so the permission ledger V51 adds
    arrives here too -- also empty, and also without a row invented for the
    history it predates.
    """

    database_path = tmp_path / "atelier.sqlite"
    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    with engine.begin() as connection:
        connection.execute(
            workflow_revisions.insert().values(revision_hash="a" * 64, document=b"kept")
        )
    engine.dispose()
    with sqlite3.connect(database_path) as connection:
        _restore_v48_definition_source_predecessor(connection)
        connection.execute("UPDATE atelier_schema_versions SET version = 48")
        connection.commit()
        _require_product_shape(connection, 48)
        standing = _rows_beside_the_version_owner(connection)
        named = _schema_object_names(connection)

    report = migrate_store(database_path)

    assert (report.source_version, report.target_version) == (48, SCHEMA_VERSION)
    with sqlite3.connect(database_path) as connection:
        assert _rows_beside_the_version_owner(connection) >= standing
        assert _schema_object_names(connection) - named == frozenset(
            {
                "host_definition_source_revisions",
                "host_definition_source_revisions_no_update",
                "host_definition_source_revisions_no_delete",
                "host_definition_source_selections",
                "host_definition_source_selections_no_update",
                "host_definition_source_selections_no_delete",
                "catalog_source_intakes",
                "catalog_source_intakes_no_update",
                "catalog_source_intakes_no_delete",
                "permission_receipts",
                "permission_receipts_no_update",
                "permission_receipts_no_delete",
                "queue_launch_bindings_release_only",
            }
        )
        assert all(
            connection.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
            for table in (
                "host_definition_source_revisions",
                "host_definition_source_selections",
                "catalog_source_intakes",
                "permission_receipts",
            )
        )
        _require_product_shape(connection, SCHEMA_VERSION)


def test_a_store_already_carrying_a_definition_source_table_is_refused_whole(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    engine.dispose()
    with sqlite3.connect(database_path) as connection:
        _restore_v48_definition_source_predecessor(connection)
        connection.execute("CREATE TABLE catalog_source_intakes (mine TEXT)")
        connection.execute("UPDATE atelier_schema_versions SET version = 48")
        connection.commit()

    with pytest.raises(StoreMigrationRefused, match="catalog_source_intakes"):
        migrate_store(database_path)

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT version FROM atelier_schema_versions"
        ).fetchone() == (48,)


def test_populated_v41_effect_rows_are_backfilled_before_v42_publish(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    _create_populated_v41_store(database_path)

    report = migrate_store(database_path)

    assert (report.source_version, report.target_version) == (41, SCHEMA_VERSION)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT operation_name FROM effect_intents"
        ).fetchall() == [("open-pr",)]
        assert connection.execute(
            "SELECT operation_name FROM effect_receipts"
        ).fetchall() == [("open-pr",)]


@pytest.mark.parametrize(
    "version",
    [
        7,
        8,
        9,
        10,
        11,
        12,
        13,
        14,
        15,
        16,
        17,
        18,
        19,
        20,
        21,
        22,
        23,
        24,
        25,
        26,
        27,
        28,
        29,
        30,
        31,
        32,
        33,
        34,
        35,
        36,
        37,
        38,
    ],
)
def test_predecessor_store_is_refused_without_mutation(
    tmp_path: Path, version: int
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            f"""
            CREATE TABLE atelier_schema_versions(version INTEGER PRIMARY KEY);
            CREATE TABLE predecessor_witness(value BLOB NOT NULL);
            INSERT INTO atelier_schema_versions VALUES({version});
            INSERT INTO predecessor_witness VALUES(X'00FF');
            """
        )
    before_files = _file_snapshot(tmp_path)
    before_logical = _logical_dump(database_path)
    engine = create_canonical_engine(database_path)

    with pytest.raises(MigrationRequired, match=f"schema version {version}"):
        initialize_schema(engine)

    engine.dispose()
    assert _file_snapshot(tmp_path) == before_files
    assert _logical_dump(database_path) == before_logical


def _published_workflow(document: bytes = b"name: lasagne\n") -> PublishedRevision:
    return PublishedRevision(RevisionKind.WORKFLOW, document)


def _write_thin_vertical_set(
    connection: Connection,
    published: PublishedRevision,
    *,
    run_id: str = "run-lasagne",
    execution: str = "11" * 32,
    request: str = "22" * 32,
    package: str = "33" * 32,
    configuration: str = "44" * 32,
    receipt: str = "ef" * 32,
) -> CatalogLineage:
    lineage = CatalogLineage(published.kind, published.revision_hash)
    connection.execute(
        published_revisions.insert().values(
            kind=published.kind.value,
            revision_hash=published.revision_hash.value,
            document=published.document,
        )
    )
    connection.execute(
        catalog_lineages.insert().values(
            lineage_id=lineage.lineage_id.value,
            kind=published.kind.value,
            founding_revision_hash=published.revision_hash.value,
        )
    )
    connection.execute(
        catalog_lineage_members.insert().values(
            lineage_id=lineage.lineage_id.value,
            revision_number=1,
            revision_hash=published.revision_hash.value,
        )
    )
    connection.execute(
        workflow_revisions.insert().values(
            revision_hash=published.revision_hash.value, document=published.document
        )
    )
    connection.execute(
        run_configuration_revisions.insert().values(
            revision_hash=configuration, preimage=b"one frozen resolution matrix"
        )
    )
    connection.execute(
        context_packages_v3.insert().values(
            package_hash=package, manifest=b"one supervised manifest"
        )
    )
    connection.execute(
        node_execution_requests_v3.insert().values(
            request_hash=request,
            node_execution_id=execution,
            run_configuration_revision_hash=configuration,
            context_package_hash=package,
            preimage=b"one node execution request",
        )
    )
    connection.execute(
        runs.insert().values(
            run_id=run_id,
            bootstrap_workflow_id="bootstrap-lasagne",
            revision_hash=published.revision_hash.value,
            workflow_format_version=3,
            agent_binding_set_hash=None,
            current_node_id="cook",
            current_round_ordinal=FIRST_ROUND_ORDINAL,
            state="STARTED",
            state_version=0,
            last_event_sequence=0,
            terminal_hash=None,
            run_configuration_revision_hash=configuration,
        )
    )
    connection.execute(
        node_receipts_v3.insert().values(
            node_execution_id=execution,
            disposition="succeeded",
            reason="completed",
            request_hash=request,
            context_package_hash=package,
            receipt_hash=receipt,
        )
    )
    return lineage


def test_thin_v14_store_accepts_revision_lineage_member_run_and_receipt(
    tmp_path: Path,
) -> None:
    engine = create_canonical_engine(tmp_path / "atelier.sqlite")
    initialize_schema(engine)
    published = _published_workflow()
    with engine.begin() as connection:
        _write_thin_vertical_set(connection, published)
    with engine.connect() as connection:
        stored_hash = connection.scalar(sa.select(published_revisions.c.revision_hash))
        assert stored_hash == published.revision_hash.value
        assert (
            connection.scalar(
                sa.select(sa.func.count()).select_from(catalog_lineage_members)
            )
            == 1
        )
        assert (
            connection.scalar(sa.select(node_receipts_v3.c.disposition)) == "succeeded"
        )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            node_receipts_v3.insert().values(
                node_execution_id="44" * 32,
                disposition="stale",
                reason="projected",
                request_hash="22" * 32,
                context_package_hash="33" * 32,
                receipt_hash="55" * 32,
            )
        )
    engine.dispose()


def test_thin_v14_store_refuses_invented_kind_and_unpublished_membership(
    tmp_path: Path,
) -> None:
    engine = create_canonical_engine(tmp_path / "atelier.sqlite")
    initialize_schema(engine)
    published = _published_workflow()
    schema_revision = PublishedRevision(RevisionKind.SCHEMA, b"type: object\n")
    missing = "ab" * 32
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            published_revisions.insert().values(
                kind="invented_kind",
                revision_hash=published.revision_hash.value,
                document=published.document,
            )
        )
    with engine.begin() as connection:
        connection.execute(
            published_revisions.insert().values(
                kind=published.kind.value,
                revision_hash=published.revision_hash.value,
                document=published.document,
            )
        )
        connection.execute(
            published_revisions.insert().values(
                kind=schema_revision.kind.value,
                revision_hash=schema_revision.revision_hash.value,
                document=schema_revision.document,
            )
        )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            catalog_lineages.insert().values(
                lineage_id="cd" * 32,
                kind=published.kind.value,
                founding_revision_hash=missing,
            )
        )
    lineage = CatalogLineage(published.kind, published.revision_hash)
    with engine.begin() as connection:
        connection.execute(
            catalog_lineages.insert().values(
                lineage_id=lineage.lineage_id.value,
                kind=published.kind.value,
                founding_revision_hash=published.revision_hash.value,
            )
        )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            catalog_lineage_members.insert().values(
                lineage_id=lineage.lineage_id.value,
                revision_number=1,
                revision_hash=missing,
            )
        )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            catalog_lineage_members.insert().values(
                lineage_id=lineage.lineage_id.value,
                revision_number=1,
                revision_hash=schema_revision.revision_hash.value,
            )
        )
    engine.dispose()


@pytest.mark.parametrize(
    "failpoint",
    (
        "published_revisions",
        "catalog_lineages",
        "catalog_lineage_members",
        "runs",
        "node_receipts_v3",
    ),
)
def test_thin_v14_write_failpoint_rolls_back_revision_lineage_run_and_receipt(
    tmp_path: Path, failpoint: str
) -> None:
    engine = create_canonical_engine(tmp_path / failpoint)
    initialize_schema(engine)
    published = _published_workflow()
    with engine.begin() as connection:
        connection.exec_driver_sql(
            f"CREATE TRIGGER fail_{failpoint} BEFORE INSERT ON {failpoint} "
            "BEGIN SELECT RAISE(ABORT, 'failpoint'); END"
        )
    with (
        pytest.raises(exc.DatabaseError, match="failpoint"),
        engine.begin() as connection,
    ):
        _write_thin_vertical_set(connection, published)
    with engine.connect() as connection:
        assert (
            connection.scalar(
                sa.select(sa.func.count()).select_from(published_revisions)
            )
            == 0
        )
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(catalog_lineages))
            == 0
        )
        assert (
            connection.scalar(
                sa.select(sa.func.count()).select_from(catalog_lineage_members)
            )
            == 0
        )
        assert connection.scalar(sa.select(sa.func.count()).select_from(runs)) == 0
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(node_receipts_v3))
            == 0
        )
        assert (
            connection.scalar(
                sa.select(sa.func.count()).select_from(workflow_revisions)
            )
            == 0
        )
    engine.dispose()


@pytest.mark.parametrize(
    ("revision_format_version", "requested_capability"),
    ((3, "headless"), (2, "unknown"), (1, "interactive")),
)
def test_v8_sql_rejects_unknown_or_incompatible_capability_contract(
    tmp_path: Path,
    revision_format_version: int,
    requested_capability: str,
) -> None:
    engine = create_canonical_engine(tmp_path / "atelier.sqlite")
    initialize_schema(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO auth_profile_revisions VALUES(?, ?, ?, ?, ?)",
            ("a" * 64, "max", 1, "anthropic", "subscription"),
        )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO agent_configuration_revisions VALUES(?, ?, ?, ?, ?, ?)",
            (
                "b" * 64,
                "opus",
                "a" * 64,
                "claude-cli/v1",
                revision_format_version,
                requested_capability,
            ),
        )
    engine.dispose()


def test_v6_requires_nonmutating_recreate(tmp_path: Path) -> None:
    database_path = tmp_path / "atelier.sqlite"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE atelier_schema_versions(version INTEGER PRIMARY KEY);
            CREATE TABLE durable_v6_witness(value BLOB NOT NULL);
            CREATE TRIGGER durable_v6_witness_no_delete
            BEFORE DELETE ON durable_v6_witness BEGIN
              SELECT RAISE(ABORT, 'immutable');
            END;
            INSERT INTO atelier_schema_versions VALUES(6);
            INSERT INTO durable_v6_witness VALUES(X'00FF');
            """
        )
    before_files = _file_snapshot(tmp_path)
    before_logical = _logical_dump(database_path)
    before_hash = hashlib.sha256(database_path.read_bytes()).hexdigest()
    engine = create_canonical_engine(database_path)

    with pytest.raises(MigrationRequired, match="schema version 6"):
        initialize_schema(engine)

    engine.dispose()
    assert _logical_dump(database_path) == before_logical
    assert _file_snapshot(tmp_path) == before_files
    assert hashlib.sha256(database_path.read_bytes()).hexdigest() == before_hash


def test_the_store_scopes_one_event_of_a_kind_to_one_execution_or_one_attempt(
    ledger_engine: Engine,
) -> None:
    """What the event log refuses to hold twice, and what it now admits again.

    An attempt-free event is keyed twice over: by the node execution it belongs
    to, and by the round of the node it stands in. One round of a node therefore
    holds one event of a kind however its execution id was derived, while the
    next round of that same node holds its own -- the sentence the V36 hop gave
    back when it re-scoped the once-per-node key to the round (#658). An
    attempt-bound event is keyed by its attempt instead, so the replacement
    attempt of one execution writes its own outcome beside the first.
    """

    with ledger_engine.begin() as connection:
        revision = str(
            connection.scalar(
                sa.select(runs.c.revision_hash).where(runs.c.run_id == "run-1")
            )
        )
        connection.execute(
            runs.insert().values(
                run_id="run-2",
                bootstrap_workflow_id="workflow-2",
                revision_hash=revision,
                workflow_format_version=1,
                agent_binding_set_hash=None,
                current_node_id="other",
                current_round_ordinal=FIRST_ROUND_ORDINAL,
                state="STARTED",
                state_version=0,
                last_event_sequence=0,
                terminal_hash=None,
            )
        )

    def insert_event(
        connection: Connection,
        *,
        run_id: str,
        sequence: int,
        node_id: str,
        execution_id: str,
        kind: str,
        round_ordinal: int = FIRST_ROUND_ORDINAL,
        attempt_id: str | None = None,
        ordinal: int | None = None,
    ) -> None:
        connection.exec_driver_sql(
            "INSERT INTO run_events("
            "run_id,revision_hash,event_sequence,node_id,node_execution_id,"
            "round_ordinal,event_kind,payload,payload_hash,receipt_logical_key,"
            "receipt_result_hash,event_hash,agent_attempt_id,attempt_ordinal,"
            "cancellation_command_id,replacement,cancellation_disposition,"
            "replacement_attempt_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                revision,
                sequence,
                node_id,
                execution_id,
                round_ordinal,
                kind,
                b"event",
                hashlib.sha256(b"event").hexdigest(),
                None,
                None,
                f"{sequence:x}" * 64,
                attempt_id,
                ordinal,
                None,
                None,
                None,
                None,
            ),
        )

    with ledger_engine.begin() as connection:
        insert_event(
            connection,
            run_id="run-1",
            sequence=1,
            node_id="agent",
            execution_id="1" * 64,
            kind="AGENT_COMPLETED",
        )
    with pytest.raises(IntegrityError), ledger_engine.begin() as connection:
        insert_event(
            connection,
            run_id="run-1",
            sequence=2,
            node_id="agent",
            execution_id="1" * 64,
            kind="AGENT_COMPLETED",
        )
    with pytest.raises(IntegrityError), ledger_engine.begin() as connection:
        insert_event(
            connection,
            run_id="run-1",
            sequence=2,
            node_id="agent",
            execution_id="2" * 64,
            kind="AGENT_COMPLETED",
        )
    with ledger_engine.begin() as connection:
        insert_event(
            connection,
            run_id="run-1",
            sequence=2,
            node_id="agent",
            execution_id="2" * 64,
            kind="AGENT_COMPLETED",
            round_ordinal=FIRST_ROUND_ORDINAL + 1,
        )
    with pytest.raises(IntegrityError), ledger_engine.begin() as connection:
        insert_event(
            connection,
            run_id="run-2",
            sequence=1,
            node_id="other",
            execution_id="1" * 64,
            kind="AGENT_COMPLETED",
        )

    with ledger_engine.begin() as connection:
        insert_event(
            connection,
            run_id="run-1",
            sequence=3,
            node_id="agent",
            execution_id="1" * 64,
            kind="AGENT_FAILED",
            attempt_id="a" * 64,
            ordinal=1,
        )
        insert_event(
            connection,
            run_id="run-1",
            sequence=4,
            node_id="agent",
            execution_id="1" * 64,
            kind="AGENT_FAILED",
            attempt_id="b" * 64,
            ordinal=2,
        )
    with pytest.raises(IntegrityError), ledger_engine.begin() as connection:
        insert_event(
            connection,
            run_id="run-1",
            sequence=5,
            node_id="agent",
            execution_id="1" * 64,
            kind="AGENT_FAILED",
            attempt_id="a" * 64,
            ordinal=1,
        )


@pytest.mark.parametrize(
    ("event_kind", "agent_receipt_hash", "admitted"),
    [
        pytest.param("AGENT_COMPLETED", "c" * 64, True, id="completion-with-binding"),
        pytest.param("AGENT_COMPLETED", None, True, id="completion-before-v3"),
        pytest.param("AGENT_COMPLETED", "c" * 63, False, id="not-a-digest"),
        pytest.param("AGENT_COMPLETED", "C" * 64, False, id="not-lowercase"),
        pytest.param("WAITING_INPUT", "c" * 64, False, id="wait-carrying-a-binding"),
        pytest.param(
            "SUBWORKFLOW_COMPLETED",
            "c" * 64,
            False,
            id="subworkflow-carrying-a-binding",
        ),
    ],
)
def test_the_store_admits_a_receipt_binding_only_on_an_agent_completion(
    ledger_engine: Engine,
    event_kind: str,
    agent_receipt_hash: str | None,
    admitted: bool,
) -> None:
    """The store's half of the rule `contracts/executions.py` states.

    Both halves land together on purpose: a contract that admits a field the
    store refuses is a rule only the integration run discovers.
    """

    def write() -> None:
        with ledger_engine.begin() as connection:
            revision = str(
                connection.scalar(
                    sa.select(runs.c.revision_hash).where(runs.c.run_id == "run-1")
                )
            )
            connection.execute(
                run_events.insert().values(
                    run_id="run-1",
                    revision_hash=revision,
                    event_sequence=1,
                    node_id="agent",
                    node_execution_id="1" * 64,
                    round_ordinal=FIRST_ROUND_ORDINAL,
                    event_kind=event_kind,
                    payload=b"event",
                    payload_hash=hashlib.sha256(b"event").hexdigest(),
                    event_hash="a" * 64,
                    agent_receipt_hash=agent_receipt_hash,
                )
            )

    if admitted:
        write()
        with ledger_engine.connect() as connection:
            assert (
                connection.scalar(sa.select(run_events.c.agent_receipt_hash))
                == agent_receipt_hash
            )
        return
    with pytest.raises(IntegrityError, match="agent_receipt_hash"):
        write()


@pytest.fixture
def ledger_engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_canonical_engine(tmp_path / "atelier.sqlite")
    initialize_schema(engine)
    revision_hash = hashlib.sha256(b"workflow-v1").hexdigest()
    with engine.begin() as connection:
        connection.execute(
            workflow_revisions.insert().values(
                revision_hash=revision_hash,
                document=b"workflow-v1",
            )
        )
        connection.execute(
            runs.insert().values(
                run_id="run-1",
                bootstrap_workflow_id="workflow-1",
                revision_hash=revision_hash,
                workflow_format_version=1,
                agent_binding_set_hash=None,
                current_node_id="agent",
                current_round_ordinal=FIRST_ROUND_ORDINAL,
                state="STARTED",
                state_version=0,
                last_event_sequence=0,
                terminal_hash=None,
            )
        )
    try:
        yield engine
    finally:
        engine.dispose()


def _add_intent(
    connection: Connection,
    *,
    logical_key: str = "effect-1",
    state: str = "PREPARED",
    state_version: int = 0,
    owner_command_id: str | None = None,
    canonical_request: bytes = b"request\x00\xff",
    overrides: Mapping[str, object] | None = None,
) -> None:
    values: dict[str, object] = {
        "logical_key": logical_key,
        "run_id": "run-1",
        "canonical_request": canonical_request,
        "request_hash": hashlib.sha256(canonical_request).hexdigest(),
        "workflow_revision_hash": hashlib.sha256(b"workflow-v1").hexdigest(),
        "adapter_revision": "adapter-1",
        "destination_identity": "destination-1",
        "adapter_operational_identity": "external-store-1",
        "operation_name": "open-pr",
        "state": state,
        "state_version": state_version,
        "reconciliation_owner_command_id": owner_command_id,
    }
    values.update(overrides or {})
    connection.execute(effect_intents.insert().values(values))


def _add_command(
    connection: Connection,
    *,
    command_id: str = "command-1",
    logical_key: str = "effect-1",
    determination: str = "AUTHORITATIVE_NOT_FOUND",
    found_effect_id: str | None = None,
    found_result: bytes | None = None,
    found_result_hash: str | None = None,
    state: str = "PENDING",
    overrides: Mapping[str, object] | None = None,
) -> None:
    values: dict[str, object] = {
        "command_id": command_id,
        "logical_key": logical_key,
        "expected_intent_version": 1,
        "determination": determination,
        "actor": "operator",
        "evidence": "authoritative adapter readback",
        "found_effect_id": found_effect_id,
        "found_result": found_result,
        "found_result_hash": found_result_hash,
        "state": state,
    }
    values.update(overrides or {})
    connection.execute(reconcile_commands.insert().values(values))


def _add_receipt(
    connection: Connection,
    *,
    logical_key: str = "effect-1",
    confirmation_source: str = "OPERATOR_AUTHORIZED_EXECUTION",
    command_id: str | None = "command-1",
    result: bytes = b"result\x00\xff",
    overrides: Mapping[str, object] | None = None,
) -> None:
    canonical_request = b"request\x00\xff"
    values: dict[str, object] = {
        "logical_key": logical_key,
        "run_id": "run-1",
        "canonical_request": canonical_request,
        "request_hash": hashlib.sha256(canonical_request).hexdigest(),
        "workflow_revision_hash": hashlib.sha256(b"workflow-v1").hexdigest(),
        "adapter_revision": "adapter-1",
        "destination_identity": "destination-1",
        "adapter_operational_identity": "external-store-1",
        "operation_name": "open-pr",
        "effect_id": "external-effect-1",
        "result": result,
        "result_hash": hashlib.sha256(result).hexdigest(),
        "confirmation_source": confirmation_source,
        "reconcile_command_id": command_id,
    }
    values.update(overrides or {})
    connection.execute(effect_receipts.insert().values(values))


@pytest.mark.parametrize(
    ("record", "field", "invalid_value"),
    [
        pytest.param("intent", "logical_key", "", id="intent-logical-key"),
        pytest.param("intent", "run_id", "", id="intent-run-id"),
        pytest.param("intent", "request_hash", "", id="intent-request-hash"),
        pytest.param(
            "intent",
            "workflow_revision_hash",
            "",
            id="intent-workflow-revision-hash",
        ),
        pytest.param("intent", "adapter_revision", "", id="intent-adapter-revision"),
        pytest.param(
            "intent",
            "destination_identity",
            "",
            id="intent-destination-identity",
        ),
        pytest.param(
            "intent",
            "adapter_operational_identity",
            "",
            id="intent-adapter-operational-identity",
        ),
        pytest.param("intent", "operation_name", "corrupt", id="intent-operation-name"),
        pytest.param(
            "intent",
            "reconciliation_owner_command_id",
            "",
            id="intent-reconciliation-owner-command-id",
        ),
        pytest.param("intent", "state_version", -1, id="intent-state-version"),
        pytest.param("command", "command_id", "", id="command-id"),
        pytest.param("command", "logical_key", "", id="command-logical-key"),
        pytest.param("command", "actor", "", id="command-actor"),
        pytest.param("command", "evidence", "", id="command-evidence"),
        pytest.param("command", "found_effect_id", "", id="command-found-effect-id"),
        pytest.param(
            "command", "found_result_hash", "", id="command-found-result-hash"
        ),
        pytest.param(
            "command",
            "expected_intent_version",
            -1,
            id="command-expected-intent-version",
        ),
        pytest.param("receipt", "logical_key", "", id="receipt-logical-key"),
        pytest.param("receipt", "run_id", "", id="receipt-run-id"),
        pytest.param("receipt", "request_hash", "", id="receipt-request-hash"),
        pytest.param(
            "receipt",
            "workflow_revision_hash",
            "",
            id="receipt-workflow-revision-hash",
        ),
        pytest.param("receipt", "adapter_revision", "", id="receipt-adapter-revision"),
        pytest.param(
            "receipt",
            "destination_identity",
            "",
            id="receipt-destination-identity",
        ),
        pytest.param(
            "receipt",
            "adapter_operational_identity",
            "",
            id="receipt-adapter-operational-identity",
        ),
        pytest.param(
            "receipt", "operation_name", "corrupt", id="receipt-operation-name"
        ),
        pytest.param("receipt", "effect_id", "", id="receipt-effect-id"),
        pytest.param("receipt", "result_hash", "", id="receipt-result-hash"),
        pytest.param(
            "receipt",
            "reconcile_command_id",
            "",
            id="receipt-reconcile-command-id",
        ),
    ],
)
def test_effect_ledger_rejects_invalid_field_shapes(
    ledger_engine: Engine,
    record: str,
    field: str,
    invalid_value: object,
) -> None:
    with (
        pytest.raises(exc.IntegrityError) as caught,
        ledger_engine.begin() as connection,
    ):
        if record == "intent":
            if field == "reconciliation_owner_command_id":
                _add_intent(connection, state="WAITING_RECONCILIATION")
                _add_command(connection)
                connection.execute(
                    effect_intents.update().values(
                        state="RECONCILING",
                        state_version=1,
                        reconciliation_owner_command_id=invalid_value,
                    )
                )
            else:
                _add_intent(connection, overrides={field: invalid_value})
        elif record == "command":
            _add_intent(connection)
            if field in {"found_effect_id", "found_result_hash"}:
                result = b"result\x00\xff"
                _add_command(
                    connection,
                    determination="FOUND",
                    found_effect_id="external-effect-1",
                    found_result=result,
                    found_result_hash=hashlib.sha256(result).hexdigest(),
                    overrides={field: invalid_value},
                )
            else:
                _add_command(connection, overrides={field: invalid_value})
        else:
            _add_intent(connection)
            _add_command(connection)
            _add_receipt(connection, overrides={field: invalid_value})

    assert "CHECK constraint failed" in str(caught.value.orig)


@pytest.mark.parametrize(
    ("state", "accepted"),
    [
        ("STARTED", True),
        ("WAITING_RECONCILIATION", True),
        ("WAITING_INPUT", True),
        ("COMPLETED", True),
        ("FAILED", True),
        ("CANCELLED", True),
        ("RECONCILING", False),
        ("PREPARED", False),
        ("CONFIRMED", False),
        ("", False),
    ],
)
def test_run_state_tokens_are_exact(
    ledger_engine: Engine, state: str, accepted: bool
) -> None:
    terminal_states = {"COMPLETED", "FAILED", "CANCELLED"}
    statement = runs.insert().values(
        run_id="candidate-run",
        bootstrap_workflow_id="candidate-workflow",
        revision_hash=hashlib.sha256(b"workflow-v1").hexdigest(),
        workflow_format_version=1,
        agent_binding_set_hash=None,
        current_node_id=("final" if state in terminal_states else "node"),
        current_round_ordinal=FIRST_ROUND_ORDINAL,
        state=state,
        state_version=0,
        last_event_sequence=0,
        terminal_hash=("0" * 64 if state in terminal_states else None),
    )
    if accepted:
        with ledger_engine.begin() as connection:
            connection.execute(statement)
    else:
        with pytest.raises(exc.IntegrityError), ledger_engine.begin() as connection:
            connection.execute(statement)


def _is_run_state_cancelled(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "CANCELLED"
        and isinstance(node.value, ast.Name)
        and node.value.id == "RunState"
    )


class _CancelledStateConstructors(ast.NodeVisitor):
    """The name of every function that constructs `RunState.CANCELLED`.

    An `is`/`==` comparison, or `in`/`not in` a literal collection of states,
    is a reader guarding against the word, never a writer, so a `Compare`
    visits its own operands first and marks them -- `visit_Attribute` then
    skips exactly those nodes, by identity, wherever the traversal reaches
    them next. A collection operand of `in`/`not in` is unwrapped one level,
    the shape `state in {RunState.CANCELLED, RunState.FAILED}` already reads
    elsewhere in this tree, so the word inside it marks the same way as a
    bare operand rather than surfacing as a false third producer. Reading
    source as its parsed tree rather than as text is what keeps a docstring's
    own prose about the word (this module's, or `uncontinuable_runs.py`'s)
    from ever being mistaken for code that constructs it: a string constant
    holds no `Attribute` node at all.
    """

    def __init__(self) -> None:
        self.owners: set[str] = set()
        self._function_stack: list[str] = []
        self._reader_nodes: set[int] = set()

    def visit_Compare(self, node: ast.Compare) -> None:
        operands = (node.left, *node.comparators)
        if any(isinstance(op, ast.Is | ast.Eq) for op in node.ops):
            for operand in operands:
                self._mark_reader(operand)
        if any(isinstance(op, ast.In | ast.NotIn) for op in node.ops):
            for operand in operands:
                self._mark_reader(operand)
                if isinstance(operand, ast.Set | ast.List | ast.Tuple):
                    for element in operand.elts:
                        self._mark_reader(element)
        self.generic_visit(node)

    def _mark_reader(self, operand: ast.expr) -> None:
        if _is_run_state_cancelled(operand):
            self._reader_nodes.add(id(operand))

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._function_stack.append(node.name)
        self.generic_visit(node)
        self._function_stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if _is_run_state_cancelled(node) and id(node) not in self._reader_nodes:
            self.owners.add(
                self._function_stack[-1] if self._function_stack else "<module>"
            )
        self.generic_visit(node)


_CANCELLED_STATE_PRODUCERS: dict[str, str] = {
    "adapters/dbos/agent_attempt_store.py::_lift_run_under_operator_cancel": (
        "the in-transaction lift `attest_cancellation_cleanup` and "
        "`_commit_runner_cancellation` both close through, on either carrier"
    ),
    "adapters/dbos/uncontinuable_runs.py::_attempt_family_target_state": (
        "the serve-start net's own answer for an operator-cancelled or "
        "-interrupted current-node attempt with no replacement in flight"
    ),
    "adapters/dbos/run_transitions.py::commit_wait_cancelled": (
        "a run resting at a pause, ended by the operator's own cancel command "
        "under the WAIT_CANCELLED attestation it writes itself (#668)"
    ),
}
"""The named producers of `RunState.CANCELLED`, so a new one is a review
question in the diff below, never a silent grep exception."""


def test_run_state_cancelled_has_exactly_its_named_producers() -> None:
    """#439 P1 gave CANCELLED its durable home; every writer of it is named.

    `test_run_state_tokens_are_exact` above proves the migrated CHECK admits
    the word at the storage layer. This is the other half: every construction
    of `RunState.CANCELLED` outside `contracts/` (its own definitional home,
    `RunState` itself and the `TERMINAL_RUN_STATES` set every terminal-state
    check already reads through) is one of the named producers above -- the
    run's own terminal lift, on either carrier, under the operator's own cancel
    command identity; the serve-start net's answer for the same identity left
    leaderless; and the resting pause that ends under its own attestation
    because it has no attempt to lift (#668). An `is`/`==` comparison anywhere
    else, the way `api/projection/runs.py` already refuses to render a V1/V2
    run whose frozen wire predates the word, is still a reader, not a producer.
    """

    source_root = Path(__file__).resolve().parents[2] / "src" / "atelier2"
    found: set[str] = set()
    for module in sorted(source_root.rglob("*.py")):
        relative = module.relative_to(source_root)
        if "contracts" in relative.parts:
            continue
        constructors = _CancelledStateConstructors()
        constructors.visit(
            ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
        )
        for owner in constructors.owners:
            found.add(f"{relative.as_posix()}::{owner}")
    assert found == set(_CANCELLED_STATE_PRODUCERS)


@pytest.mark.parametrize(
    ("state", "accepted"),
    [
        ("PREPARED", True),
        ("WAITING_RECONCILIATION", True),
        ("RECONCILING", True),
        ("CONFIRMED", True),
        ("STARTED", False),
        ("UNKNOWN_OUTCOME", False),
        ("", False),
    ],
)
def test_intent_state_tokens_are_exact(
    ledger_engine: Engine, state: str, accepted: bool
) -> None:
    with ledger_engine.begin() as connection:
        _add_intent(connection)
        if state == "RECONCILING":
            _add_command(connection)
    if state == "PREPARED":
        return

    statement = effect_intents.update().values(
        state=state,
        reconciliation_owner_command_id=(
            "command-1" if state == "RECONCILING" else None
        ),
    )
    if accepted:
        with ledger_engine.begin() as connection:
            connection.execute(statement)
    else:
        with pytest.raises(exc.IntegrityError), ledger_engine.begin() as connection:
            connection.execute(statement)


@pytest.mark.parametrize(
    ("state", "accepted"),
    [
        ("PENDING", True),
        ("APPLIED", True),
        ("REJECTED_CONFLICT", True),
        ("COMPLETED", False),
        ("", False),
    ],
)
def test_reconcile_command_state_tokens_are_exact(
    ledger_engine: Engine, state: str, accepted: bool
) -> None:
    with ledger_engine.begin() as connection:
        _add_intent(connection)
    if accepted:
        with ledger_engine.begin() as connection:
            _add_command(connection, state=state)
    else:
        with pytest.raises(exc.IntegrityError), ledger_engine.begin() as connection:
            _add_command(connection, state=state)


@pytest.mark.parametrize(
    (
        "determination",
        "found_effect_id",
        "found_result",
        "found_result_hash",
        "accepted",
    ),
    [
        ("FOUND", "external-1", b"", hashlib.sha256(b"").hexdigest(), True),
        ("FOUND", None, b"", hashlib.sha256(b"").hexdigest(), False),
        ("FOUND", "external-1", None, hashlib.sha256(b"").hexdigest(), False),
        ("FOUND", "external-1", b"", None, False),
        ("AUTHORITATIVE_NOT_FOUND", None, None, None, True),
        ("AUTHORITATIVE_NOT_FOUND", "external-1", None, None, False),
        ("AUTHORITATIVE_NOT_FOUND", None, b"", None, False),
        ("AUTHORITATIVE_NOT_FOUND", None, None, "hash", False),
        ("UNKNOWN", None, None, None, False),
    ],
)
def test_reconcile_determination_owns_the_found_payload_shape(
    ledger_engine: Engine,
    determination: str,
    found_effect_id: str | None,
    found_result: bytes | None,
    found_result_hash: str | None,
    accepted: bool,
) -> None:
    with ledger_engine.begin() as connection:
        _add_intent(connection)

    if accepted:
        with ledger_engine.begin() as connection:
            _add_command(
                connection,
                determination=determination,
                found_effect_id=found_effect_id,
                found_result=found_result,
                found_result_hash=found_result_hash,
            )
    else:
        with pytest.raises(exc.IntegrityError), ledger_engine.begin() as connection:
            _add_command(
                connection,
                determination=determination,
                found_effect_id=found_effect_id,
                found_result=found_result,
                found_result_hash=found_result_hash,
            )


@pytest.mark.parametrize(
    ("source", "command_id", "accepted"),
    [
        ("ADAPTER_READBACK", None, True),
        ("ADAPTER_EXECUTION", None, True),
        ("OPERATOR_FOUND", "command-1", True),
        ("OPERATOR_AUTHORIZED_EXECUTION", "command-1", True),
        ("ADAPTER_READBACK", "command-1", False),
        ("OPERATOR_FOUND", None, False),
        ("UNKNOWN", None, False),
    ],
)
def test_confirmation_source_owns_command_provenance(
    ledger_engine: Engine, source: str, command_id: str | None, accepted: bool
) -> None:
    with ledger_engine.begin() as connection:
        _add_intent(connection)
        if source == "OPERATOR_FOUND":
            result = b"result\x00\xff"
            _add_command(
                connection,
                determination="FOUND",
                found_effect_id="external-effect-1",
                found_result=result,
                found_result_hash=hashlib.sha256(result).hexdigest(),
            )
        else:
            _add_command(connection)

    if accepted:
        with ledger_engine.begin() as connection:
            _add_receipt(
                connection,
                confirmation_source=source,
                command_id=command_id,
            )
    else:
        with pytest.raises(exc.IntegrityError), ledger_engine.begin() as connection:
            _add_receipt(
                connection,
                confirmation_source=source,
                command_id=command_id,
            )


def test_receipt_round_trips_full_provenance_once_per_logical_key(
    ledger_engine: Engine,
) -> None:
    canonical_request = b"request\x00\xff"
    result = b"result\x00\xff"
    with ledger_engine.begin() as connection:
        _add_intent(connection, canonical_request=canonical_request)
        _add_command(connection)
        _add_receipt(connection, result=result)

    with ledger_engine.connect() as connection:
        assert connection.execute(sa.select(effect_receipts)).one() == (
            "effect-1",
            "run-1",
            canonical_request,
            hashlib.sha256(canonical_request).hexdigest(),
            hashlib.sha256(b"workflow-v1").hexdigest(),
            "adapter-1",
            "destination-1",
            "external-store-1",
            "open-pr",
            "external-effect-1",
            result,
            hashlib.sha256(result).hexdigest(),
            "OPERATOR_AUTHORIZED_EXECUTION",
            "command-1",
            None,
            None,
            None,
            None,
        )

    with pytest.raises(exc.IntegrityError), ledger_engine.begin() as connection:
        _add_receipt(connection, result=b"different")


def test_fork_reference_receipt_requires_one_existing_composite_source(
    ledger_engine: Engine,
) -> None:
    source_result = b"source-result"
    with ledger_engine.begin() as connection:
        _add_intent(connection, logical_key="effect-source")
        _add_receipt(
            connection,
            logical_key="effect-source",
            confirmation_source="ADAPTER_EXECUTION",
            command_id=None,
            result=source_result,
        )
        _add_intent(connection, logical_key="effect-successor")

    source = {
        "fork_source_logical_key": "effect-source",
        "fork_source_run_id": "run-1",
        "fork_source_workflow_revision_hash": hashlib.sha256(
            b"workflow-v1"
        ).hexdigest(),
        "fork_source_result_hash": hashlib.sha256(source_result).hexdigest(),
    }
    with pytest.raises(exc.IntegrityError), ledger_engine.begin() as connection:
        _add_receipt(
            connection,
            logical_key="effect-successor",
            confirmation_source="FORK_REFERENCE",
            command_id=None,
            result=source_result,
            overrides=source | {"fork_source_logical_key": "missing-source"},
        )
    with pytest.raises(exc.IntegrityError), ledger_engine.begin() as connection:
        _add_receipt(
            connection,
            logical_key="effect-successor",
            confirmation_source="FORK_REFERENCE",
            command_id=None,
            result=b"different-result",
            overrides=source,
        )

    with ledger_engine.begin() as connection:
        _add_receipt(
            connection,
            logical_key="effect-successor",
            confirmation_source="FORK_REFERENCE",
            command_id=None,
            result=source_result,
            overrides=source,
        )
    with ledger_engine.connect() as connection:
        assert (
            connection.scalar(
                sa.select(effect_receipts.c.confirmation_source).where(
                    effect_receipts.c.logical_key == "effect-successor"
                )
            )
            == "FORK_REFERENCE"
        )


@pytest.mark.parametrize(
    ("column", "replacement"),
    [
        pytest.param("logical_key", "effect-2", id="logical-key"),
        pytest.param("run_id", "run-2", id="run-id"),
        pytest.param("canonical_request", b"changed", id="canonical-request"),
        pytest.param("request_hash", "changed-hash", id="request-hash"),
        pytest.param(
            "workflow_revision_hash",
            hashlib.sha256(b"workflow-v2").hexdigest(),
            id="workflow-revision-hash",
        ),
        pytest.param("adapter_revision", "adapter-2", id="adapter-revision"),
        pytest.param(
            "destination_identity", "destination-2", id="destination-identity"
        ),
        pytest.param(
            "adapter_operational_identity",
            "external-store-2",
            id="adapter-operational-identity",
        ),
    ],
)
def test_every_effect_intent_binding_column_is_immutable(
    ledger_engine: Engine,
    column: str,
    replacement: object,
) -> None:
    alternate_document = b"workflow-v2"
    alternate_revision_hash = hashlib.sha256(alternate_document).hexdigest()
    with ledger_engine.begin() as connection:
        connection.execute(
            workflow_revisions.insert().values(
                revision_hash=alternate_revision_hash,
                document=alternate_document,
            )
        )
        connection.execute(
            runs.insert().values(
                run_id="run-2",
                bootstrap_workflow_id="workflow-2",
                revision_hash=alternate_revision_hash,
                workflow_format_version=1,
                agent_binding_set_hash=None,
                current_node_id="agent",
                current_round_ordinal=FIRST_ROUND_ORDINAL,
                state="STARTED",
                state_version=0,
                last_event_sequence=0,
                terminal_hash=None,
            )
        )
        _add_intent(connection)

    with (
        pytest.raises(exc.IntegrityError, match="effect intent bindings are immutable"),
        ledger_engine.begin() as connection,
    ):
        connection.execute(effect_intents.update().values({column: replacement}))


@pytest.mark.parametrize(
    ("column", "replacement"),
    [
        pytest.param("command_id", "command-2", id="command-id"),
        pytest.param("logical_key", "effect-2", id="logical-key"),
        pytest.param("expected_intent_version", 2, id="expected-intent-version"),
        pytest.param("determination", "FOUND", id="determination-update-attempt"),
        pytest.param("actor", "reviewer", id="actor"),
        pytest.param("evidence", "changed evidence", id="evidence"),
        pytest.param("found_effect_id", "external-effect-2", id="found-effect-id"),
        pytest.param("found_result", b"changed", id="found-result"),
        pytest.param("found_result_hash", "changed-hash", id="found-result-hash"),
    ],
)
def test_every_reconcile_command_identity_and_payload_column_is_immutable(
    ledger_engine: Engine,
    column: str,
    replacement: object,
) -> None:
    result = b"result\x00\xff"
    with ledger_engine.begin() as connection:
        _add_intent(connection)
        _add_intent(connection, logical_key="effect-2")
        _add_command(
            connection,
            determination="FOUND",
            found_effect_id="external-effect-1",
            found_result=result,
            found_result_hash=hashlib.sha256(result).hexdigest(),
        )

    with (
        pytest.raises(
            exc.IntegrityError, match="reconcile command payloads are immutable"
        ),
        ledger_engine.begin() as connection,
    ):
        connection.execute(reconcile_commands.update().values({column: replacement}))


@pytest.mark.parametrize(
    "operation",
    ["delete-intent", "change-receipt", "delete-receipt", "delete-command"],
)
def test_effect_evidence_rows_are_immutable(
    ledger_engine: Engine, operation: str
) -> None:
    with ledger_engine.begin() as connection:
        _add_intent(connection)
        _add_command(connection)
        _add_receipt(connection)

    statements = {
        "delete-intent": effect_intents.delete(),
        "change-receipt": effect_receipts.update().values(result=b"changed"),
        "delete-receipt": effect_receipts.delete(),
        "delete-command": reconcile_commands.delete(),
    }
    with pytest.raises(exc.IntegrityError), ledger_engine.begin() as connection:
        connection.execute(statements[operation])


def test_effect_and_command_lifecycle_fields_remain_mutable(
    ledger_engine: Engine,
) -> None:
    with ledger_engine.begin() as connection:
        _add_intent(connection)
        connection.execute(
            effect_intents.update().values(
                state="WAITING_RECONCILIATION",
                state_version=1,
            )
        )
        _add_command(connection)
        connection.execute(
            effect_intents.update().values(
                state="RECONCILING",
                state_version=2,
                reconciliation_owner_command_id="command-1",
            )
        )
        connection.execute(reconcile_commands.update().values(state="APPLIED"))
        connection.execute(
            effect_intents.update().values(
                state="CONFIRMED",
                state_version=3,
                reconciliation_owner_command_id=None,
            )
        )

    with ledger_engine.connect() as connection:
        assert connection.execute(
            sa.select(effect_intents.c.state, effect_intents.c.state_version)
        ).one() == ("CONFIRMED", 3)
        assert connection.scalar(sa.select(reconcile_commands.c.state)) == "APPLIED"


def test_reconciliation_owner_must_reference_an_existing_command(
    ledger_engine: Engine,
) -> None:
    with ledger_engine.begin() as connection:
        _add_intent(connection, state="WAITING_RECONCILIATION")

    with pytest.raises(exc.IntegrityError), ledger_engine.begin() as connection:
        connection.execute(
            effect_intents.update()
            .where(effect_intents.c.logical_key == "effect-1")
            .values(
                state="RECONCILING",
                state_version=1,
                reconciliation_owner_command_id="missing-command",
            )
        )

    with ledger_engine.begin() as connection:
        _add_command(connection)
        connection.execute(
            effect_intents.update()
            .where(effect_intents.c.logical_key == "effect-1")
            .values(
                state="RECONCILING",
                state_version=1,
                reconciliation_owner_command_id="command-1",
            )
        )

    with ledger_engine.connect() as connection:
        assert connection.execute(
            sa.select(
                effect_intents.c.state,
                effect_intents.c.reconciliation_owner_command_id,
            )
        ).one() == ("RECONCILING", "command-1")
