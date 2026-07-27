"""Privacy-safe JSONL export for independent reader audits."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Any

from wecom_reader.db.message import MSG_TYPES, _parse_content
from wecom_reader.ledger_collector import iter_stable_message_records


AUDIT_FIELDS = (
    "account_hash",
    "conversation_hash",
    "message_id",
    "sequence",
    "timestamp",
    "direction",
    "sender_hash",
    "conversation_type",
    "message_type",
    "status",
    "content_hash",
    "resource_refs",
    "source",
    "parse_status",
)
AUDIT_SOURCES = {"db", "wal", "lookup", "index"}
AUDIT_PARSE_STATUSES = {"OK", "UNSUPPORTED", "UNVERIFIABLE", "ERROR"}
FULLY_PARSED_CONTENT_TYPES = {0, 2}
SOURCE_MANIFEST_NAME = ".wecom-reader-audit-source.json"


class SourceProvenanceError(RuntimeError):
    """Raised when a decrypted snapshot cannot be bound to its source account."""


@dataclass(frozen=True)
class AuditExportSummary:
    """Non-sensitive counts from an audit export."""

    record_count: int
    unique_record_count: int
    duplicate_count: int
    parse_status_counts: dict[str, int]
    message_type_counts: dict[str, int]
    contract_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable summary."""
        return asdict(self)


def stable_hash(value: str | bytes) -> str:
    """Return the WeCom audit contract's deterministic SHA-256 representation."""
    data = value if isinstance(value, bytes) else value.encode("utf-8")
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def canonical_audit_record_json(record: Mapping[str, Any]) -> str:
    """Serialize one MessageRecord v1 audit record in canonical JSONL form."""
    payload = dict(record)
    if tuple(payload) != AUDIT_FIELDS:
        raise ValueError("audit record has an invalid field set or order")
    if payload["source"] not in AUDIT_SOURCES:
        raise ValueError("audit record has an unsupported source")
    if payload["parse_status"] not in AUDIT_PARSE_STATUSES:
        raise ValueError("audit record has an unsupported parse_status")
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def audit_record_from_message(
    record: Mapping[str, Any],
    account_id: str,
    *,
    default_source: str = "db",
) -> dict[str, Any]:
    """Convert one reader row into the strict privacy-safe audit schema."""
    parse_status = "OK"
    conversation_id = _text_value(record.get("conversation_id"))
    sender_id = _text_value(record.get("sender_id"))
    raw_message_id = record.get("message_id")
    sequence = _optional_int(record.get("sequence"))
    timestamp = _optional_int(record.get("send_time"))
    content_type = record.get("content_type")

    if not conversation_id or sequence is _INVALID_INT or timestamp is _INVALID_INT:
        parse_status = "ERROR"
    sequence = None if sequence is _INVALID_INT else sequence
    timestamp = None if timestamp is _INVALID_INT else timestamp

    if content_type in MSG_TYPES:
        message_type = MSG_TYPES[content_type]
        try:
            raw_content = record.get("content")
            parsed_content = (
                raw_content
                if isinstance(raw_content, str)
                else _parse_content(raw_content)
            )
            content_hash = stable_hash(parsed_content)
        except Exception:
            parse_status = "ERROR"
            content_hash = _fallback_content_hash(record.get("content"))
        if parse_status == "OK" and content_type not in FULLY_PARSED_CONTENT_TYPES:
            parse_status = "UNVERIFIABLE"
    else:
        message_type = _text_value(content_type) or "unknown"
        content_hash = _fallback_content_hash(record.get("content"))
        if parse_status == "OK":
            parse_status = "UNSUPPORTED"

    source = _text_value(record.get("source")) or default_source
    if source not in AUDIT_SOURCES:
        source = default_source if default_source in AUDIT_SOURCES else "db"
        parse_status = "ERROR"

    message_id = ""
    if raw_message_id not in (None, ""):
        message_id = stable_hash(_text_value(raw_message_id))
    status_value = record.get("status")
    if status_value in (None, ""):
        status_value = record.get("flag")

    result = {
        "account_hash": stable_hash(account_id),
        "conversation_hash": stable_hash(conversation_id),
        "message_id": message_id,
        "sequence": sequence,
        "timestamp": timestamp,
        "direction": _direction(account_id, sender_id),
        "sender_hash": stable_hash(sender_id) if sender_id else "",
        "conversation_type": _conversation_type(conversation_id),
        "message_type": message_type,
        "status": _text_value(status_value),
        "content_hash": content_hash,
        "resource_refs": _resource_refs(record.get("resource_refs")),
        "source": source,
        "parse_status": parse_status,
    }
    assert tuple(result) == AUDIT_FIELDS
    return result


