# WAL format research notes for issue #7

Date: 2026-07-02

Scope: research only. No production code was changed.

## Summary

`0x377f0682` is not a custom WCDB/wxSQLite3 magic. It is one of the two standard
SQLite WAL magic values. The value means the WAL checksum input is interpreted
as little-endian 32-bit words; the WAL header and frame header fields themselves
remain stored as big-endian 32-bit integers.

For encrypted wxSQLite3 / SQLite3 Multiple Ciphers databases, the WAL container
stays standard SQLite WAL:

- 32-byte WAL file header.
- Repeated frames of 24-byte frame header plus one database page.
- The frame header is plaintext metadata.
- The frame payload is the encrypted database page.
- The page number is read from the frame header and passed into the same page
  codec that encrypts/decrypts main database pages.

This makes automatic WAL merging feasible, but it should be implemented as a
careful shadow-copy/checkpoint pipeline rather than by opening live WeCom files
in place.

## Sources checked

- SQLite file format: <https://www.sqlite.org/fileformat.html>
- SQLite WAL-mode details: <https://sqlite.org/walformat.html>
- SQLite `wal.c`: <https://github.com/sqlite/sqlite/blob/master/src/wal.c>
- SQLite3 Multiple Ciphers `sqlite3mc_vfs.c`:
  <https://raw.githubusercontent.com/utelle/SQLite3MultipleCiphers/master/src/sqlite3mc_vfs.c>
- SQLite3 Multiple Ciphers `codec_algos.c`:
  <https://raw.githubusercontent.com/utelle/SQLite3MultipleCiphers/master/src/codec_algos.c>
- SQLite3 Multiple Ciphers `cipher_wxaes128.c`:
  <https://raw.githubusercontent.com/utelle/SQLite3MultipleCiphers/master/src/cipher_wxaes128.c>
- wxSQLite3 project docs/changelog:
  <https://github.com/utelle/wxsqlite3>
  and <https://utelle.github.io/wxsqlite3/>
- Prior project branch `origin/fix/multi-table-pagination`, where
  `decrypt_wal_pages()` and `decrypt_wal_file()` exist in
  `wecom_reader/crypto/decrypt.py`.

Notes:

- `origin/main` currently does not contain `decrypt_wal_pages()` /
  `decrypt_wal_file()`, but `AGENTS.md` and the issue prompt refer to them.
  They exist on `origin/fix/multi-table-pagination`.
- `tests/smoke_message.py` also exists on `origin/fix/multi-table-pagination`,
  not on `origin/main`.
- `.workbuddy/memory/2026-06-26.md` was not present in `origin/main` or
  `origin/fix/multi-table-pagination`.

## WAL file header

SQLite WAL begins with a 32-byte header:

| Offset | Size | Meaning |
|---:|---:|---|
| 0 | 4 | Magic: `0x377f0682` or `0x377f0683` |
| 4 | 4 | WAL format version, normally `3007000` |
| 8 | 4 | Database page size |
| 12 | 4 | Checkpoint sequence number |
| 16 | 4 | Salt 1 |
| 20 | 4 | Salt 2 |
| 24 | 4 | Checksum 1 |
| 28 | 4 | Checksum 2 |

The header fields are stored as big-endian integers. The magic controls checksum
calculation byte order:

- `0x377f0682`: checksum input words are little-endian.
- `0x377f0683`: checksum input words are big-endian.

Local read-only inspection of a live `message.db-wal` showed:

- magic: `0x377f0682`
- version: `3007000`
- page size: `4096`
- first frame starts immediately after the 32-byte WAL header

No real message contents or database bytes are included in this note.

## WAL frame header

Each frame is:

```text
24-byte frame header + page_size bytes of page data
```

The 24-byte frame header layout is standard SQLite:

| Offset | Size | Meaning |
|---:|---:|---|
| 0 | 4 | Page number, big-endian |
| 4 | 4 | Commit database size in pages; zero for non-commit frames |
| 8 | 4 | Salt 1, copied from WAL header |
| 12 | 4 | Salt 2, copied from WAL header |
| 16 | 4 | Checksum 1 |
| 20 | 4 | Checksum 2 |

For the inspected local WAL, the first frame header decoded as:

- page number, big-endian: `3`
- commit db size: `0`
- salts matched the WAL header salts

The little-endian interpretation of the same first four bytes would be
`50331648`, which is implausible for the database size. So page number should be
read using SQLite's frame-header rule: big-endian 32-bit.

## Encryption relationship to main database pages

The project's main database decryption matches SQLite3 Multiple Ciphers /
wxSQLite3 AES-128-CBC behavior:

- page key: `MD5(raw_key || page_no_le32 || "sAlT")`
- IV: generated from page number using the same deterministic pseudo-random
  sequence, then MD5
- page 1 has special handling for plaintext SQLite header bytes 16..23
- other pages decrypt as a full 4096-byte AES-CBC page

SQLite3 Multiple Ciphers confirms the WAL path uses the same codec:

- `sqlite3mcPagerCodec()` is called by SQLite's WAL module before writing page
  content into the log. It encrypts `pPg->pData` using `pPg->pgno`.
- `mcReadWal()` reads the frame page number from `offset - 24`, then decrypts
  the page payload with that page number.
- `mcWriteWal()` handles both split writes and combined frame writes. In both
  cases it obtains the frame page number and encrypts only the page payload.
- Frame headers are left as normal SQLite WAL metadata.

Therefore the implementation assumption should be:

