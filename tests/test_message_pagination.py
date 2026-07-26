import sqlite3

from wecom_reader.db.message import get_messages, search_messages


MESSAGE_TABLE_SCHEMA = """
    CREATE TABLE {table} (
        message_id INTEGER,
        server_id INTEGER,
        sequence INTEGER,
        sender_id TEXT,
        conversation_id TEXT,
        content_type INTEGER,
        send_time INTEGER,
        flag INTEGER,
        content TEXT,
        from_app_id INTEGER
    )
"""


def _create_message_table(conn: sqlite3.Connection, table: str) -> None:
    conn.execute(MESSAGE_TABLE_SCHEMA.format(table=table))


def _insert_message(
    conn: sqlite3.Connection,
    table: str,
    message_id: int,
    sequence: int,
    *,
    conversation_id: str = "R:1",
    content_type: int = 2,
    send_time: int | None = None,
    content: str | None = None,
) -> None:
    conn.execute(
        f'INSERT INTO "{table}" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (
            message_id,
            1000 + message_id,
            sequence,
            "sender",
            conversation_id,
            content_type,
            sequence if send_time is None else send_time,
            0,
            f"body {sequence}" if content is None else content,
            0,
        ),
    )


def _message_db(tmp_path, tables=("message_table", "message_small_table")):
    db_path = tmp_path / "message.db"
    conn = sqlite3.connect(db_path)
    try:
        for table in tables:
            _create_message_table(conn, table)
        conn.commit()
    finally:
        conn.close()
    return db_path


def _sequences(messages):
    return [message["sequence"] for message in messages]


def test_get_messages_applies_offset_after_global_sequence_order(tmp_path):
    db_path = _message_db(tmp_path)
    conn = sqlite3.connect(db_path)
    try:
        for sequence in (100, 90, 80, 70, 60):
            _insert_message(conn, "message_table", sequence, sequence)
        for sequence in (95, 85, 75, 65, 55):
            _insert_message(conn, "message_small_table", sequence, sequence)
        conn.commit()
    finally:
        conn.close()

    messages = get_messages(str(db_path), "R:1", limit=4, offset=3)

    assert _sequences(messages) == [85, 80, 75, 70]


def test_get_messages_filters_before_global_pagination(tmp_path):
    db_path = _message_db(tmp_path)
    conn = sqlite3.connect(db_path)
    try:
        _insert_message(conn, "message_table", 1, 100, content_type=2, send_time=10)
        _insert_message(conn, "message_table", 2, 90, content_type=4, send_time=20)
        _insert_message(
            conn,
            "message_small_table",
            3,
            80,
            content_type=4,
            send_time=30,
        )
        _insert_message(
            conn,
            "message_small_table",
            4,
            70,
            conversation_id="R:2",
            content_type=4,
            send_time=40,
        )
        _insert_message(conn, "message_table", 5, 60, content_type=4, send_time=50)
        conn.commit()
    finally:
        conn.close()

    messages = get_messages(
        str(db_path),
        "R:1",
        limit=10,
        offset=1,
        since=20,
        until=50,
        msg_type=4,
    )

    assert _sequences(messages) == [80]


def test_get_messages_handles_missing_tables_and_zero_limit(tmp_path):
    db_path = _message_db(tmp_path, tables=("message_small_table",))
    conn = sqlite3.connect(db_path)
    try:
        _insert_message(conn, "message_small_table", 1, 10)
        conn.commit()
    finally:
        conn.close()

    assert get_messages(str(db_path), "R:1", limit=0) == []
    assert _sequences(get_messages(str(db_path), "R:1", limit=10)) == [10]


def test_queries_return_empty_when_no_message_tables_exist(tmp_path):
    db_path = _message_db(tmp_path, tables=())

    assert get_messages(str(db_path), "R:1", limit=10) == []
    assert search_messages(str(db_path), "needle", limit=10) == []


def test_search_messages_applies_limit_after_global_sequence_order(tmp_path):
    db_path = _message_db(tmp_path)
    conn = sqlite3.connect(db_path)
    try:
        for sequence in (100, 80, 60):
            _insert_message(
                conn,
                "message_table",
                sequence,
                sequence,
                content="needle from main",
            )
        for sequence in (90, 70, 50):
            _insert_message(
                conn,
                "message_small_table",
                sequence,
                sequence,
                content="needle from small",
            )
        _insert_message(conn, "message_small_table", 1, 200, content="other")
        conn.commit()
    finally:
        conn.close()

    messages = search_messages(str(db_path), "needle", limit=4)

    assert _sequences(messages) == [100, 90, 80, 70]


def test_search_messages_filters_conversation_before_global_limit(tmp_path):
    db_path = _message_db(tmp_path)
    conn = sqlite3.connect(db_path)
    try:
        _insert_message(
            conn,
            "message_table",
            1,
            100,
            conversation_id="R:2",
            content="needle wrong room",
        )
        _insert_message(
            conn,
            "message_table",
            2,
            90,
            conversation_id="R:1",
            content="needle main",
        )
        _insert_message(
            conn,
            "message_small_table",
            3,
            80,
            conversation_id="R:1",
            content="needle small",
        )
        conn.commit()
    finally:
        conn.close()

    messages = search_messages(str(db_path), "needle", conversation_id="R:1", limit=5)

    assert _sequences(messages) == [90, 80]
