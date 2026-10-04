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


def test_context_file(tmp_path: Path) -> None:
    path = tmp_path / "context.toml"
    path.write_text(
        'names = ["Erika"]\n'
        'own_ibans = ["DE02 1203 0000 0000 2020 51"]\n'
        "[tax]\n"
        'facts = ["We rent out a flat."]\n'
        "[[tax.scopes]]\n"
        'id = "de_personal"\n'
        'jurisdiction = "DE"\n'
        'description = "German joint income tax return"\n'
        'tag = "tax-DE"\n'
    )
    context = Settings(paperless_url="u", paperless_token="t", context_file=path).context()
    assert context.names == ["Erika"]
    assert context.tax.scopes[0].tag == "tax-DE"
    assert context.tax.unclear_tag == "tax-unclear"


def test_example_context_file_is_valid() -> None:
    path = Path(__file__).parent.parent / "examples" / "context.toml"
    context = Settings(paperless_url="u", paperless_token="t", context_file=path).context()
    assert [p.tag for p in context.all_persons] == ["Erika", "Max"]
    assert {s.id for s in context.tax.scopes} == {"de_personal", "us_personal", "us_llc"}


@pytest.mark.parametrize("value", ["", "off", "OFF"])
def test_consolidation_can_be_switched_off(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("CONSOLIDATE_HOUR", value)
    assert Settings(paperless_url="u", paperless_token="t").consolidate_hour is None
