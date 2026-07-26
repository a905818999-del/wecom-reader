from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from wecom_reader.db.message import (
    _extract_mentions,
    _parse_content,
    get_messages,
    search_messages,
)


def _create_message_db(db_path: Path, rows: list[tuple]) -> None:
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
            rows,
        )
        conn.commit()


def test_extract_mentions_handles_multiple_names_and_deduplicates() -> None:
    content = "hi @alice and @张三, then @alice.smith and @alice"

    assert _extract_mentions(content) == ["@alice", "@张三", "@alice.smith"]


def test_extract_mentions_ignores_malformed_tokens() -> None:
    content = "plain @ and @@double plus mail@example.com"

    assert _extract_mentions(content) == []


def test_get_messages_adds_mentions_without_changing_parsed_content(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "message.db"
    raw_content = b"\x12\x16@alice hello @bob"
    malformed_content = b"\xff\x00@"
    _create_message_db(
        db_path,
        [
            ("m1", "s1", 1, 1, "R:test", 2, 100, 0, raw_content, ""),
            ("m2", "s2", 2, 1, "R:test", 2, 101, 0, malformed_content, ""),
        ],
    )

    messages = get_messages(str(db_path), "R:test", limit=10)
    by_id = {message["message_id"]: message for message in messages}

    assert by_id["m1"]["content"] == _parse_content(raw_content)
    assert by_id["m1"]["mentions"] == ["@alice", "@bob"]
    assert by_id["m2"]["content"] == _parse_content(malformed_content)
    assert by_id["m2"]["mentions"] == []


def test_search_messages_adds_mentions_to_text_content(tmp_path: Path) -> None:
    db_path = tmp_path / "message.db"
    content = "hello @alice and @bob"
    _create_message_db(
        db_path,
        [("m1", "s1", 1, 1, "R:test", 2, 100, 0, content, "")],
    )

    messages = search_messages(str(db_path), "hello")

    assert messages[0]["content"] == content
    assert messages[0]["mentions"] == ["@alice", "@bob"]
