"""Message (消息) queries for decrypted WeCom databases.

Actual schema (from decrypted message.db):
  message_table: message_id, server_id, sequence, sender_id, conversation_id,
                 content_type, send_time, flag, content, devinfo, from_app_id,
                 msg_from_devinfo, extra_content, local_extra_content, client_id, ...
  message_small_table: same schema
  kf_message_tableV1: same schema (客服消息)
"""

import re
import sqlite3
from typing import Optional

MESSAGE_TABLES = ("message_table", "message_small_table", "kf_message_tableV1")

MSG_TYPES = {
    0: "text",
    2: "text",
    4: "image",
    7: "voice",
    15: "image/file",
    38: "app_message",
    40: "call",
    503: "status",
    1011: "meeting",
}

MENTION_RE = re.compile(r"(?<![\w@])@[\w\u4e00-\u9fff]+(?:[.-][\w\u4e00-\u9fff]+)*")


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _parse_content(raw) -> str:
    """Parse message content, handling binary/protobuf data.

    WeCom message content is protobuf-encoded. The actual text is typically
    embedded as a length-delimited string after field tags like 0x12.
    We try multiple strategies to extract readable text.
    """
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, bytes):
        # Strategy 1: Try UTF-8 decode and extract Chinese/ASCII runs
        try:
            s = raw.decode("utf-8", errors="ignore")
            # Extract runs of Chinese chars, ASCII, and common punctuation
            chunks = re.findall(
                r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef"
                r"a-zA-Z0-9@]"
                r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef"
                r'a-zA-Z0-9 .,;:!?，。！？、；：\u2018\u2019\u201c\u201d（）【】《》\\-_/\\@#\n\r\t]*',
                s,
            )
            # Filter out very short chunks (likely noise)
            texts = [c.strip() for c in chunks if len(c.strip()) >= 2]
            if texts:
                # Join consecutive text fragments
                result = " ".join(texts)
                # Clean up: remove leading garbage bytes
                result = re.sub(r"^[\x00-\x1f]{1,10}", "", result)
                if result.strip():
                    return result.strip()
        except (UnicodeDecodeError, ValueError):
            pass

        # Strategy 2: Try latin1 decode (preserves all bytes)
        try:
            s = raw.decode("latin1")
            chunks = re.findall(
                r"[\u4e00-\u9fff]|[a-zA-Z0-9 .,;:!?]+",
                s,
            )
            texts = [c.strip() for c in chunks if len(c.strip()) >= 3]
            if texts:
                return " ".join(texts)
        except Exception:
            pass

        return f"[binary {len(raw)} bytes]"
    return str(raw).strip()


def _extract_mentions(content: str) -> list[str]:
    """Extract distinct, well-formed @mention tokens in encounter order."""
    return list(dict.fromkeys(MENTION_RE.findall(content)))


def _existing_message_tables(conn: sqlite3.Connection) -> list[str]:
    return [table for table in MESSAGE_TABLES if _table_exists(conn, table)]


def get_messages(
    db_path: str,
    conversation_id: str,
    limit: int = 50,
    offset: int = 0,
    since: Optional[int] = None,
    until: Optional[int] = None,
    msg_type: Optional[int] = None,
) -> list[dict]:
    """Get messages for a specific conversation.

    Uses actual column names from decrypted WeCom schema:
      sender_id, conversation_id, content_type, send_time, content, message_id, server_id
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        tables = _existing_message_tables(conn)
        if not tables:
            return []

        selects = []
        params = []
        for table in tables:
            where = "conversation_id = ?"
            table_params: list = [conversation_id]

            if since is not None:
                where += " AND send_time >= ?"
                table_params.append(since)
            if until is not None:
                where += " AND send_time < ?"
                table_params.append(until)
            if msg_type is not None:
                where += " AND content_type = ?"
                table_params.append(msg_type)

            selects.append(
                f'SELECT message_id, server_id, sequence, sender_id, '
                f'conversation_id, content_type, send_time, flag, content, '
                f'from_app_id FROM "{table}" WHERE {where}'
            )
            params.extend(table_params)

        query = (
            "SELECT * FROM ("
            + " UNION ALL ".join(selects)
            + ") ORDER BY sequence DESC LIMIT ? OFFSET ?"
        )
        params.extend([limit, offset])

        messages = []
        for row in conn.execute(query, params):
            ct = row["content_type"]
            content = _parse_content(row["content"])
            messages.append({
                "message_id": row["message_id"],
                "server_id": row["server_id"],
                "sequence": row["sequence"],
                "sender_id": row["sender_id"],
                "conversation_id": row["conversation_id"],
                "content_type": ct,
                "type_name": MSG_TYPES.get(ct, f"type_{ct}"),
                "send_time": row["send_time"],
                "flag": row["flag"],
                "content": content,
                "mentions": _extract_mentions(content),
                "from_app_id": row["from_app_id"],
            })
        return messages
    finally:
        conn.close()


def search_messages(
    db_path: str,
    keyword: str,
    conversation_id: Optional[str] = None,
    limit: int = 50,
) -> list[dict]:
    """Search messages by keyword in content field."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        tables = _existing_message_tables(conn)
        if not tables:
            return []

        selects = []
        params = []
        for table in tables:
            where = "content LIKE ?"
            table_params: list = [f"%{keyword}%"]
            if conversation_id:
                where += " AND conversation_id = ?"
                table_params.append(conversation_id)

            selects.append(
                f'SELECT message_id, server_id, sequence, sender_id, '
                f'conversation_id, content_type, send_time, flag, content '
                f'FROM "{table}" WHERE {where}'
            )
            params.extend(table_params)

        query = (
            "SELECT * FROM ("
            + " UNION ALL ".join(selects)
            + ") ORDER BY sequence DESC LIMIT ?"
        )
        params.append(limit)

        messages = []
        for row in conn.execute(query, params):
            ct = row["content_type"]
            content = _parse_content(row["content"])
            messages.append({
                "message_id": row["message_id"],
                "server_id": row["server_id"],
                "sequence": row["sequence"],
                "sender_id": row["sender_id"],
                "conversation_id": row["conversation_id"],
                "content_type": ct,
                "type_name": MSG_TYPES.get(ct, f"type_{ct}"),
                "send_time": row["send_time"],
                "flag": row["flag"],
                "content": content,
                "mentions": _extract_mentions(content),
            })
        return messages
    finally:
        conn.close()


def get_message_count(db_path: str, conversation_id: str) -> int:
    """Get total message count for a conversation."""
    conn = sqlite3.connect(db_path)
    try:
        total = 0
        for table in MESSAGE_TABLES:
            if not _table_exists(conn, table):
                continue
            row = conn.execute(
                f'SELECT COUNT(*) FROM "{table}" WHERE conversation_id = ?',
                (conversation_id,),
            ).fetchone()
            total += row[0] if row else 0
        return total
    finally:
        conn.close()
