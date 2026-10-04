"""Reference numbers in letter text: extraction and normalisation (no model involved)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from paperless_bedrock.checks import iban_valid

# Identifiers that belong to the sender itself and identify it. Only these may decide which
# correspondent a letter is from. Everything else (tax numbers, customer and contract numbers,
# IBANs) can be the recipient's or belong to a payment provider and is only used as a hint.
IDENTITY_KINDS = frozenset({"vat_id", "creditor_id", "commercial_register"})


@dataclass(frozen=True)
class Reference:
    kind: str
    value: str  # normalised: upper case, letters and digits only

    @property
    def identity(self) -> bool:
        return self.kind in IDENTITY_KINDS


def normalize(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", value).upper()


_LABEL = r"(?:\s*(?:[:.#]|nr\.?|no\.?|nummer|number))*\s*[:.#]?\s*"
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("iban", re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){3,7}(?: ?[A-Z0-9]{1,4})?\b")),
    ("creditor_id", re.compile(r"\b[A-Z]{2}\d{2} ?[A-Z0-9]{3} ?\d{8,11}\b")),
    ("vat_id", re.compile(r"\b(?:DE ?\d{9}|ATU ?\d{8}|[A-Z]{2} ?\d{8,12})\b")),
    ("commercial_register", re.compile(r"\bHR[AB] ?\d{2,6} ?[A-Z]?\b")),
    ("tax_number", re.compile(r"\b\d{2,3}/\d{3,4}/\d{4,5}\b|\b\d{5}/\d{5}\b")),
    ("ein", re.compile(r"\b\d{2}-\d{7}\b")),
    (
        "customer_number",
        re.compile(
            r"(?i)\b(?:kunden|customer|mitglieds|member|account|konto)"
            r"(?:-|\s)?(?:nummer|nr\.?|number|no\.?|id)" + _LABEL + r"([A-Z0-9][A-Z0-9\-/ ]{3,24})"
        ),
    ),
    (
        "contract_number",
        re.compile(
            r"(?i)\b(?:vertrags|versicherungs|policy|contract|police)"
            r"(?:-|\s)?(?:nummer|nr\.?|number|no\.?|schein-?nr\.?)"
            + _LABEL
            + r"([A-Z0-9][A-Z0-9\-/ ]{3,24})"
        ),
    ),
    (
        "case_reference",
        re.compile(
            r"(?i)\b(?:aktenzeichen|az\.|geschäftszeichen|case number)"
            + _LABEL
            + r"([A-Z0-9][A-Z0-9\-/ ]{3,24})"
        ),
    ),
    (
        "invoice_number",
        re.compile(
            r"(?i)\b(?:rechnungs|invoice)(?:-|\s)?(?:nummer|nr\.?|number|no\.?)"
            + _LABEL
            + r"([A-Z0-9][A-Z0-9\-/]{3,24})"
        ),
    ),
]

_CREDITOR_CONTEXT = re.compile(r"(?i)gl[äa]ubiger|creditor")
_VAT_CONTEXT = re.compile(r"(?i)ust|umsatzsteuer|vat|mwst")


def extract(text: str) -> list[Reference]:
    """Reference numbers found in the text, deduplicated, in order of appearance."""
    found: dict[tuple[str, str], Reference] = {}

    def add(kind: str, raw: str) -> None:
        value = normalize(raw)
        if len(value) >= 4:
            found.setdefault((kind, value), Reference(kind, value))

    for line in text.splitlines():
        for kind, pattern in _PATTERNS:
            for match in pattern.finditer(line):
                raw = match.group(match.lastindex or 0)
                value = normalize(raw)
                if kind == "iban" and not iban_valid(value):
                    continue
                if kind == "creditor_id" and (
                    "ZZZ" not in value and not _CREDITOR_CONTEXT.search(line)
                ):
                    continue
                if kind == "vat_id" and not (
                    value.startswith(("DE", "ATU")) or _VAT_CONTEXT.search(line)
                ):
                    continue
                if kind == "vat_id" and iban_valid(value):
                    continue
                add(kind, raw)
    return list(found.values())


def from_sender_identifiers(identifiers: list[tuple[str, str]]) -> list[Reference]:
    """References from the model's `sender.identifiers` (kind, value)."""
    return [Reference(kind, normalize(value)) for kind, value in identifiers if normalize(value)]
