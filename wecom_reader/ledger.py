"""SQLite asset ledger for durable WeCom message ingestion."""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from wecom_reader.db.message import MSG_TYPES, _parse_content


class LedgerIntegrityError(RuntimeError):
    """Raised when ledger invariants cannot safely isolate a single source row."""


@dataclass(frozen=True)
class IngestResult:
    """Summary counts for one ledger ingestion batch."""

    batch_id: int
    completed: bool
    already_completed: bool
    records_seen: int
    messages_inserted: int
    versions_inserted: int
    observations_inserted: int
    quarantined: int


class AssetLedger:
    """Persist message identity, versions, observations, and checkpoints."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self._init_schema()

    def ingest_records(
        self,
        account_id: str,
        source_name: str,
        batch_key: str,
        records,
        checkpoint: Any = None,
    ) -> IngestResult:
        """Ingest records atomically, isolating bad rows into quarantine.

        ``source_name`` identifies the checkpoint stream and batch provenance.
        Message identity intentionally follows the product contract instead:
        account, source table, message/server IDs, and sequence.
        """
        _require_name(account_id, "account_id")
        _require_name(source_name, "source_name")
        _require_name(batch_key, "batch_key")
        existing = self._completed_batch(account_id, source_name, batch_key)
        if existing is not None:
            return existing

        conn = self._connect()
        result = None
        try:
            try:
                with conn:
                    conn.execute("BEGIN IMMEDIATE")
                    row = conn.execute(
                        """
                        SELECT *
                        FROM ingestion_batches
                        WHERE account_id = ? AND source_name = ? AND batch_key = ?
                        """,
                        (account_id, source_name, batch_key),
                    ).fetchone()
                    if row is not None and row["status"] == "completed":
                        return _result_from_batch_row(row)
                    if row is None:
                        cursor = conn.execute(
                            """
                            INSERT INTO ingestion_batches (
                                account_id, source_name, batch_key, status,
                                records_seen, messages_inserted, versions_inserted,
                                observations_inserted, quarantined
                            )
                            VALUES (?, ?, ?, 'running', 0, 0, 0, 0, 0)
                            """,
                            (account_id, source_name, batch_key),
                        )
                        batch_id = int(cursor.lastrowid)
                    else:
                        batch_id = int(row["id"])
                        conn.execute(
                            """
                            UPDATE ingestion_batches
                            SET status = 'running',
                                records_seen = 0,
                                messages_inserted = 0,
                                versions_inserted = 0,
                                observations_inserted = 0,
                                quarantined = 0,
                                checkpoint_json = NULL,
                                error_message = NULL,
                                completed_at = NULL
                            WHERE id = ?
                            """,
                            (batch_id,),
                        )
                    result = _MutableResult(batch_id=batch_id)

                    for ordinal, record in enumerate(records):
                        result.records_seen += 1
                        counts_before = (
                            result.messages_inserted,
                            result.versions_inserted,
                            result.observations_inserted,
                        )
                        conn.execute("SAVEPOINT ingest_record")
                        try:
                            self._ingest_one(
                                conn,
                                batch_id,
                                account_id,
                                source_name,
                                ordinal,
                                record,
                                result,
                            )
                            conn.execute("RELEASE SAVEPOINT ingest_record")
                        except Exception as exc:
                            conn.execute("ROLLBACK TO SAVEPOINT ingest_record")
                            conn.execute("RELEASE SAVEPOINT ingest_record")
                            (
                                result.messages_inserted,
                                result.versions_inserted,
                                result.observations_inserted,
                            ) = counts_before
                            if isinstance(exc, (LedgerIntegrityError, sqlite3.Error)):
                                raise
                            self._quarantine(
                                conn,
                                batch_id,
                                source_name,
                                ordinal,
                                record,
                                type(exc).__name__,
                                _safe_error_message(exc),
                            )
                            result.quarantined += 1

                    if (
                        result.observations_inserted + result.quarantined
                        != result.records_seen
                    ):
                        raise RuntimeError(
                            "batch receipt invariant failed: every record must be "
                            "observed or quarantined"
                        )
                    checkpoint_json = (
                        _canonical_json(checkpoint) if checkpoint is not None else None
                    )
                    conn.execute(
                        """
                        UPDATE ingestion_batches
                        SET status = 'completed',
                            records_seen = ?,
                            messages_inserted = ?,
                            versions_inserted = ?,
                            observations_inserted = ?,
                            quarantined = ?,
                            checkpoint_json = ?,
                            error_message = NULL,
                            completed_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                        """,
                        (
                            result.records_seen,
                            result.messages_inserted,
                            result.versions_inserted,
                            result.observations_inserted,
                            result.quarantined,
                            checkpoint_json,
                            batch_id,
                        ),
                    )
                    if checkpoint is not None:
                        conn.execute(
                            """
                            INSERT INTO checkpoints (
                                account_id, source_name, checkpoint_json, updated_at
                            )
                            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                            ON CONFLICT(account_id, source_name) DO UPDATE SET
                                checkpoint_json = excluded.checkpoint_json,
                                updated_at = CURRENT_TIMESTAMP
                            """,
                            (account_id, source_name, checkpoint_json),
                        )
            except Exception as exc:
                self._record_failed_batch(
                    account_id,
                    source_name,
                    batch_key,
                    result.records_seen if result is not None else 0,
                    exc,
                )
                raise

            return result.to_public(completed=True)
        finally:
            conn.close()

    def get_checkpoint(self, account_id: str, source_name: str):
        """Return the latest successful checkpoint for an account/source."""
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT checkpoint_json
                FROM checkpoints
                WHERE account_id = ? AND source_name = ?
                """,
                (account_id, source_name),
            ).fetchone()
            if row is None:
                return None
            return json.loads(row["checkpoint_json"])
        finally:
            conn.close()

    def _init_schema(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            with conn:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS ingestion_batches (
                        id INTEGER PRIMARY KEY,
                        account_id TEXT NOT NULL,
                        source_name TEXT NOT NULL,
                        batch_key TEXT NOT NULL,
                        status TEXT NOT NULL,
                        records_seen INTEGER NOT NULL DEFAULT 0,
                        messages_inserted INTEGER NOT NULL DEFAULT 0,
                        versions_inserted INTEGER NOT NULL DEFAULT 0,
                        observations_inserted INTEGER NOT NULL DEFAULT 0,
                        quarantined INTEGER NOT NULL DEFAULT 0,
                        checkpoint_json TEXT,
                        error_message TEXT,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        completed_at TEXT,
                        UNIQUE(account_id, source_name, batch_key)
                    );

                    CREATE TABLE IF NOT EXISTS messages (
                        id INTEGER PRIMARY KEY,
                        account_id TEXT NOT NULL,
                        source_table TEXT NOT NULL,
                        identity_hash TEXT NOT NULL UNIQUE,
                        identity_json TEXT NOT NULL,
                        first_batch_id INTEGER NOT NULL,
                        latest_version_id INTEGER,
                        last_batch_id INTEGER NOT NULL,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY(first_batch_id) REFERENCES ingestion_batches(id),
                        FOREIGN KEY(last_batch_id) REFERENCES ingestion_batches(id),
                        FOREIGN KEY(latest_version_id) REFERENCES message_versions(id)
                    );

                    CREATE TABLE IF NOT EXISTS message_versions (
                        id INTEGER PRIMARY KEY,
                        message_id INTEGER NOT NULL,
                        batch_id INTEGER NOT NULL,
                        version_hash TEXT NOT NULL,
                        raw_json TEXT NOT NULL,
                        content_type TEXT,
                        type_name TEXT NOT NULL,
                        parsed_content TEXT,
                        unsupported INTEGER NOT NULL,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(message_id, version_hash),
                        FOREIGN KEY(message_id) REFERENCES messages(id),
                        FOREIGN KEY(batch_id) REFERENCES ingestion_batches(id)
                    );

                    CREATE TABLE IF NOT EXISTS message_observations (
                        id INTEGER PRIMARY KEY,
                        batch_id INTEGER NOT NULL,
                        source_table TEXT NOT NULL,
                        source_rowid TEXT,
                        record_ordinal INTEGER NOT NULL,
                        observation_key TEXT NOT NULL,
                        message_id INTEGER NOT NULL,
                        version_id INTEGER NOT NULL,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(batch_id, record_ordinal),
                        FOREIGN KEY(batch_id) REFERENCES ingestion_batches(id),
                        FOREIGN KEY(message_id) REFERENCES messages(id),
                        FOREIGN KEY(version_id) REFERENCES message_versions(id)
                    );

                    CREATE TABLE IF NOT EXISTS quarantine_errors (
                        id INTEGER PRIMARY KEY,
                        batch_id INTEGER NOT NULL,
                        source_table TEXT NOT NULL,
                        source_rowid TEXT,
                        record_ordinal INTEGER NOT NULL,
                        error_type TEXT NOT NULL,
                        error_message TEXT NOT NULL,
                        raw_json TEXT NOT NULL,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(batch_id, source_table, record_ordinal),
                        FOREIGN KEY(batch_id) REFERENCES ingestion_batches(id)
                    );

                    CREATE TABLE IF NOT EXISTS checkpoints (
                        account_id TEXT NOT NULL,
                        source_name TEXT NOT NULL,
                        checkpoint_json TEXT NOT NULL,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY(account_id, source_name)
                    );

                    PRAGMA user_version = 1;
                    """
                )
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _completed_batch(
        self, account_id: str, source_name: str, batch_key: str
    ) -> IngestResult | None:
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT *
                FROM ingestion_batches
                WHERE account_id = ?
                    AND source_name = ?
                    AND batch_key = ?
                    AND status = 'completed'
                """,
                (account_id, source_name, batch_key),
            ).fetchone()
            if row is None:
                return None
            return _result_from_batch_row(row)
        finally:
            conn.close()

    def _ingest_one(
        self,
        conn: sqlite3.Connection,
        batch_id: int,
        account_id: str,
        source_name: str,
        ordinal: int,
        record,
        result: _MutableResult,
    ) -> None:
        if not isinstance(record, dict):
            raise ValueError("record must be a dict")

        source_table = record.get("source_table", source_name)
        source_rowid = record.get("source_rowid")
        identity = _message_identity(account_id, source_table, record)
        identity_json = _canonical_json(identity)
        identity_hash = _sha256(identity_json)

        cursor = conn.execute(
            """
            INSERT INTO messages (
                account_id, source_table,
                identity_hash, identity_json, first_batch_id, last_batch_id
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(identity_hash) DO NOTHING
            """,
            (
                account_id,
                source_table,
                identity_hash,
                identity_json,
                batch_id,
                batch_id,
            ),
        )
        if cursor.rowcount == 1:
            result.messages_inserted += 1

        message_row = conn.execute(
            "SELECT id, identity_json FROM messages WHERE identity_hash = ?",
            (identity_hash,),
        ).fetchone()
        if message_row["identity_json"] != identity_json:
            raise LedgerIntegrityError("message identity hash collision")
        message_id = int(message_row["id"])

        version_record = {
            key: value
            for key, value in record.items()
            if key not in {"source_table", "source_rowid"}
        }
        raw_json = _canonical_json(version_record)
        version_hash = _sha256(raw_json)
        content_type = record.get("content_type")
        unsupported = content_type not in MSG_TYPES
        type_name = "unsupported" if unsupported else MSG_TYPES[content_type]
        parsed_content = None if unsupported else _parse_content(record.get("content"))

        cursor = conn.execute(
            """
            INSERT INTO message_versions (
                message_id, batch_id, version_hash, raw_json, content_type,
                type_name, parsed_content, unsupported
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(message_id, version_hash) DO NOTHING
            """,
            (
                message_id,
                batch_id,
                version_hash,
                raw_json,
                _storage_text(content_type),
                type_name,
                parsed_content,
                int(unsupported),
            ),
        )
        if cursor.rowcount == 1:
            result.versions_inserted += 1

        version_row = conn.execute(
            """
            SELECT id, raw_json
            FROM message_versions
            WHERE message_id = ? AND version_hash = ?
            """,
            (message_id, version_hash),
        ).fetchone()
        if version_row["raw_json"] != raw_json:
            raise LedgerIntegrityError("message version hash collision")
        version_id = int(version_row["id"])
        conn.execute(
            """
            UPDATE messages
            SET latest_version_id = ?,
                last_batch_id = ?,
                last_seen_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (version_id, batch_id, message_id),
        )

        observation_key = _observation_key(source_rowid, ordinal)
        conn.execute(
            """
            INSERT INTO message_observations (
                batch_id, source_table, source_rowid, record_ordinal,
                observation_key, message_id, version_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                batch_id,
                source_table,
                _storage_text(source_rowid),
                ordinal,
                observation_key,
                message_id,
                version_id,
            ),
        )
        result.observations_inserted += 1

    def _quarantine(
        self,
        conn: sqlite3.Connection,
        batch_id: int,
        source_name: str,
        ordinal: int,
        record,
        error_type: str,
        error_message: str,
    ) -> None:
        source_table = source_name
        source_rowid = None
        if isinstance(record, dict):
            source_table = record.get("source_table", source_name)
            source_rowid = record.get("source_rowid")
        if not isinstance(source_table, str) or not source_table.strip():
            source_table = source_name

        conn.execute(
            """
            INSERT INTO quarantine_errors (
                batch_id, source_table, source_rowid, record_ordinal,
                error_type, error_message, raw_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                batch_id,
                source_table,
                _safe_storage_text(source_rowid),
                ordinal,
                error_type,
                error_message,
                _safe_canonical_json(record),
            ),
        )

    def _record_failed_batch(
        self,
        account_id: str,
        source_name: str,
        batch_key: str,
        records_seen: int,
        error: Exception,
    ) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO ingestion_batches (
                        account_id, source_name, batch_key, status,
                        records_seen, messages_inserted, versions_inserted,
                        observations_inserted, quarantined, error_message,
                        completed_at
                    )
                    VALUES (?, ?, ?, 'failed', ?, 0, 0, 0, 0, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(account_id, source_name, batch_key) DO UPDATE SET
                        status = 'failed',
                        records_seen = excluded.records_seen,
                        messages_inserted = 0,
                        versions_inserted = 0,
                        observations_inserted = 0,
                        quarantined = 0,
                        checkpoint_json = NULL,
                        error_message = excluded.error_message,
                        completed_at = CURRENT_TIMESTAMP
                    """,
                    (
                        account_id,
                        source_name,
                        batch_key,
                        records_seen,
                        _safe_error_message(error),
                    ),
                )
        finally:
            conn.close()


