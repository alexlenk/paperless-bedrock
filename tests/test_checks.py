from decimal import Decimal

from conftest import LETTER_LINES, content_dict

from paperless_bedrock import checks
from paperless_bedrock.schema import LetterContent

TEXT = "\n".join(LETTER_LINES)


def test_clean_analysis_passes(content: LetterContent) -> None:
    issues = checks.check(content, TEXT)
    assert issues == []
    assert checks.status(issues) == "passed"


def test_iban_checksum() -> None:
    assert checks.iban_valid("DE89 3704 0044 0532 0130 00")
    assert not checks.iban_valid("DE88 3704 0044 0532 0130 00")
    assert not checks.iban_valid("not an iban")


def test_invalid_iban_fails() -> None:
    data = content_dict()
    data["payment"]["payee_iban"] = "DE88370400440532013000"
    issues = checks.check(LetterContent.model_validate(data), TEXT)
    assert any(i.field == "payment.payee_iban" and i.severity == "error" for i in issues)
    assert checks.status(issues) == "failed"


def test_own_iban_as_payee_fails(content: LetterContent) -> None:
    issues = checks.check(content, TEXT, own_ibans=["DE89 3704 0044 0532 0130 00"])
    assert checks.status(issues) == "failed"


def test_amounts_in_german_and_english_format() -> None:
    found = checks.amounts_in_text("Betrag 1.234,56 EUR, total $1,234.56, fee 0,95, 447")
    assert {Decimal("1234.56"), Decimal("0.95"), Decimal("447")} <= found


def test_amount_not_in_text_is_reported() -> None:
    data = content_dict()
    data["money"][0]["amount"] = "474.19"
    issues = checks.check(LetterContent.model_validate(data), TEXT)
    assert any(i.field == "money[0].amount" for i in issues)
    assert checks.status(issues) == "passed_with_warnings"


def test_invented_quote_is_reported() -> None:
    data = content_dict()
    data["deadlines"][0]["evidence"] = {"quote": "Zahlen Sie sofort oder es wird teuer", "page": 1}
    issues = checks.check(LetterContent.model_validate(data), TEXT)
    assert [i.field for i in issues] == ["deadlines[0]"]


def test_quote_with_small_ocr_differences_passes() -> None:
    data = content_dict()
    quote = "Bitte zahIen Sie 447,19 EUR spätestens am 26.10.2026"  # OCR: l -> I
    data["deadlines"][0]["evidence"] = {"quote": quote, "page": 1}
    assert checks.check(LetterContent.model_validate(data), TEXT) == []


def test_due_date_before_document_date() -> None:
    data = content_dict()
    data["payment"]["due_date"] = "2026-01-01"
    issues = checks.check(LetterContent.model_validate(data), TEXT)
    assert any(i.field == "payment.due_date" for i in issues)


def test_tax_scope_checks() -> None:
    evidence = [{"quote": "Geänderter Bescheid für 2024 über Einkommensteuer", "page": 1}]

    def tax(**kw: object) -> LetterContent:
        return LetterContent.model_validate(
            content_dict(tax={"reason": "r", "evidence": evidence, **kw})
        )

    scopes = ["de_personal"]
    assert checks.check(tax(relevance="yes", scopes=["de_personal"]), TEXT, tax_scopes=scopes) == []
    fields = [i.field for i in checks.check(tax(relevance="yes", scopes=["x"]), TEXT, None, scopes)]
    assert fields == ["tax.scopes"]
    assert [i.field for i in checks.check(tax(relevance="yes"), TEXT, None, scopes)] == [
        "tax.scopes"
    ]
    assert [i.field for i in checks.check(tax(relevance="no"), TEXT)] == ["tax"]
    missing = checks.check(LetterContent.model_validate(content_dict()), TEXT, None, scopes)
    assert [i.field for i in missing] == ["tax"]
