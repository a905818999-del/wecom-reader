"""Pytest tests for wecom_reader.reader (WeComReader class).

Covers the high-level reader interface and the WAL detection added 2026-06-26.
Uses a fake decrypted dir to test without real WeCom installation.
"""
import os
import shutil
import sqlite3
import sys
import tempfile

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_decrypted_dir():
    """Build a fake decrypted/ dir with message.db, session.db, user.db."""
    tmpdir = tempfile.mkdtemp(prefix='wecom_reader_test_')
    # Build message.db
    mdb = os.path.join(tmpdir, 'message.db')
    conn = sqlite3.connect(mdb)
    for t in ('message_table', 'message_small_table', 'kf_message_tableV1'):
        conn.execute(f'''CREATE TABLE "{t}" (
            message_id INTEGER PRIMARY KEY, server_id TEXT, sequence INTEGER,
            sender_id INTEGER, conversation_id TEXT, content_type INTEGER,
            send_time INTEGER, flag INTEGER, content BLOB, from_app_id TEXT
        )''')
    for i in range(20):
        conn.execute('INSERT INTO message_table VALUES (?,?,?,?,?,?,?,?,?,?)',
            (i+1, f'm{i}', 100+i, 1, 'X', 0, 1700000000+i*60, 0, f'main_{i}'.encode(), None))
    conn.commit(); conn.close()

    # Build session.db
    sdb = os.path.join(tmpdir, 'session.db')
    conn = sqlite3.connect(sdb)
    conn.execute('''CREATE TABLE "conversation_table" (
        id TEXT PRIMARY KEY, name TEXT, roomname_remark TEXT,
        last_message_time INTEGER, last_message_id INTEGER
    )''')
    conn.execute('INSERT INTO conversation_table VALUES (?,?,?,?,?)',
        ('R:1', 'Test Conv', 'Test Group', 1700000000, 1))
    conn.commit(); conn.close()

    # Build user.db
    udb = os.path.join(tmpdir, 'user.db')
    conn = sqlite3.connect(udb)
    conn.execute('''CREATE TABLE "user_table" (
        id INTEGER PRIMARY KEY, name TEXT, real_name TEXT,
        account TEXT, external_corp_name TEXT, external_job TEXT
    )''')
    conn.execute('INSERT INTO user_table VALUES (?,?,?,?,?,?)',
        (1, 'alice', 'Alice', 'alice@corp', None, None))
    conn.commit(); conn.close()

    yield tmpdir
    shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# status() tests
# ---------------------------------------------------------------------------

def test_status_no_decrypted_dir():
    from wecom_reader.reader import WeComReader
    r = WeComReader(decrypted_dir=os.path.join(tempfile.gettempdir(), 'wecom_no_exist_xyz'))
    s = r.status()
    assert s['decrypted'] is False
    assert s['databases'] == {}


def test_status_with_decrypted_dir(fake_decrypted_dir):
    from wecom_reader.reader import WeComReader
    r = WeComReader(decrypted_dir=fake_decrypted_dir)
    s = r.status()
    assert s['decrypted'] is True
    assert 'message.db' in s['databases']
    assert 'message_table' in s['databases']['message.db']['tables']
    # size_mb is rounded to 1 decimal — small fixture may round to 0.0
    # Just check that the field exists and is non-negative
    assert s['databases']['message.db']['size_mb'] >= 0


# ---------------------------------------------------------------------------
# get_messages() tests (high-level)
# ---------------------------------------------------------------------------

def test_reader_get_messages(fake_decrypted_dir):
    from wecom_reader.reader import WeComReader
    r = WeComReader(decrypted_dir=fake_decrypted_dir)
    msgs = r.get_messages('X', limit=10)
    assert len(msgs) == 10
    # Newest first window covers seq 90..99 (offset 10 from top 20)
    # Wait: top 20 are seq 100..119. With limit=10, returns top 10: 110..119
    seqs = [m['sequence'] for m in msgs]
    assert seqs == sorted(seqs)  # ASC per docstring
    assert max(seqs) == 119


def test_reader_get_messages_with_sender(fake_decrypted_dir):
    """Verify sender_name enrichment from user.db."""
    from wecom_reader.reader import WeComReader
    r = WeComReader(decrypted_dir=fake_decrypted_dir)
    msgs = r.get_messages('X', limit=5)
    for m in msgs:
        assert m.get('sender_name') == 'Alice', f"missing sender_name: {m}"


