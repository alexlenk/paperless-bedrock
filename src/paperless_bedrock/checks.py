"""Deterministic checks of a model analysis against the letter text."""

from __future__ import annotations

import re
import unicodedata
from contextlib import suppress
from decimal import Decimal, InvalidOperation

from rapidfuzz import fuzz

from paperless_bedrock.schema import Evidence, LetterContent, ValidationIssue

QUOTE_MIN_SCORE = 90.0  # fuzzy match: OCR text and what the model read from the image differ


_QUOTES = str.maketrans("„“”‚‘’", "\"\"\"'''")


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"-\s*\n\s*", "", text)  # hyphenation at line breaks
    text = text.translate(_QUOTES)
    text = text.replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", text).strip().casefold()


def iban_valid(iban: str) -> bool:
    """ISO 13616 mod-97 check."""
    s = re.sub(r"\s+", "", iban).upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}", s):
        return False
    digits = "".join(str(int(c, 36)) for c in s[4:] + s[:4])
    return int(digits) % 97 == 1


_NUMBER = re.compile(r"\d(?:[\d.,']*\d)?")


def amounts_in_text(text: str) -> set[Decimal]:
    """All amounts in the text, read in both German (1.234,56) and English (1,234.56) style."""
    found: set[Decimal] = set()
    for token in _NUMBER.findall(text):
        token = token.replace("'", "")
        candidates = {token}
        if "," in token or "." in token:
            candidates.add(token.replace(".", "").replace(",", "."))  # German
            candidates.add(token.replace(",", ""))  # English
        for c in candidates:
            with suppress(InvalidOperation):
                found.add(Decimal(c))
    return found


def _quote_ok(evidence: Evidence, normalized_text: str) -> bool:
    quote = normalize(evidence.quote)
    if quote in normalized_text:
        return True
    return fuzz.partial_ratio(quote, normalized_text) >= QUOTE_MIN_SCORE


def _evidence(content: LetterContent) -> list[tuple[str, Evidence]]:
    items: list[tuple[str, Evidence]] = []
    items += [
        (f"sender.identifiers[{i}]", x.evidence) for i, x in enumerate(content.sender.identifiers)
    ]
    items += [(f"money[{i}]", x.evidence) for i, x in enumerate(content.money)]
    items += [(f"payment.evidence[{i}]", e) for i, e in enumerate(content.payment.evidence)]
    for i, action in enumerate(content.requested_actions):
        items += [
            (f"requested_actions[{i}].evidence[{j}]", e) for j, e in enumerate(action.evidence)
        ]
    items += [(f"deadlines[{i}]", x.evidence) for i, x in enumerate(content.deadlines)]
    return items


def check(
    content: LetterContent, text: str, own_ibans: list[str] | None = None
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    normalized_text = normalize(text)

    def issue(field: str, problem: str, severity: str = "warning") -> None:
        issues.append(ValidationIssue(field=field, problem=problem, severity=severity))

    # Evidence quotes
    for field, ev in _evidence(content):
        if not _quote_ok(ev, normalized_text):
            issue(field, f"quote not found in letter text: {ev.quote[:80]!r}")

    # Amounts
    amounts = amounts_in_text(text)
    for i, m in enumerate(content.money):
        if Decimal(m.amount).copy_abs() not in amounts:
            issue(f"money[{i}].amount", f"amount {m.amount} not found in letter text")
    payment = content.payment
    if payment.amount and Decimal(payment.amount).copy_abs() not in amounts:
        issue("payment.amount", f"amount {payment.amount} not found in letter text")

    # IBAN
    if payment.payee_iban:
        if not iban_valid(payment.payee_iban):
            issue("payment.payee_iban", "IBAN checksum invalid", "error")
        own = {re.sub(r"\s+", "", i).upper() for i in own_ibans or []}
        if re.sub(r"\s+", "", payment.payee_iban).upper() in own:
            issue("payment.payee_iban", "payee IBAN is one of the recipients' own IBANs", "error")

    # Dates
    doc_date = content.document.date
    if doc_date:
        if payment.due_date and payment.due_date < doc_date:
            issue("payment.due_date", "due date is before the document date")
        for i, a in enumerate(content.requested_actions):
            if a.deadline and a.deadline < doc_date:
                issue(f"requested_actions[{i}].deadline", "deadline is before the document date")

    # Consistency
    has_amount_due = any(m.role == "amount_due" for m in content.money)
    if payment.direction == "none" and has_amount_due:
        issue("payment.direction", "direction is 'none' but an amount_due is reported")
    if payment.direction == "recipient_pays" and not payment.amount:
        issue("payment.amount", "recipient_pays without an amount")
    if payment.method_stated == "transfer_requested" and not (
        payment.payee_iban or payment.reference
    ):
        issue("payment", "transfer requested but neither payee IBAN nor reference given")
    if (payment.amount is None) != (payment.currency is None):
        issue("payment.currency", "amount and currency must be given together", "error")
    return issues


def status(issues: list[ValidationIssue]) -> str:
    if any(i.severity == "error" for i in issues):
        return "failed"
    return "passed_with_warnings" if issues else "passed"