@dataclass
class _MutableResult:
    batch_id: int
    records_seen: int = 0
    messages_inserted: int = 0
    versions_inserted: int = 0
    observations_inserted: int = 0
    quarantined: int = 0

    def to_public(self, completed: bool) -> IngestResult:
        return IngestResult(
            batch_id=self.batch_id,
            completed=completed,
            already_completed=False,
            records_seen=self.records_seen,
            messages_inserted=self.messages_inserted,
            versions_inserted=self.versions_inserted,
            observations_inserted=self.observations_inserted,
            quarantined=self.quarantined,
        )


def _result_from_batch_row(row: sqlite3.Row) -> IngestResult:
    return IngestResult(
        batch_id=row["id"],
        completed=True,
        already_completed=True,
        records_seen=row["records_seen"],
        messages_inserted=row["messages_inserted"],
        versions_inserted=row["versions_inserted"],
        observations_inserted=row["observations_inserted"],
        quarantined=row["quarantined"],
    )


def _message_identity(
    account_id: str, source_table: Any, record: dict[str, Any]
) -> dict:
    _require_name(source_table, "source_table")

    identity_fields = {
        "message_id": record.get("message_id"),
        "server_id": record.get("server_id"),
        "sequence": record.get("sequence"),
    }
    if all(value is None for value in identity_fields.values()):
        raise ValueError("record has no message identity")

    return {
        "account_id": account_id,
        "source_table": source_table,
        **identity_fields,
    }


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _normalize_json(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _normalize_json(value: Any):
    if isinstance(value, dict):
        return {str(key): _normalize_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize_json(item) for item in value]
    if isinstance(value, bytes):
        return {
            "__type__": "bytes",
            "base64": base64.b64encode(value).decode("ascii"),
        }
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return {"__type__": type(value).__name__, "repr": repr(value)}


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _storage_text(value: Any) -> str | None:
    if value is None:
        return None
    return _canonical_json(value)


def _safe_storage_text(value: Any) -> str | None:
    if value is None:
        return None
    return _safe_canonical_json(value)


def _observation_key(source_rowid: Any, ordinal: int) -> str:
    if source_rowid is not None:
        return "rowid:" + _canonical_json(source_rowid)
    return f"ordinal:{ordinal}"


def _safe_canonical_json(value: Any) -> str:
    try:
        return _canonical_json(value)
    except Exception as exc:
        try:
            representation = repr(value)
        except Exception:
            representation = f"<{type(value).__name__}>"
        return _canonical_json(
            {
                "__serialization_error__": type(exc).__name__,
                "repr": representation,
            }
        )


def _require_name(value: Any, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")


def _safe_error_message(error: Exception) -> str:
    try:
        return str(error)
    except Exception:
        return f"<{type(error).__name__}>"
