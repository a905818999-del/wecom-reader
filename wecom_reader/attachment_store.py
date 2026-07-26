"""Content-addressed attachment storage backed by the asset ledger DB."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path


_CHUNK_SIZE = 1024 * 1024
_PENDING_WAIT_INTERVAL_SECONDS = 0.01
_PENDING_WAIT_TIMEOUT_SECONDS = 2.0


class _ContentAddressConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class AttachmentReference:
    """Stored attachment reference or retryable ingest error."""

    reference_id: int
    account_id: str
    message_identity_hash: str
    source_path: str
    attachment_kind: str
    resolver_version: str
    status: str
    attempts: int
    retryable: bool
    attachment_id: int | None = None
    source_size: int | None = None
    content_sha256: str | None = None
    storage_path: str | None = None
    error_code: str | None = None
    error_message: str | None = None


class AttachmentStore:
    """Store attachment files by SHA-256 and persist message references."""

    def __init__(self, ledger_db_path: str | Path, asset_root: str | Path) -> None:
        self.ledger_db_path = Path(ledger_db_path)
        self.asset_root = Path(asset_root)
        self._init_schema()

    def ingest_file(
        self,
        account_id: str,
        message_identity_hash: str,
        source_path: str | Path,
        attachment_kind: str,
        resolver_version: str,
    ) -> AttachmentReference:
        """Copy one source file into content-addressed storage and record a reference."""
        _require_name(account_id, "account_id")
        _require_name(message_identity_hash, "message_identity_hash")
        _require_name(attachment_kind, "attachment_kind")
        _require_name(resolver_version, "resolver_version")

        source = Path(source_path)
        source_text = os.fspath(source)
        reference_id, attempts = self._begin_attempt(
            account_id,
            message_identity_hash,
            source_text,
            attachment_kind,
            resolver_version,
        )
        temp_path = self._temp_path()
        known_size = None
        known_digest = None

        try:
            if not source.exists():
                return self._finish_error(
                    reference_id,
                    attempts,
                    "source_missing",
                    f"source file does not exist: {source_text}",
                )
            if not source.is_file():
                return self._finish_error(
                    reference_id,
                    attempts,
                    "read_failed",
                    f"source path is not a file: {source_text}",
                )

            before = source.stat()
            known_size = before.st_size
            digest, copied_size = self._copy_and_hash(source, temp_path)
            after = source.stat()
            if not _same_stat(before, after):
                _remove_if_exists(temp_path)
                return self._finish_error(
                    reference_id,
                    attempts,
                    "source_changed",
                    f"source changed while ingesting: {source_text}",
                    source_size=known_size,
                )
            if copied_size != after.st_size:
                _remove_if_exists(temp_path)
                return self._finish_error(
                    reference_id,
                    attempts,
                    "source_changed",
                    f"source size changed while ingesting: {source_text}",
                    source_size=known_size,
                )
            verify_before = source.stat()
            source_matches = _file_matches(source, digest, copied_size)
            verify_after = source.stat()
            if (
                not source_matches
                or not _same_stat(after, verify_before)
                or not _same_stat(verify_before, verify_after)
            ):
                _remove_if_exists(temp_path)
                return self._finish_error(
                    reference_id,
                    attempts,
                    "source_changed",
                    f"source content changed while ingesting: {source_text}",
                    source_size=known_size,
                )
            known_digest = digest

            current = self._reference_by_id(reference_id)
            if current.attempts != attempts:
                _remove_if_exists(temp_path)
                return self._wait_for_terminal_reference(reference_id)

            target = self._target_path(digest)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if not _file_matches(target, digest, copied_size):
                    _remove_if_exists(temp_path)
                    return self._finish_error(
                        reference_id,
                        attempts,
                        "content_address_conflict",
                        f"target path has unexpected content: {target}",
                        source_size=known_size,
                        content_sha256=known_digest,
                    )
                _remove_if_exists(temp_path)
            else:
                try:
                    os.replace(temp_path, target)
                except OSError:
                    if not _file_matches(target, digest, copied_size):
                        raise
                    _remove_if_exists(temp_path)

            if not _file_matches(target, digest, copied_size):
                return self._finish_error(
                    reference_id,
                    attempts,
                    "target_verify_failed",
                    f"stored file failed verification: {target}",
                    source_size=known_size,
                    content_sha256=known_digest,
                )

            try:
                attachment_id = self._record_attachment(digest, copied_size, target)
            except _ContentAddressConflict as exc:
                return self._finish_error(
                    reference_id,
                    attempts,
                    "content_address_conflict",
                    str(exc),
                    source_size=known_size,
                    content_sha256=known_digest,
                )
            return self._finish_success(
                reference_id,
                attempts,
                attachment_id,
                copied_size,
                digest,
                target,
            )
        except OSError as exc:
            _remove_if_exists(temp_path)
            return self._finish_error(
                reference_id,
                attempts,
                "read_failed",
                str(exc),
                source_size=known_size,
                content_sha256=known_digest,
            )
        except Exception as exc:
            _remove_if_exists(temp_path)
            return self._finish_error(
                reference_id,
                attempts,
                "ingest_failed",
                f"{type(exc).__name__}: {exc}",
                source_size=known_size,
                content_sha256=known_digest,
            )

    def get_reference(
        self,
        account_id: str,
        message_identity_hash: str,
        source_path: str | Path,
        attachment_kind: str,
        resolver_version: str,
    ) -> AttachmentReference | None:
        """Return the current reference row for the logical attachment source."""
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT *
                FROM attachment_references
                WHERE account_id = ?
                    AND message_identity_hash = ?
                    AND source_path = ?
                    AND attachment_kind = ?
                    AND resolver_version = ?
                """,
                (
                    account_id,
                    message_identity_hash,
                    os.fspath(Path(source_path)),
                    attachment_kind,
                    resolver_version,
                ),
            ).fetchone()
            return None if row is None else _reference_from_row(row)
        finally:
            conn.close()

    def status(
        self,
        account_id: str,
        message_identity_hash: str,
        source_path: str | Path,
        attachment_kind: str,
        resolver_version: str,
    ) -> str | None:
        """Return only the current ingest status for a reference."""
        reference = self.get_reference(
            account_id,
            message_identity_hash,
            source_path,
            attachment_kind,
            resolver_version,
        )
        return None if reference is None else reference.status

    def _init_schema(self) -> None:
        self.ledger_db_path.parent.mkdir(parents=True, exist_ok=True)
        self.asset_root.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            with conn:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS attachments (
                        id INTEGER PRIMARY KEY,
                        content_sha256 TEXT NOT NULL UNIQUE,
                        size_bytes INTEGER NOT NULL,
                        storage_path TEXT NOT NULL,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );

                    CREATE TABLE IF NOT EXISTS attachment_references (
                        id INTEGER PRIMARY KEY,
                        account_id TEXT NOT NULL,
                        message_identity_hash TEXT NOT NULL,
                        source_path TEXT NOT NULL,
                        attachment_kind TEXT NOT NULL,
                        resolver_version TEXT NOT NULL,
                        attachment_id INTEGER,
                        source_size INTEGER,
                        content_sha256 TEXT,
                        storage_path TEXT,
                        status TEXT NOT NULL,
                        error_code TEXT,
                        error_message TEXT,
                        retryable INTEGER NOT NULL,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE (
                            account_id,
                            message_identity_hash,
                            source_path,
                            attachment_kind,
                            resolver_version
                        ),
                        FOREIGN KEY(attachment_id) REFERENCES attachments(id)
                    );
                    """
                )
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.ledger_db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _begin_attempt(
        self,
        account_id: str,
        message_identity_hash: str,
        source_path: str,
        attachment_kind: str,
        resolver_version: str,
    ) -> tuple[int, int]:
        conn = self._connect()
        try:
            with conn:
                row = conn.execute(
                    """
                    INSERT INTO attachment_references (
                        account_id, message_identity_hash, source_path,
                        attachment_kind, resolver_version, status, retryable,
                        attempts
                    )
                    VALUES (?, ?, ?, ?, ?, 'pending', 1, 1)
                    ON CONFLICT (
                        account_id,
                        message_identity_hash,
                        source_path,
                        attachment_kind,
                        resolver_version
                    )
                    DO UPDATE SET
                        status = 'pending',
                        retryable = 1,
                        attempts = attachment_references.attempts + 1,
                        updated_at = CURRENT_TIMESTAMP
                    RETURNING id, attempts
                    """,
                    (
                        account_id,
                        message_identity_hash,
                        source_path,
                        attachment_kind,
                        resolver_version,
                    ),
                ).fetchone()
                return int(row["id"]), int(row["attempts"])
        finally:
            conn.close()

    def _record_attachment(self, digest: str, size: int, target: Path) -> int:
        storage_path = self._storage_path(target)
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO attachments (content_sha256, size_bytes, storage_path)
                    VALUES (?, ?, ?)
                    ON CONFLICT(content_sha256) DO NOTHING
                    """,
                    (digest, size, storage_path),
                )
                row = conn.execute(
                    """
                    SELECT id, size_bytes, storage_path
                    FROM attachments
                    WHERE content_sha256 = ?
                    """,
                    (digest,),
                ).fetchone()
                if row["size_bytes"] != size or row["storage_path"] != storage_path:
                    raise _ContentAddressConflict("attachment hash collision")
                return int(row["id"])
        finally:
            conn.close()

    def _reference_by_id(self, reference_id: int) -> AttachmentReference:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM attachment_references WHERE id = ?",
                (reference_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError(f"attachment reference disappeared: {reference_id}")
            return _reference_from_row(row)
        finally:
            conn.close()

    def _finish_success(
        self,
        reference_id: int,
        attempts: int,
        attachment_id: int,
        size: int,
        digest: str,
        target: Path,
    ) -> AttachmentReference:
        conn = self._connect()
        try:
            with conn:
                row = conn.execute(
                    """
                    UPDATE attachment_references
                    SET attachment_id = ?,
                        source_size = ?,
                        content_sha256 = ?,
                        storage_path = ?,
                        status = 'stored',
                        error_code = NULL,
                        error_message = NULL,
                        retryable = 0,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND attempts = ?
                    RETURNING *
                    """,
                    (
                        attachment_id,
                        size,
                        digest,
                        self._storage_path(target),
                        reference_id,
                        attempts,
                    ),
                ).fetchone()
                if row is not None:
                    return _reference_from_row(row)
        finally:
            conn.close()
        return self._wait_for_terminal_reference(reference_id)

    def _finish_error(
        self,
        reference_id: int,
        attempts: int,
        error_code: str,
        error_message: str,
        source_size: int | None = None,
        content_sha256: str | None = None,
    ) -> AttachmentReference:
        conn = self._connect()
        try:
            with conn:
                row = conn.execute(
                    """
                    UPDATE attachment_references
                    SET attachment_id = NULL,
                        source_size = ?,
                        content_sha256 = ?,
                        storage_path = NULL,
                        status = 'error',
                        error_code = ?,
                        error_message = ?,
                        retryable = 1,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND attempts = ?
                    RETURNING *
                    """,
                    (
                        source_size,
                        content_sha256,
                        error_code,
                        error_message,
                        reference_id,
                        attempts,
                    ),
                ).fetchone()
                if row is not None:
                    return _reference_from_row(row)
        finally:
            conn.close()
        return self._wait_for_terminal_reference(reference_id)

    def _wait_for_terminal_reference(self, reference_id: int) -> AttachmentReference:
        deadline = time.monotonic() + _PENDING_WAIT_TIMEOUT_SECONDS
        while True:
            reference = self._reference_by_id(reference_id)
            if reference.status != "pending":
                return reference
            if time.monotonic() >= deadline:
                return self._finish_pending_timeout(reference)
            time.sleep(_PENDING_WAIT_INTERVAL_SECONDS)

    def _finish_pending_timeout(
        self, reference: AttachmentReference
    ) -> AttachmentReference:
        conn = self._connect()
        try:
            with conn:
                row = conn.execute(
                    """
                    UPDATE attachment_references
                    SET attachment_id = NULL,
                        storage_path = NULL,
                        status = 'error',
                        error_code = 'pending_timeout',
                        error_message = 'superseded ingest did not reach a terminal status before timeout',
                        retryable = 1,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND attempts = ? AND status = 'pending'
                    RETURNING *
                    """,
                    (reference.reference_id, reference.attempts),
                ).fetchone()
                if row is not None:
                    return _reference_from_row(row)
        finally:
            conn.close()
        return self._wait_for_terminal_reference(reference.reference_id)

    def _copy_and_hash(self, source: Path, temp_path: Path) -> tuple[str, int]:
        temp_path.parent.mkdir(parents=True, exist_ok=True)
        hasher = hashlib.sha256()
        copied_size = 0
        with source.open("rb") as input_file, temp_path.open("wb") as output_file:
            for chunk in iter(lambda: input_file.read(_CHUNK_SIZE), b""):
                hasher.update(chunk)
                output_file.write(chunk)
                copied_size += len(chunk)
            output_file.flush()
            os.fsync(output_file.fileno())
        return hasher.hexdigest(), copied_size

    def _target_path(self, digest: str) -> Path:
        return self.asset_root / "sha256" / digest[:2] / digest

    def _temp_path(self) -> Path:
        return self.asset_root / "_tmp" / f"{uuid.uuid4().hex}.tmp"

    def _storage_path(self, target: Path) -> str:
        return target.relative_to(self.asset_root).as_posix()


def _reference_from_row(row: sqlite3.Row) -> AttachmentReference:
    return AttachmentReference(
        reference_id=int(row["id"]),
        account_id=row["account_id"],
        message_identity_hash=row["message_identity_hash"],
        source_path=row["source_path"],
        attachment_kind=row["attachment_kind"],
        resolver_version=row["resolver_version"],
        attachment_id=row["attachment_id"],
        source_size=row["source_size"],
        content_sha256=row["content_sha256"],
        storage_path=row["storage_path"],
        status=row["status"],
        error_code=row["error_code"],
        error_message=row["error_message"],
        retryable=bool(row["retryable"]),
        attempts=int(row["attempts"]),
    )


def _require_name(value: str, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")


def _same_stat(before: os.stat_result, after: os.stat_result) -> bool:
    return (
        before.st_size == after.st_size
        and before.st_mtime_ns == after.st_mtime_ns
        and before.st_ctime_ns == after.st_ctime_ns
        and before.st_ino == after.st_ino
    )


def _file_matches(path: Path, digest: str, size: int) -> bool:
    if not path.is_file() or path.stat().st_size != size:
        return False
    hasher = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(_CHUNK_SIZE), b""):
            hasher.update(chunk)
    return hasher.hexdigest() == digest


def _remove_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
