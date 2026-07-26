import base64
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

import wecom_reader.ledger as ledger_module
from wecom_reader.ledger import AssetLedger


def row_count(db_path, table):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def test_initializes_required_schema(tmp_path):
    db_path = tmp_path / "ledger.db"

    AssetLedger(db_path)

    conn = sqlite3.connect(db_path)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    finally:
        conn.close()

    assert {
        "ingestion_batches",
        "messages",
        "message_versions",
        "message_observations",
        "quarantine_errors",
        "checkpoints",
    } <= tables
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    finally:
        conn.close()


def test_completed_batch_is_idempotent(tmp_path):
    ledger = AssetLedger(tmp_path / "ledger.db")
    records = [
        {
            "source_table": "message_table",
            "message_id": "m1",
            "server_id": None,
            "sequence": 1,
            "content_type": 2,
            "content": "hello",
        }
    ]

    first = ledger.ingest_records("acct", "message.db", "batch-1", records)
    second = ledger.ingest_records("acct", "message.db", "batch-1", records)

    assert first.completed is True
    assert second.completed is True
    assert second.already_completed is True
    assert row_count(ledger.db_path, "messages") == 1
    assert row_count(ledger.db_path, "message_versions") == 1
    assert row_count(ledger.db_path, "message_observations") == 1
    assert row_count(ledger.db_path, "quarantine_errors") == 0


def test_same_payload_new_batch_adds_observation_not_version(tmp_path):
    ledger = AssetLedger(tmp_path / "ledger.db")
    record = {
        "source_table": "message_table",
        "source_rowid": 10,
        "message_id": "m1",
        "server_id": None,
        "sequence": 1,
        "content_type": 2,
        "content": "hello",
    }

    ledger.ingest_records("acct", "message.db", "batch-1", [record])
    result = ledger.ingest_records("acct", "message.db", "batch-2", [record])

    assert result.messages_inserted == 0
    assert result.versions_inserted == 0
    assert result.observations_inserted == 1
    assert row_count(ledger.db_path, "messages") == 1
    assert row_count(ledger.db_path, "message_versions") == 1
    assert row_count(ledger.db_path, "message_observations") == 2


def test_concurrent_batches_serialize_without_losing_observations(tmp_path):
    ledger = AssetLedger(tmp_path / "ledger.db")
    record = {
        "source_table": "message_table",
        "source_rowid": 10,
        "message_id": "m1",
        "server_id": None,
        "sequence": 1,
        "content_type": 2,
        "content": "hello",
    }

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda batch_key: ledger.ingest_records(
                    "acct", "message.db", batch_key, [record]
                ),
                ("batch-1", "batch-2"),
            )
        )

    assert all(result.completed for result in results)
    assert sum(result.messages_inserted for result in results) == 1
    assert sum(result.versions_inserted for result in results) == 1
    assert sum(result.observations_inserted for result in results) == 2
    assert row_count(ledger.db_path, "messages") == 1
    assert row_count(ledger.db_path, "message_versions") == 1
    assert row_count(ledger.db_path, "message_observations") == 2


def test_source_name_is_batch_provenance_not_message_identity(tmp_path):
    ledger = AssetLedger(tmp_path / "ledger.db")
    record = {
        "source_table": "message_table",
        "message_id": "m1",
        "server_id": "s1",
        "sequence": 1,
        "content_type": 2,
        "content": "hello",
    }

    ledger.ingest_records("acct", "snapshot-a", "batch-a", [record])
    second = ledger.ingest_records("acct", "snapshot-b", "batch-b", [record])

    assert second.messages_inserted == 0
    assert row_count(ledger.db_path, "messages") == 1
    assert row_count(ledger.db_path, "message_observations") == 2
    conn = sqlite3.connect(ledger.db_path)
    try:
        sources = {
            row[0]
            for row in conn.execute(
                """
                SELECT ingestion_batches.source_name
                FROM message_observations
                JOIN ingestion_batches
                    ON ingestion_batches.id = message_observations.batch_id
                """
            )
        }
    finally:
        conn.close()
    assert sources == {"snapshot-a", "snapshot-b"}


def test_source_rowid_change_does_not_create_a_message_version(tmp_path):
    ledger = AssetLedger(tmp_path / "ledger.db")
    record = {
        "source_table": "message_table",
        "source_rowid": 10,
        "message_id": "m1",
        "server_id": None,
        "sequence": 1,
        "content_type": 2,
        "content": "hello",
    }

    ledger.ingest_records("acct", "message.db", "batch-1", [record])
    moved = dict(record, source_rowid=99)
    result = ledger.ingest_records("acct", "message.db", "batch-2", [moved])

    assert result.versions_inserted == 0
    assert result.observations_inserted == 1
    assert row_count(ledger.db_path, "message_versions") == 1
    assert row_count(ledger.db_path, "message_observations") == 2


