import hashlib
import json
import os
from pathlib import Path
import sqlite3

import pytest
from click.testing import CliRunner

import wecom_reader.export.audit as audit_export
from wecom_reader.cli import main
from wecom_reader.export.audit import (
    AUDIT_FIELDS,
    audit_record_from_message,
    export_audit_jsonl,
    stable_hash,
    verify_audit_source_manifest,
    write_audit_source_manifest,
    write_audit_jsonl,
)
from wecom_reader.reader import WeComReader


def _create_message_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        schema = """
            message_id INTEGER,
            server_id INTEGER,
            sequence INTEGER,
            sender_id INTEGER,
            conversation_id TEXT,
            content_type INTEGER,
            send_time INTEGER,
            flag INTEGER,
            content BLOB,
            from_app_id TEXT
        """
        conn.execute(f"CREATE TABLE message_table ({schema})")
        conn.execute(f"CREATE TABLE message_small_table ({schema})")
        conn.executemany(
            "INSERT INTO message_table VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (1, 101, 10, 42, "S:42_7", 0, 1000, 0, "正文😀", ""),
                (2, 102, 11, None, "R:8", 9999, 1001, 0, b"\xffraw", ""),
            ],
        )
        conn.execute(
            "INSERT INTO message_small_table VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (1, 101, 10, 42, "S:42_7", 0, 1000, 0, "正文😀", ""),
        )
        conn.commit()
    finally:
        conn.close()


def _read_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def test_standard_record_matches_contract_and_hashes_sensitive_values():
    raw = {
        "message_id": 7,
        "sequence": 8,
        "sender_id": "acct",
        "conversation_id": "S:acct_other",
        "content_type": 0,
        "send_time": 9,
        "content": "秘密正文",
        "resource_refs": [r"C:\private\image.jpg"],
    }

    record = audit_record_from_message(raw, "acct")

    assert tuple(record) == AUDIT_FIELDS
    assert record["account_hash"] == stable_hash("acct")
    assert record["conversation_hash"] == stable_hash("S:acct_other")
    assert record["message_id"] == stable_hash("7")
    assert record["sender_hash"] == stable_hash("acct")
    assert record["content_hash"] == stable_hash("秘密正文")
    assert record["resource_refs"] == [stable_hash(r"C:\private\image.jpg")]
    assert record["direction"] == "outgoing"
    assert record["conversation_type"] == "single"
    assert record["parse_status"] == "OK"
    assert record["status"] == ""
    assert "秘密正文" not in json.dumps(record, ensure_ascii=False)
    assert "private" not in json.dumps(record)


def test_hashes_are_stable_and_match_consumer_algorithm():
    expected = "sha256:" + hashlib.sha256("同一输入".encode()).hexdigest()
    assert stable_hash("同一输入") == expected
    assert stable_hash("同一输入") == stable_hash("同一输入")


def test_text_hash_preserves_unicode_and_whitespace():
    content = "前置和尾随空格  \n"

    record = audit_record_from_message(
        {
            "message_id": 1,
            "sequence": 1,
            "conversation_id": "S:1_2",
            "content_type": 0,
            "content": content,
        },
        "acct",
    )

    assert record["content_hash"] == stable_hash(content)


def test_unknown_type_and_missing_fields_are_explicit():
    unsupported = audit_record_from_message(
        {
            "message_id": 1,
            "sequence": 2,
            "conversation_id": "R:3",
            "content_type": 4242,
            "content": b"\xff\x00",
        },
        "acct",
        default_source="wal",
    )
    failed = audit_record_from_message(
        {
            "message_id": 2,
            "sequence": "bad",
            "conversation_id": None,
            "content_type": 0,
            "content": None,
        },
        "acct",
    )

    assert unsupported["message_type"] == "4242"
    assert unsupported["parse_status"] == "UNSUPPORTED"
    assert unsupported["source"] == "wal"
    assert failed["parse_status"] == "ERROR"
    assert failed["sequence"] is None
    assert failed["conversation_hash"] == stable_hash("")
    assert failed["resource_refs"] == []


