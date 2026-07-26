"""Resolve WeCom image messages to files in the local image cache."""

import mimetypes
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

ALLOWED_IMAGE_MIMES = frozenset(
    {"image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp"}
)


@dataclass(frozen=True)
class ResolvedImage:
    message_id: str
    local_path: Path
    mime: str


class ImageResolver:
    """Resolve file.db references without allowing paths outside Cache/Image."""

    def __init__(self, db_dir: str | None, decrypted_dir: str) -> None:
        self._db_dir = Path(db_dir) if db_dir else None
        self._decrypted_dir = Path(decrypted_dir)

    def resolve_image(self, message_id: str) -> ResolvedImage | None:
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

        mime = mimetypes.guess_type(local_path.name)[0]
        if mime not in ALLOWED_IMAGE_MIMES:
            return None
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
        return str(row[0]) if row and row[0] else None

    def _lookup_cache_file_name(self, server_id: str) -> str | None:
        if self._db_dir is None:
            return None
        mapping_dir = self._db_dir / "CacheMapping"
        if not mapping_dir.is_dir():
            return None

        for candidate in sorted(mapping_dir.glob("*.db")):
            try:
                with closing(sqlite3.connect(candidate)) as conn:
                    row = conn.execute(
                        "SELECT file_name FROM mapping WHERE key = ? LIMIT 1",
                        (server_id,),
                    ).fetchone()
            except sqlite3.Error:
                continue
            if row and row[0]:
                return str(row[0])
        return None

    def _find_cached_file(self, file_name: str) -> Path | None:
        if self._db_dir is None:
            return None

        windows_path = PureWindowsPath(file_name)
        if windows_path.is_absolute() or windows_path.drive:
            return None
        relative_path = Path(*windows_path.parts)
        if relative_path.is_absolute():
            return None

        cache_root = (self._db_dir / "Cache" / "Image").resolve()
        candidate = (cache_root / relative_path).resolve()
        try:
            candidate.relative_to(cache_root)
        except ValueError:
            return None
        return candidate if candidate.is_file() else None
