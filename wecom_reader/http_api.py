"""JSON HTTP facade for WeComReader."""

import base64
import binascii
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Flask, Response, request

from .db.message import MESSAGE_TABLES, MSG_TYPES, _parse_content

CURSOR_VERSION = 1
DEFAULT_LIMIT = 50
MAX_LIMIT = 500


class SnapshotChangedError(RuntimeError):
    pass


def create_app(reader: Any) -> Flask:
    """Create a Flask JSON API for an initialized WeComReader-like object."""
    app = Flask(__name__)
    app.config["WECOM_READER"] = reader
    app.config["LAST_REFRESH_RESULT"] = None
    app.config["REFRESH_IN_PROGRESS"] = False
    refresh_run_lock = threading.Lock()
    refresh_state_lock = threading.Lock()

    @app.get("/api/v1/health")
    def health() -> Response:
        with refresh_state_lock:
            last_refresh = app.config["LAST_REFRESH_RESULT"]
            refresh_in_progress = app.config["REFRESH_IN_PROGRESS"]
        return _json_response(
            _health_payload(reader, last_refresh, refresh_in_progress)
        )

    @app.get("/api/v1/sessions")
    def sessions() -> Response:
        try:
            limit = _parse_limit(request.args.get("limit"))
        except ValueError as exc:
            return _bad_request("invalid_limit", str(exc))

        keyword = request.args.get("q")
        result = reader.list_sessions(limit=limit, keyword=keyword)
        return _json_response({"sessions": result, "count": len(result)})

    @app.get("/api/v1/messages")
    def messages() -> Response:
        conversation_id = request.args.get("conversation_id")
        if not conversation_id:
            return _bad_request(
                "missing_conversation_id", "conversation_id is required"
            )

        try:
            limit = _parse_limit(request.args.get("limit"))
            cursor = _decode_cursor(request.args.get("cursor"))
        except ValueError as exc:
            return _bad_request("invalid_request", str(exc))

        try:
            page = _query_messages(reader, conversation_id, limit, cursor)
        except SnapshotChangedError as exc:
            return _json_response(
                {"error": {"code": "snapshot_changed", "message": str(exc)}}, 409
            )
        except sqlite3.Error as exc:
            return _json_response(
                {"error": {"code": "message_db_error", "message": str(exc)}}, 500
            )

        return _json_response(page)

    @app.get("/api/v1/search")
    def search() -> Response:
        q = request.args.get("q")
        if not q:
            return _bad_request("missing_q", "q is required")

        try:
            limit = _parse_limit(request.args.get("limit"))
        except ValueError as exc:
            return _bad_request("invalid_limit", str(exc))

        conversation_id = request.args.get("conversation_id")
        result = reader.search_messages(q, conversation_id=conversation_id, limit=limit)
        return _json_response({"results": result, "count": len(result)})

    @app.post("/api/v1/refresh")
    def refresh() -> Response:
        with refresh_run_lock:
            with refresh_state_lock:
                app.config["REFRESH_IN_PROGRESS"] = True
            started_at = _utc_now()
            try:
                result = reader.init(verbose=False)
                payload = {
                    "started_at": started_at,
                    "finished_at": _utc_now(),
                    "ok": (
                        bool(result.get("success"))
                        if isinstance(result, dict)
                        else True
                    ),
                    "result": result,
                    "error": None,
                }
            except Exception as exc:
                payload = {
                    "started_at": started_at,
                    "finished_at": _utc_now(),
                    "ok": False,
                    "result": None,
                    "error": str(exc),
                }

            with refresh_state_lock:
                app.config["LAST_REFRESH_RESULT"] = payload
                app.config["REFRESH_IN_PROGRESS"] = False
        status = 200 if payload["ok"] else 500
        return _json_response(payload, status)

    return app


