"""wxSQLite3 AES-128-CBC database decryption for WeCom (企业微信).

Encryption parameters (from wechat-decrypt wxwork_crypto.py):
- Algorithm: AES-128-CBC per page
- Key derivation: MD5(raw_key + page_no_le32 + "sAlT")
- IV derivation: MD5 of pseudo-random initkey based on page_no
- No HMAC, no reserve area (unlike SQLCipher)
"""

import hashlib
import os
import struct
from typing import Optional

from Cryptodome.Cipher import AES

PAGE_SZ = 4096
SQLITE_HDR = b"SQLite format 3\x00"
WXSQLITE3_SALT = b"sAlT"


def _modmult(a: int, b: int, c: int, m: int, s: int) -> int:
    """Modular multiplication matching SQLite3MultipleCiphers."""
    q = s // a
    s = b * (s - a * q) - c * q
    if s < 0:
        s += m
    return s


def generate_initial_vector(page_no: int) -> bytes:
    """Generate per-page IV matching sqlite3mcGenerateInitialVector()."""
    z = page_no + 1
    initkey = bytearray(16)
    for idx in range(4):
        z = _modmult(52774, 40692, 3791, 2147483399, z)
        initkey[idx * 4 : idx * 4 + 4] = struct.pack("<I", z & 0xFFFFFFFF)
    return hashlib.md5(initkey).digest()


def derive_page_key(raw_key: bytes, page_no: int) -> bytes:
    """Derive per-page AES-128 key from raw_key and page number."""
    if len(raw_key) != 16:
        raise ValueError("wxSQLite3 AES-128 raw key must be 16 bytes")
    material = raw_key + struct.pack("<I", page_no) + WXSQLITE3_SALT
    return hashlib.md5(material).digest()


def is_plain_sqlite(page: bytes) -> bool:
    """Check if page starts with SQLite header (unencrypted)."""
    return page[: len(SQLITE_HDR)] == SQLITE_HDR


def has_wxsqlite3_plain_header_fragment(page: bytes) -> bool:
    """Check for wxSQLite3 AES mode: header bytes 16..23 kept in plaintext."""
    if len(page) < 24:
        return False
    header = page[16:24]
    page_size = (header[0] << 8) | header[1]
    if page_size == 1:
        page_size = 65536
    return (
        512 <= page_size <= 65536
        and (page_size & (page_size - 1)) == 0
        and header[5] == 0x40
        and header[6] == 0x20
        and header[7] == 0x20
    )


def is_wxsqlite3_aes128_page1(page: bytes) -> bool:
    """Check if page 1 is wxSQLite3 AES-128 encrypted."""
    return not is_plain_sqlite(page) and has_wxsqlite3_plain_header_fragment(page)


def _decrypt_aes128_cbc(raw_key: bytes, page_no: int, data: bytes) -> bytes:
    page_key = derive_page_key(raw_key, page_no)
    iv = generate_initial_vector(page_no)
    return AES.new(page_key, AES.MODE_CBC, iv).decrypt(data)


def decrypt_page(raw_key: bytes, page_data: bytes, page_no: int) -> bytes:
    """Decrypt one wxSQLite3 AES-128-CBC page."""
    if len(page_data) != PAGE_SZ:
        raise ValueError(f"page must be exactly {PAGE_SZ} bytes")

    data = bytearray(page_data)
    if page_no == 1 and has_wxsqlite3_plain_header_fragment(data):
        db_header_fragment = bytes(data[16:24])
        data[16:24] = data[8:16]
        decrypted_tail = _decrypt_aes128_cbc(raw_key, page_no, bytes(data[16:]))
        data[16:] = decrypted_tail
        if bytes(data[16:24]) != db_header_fragment:
            raise ValueError("wxSQLite3 AES-128 key validation failed")
        data[:16] = SQLITE_HDR
        return bytes(data)

    return _decrypt_aes128_cbc(raw_key, page_no, bytes(data))


def looks_like_sqlite_page1(page: bytes) -> bool:
    """Verify decrypted page looks like a valid SQLite page 1."""
    if page[: len(SQLITE_HDR)] != SQLITE_HDR:
        return False
    if len(page) < 108:
        return False
    btree_page_type = page[100]
    return btree_page_type in (0x02, 0x05, 0x0A, 0x0D)


def verify_key(raw_key: bytes, page1: bytes) -> bool:
    """Verify raw_key can decrypt page 1."""
    if len(raw_key) != 16 or len(page1) < PAGE_SZ:
        return False
    try:
        decrypted = decrypt_page(raw_key, page1[:PAGE_SZ], 1)
    except (ValueError, KeyError):
        return False
    return looks_like_sqlite_page1(decrypted)


def decrypt_database(db_path: str, out_path: str, raw_key: bytes) -> None:
    """Decrypt an entire wxSQLite3 AES-128-CBC database file."""
    size = os.path.getsize(db_path)
    total_pages = (size + PAGE_SZ - 1) // PAGE_SZ
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    with open(db_path, "rb") as fin, open(out_path, "wb") as fout:
        for page_no in range(1, total_pages + 1):
            page = fin.read(PAGE_SZ)
            if not page:
                break
            if len(page) < PAGE_SZ:
                page += b"\x00" * (PAGE_SZ - len(page))
            fout.write(decrypt_page(raw_key, page, page_no))


def decrypt_wal_pages(raw_key: bytes, wal_data: bytes) -> list[tuple[int, bytes]]:
    """Decrypt pages from a wxSQLite3 AES-128 WAL file.

    NOTE (2026-06-26): The page_no encoding for WeCom WAL has not yet been
    fully validated. Inspection of E:\\WXWork\\...\\message.db-wal shows the
    file is NOT a standard SQLite WAL (first 4 bytes are 0x377f0682, not
    "SQLite WAL\\n"), and decrypting frames with the same key as the main
    db produces garbage. The page_no byte-order/offset hypotheses below
    are best-effort and disabled by default.

    The 24-byte frame-header layout follows the SQLite WAL spec:
        offset 0..4   : page_no (big-endian, 4 bytes)
        offset 4..8   : db_size after commit (big-endian, 4 bytes)
        offset 8..24  : checksum + salts (16 bytes)
        offset 24..   : PAGE_SZ bytes of encrypted page data
    """
    import struct

    out: list[tuple[int, bytes]] = []
    FRAME_HDR = 24

    if len(wal_data) < FRAME_HDR + PAGE_SZ:
        return out

    i = 0
    while i + FRAME_HDR + PAGE_SZ <= len(wal_data):
        # Try the SQLite-spec layout: page_no big-endian at frame-header offset 0.
        page_no = struct.unpack_from(">I", wal_data, i)[0]
        encrypted = wal_data[i + FRAME_HDR : i + FRAME_HDR + PAGE_SZ]
        try:
            decrypted = decrypt_page(raw_key, encrypted, page_no)
        except Exception:
            i += FRAME_HDR + PAGE_SZ
            continue
        if page_no >= 1 and decrypted[:16] == SQLITE_HDR:
            out.append((page_no, decrypted))
        i += FRAME_HDR + PAGE_SZ

    return out


def decrypt_wal_file(wal_path: str, raw_key: bytes) -> list[tuple[int, bytes]]:
    """Read WAL file from disk and decrypt all valid pages.

    Returns only frames that decrypt to a valid SQLite page header — this
    guards against silent corruption from misaligned frame offsets.
    """
    with open(wal_path, "rb") as f:
        data = f.read()
    return decrypt_wal_pages(raw_key, data)
