"""Resolve WeCom image messages to local cached image files."""

from __future__ import annotations

import mimetypes
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ResolvedImage:
    """Resolved image metadata for internal file streaming."""

    message_id: str
    local_path: Path
    mime: str


class ImageResolver:
    """Resolve image message ids through decrypted file.db and CacheMapping."""

    def __init__(self, db_dir: str | None, decrypted_dir: str) -> None:
        self._db_dir = Path(db_dir) if db_dir else None
        self._decrypted_dir = Path(decrypted_dir)

    def resolve_image(self, message_id: str) -> ResolvedImage | None:
        """Resolve an image message id to a cached local file."""
        if self._db_dir is None:
            return None

        server_id = self._lookup_server_id(message_id)
        if not server_id:
            return None

        file_name = self._lookup_cache_file_name(server_id)
        if not file_name:
            return None

        local_path = self._find_cached_file(file_name)
        if local_path is None:
            return None

        mime = mimetypes.guess_type(local_path.name)[0] or "application/octet-stream"
        return ResolvedImage(message_id=message_id, local_path=local_path, mime=mime)

    def _lookup_server_id(self, message_id: str) -> str | None:
        file_db = self._decrypted_dir / "file.db"
        if not file_db.is_file():
            return None

        try:
            with closing(sqlite3.connect(file_db)) as conn:
                row = conn.execute(
                    "SELECT server_id FROM file_table4 "
                    "WHERE message_id = ? AND message_type = 1 "
                    "ORDER BY file_index ASC LIMIT 1",
                    (message_id,),
                ).fetchone()
        except sqlite3.Error:
            return None

        if not row or not row[0]:
            return None
        return str(row[0])

    def _lookup_cache_file_name(self, server_id: str) -> str | None:
        cache_mapping_db = self._find_cache_mapping_db()
        if cache_mapping_db is None:
            return None

        try:
            with closing(sqlite3.connect(cache_mapping_db)) as conn:
                row = conn.execute(
                    "SELECT file_name FROM mapping WHERE key = ? LIMIT 1",
                    (server_id,),
                ).fetchone()
        except sqlite3.Error:
            return None

        if not row or not row[0]:
            return None
        return str(row[0])

    def _find_cache_mapping_db(self) -> Path | None:
        if self._db_dir is None:
            return None

        mapping_dir = self._db_dir / "CacheMapping"
        if not mapping_dir.is_dir():
            return None

        for candidate in mapping_dir.glob("*.db"):
            try:
                with closing(sqlite3.connect(candidate)) as conn:
                    row = conn.execute(
                        "SELECT 1 FROM sqlite_master "
                        "WHERE type = 'table' AND name = 'mapping' LIMIT 1"
                    ).fetchone()
            except sqlite3.Error:
                continue
            if row:
                return candidate
        return None

    def _find_cached_file(self, file_name: str) -> Path | None:
        if self._db_dir is None:
            return None

        normalized = Path(file_name.replace("\\", os.sep).replace("/", os.sep))
        for cache_name in ("Image", "File"):
            cache_root = self._db_dir / "Cache" / cache_name
            candidate = cache_root / normalized
            try:
                candidate.resolve().relative_to(cache_root.resolve())
            except (OSError, ValueError):
                continue
            if candidate.is_file():
                return candidate
        return None
