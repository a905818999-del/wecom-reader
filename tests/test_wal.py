import os
from pathlib import Path
import shutil
import sqlite3
import struct

from wecom_reader.wal import (
    WAL_FRAME_HEADER_SIZE,
    WAL_HEADER_SIZE,
    WAL_MAGIC_CHECKSUM_BIG_ENDIAN,
    WAL_MAGIC_CHECKSUM_LITTLE_ENDIAN,
    SQLITE_HEADER_SIZE,
    recover_wal,
    scan_wal,
)
from wecom_reader.wal import _wal_checksum

PAGE_SIZE = 512
SALT1 = 0x01020304
SALT2 = 0xA0B0C0D0


def test_recover_multiple_commits_uses_last_commit(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path / "base.db")
    page2_a = _read_page(db_path, 2)
    page2_b = _with_cell_value(page2_a, 2, "scnd")
    wal_path = tmp_path / "base.db-wal"
    _write_wal(wal_path, [(2, 2, page2_a), (2, 2, page2_b)])

    out_path = tmp_path / "out.db"
    result = recover_wal(db_path, wal_path, out_path, strict=False)

    assert result.applied is True
    assert result.quick_check == "ok"
    assert _names(out_path) == ["scnd"]
    assert result.scan.last_valid_commit_index == 2


def test_uncommitted_tail_is_not_applied(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path / "base.db")
    committed = _with_cell_value(_read_page(db_path, 2), 2, "cmtd")
    uncommitted = _with_cell_value(_read_page(db_path, 2), 2, "tail")
    wal_path = tmp_path / "base.db-wal"
    _write_wal(wal_path, [(2, 2, committed), (2, 0, uncommitted)])

    out_path = tmp_path / "out.db"
    result = recover_wal(db_path, wal_path, out_path)

    assert result.applied is True
    assert _names(out_path) == ["cmtd"]
    assert result.scan.last_valid_commit_index == 1


def test_salt_reset_stops_scan_and_strict_recover_preserves_output(
    tmp_path: Path,
) -> None:
    db_path = _make_db(tmp_path / "base.db")
    out_path = tmp_path / "out.db"
    out_path.write_bytes(b"old output")
    first = _with_cell_value(_read_page(db_path, 2), 2, "vld1")
    bad = _with_cell_value(_read_page(db_path, 2), 2, "bad")
    wal_path = tmp_path / "base.db-wal"
    _write_wal(wal_path, [(2, 2, first), (2, 2, bad)], bad_salt_frame=2)

    result = recover_wal(db_path, wal_path, out_path)

    assert result.applied is False
    assert result.scan.error is not None
    assert result.scan.error.kind == "salt"
    assert result.scan.last_valid_commit_index == 1
    assert out_path.read_bytes() == b"old output"


def test_checksum_damage_stops_scan_and_strict_recover_preserves_output(
    tmp_path: Path,
) -> None:
    db_path = _make_db(tmp_path / "base.db")
    out_path = tmp_path / "out.db"
    out_path.write_bytes(b"old output")
    first = _with_cell_value(_read_page(db_path, 2), 2, "vld1")
    bad = _with_cell_value(_read_page(db_path, 2), 2, "bad")
    wal_path = tmp_path / "base.db-wal"
    _write_wal(wal_path, [(2, 2, first), (2, 2, bad)])
    second_payload = (
        WAL_HEADER_SIZE + WAL_FRAME_HEADER_SIZE + PAGE_SIZE + WAL_FRAME_HEADER_SIZE
    )
    _flip_byte(wal_path, second_payload + 10)

    result = recover_wal(db_path, wal_path, out_path)

    assert result.applied is False
    assert result.scan.error is not None
    assert result.scan.error.kind == "checksum"
    assert result.scan.last_valid_commit_index == 1
    assert out_path.read_bytes() == b"old output"


def test_repeated_page_later_frame_wins(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path / "base.db")
    old = _with_cell_value(_read_page(db_path, 2), 2, "old")
    new = _with_cell_value(_read_page(db_path, 2), 2, "new")
    wal_path = tmp_path / "base.db-wal"
    _write_wal(wal_path, [(2, 0, old), (2, 2, new)])

    out_path = tmp_path / "out.db"
    result = recover_wal(db_path, wal_path, out_path)

    assert result.applied is True
    assert _names(out_path) == ["new"]


def test_commit_db_size_truncates_database(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path / "base.db")
    shutil.copyfile(db_path, tmp_path / "copy.db")
    with (tmp_path / "copy.db").open("ab") as f:
        f.write(b"\x00" * PAGE_SIZE)
    wal_path = tmp_path / "base.db-wal"
    _write_wal(wal_path, [(2, 2, _read_page(db_path, 2))])

    out_path = tmp_path / "out.db"
    result = recover_wal(tmp_path / "copy.db", wal_path, out_path)

    assert result.applied is True
    assert os.path.getsize(out_path) == 2 * PAGE_SIZE


