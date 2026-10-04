import json
from pathlib import Path

import pytest
from conftest import content_dict
from fake_paperless import FakePaperless

from paperless_bedrock.config import Settings
from paperless_bedrock.model import JudgeRequest, JudgeResult, ModelRequest, ModelResult
from paperless_bedrock.paperless import PaperlessClient, PaperlessError
from paperless_bedrock.pdf import read_xmp_analysis
from paperless_bedrock.schema import Assignment, LetterContent
from paperless_bedrock.service import Pipeline, convert


class FakeAnalyzer:
    model_id = "fake-model"

    def __init__(self, *responses: LetterContent) -> None:
        self.responses = list(responses)
        self.requests: list[ModelRequest] = []

    def analyze(self, request: ModelRequest) -> ModelResult:
        self.requests.append(request)
        return ModelResult(self.responses.pop(0), usage={"inputTokens": 100, "outputTokens": 10})


class FakeJudge:
    def __init__(self, *results: JudgeResult) -> None:
        self.results = list(results)
        self.requests: list[JudgeRequest] = []

    def judge(self, request: JudgeRequest) -> JudgeResult:
        self.requests.append(request)
        if self.results:
            return self.results.pop(0)
        return JudgeResult(match_id=None, confident=False, reason="no match")


def settings(tmp_path: Path, **overrides: object) -> Settings:
    values = {
        "paperless_url": "http://paperless",
        "paperless_token": "secret",
        "data_dir": tmp_path,
    }
    return Settings(**{**values, **overrides})  # type: ignore[arg-type]


def pipeline(
    fake: FakePaperless,
    analyzer: FakeAnalyzer,
    tmp_path: Path,
    judge: FakeJudge | None = None,
    **kw: object,
) -> Pipeline:
    client = PaperlessClient("http://paperless", "secret", transport=fake.transport())
    return Pipeline(settings(tmp_path, **kw), client, analyzer, judge or FakeJudge())


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


# --- tax classification ------------------------------------------------------------------

TAX_CONTEXT = """
[tax]
facts = ["Alex runs a US LLC; its business expenses are deductible in the US."]
[[tax.scopes]]
id = "de_personal"
jurisdiction = "DE"
description = "German joint income tax return"
tag = "tax-DE"
[[tax.scopes]]
id = "us_llc"
jurisdiction = "US"
description = "US LLC business return"
tag = "tax-LLC"
"""


def tax_setup(letter_pdf: bytes, tmp_path: Path) -> tuple[FakePaperless, Path]:
    path = tmp_path / "context.toml"
    path.write_text(TAX_CONTEXT)
    fake = FakePaperless(letter_pdf)
    fake.tags.update({"tax-DE": 20, "tax-LLC": 21, "tax-unclear": 22})
    fake.custom_fields.update({"Tax year": (15, "integer"), "Tax categories": (16, "string")})
    return fake, path


def with_tax(**tax: object) -> LetterContent:
    evidence = [{"quote": "Geänderter Bescheid für 2024 über Einkommensteuer", "page": 1}]
    return LetterContent.model_validate(
        content_dict(tax={"reason": "Steuerbescheid", "evidence": evidence, **tax})
    )


def test_tax_relevant_sets_scope_tags_and_fields(letter_pdf: bytes, tmp_path: Path) -> None:
    fake, path = tax_setup(letter_pdf, tmp_path)
    fake.doc["tags"] = [5, 22]  # stale tax-unclear from an earlier run
    content = with_tax(relevance="yes", scopes=["de_personal"], year=2024, categories=["ESt"])
    analyzer = FakeAnalyzer(content)
    outcome = pipeline(fake, analyzer, tmp_path, context_file=path).run(1)

    assert outcome.analysis is not None and outcome.analysis.validation.status == "passed"
    assert "de_personal (DE): German joint income tax return" in analyzer.requests[0].system_prompt
    assert "Alex runs a US LLC" in analyzer.requests[0].system_prompt
    patch = fake.patches[0]
    assert patch["tags"] == [5, 20]
    values = {f["field"]: f["value"] for f in patch["custom_fields"]}
    assert values[15] == 2024 and values[16] == "ESt"
    assert json.loads(fake.notes[0])["tax"]["year"] == 2024


def test_not_tax_relevant_removes_tax_tags(letter_pdf: bytes, tmp_path: Path) -> None:
    fake, path = tax_setup(letter_pdf, tmp_path)
    fake.doc["tags"] = [5, 20, 21]
    pipeline(fake, FakeAnalyzer(with_tax(relevance="no")), tmp_path, context_file=path).run(1)
    patch = fake.patches[0]
    assert patch["tags"] == [5]
    values = {f["field"]: f["value"] for f in patch["custom_fields"]}
    assert values[15] is None and values[16] is None


