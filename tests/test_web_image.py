from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from wecom_reader import web


@dataclass
class FakeImage:
    local_path: Path
    mime: str


class FakeReader:
    def __init__(self, image: FakeImage | None) -> None:
        self.image = image
        self.image_resolver = self

    def resolve_image(self, message_id: str) -> FakeImage | None:
        if message_id == "found":
            return self.image
        return None

    def list_sessions(self, limit: int, keyword: str | None = None) -> list[dict]:
        return [{"id": "R:test", "name": keyword or "test", "limit": limit}]

    def get_messages(self, session_id: str, limit: int, offset: int) -> list[dict]:
        return [
            {
                "message_id": "m1",
                "session_id": session_id,
                "limit": limit,
                "offset": offset,
            }
        ]

    def search_messages(
        self,
        keyword: str,
        conversation_id: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        return [
            {"keyword": keyword, "conversation_id": conversation_id, "limit": limit}
        ]

    def init(self, verbose: bool = False) -> dict:
        return {"success": True, "verbose": verbose}


@pytest.fixture(autouse=True)
def reset_reader() -> None:
    web.reader = None


def test_api_image_streams_resolved_image(tmp_path: Path) -> None:
    image_path = tmp_path / "cached.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n")
    web.reader = FakeReader(FakeImage(local_path=image_path, mime="image/png"))  # type: ignore[assignment]

    response = web.app.test_client().get("/api/image/found")

    assert response.status_code == 200
    assert response.mimetype == "image/png"
    assert response.data == b"\x89PNG\r\n\x1a\n"


def test_api_image_returns_404_when_message_cannot_resolve() -> None:
    web.reader = FakeReader(None)  # type: ignore[assignment]

    response = web.app.test_client().get("/api/image/missing")

    assert response.status_code == 404
    assert response.data == b""


def test_api_image_returns_404_when_file_is_missing(tmp_path: Path) -> None:
    missing_path = tmp_path / "missing.jpg"
    web.reader = FakeReader(FakeImage(local_path=missing_path, mime="image/jpeg"))  # type: ignore[assignment]

    response = web.app.test_client().get("/api/image/found")

    assert response.status_code == 404
    assert response.data == b""


def test_index_contains_image_and_mention_rendering() -> None:
    response = web.app.test_client().get("/")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert ".mention" in html
    assert "highlightMentions" in html
    assert "m.content_type === 4 || m.content_type === 15" in html
    assert "/api/image/${encodeURIComponent(m.message_id)}" in html


def test_json_api_routes_use_reader() -> None:
    web.reader = FakeReader(None)  # type: ignore[assignment]
    client = web.app.test_client()

    sessions = client.get("/api/sessions?keyword=alice&limit=3")
    messages = client.get("/api/messages?session_id=R:test&limit=2&offset=1")
    missing_session = client.get("/api/messages")
    search = client.get("/api/search?q=hello&session_id=R:test&limit=4")
    refresh = client.post("/api/refresh")

    assert sessions.status_code == 200
    assert sessions.get_json()["sessions"][0]["name"] == "alice"
    assert messages.status_code == 200
    assert messages.get_json()["messages"][0]["offset"] == 1
    assert missing_session.status_code == 400
    assert search.status_code == 200
    assert search.get_json()["results"][0]["keyword"] == "hello"
    assert refresh.status_code == 200
    assert refresh.get_json()["success"] is True


def test_safe_jsonify_serializes_bytes_and_datetime() -> None:
    class Marker:
        def __str__(self) -> str:
            return "marker"

    response = web.safe_jsonify(
        {
            "raw": b"hello",
            "at": datetime(2026, 7, 1, 8, 0),
            "other": Marker(),
        }
    )

    assert response.get_data(as_text=True) == (
        '{"raw": "hello", "at": "2026-07-01T08:00:00", "other": "marker"}'
    )


def test_api_refresh_reports_errors() -> None:
    class BrokenReader(FakeReader):
        def init(self, verbose: bool = False) -> dict:
            raise RuntimeError("boom")

    web.reader = BrokenReader(None)  # type: ignore[assignment]

    response = web.app.test_client().post("/api/refresh")

    assert response.status_code == 200
    assert response.get_json() == {"success": False, "error": "boom"}


def test_api_image_supports_reader_image_resolver_fallback(tmp_path: Path) -> None:
    image_path = tmp_path / "fallback.png"
    image_path.write_bytes(b"fallback")
    resolver = FakeReader(FakeImage(local_path=image_path, mime="image/png"))
    web.reader = SimpleNamespace(image_resolver=resolver)  # type: ignore[assignment]

    response = web.app.test_client().get("/api/image/found")

    assert response.status_code == 200
    assert response.data == b"fallback"


def test_api_sessions_errors_when_reader_is_uninitialized() -> None:
    web.reader = None

    response = web.app.test_client().get("/api/sessions")

    assert response.status_code == 500


def test_main_initializes_reader_and_runs_app(monkeypatch) -> None:
    created = {}
    run_args = {}

    class FakeWeComReader:
        def __init__(self, db_dir: str, decrypted_dir: str) -> None:
            created["db_dir"] = db_dir
            created["decrypted_dir"] = decrypted_dir

    def fake_run(host: str, port: int, debug: bool) -> None:
        run_args.update({"host": host, "port": port, "debug": debug})

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "web",
            "--db-dir",
            "sample-data",
            "--decrypted-dir",
            "decrypted",
            "--host",
            "0.0.0.0",
            "--port",
            "9000",
        ],
    )
    monkeypatch.setattr(web, "WeComReader", FakeWeComReader)
    monkeypatch.setattr(web.app, "run", fake_run)

    web.main()

    assert created == {"db_dir": "sample-data", "decrypted_dir": "decrypted"}
    assert run_args == {"host": "0.0.0.0", "port": 9000, "debug": False}