def test_reader_get_messages_missing_db(tmp_path):
    from wecom_reader.reader import WeComReader
    r = WeComReader(decrypted_dir=str(tmp_path))
    msgs = r.get_messages('X', limit=10)
    assert msgs == []


# ---------------------------------------------------------------------------
# get_message_count / session_count tests
# ---------------------------------------------------------------------------

def test_reader_message_count(fake_decrypted_dir):
    from wecom_reader.reader import WeComReader
    r = WeComReader(decrypted_dir=fake_decrypted_dir)
    assert r.message_count('X') == 20


def test_reader_message_count_missing_db(tmp_path):
    from wecom_reader.reader import WeComReader
    r = WeComReader(decrypted_dir=str(tmp_path))
    assert r.message_count('X') == 0


# ---------------------------------------------------------------------------
# search_messages() tests
# ---------------------------------------------------------------------------

def test_reader_search_messages(fake_decrypted_dir):
    from wecom_reader.reader import WeComReader
    r = WeComReader(decrypted_dir=fake_decrypted_dir)
    results = r.search_messages('main_1', limit=10)
    # main_1, main_10..main_19 = 11 matches, top 10 = main_19..main_10
    assert len(results) == 10
    seqs = [m['sequence'] for m in results]
    assert seqs == sorted(seqs)
    assert max(seqs) == 119


# ---------------------------------------------------------------------------
# session listing tests
# ---------------------------------------------------------------------------

def test_reader_list_sessions(fake_decrypted_dir):
    from wecom_reader.reader import WeComReader
    r = WeComReader(decrypted_dir=fake_decrypted_dir)
    sessions = r.list_sessions(limit=10)
    assert len(sessions) >= 1
    assert sessions[0]['id'] == 'R:1'


def test_reader_session_count(fake_decrypted_dir):
    from wecom_reader.reader import WeComReader
    r = WeComReader(decrypted_dir=fake_decrypted_dir)
    c = r.session_count()
    assert c >= 1


# ---------------------------------------------------------------------------
# WAL detection tests (init() return shape)
# ---------------------------------------------------------------------------

def test_init_with_wal_detection_shape(tmp_path):
    """init() must return dict with wal_present (list) and wal_warning (str|None)."""
    from wecom_reader.reader import WeComReader
    fake_src = tmp_path / "fake_src"
    fake_src.mkdir()
    # Create a fake db file (plain SQLite)
    db = fake_src / "test.db"
    conn = sqlite3.connect(str(db))
    conn.execute('CREATE TABLE t (x INTEGER)')
    conn.execute('INSERT INTO t VALUES (1)')
    conn.commit(); conn.close()
    # Create a sibling WAL file with non-zero size
    wal = fake_src / "test.db-wal"
    wal.write_bytes(b"\x37\x7f\x06\x82" + b"\x00" * 100)

    out_dir = tmp_path / "out"

    r = WeComReader(db_dir=str(fake_src), decrypted_dir=str(out_dir))
    # Pretend we have a key map so init() skips the live memory scan
    r._key_map = {
        "_db_dir": str(fake_src),
        # No key for test.db — we expect is_plain_sqlite path to take it
    }
    result = r.init()
    assert 'wal_present' in result
    assert 'wal_warning' in result
    assert isinstance(result['wal_present'], list)
    # test.db has a sibling test.db-wal with size > 0, so it should be flagged
    assert 'test.db' in result['wal_present']
    assert result['wal_warning'] is not None
    assert 'WAL' in result['wal_warning']


def test_init_without_wal(tmp_path):
    """No WAL files: wal_present empty, wal_warning None."""
    from wecom_reader.reader import WeComReader
    fake_src = tmp_path / "fake_src"
    fake_src.mkdir()
    db = fake_src / "plain.db"
    conn = sqlite3.connect(str(db))
    conn.execute('CREATE TABLE t (x INTEGER)')
    conn.commit(); conn.close()

    out_dir = tmp_path / "out"

    r = WeComReader(db_dir=str(fake_src), decrypted_dir=str(out_dir))
    r._key_map = {"_db_dir": str(fake_src)}
    result = r.init()
    assert result['wal_present'] == []
    assert result['wal_warning'] is None
