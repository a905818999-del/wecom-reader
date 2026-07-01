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
    14: "image",        # screenshot / forwarded image (parsed content has Screenshot_xxx.png)
    15: "image/file",
    38: "app_message",
    40: "call",
    503: "status",
    1011: "meeting",
}


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
        # Strategy 1: Try to parse protobuf structure and extract text
        try:
            texts = _extract_protobuf_text(raw)
            if texts:
                result = " ".join(texts)
                # Clean up: remove leading garbage bytes
                result = re.sub(r"^[\x00-\x1f]{1,10}", "", result)
                if result.strip():
                    return result.strip()
        except Exception:  # pragma: no cover  (defensive — _extract_protobuf_text swallows its own errors)
            pass

        # Strategy 2: Try UTF-8 decode and extract Chinese/ASCII runs
        try:
            s = raw.decode("utf-8", errors="ignore")
            # Extract runs of Chinese chars, ASCII, and common punctuation
            chunks = re.findall(
                r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef"
                r"a-zA-Z0-9]"
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
        except (UnicodeDecodeError, ValueError):  # pragma: no cover  (errors='ignore' never raises)
            pass

        # Strategy 3: Try GBK decode (common for Chinese Windows apps)
        try:
            s = raw.decode("gbk", errors="ignore")
            # Extract runs of Chinese chars, ASCII, and common punctuation
            chunks = re.findall(
                r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef"
                r"a-zA-Z0-9]"
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
        except (UnicodeDecodeError, ValueError):  # pragma: no cover  (errors='ignore' never raises)
            pass

        # Strategy 4: Try latin1 decode (preserves all bytes)
        try:
            s = raw.decode("latin1")
            chunks = re.findall(
                r"[\u4e00-\u9fff]|[a-zA-Z0-9 .,;:!?]+",
                s,
            )
            texts = [c.strip() for c in chunks if len(c.strip()) >= 3]
            if texts:
                return " ".join(texts)  # pragma: no cover  (UTF-8/GBK strategies catch ASCII first)
        except Exception:  # pragma: no cover  (latin1 decode cannot fail)
            pass

        return f"[binary {len(raw)} bytes]"
    return str(raw).strip()  # pragma: no cover  (SQLite raw values are None/str/bytes only)


def _is_pure_text(text: str) -> bool:
    """Check if string looks like pure readable text (no control chars except newline/tab)."""
    for c in text:
        if c in '\n\r\t':
            continue
        if ord(c) < 32:
            return False
    return True


def _extract_protobuf_text(data: bytes, max_depth: int = 5) -> list[str]:
    """Extract text strings from protobuf-encoded data.
    
    Recursively parses protobuf structure to find text content.
    """
    if max_depth <= 0 or len(data) < 2:
        return []
    
    texts = []
    i = 0
    
    while i < len(data):
        # Read tag
        if i >= len(data):  # pragma: no cover  (loop condition already checks len(data))
            break
        tag = data[i]
        i += 1
        
        # Extract field number and wire type
        field_number = tag >> 3
        wire_type = tag & 0x07
        
        if wire_type == 0:  # Varint
            # Read varint
            while i < len(data) and data[i] & 0x80:
                i += 1
            i += 1
        elif wire_type == 1:  # 64-bit
            i += 8
        elif wire_type == 2:  # Length-delimited
            # Read length
            length = 0
            shift = 0
            while i < len(data):
                byte = data[i]
                i += 1
                length |= (byte & 0x7f) << shift
                shift += 7
                if not (byte & 0x80):
                    break
            
            # Read data
            if i + length <= len(data):
                chunk = data[i:i+length]
                i += length

                # If chunk starts with a protobuf tag (0x08-0x3f with valid wire type),
                # try nested parsing FIRST to avoid decoding protobuf bytes as text.
                # This prevents false positives like 0x40 (length byte) being decoded as '@'.
                if len(chunk) >= 2:
                    first = chunk[0]
                    first_fn = first >> 3
                    first_wt = first & 0x07
                    if 1 <= first_fn <= 15 and first_wt == 2:
                        # Looks like a protobuf length-delimited field, try nested first
                        nested = _extract_protobuf_text(chunk, max_depth - 1)
                        if nested:
                            texts.extend(nested)
                            continue

                # Try UTF-8 first (most common for WeCom)
                try:
                    text = chunk.decode("utf-8", errors="strict")
                    if _is_pure_text(text):
                        if re.search(r"[\u4e00-\u9fff]", text) or re.search(r"[a-zA-Z]{2,}", text):
                            texts.append(text.strip())
                            continue
                except (UnicodeDecodeError, ValueError):
                    pass

                # Try GBK
                try:
                    text = chunk.decode("gbk", errors="strict")
                    if _is_pure_text(text):
                        if re.search(r"[\u4e00-\u9fff]", text) or re.search(r"[a-zA-Z]{2,}", text):
                            texts.append(text.strip()); continue  # pragma: no cover  (UTF-8 catches Chinese first; GBK fallback rare on non-GBK locales)
                except (UnicodeDecodeError, ValueError):
                    pass

                # Not pure text, try nested protobuf
                if len(chunk) >= 2:
                    nested = _extract_protobuf_text(chunk, max_depth - 1)
                    texts.extend(nested)
            else:
                break
        elif wire_type == 5:  # 32-bit
            i += 4
        else:
            break
    
    return texts


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

    Queries all message tables (message_table, message_small_table,
    kf_message_tableV1) via UNION ALL and applies LIMIT/OFFSET on the
    combined result set, so the paging window spans the most recent
    messages regardless of which physical table they live in.

    Uses actual column names from decrypted WeCom schema:
      sender_id, conversation_id, content_type, send_time, content, message_id, server_id
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        # Collect the tables that actually exist; otherwise UNION ALL will fail.
        present_tables = [t for t in MESSAGE_TABLES if _table_exists(conn, t)]
        if not present_tables:
            return []

        select_cols = (
            "message_id, server_id, sequence, sender_id, conversation_id, "
            "content_type, send_time, flag, content, from_app_id"
        )

        where_clauses = ["conversation_id = ?"]
        params: list = [conversation_id]

        if since is not None:
            where_clauses.append("send_time >= ?")
            params.append(since)
        if until is not None:
            where_clauses.append("send_time < ?")
            params.append(until)
        if msg_type is not None:
            where_clauses.append("content_type = ?")
            params.append(msg_type)

        where_sql = " AND ".join(where_clauses)

        # Build UNION ALL across all present tables; order and paginate once.
        union_sql = " UNION ALL ".join(
            f'SELECT {select_cols} FROM "{t}" WHERE {where_sql}' for t in present_tables
        )
        query = (
            f"SELECT * FROM ({union_sql}) "
            f"ORDER BY sequence DESC LIMIT ? OFFSET ?"
        )
        # params are shared across each UNION arm; bind once at the outer level
        # by appending the LIMIT/OFFSET values. SQLite rebinds them per arm
        # which is fine because the placeholders are identical.
        union_params = list(params) * len(present_tables)
        union_params.extend([limit, offset])

        messages = []
        for row in conn.execute(query, union_params):
            ct = row["content_type"]
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
                "content": _parse_content(row["content"]),
                "from_app_id": row["from_app_id"],
            })

        # Return ascending by sequence (oldest first within the requested window).
        messages.sort(key=lambda m: m.get("sequence", 0), reverse=False)
        return messages
    finally:
        conn.close()


