import json
from pathlib import Path

import pytest
from conftest import content_dict
from fake_paperless import FakePaperless

from paperless_bedrock.config import Settings
from paperless_bedrock.model import ModelRequest, ModelResult
from paperless_bedrock.paperless import PaperlessClient, PaperlessError
from paperless_bedrock.pdf import read_xmp_analysis
from paperless_bedrock.schema import LetterContent
from paperless_bedrock.service import Pipeline, convert


class FakeAnalyzer:
    model_id = "fake-model"

    def __init__(self, *responses: LetterContent) -> None:
        self.responses = list(responses)
        self.requests: list[ModelRequest] = []

    def analyze(self, request: ModelRequest) -> ModelResult:
        self.requests.append(request)
        return ModelResult(self.responses.pop(0), usage={"inputTokens": 100, "outputTokens": 10})


def settings(tmp_path: Path, **overrides: object) -> Settings:
    values = {
        "paperless_url": "http://paperless",
        "paperless_token": "secret",
        "data_dir": tmp_path,
    }
    return Settings(**{**values, **overrides})  # type: ignore[arg-type]


def pipeline(fake: FakePaperless, analyzer: FakeAnalyzer, tmp_path: Path, **kw: object) -> Pipeline:
    client = PaperlessClient("http://paperless", "secret", transport=fake.transport())
    return Pipeline(settings(tmp_path, **kw), client, analyzer)


def good() -> LetterContent:
    return LetterContent.model_validate(content_dict())


def test_full_run_writes_everything(letter_pdf: bytes, tmp_path: Path) -> None:
    fake = FakePaperless(letter_pdf)
    analyzer = FakeAnalyzer(good())
    outcome = pipeline(fake, analyzer, tmp_path).run(1)

    assert outcome.status == "analyzed" and outcome.analysis is not None
    assert outcome.analysis.validation.status == "passed"
    # model input: text with page markers and one page image
    request = analyzer.requests[0]
    assert '<page number="1">' in request.text and "447,19 EUR" in request.text
    assert list(request.images) == [1]
    # root version is always requested explicitly, archive file preferred
    assert fake.downloads == [{"version": "1"}]
    # fields: merged with the foreign field 99, typed per paperless data type
    patch = fake.patches[0]
    assert (
        patch["title"]
        == "Finanzamt Musterstadt – Geänderter Bescheid für 2024 über Einkommensteuer"
    )
    assert {"field": 99, "value": "keep me"} in patch["custom_fields"]
    values = {f["field"]: f["value"] for f in patch["custom_fields"]}
    assert values[11] is True
    assert values[12] == "EUR447.19"
    assert values[13] == "2026-10-26"
    assert values[14] == "DE89370400440532013000"
    assert "tags" not in patch
    # version with XMP, then the note as completion marker
    filename, pdf, label = fake.versions[0]
    assert (filename, label) == ("1-analysis-v1.pdf", "analysis-v1")
    stamped = read_xmp_analysis(pdf)
    assert stamped is not None and stamped["source"]["paperless_document_id"] == 1
    note = json.loads(fake.notes[0])
    assert note["schema_version"] == "1.0"
    assert note["source"]["source_checksum"] == "sha256:" + fake.doc["versions"][0]["checksum"]


def test_second_run_is_skipped(letter_pdf: bytes, tmp_path: Path) -> None:
    fake = FakePaperless(letter_pdf)
    pipeline(fake, FakeAnalyzer(good()), tmp_path).run(1)
    analyzer = FakeAnalyzer()
    assert pipeline(fake, analyzer, tmp_path).run(1).status == "skipped"
    assert analyzer.requests == []


def test_issues_trigger_one_retry_with_feedback(letter_pdf: bytes, tmp_path: Path) -> None:
    bad = content_dict()
    bad["money"][0]["amount"] = "474.19"
    analyzer = FakeAnalyzer(LetterContent.model_validate(bad), good())
    outcome = pipeline(FakePaperless(letter_pdf), analyzer, tmp_path).run(1)
    assert len(analyzer.requests) == 2
    assert analyzer.requests[1].feedback and "474.19" in analyzer.requests[1].feedback
    assert outcome.analysis is not None and outcome.analysis.validation.status == "passed"


def test_failed_validation_is_stored_and_tagged(letter_pdf: bytes, tmp_path: Path) -> None:
    bad = content_dict()
    bad["payment"]["payee_iban"] = "DE88370400440532013000"
    bad_content = LetterContent.model_validate(bad)
    fake = FakePaperless(letter_pdf)
    outcome = pipeline(fake, FakeAnalyzer(bad_content, bad_content), tmp_path).run(1)
    assert outcome.analysis is not None and outcome.analysis.validation.status == "failed"
    assert fake.patches[0]["tags"] == [5, 7]
    assert json.loads(fake.notes[0])["validation"]["status"] == "failed"


def test_original_used_when_no_archive(letter_pdf: bytes, tmp_path: Path) -> None:
    fake = FakePaperless(letter_pdf, has_archive=False)
    pipeline(fake, FakeAnalyzer(good()), tmp_path).run(1)
    assert fake.downloads == [{"version": "1", "original": "true"}]


def test_optional_outputs_can_be_disabled(letter_pdf: bytes, tmp_path: Path) -> None:
    fake = FakePaperless(letter_pdf)
    fake.custom_fields = {}
    pipeline(fake, FakeAnalyzer(good()), tmp_path, set_title=False, write_version=False).run(1)
    assert fake.patches == [] and fake.versions == [] and len(fake.notes) == 1


def test_paperless_client_errors_are_permanent(letter_pdf: bytes, tmp_path: Path) -> None:
    fake = FakePaperless(letter_pdf)
    fake.fail_notes_with = 403
    with pytest.raises(PaperlessError):
        pipeline(fake, FakeAnalyzer(good()), tmp_path).run(1)


def test_mark_failed_adds_tag(letter_pdf: bytes, tmp_path: Path) -> None:
    fake = FakePaperless(letter_pdf)
    pipeline(fake, FakeAnalyzer(), tmp_path).mark_failed(1, "boom")
    assert fake.patches == [{"tags": [5, 7]}]


@pytest.mark.parametrize(
    ("data_type", "expected"),
    [("monetary", "EUR5.00"), ("float", 5.0), ("string", "5.00 EUR")],
)
def test_convert_amount(data_type: str, expected: object) -> None:
    from decimal import Decimal

    assert convert((Decimal("5"), "EUR"), data_type) == expected
