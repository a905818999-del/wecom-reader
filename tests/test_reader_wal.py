"""Reader integration tests for WAL-aware atomic snapshots."""

from pathlib import Path
import shutil
import sqlite3

import wecom_reader.reader as reader_module
from wecom_reader.reader import WeComReader


def _reader(source, output):
    reader = WeComReader(db_dir=str(source), decrypted_dir=str(output))
    reader._key_map = {"_db_dir": str(source)}
    return reader


def test_init_recovers_committed_plain_sqlite_wal(tmp_path):
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    db_path = source / "message.db"

    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute("CREATE TABLE messages (body TEXT NOT NULL)")
    connection.commit()
    connection.execute("INSERT INTO messages VALUES ('latest')")
    connection.commit()

    try:
        result = _reader(source, output).init()
    finally:
        connection.close()

    assert result["wal_present"] == ["message.db"]
    assert result["wal_recovered"] == ["message.db"]
    assert result["wal_failed"] == []
    assert result["wal_warning"] is None
    with sqlite3.connect(output / "message.db") as recovered:
        assert recovered.execute("SELECT body FROM messages").fetchall() == [
            ("latest",)
        ]


def test_init_recovers_last_valid_commit_but_blocks_checkpoint_on_bad_tail(tmp_path):
    writer_dir = tmp_path / "writer"
    source = tmp_path / "source"
    output = tmp_path / "output"
    writer_dir.mkdir()
    source.mkdir()
    output.mkdir()

    writer_db = writer_dir / "message.db"
    connection = sqlite3.connect(writer_db)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute("CREATE TABLE messages (body TEXT NOT NULL)")
    connection.execute("INSERT INTO messages VALUES ('new')")
    connection.commit()

    source_db = source / "message.db"
    source_wal = source / "message.db-wal"
    shutil.copy2(writer_db, source_db)
    shutil.copy2(f"{writer_db}-wal", source_wal)
    connection.close()

    wal_bytes = bytearray(source_wal.read_bytes())
    wal_bytes[-1] ^= 0xFF
    source_wal.write_bytes(wal_bytes)

    old_output = output / "message.db"
    old_connection = sqlite3.connect(old_output)
    try:
        old_connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
        old_connection.execute("INSERT INTO marker VALUES ('previous')")
        old_connection.commit()
    finally:
        old_connection.close()

    result = _reader(source, output).init()

    assert result["wal_recovered"] == ["message.db"]
    assert result["wal_degraded"] == ["message.db"]
    assert result["wal_failed"] == []
    assert result["wal_retained_snapshot"] == []
    assert result["wal_checkpoint_safe"] is False
    assert "checksum" in result["wal_warning"]
    recovered = sqlite3.connect(old_output)
    try:
        assert recovered.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert recovered.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='messages'"
        ).fetchone() == ("messages",)
    finally:
        recovered.close()


def test_init_atomically_publishes_database_without_wal(tmp_path):
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    db_path = source / "session.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE sessions (name TEXT NOT NULL)")
        connection.execute("INSERT INTO sessions VALUES ('kept')")

    result = _reader(source, output).init()

    assert result["success"] is True
    assert result["copied"] == 1
    assert result["wal_present"] == []
    assert result["wal_retained_snapshot"] == []
    with sqlite3.connect(output / "session.db") as copied:
        assert copied.execute("SELECT name FROM sessions").fetchone() == ("kept",)


def test_init_falls_back_to_main_database_when_wal_header_is_invalid(tmp_path):
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    db_path = source / "message.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE messages (body TEXT NOT NULL)")
        connection.execute("INSERT INTO messages VALUES ('main-only')")
    (source / "message.db-wal").write_bytes(b"invalid")

    result = _reader(source, output).init()

    assert result["wal_failed"] == ["message.db"]
    assert result["wal_retained_snapshot"] == []
    assert "32-byte header" in result["wal_warning"]
    with sqlite3.connect(output / "message.db") as fallback:
        assert fallback.execute("SELECT body FROM messages").fetchone() == (
            "main-only",
        )


def test_snapshot_retries_when_live_wal_disappears(tmp_path, monkeypatch):
    source_db = tmp_path / "message.db"
    source_db.write_bytes(b"database")
    source_wal = tmp_path / "message.db-wal"
    source_wal.write_bytes(b"wal")
    snapshot_dir = tmp_path / "snapshot"
    snapshot_dir.mkdir()
    real_copy2 = shutil.copy2
    wal_attempts = 0

    def copy2_with_wal_reset(source, destination):
        nonlocal wal_attempts
        if str(source).endswith("-wal"):
            wal_attempts += 1
            if wal_attempts == 1:
                source_wal.unlink()
                raise FileNotFoundError(source)
        return real_copy2(source, destination)

    monkeypatch.setattr(reader_module.shutil, "copy2", copy2_with_wal_reset)

    db_copy, wal_copy = reader_module._copy_database_snapshot(
        str(source_db), str(snapshot_dir)
    )

    assert Path(db_copy).read_bytes() == b"database"
    assert wal_copy is None
    assert wal_attempts == 1
