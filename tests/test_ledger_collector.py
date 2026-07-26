import os
import sqlite3
import time

import pytest

import wecom_reader.ledger_collector as ledger_collector
from wecom_reader.ledger import AssetLedger
from wecom_reader.ledger_collector import SourceUnstableError, collect_message_db


class FakeLedger:
    def __init__(self):
        self.calls = []

    def ingest_records(
        self, account_id, source_name, batch_key, records, checkpoint=None
    ):
        records = list(records)
        self.calls.append(
            {
                "account_id": account_id,
                "source_name": source_name,
                "batch_key": batch_key,
                "records": records,
                "checkpoint": checkpoint,
            }
        )
        return {"ingested_count": len(records)}


def create_message_db(path):
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE message_table (sequence INTEGER, content BLOB, conversation_id TEXT)"
        )
        conn.execute(
            "INSERT INTO message_table VALUES (?, ?, ?)",
            (2, b"\x00hello", "S:1"),
        )
        conn.execute(
            "CREATE TABLE message_small_table (sequence INTEGER, content BLOB, extra TEXT)"
        )
        conn.execute(
            "INSERT INTO message_small_table VALUES (?, ?, ?)",
            (5, b"\xffsmall", "kept"),
        )
        conn.execute("CREATE TABLE kf_message_tableV1 (only_blob BLOB, note TEXT)")
        conn.execute(
            "INSERT INTO kf_message_tableV1 VALUES (?, ?)",
            (b"\x01\x02", "unknown schema"),
        )
        conn.execute("CREATE TABLE ignored_table (sequence INTEGER)")
        conn.execute("INSERT INTO ignored_table VALUES (99)")
        conn.commit()
    finally:
        conn.close()


def test_collects_existing_message_tables_with_raw_rows(tmp_path):
    db_path = tmp_path / "message.db"
    create_message_db(db_path)
    ledger = FakeLedger()

    result = collect_message_db(ledger, db_path, "acct-1")

    assert result.observed_count == 3
    assert result.ingested_count == 3
    assert result.tables == (
        "message_table",
        "message_small_table",
        "kf_message_tableV1",
    )
    assert result.batch_key == result.checkpoint["source_sha256"]
    assert result.checkpoint["observed_count"] == 3
    assert result.checkpoint["max_sequence"] == 5
    call = ledger.calls[0]
    assert call["account_id"] == "acct-1"
    assert call["source_name"] == "message.db"
    assert call["batch_key"] == result.batch_key
    assert call["checkpoint"] == result.checkpoint
    assert [record["source_table"] for record in call["records"]] == [
        "message_table",
        "message_small_table",
        "kf_message_tableV1",
    ]
    assert call["records"][0]["source_rowid"] == 1
    assert call["records"][0]["content"] == b"\x00hello"


def test_unknown_schema_rows_are_forwarded_for_ledger_isolation(tmp_path):
    db_path = tmp_path / "message.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE message_table (custom BLOB, strange TEXT)")
        conn.execute("INSERT INTO message_table VALUES (?, ?)", (b"\x03\x04", "value"))
        conn.commit()
    finally:
        conn.close()
    ledger = FakeLedger()

    result = collect_message_db(ledger, db_path, "acct-1")

    assert result.observed_count == 1
    record = ledger.calls[0]["records"][0]
    assert record["custom"] == b"\x03\x04"
    assert record["strange"] == "value"
    assert record["source_table"] == "message_table"
    assert record["source_rowid"] == 1


def test_without_rowid_table_is_forwarded_with_null_source_rowid(tmp_path):
    db_path = tmp_path / "message.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE message_table (
                custom_key TEXT PRIMARY KEY,
                content BLOB
            ) WITHOUT ROWID
            """
        )
        conn.execute("INSERT INTO message_table VALUES (?, ?)", ("key-1", b"raw"))
        conn.commit()
    finally:
        conn.close()
    ledger = FakeLedger()

    result = collect_message_db(ledger, db_path, "acct-1")

    assert result.observed_count == 1
    assert ledger.calls[0]["records"][0]["source_rowid"] is None


def test_repeated_stable_snapshot_uses_same_batch_key(tmp_path):
    db_path = tmp_path / "message.db"
    create_message_db(db_path)

    first = collect_message_db(FakeLedger(), db_path, "acct-1")
    second = collect_message_db(FakeLedger(), db_path, "acct-1")

    assert first.batch_key == second.batch_key
    assert first.checkpoint["source_sha256"] == second.checkpoint["source_sha256"]


def test_real_ledger_accounts_for_every_source_row_and_is_idempotent(tmp_path):
    db_path = tmp_path / "message.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE message_table (
                message_id INTEGER,
                server_id INTEGER,
                sequence INTEGER,
                content_type INTEGER,
                content BLOB
            )
            """
        )
        conn.executemany(
            "INSERT INTO message_table VALUES (?, ?, ?, ?, ?)",
            [
                (1, 101, 1, 2, b"hello"),
                (None, None, None, 9999, b"unidentified raw"),
            ],
        )
        conn.commit()
    finally:
        conn.close()
    ledger = AssetLedger(tmp_path / "asset-ledger.db")

    first = collect_message_db(ledger, db_path, "acct-1")
    second = collect_message_db(ledger, db_path, "acct-1")

    assert first.ledger_result.records_seen == 2
    assert first.ledger_result.observations_inserted == 1
    assert first.ledger_result.quarantined == 1
    assert (
        first.ledger_result.observations_inserted + first.ledger_result.quarantined
        == first.observed_count
    )
    assert second.ledger_result.already_completed is True
    assert second.ledger_result.batch_id == first.ledger_result.batch_id
    assert ledger.get_checkpoint("acct-1", "message.db") == first.checkpoint


def test_nonempty_wal_is_rejected_without_ingest(tmp_path):
    db_path = tmp_path / "message.db"
    create_message_db(db_path)
    wal_path = tmp_path / "message.db-wal"
    wal_path.write_bytes(b"pending")
    ledger = FakeLedger()

    with pytest.raises(SourceUnstableError, match="WAL"):
        collect_message_db(ledger, db_path, "acct-1")

    assert ledger.calls == []


def test_source_change_during_collection_is_rejected_without_ingest(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "message.db"
    create_message_db(db_path)
    real_iter = ledger_collector._iter_message_records

    def changing_iter(*args, **kwargs):
        changed = False
        for record in real_iter(*args, **kwargs):
            yield record
            if not changed:
                with open(db_path, "ab") as handle:
                    handle.write(b"changed")
                os.utime(db_path, None)
                changed = True

    monkeypatch.setattr(ledger_collector, "_iter_message_records", changing_iter)
    ledger = FakeLedger()

    with pytest.raises(SourceUnstableError, match="changed"):
        collect_message_db(ledger, db_path, "acct-1")

    assert ledger.calls == []


def test_source_database_is_not_modified(tmp_path):
    db_path = tmp_path / "message.db"
    create_message_db(db_path)
    before = db_path.read_bytes()
    before_stat = db_path.stat()
    time.sleep(0.01)

    collect_message_db(FakeLedger(), db_path, "acct-1")

    after_stat = db_path.stat()
    assert db_path.read_bytes() == before
    assert after_stat.st_size == before_stat.st_size
    assert after_stat.st_mtime_ns == before_stat.st_mtime_ns