def test_content_edit_adds_new_version_without_new_message(tmp_path):
    ledger = AssetLedger(tmp_path / "ledger.db")
    base = {
        "source_table": "message_table",
        "message_id": "m1",
        "server_id": None,
        "sequence": 1,
        "content_type": 2,
        "content": "hello",
    }

    ledger.ingest_records("acct", "message.db", "batch-1", [base])
    edited = dict(base, content="hello edited")
    result = ledger.ingest_records("acct", "message.db", "batch-2", [edited])

    assert result.messages_inserted == 0
    assert result.versions_inserted == 1
    assert result.observations_inserted == 1
    assert row_count(ledger.db_path, "messages") == 1
    assert row_count(ledger.db_path, "message_versions") == 2
    assert row_count(ledger.db_path, "message_observations") == 2
    conn = sqlite3.connect(ledger.db_path)
    try:
        current = conn.execute(
            """
            SELECT message_versions.raw_json
            FROM messages
            JOIN message_versions
                ON message_versions.id = messages.latest_version_id
            """
        ).fetchone()[0]
    finally:
        conn.close()
    assert "hello edited" in current


def test_unknown_content_type_is_preserved_and_marked_unsupported(tmp_path):
    ledger = AssetLedger(tmp_path / "ledger.db")
    record = {
        "source_table": "message_table",
        "message_id": "m1",
        "server_id": None,
        "sequence": 1,
        "content_type": 999999,
        "content": b"\x00raw payload",
        "extra": {"kept": True},
    }

    result = ledger.ingest_records("acct", "message.db", "batch-1", [record])

    assert result.versions_inserted == 1
    conn = sqlite3.connect(ledger.db_path)
    conn.row_factory = sqlite3.Row
    try:
        version = conn.execute("SELECT * FROM message_versions").fetchone()
    finally:
        conn.close()
    assert version["unsupported"] == 1
    assert version["type_name"] == "unsupported"
    raw_record = json.loads(version["raw_json"])
    assert base64.b64decode(raw_record["content"]["base64"]) == b"\x00raw payload"
    assert raw_record["extra"] == {"kept": True}


def test_missing_identity_is_quarantined(tmp_path):
    ledger = AssetLedger(tmp_path / "ledger.db")
    result = ledger.ingest_records(
        "acct",
        "message.db",
        "batch-1",
        [{"source_table": "message_table", "content_type": 2, "content": "bad"}],
    )

    assert result.records_seen == 1
    assert result.messages_inserted == 0
    assert result.versions_inserted == 0
    assert result.quarantined == 1
    assert row_count(ledger.db_path, "quarantine_errors") == 1


def test_failed_record_rolls_back_partial_asset_before_quarantine(
    tmp_path, monkeypatch
):
    ledger = AssetLedger(tmp_path / "ledger.db")

    def fail_parse(_content):
        raise ValueError("cannot parse")

    monkeypatch.setattr("wecom_reader.ledger._parse_content", fail_parse)
    result = ledger.ingest_records(
        "acct",
        "message.db",
        "batch-1",
        [
            {
                "source_table": "message_table",
                "message_id": "m1",
                "server_id": None,
                "sequence": 1,
                "content_type": 2,
                "content": "bad",
            }
        ],
    )

    assert result.messages_inserted == 0
    assert result.versions_inserted == 0
    assert result.observations_inserted == 0
    assert result.quarantined == 1
    assert row_count(ledger.db_path, "messages") == 0
    assert row_count(ledger.db_path, "message_versions") == 0
    assert row_count(ledger.db_path, "message_observations") == 0
    assert row_count(ledger.db_path, "quarantine_errors") == 1


def test_duplicate_source_rowids_each_receive_an_observation(tmp_path):
    ledger = AssetLedger(tmp_path / "ledger.db")
    records = [
        {
            "source_table": "message_table",
            "source_rowid": 1,
            "message_id": f"m{index}",
            "server_id": None,
            "sequence": index,
            "content_type": 2,
            "content": "ok",
        }
        for index in (1, 2)
    ]

    result = ledger.ingest_records("acct", "message.db", "batch-1", records)

    assert result.records_seen == 2
    assert result.observations_inserted == 2
    assert result.quarantined == 0
    assert row_count(ledger.db_path, "message_observations") == 2


def test_mixed_valid_and_bad_records_complete_batch_and_checkpoint(tmp_path):
    ledger = AssetLedger(tmp_path / "ledger.db")

    result = ledger.ingest_records(
        "acct",
        "message.db",
        "batch-1",
        [
            {
                "source_table": "message_table",
                "message_id": "m1",
                "server_id": None,
                "sequence": 1,
                "content_type": 2,
                "content": "ok",
            },
            {"source_table": "message_table", "content_type": 2, "content": "bad"},
        ],
        checkpoint={"offset": 2},
    )

    assert result.completed is True
    assert result.messages_inserted == 1
    assert result.versions_inserted == 1
    assert result.observations_inserted == 1
    assert result.quarantined == 1
    assert ledger.get_checkpoint("acct", "message.db") == {"offset": 2}


