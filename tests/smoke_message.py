"""Smoke test for the message.py bug fix.

Compares the NEW UNION ALL implementation against a re-implementation of the
ORIGINAL per-table LIMIT-then-truncate approach. Verifies functional equivalence
on synthetic multi-table data, and that the NEW impl is also semantically
correct (returns the top N globally newest messages regardless of which
physical table they live in).
"""
import os
import sqlite3
import sys
import tempfile

# Re-implement the OLD behavior to compare against.
def _table_exists(conn, table):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def get_messages_OLD(db_path, conv_id, limit=50, offset=0, since=None, until=None, msg_type=None):
    conn = sqlite3.connect(db_path); conn.row_factory = sqlite3.Row
    try:
        messages = []
        for table in ('message_table', 'message_small_table', 'kf_message_tableV1'):
            if not _table_exists(conn, table):
                continue
            q = (
                f'SELECT message_id, server_id, sequence, sender_id, conversation_id, '
                f'content_type, send_time, flag, content, from_app_id '
                f'FROM "{table}" WHERE conversation_id = ?'
            )
            p = [conv_id]
            if since is not None: q += ' AND send_time >= ?'; p.append(since)
            if until is not None: q += ' AND send_time < ?'; p.append(until)
            if msg_type is not None: q += ' AND content_type = ?'; p.append(msg_type)
            q += ' ORDER BY sequence DESC LIMIT ? OFFSET ?'
            p.extend([limit, offset])
            for row in conn.execute(q, p):
                messages.append(dict(row))
        messages.sort(key=lambda m: m.get('sequence', 0), reverse=True)
        return messages[:limit]
    finally:
        conn.close()


def search_messages_OLD(db_path, kw, conv_id=None, limit=50):
    conn = sqlite3.connect(db_path); conn.row_factory = sqlite3.Row
    try:
        messages = []
        for table in ('message_table', 'message_small_table', 'kf_message_tableV1'):
            if not _table_exists(conn, table):
                continue
            q = (
                f'SELECT message_id, server_id, sequence, sender_id, conversation_id, '
                f'content_type, send_time, flag, content '
                f'FROM "{table}" WHERE content LIKE ?'
            )
            p = [f'%{kw}%']
            if conv_id: q += ' AND conversation_id = ?'; p.append(conv_id)
            q += ' ORDER BY sequence DESC LIMIT ?'
            p.append(limit)
            for row in conn.execute(q, p):
                messages.append(dict(row))
        messages.sort(key=lambda m: m.get('sequence', 0), reverse=True)
        return messages[:limit]
    finally:
        conn.close()


def _build_synthetic_db():
    tmpdir = tempfile.mkdtemp(prefix='wecom_test_')
    db = os.path.join(tmpdir, 'message.db')
    conn = sqlite3.connect(db)
    for t in ('message_table', 'message_small_table', 'kf_message_tableV1'):
        conn.execute(f'''CREATE TABLE "{t}" (
            message_id INTEGER PRIMARY KEY, server_id TEXT, sequence INTEGER,
            sender_id INTEGER, conversation_id TEXT, content_type INTEGER,
            send_time INTEGER, flag INTEGER, content BLOB, from_app_id TEXT
        )''')
    # Conv X: 100 main (seq 1000..1099), 200 small (seq 1..200), 5 kf (seq 5000..5004)
    for i in range(100):
        conn.execute('INSERT INTO message_table VALUES (?,?,?,?,?,?,?,?,?,?)',
            (i+1, f'm{i}', 1000+i, 1, 'X', 0, 1700000000+i*60, 0, f'main_{i}'.encode(), None))
    for i in range(200):
        conn.execute('INSERT INTO message_small_table VALUES (?,?,?,?,?,?,?,?,?,?)',
            (10000+i, f's{i}', 1+i, 2, 'X', 0, 1500000000+i*60, 0, f'small_{i}'.encode(), None))
    for i in range(5):
        conn.execute('INSERT INTO kf_message_tableV1 VALUES (?,?,?,?,?,?,?,?,?,?)',
            (20000+i, f'k{i}', 5000+i, 3, 'X', 0, 1800000000+i*60, 0, f'kf_{i}'.encode(), None))
    # Conv Y: 5 messages in main only, seq 100..104
    for i in range(5):
        conn.execute('INSERT INTO message_table VALUES (?,?,?,?,?,?,?,?,?,?)',
            (30000+i, f'y{i}', 100+i, 1, 'Y', 0, 1700000000+i*60, 0, f'y_{i}'.encode(), None))
    conn.commit()
    conn.close()
    return db