```text
frame_page_plaintext =
    decrypt_page(raw_key, frame_payload_ciphertext, frame_header.page_no)
```

Do not treat the WAL file header or frame headers as encrypted.

## Current project helper status

On `origin/fix/multi-table-pagination`, the existing helper is:

```python
def decrypt_wal_pages(raw_key: bytes, wal_data: bytes) -> list[tuple[int, bytes]]:
    ...
```

Its current limitations:

- It starts parsing at byte offset 0, but a standard WAL has a 32-byte file
  header. Frame parsing should start at offset 32.
- It accepts only frames where the decrypted page starts with the SQLite file
  header. That only works for page 1, but most WAL frames are not page 1.
- It does not validate WAL salts or checksums.
- It treats `0x377f0682` as suspicious/non-standard in comments. That should be
  corrected.

The second-round engineering work should either move/fix this helper on the
feature branch or reimplement it with tests, depending on the actual branch base.

## Decryption experiment

Requested experiment:

> Try decrypting one frame with main db key, page_no from header, AES-128-CBC,
> and document the failure mode.

What was completed:

- Confirmed a local live `message.db-wal` exists.
- Parsed its WAL header and first frame header read-only.
- Confirmed first frame page number is big-endian `3`.
- Confirmed frame salts match WAL header salts.
- Created a local `.venv` with project dependencies via `uv sync --group dev`.

What did not complete:

- `extract_key(db_dir=..., timeout=10)` did not return before the shell command
  timeout. The outer command timed out after about 64 seconds while scanning the
  live `WXWork.exe` process memory.
- Because the main database key was not obtained during this research pass, the
  real first-frame payload was not decrypted.

Expected failure mode if using the wrong parsing assumptions:

- Starting frame parsing at byte 0 will treat the WAL header magic as a page
  number and decrypt the wrong 4096-byte slice. Output should be garbage.
- Reading page number little-endian from the frame header will use a huge wrong
  page number, producing garbage.
- Checking every decrypted page for `SQLite format 3\0` will reject valid
  decrypted non-page-1 frames.

Recommended follow-up verification:

1. Re-run key extraction with WeCom open and enough timeout/admin permissions.
2. Parse the WAL header.
3. Validate each frame salt before decrypting.
4. For a candidate frame, decrypt payload with frame `page_no` as big-endian.
5. Validate by structural page checks, not only page-1 SQLite header:
   page type should be plausible for b-tree pages where applicable, and applying
   the latest frames to a decrypted database copy should pass `PRAGMA
   integrity_check`.

## WCDB / wxSQLite3 comparison

No evidence was found that `0x377f0682` is a WCDB-specific or wxSQLite3-specific
magic. It is standard SQLite WAL magic.

WCDB builds on SQLite and supports WAL mode, but the useful code path for this
project is closer to wxSQLite3 / SQLite3 Multiple Ciphers, because the current
Python code already implements that page cipher. SQLite3 Multiple Ciphers shows
that encrypted WAL is achieved by a VFS/codec layer that encrypts/decrypts WAL
page payloads while preserving normal SQLite WAL metadata.

This also explains why the WAL can look standard at the header level while still
not be readable by stock SQLite: stock SQLite can parse frame metadata, but the
page payloads are encrypted.

## Feasibility and recommended next step

Automatic WAL merging is feasible.

Recommended approach for issue #7: approach C, shadow db, with approach A's
decoder as an internal component.

Concrete design:

1. During `init()`, copy each encrypted `*.db` and matching `*.db-wal` /
   `*.db-shm` to a temporary snapshot directory first. This avoids racing live
   WeCom writes.
2. Decrypt the main DB into `decrypted_dir` as today.
3. Decode the copied WAL:
   - read and validate 32-byte WAL header
   - require page size 4096 for now, or explicitly reject other sizes
   - iterate frames from offset 32
   - parse 24-byte frame header as big-endian fields
   - require frame salts to match WAL header salts
   - decrypt frame payload with `decrypt_page(raw_key, payload, page_no)`
   - keep the last valid frame for each page number up to the last valid commit
     frame
4. Apply the selected decrypted pages to a shadow copy of the decrypted main DB
   at `(page_no - 1) * page_size`.
5. Run `PRAGMA integrity_check` on the shadow DB.
6. If integrity check passes, atomically replace the decrypted DB snapshot or
   name it as the merged DB.
7. If WAL processing fails, keep the main decrypted DB and report
   `wal_present=True` plus a human-readable `wal_warning`.

Why not pure approach A directly in place:

- Live WAL can change while reading.
- SQLite WAL validity depends on salts, checksums, and commit frames.
- Applying uncommitted frames could corrupt the snapshot.
- A shadow copy gives a clean rollback path and an integrity check gate.

Testing recommendation for second round:

- Add synthetic encrypted WAL tests with:
  - standard 32-byte WAL header
  - one or more 24-byte frame headers
  - known plaintext pages encrypted through the existing `derive_page_key()` /
    `generate_initial_vector()` path
  - repeated page numbers where the later valid frame wins
  - invalid salt/checksum cases rejected
  - non-page-1 pages accepted after decryption
- Do not use real WeCom data in tests.
- Keep coverage focused on new WAL decoding paths in
  `wecom_reader/crypto/decrypt.py`.

Open risk:

- The checksum must be implemented exactly if we want to stop at the same
  validity boundary as SQLite. Salt-only validation is not enough for production
  merging.
- The current Python decrypt helper on the PR #2 branch likely needs correction
  before it can be trusted.
- Real-data validation still needs a successful key extraction run.