def test_known_non_text_type_is_not_claimed_as_fully_parsed():
    record = audit_record_from_message(
        {
            "message_id": 1,
            "sequence": 2,
            "conversation_id": "R:3",
            "content_type": 15,
            "content": b"resource envelope",
        },
        "acct",
    )

    assert record["message_type"] == "image/file"
    assert record["parse_status"] == "UNVERIFIABLE"


def test_status_preserves_numeric_flag_without_exposing_other_fields():
    record = audit_record_from_message(
        {
            "message_id": 1,
            "sequence": 2,
            "conversation_id": "R:3",
            "content_type": 0,
            "content": "text",
            "status": None,
            "flag": 16777216,
        },
        "acct",
    )

    assert record["status"] == "16777216"


def test_export_streams_all_tables_and_reports_duplicates(tmp_path: Path):
    database = tmp_path / "message.db"
    output = tmp_path / "output" / "audit.jsonl"
    _create_message_db(database)

    summary = export_audit_jsonl(database, output, "acct")
    records = _read_jsonl(output)

    assert summary.record_count == 3
    assert summary.unique_record_count == 2
    assert summary.duplicate_count == 1
    assert summary.parse_status_counts == {"OK": 2, "UNSUPPORTED": 1}
    assert len(records) == 3
    assert records[0]["source"] == "db"
    serialized = output.read_text(encoding="utf-8")
    assert "正文" not in serialized
    assert "S:42_7" not in serialized
    assert '"message_id":1' not in serialized


def test_parse_failure_produces_error_record(monkeypatch):
    def fail_parse(_value):
        raise ValueError("contains secret")

    monkeypatch.setattr(audit_export, "_parse_content", fail_parse)

    record = audit_record_from_message(
        {
            "message_id": 1,
            "sequence": 1,
            "conversation_id": "R:1",
            "content_type": 0,
            "content": b"\xffsecret",
        },
        "acct",
    )

    assert record["parse_status"] == "ERROR"
    assert record["content_hash"] == stable_hash(b"\xffsecret")
    assert "secret" not in json.dumps(record)


def test_atomic_failure_preserves_existing_output(tmp_path: Path, monkeypatch):
    output = tmp_path / "audit.jsonl"
    output.write_text("old\n", encoding="utf-8")
    record = audit_record_from_message(
        {
            "message_id": 1,
            "sequence": 1,
            "conversation_id": "R:1",
            "content_type": 0,
            "content": "content",
        },
        "acct",
    )

    def fail_replace(_source, _target):
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError):
        write_audit_jsonl(output, [record])

    assert output.read_text(encoding="utf-8") == "old\n"
    assert list(tmp_path.glob("*.tmp")) == []
    assert list(tmp_path.glob(".*.tmp")) == []
    assert list(tmp_path.glob("*.sqlite3")) == []
    assert list(tmp_path.glob(".*.sqlite3")) == []


def test_empty_export_is_not_published(tmp_path: Path):
    output = tmp_path / "audit.jsonl"

    with pytest.raises(ValueError, match="empty"):
        write_audit_jsonl(output, [])

    assert not output.exists()


