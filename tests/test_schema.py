import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from paperless_bedrock.cli import json_schema
from paperless_bedrock.schema import LetterAnalysis, LetterContent

SCHEMA_FILE = Path(__file__).parent.parent / "schema" / "letter-analysis-v1.schema.json"

EVIDENCE = {"quote": "Bitte zahlen Sie spätestens am 26.10.2026", "page": 2}

CONTENT: dict[str, Any] = {
    "document": {
        "language": "de",
        "country": "DE",
        "date": "2026-09-22",
        "type": "tax_assessment",
        "subject": "Geänderter Bescheid für 2024 über Einkommensteuer",
        "summary": "Amended income tax assessment 2024 with a back-payment.",
    },
    "sender": {"name": "Finanzamt Musterstadt", "sender_type": "tax_office"},
    "recipients": {"addressed_to": ["Erika Mustermann"]},
    "money": [{"role": "amount_due", "amount": "447.19", "currency": "EUR", "evidence": EVIDENCE}],
    "payment": {
        "direction": "recipient_pays",
        "method_stated": "transfer_requested",
        "amount": "447.19",
        "currency": "EUR",
        "due_date": "2026-10-26",
        "evidence": [EVIDENCE],
    },
    "requested_actions": [
        {
            "action": "pay",
            "obligation": "required",
            "deadline": "2026-10-26",
            "description": "Pay 447.19 EUR income tax back-payment 2024",
            "evidence": [EVIDENCE],
        }
    ],
    "deadlines": [{"kind": "payment_due", "date": "2026-10-26", "evidence": EVIDENCE}],
    "escalation_level": "none",
}

RECORD = {
    **CONTENT,
    "analyzer": {
        "model": "eu.anthropic.claude-sonnet-5-5",
        "prompt_version": "1",
        "tool_version": "0.1.0",
        "analyzed_at": "2026-10-03T10:00:00Z",
    },
    "source": {"paperless_document_id": 123, "source_checksum": "sha256:" + "0" * 64, "pages": 9},
    "input": {"pages_sent": 9, "images_sent": True, "truncated": False},
    "validation": {"status": "passed"},
}


def test_valid_record_round_trips() -> None:
    analysis = LetterAnalysis.model_validate(RECORD)
    assert analysis.schema_version == "1.0"
    assert LetterAnalysis.model_validate_json(analysis.model_dump_json()) == analysis


def test_model_output_cannot_contain_code_fields() -> None:
    with pytest.raises(ValidationError):
        LetterContent.model_validate({**CONTENT, "validation": {"status": "passed"}})


@pytest.mark.parametrize("amount", ["447,19", "1.234,56", "447.199", "EUR 447.19"])
def test_amount_must_be_decimal_string(amount: str) -> None:
    bad = {**CONTENT, "money": [{**CONTENT["money"][0], "amount": amount}]}
    with pytest.raises(ValidationError):
        LetterContent.model_validate(bad)


def test_required_action_needs_evidence() -> None:
    action = {**CONTENT["requested_actions"][0], "evidence": []}
    with pytest.raises(ValidationError):
        LetterContent.model_validate({**CONTENT, "requested_actions": [action]})


def test_committed_schema_is_up_to_date() -> None:
    assert json.loads(SCHEMA_FILE.read_text(encoding="utf-8")) == json.loads(json_schema()), (
        "Run: paperless-bedrock schema > schema/letter-analysis-v1.schema.json"
    )
