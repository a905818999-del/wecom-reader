"""Image resolver — map WeCom image messages to local cached files.

WeCom (企微) stores images as **standard unencrypted files** in:
    <WXWork>/<account_id>/Cache/Image/<YYYY-MM>/<filename>

The mapping chain is:
    message_table.message_id
        → file_table4.message_id (file.db, message_type=1)
        → file_table4.server_id (= CacheMapping.mapping.key)
        → CacheMapping.mapping.file_name
        → Cache/Image/YYYY-MM/<file_name>

Fallback chain (when server_id doesn't match):
    protobuf content → extract filename → CacheMapping.file_name → Cache/Image/

This module implements that chain to resolve image messages to local files.
"""

import os
import re
import shutil
import sqlite3
from dataclasses import dataclass
from typing import Optional


# URL patterns in WeCom image message content (protobuf-encoded)
# For extracting remote URLs when local file is not available
_URL_PATTERN = re.compile(
    rb"(https?://wework\.qpic\.cn/(?:wwpic|bizmail)/[^\x00-\x1f\x7f-\xff]+)"
)

# Filename pattern: 企业微信截图_xxxxx.ext or uuid.ext
# Include CJK characters and other Unicode word chars
_IMG_FILENAME_PATTERN = re.compile(
    r"([\w\u4e00-\u9fff\u3000-\u303f\uff00-\uffef-]+\.(?:png|jpg|jpeg|gif|webp|bmp))",
    re.IGNORECASE,
)


@dataclass
class ImageInfo:
    """Resolved image information."""

    message_id: int
    url: str = ""
    local_path: Optional[str] = None
    file_name: Optional[str] = None
    file_md5: Optional[str] = None
    file_index: int = 0  # zero-based index within a multi-image message
    found: bool = False


