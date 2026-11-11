import os
from pathlib import Path

from ethos.config import load_environment

SAMPLE = (
    "ETHOS_PORT=9999\n"
    'DATABASE_URL="postgresql://u:p@h:5432/db?sslmode=require&connect_timeout=10"\n'
    "EMPTY=\n"
)


def test_values_are_loaded_into_the_environment(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(SAMPLE, encoding="utf-8")
    fake: dict[str, str] = {}
    monkeypatch.setattr(os, "environ", fake)

    assert load_environment(env_file) == env_file
    assert fake["ETHOS_PORT"] == "9999"
    # Quotes are stripped and the query string survives intact.
    assert "&" in fake["DATABASE_URL"]
    assert fake["DATABASE_URL"].endswith("connect_timeout=10")
    assert not fake["DATABASE_URL"].startswith('"')


def test_exported_variables_win_over_the_file(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(SAMPLE, encoding="utf-8")
    fake = {"ETHOS_PORT": "1111"}
    monkeypatch.setattr(os, "environ", fake)

    load_environment(env_file)

    assert fake["ETHOS_PORT"] == "1111"


def test_missing_file_is_not_an_error(tmp_path):
    assert load_environment(tmp_path / "absent.env") is None


def test_project_root_is_found_from_another_cwd(tmp_path, monkeypatch):
    # The repository root is the parent of the ethos package.
    root = Path(__file__).resolve().parents[2]
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(os, "environ", {})  # keep the real .env out of this process
    loaded = load_environment()
    assert loaded is not None
    assert loaded == root / ".env"
