import hashlib
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import wecom_reader.attachment_store as attachment_store_module
from wecom_reader.attachment_store import AttachmentStore


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def table_count(db_path, table):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def test_successful_copy_records_hash_and_reference(tmp_path):
    source = tmp_path / "source.bin"
    payload = b"attachment payload"
    source.write_bytes(payload)
    store = AttachmentStore(tmp_path / "ledger.db", tmp_path / "assets")

    reference = store.ingest_file("acct", "msg-1", source, "image", "v1")

    expected_hash = sha256(payload)
    stored = tmp_path / "assets" / "sha256" / expected_hash[:2] / expected_hash
    assert reference.status == "stored"
    assert reference.content_sha256 == expected_hash
    assert reference.source_size == len(payload)
    assert reference.storage_path == f"sha256/{expected_hash[:2]}/{expected_hash}"
    assert str(tmp_path) not in reference.storage_path
    assert stored.read_bytes() == payload
    assert store.status("acct", "msg-1", source, "image", "v1") == "stored"
    assert store.get_reference("acct", "msg-1", source, "image", "v1") == reference


def test_same_content_is_deduplicated_across_messages(tmp_path):
    first = tmp_path / "a.bin"
    second = tmp_path / "b.bin"
    first.write_bytes(b"same")
    second.write_bytes(b"same")
    store = AttachmentStore(tmp_path / "ledger.db", tmp_path / "assets")

    first_ref = store.ingest_file("acct", "msg-1", first, "image", "v1")
    second_ref = store.ingest_file("acct", "msg-2", second, "image", "v1")

    assert first_ref.attachment_id == second_ref.attachment_id
    assert table_count(store.ledger_db_path, "attachments") == 1
    assert table_count(store.ledger_db_path, "attachment_references") == 2


def test_retry_updates_missing_error_reference_without_duplicate(tmp_path):
    source = tmp_path / "missing.bin"
    store = AttachmentStore(tmp_path / "ledger.db", tmp_path / "assets")

    missing = store.ingest_file("acct", "msg-1", source, "image", "v1")
    source.write_bytes(b"now present")
    recovered = store.ingest_file("acct", "msg-1", source, "image", "v1")

    assert missing.status == "error"
    assert missing.error_code == "source_missing"
    assert missing.retryable is True
    assert recovered.status == "stored"
    assert recovered.attempts == 2
    assert recovered.error_code is None
    assert table_count(store.ledger_db_path, "attachment_references") == 1


def test_source_change_creates_retryable_error_and_removes_temp(tmp_path, monkeypatch):
    source = tmp_path / "source.bin"
    source.write_bytes(b"before")
    store = AttachmentStore(tmp_path / "ledger.db", tmp_path / "assets")
    original_copy = store._copy_and_hash

    def change_source_after_copy(source_path, temp_path):
        result = original_copy(source_path, temp_path)
        source_path.write_bytes(b"changed after copy")
        return result

    monkeypatch.setattr(store, "_copy_and_hash", change_source_after_copy)

    reference = store.ingest_file("acct", "msg-1", source, "image", "v1")

    assert reference.status == "error"
    assert reference.error_code == "source_changed"
    assert reference.retryable is True
    assert reference.source_size == len(b"before")
    assert list((tmp_path / "assets").rglob("*.tmp")) == []


def test_existing_target_hash_conflict_is_retryable_error(tmp_path):
    source = tmp_path / "source.bin"
    payload = b"payload"
    source.write_bytes(payload)
    expected_hash = sha256(payload)
    target_dir = tmp_path / "assets" / "sha256" / expected_hash[:2]
    target_dir.mkdir(parents=True)
    (target_dir / expected_hash).write_bytes(b"wrong")
    store = AttachmentStore(tmp_path / "ledger.db", tmp_path / "assets")

    reference = store.ingest_file("acct", "msg-1", source, "image", "v1")

    assert reference.status == "error"
    assert reference.error_code == "content_address_conflict"
    assert reference.retryable is True
    assert reference.source_size == len(payload)
    assert reference.content_sha256 == expected_hash
    assert table_count(store.ledger_db_path, "attachments") == 0
    assert list((tmp_path / "assets").rglob("*.tmp")) == []


def test_reingesting_same_reference_does_not_duplicate(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"payload")
    store = AttachmentStore(tmp_path / "ledger.db", tmp_path / "assets")

    first = store.ingest_file("acct", "msg-1", source, "image", "v1")
    second = store.ingest_file("acct", "msg-1", source, "image", "v1")

    assert first.reference_id == second.reference_id
    assert second.attempts == 2
    assert table_count(store.ledger_db_path, "attachment_references") == 1
    assert table_count(store.ledger_db_path, "attachments") == 1


def test_read_failure_is_retryable_error(tmp_path):
    source = tmp_path / "not-a-file"
    source.mkdir()
    store = AttachmentStore(tmp_path / "ledger.db", tmp_path / "assets")

    reference = store.ingest_file("acct", "msg-1", source, "image", "v1")

    assert reference.status == "error"
    assert reference.error_code == "read_failed"
    assert reference.retryable is True


def test_unknown_kind_is_stored_unchanged(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"payload")
    store = AttachmentStore(tmp_path / "ledger.db", tmp_path / "assets")

    reference = store.ingest_file("acct", "msg-1", source, "custom-kind", "v1")

    assert reference.status == "stored"
    assert reference.attachment_kind == "custom-kind"