def write_audit_source_manifest(
    db_dir: str | os.PathLike[str] | None,
    decrypted_dir: str | os.PathLike[str],
) -> bool:
    """Atomically record privacy-safe provenance for a canonical account snapshot."""
    account_id = _account_id_from_db_dir(db_dir)
    if db_dir is None or account_id is None:
        return False
    source_dir = Path(db_dir)
    message_db = Path(decrypted_dir) / "message.db"
    if not message_db.is_file():
        return False
    payload = {
        "schema_version": 1,
        "account_hash": stable_hash(account_id),
        "db_dir_hash": stable_hash(_normalized_path(source_dir)),
        "message_db_sha256": _file_sha256(message_db),
    }
    manifest = Path(decrypted_dir) / SOURCE_MANIFEST_NAME
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=manifest.parent,
            prefix=f".{manifest.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(
                json.dumps(
                    payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, manifest)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    return True


def verify_audit_source_manifest(
    db_dir: str | os.PathLike[str] | None,
    decrypted_dir: str | os.PathLike[str],
) -> str:
    """Validate snapshot provenance and return the verified raw account identifier."""
    account_id = _account_id_from_db_dir(db_dir)
    if db_dir is None or account_id is None:
        raise SourceProvenanceError("source account directory is not canonical")
    source_dir = Path(db_dir)
    decrypted = Path(decrypted_dir)
    message_db = decrypted / "message.db"
    manifest = decrypted / SOURCE_MANIFEST_NAME
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SourceProvenanceError("source manifest is unavailable") from exc
    try:
        expected = {
            "schema_version": 1,
            "account_hash": stable_hash(account_id),
            "db_dir_hash": stable_hash(_normalized_path(source_dir)),
            "message_db_sha256": _file_sha256(message_db),
        }
    except OSError as exc:
        raise SourceProvenanceError("decrypted snapshot is unavailable") from exc
    if payload != expected:
        raise SourceProvenanceError("source manifest does not match the snapshot")
    return account_id


def export_audit_jsonl(
    db_path: str | os.PathLike[str],
    output_path: str | os.PathLike[str],
    account_id: str,
    *,
    source: str = "db",
) -> AuditExportSummary:
    """Stream a stable message snapshot to an atomically published JSONL file."""
    if not account_id:
        raise ValueError("account_id is required")
    if source not in AUDIT_SOURCES:
        raise ValueError("unsupported audit source")
    records = (
        audit_record_from_message(record, account_id, default_source=source)
        for record in iter_stable_message_records(db_path)
    )
    return write_audit_jsonl(output_path, records)


