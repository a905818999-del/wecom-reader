from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from wecom_reader.db.message import _extract_mentions, get_messages


def test_extract_mentions_deduplicates_mentions() -> None:
    assert _extract_mentions("hi @alice and @bob, @alice") == ["@alice", "@bob"]


def test_extract_mentions_returns_empty_list_without_mentions() -> None:
    assert _extract_mentions("plain text") == []


def test_get_messages_returns_mentions_key_from_protobuf_content(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "message.db"
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            """
            CREATE TABLE message_table (
                message_id TEXT,
                server_id TEXT,
                sequence INTEGER,
                sender_id INTEGER,
                conversation_id TEXT,
                content_type INTEGER,
                send_time INTEGER,
                flag INTEGER,
                content BLOB,
                from_app_id TEXT
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO message_table (
                message_id, server_id, sequence, sender_id, conversation_id,
                content_type, send_time, flag, content, from_app_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("m1", "s1", 1, 1, "R:test", 2, 100, 0, b"plain hello", ""),
                ("m2", "s2", 2, 1, "R:test", 2, 101, 0, b"\x12\x0f@nickname hello", ""),
            ],
        )
        conn.commit()

    messages = get_messages(str(db_path), "R:test", limit=10)
    by_id = {message["message_id"]: message for message in messages}

    assert by_id["m1"]["mentions"] == []
    assert by_id["m2"]["mentions"] == ["@nickname"]
