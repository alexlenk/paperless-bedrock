import io
from typing import Any

import pytest
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from paperless_bedrock.schema import LetterContent

LETTER_LINES = [
    "Finanzamt Musterstadt",
    "Geänderter Bescheid für 2024 über Einkommensteuer",
    "Sehr geehrte Frau Mustermann,",
    "für das Jahr 2024 ergibt sich eine Nachzahlung.",
    "Bitte zahlen Sie 447,19 EUR spätestens am 26.10.2026",
    "auf das Konto IBAN DE89 3704 0044 0532 0130 00.",
    "Verwendungszweck: St.-Nr. 12345/67890 ESt 2024",
    "Mit freundlichen Grüßen",
]


def make_pdf(pages: list[list[str]]) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    for lines in pages:
        y = 800
        for line in lines:
            c.drawString(72, y, line)
            y -= 18
        c.showPage()
    c.save()
    return buf.getvalue()


EVIDENCE = {"quote": "Bitte zahlen Sie 447,19 EUR spätestens am 26.10.2026", "page": 1}


def content_dict(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "document": {
            "language": "de",
            "country": "DE",
            "date": "2026-09-22",
            "type": "tax_assessment",
            "subject": "Geänderter Bescheid für 2024 über Einkommensteuer",
            "summary": "Geänderter Steuerbescheid 2024 mit Nachzahlung.",
        },
        "sender": {"name": "Finanzamt Musterstadt", "sender_type": "tax_office"},
        "recipients": {"addressed_to": ["Erika Mustermann"]},
        "money": [
            {"role": "amount_due", "amount": "447.19", "currency": "EUR", "evidence": EVIDENCE}
        ],
        "payment": {
            "direction": "recipient_pays",
            "method_stated": "transfer_requested",
            "amount": "447.19",
            "currency": "EUR",
            "due_date": "2026-10-26",
            "payee_name": "Finanzamt Musterstadt",
            "payee_iban": "DE89370400440532013000",
            "reference": "St.-Nr. 12345/67890 ESt 2024",
            "evidence": [EVIDENCE],
        },
        "requested_actions": [
            {
                "action": "pay",
                "obligation": "required",
                "deadline": "2026-10-26",
                "description": "447,19 EUR Einkommensteuer 2024 nachzahlen",
                "evidence": [EVIDENCE],
            }
        ],
        "deadlines": [{"kind": "payment_due", "date": "2026-10-26", "evidence": EVIDENCE}],
        "escalation_level": "none",
    }
    data.update(overrides)
    return data


@pytest.fixture
def content() -> LetterContent:
    return LetterContent.model_validate(content_dict())


@pytest.fixture
def letter_pdf() -> bytes:
    return make_pdf([LETTER_LINES])
