"""SQLite WAL parsing and recovery helpers."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import sqlite3
import struct
import tempfile
from typing import Callable

WAL_MAGIC_CHECKSUM_LITTLE_ENDIAN = 0x377F0682
WAL_MAGIC_CHECKSUM_BIG_ENDIAN = 0x377F0683
WAL_HEADER_SIZE = 32
WAL_FRAME_HEADER_SIZE = 24
SQLITE_HEADER_SIZE = 100

PageDecoder = Callable[[int, bytes], bytes]


@dataclass(frozen=True)
class WalFrame:
    index: int
    page_number: int
    db_size: int
    salt1: int
    salt2: int
    checksum1: int
    checksum2: int
    payload_offset: int

    @property
    def is_commit(self) -> bool:
        return self.db_size > 0


@dataclass(frozen=True)
class WalScanError:
    frame_index: int | None
    kind: str
    message: str


@dataclass(frozen=True)
class WalScanResult:
    page_size: int
    salt1: int
    salt2: int
    frames: tuple[WalFrame, ...]
    last_valid_commit_index: int | None
    error: WalScanError | None

    @property
    def valid_frames_for_recovery(self) -> tuple[WalFrame, ...]:
        if self.last_valid_commit_index is None:
            return ()
        return tuple(f for f in self.frames if f.index <= self.last_valid_commit_index)


@dataclass(frozen=True)
class WalRecoveryResult:
    output_path: Path
    applied: bool
    scan: WalScanResult
    quick_check: str | None


def scan_wal(wal_path: str | os.PathLike[str]) -> WalScanResult:
    """Scan a SQLite WAL and stop at the first invalid frame."""
    with Path(wal_path).open("rb") as wal_file:
        wal_header = wal_file.read(WAL_HEADER_SIZE)
        if len(wal_header) < WAL_HEADER_SIZE:
            raise ValueError("WAL is shorter than the 32-byte header")

        (
            magic,
            version,
            page_size,
            _seq,
            salt1,
            salt2,
            checksum1,
            checksum2,
        ) = struct.unpack(">8I", wal_header)
        if magic not in (
            WAL_MAGIC_CHECKSUM_LITTLE_ENDIAN,
            WAL_MAGIC_CHECKSUM_BIG_ENDIAN,
        ):
            raise ValueError(f"unsupported WAL magic: 0x{magic:08x}")
        if version != 3007000:
            raise ValueError(f"unsupported WAL format version: {version}")
        page_size = _normalize_page_size(page_size)

        checksum_order = "<" if magic == WAL_MAGIC_CHECKSUM_LITTLE_ENDIAN else ">"
        expected_checksum = _wal_checksum(wal_header[:24], 0, 0, checksum_order)
        if expected_checksum != (checksum1, checksum2):
            return WalScanResult(
                page_size=page_size,
                salt1=salt1,
                salt2=salt2,
                frames=(),
                last_valid_commit_index=None,
                error=WalScanError(None, "checksum", "WAL header checksum mismatch"),
            )

        frame_index = 0
        frames: list[WalFrame] = []
        last_valid_commit_index: int | None = None
        running_checksum = expected_checksum

        while True:
            header = wal_file.read(WAL_FRAME_HEADER_SIZE)
            if not header:
                break
            frame_index += 1
            if len(header) < WAL_FRAME_HEADER_SIZE:
                return WalScanResult(
                    page_size,
                    salt1,
                    salt2,
                    tuple(frames),
                    last_valid_commit_index,
                    WalScanError(
                        frame_index, "truncated", "truncated WAL frame header"
                    ),
                )

            payload_offset = wal_file.tell()
            payload = wal_file.read(page_size)
            if len(payload) < page_size:
                return WalScanResult(
                    page_size,
                    salt1,
                    salt2,
                    tuple(frames),
                    last_valid_commit_index,
                    WalScanError(
                        frame_index, "truncated", "truncated WAL frame payload"
                    ),
                )

            (
                page_number,
                db_size,
                frame_salt1,
                frame_salt2,
                frame_checksum1,
                frame_checksum2,
            ) = struct.unpack(">6I", header)
            if (frame_salt1, frame_salt2) != (salt1, salt2):
                return WalScanResult(
                    page_size,
                    salt1,
                    salt2,
                    tuple(frames),
                    last_valid_commit_index,
                    WalScanError(frame_index, "salt", "WAL frame salt mismatch"),
                )

            running_checksum = _wal_checksum(
                header[:8] + payload, *running_checksum, checksum_order
            )
            if running_checksum != (frame_checksum1, frame_checksum2):
                return WalScanResult(
                    page_size,
                    salt1,
                    salt2,
                    tuple(frames),
                    last_valid_commit_index,
                    WalScanError(
                        frame_index, "checksum", "WAL frame checksum mismatch"
                    ),
                )

            frame = WalFrame(
                index=frame_index,
                page_number=page_number,
                db_size=db_size,
                salt1=frame_salt1,
                salt2=frame_salt2,
                checksum1=frame_checksum1,
                checksum2=frame_checksum2,
                payload_offset=payload_offset,
            )
            frames.append(frame)
            if frame.is_commit:
                last_valid_commit_index = frame.index

    return WalScanResult(
        page_size, salt1, salt2, tuple(frames), last_valid_commit_index, None
    )


def recover_wal(
    db_path: str | os.PathLike[str],
    wal_path: str | os.PathLike[str],
    output_path: str | os.PathLike[str],
    *,
    page_decoder: PageDecoder | None = None,
    strict: bool = True,
) -> WalRecoveryResult:
    """Apply the last valid WAL commit to a shadow copy, then atomically publish it."""
    db_path = Path(db_path)
    output_path = Path(output_path)
    scan = scan_wal(wal_path)
    frames = scan.valid_frames_for_recovery
    if not frames or (strict and scan.error is not None):
        return WalRecoveryResult(output_path, False, scan, None)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.", suffix=".tmp", dir=output_path.parent
    )
    os.close(fd)
    tmp_path = Path(tmp_name)

    try:
        shutil.copyfile(db_path, tmp_path)
        _apply_frames(
            tmp_path,
            Path(wal_path),
            scan.page_size,
            frames,
            page_decoder,
        )
        quick_check = _quick_check(tmp_path)
        if quick_check != "ok":
            tmp_path.unlink(missing_ok=True)
            return WalRecoveryResult(output_path, False, scan, quick_check)
        os.replace(tmp_path, output_path)
        return WalRecoveryResult(output_path, True, scan, quick_check)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _apply_frames(
    db_path: Path,
    wal_path: Path,
    page_size: int,
    frames: tuple[WalFrame, ...],
    page_decoder: PageDecoder | None,
) -> None:
    with db_path.open("r+b") as db_file, wal_path.open("rb") as wal_file:
        for frame in frames:
            if frame.page_number < 1:
                raise ValueError(f"invalid WAL page number: {frame.page_number}")
            wal_file.seek(frame.payload_offset)
            payload = wal_file.read(page_size)
            if len(payload) != page_size:
                raise ValueError("WAL changed after validation")
            if page_decoder is not None:
                payload = page_decoder(frame.page_number, payload)
            if len(payload) != page_size:
                raise ValueError(
                    "decoded WAL page size does not match WAL header page size"
                )

            db_file.seek((frame.page_number - 1) * page_size)
            db_file.write(payload)

        db_file.truncate(frames[-1].db_size * page_size)


def _quick_check(db_path: Path) -> str:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        try:
            row = conn.execute("PRAGMA quick_check").fetchone()
        except sqlite3.DatabaseError as exc:
            return str(exc)
    finally:
        conn.close()
    return row[0] if row else "missing quick_check result"


def _normalize_page_size(page_size: int) -> int:
    if page_size == 1:
        page_size = 65536
    if not 512 <= page_size <= 65536 or page_size & (page_size - 1):
        raise ValueError(f"invalid WAL page size: {page_size}")
    return page_size


def _wal_checksum(
    data: bytes, checksum1: int, checksum2: int, order: str
) -> tuple[int, int]:
    if len(data) % 8:
        raise ValueError("WAL checksum input must be 8-byte aligned")
    mask = 0xFFFFFFFF
    for offset in range(0, len(data), 8):
        word1, word2 = struct.unpack(f"{order}2I", data[offset : offset + 8])
        checksum1 = (checksum1 + word1 + checksum2) & mask
        checksum2 = (checksum2 + word2 + checksum1) & mask
    return checksum1, checksum2
