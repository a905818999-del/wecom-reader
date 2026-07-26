"""Main WeComReader class — unified interface for WeCom chat data access."""

import os
import shutil
import sqlite3
import tempfile
from typing import Optional

from .crypto.decrypt import (
    decrypt_database,
    decrypt_page,
    is_plain_sqlite,
    is_wxsqlite3_aes128_page1,
    verify_key,
)
from .crypto.key_extract import extract_key
from .db.contact import build_user_map, get_group_members, list_contacts
from .db.message import get_message_count, get_messages, search_messages
from .db.session import get_session_count, list_sessions
from .image_resolver import ImageResolver, ResolvedImage
from .wal import recover_wal


def _database_fingerprint(path: str) -> tuple[int, int]:
    """Return main database metadata that should stay stable during a snapshot."""
    stat = os.stat(path)
    return stat.st_size, stat.st_mtime_ns


def _wal_fingerprint(path: str) -> tuple[int, int, bytes] | None:
    """Return enough WAL metadata to detect a reset or concurrent write."""
    try:
        stat = os.stat(path)
        with open(path, "rb") as wal_file:
            header = wal_file.read(32)
    except FileNotFoundError:
        return None
    return stat.st_size, stat.st_mtime_ns, header


def _copy_database_snapshot(db_path: str, snapshot_dir: str) -> tuple[str, str | None]:
    """Copy a stable main/WAL pair without opening the live database."""
    db_copy = os.path.join(snapshot_dir, os.path.basename(db_path))
    wal_path = f"{db_path}-wal"
    wal_copy = f"{db_copy}-wal"

    for _attempt in range(3):
        try:
            db_before = _database_fingerprint(db_path)
            wal_before = _wal_fingerprint(wal_path)
            shutil.copy2(db_path, db_copy)
            if wal_before is not None:
                shutil.copy2(wal_path, wal_copy)
            elif os.path.exists(wal_copy):
                os.remove(wal_copy)
            db_after = _database_fingerprint(db_path)
            wal_after = _wal_fingerprint(wal_path)
        except OSError:
            for partial_path in (db_copy, wal_copy):
                if os.path.exists(partial_path):
                    os.remove(partial_path)
            continue
        if db_before == db_after and wal_before == wal_after:
            return db_copy, wal_copy if wal_before is not None else None

    raise RuntimeError(
        "database or WAL changed while the read-only snapshot was copied"
    )


def _sqlite_quick_check(path: str) -> str:
    """Run SQLite's lightweight structural verification on a candidate snapshot."""
    try:
        connection = sqlite3.connect(path)
        try:
            row = connection.execute("PRAGMA quick_check").fetchone()
        finally:
            connection.close()
    except sqlite3.DatabaseError as exc:
        return str(exc)
    return row[0] if row else "missing quick_check result"


def _publish_without_wal(candidate_path: str, out_path: str) -> None:
    """Publish a verified candidate without exposing a partially written file."""
    check = _sqlite_quick_check(candidate_path)
    if check != "ok":
        raise RuntimeError(f"SQLite quick_check failed: {check}")
    os.replace(candidate_path, out_path)