def test_unclear_or_missing_tax_gets_unclear_tag(letter_pdf: bytes, tmp_path: Path) -> None:
    fake, path = tax_setup(letter_pdf, tmp_path)
    unclear = with_tax(relevance="unclear")
    pipeline(fake, FakeAnalyzer(unclear), tmp_path, context_file=path).run(1)
    assert fake.patches[0]["tags"] == [5, 22]

    fake, path = tax_setup(letter_pdf, tmp_path)
    missing = good()  # model ignored the tax field twice
    outcome = pipeline(fake, FakeAnalyzer(missing, missing), tmp_path, context_file=path).run(1)
    assert fake.patches[0]["tags"] == [5, 22]
    assert outcome.analysis is not None
    assert any(i.field == "tax" for i in outcome.analysis.validation.issues)


def test_without_tax_scopes_tags_are_untouched(letter_pdf: bytes, tmp_path: Path) -> None:
    fake = FakePaperless(letter_pdf)
    fake.tags.update({"tax-DE": 20})
    fake.doc["tags"] = [5, 20]
    analyzer = FakeAnalyzer(good())
    pipeline(fake, analyzer, tmp_path).run(1)
    assert "tags" not in fake.patches[0]
    assert "set `tax` to null" in analyzer.requests[0].system_prompt


# --- correspondents, document types, persons, knowledge index ----------------------------


def assigned(outcome: object, field: str = "correspondent") -> Assignment:
    analysis = getattr(outcome, "analysis", None)
    assert analysis is not None and analysis.paperless is not None
    value = getattr(analysis.paperless, field)
    assert isinstance(value, Assignment)
    return value


def classified(
    correspondent: str, document_type: str = "Tax assessment", **extra: object
) -> LetterContent:
    data = content_dict(
        classification={"correspondent": correspondent, "document_type": document_type}
    )
    data.update(extra)
    return LetterContent.model_validate(data)


CREDITOR = {
    "kind": "creditor_id",
    "value": "DE98ZZZ09999999999",
    "evidence": {"quote": "Finanzamt Musterstadt", "page": 1},
}


def test_new_correspondent_and_type_are_created_and_assigned(
    letter_pdf: bytes, tmp_path: Path
) -> None:
    fake = FakePaperless(letter_pdf)
    analyzer = FakeAnalyzer(classified("Finanzamt Musterstadt"))
    outcome = pipeline(fake, analyzer, tmp_path).run(1)

    assert outcome.analysis is not None and outcome.analysis.paperless is not None
    corr = outcome.analysis.paperless.correspondent
    assert corr is not None and corr.matched_by == "created"
    assert fake.objects["correspondents"][corr.id]["name"] == "Finanzamt Musterstadt"
    assert fake.patches[0]["correspondent"] == corr.id
    assert fake.patches[0]["document_type"] == assigned(outcome, "document_type").id
    assert "Existing correspondents" in analyzer.requests[0].system_prompt


def test_existing_and_alias_names_are_reused(letter_pdf: bytes, tmp_path: Path) -> None:
    fake = FakePaperless(letter_pdf)
    fake.objects["correspondents"][5] = {"name": "Finanzamt Musterstadt", "owner": None}
    p = pipeline(fake, FakeAnalyzer(classified("finanzamt  musterstadt")), tmp_path)
    outcome = p.run(1)
    assert outcome.analysis.paperless.correspondent.matched_by == "exact"  # type: ignore[union-attr]

    p.index.add_alias("correspondent", "FA Musterstadt", 5)
    p.analyzer = FakeAnalyzer(classified("FA Musterstadt"))
    outcome = p.run(1, force=True)
    assert outcome.analysis.paperless.correspondent.matched_by == "alias"  # type: ignore[union-attr]
    assert len(fake.objects["correspondents"]) == 1


