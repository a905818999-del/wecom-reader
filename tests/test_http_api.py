import base64
import json
import os
import sqlite3
import threading
import time

import pytest

from wecom_reader.http_api import create_app


class StubReader:
    def __init__(self, tmp_path):
        self.db_dir = str(tmp_path / "source")
        self.decrypted_dir = str(tmp_path / "decrypted")
        self.sessions_calls = []
        self.search_calls = []
        self.refresh_result = {"success": True, "decrypted": 1}
        os.makedirs(self.db_dir)
        os.makedirs(self.decrypted_dir)

    def list_sessions(self, **kwargs):
        self.sessions_calls.append(kwargs)
        return [{"id": "R:1", "name": "Room"}]

    def search_messages(self, *args, **kwargs):
        self.search_calls.append((args, kwargs))
        return [{"message_id": 1, "content": "hit"}]

    def init(self, verbose=False):
        return self.refresh_result


def create_message_db(path):
    conn = sqlite3.connect(path)
    try:
        for table in ("message_table", "message_small_table", "kf_message_tableV1"):
            conn.execute(
                f"""
                CREATE TABLE {table} (
                    message_id INTEGER,
                    server_id INTEGER,
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
        conn.commit()
    finally:
        conn.close()


def insert_message(
    db_path,
    table,
    message_id,
    sequence,
    conversation_id="R:1",
    server_id=None,
):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            f"""
            INSERT INTO {table}
            (message_id, server_id, sequence, sender_id, conversation_id,
             content_type, send_time, flag, content, from_app_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                message_id + 1000
                if server_id is None and message_id is not None
                else server_id,
                sequence,
                7,
                conversation_id,
                2,
                sequence * 10 if sequence is not None else None,
                0,
                f"message {message_id}".encode(),
                "app",
            ),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def reader(tmp_path):
    reader = StubReader(tmp_path)
    create_message_db(os.path.join(reader.decrypted_dir, "message.db"))
    return reader


@pytest.fixture
def client(reader):
    return create_app(reader).test_client()


def test_messages_use_stable_keyset_order_across_tables(client, reader):
    db_path = os.path.join(reader.decrypted_dir, "message.db")
    insert_message(db_path, "message_small_table", 2, 10)
    insert_message(db_path, "message_table", 1, 10)
    insert_message(db_path, "kf_message_tableV1", 3, 11)

    response = client.get("/api/v1/messages?conversation_id=R:1&limit=10")

    assert response.status_code == 200
    payload = response.get_json()
    identities = [
        (m["sequence"], m["source_table"], m["message_id"], m["server_id"])
        for m in payload["messages"]
    ]
    assert identities == [
        (10, "message_small_table", 2, 1002),
        (10, "message_table", 1, 1001),
        (11, "kf_message_tableV1", 3, 1003),
    ]
    assert payload["messages"][0]["raw_identity"] == {
        "source_table": "message_small_table",
        "message_id": 2,
        "server_id": 1002,
        "sequence": 10,
    }


def test_messages_cursor_is_opaque_base64url_json_and_stably_continues(client, reader):
    db_path = os.path.join(reader.decrypted_dir, "message.db")
    insert_message(db_path, "message_table", 1, 1)
    insert_message(db_path, "message_small_table", 2, 2)
    insert_message(db_path, "kf_message_tableV1", 3, 3)

    first = client.get("/api/v1/messages?conversation_id=R:1&limit=2").get_json()
    cursor = first["next_cursor"]
    decoded = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    assert decoded["v"] == 1
    assert set(decoded) == {
        "v",
        "sequence",
        "source_table",
        "message_id",
        "server_id",
        "source_rowid",
        "snapshot",
    }

    second_response = client.get(
        f"/api/v1/messages?conversation_id=R:1&limit=2&cursor={cursor}"
    )
    second = second_response.get_json()

    assert [m["message_id"] for m in first["messages"]] == [1, 2]
    assert second_response.status_code == 200
    assert [m["message_id"] for m in second["messages"]] == [3]


def test_messages_cursor_rejects_changed_snapshot_instead_of_silently_skipping(
    client, reader
):
    db_path = os.path.join(reader.decrypted_dir, "message.db")
    insert_message(db_path, "message_table", 1, 1)
    insert_message(db_path, "message_small_table", 2, 2)

    first = client.get("/api/v1/messages?conversation_id=R:1&limit=1").get_json()
    insert_message(db_path, "message_table", 99, 1)
    response = client.get(
        f"/api/v1/messages?conversation_id=R:1&limit=1&cursor={first['next_cursor']}"
    )

    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "snapshot_changed"


def test_messages_cursor_does_not_skip_null_identity_fields(client, reader):
    db_path = os.path.join(reader.decrypted_dir, "message.db")
    insert_message(db_path, "message_table", None, 1, server_id=None)
    insert_message(db_path, "message_table", 2, 2)

    first = client.get("/api/v1/messages?conversation_id=R:1&limit=1").get_json()
    second = client.get(
        f"/api/v1/messages?conversation_id=R:1&limit=1&cursor={first['next_cursor']}"
    ).get_json()

    assert first["messages"][0]["message_id"] is None
    assert second["messages"][0]["message_id"] == 2


def test_messages_cursor_uses_rowid_to_preserve_duplicate_null_identities(
    client, reader
):
    db_path = os.path.join(reader.decrypted_dir, "message.db")
    insert_message(db_path, "message_table", None, None, server_id=None)
    insert_message(db_path, "message_table", None, None, server_id=None)

    first = client.get("/api/v1/messages?conversation_id=R:1&limit=1").get_json()
    second = client.get(
        f"/api/v1/messages?conversation_id=R:1&limit=1&cursor={first['next_cursor']}"
    ).get_json()

    assert first["messages"][0]["source_rowid"] != second["messages"][0]["source_rowid"]
    assert first["messages"][0]["message_id"] is None
    assert second["messages"][0]["message_id"] is None


@pytest.mark.parametrize(
    "query, code",
    [
        ("conversation_id=R:1&limit=0", "invalid_request"),
        ("conversation_id=R:1&limit=501", "invalid_request"),
        ("conversation_id=R:1&cursor=not-base64", "invalid_request"),
        (
            "conversation_id=R:1&cursor="
            + base64.urlsafe_b64encode(b"[]").decode().rstrip("="),
            "invalid_request",
        ),
    ],
)
def test_messages_returns_structured_400_for_bad_inputs(client, query, code):
    response = client.get(f"/api/v1/messages?{query}")

    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == code


def test_messages_requires_conversation_id(client):
    response = client.get("/api/v1/messages")

    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "missing_conversation_id"


def test_health_reports_lag_wal_and_degraded_state(client, reader):
    source_db = os.path.join(reader.db_dir, "message.db")
    snapshot_db = os.path.join(reader.decrypted_dir, "message.db")
    wal_path = snapshot_db + "-wal"
    open(source_db, "wb").close()
    with open(wal_path, "wb") as f:
        f.write(b"pending")

    now = time.time()
    os.utime(snapshot_db, (now - 20, now - 20))
    os.utime(source_db, (now, now))

    payload = client.get("/api/v1/health").get_json()

    assert payload["degraded"] is True
    assert payload["ok"] is False
    assert payload["wal_present"] is True
    assert payload["wal_files"] == {
        "source": [],
        "snapshot": ["message.db-wal"],
    }
    assert payload["lag_seconds_estimate"] >= 19
    assert payload["checkpoint"] is None
    assert payload["message_gaps"] is None
    assert payload["attachment_metrics"] is None


def test_health_reports_source_wal_as_degraded(client, reader):
    source_wal = os.path.join(reader.db_dir, "message.db-wal")
    with open(source_wal, "wb") as f:
        f.write(b"active")

    payload = client.get("/api/v1/health").get_json()

    assert payload["wal_present"] is True
    assert payload["wal_files"] == {
        "source": ["message.db-wal"],
        "snapshot": [],
    }
    assert payload["degraded"] is True
    assert payload["ok"] is False


def test_sessions_and_search_delegate_to_reader(client, reader):
    sessions = client.get("/api/v1/sessions?limit=25&q=room").get_json()
    search = client.get("/api/v1/search?limit=5&q=hello&conversation_id=R:1").get_json()

    assert sessions["count"] == 1
    assert reader.sessions_calls == [{"limit": 25, "keyword": "room"}]
    assert search["count"] == 1
    assert reader.search_calls == [(("hello",), {"conversation_id": "R:1", "limit": 5})]


def test_search_validates_required_q(client):
    response = client.get("/api/v1/search")

    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "missing_q"


def test_refresh_records_success_and_error(tmp_path):
    reader = StubReader(tmp_path)
    app = create_app(reader)
    client = app.test_client()

    ok_payload = client.post("/api/v1/refresh").get_json()
    assert ok_payload["ok"] is True
    assert app.config["LAST_REFRESH_RESULT"]["result"] == {
        "success": True,
        "decrypted": 1,
    }

    def fail_refresh(verbose=False):
        raise RuntimeError("refresh failed")

    reader.init = fail_refresh
    failed = client.post("/api/v1/refresh")

    assert failed.status_code == 500
    assert failed.get_json()["error"] == "refresh failed"
    assert client.get("/api/v1/health").get_json()["degraded"] is True


def test_refresh_unsuccessful_result_marks_health_degraded(tmp_path):
    reader = StubReader(tmp_path)
    reader.refresh_result = {"success": False, "failed": 1}
    client = create_app(reader).test_client()

    response = client.post("/api/v1/refresh")

    assert response.status_code == 500
    assert response.get_json()["error"] is None
    assert client.get("/api/v1/health").get_json()["degraded"] is True


def test_health_remains_available_while_refresh_is_running(tmp_path):
    reader = StubReader(tmp_path)
    started = threading.Event()
    release = threading.Event()

    def blocking_refresh(verbose=False):
        started.set()
        assert release.wait(timeout=2)
        return {"success": True}

    reader.init = blocking_refresh
    app = create_app(reader)
    refresh_thread = threading.Thread(
        target=lambda: app.test_client().post("/api/v1/refresh")
    )
    refresh_thread.start()
    assert started.wait(timeout=2)

    health = app.test_client().get("/api/v1/health")

    assert health.status_code == 200
    assert health.get_json()["refresh_in_progress"] is True
    release.set()
    refresh_thread.join(timeout=2)
    assert refresh_thread.is_alive() is False