class WeComReader:
    """Agent-reusable WeCom (企业微信) local chat reader.

    Usage:
        reader = WeComReader()           # auto-detect
        reader = WeComReader(db_dir=...) # explicit path

        # One-step init + decrypt
        reader.init()

        # Query
        sessions = reader.list_sessions()
        msgs = reader.get_messages("R:12345")
        results = reader.search_messages("keyword")
        contacts = reader.contacts()
    """

    def __init__(
        self,
        db_dir: Optional[str] = None,
        decrypted_dir: Optional[str] = None,
        key_map: Optional[dict] = None,
    ):
        """Initialize reader.

        Args:
            db_dir: Path to WeCom Data directory (auto-detected if None).
            decrypted_dir: Path for decrypted DB output (default: ./wxwork_decrypted).
            key_map: Pre-extracted key map from extract_key(). If None, init() will extract.
        """
        self._db_dir = db_dir
        self._decrypted_dir = decrypted_dir or os.path.join(
            os.getcwd(), "wxwork_decrypted"
        )
        self._key_map = key_map
        self._user_map: Optional[dict] = None
        self._image_resolver: Optional[ImageResolver] = None

    @property
    def db_dir(self) -> Optional[str]:
        return self._db_dir

    @property
    def decrypted_dir(self) -> str:
        return self._decrypted_dir

    def status(self) -> dict:
        """Check current status of decrypted data."""
        result = {
            "db_dir": self._db_dir,
            "decrypted_dir": self._decrypted_dir,
            "decrypted": os.path.isdir(self._decrypted_dir),
            "databases": {},
        }

        if os.path.isdir(self._decrypted_dir):
            for name in os.listdir(self._decrypted_dir):
                path = os.path.join(self._decrypted_dir, name)
                if name.endswith(".db") and os.path.isfile(path):
                    sz = os.path.getsize(path)
                    try:
                        conn = sqlite3.connect(path)
                        tables = [
                            r[0]
                            for r in conn.execute(
                                "SELECT name FROM sqlite_master WHERE type='table'"
                            ).fetchall()
                        ]
                        conn.close()
                    except Exception:
                        tables = []
                    result["databases"][name] = {
                        "size_mb": round(sz / 1024 / 1024, 1),
                        "tables": tables,
                    }

        return result

    def init(
        self,
        timeout: int = 120,
        verbose: bool = False,
    ) -> dict:
        """Extract keys from WXWork.exe and decrypt all databases.

        Args:
            timeout: Max seconds for memory scan per process.
            verbose: Print progress.

        Returns:
            Dict with success status, key count, db count, etc.
        """
        # Extract keys
        if self._key_map is None:
            if verbose:
                print("[*] Extracting keys from WXWork.exe memory...")
            self._key_map = extract_key(
                db_dir=self._db_dir, timeout=timeout, verbose=verbose
            )

        self._db_dir = self._key_map.get("_db_dir", self._db_dir)
        if not self._db_dir:
            raise RuntimeError("No db_dir found in key map")

        # Decrypt databases
        os.makedirs(self._decrypted_dir, exist_ok=True)
        success = 0
        copied = 0
        failed = 0
        wal_present: list[str] = []
        wal_recovered: list[str] = []
        wal_degraded: list[str] = []
        wal_failed: list[str] = []
        wal_retained_snapshot: list[str] = []
        wal_warnings: list[str] = []

        for root, dirs, files in os.walk(self._db_dir):
            dirs[:] = [d for d in dirs if d not in ("-journal",)]
            for name in files:
                if (
                    not name.endswith(".db")
                    or name.endswith("-wal")
                    or name.endswith("-shm")
                ):
                    continue
                path = os.path.join(root, name)
                rel = os.path.relpath(path, self._db_dir)
                out_path = os.path.join(self._decrypted_dir, rel)

                out_dir = os.path.dirname(out_path)
                os.makedirs(out_dir, exist_ok=True)
                candidate_fd, candidate_path = tempfile.mkstemp(
                    prefix=f".{name}.", suffix=".candidate", dir=out_dir
                )
                os.close(candidate_fd)
                published = False
                was_encrypted = False
                try:
                    with tempfile.TemporaryDirectory(
                        prefix="wecom-reader-snapshot-"
                    ) as snapshot_dir:
                        db_snapshot, wal_snapshot = _copy_database_snapshot(
                            path, snapshot_dir
                        )
                        with open(db_snapshot, "rb") as db_file:
                            page1 = db_file.read(4096)

                        raw_key: bytes | None = None
                        if is_plain_sqlite(page1):
                            shutil.copy2(db_snapshot, candidate_path)
                            was_encrypted = False
                        elif is_wxsqlite3_aes128_page1(page1):
                            key_hex = self._key_map.get(page1[:16].hex())
                            if not key_hex:
                                for key_salt, key_value in self._key_map.items():
                                    if key_salt.startswith("_"):
                                        continue
                                    if verify_key(bytes.fromhex(key_value), page1):
                                        key_hex = key_value
                                        break
                            if not key_hex:
                                raise RuntimeError("no matching database key")
                            raw_key = bytes.fromhex(key_hex)
                            decrypt_database(db_snapshot, candidate_path, raw_key)
                            was_encrypted = True
                        else:
                            raise RuntimeError("unsupported database header")

                        if wal_snapshot is None or os.path.getsize(wal_snapshot) == 0:
                            _publish_without_wal(candidate_path, out_path)
                            published = True
                        else:
                            wal_present.append(rel)
                            decoder = None
                            if raw_key is not None:

                                def decoder(page_no, payload):
                                    return decrypt_page(raw_key, payload, page_no)

                            try:
                                recovery = recover_wal(
                                    candidate_path,
                                    wal_snapshot,
                                    out_path,
                                    page_decoder=decoder,
                                    strict=False,
                                )
                            except (OSError, ValueError) as exc:
                                detail = str(exc)
                                recovery = None
                            if recovery is not None and recovery.applied:
                                wal_recovered.append(rel)
                                published = True
                                if recovery.scan.error is not None:
                                    scan_error = recovery.scan.error
                                    wal_degraded.append(rel)
                                    wal_warnings.append(
                                        f"{rel}: recovered through commit "
                                        f"{recovery.scan.last_valid_commit_index}; "
                                        f"checkpoint blocked by {scan_error.kind} at frame "
                                        f"{scan_error.frame_index}"
                                    )
                            else:
                                if (
                                    recovery is not None
                                    and recovery.scan.error is not None
                                ):
                                    scan_error = recovery.scan.error
                                    detail = (
                                        f"{scan_error.kind} at frame "
                                        f"{scan_error.frame_index}: {scan_error.message}"
                                    )
                                elif (
                                    recovery is not None
                                    and recovery.scan.last_valid_commit_index is None
                                ):
                                    detail = "no committed WAL frame"
                                elif recovery is not None:
                                    detail = (
                                        f"quick_check failed: {recovery.quick_check}"
                                    )
                                else:
                                    detail = f"WAL validation failed: {detail}"
                                wal_failed.append(rel)
                                wal_warnings.append(f"{rel}: {detail}")
                                if not os.path.exists(out_path):
                                    _publish_without_wal(candidate_path, out_path)
                                    published = True
                                else:
                                    wal_retained_snapshot.append(rel)

                        if published:
                            if was_encrypted:
                                success += 1
                            else:
                                copied += 1
                except (OSError, RuntimeError, ValueError) as exc:
                    failed += 1
                    if rel in wal_present and rel not in wal_failed:
                        wal_failed.append(rel)
                        wal_warnings.append(f"{rel}: snapshot failed: {exc}")
                finally:
                    if os.path.exists(candidate_path):
                        os.remove(candidate_path)

        return {
            "success": success + copied > 0 or bool(wal_retained_snapshot),
            "decrypted": success,
            "copied": copied,
            "failed": failed,
            "decrypted_dir": self._decrypted_dir,
            "wal_present": wal_present,
            "wal_recovered": wal_recovered,
            "wal_degraded": wal_degraded,
            "wal_failed": wal_failed,
            "wal_retained_snapshot": wal_retained_snapshot,
            "wal_checkpoint_safe": not wal_degraded and not wal_failed,
            "wal_warning": "; ".join(wal_warnings) if wal_warnings else None,
        }

    def _get_db_path(self, name: str) -> Optional[str]:
        """Get path to a decrypted database file."""
        if not os.path.isdir(self._decrypted_dir):
            return None
        path = os.path.join(self._decrypted_dir, name)
        return path if os.path.isfile(path) else None

    def _ensure_user_map(self):
        """Lazy-load user map for sender name resolution."""
        if self._user_map is not None:
            return
        user_db = self._get_db_path("user.db")
        if user_db:
            self._user_map = build_user_map(user_db)
        else:
            self._user_map = {}

    def list_sessions(
        self,
        limit: int = 50,
        offset: int = 0,
        keyword: Optional[str] = None,
        session_type: Optional[str] = None,
    ) -> list[dict]:
        """List WeCom sessions/conversations."""
        session_db = self._get_db_path("session.db")
        if not session_db:
            return []
        return list_sessions(
            session_db,
            limit=limit,
            offset=offset,
            keyword=keyword,
            session_type=session_type,
        )

    def get_messages(
        self,
        conversation_id: str,
        limit: int = 50,
        offset: int = 0,
        since: Optional[int] = None,
        until: Optional[int] = None,
    ) -> list[dict]:
        """Get messages for a conversation."""
        msg_db = self._get_db_path("message.db")
        if not msg_db:
            return []

        self._ensure_user_map()
        messages = get_messages(
            msg_db,
            conversation_id,
            limit=limit,
            offset=offset,
            since=since,
            until=until,
        )

        # Enrich with sender names
        for msg in messages:
            sender_id = msg.get("sender_id")
            if sender_id and isinstance(sender_id, int) and sender_id in self._user_map:
                msg["sender_name"] = self._user_map[sender_id]

        return messages

    def search_messages(
        self,
        keyword: str,
        conversation_id: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]:
        """Search messages by keyword."""
        msg_db = self._get_db_path("message.db")
        if not msg_db:
            return []

        self._ensure_user_map()
        results = search_messages(
            msg_db, keyword, conversation_id=conversation_id, limit=limit
        )

        for msg in results:
            sender_id = msg.get("sender_id")
            if sender_id and isinstance(sender_id, int) and sender_id in self._user_map:
                msg["sender_name"] = self._user_map[sender_id]

        return results

    def contacts(
        self,
        keyword: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """List contacts."""
        user_db = self._get_db_path("user.db")
        if not user_db:
            return []
        return list_contacts(user_db, keyword=keyword, limit=limit, offset=offset)

    def group_members(self, conversation_id: str) -> dict[int, str]:
        """Get group members with nicknames."""
        session_db = self._get_db_path("session.db")
        if not session_db:
            return {}
        return get_group_members(session_db, conversation_id)

    def session_count(self) -> int:
        """Get total session count."""
        session_db = self._get_db_path("session.db")
        if not session_db:
            return 0
        return get_session_count(session_db)

    def message_count(self, conversation_id: str) -> int:
        """Get message count for a conversation."""
        msg_db = self._get_db_path("message.db")
        if not msg_db:
            return 0
        return get_message_count(msg_db, conversation_id)

    @property
    def image_resolver(self) -> ImageResolver:
        """Return the image resolver without duplicating its lookup logic."""
        if self._image_resolver is None:
            self._image_resolver = ImageResolver(self._db_dir, self._decrypted_dir)
        return self._image_resolver

    def resolve_image(self, message_id: str) -> Optional[ResolvedImage]:
        """Resolve an image message to a validated local cache file."""
        return self.image_resolver.resolve_image(message_id)