def test_export_audit_cli_writes_summary_without_raw_paths(tmp_path: Path):
    account_dir = tmp_path / "123456789"
    decrypted = tmp_path / "decrypted"
    account_dir.joinpath("Data").mkdir(parents=True)
    account_dir.joinpath("Data", "message.db").write_bytes(b"source")
    decrypted.mkdir()
    _create_message_db(decrypted / "message.db")
    assert write_audit_source_manifest(account_dir / "Data", decrypted)
    output = tmp_path / "output" / "reader.jsonl"

    result = CliRunner().invoke(
        main,
        [
            "--db-dir",
            str(account_dir / "Data"),
            "--decrypted-dir",
            str(decrypted),
            "export-audit",
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["success"] is True
    assert payload["output"] == "reader.jsonl"
    assert payload["record_count"] == 3
    assert "123456789" not in result.output
    assert str(tmp_path) not in result.output


def test_export_audit_cli_returns_nonzero_without_leaking_path(tmp_path: Path):
    missing = tmp_path / "private" / "missing"
    output = tmp_path / "reader.jsonl"
    source = tmp_path / "123456789" / "Data"
    source.mkdir(parents=True)
    source.joinpath("message.db").write_bytes(b"source")
    decrypted = tmp_path / "decrypted"
    decrypted.mkdir()
    _create_message_db(decrypted / "message.db")
    assert write_audit_source_manifest(source, decrypted)
    decrypted.joinpath("message.db").unlink()

    result = CliRunner().invoke(
        main,
        [
            "--db-dir",
            str(source),
            "--decrypted-dir",
            str(decrypted),
            "export-audit",
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 1
    assert json.loads(result.output) == {
        "success": False,
        "error": "SourceProvenanceError",
    }
    assert str(missing) not in result.output
    assert not output.exists()


def test_source_manifest_is_private_and_binds_account_and_snapshot(tmp_path: Path):
    source = tmp_path / "123456789" / "Data"
    source.mkdir(parents=True)
    source.joinpath("message.db").write_bytes(b"encrypted source")
    decrypted = tmp_path / "decrypted"
    decrypted.mkdir()
    _create_message_db(decrypted / "message.db")

    assert write_audit_source_manifest(source, decrypted)
    manifest_text = decrypted.joinpath(".wecom-reader-audit-source.json").read_text(
        encoding="utf-8"
    )

    assert "123456789" not in manifest_text
    assert str(tmp_path) not in manifest_text
    assert verify_audit_source_manifest(source, decrypted) == "123456789"


def test_source_manifest_rejects_account_or_snapshot_mismatch(tmp_path: Path):
    source = tmp_path / "123456789" / "Data"
    other_source = tmp_path / "987654321" / "Data"
    source.mkdir(parents=True)
    other_source.mkdir(parents=True)
    source.joinpath("message.db").write_bytes(b"source")
    other_source.joinpath("message.db").write_bytes(b"other")
    decrypted = tmp_path / "decrypted"
    decrypted.mkdir()
    _create_message_db(decrypted / "message.db")
    assert write_audit_source_manifest(source, decrypted)

    with pytest.raises(audit_export.SourceProvenanceError):
        verify_audit_source_manifest(other_source, decrypted)

    with sqlite3.connect(decrypted / "message.db") as conn:
        conn.execute("INSERT INTO message_table DEFAULT VALUES")
        conn.commit()
    with pytest.raises(audit_export.SourceProvenanceError):
        verify_audit_source_manifest(source, decrypted)


def test_init_invalidates_manifest_when_message_publish_fails(tmp_path: Path):
    source = tmp_path / "123456789" / "Data"
    source.mkdir(parents=True)
    source.joinpath("message.db").write_bytes(b"unsupported current snapshot")
    decrypted = tmp_path / "decrypted"
    decrypted.mkdir()
    _create_message_db(decrypted / "message.db")
    assert write_audit_source_manifest(source, decrypted)

    reader = WeComReader(
        db_dir=str(source),
        decrypted_dir=str(decrypted),
        key_map={"_db_dir": str(source)},
    )
    result = reader.init()

    assert result["audit_source_manifest"] is False
    assert result["failed"] == 1
    assert not decrypted.joinpath(".wecom-reader-audit-source.json").exists()
    with pytest.raises(audit_export.SourceProvenanceError):
        verify_audit_source_manifest(source, decrypted)


def test_export_audit_cli_rejects_unverifiable_account_directory(tmp_path: Path):
    decrypted = tmp_path / "decrypted"
    decrypted.mkdir()
    _create_message_db(decrypted / "message.db")

    result = CliRunner().invoke(
        main,
        [
            "--db-dir",
            str(tmp_path / "named-account" / "Data"),
            "--decrypted-dir",
            str(decrypted),
            "export-audit",
            "--output",
            str(tmp_path / "reader.jsonl"),
        ],
    )

    assert result.exit_code == 1
    assert json.loads(result.output) == {
        "success": False,
        "error": "SourceProvenanceError",
    }
    assert "named-account" not in result.output