def test_checkpoint_does_not_advance_when_batch_transaction_fails(tmp_path):
    ledger = AssetLedger(tmp_path / "ledger.db")

    def broken_records():
        yield {
            "source_table": "message_table",
            "message_id": "m1",
            "server_id": None,
            "sequence": 1,
            "content_type": 2,
            "content": "ok",
        }
        raise RuntimeError("source cursor failed")

    with pytest.raises(RuntimeError, match="source cursor failed"):
        ledger.ingest_records(
            "acct", "message.db", "batch-1", broken_records(), checkpoint={"offset": 1}
        )

    assert ledger.get_checkpoint("acct", "message.db") is None
    assert row_count(ledger.db_path, "messages") == 0
    assert row_count(ledger.db_path, "message_versions") == 0
    assert row_count(ledger.db_path, "message_observations") == 0
    assert row_count(ledger.db_path, "ingestion_batches") == 1
    conn = sqlite3.connect(ledger.db_path)
    try:
        failed = conn.execute(
            "SELECT status, records_seen, error_message FROM ingestion_batches"
        ).fetchone()
    finally:
        conn.close()
    assert failed[0] == "failed"
    assert failed[1] == 1
    assert failed[2] == "source cursor failed"


def test_failed_batch_can_retry_with_same_key(tmp_path):
    ledger = AssetLedger(tmp_path / "ledger.db")

    def broken_records():
        raise RuntimeError("temporary source failure")
        yield

    with pytest.raises(RuntimeError, match="temporary source failure"):
        ledger.ingest_records("acct", "message.db", "batch-1", broken_records())

    result = ledger.ingest_records(
        "acct",
        "message.db",
        "batch-1",
        [
            {
                "source_table": "message_table",
                "message_id": "m1",
                "server_id": None,
                "sequence": 1,
                "content_type": 2,
                "content": "recovered",
            }
        ],
        checkpoint={"offset": 1},
    )

    assert result.completed is True
    assert result.records_seen == 1
    assert result.observations_inserted == 1
    assert row_count(ledger.db_path, "ingestion_batches") == 1
    assert ledger.get_checkpoint("acct", "message.db") == {"offset": 1}


def test_hash_collision_fails_batch_without_advancing_checkpoint(tmp_path, monkeypatch):
    ledger = AssetLedger(tmp_path / "ledger.db")
    base = {
        "source_table": "message_table",
        "server_id": "server",
        "sequence": 1,
        "content_type": 2,
        "content": "first",
    }
    ledger.ingest_records(
        "acct",
        "message.db",
        "batch-1",
        [dict(base, message_id="m1")],
        checkpoint={"offset": 1},
    )
    monkeypatch.setattr(ledger_module, "_sha256", lambda _value: "forced-collision")

    ledger.ingest_records(
        "other-account",
        "message.db",
        "seed-collision",
        [dict(base, message_id="seed")],
    )
    with pytest.raises(ledger_module.LedgerIntegrityError, match="collision"):
        ledger.ingest_records(
            "other-account",
            "message.db",
            "batch-2",
            [dict(base, message_id="different")],
            checkpoint={"offset": 2},
        )

    assert ledger.get_checkpoint("acct", "message.db") == {"offset": 1}
    assert ledger.get_checkpoint("other-account", "message.db") is None
    conn = sqlite3.connect(ledger.db_path)
    try:
        status = conn.execute(
            """
            SELECT status
            FROM ingestion_batches
            WHERE account_id = 'other-account' AND batch_key = 'batch-2'
            """
        ).fetchone()[0]
    finally:
        conn.close()
    assert status == "failed"


def test_null_and_type_identity_values_do_not_collide(tmp_path):
    ledger = AssetLedger(tmp_path / "ledger.db")

    ledger.ingest_records(
        "acct",
        "message.db",
        "batch-1",
        [
            {
                "source_table": "message_table",
                "message_id": None,
                "server_id": "srv",
                "sequence": 1,
                "content_type": 2,
                "content": "null id",
            },
            {
                "source_table": "message_table",
                "message_id": "null",
                "server_id": "srv",
                "sequence": "1",
                "content_type": 2,
                "content": "string id",
            },
        ],
    )

    assert row_count(ledger.db_path, "messages") == 2
    conn = sqlite3.connect(ledger.db_path)
    try:
        identities = [
            row[0] for row in conn.execute("SELECT identity_json FROM messages")
        ]
    finally:
        conn.close()
    assert '"message_id":null' in identities[0] or '"message_id":null' in identities[1]
    assert '"sequence":"1"' in identities[0] or '"sequence":"1"' in identities[1]


def test_account_and_source_table_are_message_identity_dimensions(tmp_path):
    ledger = AssetLedger(tmp_path / "ledger.db")
    base = {
        "source_table": "message_table",
        "message_id": "m1",
        "server_id": "s1",
        "sequence": 1,
        "content_type": 2,
        "content": "hello",
    }

    ledger.ingest_records("acct-a", "message.db", "batch-1", [base])
    ledger.ingest_records("acct-b", "message.db", "batch-2", [base])
    ledger.ingest_records(
        "acct-a",
        "message.db",
        "batch-3",
        [dict(base, source_table="message_small_table")],
    )

    assert row_count(ledger.db_path, "messages") == 3
