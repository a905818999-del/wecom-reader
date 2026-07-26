import sqlite3
from pathlib import Path

import pytest

from wecom_reader.image_resolver import ImageResolver


def _create_fixture(
    tmp_path: Path,
    mapped_name: str,
    *,
    cached_name: str | None = None,
) -> tuple[ImageResolver, Path]:
    db_dir = tmp_path / "data"
    decrypted_dir = tmp_path / "decrypted"
    mapping_dir = db_dir / "CacheMapping"
    image_dir = db_dir / "Cache" / "Image" / "2026-07"
    mapping_dir.mkdir(parents=True)
    image_dir.mkdir(parents=True)
    decrypted_dir.mkdir()

    with sqlite3.connect(decrypted_dir / "file.db") as conn:
        conn.execute(
            "CREATE TABLE file_table4 "
            "(message_id TEXT, message_type INTEGER, server_id TEXT, file_index INTEGER)"
        )
        conn.execute(
            "INSERT INTO file_table4 VALUES (?, ?, ?, ?)",
            ("msg-string", 1, "server-1", 0),
        )

    with sqlite3.connect(mapping_dir / "mapping.db") as conn:
        conn.execute("CREATE TABLE mapping (key TEXT, file_name TEXT)")
        conn.execute("INSERT INTO mapping VALUES (?, ?)", ("server-1", mapped_name))

    image_path = image_dir / (cached_name or "sample.jpg")
    return ImageResolver(str(db_dir), str(decrypted_dir)), image_path


@pytest.mark.parametrize(
    "mapped_name",
    [r"2026-07\sample.jpg", "2026-07/sample.jpg"],
)
def test_resolves_string_message_id_for_both_path_separators(
    tmp_path: Path, mapped_name: str
) -> None:
    resolver, image_path = _create_fixture(tmp_path, mapped_name)
    image_path.write_bytes(b"image")

    resolved = resolver.resolve_image("msg-string")

    assert resolved is not None
    assert resolved.local_path == image_path
    assert resolved.mime == "image/jpeg"


@pytest.mark.parametrize(
    "mapped_name",
    [
        r"..\secret.jpg",
        r"2026-07\..\..\secret.jpg",
        r"C:\secret.jpg",
        "/secret.jpg",
    ],
)
def test_rejects_paths_outside_image_cache(tmp_path: Path, mapped_name: str) -> None:
    resolver, _ = _create_fixture(tmp_path, mapped_name)

    assert resolver.resolve_image("msg-string") is None


def test_rejects_non_image_file(tmp_path: Path) -> None:
    resolver, cached_path = _create_fixture(
        tmp_path,
        r"2026-07\payload.html",
        cached_name="payload.html",
    )
    cached_path.write_text("<script>alert(1)</script>", encoding="utf-8")

    assert resolver.resolve_image("msg-string") is None


def test_rejects_same_origin_svg(tmp_path: Path) -> None:
    resolver, cached_path = _create_fixture(
        tmp_path,
        r"2026-07\payload.svg",
        cached_name="payload.svg",
    )
    cached_path.write_text(
        "<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>",
        encoding="utf-8",
    )

    assert resolver.resolve_image("msg-string") is None


def test_rejects_symlink_that_resolves_outside_image_cache(tmp_path: Path) -> None:
    resolver, cached_path = _create_fixture(tmp_path, r"2026-07\sample.jpg")
    outside_path = tmp_path / "outside.jpg"
    outside_path.write_bytes(b"outside")
    try:
        cached_path.symlink_to(outside_path)
    except OSError:
        pytest.skip("creating symlinks is not permitted on this platform")

    assert resolver.resolve_image("msg-string") is None


def test_missing_or_invalid_databases_return_none(tmp_path: Path) -> None:
    assert ImageResolver(None, str(tmp_path)).resolve_image("msg-string") is None

    resolver = ImageResolver(str(tmp_path / "missing"), str(tmp_path / "decrypted"))
    assert resolver.resolve_image("msg-string") is None

    resolver, _ = _create_fixture(tmp_path, r"2026-07\missing.jpg")
    assert resolver.resolve_image("unknown") is None
    assert resolver.resolve_image("msg-string") is None
