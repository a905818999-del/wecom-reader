import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from wecom_reader.export.audit import (
    AUDIT_FIELDS,
    AUDIT_PARSE_STATUSES,
    AUDIT_SOURCES,
    AuditExportSummary,
    canonical_audit_record_json,
    write_audit_jsonl,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "audit_contract_v1.jsonl"
FIXTURE_SHA256 = "46c9e7e7738fff7835c43876161c6c9975d5604a2d539d84bafa4b6b3aae0bef"


def _fixture_record() -> dict:
    lines = FIXTURE.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    return json.loads(lines[0])


def test_contract_fixture_sha_is_pinned():
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == FIXTURE_SHA256


def test_canonical_serializer_matches_contract_fixture_bytes():
    record = _fixture_record()

    assert tuple(record) == AUDIT_FIELDS
    assert FIXTURE.read_text(encoding="utf-8") == (
        canonical_audit_record_json(record) + "\n"
    )


def test_canonical_generation_vector_has_exact_fields_and_lf(tmp_path: Path):
    record = _fixture_record()
    output = tmp_path / "reader.jsonl"

    summary = write_audit_jsonl(output, [record])

    assert output.read_bytes() == FIXTURE.read_bytes()
    assert summary.to_dict()["contract_version"] == 1
    assert summary.record_count == 1
    assert summary.unique_record_count == 1
    assert summary.duplicate_count == 0
    assert tuple(record) == AUDIT_FIELDS


@pytest.mark.parametrize("source", sorted(AUDIT_SOURCES))
def test_producer_source_allowlist_accepts_contract_sources(source: str):
    record = _fixture_record()
    record["source"] = source

    assert json.loads(canonical_audit_record_json(record))["source"] == source


@pytest.mark.parametrize("parse_status", sorted(AUDIT_PARSE_STATUSES))
def test_producer_parse_status_allowlist_accepts_contract_statuses(parse_status: str):
    record = _fixture_record()
    record["parse_status"] = parse_status

    assert (
        json.loads(canonical_audit_record_json(record))["parse_status"] == parse_status
    )


@pytest.mark.parametrize(
    "source",
    ["ui", "historical-only", "truth", "visual", "ocr"],
)
def test_producer_rejects_verifier_only_source(source: str):
    record = _fixture_record()
    record["source"] = source

    with pytest.raises(ValueError, match="source"):
        canonical_audit_record_json(record)


def test_producer_rejects_verifier_only_drift_status():
    record = _fixture_record()
    record["parse_status"] = "DRIFT"

    with pytest.raises(ValueError, match="parse_status"):
        canonical_audit_record_json(record)


def test_summary_declares_contract_version():
    summary = AuditExportSummary(
        record_count=0,
        unique_record_count=0,
        duplicate_count=0,
        parse_status_counts={},
        message_type_counts={},
    )

    assert summary.to_dict()["contract_version"] == 1


def test_privacy_scan_reports_only_file_and_category(tmp_path: Path):
    leak = tmp_path / "leak.jsonl"
    leak.write_text(
        '{"content":"C:\\\\Users\\\\alice\\\\secret.txt","token":"abc"}\n',
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_audit_privacy.py"), str(leak)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert result.stderr == ""
    assert "secret.txt" not in result.stdout
    for line in result.stdout.splitlines():
        payload = json.loads(line)
        assert set(payload) == {"file", "category"}


def test_privacy_scan_accepts_directory_and_missing_output(tmp_path: Path):
    fixture_dir = tmp_path / "fixtures"
    fixture_dir.mkdir()
    fixture_dir.joinpath("safe.jsonl").write_bytes(FIXTURE.read_bytes())

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "check_audit_privacy.py"),
            str(fixture_dir),
            str(tmp_path / "missing-output"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_privacy_scan_does_not_treat_url_scheme_as_windows_path(tmp_path: Path):
    artifact = tmp_path / "url.json"
    artifact.write_text('{"url":"https://example.invalid/a"}\n', encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "check_audit_privacy.py"),
            str(artifact),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""
