from pathlib import Path

import pytest

from paperless_bedrock.config import Settings


def test_settings_from_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for key, value in {
        "PAPERLESS_URL": "http://webserver:8000",
        "PAPERLESS_TOKEN": "t",
        "CUSTOM_FIELDS__AMOUNT": "Betrag",
        "NOISE_PROFILES": "[]",
        "WRITE_VERSION": "false",
    }.items():
        monkeypatch.setenv(key, value)
    s = Settings()
    assert s.custom_fields.amount == "Betrag"
    assert s.custom_fields.due_date == "Due date"
    assert s.noise_profiles == []
    assert s.write_version is False
    assert s.link_base == "http://webserver:8000"


def test_recipient_context_file(tmp_path: Path) -> None:
    path = tmp_path / "r.toml"
    path.write_text('names = ["Erika"]\nown_ibans = ["DE02 1203 0000 0000 2020 51"]\n')
    s = Settings(paperless_url="u", paperless_token="t", recipient_context_file=path)
    assert s.recipient_context().names == ["Erika"]