class ImageResolver:
    """Resolve WeCom image messages to local cached files.

    Uses the mapping chain:
        message.db (content_type=4) → file.db (server_id) → CacheMapping (file_name) → Cache/Image/

    Usage:
        resolver = ImageResolver(
            db_dir="E:/WXWork/1688851235369380",
            decrypted_dir="wxwork_decrypted"
        )
        info = resolver.resolve_message(90)
        result = resolver.export_conversation("R:10838562192818308", "./images/")
    """

    def __init__(
        self,
        db_dir: str,
        decrypted_dir: str = "wxwork_decrypted",
    ):
        """Initialize resolver.

        Args:
            db_dir: Path to WeCom account directory (e.g. E:/WXWork/1688851235369380).
            decrypted_dir: Path to decrypted database directory.
        """
        self._db_dir = db_dir
        self._decrypted_dir = decrypted_dir
        self._cache_image_dir = os.path.join(db_dir, "Cache", "Image")
        self._cache_mapping_db = self._find_cache_mapping_db()
        self._file_index: Optional[dict[str, str]] = None  # file_name → full_path

    def _find_cache_mapping_db(self) -> Optional[str]:
        """Find the CacheMapping database file.

        CacheMapping dir may contain multiple .db files. The one named
        ``CacheMapping.db`` is often a 0KB stub; the real data lives in a
        hash-named file (e.g. ``926a76fd...db``). We pick the first .db
        file that actually contains the ``mapping`` table.
        """
        mapping_dir = os.path.join(self._db_dir, "CacheMapping")
        if not os.path.isdir(mapping_dir):
            return None
        # Pass 1: prefer db files that contain a "mapping" table
        for name in os.listdir(mapping_dir):
            if not (name.endswith(".db") and not name.endswith("-wal") and not name.endswith("-shm")):
                continue
            path = os.path.join(mapping_dir, name)
            try:
                conn = sqlite3.connect(path)
                has_mapping = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='mapping' LIMIT 1"
                ).fetchone() is not None
                conn.close()
            except Exception:
                has_mapping = False
            if has_mapping:
                return path
        # Pass 2: fall back to the first .db file (legacy behaviour)
        for name in os.listdir(mapping_dir):
            if name.endswith(".db") and not name.endswith("-wal") and not name.endswith("-shm"):
                return os.path.join(mapping_dir, name)
        return None

    def _build_file_index(self) -> dict[str, str]:
        """Build index of all cached image/file: filename → full path.

        Scans both Cache/Image/ and Cache/File/ directories.

        Returns:
            Dict mapping "YYYY-MM\\filename" → full absolute path.
        """
        if self._file_index is not None:
            return self._file_index

        index: dict[str, str] = {}

        # Scan Cache/Image/ and Cache/File/
        for cache_subdir in ("Image", "File"):
            cache_dir = os.path.join(self._db_dir, "Cache", cache_subdir)
            if not os.path.isdir(cache_dir):
                continue
            for month_dir in os.listdir(cache_dir):
                month_path = os.path.join(cache_dir, month_dir)
                if not os.path.isdir(month_path):
                    continue
                for filename in os.listdir(month_path):
                    # Key: "YYYY-MM\\filename" (matches CacheMapping file_name format)
                    key = f"{month_dir}\\{filename}"
                    if key not in index:
                        index[key] = os.path.join(month_path, filename)
                    # Also index by filename alone for fallback
                    if filename not in index:
                        index[filename] = os.path.join(month_path, filename)

        self._file_index = index
        return index

    @staticmethod
    def extract_image_url(content: bytes) -> Optional[str]:
        """Extract the primary image URL from protobuf-encoded message content.

        Args:
            content: Raw bytes from message_table.content (content_type=4).

        Returns:
            Cleaned image URL string, or None if not found.
        """
        if not content:
            return None
        matches = _URL_PATTERN.findall(content)
        if not matches:
            return None
        # Clean trailing protobuf tag bytes, decode
        raw = matches[0]
        # Remove trailing /0h, /0P, /0H etc.
        url = re.sub(rb"/0[hpHP]?$", b"", raw).decode("utf-8", errors="replace")
        return url.rstrip("\x00\r\n\t ") or None

    @staticmethod
    def extract_file_name(content: bytes) -> Optional[str]:
        """Extract image filename from protobuf-encoded message content.

        Looks for patterns like: 企业微信截图_xxxxx.png, uuid.jpg, etc.

        Args:
            content: Raw bytes from message_table.content (content_type=4).

        Returns:
            Filename string (e.g. "企业微信截图_17757188377558.png"), or None.
        """
        if not content:
            return None
        # Decode to string and search for image filename pattern
        try:
            text = content.decode("utf-8", errors="ignore")
        except Exception:
            return None
        matches = _IMG_FILENAME_PATTERN.findall(text)
        if matches:
            # Return the longest match (most specific)
            return max(matches, key=len)
        return None

    def _resolve_by_file_name(self, file_name: str) -> Optional[tuple[str, str, str]]:
        """Resolve by filename via CacheMapping file_name column.

        This is a fallback when server_id doesn't match CacheMapping key.
        Queries CacheMapping by file_name (indexed column).

        Args:
            file_name: Image filename (e.g. "企业微信截图_17757188377558.png").

        Returns:
            Tuple of (file_name_with_prefix, full_path, file_md5) or None.
        """
        if not self._cache_mapping_db or not file_name:
            return None

        try:
            conn = sqlite3.connect(self._cache_mapping_db)
            cur = conn.cursor()
            # file_name in CacheMapping is like "YYYY-MM\企业微信截图_xxxxx.png"
            # Search by suffix match using the index
            rows = cur.execute(
                "SELECT file_name, file_md5 FROM mapping WHERE file_name LIKE ? LIMIT 5",
                (f"%\\{file_name}",),
            ).fetchall()
            conn.close()
        except Exception:
            return None

        if not rows:
            return None

        # Try each match
        index = self._build_file_index()
        for row in rows:
            full_file_name, file_md5 = row[0], row[1] or ""
            full_path = index.get(full_file_name)
            if full_path and os.path.isfile(full_path):
                return (full_file_name, full_path, file_md5)
            # Try direct path
            direct_path = os.path.join(self._cache_image_dir, full_file_name)
            if os.path.isfile(direct_path):
                return (full_file_name, direct_path, file_md5)

        return None

    def _resolve_by_server_id(self, server_id: str) -> Optional[tuple[str, str, str]]:
        """Resolve server_id to local file via CacheMapping.

        Args:
            server_id: The server_id from file_table4 (used as CacheMapping key).

        Returns:
            Tuple of (file_name, full_path, file_md5) or None if not found.
        """
        if not self._cache_mapping_db or not server_id:
            return None

        try:
            conn = sqlite3.connect(self._cache_mapping_db)
            cur = conn.cursor()
            row = cur.execute(
                "SELECT file_name, file_md5 FROM mapping WHERE key = ? LIMIT 1",
                (server_id,),
            ).fetchone()
            conn.close()
        except Exception:
            return None

        if not row or not row[0]:
            return None

        file_name, file_md5 = row[0], row[1] or ""

        # Find the actual file in Cache/Image/
        index = self._build_file_index()
        full_path = index.get(file_name)
        if full_path and os.path.isfile(full_path):
            return (file_name, full_path, file_md5)

        # Try direct path construction
        direct_path = os.path.join(self._cache_image_dir, file_name)
        if os.path.isfile(direct_path):
            return (file_name, direct_path, file_md5)

        return None

    def resolve_message(self, message_id: int, file_index: int = 0) -> ImageInfo:
        """Resolve a single image message to its local file.

        Uses the mapping chain:
            message_table.message_id → file_table4.server_id → CacheMapping.file_name → file

        For multi-image messages, use ``file_index`` to select a specific image
        (0 = first, 1 = second, etc.). Use ``resolve_message_all`` to get all
        images at once.

        Args:
            message_id: The message_id from message_table.
            file_index: Zero-based index for multi-image messages (default 0 = first).

        Returns:
            ImageInfo with resolution results.
        """
        msg_db = os.path.join(self._decrypted_dir, "message.db")
        file_db = os.path.join(self._decrypted_dir, "file.db")

        # Step 1: Get message content for URL extraction
        url = ""
        if os.path.isfile(msg_db):
            try:
                conn = sqlite3.connect(msg_db)
                cur = conn.cursor()
                row = cur.execute(
                    "SELECT content FROM message_table WHERE message_id = ? AND content_type IN (4, 14, 15, 123, 653)",
                    (message_id,),
                ).fetchone()
                conn.close()
                if row and row[0]:
                    url = self.extract_image_url(row[0]) or ""
            except Exception:
                pass

        # Step 2: Get server_id from file.db (message_type=1 = image)
        if not os.path.isfile(file_db):
            return ImageInfo(message_id=message_id, url=url, found=False)

        try:
            conn = sqlite3.connect(file_db)
            cur = conn.cursor()
            row = cur.execute(
                "SELECT server_id, md5, name FROM file_table4 WHERE message_id = ? AND message_type = 1 AND file_index = ? LIMIT 1",
                (message_id, file_index),
            ).fetchone()
            conn.close()
        except Exception:
            return ImageInfo(message_id=message_id, url=url, found=False)

        # Fallback 0: if no file_table4 record at this file_index, try file_index=0
        if not row:
            try:
                conn = sqlite3.connect(file_db)
                cur = conn.cursor()
                row = cur.execute(
                    "SELECT server_id, md5, name FROM file_table4 WHERE message_id = ? AND message_type = 1 ORDER BY file_index ASC LIMIT 1",
                    (message_id,),
                ).fetchone()
                conn.close()
            except Exception:
                pass
        if not row:
            # Try resolving by parsed filename from content
            if os.path.isfile(msg_db):
                try:
                    conn = sqlite3.connect(msg_db)
                    cur = conn.cursor()
                    row2 = cur.execute(
                        "SELECT content FROM message_table WHERE message_id = ?",
                        (message_id,),
                    ).fetchone()
                    conn.close()
                    if row2 and row2[0] and isinstance(row2[0], bytes):
                        fname = self.extract_file_name(row2[0])
                        if fname:
                            result = self._resolve_by_file_name(fname)
                            if result:
                                fn, fp, fmd5 = result
                                return ImageInfo(
                                    message_id=message_id,
                                    url=url,
                                    local_path=fp,
                                    file_name=fn,
                                    file_md5=fmd5,
                                    found=True,
                                )
                except Exception:
                    pass
            return ImageInfo(message_id=message_id, url=url, found=False)

        server_id, md5, name = row[0] or "", row[1] or "", row[2] or ""

        # Step 3: Resolve via CacheMapping
        result = self._resolve_by_server_id(server_id)
        if result:
            file_name, full_path, mapping_md5 = result
            return ImageInfo(
                message_id=message_id,
                url=url,
                local_path=full_path,
                file_name=file_name,
                file_md5=md5 or mapping_md5,
                found=True,
            )

        # Fallback 1: try to find by MD5 in CacheMapping
        if md5:
            try:
                conn = sqlite3.connect(self._cache_mapping_db)
                cur = conn.cursor()
                row = cur.execute(
                    "SELECT file_name FROM mapping WHERE file_md5 = ? LIMIT 1",
                    (md5.upper(),),
                ).fetchone()
                conn.close()
                if row and row[0]:
                    index = self._build_file_index()
                    full_path = index.get(row[0])
                    if full_path and os.path.isfile(full_path):
                        return ImageInfo(
                            message_id=message_id,
                            url=url,
                            local_path=full_path,
                            file_name=row[0],
                            file_md5=md5,
                            found=True,
                        )
            except Exception:
                pass

        # Fallback 1b: try to find directly by file_table4.name in Cache/Image/
        # (CacheMapping might not have this entry but the file itself is on disk)
        if name:
            index = self._build_file_index()
            full_path = index.get(name)
            if full_path and os.path.isfile(full_path):
                return ImageInfo(
                    message_id=message_id,
                    url=url,
                    local_path=full_path,
                    file_name=name,
                    file_md5=md5,
                    found=True,
                )

        # Fallback 2: try to find by filename from content
        if os.path.isfile(msg_db):
            try:
                conn = sqlite3.connect(msg_db)
                cur = conn.cursor()
                row = cur.execute(
                    "SELECT content FROM message_table WHERE message_id = ?",
                    (message_id,),
                ).fetchone()
                conn.close()
                if row and row[0] and isinstance(row[0], bytes):
                    fname = self.extract_file_name(row[0])
                    if fname:
                        result = self._resolve_by_file_name(fname)
                        if result:
                            fn, fp, fmd5 = result
                            return ImageInfo(
                                message_id=message_id,
                                url=url,
                                local_path=fp,
                                file_name=fn,
                                file_md5=fmd5 or md5,
                                found=True,
                            )
            except Exception:
                pass

        return ImageInfo(message_id=message_id, url=url, file_md5=md5, found=False)

    def resolve_message_all(self, message_id: int) -> list[ImageInfo]:
        """Resolve every image in a message (handles multi-image messages).

        A single WeCom message can carry many images (e.g. a screenshot album);
        each is stored as a separate ``file_table4`` row with a distinct
        ``file_index``. This iterates all of them.

        Args:
            message_id: The message_id from message_table.

        Returns:
            List of ImageInfo, one per image (empty if none found).
        """
        file_db = os.path.join(self._decrypted_dir, "file.db")
        if not os.path.isfile(file_db):
            return []
        try:
            conn = sqlite3.connect(file_db)
            cur = conn.cursor()
            rows = cur.execute(
                "SELECT server_id, md5, name, file_index FROM file_table4 "
                "WHERE message_id = ? AND message_type = 1 ORDER BY file_index ASC",
                (message_id,),
            ).fetchall()
            conn.close()
        except Exception:
            return []

        if not rows:
            return []

        url = ""
        msg_db = os.path.join(self._decrypted_dir, "message.db")
        if os.path.isfile(msg_db):
            try:
                conn = sqlite3.connect(msg_db)
                cur = conn.cursor()
                row = cur.execute(
                    "SELECT content FROM message_table WHERE message_id = ? AND content_type IN (4, 14, 15, 123, 653)",
                    (message_id,),
                ).fetchone()
                conn.close()
                if row and row[0]:
                    url = self.extract_image_url(row[0]) or ""
            except Exception:
                pass

        infos: list[ImageInfo] = []
        index = self._build_file_index()
        for server_id, md5, name, file_index in rows:
            info = self._resolve_one(
                server_id or "", md5 or "", name or "", url, message_id, file_index, index
            )
            if info.found:
                infos.append(info)
        return infos

    def _resolve_one(
        self,
        server_id: str,
        md5: str,
        name: str,
        url: str,
        message_id: int,
        file_index: int,
        index: dict,
    ) -> ImageInfo:
        """Resolve a single file_table4 row to ImageInfo."""
        # 1) CacheMapping by server_id
        if server_id and self._cache_mapping_db:
            try:
                conn = sqlite3.connect(self._cache_mapping_db)
                row = conn.execute(
                    "SELECT file_name, file_md5 FROM mapping WHERE key = ? LIMIT 1",
                    (server_id,),
                ).fetchone()
                conn.close()
                if row and row[0]:
                    full_path = index.get(row[0])
                    if full_path and os.path.isfile(full_path):
                        return ImageInfo(
                            message_id=message_id, url=url, local_path=full_path,
                            file_name=row[0], file_md5=md5 or row[1] or "",
                            file_index=file_index, found=True,
                        )
                    direct = os.path.join(self._cache_image_dir, row[0])
                    if os.path.isfile(direct):
                        return ImageInfo(
                            message_id=message_id, url=url, local_path=direct,
                            file_name=row[0], file_md5=md5 or row[1] or "",
                            file_index=file_index, found=True,
                        )
            except Exception:
                pass
        # 2) CacheMapping by md5
        if md5 and self._cache_mapping_db:
            try:
                conn = sqlite3.connect(self._cache_mapping_db)
                row = conn.execute(
                    "SELECT file_name FROM mapping WHERE file_md5 = ? LIMIT 1",
                    (md5.upper(),),
                ).fetchone()
                conn.close()
                if row and row[0]:
                    full_path = index.get(row[0])
                    if full_path and os.path.isfile(full_path):
                        return ImageInfo(
                            message_id=message_id, url=url, local_path=full_path,
                            file_name=row[0], file_md5=md5,
                            file_index=file_index, found=True,
                        )
            except Exception:
                pass
        # 3) Direct lookup by file_table4.name (handles ct=653 Screenshot_xxx.jpg etc.)
        if name:
            full_path = index.get(name)
            if full_path and os.path.isfile(full_path):
                return ImageInfo(
                    message_id=message_id, url=url, local_path=full_path,
                    file_name=name, file_md5=md5,
                    file_index=file_index, found=True,
                )
        return ImageInfo(message_id=message_id, url=url, file_md5=md5, file_index=file_index, found=False)

    def resolve_by_content(self, content: bytes, message_id: int = 0) -> ImageInfo:
        """Resolve image directly from protobuf content bytes.

        This is a lightweight resolver that doesn't need file.db.
        Uses CacheMapping file_name column as the primary lookup.

        Args:
            content: Raw bytes from message_table.content (content_type=4).
            message_id: Optional message_id for the result.

        Returns:
            ImageInfo with resolution results.
        """
        if not content:
            return ImageInfo(message_id=message_id, found=False)

        # Try URL extraction
        url = self.extract_image_url(content) or ""

        # Try filename extraction
        fname = self.extract_file_name(content)
        if fname:
            result = self._resolve_by_file_name(fname)
            if result:
                fn, fp, fmd5 = result
                return ImageInfo(
                    message_id=message_id,
                    url=url,
                    local_path=fp,
                    file_name=fn,
                    file_md5=fmd5,
                    found=True,
                )

        return ImageInfo(message_id=message_id, url=url, found=False)

    def resolve_conversation(
        self,
        conversation_id: str,
        limit: int = 1000,
        offset: int = 0,
    ) -> list[ImageInfo]:
        """Resolve all image messages in a conversation.

        Args:
            conversation_id: Conversation ID (e.g. R:12345).
            limit: Max messages to process.
            offset: Pagination offset.

        Returns:
            List of ImageInfo for each image message.
        """
        msg_db = os.path.join(self._decrypted_dir, "message.db")
        file_db = os.path.join(self._decrypted_dir, "file.db")

        if not os.path.isfile(msg_db):
            return []

        # Get all image message_ids for this conversation
        try:
            conn = sqlite3.connect(msg_db)
            cur = conn.cursor()
            rows = cur.execute(
                "SELECT message_id FROM message_table "
                "WHERE conversation_id = ? AND content_type IN (4, 14, 15, 123, 653) "
                "ORDER BY send_time DESC LIMIT ? OFFSET ?",
                (conversation_id, limit, offset),
            ).fetchall()
            conn.close()
        except Exception:
            return []

        # Batch resolve
        return [self.resolve_message(row[0]) for row in rows]

    def export_image(
        self,
        message_id: int,
        output_dir: str,
        overwrite: bool = False,
    ) -> Optional[str]:
        """Export a single image message to output directory.

        Args:
            message_id: The message_id from message_table.
            output_dir: Directory to copy the image to.
            overwrite: Whether to overwrite existing files.

        Returns:
            Path to exported file, or None if failed.
        """
        info = self.resolve_message(message_id)
        if not info.found or not info.local_path:
            return None

        os.makedirs(output_dir, exist_ok=True)

        # Use original name if available, otherwise message_id
        ext = os.path.splitext(info.file_name)[1] if info.file_name else ".jpg"
        out_name = f"{message_id}{ext}"
        out_path = os.path.join(output_dir, out_name)

        if os.path.exists(out_path) and not overwrite:
            return out_path

        shutil.copy2(info.local_path, out_path)
        return out_path

    def export_conversation(
        self,
        conversation_id: str,
        output_dir: str,
        limit: int = 10000,
        overwrite: bool = False,
    ) -> dict:
        """Export all images from a conversation.

        Args:
            conversation_id: Conversation ID.
            output_dir: Directory to copy images to.
            limit: Max messages to process.
            overwrite: Whether to overwrite existing files.

        Returns:
            Dict with export statistics.
        """
        os.makedirs(output_dir, exist_ok=True)
        infos = self.resolve_conversation(conversation_id, limit=limit)

        exported = 0
        skipped = 0
        failed = 0

        for info in infos:
            if not info.found or not info.local_path:
                failed += 1
                continue

            ext = os.path.splitext(info.file_name)[1] if info.file_name else ".jpg"
            out_name = f"{info.message_id}{ext}"
            out_path = os.path.join(output_dir, out_name)

            if os.path.exists(out_path) and not overwrite:
                skipped += 1
                continue

            try:
                shutil.copy2(info.local_path, out_path)
                exported += 1
            except Exception:
                failed += 1

        return {
            "conversation_id": conversation_id,
            "total_images": len(infos),
            "exported": exported,
            "skipped": skipped,
            "failed": failed,
            "output_dir": output_dir,
        }

    def stats(self) -> dict:
        """Get statistics about image cache and mapping.

        Returns:
            Dict with cache and mapping statistics.
        """
        result = {
            "cache_image_dir": self._cache_image_dir,
            "cache_mapping_db": self._cache_mapping_db,
            "cache_exists": os.path.isdir(self._cache_image_dir),
            "mapping_exists": self._cache_mapping_db is not None,
        }

        # Count cached files (Image + File)
        for cache_subdir in ("Image", "File"):
            cache_dir = os.path.join(self._db_dir, "Cache", cache_subdir)
            if os.path.isdir(cache_dir):
                total_files = 0
                month_dirs = 0
                for month_dir in os.listdir(cache_dir):
                    month_path = os.path.join(cache_dir, month_dir)
                    if os.path.isdir(month_path):
                        month_dirs += 1
                        total_files += len(os.listdir(month_path))
                result[f"cached_{cache_subdir.lower()}_files"] = total_files
                result[f"{cache_subdir.lower()}_month_dirs"] = month_dirs

        # Count mapping entries
        if self._cache_mapping_db:
            try:
                conn = sqlite3.connect(self._cache_mapping_db)
                cur = conn.cursor()
                result["mapping_total"] = cur.execute(
                    "SELECT COUNT(*) FROM mapping"
                ).fetchone()[0]
                result["mapping_with_url"] = cur.execute(
                    "SELECT COUNT(*) FROM mapping WHERE key LIKE '%qpic.cn%'"
                ).fetchone()[0]
                result["mapping_type_distribution"] = dict(
                    cur.execute("SELECT type, COUNT(*) FROM mapping GROUP BY type").fetchall()
                )
                conn.close()
            except Exception as e:
                result["mapping_error"] = str(e)

        # Count image messages
        msg_db = os.path.join(self._decrypted_dir, "message.db")
        if os.path.isfile(msg_db):
            try:
                conn = sqlite3.connect(msg_db)
                cur = conn.cursor()
                result["image_messages"] = cur.execute(
                    "SELECT COUNT(*) FROM message_table WHERE content_type IN (4, 14, 15, 123, 653)"
                ).fetchone()[0]
                conn.close()
            except Exception as e:
                result["message_error"] = str(e)

        # Count file_table4 image entries
        file_db = os.path.join(self._decrypted_dir, "file.db")
        if os.path.isfile(file_db):
            try:
                conn = sqlite3.connect(file_db)
                cur = conn.cursor()
                result["file_table_images"] = cur.execute(
                    "SELECT COUNT(*) FROM file_table4 WHERE message_type = 1"
                ).fetchone()[0]
                conn.close()
            except Exception as e:
                result["file_db_error"] = str(e)

        return result