def test_similar_name_is_judged(letter_pdf: bytes, tmp_path: Path) -> None:
    fake = FakePaperless(letter_pdf)
    fake.objects["correspondents"][5] = {"name": "Finanzamt Musterstadt", "owner": None}
    judge = FakeJudge(JudgeResult(match_id=5, confident=True, reason="abbreviation"))
    p = pipeline(fake, FakeAnalyzer(classified("Finanzamt Musterstdt")), tmp_path, judge=judge)
    outcome = p.run(1)
    assert outcome.analysis.paperless.correspondent.id == 5  # type: ignore[union-attr]
    assert judge.requests[0].candidates[0]["name"] == "Finanzamt Musterstadt"
    assert p.index.alias_target("correspondent", "Finanzamt Musterstdt") == 5

    # a confident "no" creates a new correspondent
    fake2 = FakePaperless(letter_pdf)
    fake2.objects["correspondents"][5] = {"name": "Stadtwerke Musterstadt", "owner": None}
    p2 = pipeline(fake2, FakeAnalyzer(classified("Stadtwerke Musterstadt Netze")), tmp_path / "b")
    assert p2.run(1).analysis.paperless.correspondent.matched_by == "created"  # type: ignore[union-attr]


def test_identity_reference_decides_sender(letter_pdf: bytes, tmp_path: Path) -> None:
    fake = FakePaperless(letter_pdf)
    sender = {
        "name": "Stadtwerke Musterstadt GmbH",
        "sender_type": "utility",
        "identifiers": [CREDITOR],
    }
    first = classified("Stadtwerke Musterstadt GmbH", sender=sender)
    p = pipeline(fake, FakeAnalyzer(first), tmp_path)
    created = assigned(p.run(1))

    judge = FakeJudge()
    p.resolver.judge = judge
    second = classified("SWM Energie", sender={**sender, "name": "SWM Energie"})
    p.analyzer = FakeAnalyzer(second)
    assignment = assigned(p.run(1, force=True))
    assert assignment.id == created.id and assignment.matched_by == "identity_reference"
    assert judge.requests == []
    assert p.index.alias_target("correspondent", "SWM Energie") == created.id


def test_related_documents_are_given_as_context(letter_pdf: bytes, tmp_path: Path) -> None:
    fake = FakePaperless(letter_pdf)
    p = pipeline(fake, FakeAnalyzer(classified("Finanzamt Musterstadt")), tmp_path)
    p.index.record_document(
        42,
        correspondent_id=None,
        document_type_id=None,
        sender_name="Finanzamt Musterstadt",
        sender_address=None,
        subject="Bescheid 2023",
        document_date="2025-05-01",
        summary={"document_id": 42, "subject": "Bescheid 2023"},
        refs=[
            __import__("paperless_bedrock.identifiers").identifiers.Reference(
                "tax_number", "1234567890"
            )
        ],
    )
    p.run(1)
    assert '"document_id": 42' in p.analyzer.requests[0].text  # type: ignore[attr-defined]
    assert "tax_number:1234567890" in p.analyzer.requests[0].text  # type: ignore[attr-defined]


def test_person_tags_and_light_mode(letter_pdf: bytes, tmp_path: Path) -> None:
    path = tmp_path / "context.toml"
    path.write_text(
        '[[persons]]\nname = "Erika Mustermann"\ntag = "Erika"\n'
        'aliases = ["Dr. Erika Mustermann"]\n'
        '[[persons]]\nname = "Max Mustermann"\ntag = "Max"\n'
    )
    fake = FakePaperless(letter_pdf)
    fake.tags.update({"Erika": 30, "Max": 31})
    fake.doc["tags"] = [5, 31]  # stale person tag
    content = classified(
        "Finanzamt Musterstadt", recipients={"known_person_match": ["Dr. Erika Mustermann"]}
    )
    p = pipeline(fake, FakeAnalyzer(content), tmp_path, context_file=path)
    outcome = p.run(1, light=True)
    assert outcome.analysis is not None and outcome.analysis.paperless is not None
    assert outcome.analysis.paperless.persons == ["Erika Mustermann"]
    patch = fake.patches[0]
    assert patch["tags"] == [5, 30]
    assert patch["title"].startswith("Finanzamt Musterstadt") and fake.versions == []
    assert len(fake.notes) == 1


def test_reindex_takes_paperless_assignments(letter_pdf: bytes, tmp_path: Path) -> None:
    fake = FakePaperless(letter_pdf)
    sender = {"name": "Stadtwerke", "sender_type": "utility", "identifiers": [CREDITOR]}
    p = pipeline(fake, FakeAnalyzer(classified("Stadtwerke", sender=sender)), tmp_path)
    p.run(1)
    fake.doc["correspondent"] = 999  # reassigned by hand in paperless
    assert p.reindex() == 1
    assert p.index.correspondents_for_identity(
        [
            __import__("paperless_bedrock.identifiers").identifiers.Reference(
                "creditor_id", "DE98ZZZ09999999999"
            )
        ]
    ) == {999}