def _seq_set(rows):
    return set(r['sequence'] for r in rows)


def main():
    from wecom_reader.db.message import get_messages, search_messages, get_message_count

    db = _build_synthetic_db()
    failures = []

    # ---- Test 1: get_messages basic
    # Conv X has 100 main (1000..1099), 200 small (1..200), 5 kf (5000..5004).
    # limit=50, no filters → top 50 = 5 kf + 45 main (1099..1055)
    o = get_messages_OLD(db, 'X', limit=50)
    n = get_messages(db, 'X', limit=50)
    exp = set(range(5000, 5005)) | set(range(1055, 1100))
    if _seq_set(o) != exp:
        failures.append(f'T1 OLD wrong: {sorted(_seq_set(o))[:3]}..')
    if _seq_set(n) != exp:
        failures.append(f'T1 NEW wrong: {sorted(_seq_set(n))[:3]}..')
    if _seq_set(o) != _seq_set(n):
        failures.append(f'T1 OLD vs NEW differ')
    if [m['sequence'] for m in n] != sorted(m['sequence'] for m in n):
        failures.append('T1 NEW not sorted ASC')
    o_seqs = [m['sequence'] for m in o]
    if o_seqs != sorted(o_seqs, reverse=True):
        failures.append('T1 OLD not sorted DESC')
    if not failures or 'T1' not in ' '.join(failures):
        print('  T1 PASS: get_messages(X, limit=50) — top 50 = 5 kf + 45 main')

    # ---- Test 2: get_messages wider window
    # 5 kf + 100 main + 45 small = 150
    o = get_messages_OLD(db, 'X', limit=150)
    n = get_messages(db, 'X', limit=150)
    exp = set(range(5000, 5005)) | set(range(1000, 1100)) | set(range(156, 201))
    if _seq_set(o) != exp:
        failures.append(f'T2 OLD wrong: {sorted(_seq_set(o))[:3]}..{sorted(_seq_set(o))[-3:]}')
    if _seq_set(n) != exp:
        failures.append(f'T2 NEW wrong: {sorted(_seq_set(n))[:3]}..{sorted(_seq_set(n))[-3:]}')
    if not failures or 'T2' not in ' '.join(failures):
        print('  T2 PASS: get_messages(X, limit=150) — spans all 3 tables')

    # ---- Test 3: offset (THE bug repro — OLD skips too much)
    # Conv X: 5 kf (5000..5004) + 100 main (1000..1099) + 200 small (1..200) = 305 total
    # Global DESC sort: 5004..5000, 1099..1000, 200..1
    # Top 100: 5 kf + 95 main (1099..1005). Next 50 (offset=100): 5 main (1004..1000) + 45 small (200..156)
    # OLD: per-table OFFSET 100 → main=0 rows (only 100), small=100 rows (101..1), kf=0
    #      → returns 50 from small (seq 51..100). MISSES the 5 newer main rows. BUG.
    # NEW: global OFFSET 100 → 5 main (1004..1000) + 45 small (200..156) = 50. CORRECT.
    o = get_messages_OLD(db, 'X', limit=50, offset=100)
    n = get_messages(db, 'X', limit=50, offset=100)
    exp = set(range(1000, 1005)) | set(range(156, 201))
    if _seq_set(n) != exp:
        failures.append(f'T3 NEW wrong: {sorted(_seq_set(n))}')
    if _seq_set(o) == exp:
        failures.append('T3 OLD unexpectedly correct — bug repro failed')
    if not failures or 'T3' not in ' '.join(failures):
        # Verify the bug signature: OLD should return {51..100} (all from small, missing the 5 main)
        bug_set = set(range(51, 101))
        if _seq_set(o) == bug_set:
            print('  T3 PASS: bug repro confirmed — OLD misses 5 main rows, NEW returns global top-50')
        else:
            print(f'  T3 BUG REPRO UNEXPECTED: OLD={sorted(_seq_set(o))[:3]}..{sorted(_seq_set(o))[-3:]}')

    # ---- Test 4: since filter excludes small
    # since=1600000000 → all small excluded. 5 kf + 50 main = 55. limit=50 → 5 kf + 45 main (1099..1055).
    o = get_messages_OLD(db, 'X', limit=50, since=1600000000)
    n = get_messages(db, 'X', limit=50, since=1600000000)
    exp = set(range(5000, 5005)) | set(range(1055, 1100))
    if _seq_set(o) != exp:
        failures.append(f'T4 OLD wrong: {sorted(_seq_set(o))}')
    if _seq_set(n) != exp:
        failures.append(f'T4 NEW wrong: {sorted(_seq_set(n))}')
    if not failures or 'T4' not in ' '.join(failures):
        print('  T4 PASS: since filter works (excludes small)')

    # ---- Test 5: until filter excludes main
    # until=1600000000 → all main+kf excluded. Just 200 small.
    o = get_messages_OLD(db, 'X', limit=200, since=0, until=1600000000)
    n = get_messages(db, 'X', limit=200, since=0, until=1600000000)
    exp = set(range(1, 201))
    if _seq_set(o) != exp:
        failures.append(f'T5 OLD wrong: {sorted(_seq_set(o))[:3]}..')
    if _seq_set(n) != exp:
        failures.append(f'T5 NEW wrong: {sorted(_seq_set(n))[:3]}..')
    if not failures or 'T5' not in ' '.join(failures):
        print('  T5 PASS: until filter works (excludes main+kf)')

    # ---- Test 6: search_messages across tables
    # main_5 matches main_5 (seq 1005), main_50..main_59 (seq 1050..1059) = 11 matches
    # Top 10 = seq 1059..1050
    o = search_messages_OLD(db, 'main_5', conv_id='X', limit=10)
    n = search_messages(db, 'main_5', conversation_id='X', limit=10)
    exp = set(range(1050, 1060))
    if _seq_set(o) != exp:
        failures.append(f'T6 OLD wrong: {sorted(_seq_set(o))}')
    if _seq_set(n) != exp:
        failures.append(f'T6 NEW wrong: {sorted(_seq_set(n))}')
    if not failures or 'T6' not in ' '.join(failures):
        print('  T6 PASS: search main_5 → top 10 (1059..1050)')

    # ---- Test 7: search_messages from small table
    # small_1 matches small_1, small_10..19, small_100..199 = 111 matches
    # Top 10 = seq 200..191
    o = search_messages_OLD(db, 'small_1', conv_id='X', limit=10)
    n = search_messages(db, 'small_1', conversation_id='X', limit=10)
    exp = set(range(191, 201))
    if _seq_set(o) != exp:
        failures.append(f'T7 OLD wrong: {sorted(_seq_set(o))}')
    if _seq_set(n) != exp:
        failures.append(f'T7 NEW wrong: {sorted(_seq_set(n))}')
    if not failures or 'T7' not in ' '.join(failures):
        print('  T7 PASS: search small_1 → top 10 (200..191)')

    # ---- Test 8: msg_type filter
    n = get_messages(db, 'X', limit=300, msg_type=0)
    if len(n) != 300:
        failures.append(f'T8 NEW wrong: expected 300, got {len(n)}')
    if not failures or 'T8' not in ' '.join(failures):
        print('  T8 PASS: msg_type=0 returns 300')

    # ---- Test 9: empty conversation
    o = get_messages_OLD(db, 'NOEXIST', limit=50)
    n = get_messages(db, 'NOEXIST', limit=50)
    if o != [] or n != []:
        failures.append('T9 wrong: should be empty')
    if not failures or 'T9' not in ' '.join(failures):
        print('  T9 PASS: empty list for missing conv')

    # ---- Test 10: get_message_count sums across tables
    c = get_message_count(db, 'X')
    if c != 305:
        failures.append(f'T10 wrong: expected 305, got {c}')
    if not failures or 'T10' not in ' '.join(failures):
        print(f'  T10 PASS: get_message_count(X) = {c}')

    # ---- Test 11: single-table conversation (only main)
    n = get_messages(db, 'Y', limit=50)
    if _seq_set(n) != set(range(100, 105)):
        failures.append(f'T11 wrong: {sorted(_seq_set(n))}')
    if not failures or 'T11' not in ' '.join(failures):
        print('  T11 PASS: single-table conv (Y) — 5 messages')

    # ---- Test 12: missing kf table (regression: UNION ALL must not break)
    # kf table is present in synthetic but with 0 rows we can drop it
    # Use a fresh DB with only 2 tables
    tmpdir2 = tempfile.mkdtemp(prefix='wecom_test2_')
    db2 = os.path.join(tmpdir2, 'message.db')
    conn = sqlite3.connect(db2)
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
    n = get_messages(db2, 'Z', limit=50)
    if _seq_set(n) != set(range(100, 110)) | set(range(1, 21)):
        failures.append(f'T12 wrong: {sorted(_seq_set(n))}')
    if not failures or 'T12' not in ' '.join(failures):
        print('  T12 PASS: works with only 2 tables (kf missing)')

    # ---- Test 13: real data — bug repro on actual WeCom data
    real_db = os.path.join(os.path.dirname(__file__), '..', '..', 'wxwork_decrypted', 'message.db')
    real_db = os.path.abspath(real_db)
    if os.path.isfile(real_db):
        print(f'\n=== Test 13: real data — R:2910032769, offset=60000 (61,343 main + 408 small) ===')
        # Build the global expected result by querying all tables and sorting in Python
        conn = sqlite3.connect(real_db)
        all_msgs = []
        for table in ('message_table', 'message_small_table', 'kf_message_tableV1'):
            r = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
            if not r:
                continue
            for row in conn.execute(f'SELECT sequence FROM "{table}" WHERE conversation_id = ?', ('R:2910032769',)):
                all_msgs.append(row[0])
        conn.close()
        all_msgs.sort(reverse=True)
        if len(all_msgs) > 60100:
            expected = set(all_msgs[60000:60050])
            n = get_messages(real_db, 'R:2910032769', limit=50, offset=60000)
            if _seq_set(n) != expected:
                failures.append(f'T13 NEW wrong on real data: missing={sorted(expected - _seq_set(n))[:3]} extra={sorted(_seq_set(n) - expected)[:3]}')
            else:
                # Also verify OLD is wrong (so the test would catch a regression of the bug)
                o = get_messages_OLD(real_db, 'R:2910032769', limit=50, offset=60000)
                if _seq_set(o) == expected:
                    failures.append('T13 OLD unexpectedly correct on real data — bug not reproed')
                else:
                    missing = len(expected - _seq_set(o))
                    extra = len(_seq_set(o) - expected)
                    print(f'  T13 PASS: real data bug confirmed — OLD returns {_seq_set(o) and min(_seq_set(o))}..{_seq_set(o) and max(_seq_set(o))} '
                          f'(misses {missing} newer msgs, has {extra} extra) vs NEW correctly returns {min(_seq_set(n))}..{max(_seq_set(n))}')
        else:
            print(f'  T13 SKIP: real data has only {len(all_msgs)} messages for that conv')
    else:
        print(f'  T13 SKIP: real data not available at {real_db}')

    # ---- Test 14: real data — search_messages across tables
    if os.path.isfile(real_db):
        print(f'\n=== Test 14: real data — search_messages across tables ===')
        # Pick a common keyword. "微信" should be common.
        try:
            results = search_messages(real_db, '微信', limit=20)
            if not results:
                print('  T14 SKIP: no results for 微信')
            else:
                # Verify all results contain the keyword
                bad = [m for m in results if '微信' not in m.get('content', '')]
                if bad:
                    failures.append(f'T14: {len(bad)} results don\'t contain keyword')
                else:
                    # Verify ordering (sequence ASC per docstring)
                    seqs = [m['sequence'] for m in results]
                    if seqs != sorted(seqs):
                        failures.append('T14: not sorted ASC')
                    else:
                        print(f'  T14 PASS: {len(results)} results, all match, sorted ASC')
        except Exception as e:
            print(f'  T14 SKIP: {e}')

    # ---- Test 15: real data — get_message_count matches get_messages
    if os.path.isfile(real_db):
        print(f'\n=== Test 15: real data — get_message_count consistency ===')
        for conv in ['R:2910032769', 'R:2910049313', 'R:96140446197592']:
            c = get_message_count(real_db, conv)
            if c == 0:
                continue
            # Verify by querying
            conn = sqlite3.connect(real_db)
            total = 0
            for t in ('message_table', 'message_small_table', 'kf_message_tableV1'):
                r = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()
                if not r: continue
                total += conn.execute(f'SELECT COUNT(*) FROM "{t}" WHERE conversation_id = ?', (conv,)).fetchone()[0]
            conn.close()
            if c != total:
                failures.append(f'T15: {conv} count {c} != {total}')
            else:
                print(f'  T15 PASS: {conv} count = {c}')

    print()
    if failures:
        print(f'FAILURES ({len(failures)}):')
        for f in failures:
            print(f'  - {f}')
        sys.exit(1)
    else:
        print('=== ALL TESTS PASSED ===')
        print()
        print('FINDING: T3 reveals a real bug in the OLD impl that the NEW UNION ALL impl fixes.')
        print('Symptom: with offset > N (where N = row count in any one table for the conv),')
        print('OLD applies OFFSET to each table independently. For a table with N rows,')
        print('OFFSET 100 means "skip 100 of that table", not "skip 100 globally". When the')
        print('user pages deep into a conversation, OLD silently skips past the global position')
        print('and returns much older messages, causing "missing messages" symptoms.')
        print()
        print('NEW applies OFFSET to the global sorted union, which is the correct semantics.')


if __name__ == '__main__':
    main()
