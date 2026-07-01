from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from wecom_reader.image_resolver import ImageResolver


def test_image_resolver_maps_message_to_cached_file(tmp_path: Path) -> None:
    db_dir = tmp_path / "data"
    decrypted_dir = tmp_path / "decrypted"
    cache_dir = db_dir / "Cache" / "Image" / "2026-07"
    mapping_dir = db_dir / "CacheMapping"
    cache_dir.mkdir(parents=True)
    mapping_dir.mkdir(parents=True)
    decrypted_dir.mkdir()

    image_path = cache_dir / "sample.jpg"
    image_path.write_bytes(b"image")

    with closing(sqlite3.connect(decrypted_dir / "file.db")) as conn:
        conn.execute(
            "CREATE TABLE file_table4 "
            "(message_id TEXT, message_type INTEGER, server_id TEXT, file_index INTEGER)"
        )
        conn.execute(
            "INSERT INTO file_table4 VALUES (?, ?, ?, ?)",
            ("msg-1", 1, "server-1", 0),
        )
        conn.commit()

    with closing(sqlite3.connect(mapping_dir / "mapping.db")) as conn:
        conn.execute("CREATE TABLE mapping (key TEXT, file_name TEXT)")
        conn.execute(
            "INSERT INTO mapping VALUES (?, ?)",
            ("server-1", r"2026-07\sample.jpg"),
        )
        conn.commit()

    resolved = ImageResolver(str(db_dir), str(decrypted_dir)).resolve_image("msg-1")

    assert resolved is not None
    assert resolved.message_id == "msg-1"
    assert resolved.local_path == image_path
    assert resolved.mime == "image/jpeg"


def test_image_resolver_rejects_cache_path_traversal(tmp_path: Path) -> None:
    db_dir = tmp_path / "data"
    decrypted_dir = tmp_path / "decrypted"
    mapping_dir = db_dir / "CacheMapping"
    mapping_dir.mkdir(parents=True)
    decrypted_dir.mkdir()

    with closing(sqlite3.connect(decrypted_dir / "file.db")) as conn:
        conn.execute(
            "CREATE TABLE file_table4 "
            "(message_id TEXT, message_type INTEGER, server_id TEXT, file_index INTEGER)"
        )
        conn.execute(
            "INSERT INTO file_table4 VALUES (?, ?, ?, ?)",
            ("msg-1", 1, "server-1", 0),
        )
        conn.commit()

    with closing(sqlite3.connect(mapping_dir / "mapping.db")) as conn:
        conn.execute("CREATE TABLE mapping (key TEXT, file_name TEXT)")
        conn.execute(
            "INSERT INTO mapping VALUES (?, ?)", ("server-1", r"..\secret.jpg")
        )
        conn.commit()

    resolved = ImageResolver(str(db_dir), str(decrypted_dir)).resolve_image("msg-1")

    assert resolved is None
