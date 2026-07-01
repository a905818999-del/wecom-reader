"""Pytest test suite for wecom_reader.db.message.

Covers the multi-table UNION ALL implementation: get_messages, search_messages,
get_message_count. The critical test (test_offset_bug_repro) verifies the
real bug fix — when paging deep into a conversation, OFFSET must apply to
the global sorted union, not per-table.

Run from project root:
    .venv/Scripts/python.exe -m pytest tests/test_message.py -v
    .venv/Scripts/python.exe -m pytest tests/test_message.py --cov=wecom_reader.db.message --cov-report=term-missing
"""
import os
import sqlite3
import sys
import tempfile

import pytest

from wecom_reader.db.message import (
    get_message_count,
    get_messages,
    search_messages,
    MESSAGE_TABLES,
    MSG_TYPES,
    _table_exists,
    _parse_content,
    _is_pure_text,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def synthetic_db():
    """Build a 3-table message.db with controlled data.

    Conv X: 5 kf (5000..5004) + 100 main (1000..1099) + 200 small (1..200) = 305
    Conv Y: 5 main (100..104) only
    """
    tmpdir = tempfile.mkdtemp(prefix='wecom_test_')
    db = os.path.join(tmpdir, 'message.db')
    conn = sqlite3.connect(db)
    for t in MESSAGE_TABLES:
        conn.execute(f'''CREATE TABLE "{t}" (
            message_id INTEGER PRIMARY KEY, server_id TEXT, sequence INTEGER,
            sender_id INTEGER, conversation_id TEXT, content_type INTEGER,
            send_time INTEGER, flag INTEGER, content BLOB, from_app_id TEXT
        )''')
    for i in range(100):
        conn.execute('INSERT INTO message_table VALUES (?,?,?,?,?,?,?,?,?,?)',
            (i+1, f'm{i}', 1000+i, 1, 'X', 0, 1700000000+i*60, 0, f'main_{i}'.encode(), None))
    for i in range(200):
        conn.execute('INSERT INTO message_small_table VALUES (?,?,?,?,?,?,?,?,?,?)',
            (10000+i, f's{i}', 1+i, 2, 'X', 0, 1500000000+i*60, 0, f'small_{i}'.encode(), None))
    for i in range(5):
        conn.execute('INSERT INTO kf_message_tableV1 VALUES (?,?,?,?,?,?,?,?,?,?)',
            (20000+i, f'k{i}', 5000+i, 3, 'X', 0, 1800000000+i*60, 0, f'kf_{i}'.encode(), None))
    for i in range(5):
        conn.execute('INSERT INTO message_table VALUES (?,?,?,?,?,?,?,?,?,?)',
            (30000+i, f'y{i}', 100+i, 1, 'Y', 0, 1700000000+i*60, 0, f'y_{i}'.encode(), None))
    conn.commit()
    conn.close()
    return db


def _seq_set(rows):
    return set(r['sequence'] for r in rows)


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------

def test_table_exists(synthetic_db):
    conn = sqlite3.connect(synthetic_db)
    for t in MESSAGE_TABLES:
        assert _table_exists(conn, t)
    assert not _table_exists(conn, 'nonexistent_table')
    conn.close()


def test_msg_types_complete():
    """MSG_TYPES must have all common WeCom content types."""
    expected_keys = {0, 2, 4, 7, 14, 15, 38, 40, 503, 1011}
    assert expected_keys.issubset(set(MSG_TYPES.keys()))


def test_is_pure_text():
    assert _is_pure_text("hello world")
    assert _is_pure_text("中文")
    assert _is_pure_text("line1\nline2\ttab")
    assert not _is_pure_text("ctrl\x00char")
    assert not _is_pure_text("bell\x07")


def test_parse_content_none():
    assert _parse_content(None) == ""


def test_parse_content_string():
    assert _parse_content("hello") == "hello"
    assert _parse_content("  spaced  ") == "spaced"


def test_parse_content_bytes_utf8():
    raw = "中文消息".encode('utf-8')
    result = _parse_content(raw)
    assert "中文消息" in result


def test_parse_content_unparseable():
    # Random bytes
    raw = b"\x00\x01\x02\x03\xff\xfe"
    result = _parse_content(raw)
    # Should not crash; returns either empty or "[binary N bytes]"
    assert isinstance(result, str)


# ---------------------------------------------------------------------------
# get_messages tests
# ---------------------------------------------------------------------------

def test_get_messages_basic(synthetic_db):
    """limit=50, no filters: top 50 = 5 kf + 45 main."""
    r = get_messages(synthetic_db, 'X', limit=50)
    expected = set(range(5000, 5005)) | set(range(1055, 1100))
    assert _seq_set(r) == expected
    assert len(r) == 50


def test_get_messages_ascending_order(synthetic_db):
    """Per docstring: returns sorted by sequence ASC (oldest first in window)."""
    r = get_messages(synthetic_db, 'X', limit=50)
    seqs = [m['sequence'] for m in r]
    assert seqs == sorted(seqs)


def test_get_messages_spans_all_tables(synthetic_db):
    """limit=150 should pull from all 3 tables."""
    r = get_messages(synthetic_db, 'X', limit=150)
    expected = set(range(5000, 5005)) | set(range(1000, 1100)) | set(range(156, 201))
    assert _seq_set(r) == expected
    assert len(r) == 150


def test_get_messages_offset(synthetic_db):
    """offset=100, limit=50: skip 100 globally, take 50.

    Global DESC sort: 5004..5000 (5), 1099..1000 (100), 200..1 (200)
    Top 100: 5 kf + 95 main (1099..1005)
    Next 50: 5 main (1004..1000) + 45 small (200..156)
    """
    r = get_messages(synthetic_db, 'X', limit=50, offset=100)
    expected = set(range(1000, 1005)) | set(range(156, 201))
    assert _seq_set(r) == expected
    assert len(r) == 50


def test_get_messages_offset_pagination_continuity(synthetic_db):
    """Paging through covers all 305 messages without duplicates or gaps."""
    pages = [0, 50, 100, 150, 200, 250, 300]  # 7 pages × 50 (last page has 5)
    seen = set()
    for offset in pages:
        page = get_messages(synthetic_db, 'X', limit=50, offset=offset)
        seen.update(_seq_set(page))
    # All 305 unique messages covered
    assert len(seen) == 305
    # No duplicates across pages
    total = sum(len(get_messages(synthetic_db, 'X', limit=50, offset=o)) for o in pages)
    assert total == 305


def test_get_messages_since_filter(synthetic_db):
    """since=1600000000 excludes all small (max 1500011940), keeps kf+main."""
    r = get_messages(synthetic_db, 'X', limit=50, since=1600000000)
    expected = set(range(5000, 5005)) | set(range(1055, 1100))
    assert _seq_set(r) == expected


def test_get_messages_until_filter(synthetic_db):
    """until=1600000000 excludes all main+kf, keeps small only."""
    r = get_messages(synthetic_db, 'X', limit=200, since=0, until=1600000000)
    assert _seq_set(r) == set(range(1, 201))
    assert len(r) == 200


def test_get_messages_time_range_no_match(synthetic_db):
    """Time range with no matches returns empty list."""
    r = get_messages(synthetic_db, 'X', since=0, until=1000000000)
    assert r == []


def test_get_messages_msg_type_filter(synthetic_db):
    """msg_type=0 returns all 300 (main + small; kf also has type 0)."""
    r = get_messages(synthetic_db, 'X', limit=305, msg_type=0)
    assert len(r) == 305  # all data is type 0


def test_get_messages_msg_type_no_match(synthetic_db):
    """msg_type=99 (nonexistent) returns empty."""
    r = get_messages(synthetic_db, 'X', limit=50, msg_type=99)
    assert r == []


def test_get_messages_empty_conversation(synthetic_db):
    r = get_messages(synthetic_db, 'NOEXIST', limit=50)
    assert r == []


def test_get_messages_single_table_conv(synthetic_db):
    """Conv Y has 5 messages, all in main table."""
    r = get_messages(synthetic_db, 'Y', limit=50)
    assert _seq_set(r) == set(range(100, 105))
    assert len(r) == 5


def test_get_messages_msg_dict_shape(synthetic_db):
    """Each message dict has the expected keys."""
    r = get_messages(synthetic_db, 'X', limit=5)
    expected_keys = {
        'message_id', 'server_id', 'sequence', 'sender_id',
        'conversation_id', 'content_type', 'type_name', 'send_time',
        'flag', 'content', 'from_app_id',
    }
    for m in r:
        assert expected_keys.issubset(set(m.keys())), f"missing keys: {expected_keys - set(m.keys())}"


# ---------------------------------------------------------------------------
# search_messages tests
# ---------------------------------------------------------------------------

def test_search_messages_main_table(synthetic_db):
    """'main_5' matches main_5 (seq 1005), main_50..main_59 (seq 1050..1059) = 11.
    Top 10 = 1059..1050."""
    r = search_messages(synthetic_db, 'main_5', conversation_id='X', limit=10)
    assert _seq_set(r) == set(range(1050, 1060))
    assert len(r) == 10


def test_search_messages_small_table(synthetic_db):
    """'small_1' matches small_1, small_10..19, small_100..199 = 111.
    Top 10 = 200..191."""
    r = search_messages(synthetic_db, 'small_1', conversation_id='X', limit=10)
    assert _seq_set(r) == set(range(191, 201))
    assert len(r) == 10


def test_search_messages_no_match(synthetic_db):
    r = search_messages(synthetic_db, 'nonexistent_keyword_xyz', conversation_id='X', limit=10)
    assert r == []


def test_search_messages_empty_conversation(synthetic_db):
    r = search_messages(synthetic_db, 'main', conversation_id='NOEXIST', limit=10)
    assert r == []


def test_search_messages_ascending_order(synthetic_db):
    """search_messages also returns ASC (per docstring)."""
    r = search_messages(synthetic_db, 'main', conversation_id='X', limit=20)
    seqs = [m['sequence'] for m in r]
    assert seqs == sorted(seqs)


def test_search_messages_no_conversation_filter(synthetic_db):
    """Without conv filter, searches all conversations."""
    r = search_messages(synthetic_db, 'main_5', limit=20)
    # 11 from conv X (seq 1005, 1050..1059) + 1 from conv Y (seq 105? no, Y has y_0..y_4)
    # Y content is 'y_0'..'y_4', none contain 'main_5'
    # So just the 11 from X
    assert _seq_set(r).issubset(set(range(1000, 1100)))


# ---------------------------------------------------------------------------
# get_message_count tests
# ---------------------------------------------------------------------------

def test_get_message_count_sums_across_tables(synthetic_db):
    assert get_message_count(synthetic_db, 'X') == 305


def test_get_message_count_single_table(synthetic_db):
    assert get_message_count(synthetic_db, 'Y') == 5


def test_get_message_count_empty(synthetic_db):
    assert get_message_count(synthetic_db, 'NOEXIST') == 0


# ---------------------------------------------------------------------------
# Regression: missing table (kf absent) must not break UNION ALL
# ---------------------------------------------------------------------------

def test_works_with_only_2_tables(tmp_path):
    """kf_message_tableV1 missing — UNION ALL must skip it gracefully."""
    db = tmp_path / "message.db"
    conn = sqlite3.connect(str(db))
    for t in ('message_table', 'message_small_table'):
        conn.execute(f'''CREATE TABLE "{t}" (
            message_id INTEGER PRIMARY KEY, server_id TEXT, sequence INTEGER,
            sender_id INTEGER, conversation_id TEXT, content_type INTEGER,
            send_time INTEGER, flag INTEGER, content BLOB, from_app_id TEXT
        )''')
    for i in range(10):
        conn.execute('INSERT INTO message_table VALUES (?,?,?,?,?,?,?,?,?,?)',
            (i+1, f'm{i}', 100+i, 1, 'Z', 0, 1700000000+i*60, 0, f'main_{i}'.encode(), None))
    for i in range(20):
        conn.execute('INSERT INTO message_small_table VALUES (?,?,?,?,?,?,?,?,?,?)',
            (100+i, f's{i}', 1+i, 2, 'Z', 0, 1500000000+i*60, 0, f'small_{i}'.encode(), None))
    conn.commit(); conn.close()

    r = get_messages(str(db), 'Z', limit=50)
    assert _seq_set(r) == set(range(100, 110)) | set(range(1, 21))
    assert len(r) == 30


def test_works_with_only_1_table(tmp_path):
    """Only message_table present — must still work."""
    db = tmp_path / "message.db"
    conn = sqlite3.connect(str(db))
    conn.execute('''CREATE TABLE "message_table" (
        message_id INTEGER PRIMARY KEY, server_id TEXT, sequence INTEGER,
        sender_id INTEGER, conversation_id TEXT, content_type INTEGER,
        send_time INTEGER, flag INTEGER, content BLOB, from_app_id TEXT
    )''')
    for i in range(5):
        conn.execute('INSERT INTO message_table VALUES (?,?,?,?,?,?,?,?,?,?)',
            (i+1, f'm{i}', 100+i, 1, 'Z', 0, 1700000000+i*60, 0, f'main_{i}'.encode(), None))
    conn.commit(); conn.close()

    r = get_messages(str(db), 'Z', limit=50)
    assert _seq_set(r) == set(range(100, 105))
    assert get_message_count(str(db), 'Z') == 5


def test_works_with_no_message_tables(tmp_path):
    """DB exists but has no message tables — must return [] gracefully."""
    db = tmp_path / "message.db"
    conn = sqlite3.connect(str(db))
    conn.execute('CREATE TABLE unrelated (x INTEGER)')
    conn.execute('INSERT INTO unrelated VALUES (1)')
    conn.commit(); conn.close()

    assert get_messages(str(db), 'X', limit=10) == []
    assert search_messages(str(db), 'keyword', limit=10) == []
    assert get_message_count(str(db), 'X') == 0


def test_parse_content_gbk():
    """GBK-encoded bytes should be decoded."""
    raw = "中文消息".encode('gbk')
    result = _parse_content(raw)
    assert "中文" in result


def test_parse_content_latin1():
    """Latin1 fallback for non-UTF-8/GBK content."""
    # Pure ASCII is valid in latin1
    raw = b"hello world test"
    result = _parse_content(raw)
    assert "hello" in result or "binary" in result


def test_parse_content_empty_bytes():
    """Empty bytes should not crash."""
    result = _parse_content(b"")
    assert result == "[binary 0 bytes]"


def test_parse_content_protobuf_nested():
    """Protobuf with nested length-delimited fields extracts inner text."""
    # Simulate a protobuf structure: tag=0x0A (field 1, wire 2), length, content
    # 0x0A is "field 1, length-delimited"
    inner = b"hello"
    data = bytes([0x0A, len(inner)]) + inner
    result = _parse_content(data)
    assert "hello" in result


def test_parse_content_protobuf_varint():
    """Protobuf varint field (wire type 0) is handled."""
    # tag = 0x08 (field 1, wire 0 = varint)
    data = bytes([0x08, 0x05])
    result = _parse_content(data)
    # Should not crash; may return empty or binary
    assert isinstance(result, str)


def test_parse_content_protobuf_32bit():
    """Protobuf 32-bit field (wire type 5) is handled."""
    # tag = 0x2D (field 5, wire 5 = 32-bit)
    data = bytes([0x2D]) + b"\x00" * 4
    result = _parse_content(data)
    assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Real data integration test (the critical bug fix verification)
# ---------------------------------------------------------------------------

REAL_DB = os.path.join(
    os.path.dirname(__file__), '..', '..', 'wxwork_decrypted', 'message.db'
)
REAL_DB = os.path.abspath(REAL_DB)


@pytest.mark.skipif(not os.path.isfile(REAL_DB), reason="real data not available")
def test_real_data_offset_bug_repro():
    """The bug fix: offset must apply to the global sorted union, not per-table.

    Conv R:2910032769 has 61,343 main + 408 small. At offset=60000, the OLD
    implementation returned messages from 9068114..9069792 (50 too old because
    it applied OFFSET to each table independently). NEW returns the correct
    global position 9074425..9075563.
    """
    # Build the ground truth by querying all tables and sorting in Python
    conn = sqlite3.connect(REAL_DB)
    all_msgs = []
    for table in MESSAGE_TABLES:
        r = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if not r:
            continue
        for row in conn.execute(f'SELECT sequence FROM "{table}" WHERE conversation_id = ?',
                                ('R:2910032769',)):
            all_msgs.append(row[0])
    conn.close()
    all_msgs.sort(reverse=True)
    assert len(all_msgs) > 60100, f"need >60100 messages for this test, have {len(all_msgs)}"

    expected = set(all_msgs[60000:60050])
    actual = _seq_set(get_messages(REAL_DB, 'R:2910032769', limit=50, offset=60000))
    assert actual == expected, (
        f"NEW impl must return global position. "
        f"missing={sorted(expected - actual)[:3]}, extra={sorted(actual - expected)[:3]}"
    )


@pytest.mark.skipif(not os.path.isfile(REAL_DB), reason="real data not available")
def test_real_data_get_message_count_consistency():
    """get_message_count must equal the sum across all tables."""
    for conv in ['R:2910032769', 'R:2910049313', 'R:96140446197592']:
        c = get_message_count(REAL_DB, conv)
        assert c > 0, f"{conv} should have messages"
        # Verify by direct count
        conn = sqlite3.connect(REAL_DB)
        total = 0
        for t in MESSAGE_TABLES:
            r = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()
            if not r:
                continue
            total += conn.execute(f'SELECT COUNT(*) FROM "{t}" WHERE conversation_id = ?', (conv,)).fetchone()[0]
        conn.close()
        assert c == total


@pytest.mark.skipif(not os.path.isfile(REAL_DB), reason="real data not available")
def test_real_data_get_messages_top_50():
    """Sanity check: top 50 from a heavy conversation."""
    r = get_messages(REAL_DB, 'R:2910032769', limit=50)
    assert len(r) == 50
    seqs = [m['sequence'] for m in r]
    # Must be sorted ASC
    assert seqs == sorted(seqs)
    # Top 50 must be in the top 100 of the global set
    conn = sqlite3.connect(REAL_DB)
    all_top = []
    for table in MESSAGE_TABLES:
        rr = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if not rr: continue
        for row in conn.execute(f'SELECT sequence FROM "{table}" WHERE conversation_id = ?',
                                ('R:2910032769',)):
            all_top.append(row[0])
    conn.close()
    all_top.sort(reverse=True)
    assert max(seqs) >= all_top[49]  # max of our 50 must be in top-50 globally


@pytest.mark.skipif(not os.path.isfile(REAL_DB), reason="real data not available")
def test_real_data_search_messages_basic():
    """Search for a common keyword, verify results match."""
    r = search_messages(REAL_DB, '微信', limit=20)
    if not r:
        pytest.skip("no results for '微信'")
    for m in r:
        assert '微信' in m.get('content', ''), f"result {m['sequence']} doesn't contain keyword"
    seqs = [m['sequence'] for m in r]
    assert seqs == sorted(seqs)  # must be ASC