def test_failed_quick_check_preserves_existing_output(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path / "base.db")
    out_path = tmp_path / "out.db"
    out_path.write_bytes(b"old output")
    corrupt_page1 = bytearray(_read_page(db_path, 1))
    corrupt_page1[:16] = b"not sqlite hdr!!"
    wal_path = tmp_path / "base.db-wal"
    _write_wal(wal_path, [(1, 2, bytes(corrupt_page1))])

    result = recover_wal(db_path, wal_path, out_path)

    assert result.applied is False
    assert result.quick_check != "ok"
    assert out_path.read_bytes() == b"old output"


def test_page_decoder_callback_decodes_payload(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path / "base.db")
    decoded = _with_cell_value(_read_page(db_path, 2), 2, "dcod")
    encoded = bytes(b ^ 0xA5 for b in decoded)
    wal_path = tmp_path / "base.db-wal"
    _write_wal(wal_path, [(2, 2, encoded)])

    def decoder(page_number: int, payload: bytes) -> bytes:
        assert page_number == 2
        return bytes(b ^ 0xA5 for b in payload)

    out_path = tmp_path / "out.db"
    result = recover_wal(db_path, wal_path, out_path, page_decoder=decoder)

    assert result.applied is True
    assert _names(out_path) == ["dcod"]


def test_checksum_byte_order_matches_fixed_vectors() -> None:
    data = bytes.fromhex("00000001000000020000000300000004")

    assert _wal_checksum(data, 0, 0, "<") == (0x07000000, 0x0E000000)
    assert _wal_checksum(data, 0, 0, ">") == (0x00000007, 0x0000000E)


def test_big_endian_magic_changes_checksum_input_order(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path / "base.db")
    wal_path = tmp_path / "base.db-wal"
    _write_wal(
        wal_path,
        [(2, 2, _with_cell_value(_read_page(db_path, 2), 2, "ltle"))],
        magic=WAL_MAGIC_CHECKSUM_BIG_ENDIAN,
    )

    result = scan_wal(wal_path)

    assert result.error is None
    assert result.last_valid_commit_index == 1
    assert result.frames[0].payload_offset == WAL_HEADER_SIZE + WAL_FRAME_HEADER_SIZE
    assert not hasattr(result.frames[0], "payload")


def _make_db(path: Path) -> Path:
    conn = sqlite3.connect(path)
    try:
        conn.execute(f"PRAGMA page_size = {PAGE_SIZE}")
        conn.execute("PRAGMA journal_mode = DELETE")
        conn.execute("VACUUM")
        conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO t(name) VALUES ('base')")
        conn.commit()
    finally:
        conn.close()
    assert os.path.getsize(path) == 2 * PAGE_SIZE
    return path


def _names(path: Path) -> list[str]:
    conn = sqlite3.connect(path)
    try:
        return [
            row[0].rstrip() for row in conn.execute("SELECT name FROM t ORDER BY id")
        ]
    finally:
        conn.close()


def _read_page(path: Path, page_number: int) -> bytes:
    with path.open("rb") as f:
        f.seek((page_number - 1) * PAGE_SIZE)
        page = f.read(PAGE_SIZE)
    assert len(page) == PAGE_SIZE
    return page


def _with_cell_value(page: bytes, page_number: int, value: str) -> bytes:
    old = b"base"
    new = value.encode()
    if len(new) > len(old):
        raise ValueError("replacement must fit in existing cell")
    page = bytearray(page)
    if page_number == 1:
        # Keep the SQLite header valid when page 1 is deliberately edited.
        start = page.find(old, SQLITE_HEADER_SIZE)
    else:
        start = page.find(old)
    assert start >= 0
    page[start : start + len(old)] = new.ljust(len(old), b" ")
    return bytes(page)


def _write_wal(
    path: Path,
    frames: list[tuple[int, int, bytes]],
    *,
    magic: int = WAL_MAGIC_CHECKSUM_LITTLE_ENDIAN,
    bad_salt_frame: int | None = None,
) -> None:
    checksum_order = "<" if magic == WAL_MAGIC_CHECKSUM_LITTLE_ENDIAN else ">"
    header_prefix = struct.pack(
        ">6I",
        magic,
        3007000,
        PAGE_SIZE,
        0,
        SALT1,
        SALT2,
    )
    checksum = _wal_checksum(header_prefix, 0, 0, checksum_order)
    data = bytearray(header_prefix + struct.pack(">2I", *checksum))

    for index, (page_number, db_size, payload) in enumerate(frames, start=1):
        frame_salt1 = SALT1 ^ 0xFFFFFFFF if index == bad_salt_frame else SALT1
        frame_prefix = struct.pack(">4I", page_number, db_size, frame_salt1, SALT2)
        checksum = _wal_checksum(frame_prefix[:8] + payload, *checksum, checksum_order)
        data += frame_prefix + struct.pack(">2I", *checksum) + payload

    path.write_bytes(data)


def _flip_byte(path: Path, offset: int) -> None:
    data = bytearray(path.read_bytes())
    data[offset] ^= 0xFF
    path.write_bytes(data)
