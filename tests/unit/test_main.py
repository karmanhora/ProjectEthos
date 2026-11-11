import uvicorn

from ethos import __main__ as cli


def capture(monkeypatch) -> dict:
    calls: dict = {}

    def fake_run(target, **kwargs):
        calls["target"] = target
        calls.update(kwargs)

    monkeypatch.setattr(uvicorn, "run", fake_run)
    return calls


def test_reload_uses_the_import_string_and_a_factory(monkeypatch):
    calls = capture(monkeypatch)
    cli.main(["--reload"])

    assert calls["target"] == "ethos.api.app:create_app"
    assert calls["factory"] is True
    assert calls["reload"] is True
    # Only the package is watched, so .venv cannot trigger restarts.
    assert len(calls["reload_dirs"]) == 1
    assert calls["reload_dirs"][0].endswith("ethos")


def test_without_reload_it_passes_an_app_object(monkeypatch):
    calls = capture(monkeypatch)
    cli.main([])

    assert not isinstance(calls["target"], str)
    assert "factory" not in calls
    assert "reload" not in calls