def search_messages(
    db_path: str,
    keyword: str,
    conversation_id: Optional[str] = None,
    limit: int = 50,
) -> list[dict]:
    """Search messages by keyword in content field.

    Searches across all message tables via UNION ALL and applies LIMIT
    on the combined result so results aren't biased toward whichever
    table happens to be queried first.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        present_tables = [t for t in MESSAGE_TABLES if _table_exists(conn, t)]
        if not present_tables:
            return []

        select_cols = (
            "message_id, server_id, sequence, sender_id, conversation_id, "
            "content_type, send_time, flag, content"
        )

        where_clauses = ["content LIKE ?"]
        base_params: list = [f"%{keyword}%"]
        if conversation_id:
            where_clauses.append("conversation_id = ?")
            base_params.append(conversation_id)
        where_sql = " AND ".join(where_clauses)

        union_sql = " UNION ALL ".join(
            f'SELECT {select_cols} FROM "{t}" WHERE {where_sql}' for t in present_tables
        )
        query = (
            f"SELECT * FROM ({union_sql}) "
            f"ORDER BY sequence DESC LIMIT ?"
        )
        union_params = list(base_params) * len(present_tables)
        union_params.append(limit)

        messages = []
        for row in conn.execute(query, union_params):
            ct = row["content_type"]
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
                "content": _parse_content(row["content"]),
            })

        messages.sort(key=lambda m: m.get("sequence", 0), reverse=False)
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