def _query_messages(
    reader: Any,
    conversation_id: str,
    limit: int,
    cursor: tuple[int, str, str, str, int, str] | None,
) -> dict[str, Any]:
    db_path = os.path.join(reader.decrypted_dir, "message.db")
    if not os.path.isfile(db_path):
        return {"messages": [], "count": 0, "next_cursor": None}

    snapshot = _snapshot_fingerprint(db_path)
    if cursor is not None and cursor[5] != snapshot:
        raise SnapshotChangedError(
            "message snapshot changed; restart pagination without a cursor"
        )

    conn = sqlite3.connect(f"{Path(db_path).resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        selects = []
        params: list[Any] = []
        for table in MESSAGE_TABLES:
            if not _table_exists(conn, table):
                continue
            selects.append(
                f"SELECT message_id, server_id, sequence, sender_id, conversation_id, "
                f"content_type, send_time, flag, content, from_app_id, "
                f"? AS source_table, "
                f"COALESCE(CAST(sequence AS INTEGER), -1) AS cursor_sequence, "
                f"COALESCE(CAST(message_id AS TEXT), '') AS cursor_message_id, "
                f"COALESCE(CAST(server_id AS TEXT), '') AS cursor_server_id, "
                f"rowid AS source_rowid "
                f'FROM "{table}" WHERE conversation_id = ?'
            )
            params.extend([table, conversation_id])

        if not selects:
            return {"messages": [], "count": 0, "next_cursor": None}

        union_sql = " UNION ALL ".join(selects)
        query = f"SELECT * FROM ({union_sql})"
        if cursor is not None:
            query += (
                " WHERE (cursor_sequence, source_table, cursor_message_id, "
                "cursor_server_id, source_rowid) "
                "> (?, ?, ?, ?, ?)"
            )
            params.extend(cursor[:5])
        query += (
            " ORDER BY cursor_sequence ASC, source_table ASC, "
            "cursor_message_id ASC, cursor_server_id ASC, source_rowid ASC "
            "LIMIT ?"
        )
        params.append(limit + 1)

        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()

    if _snapshot_fingerprint(db_path) != snapshot:
        raise SnapshotChangedError(
            "message snapshot changed during pagination; restart without a cursor"
        )

    page_rows = rows[:limit]
    messages = [_message_from_row(row) for row in page_rows]
    next_cursor = None
    if len(rows) > limit and page_rows:
        last = page_rows[-1]
        next_cursor = _encode_cursor(
            (
                last["cursor_sequence"],
                last["source_table"],
                last["cursor_message_id"],
                last["cursor_server_id"],
                last["source_rowid"],
                snapshot,
            )
        )
    return {"messages": messages, "count": len(messages), "next_cursor": next_cursor}


def _message_from_row(row: sqlite3.Row) -> dict[str, Any]:
    content_type = row["content_type"]
    return {
        "message_id": row["message_id"],
        "server_id": row["server_id"],
        "sequence": row["sequence"],
        "source_table": row["source_table"],
        "source_rowid": row["source_rowid"],
        "raw_identity": {
            "source_table": row["source_table"],
            "message_id": row["message_id"],
            "server_id": row["server_id"],
            "sequence": row["sequence"],
        },
        "sender_id": row["sender_id"],
        "conversation_id": row["conversation_id"],
        "content_type": content_type,
        "type_name": MSG_TYPES.get(content_type, f"type_{content_type}"),
        "send_time": row["send_time"],
        "flag": row["flag"],
        "content": _parse_content(row["content"]),
        "from_app_id": row["from_app_id"],
    }


def _health_payload(
    reader: Any,
    last_refresh: dict[str, Any] | None,
    refresh_in_progress: bool = False,
) -> dict[str, Any]:
    decrypted_dir = getattr(reader, "decrypted_dir", None)
    message_db = os.path.join(decrypted_dir, "message.db") if decrypted_dir else None
    source_mtime = _max_source_mtime(getattr(reader, "db_dir", None))
    snapshot_mtime = _mtime(message_db)
    source_wal_files = _wal_files(getattr(reader, "db_dir", None))
    snapshot_wal_files = _wal_files(decrypted_dir)
    refresh_failed = bool(last_refresh and not last_refresh.get("ok"))
    refresh_result = last_refresh["result"] if last_refresh else None
    wal_degraded = bool(
        isinstance(refresh_result, dict)
        and (refresh_result.get("wal_degraded") or refresh_result.get("wal_failed"))
    )
    degraded = bool(
        source_wal_files or snapshot_wal_files or wal_degraded or refresh_failed
    )

    return {
        "ok": not degraded,
        "degraded": degraded,
        "snapshot": {
            "path": message_db,
            "mtime": snapshot_mtime,
        },
        "source": {
            "path": getattr(reader, "db_dir", None),
            "mtime": source_mtime,
        },
        "lag_seconds_estimate": (
            max(0.0, source_mtime - snapshot_mtime)
            if source_mtime is not None and snapshot_mtime is not None
            else None
        ),
        "wal_present": bool(source_wal_files or snapshot_wal_files),
        "wal_files": {
            "source": source_wal_files,
            "snapshot": snapshot_wal_files,
        },
        "refresh_in_progress": refresh_in_progress,
        "last_refresh": last_refresh,
        "checkpoint": None,
        "message_gaps": None,
        "attachment_metrics": None,
    }


def _parse_limit(raw: str | None) -> int:
    if raw is None:
        return DEFAULT_LIMIT
    try:
        limit = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("limit must be an integer") from exc
    if limit < 1 or limit > MAX_LIMIT:
        raise ValueError("limit must be between 1 and 500")
    return limit


def _encode_cursor(key: tuple[int, str, str, str, int, str]) -> str:
    payload = {
        "v": CURSOR_VERSION,
        "sequence": key[0],
        "source_table": key[1],
        "message_id": key[2],
        "server_id": key[3],
        "source_rowid": key[4],
        "snapshot": key[5],
    }
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(raw: str | None) -> tuple[int, str, str, str, int, str] | None:
    if not raw:
        return None
    try:
        padded = raw + "=" * (-len(raw) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except (
        binascii.Error,
        UnicodeDecodeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        raise ValueError("cursor must be opaque base64url JSON") from exc

    if not isinstance(payload, dict):
        raise ValueError("cursor payload must be an object")
    if payload.get("v") != CURSOR_VERSION:
        raise ValueError("cursor version is not supported")
    required = (
        "sequence",
        "source_table",
        "message_id",
        "server_id",
        "source_rowid",
        "snapshot",
    )
    if any(name not in payload for name in required):
        raise ValueError("cursor is missing required key fields")
    if payload["source_table"] not in MESSAGE_TABLES:
        raise ValueError("cursor source_table is invalid")
    if not isinstance(payload["message_id"], str) or not isinstance(
        payload["server_id"], str
    ):
        raise ValueError("cursor identity fields are invalid")
    if isinstance(payload["sequence"], bool):
        raise ValueError("cursor sequence is invalid")
    if isinstance(payload["source_rowid"], bool):
        raise ValueError("cursor source_rowid is invalid")
    if not isinstance(payload["snapshot"], str) or not payload["snapshot"]:
        raise ValueError("cursor snapshot is invalid")
    try:
        sequence = int(payload["sequence"])
        source_rowid = int(payload["source_rowid"])
    except (TypeError, ValueError) as exc:
        raise ValueError("cursor numeric fields are invalid") from exc

    return (
        sequence,
        payload["source_table"],
        payload["message_id"],
        payload["server_id"],
        source_rowid,
        payload["snapshot"],
    )


def _bad_request(code: str, message: str) -> tuple[Response, int]:
    return _json_response({"error": {"code": code, "message": message}}, 400), 400


def _json_response(data: Any, status: int = 200) -> Response:
    return Response(
        json.dumps(data, ensure_ascii=False, default=str),
        status=status,
        mimetype="application/json",
    )


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _mtime(path: str | None) -> float | None:
    if not path:
        return None
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def _max_source_mtime(path: str | None) -> float | None:
    if not path:
        return None
    try:
        if os.path.isfile(path):
            return os.path.getmtime(path)
    except OSError:
        return None

    mtimes = []
    for root, _dirs, files in os.walk(path):
        for name in files:
            if (
                name.endswith(".db")
                or name.endswith(".db-wal")
                or name.endswith(".db-shm")
            ):
                try:
                    mtimes.append(os.path.getmtime(os.path.join(root, name)))
                except OSError:
                    continue
    return max(mtimes) if mtimes else None


def _wal_files(path: str | None) -> list[str]:
    if not path or not os.path.isdir(path):
        return []
    result = []
    for root, _dirs, files in os.walk(path):
        for name in files:
            if not name.endswith(".db-wal"):
                continue
            wal_path = os.path.join(root, name)
            try:
                if os.path.getsize(wal_path) > 0:
                    result.append(os.path.relpath(wal_path, path))
            except OSError:
                continue
    return sorted(result)


def _snapshot_fingerprint(db_path: str) -> str:
    db_stat = os.stat(db_path)
    wal_path = f"{db_path}-wal"
    try:
        wal_stat = os.stat(wal_path)
        wal_parts = (wal_stat.st_mtime_ns, wal_stat.st_size)
    except FileNotFoundError:
        wal_parts = (-1, -1)
    return ":".join(
        str(value)
        for value in (
            db_stat.st_mtime_ns,
            db_stat.st_size,
            wal_parts[0],
            wal_parts[1],
        )
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
