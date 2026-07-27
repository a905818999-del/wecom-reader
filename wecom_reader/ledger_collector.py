"""Read-only collection of message.db rows for the asset ledger."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import sqlite3
from typing import Any
from urllib.parse import quote

from wecom_reader.db.message import MESSAGE_TABLES


class SourceUnstableError(RuntimeError):
    """Raised when message.db cannot be collected as a stable read-only snapshot."""


@dataclass(frozen=True)
class BatchResult:
    """Summary of one message.db collection attempt."""

    account_id: str
    source_name: str
    batch_key: str
    checkpoint: dict[str, Any]
    observed_count: int
    ingested_count: int | None
    tables: tuple[str, ...]
    ledger_result: Any = None


def collect_message_db(
    ledger: Any,
    db_path: str | os.PathLike[str],
    account_id: str,
    source_name: str = "message.db",
) -> BatchResult:
    """Collect existing WeCom message tables and ingest their raw rows.

    The source database is opened through SQLite's read-only URI mode. Collection
    refuses non-empty WAL files and rejects snapshots whose file metadata or
    content hash changes while being read.
    """
    path = Path(db_path)
    _reject_nonempty_wal(path)

    before_digest, before_stat = _stable_sha256(path)
    tables, observed_count, max_sequence = _source_summary(path)
    checkpoint = {
        "source_sha256": before_digest,
        "source_size": before_stat.st_size,
        "source_mtime_ns": before_stat.st_mtime_ns,
        "observed_count": observed_count,
        "max_sequence": max_sequence,
    }
    validation = {"completed": False}
    records = _iter_message_records(
        path,
        tables,
        before_digest,
        before_stat,
        validation,
    )

    ledger_result = ledger.ingest_records(
        account_id,
        source_name,
        before_digest,
        records,
        checkpoint=checkpoint,
    )
    if not validation["completed"]:
        _validate_unchanged(path, before_digest, before_stat)
    return BatchResult(
        account_id=account_id,
        source_name=source_name,
        batch_key=before_digest,
        checkpoint=checkpoint,
        observed_count=observed_count,
        ingested_count=_ingested_count(ledger_result),
        tables=tuple(tables),
        ledger_result=ledger_result,
    )


def iter_stable_message_records(db_path: str | os.PathLike[str]):
    """Yield every supported message-table row from a stable read-only snapshot."""
    path = Path(db_path)
    _reject_nonempty_wal(path)
    before_digest, before_stat = _stable_sha256(path)
    tables, _observed_count, _max_sequence = _source_summary(path)
    validation = {"completed": False}
    yield from _iter_message_records(
        path,
        tables,
        before_digest,
        before_stat,
        validation,
    )


def _reject_nonempty_wal(path: Path) -> None:
    wal_path = path.with_name(path.name + "-wal")
    try:
        if wal_path.stat().st_size > 0:
            raise SourceUnstableError(f"refusing unstable SQLite WAL: {wal_path}")
    except FileNotFoundError:
        return


def _stable_sha256(path: Path) -> tuple[str, os.stat_result]:
    before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    after = path.stat()
    if _stat_fingerprint(before) != _stat_fingerprint(after):
        raise SourceUnstableError(f"{path} changed while hashing")
    return digest.hexdigest(), after


def _stat_fingerprint(stat_result: os.stat_result) -> tuple[int, int]:
    return stat_result.st_size, stat_result.st_mtime_ns


def _source_summary(path: Path) -> tuple[list[str], int, Any]:
    tables: list[str] = []
    observed_count = 0
    max_sequence = None
    conn = sqlite3.connect(_readonly_sqlite_uri(path), uri=True)
    try:
        for table in MESSAGE_TABLES:
            if not _table_exists(conn, table):
                continue
            tables.append(table)
            observed_count += conn.execute(
                f'SELECT COUNT(*) FROM "{table}"'
            ).fetchone()[0]
            columns = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
            if "sequence" in columns:
                table_max = conn.execute(
                    f'SELECT MAX(sequence) FROM "{table}" '
                    "WHERE typeof(sequence) = 'integer'"
                ).fetchone()[0]
                if table_max is not None:
                    max_sequence = (
                        table_max
                        if max_sequence is None
                        else max(max_sequence, table_max)
                    )
    finally:
        conn.close()
    return tables, observed_count, max_sequence


def _iter_message_records(
    path: Path,
    tables: list[str],
    expected_digest: str,
    expected_stat: os.stat_result,
    validation: dict[str, bool],
):
    conn = sqlite3.connect(_readonly_sqlite_uri(path), uri=True)
    conn.row_factory = sqlite3.Row
    try:
        for table in tables:
            if _table_supports_rowid(conn, table):
                rows = conn.execute(
                    f'SELECT rowid AS __source_rowid__, * FROM "{table}" ORDER BY rowid'
                )
            else:
                rows = conn.execute(f'SELECT * FROM "{table}"')
            for row in rows:
                record = dict(row)
                record["source_table"] = table
                record["source_rowid"] = record.pop("__source_rowid__", None)
                yield record
    finally:
        conn.close()
    _validate_unchanged(path, expected_digest, expected_stat)
    validation["completed"] = True


def _validate_unchanged(
    path: Path,
    expected_digest: str,
    expected_stat: os.stat_result,
) -> None:
    _reject_nonempty_wal(path)
    actual_digest, actual_stat = _stable_sha256(path)
    if expected_digest != actual_digest or _stat_fingerprint(
        expected_stat
    ) != _stat_fingerprint(actual_stat):
        raise SourceUnstableError(f"{path} changed during collection")


def _readonly_sqlite_uri(path: Path) -> str:
    absolute = path.resolve()
    return "file:" + quote(str(absolute).replace("\\", "/"), safe="/:") + "?mode=ro"


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _table_supports_rowid(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return bool(row and row[0] and "WITHOUT ROWID" not in row[0].upper())


def _ingested_count(ledger_result: Any) -> int | None:
    if isinstance(ledger_result, int):
        return ledger_result
    if isinstance(ledger_result, dict) and isinstance(
        ledger_result.get("ingested_count"), int
    ):
        return ledger_result["ingested_count"]
    if hasattr(ledger_result, "records_seen"):
        value = getattr(ledger_result, "records_seen")
        if isinstance(value, int):
            return value
    if hasattr(ledger_result, "ingested_count"):
        value = getattr(ledger_result, "ingested_count")
        if isinstance(value, int):
            return value
    return None