def test_unexpected_copy_failure_is_recorded_and_temp_is_removed(tmp_path, monkeypatch):
    source = tmp_path / "source.bin"
    source.write_bytes(b"payload")
    store = AttachmentStore(tmp_path / "ledger.db", tmp_path / "assets")

    def fail_copy(_source, temp_path):
        temp_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path.write_bytes(b"partial")
        raise RuntimeError("copy implementation failed")

    monkeypatch.setattr(store, "_copy_and_hash", fail_copy)

    reference = store.ingest_file("acct", "msg-1", source, "image", "v1")

    assert reference.status == "error"
    assert reference.error_code == "ingest_failed"
    assert reference.retryable is True
    assert "copy implementation failed" in reference.error_message
    assert list((tmp_path / "assets").rglob("*.tmp")) == []


def test_same_size_source_replacement_is_detected_by_content_hash(
    tmp_path, monkeypatch
):
    source = tmp_path / "source.bin"
    source.write_bytes(b"before")
    store = AttachmentStore(tmp_path / "ledger.db", tmp_path / "assets")
    original_copy = store._copy_and_hash

    def replace_after_copy(source_path, temp_path):
        result = original_copy(source_path, temp_path)
        source_path.write_bytes(b"after!")
        return result

    monkeypatch.setattr(store, "_copy_and_hash", replace_after_copy)
    monkeypatch.setattr(attachment_store_module, "_same_stat", lambda *_args: True)

    reference = store.ingest_file("acct", "msg-1", source, "image", "v1")

    assert reference.status == "error"
    assert reference.error_code == "source_changed"
    assert list((tmp_path / "assets").rglob("*.tmp")) == []


def test_concurrent_ingest_uses_one_reference_and_atomic_attempt_count(
    tmp_path, monkeypatch
):
    source = tmp_path / "source.bin"
    source.write_bytes(b"payload")
    store = AttachmentStore(tmp_path / "ledger.db", tmp_path / "assets")
    original_copy = store._copy_and_hash
    copy_barrier = threading.Barrier(2)

    def synchronized_copy(source_path, temp_path):
        result = original_copy(source_path, temp_path)
        copy_barrier.wait(timeout=5)
        return result

    monkeypatch.setattr(store, "_copy_and_hash", synchronized_copy)
    arguments = ("acct", "msg-1", source, "image", "v1")
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: store.ingest_file(*arguments), range(2)))

    current = store.get_reference(*arguments)
    assert {result.reference_id for result in results} == {current.reference_id}
    assert {result.status for result in results} == {"stored"}
    assert current.status == "stored"
    assert current.attempts == 2
    assert table_count(store.ledger_db_path, "attachment_references") == 1
    assert table_count(store.ledger_db_path, "attachments") == 1


def test_superseded_concurrent_attempt_waits_for_terminal_reference(
    tmp_path, monkeypatch
):
    source = tmp_path / "source.bin"
    source.write_bytes(b"payload")
    store = AttachmentStore(tmp_path / "ledger.db", tmp_path / "assets")
    original_begin = store._begin_attempt
    original_copy = store._copy_and_hash
    original_wait = store._wait_for_terminal_reference
    thread_state = threading.local()
    first_began = threading.Event()
    second_began = threading.Event()
    first_wait_started = threading.Event()
    allow_second_copy = threading.Event()

    def coordinated_begin(*args, **kwargs):
        reference_id, attempts = original_begin(*args, **kwargs)
        thread_state.attempts = attempts
        if attempts == 1:
            first_began.set()
        if attempts == 2:
            second_began.set()
        return reference_id, attempts

    def coordinated_copy(source_path, temp_path):
        result = original_copy(source_path, temp_path)
        if thread_state.attempts == 1:
            assert second_began.wait(timeout=5)
        if thread_state.attempts == 2:
            assert allow_second_copy.wait(timeout=5)
        return result

    def coordinated_wait(reference_id):
        if thread_state.attempts == 1:
            first_wait_started.set()
        return original_wait(reference_id)

    def release_second_copy_after_first_waits():
        assert first_wait_started.wait(timeout=5)
        time.sleep(0.05)
        allow_second_copy.set()

    monkeypatch.setattr(store, "_begin_attempt", coordinated_begin)
    monkeypatch.setattr(store, "_copy_and_hash", coordinated_copy)
    monkeypatch.setattr(store, "_wait_for_terminal_reference", coordinated_wait)
    arguments = ("acct", "msg-1", source, "image", "v1")
    releaser = threading.Thread(target=release_second_copy_after_first_waits)
    releaser.start()
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(store.ingest_file, *arguments)
            assert first_began.wait(timeout=5)
            second = executor.submit(store.ingest_file, *arguments)
            results = [first.result(timeout=5), second.result(timeout=5)]
    finally:
        allow_second_copy.set()
        releaser.join(timeout=5)

    current = store.get_reference(*arguments)
    assert {result.status for result in results} == {"stored"}
    assert current.status == "stored"
    assert current.attempts == 2
    assert table_count(store.ledger_db_path, "attachment_references") == 1


def test_success_record_failure_is_explicit_and_retryable(tmp_path, monkeypatch):
    source = tmp_path / "source.bin"
    source.write_bytes(b"payload")
    store = AttachmentStore(tmp_path / "ledger.db", tmp_path / "assets")
    original_finish = store._finish_success
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError("simulated finish failure")
        return original_finish(*args, **kwargs)

    monkeypatch.setattr(store, "_finish_success", fail_once)

    failed = store.ingest_file("acct", "msg-1", source, "image", "v1")
    recovered = store.ingest_file("acct", "msg-1", source, "image", "v1")

    assert failed.status == "error"
    assert failed.error_code == "ingest_failed"
    assert recovered.status == "stored"
    assert recovered.attempts == 2
    assert table_count(store.ledger_db_path, "attachment_references") == 1
    assert table_count(store.ledger_db_path, "attachments") == 1
