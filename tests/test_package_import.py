"""Minimal CI smoke test for installed distribution metadata."""

from importlib.metadata import version


def test_distribution_metadata_is_available() -> None:
    assert version("wecom-reader") == "0.1.0"
