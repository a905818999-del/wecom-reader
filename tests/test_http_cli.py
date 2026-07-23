from click.testing import CliRunner

from wecom_reader.cli import main


def test_serve_api_defaults_to_loopback_and_passes_reader(monkeypatch, tmp_path):
    calls = {}

    class StubApp:
        def run(self, **kwargs):
            calls["run"] = kwargs

    def fake_create_app(reader):
        calls["reader"] = reader
        return StubApp()

    monkeypatch.setattr("wecom_reader.http_api.create_app", fake_create_app)
    result = CliRunner().invoke(
        main,
        [
            "--db-dir",
            str(tmp_path / "source"),
            "--decrypted-dir",
            str(tmp_path / "decrypted"),
            "serve-api",
        ],
    )

    assert result.exit_code == 0
    assert calls["reader"].db_dir == str(tmp_path / "source")
    assert calls["reader"].decrypted_dir == str(tmp_path / "decrypted")
    assert calls["run"] == {
        "host": "127.0.0.1",
        "port": 8765,
        "debug": False,
        "use_reloader": False,
    }