def write_audit_jsonl(
    output_path: str | os.PathLike[str],
    records: Iterable[Mapping[str, Any]],
) -> AuditExportSummary:
    """Write audit records through a same-directory temporary file."""
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    key_db_path: Path | None = None
    key_db: sqlite3.Connection | None = None
    record_count = 0
    unique_record_count = 0
    duplicate_count = 0
    parse_status_counts: Counter[str] = Counter()
    message_type_counts: Counter[str] = Counter()
    try:
        key_file = tempfile.NamedTemporaryFile(
            dir=output.parent,
            prefix=f".{output.name}.keys.",
            suffix=".sqlite3",
            delete=False,
        )
        key_db_path = Path(key_file.name)
        key_file.close()
        key_db = sqlite3.connect(key_db_path)
        key_db.execute("PRAGMA journal_mode = OFF")
        key_db.execute("PRAGMA synchronous = OFF")
        key_db.execute("CREATE TABLE stable_keys (key TEXT PRIMARY KEY)")
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=output.parent,
            prefix=f".{output.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            for record in records:
                payload = dict(record)
                canonical = canonical_audit_record_json(payload)
                stable_key = json.dumps(
                    _stable_key(payload),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                cursor = key_db.execute(
                    "INSERT OR IGNORE INTO stable_keys (key) VALUES (?)",
                    (stable_key,),
                )
                if cursor.rowcount == 0:
                    duplicate_count += 1
                parse_status_counts[payload["parse_status"]] += 1
                message_type_counts[str(payload["message_type"])] += 1
                handle.write(canonical)
                handle.write("\n")
                record_count += 1
            handle.flush()
            os.fsync(handle.fileno())
        unique_record_count = int(
            key_db.execute("SELECT COUNT(*) FROM stable_keys").fetchone()[0]
        )
        if record_count == 0:
            raise ValueError("refusing to publish an empty audit export")
        os.replace(temp_path, output)
        temp_path = None
    finally:
        if key_db is not None:
            key_db.close()
        if key_db_path is not None:
            key_db_path.unlink(missing_ok=True)
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)

    return AuditExportSummary(
        record_count=record_count,
        unique_record_count=unique_record_count,
        duplicate_count=duplicate_count,
        parse_status_counts=dict(sorted(parse_status_counts.items())),
        message_type_counts=dict(sorted(message_type_counts.items())),
    )


class _InvalidInt:
    pass


_INVALID_INT = _InvalidInt()


def _optional_int(value: Any) -> int | None | _InvalidInt:
    if value in (None, ""):
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        return _INVALID_INT
    return value


def _text_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _fallback_content_hash(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return stable_hash(value)
    return stable_hash(_text_value(value))


def _direction(account_id: str, sender_id: str) -> str:
    if not sender_id:
        return ""
    return "outgoing" if sender_id == account_id else "incoming"


def _conversation_type(conversation_id: str) -> str:
    types = {
        "R:": "group",
        "S:": "single",
        "M:": "wechat_contact",
        "O:": "app",
        "Y:": "system",
    }
    return next(
        (name for prefix, name in types.items() if conversation_id.startswith(prefix)),
        "other" if conversation_id else "",
    )


def _resource_refs(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, (str, bytes)):
        values = [value]
    else:
        try:
            values = list(value)
        except TypeError:
            values = [value]
    hashes = {
        stable_hash(_text_value(item)) for item in values if item not in (None, "")
    }
    return sorted(hashes)


def _stable_key(record: Mapping[str, Any]) -> tuple[Any, ...]:
    if record["message_id"]:
        return (
            record["account_hash"],
            record["conversation_hash"],
            "message_id",
            record["message_id"],
        )
    if record["sequence"] is not None:
        return (
            record["account_hash"],
            record["conversation_hash"],
            "sequence",
            record["sequence"],
        )
    return (
        record["account_hash"],
        record["conversation_hash"],
        "fingerprint",
        record["timestamp"],
        record["direction"],
        record["sender_hash"],
        record["message_type"],
        record["content_hash"],
    )


def _account_id_from_db_dir(
    db_dir: str | os.PathLike[str] | None,
) -> str | None:
    if db_dir is None:
        return None
    path = Path(db_dir)
    account_id = path.parent.name
    if (
        path.name.lower() != "data"
        or not account_id.isdigit()
        or not (path / "message.db").is_file()
    ):
        return None
    return account_id


def _normalized_path(path: Path) -> str:
    return str(path.resolve()).replace("\\", "/").casefold()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"
