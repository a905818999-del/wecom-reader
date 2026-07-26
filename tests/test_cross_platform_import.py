"""Cross-platform import behavior for the Windows-only key extractor."""

import os

import pytest


def test_public_package_imports() -> None:
    import wecom_reader

    assert wecom_reader.__version__ == "0.1.0"


@pytest.mark.skipif(os.name == "nt", reason="non-Windows guard")
def test_key_extraction_reports_windows_requirement() -> None:
    from wecom_reader.crypto.key_extract import extract_key

    with pytest.raises(RuntimeError, match="only supported on Windows"):
        extract_key()
