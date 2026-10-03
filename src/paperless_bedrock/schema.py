"""Letter analysis schema v1.

Two layers (see docs/design.md, section 6):

- ``LetterContent``: what the model returns, everything that can be read from the letter.
- ``LetterAnalysis``: the stored record, ``LetterContent`` plus fields set by code only.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION: Final = "1.0"

Amount = Annotated[
    str, Field(pattern=r"^-?\d+(\.\d{1,2})?$", description="Decimal string, e.g. '447.19'.")
]
Currency = Annotated[str, Field(pattern=r"^[A-Z]{3}$", description="ISO 4217 code, e.g. 'EUR'.")]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Evidence(_Model):
    quote: str = Field(min_length=1, description="Verbatim quote from the letter text.")
    page: int = Field(ge=1, description="1-based page number of the quote.")


# --- Model output -----------------------------------------------------------------------------

DocumentType = Literal[
    "invoice",
    "payment_reminder",
    "dunning_notice",
    "debt_collection",
    "tax_assessment",
    "official_notice",
    "court_document",
    "contract",
    "contract_change",
    "premium_change",
    "policy",
    "statement",
    "receipt",
    "payment_confirmation",
    "direct_debit_confirmation",
    "appointment",
    "information",
    "marketing",
    "other",
]

SenderType = Literal[
    "tax_office",
    "court",
    "government",
    "health_insurer",
    "insurer",
    "bank",
    "utility",
    "telecom",
    "medical_provider",
    "employer",
    "tax_advisor",
    "lawyer",
    "notary",
    "debt_collector",
    "landlord",
    "retailer",
    "school",
    "other",
]


class Document(_Model):
    language: str = Field(description="ISO 639-1 code, or 'unknown'.")
    country: str = Field(
        description="ISO 3166-1 alpha-2 code of the sender's country, or 'unknown'."
    )
    date: dt.date | None = Field(description="Date printed on the letter.")
    type: DocumentType
    subject: str
    summary: str = Field(description="2-3 sentence neutral summary.")
    enclosures: list[str] = []


class Identifier(_Model):
    kind: Literal[
        "tax_number",
        "customer_number",
        "contract_number",
        "policy_number",
        "case_reference",
        "invoice_number",
        "vat_id",
        "other",
    ]
    value: str
    evidence: Evidence


class Sender(_Model):
    name: str
    sender_type: SenderType
    address: str | None = None
    identifiers: list[Identifier] = []


class Recipients(_Model):
    addressed_to: list[str] = []
    concerning_persons: list[str] = []
    known_person_match: list[str] = Field(
        default=[], description="Names from the optional recipient context that match this letter."
    )


class MoneyItem(_Model):
    role: Literal[
        "amount_due",
        "already_paid",
        "refund",
        "credit",
        "new_premium",
        "old_premium",
        "total",
        "fee",
        "interest",
        "other",
    ]
    amount: Amount
    currency: Currency
    evidence: Evidence


class Payment(_Model):
    direction: Literal["recipient_pays", "recipient_receives", "none", "unknown"]
    method_stated: Literal[
        "transfer_requested",
        "direct_debit_active",
        "direct_debit_offered",
        "card_charged",
        "already_paid",
        "offset",
        "none",
        "unknown",
    ]
    amount: Amount | None = None
    currency: Currency | None = None
    due_date: dt.date | None = None
    payee_name: str | None = None
    payee_iban: str | None = None
    payee_bic: str | None = None
    reference: str | None = None
    girocode_present: bool = False
    evidence: list[Evidence] = []


class RequestedAction(_Model):
    action: Literal[
        "pay",
        "reply",
        "sign_and_return",
        "send_documents",
        "provide_information",
        "attend_appointment",
        "call",
        "renew",
        "cancel",
        "object_or_appeal",
        "register",
        "other",
    ]
    obligation: Literal["required", "optional"]
    deadline: dt.date | None = None
    deadline_text: str | None = Field(
        default=None, description="Deadline as written in the letter."
    )
    consequence_if_missed: str | None = None
    description: str
    evidence: list[Evidence] = Field(min_length=1)


class Deadline(_Model):
    kind: Literal[
        "payment_due",
        "reply_by",
        "objection_period_end",
        "effective_date",
        "appointment",
        "contract_end",
        "other",
    ]
    date: dt.date
    evidence: Evidence


class PreviousReference(_Model):
    kind: Literal["invoice_number", "letter_date", "case_reference", "other"]
    value: str


EscalationLevel = Literal[
    "none", "reminder", "dunning_1", "dunning_2_plus", "debt_collection", "legal", "unknown"
]


class LetterContent(_Model):
    """Everything the model reads from one letter. No judgement about what to do."""

    document: Document
    sender: Sender
    recipients: Recipients
    money: list[MoneyItem] = []
    payment: Payment
    requested_actions: list[RequestedAction] = []
    deadlines: list[Deadline] = []
    escalation_level: EscalationLevel
    references_previous: list[PreviousReference] = []
    uncertainties: list[str] = []


# --- Set by code, never by the model ---------------------------------------------------------


class Analyzer(_Model):
    model: str
    prompt_version: str
    tool_version: str
    analyzed_at: dt.datetime


class Source(_Model):
    paperless_document_id: int
    paperless_url: str | None = None
    source_checksum: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    channel: str | None = Field(default=None, description="Free label from configuration.")
    scan_job_id: str | None = None
    received_at: dt.datetime | None = None
    pages: int = Field(ge=1)


class InputInfo(_Model):
    pages_sent: int = Field(ge=0)
    images_sent: bool
    truncated: bool
    noise_pages_dropped: list[int] = []


class ValidationIssue(_Model):
    field: str = Field(description="Dotted path, e.g. 'payment.payee_iban'.")
    problem: str
    severity: Literal["warning", "error"]


class Validation(_Model):
    status: Literal["passed", "passed_with_warnings", "failed"]
    issues: list[ValidationIssue] = []


class LetterAnalysis(LetterContent):
    """The stored analysis record (schema v1)."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    analyzer: Analyzer
    source: Source
    input: InputInfo
    validation: Validation
