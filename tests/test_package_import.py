"""Minimal CI smoke test for the installable package."""


def test_package_imports() -> None:
    import wecom_reader

    assert wecom_reader.__version__ == "0.1.0"
