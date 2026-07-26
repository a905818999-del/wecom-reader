from dataclasses import dataclass
from pathlib import Path
import sqlite3

import pytest

from wecom_reader import web
from wecom_reader.reader import WeComReader


@dataclass
class FakeImage:
    local_path: Path
    mime: str


class FakeReader:
    def __init__(self, image: FakeImage | None) -> None:
        self.image = image

    def resolve_image(self, message_id: str) -> FakeImage | None:
        return self.image if message_id == "found" else None


@pytest.fixture(autouse=True)
def reset_reader():
    previous = web.reader
    yield
    web.reader = previous


def test_api_image_streams_resolved_image(tmp_path: Path) -> None:
    image_path = tmp_path / "cached.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n")
    web.reader = FakeReader(FakeImage(image_path, "image/png"))

    response = web.app.test_client().get("/api/image/found")

    assert response.status_code == 200
    assert response.mimetype == "image/png"
    assert response.data == b"\x89PNG\r\n\x1a\n"


def test_api_image_uses_real_resolver_chain(tmp_path: Path) -> None:
    db_dir = tmp_path / "data"
    decrypted_dir = tmp_path / "decrypted"
    mapping_dir = db_dir / "CacheMapping"
    image_dir = db_dir / "Cache" / "Image" / "2026-07"
    mapping_dir.mkdir(parents=True)
    image_dir.mkdir(parents=True)
    decrypted_dir.mkdir()
    (image_dir / "sample.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    with sqlite3.connect(decrypted_dir / "file.db") as conn:
        conn.execute(
            "CREATE TABLE file_table4 "
            "(message_id TEXT, message_type INTEGER, server_id TEXT, file_index INTEGER)"
        )
        conn.execute(
            "INSERT INTO file_table4 VALUES (?, ?, ?, ?)",
            ("message-id", 1, "server-id", 0),
        )
    with sqlite3.connect(mapping_dir / "mapping.db") as conn:
        conn.execute("CREATE TABLE mapping (key TEXT, file_name TEXT)")
        conn.execute(
            "INSERT INTO mapping VALUES (?, ?)",
            ("server-id", r"2026-07\sample.png"),
        )

    web.reader = WeComReader(str(db_dir), str(decrypted_dir))
    response = web.app.test_client().get("/api/image/message-id")

    assert response.status_code == 200
    assert response.mimetype == "image/png"
    assert response.data == b"\x89PNG\r\n\x1a\n"


@pytest.mark.parametrize("message_id", ["missing", "string-message-id"])
def test_api_image_returns_empty_404_without_path_leak(
    tmp_path: Path, message_id: str
) -> None:
    missing_path = tmp_path / "private" / "missing.jpg"
    image = (
        FakeImage(missing_path, "image/jpeg")
        if message_id == "string-message-id"
        else None
    )
    web.reader = FakeReader(image)

    response = web.app.test_client().get(f"/api/image/{message_id}")

    assert response.status_code == 404
    assert response.data == b""
    assert str(tmp_path).encode() not in response.data


def test_index_renders_only_supported_image_message_types() -> None:
    response = web.app.test_client().get("/")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "m.content_type === 4 || m.content_type === 15" in html
    assert "/api/image/${encodeURIComponent(m.message_id)}" in html
    assert "msg-image-fallback" in html
    assert "span.textContent=this.alt" in html
    assert "outerHTML=" not in html
    assert "return highlightMentions(content, m.mentions)" in html
