"""The analysis pipeline for one document."""

from __future__ import annotations

import datetime as dt
import json
import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from paperless_bedrock import __version__, checks
from paperless_bedrock.config import Settings
from paperless_bedrock.model import Analyzer, ModelRequest, ModelResult
from paperless_bedrock.paperless import DocumentInfo, PaperlessClient, PaperlessError
from paperless_bedrock.pdf import page_texts, render_pages, stamp_xmp
from paperless_bedrock.prepare import PreparedInput, prepare
from paperless_bedrock.prompt import (
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    letter_text,
    recipient_context_text,
    retry_text,
)
from paperless_bedrock.schema import (
    SCHEMA_VERSION,
    InputInfo,
    LetterAnalysis,
    LetterContent,
    Source,
    Validation,
    ValidationIssue,
)
from paperless_bedrock.schema import Analyzer as AnalyzerInfo

log = logging.getLogger(__name__)

TITLE_MAX = 128
STRING_FIELD_MAX = 128


@dataclass
class Outcome:
    status: Literal["analyzed", "skipped"]
    analysis: LetterAnalysis | None = None


def existing_analysis(notes: list[str], checksum: str) -> bool:
    for note in notes:
        try:
            data = json.loads(note)
        except ValueError:
            continue
        if (
            isinstance(data, dict)
            and data.get("schema_version") == SCHEMA_VERSION
            and (data.get("source") or {}).get("source_checksum") == checksum
        ):
            return True
    return False


def title_for(analysis: LetterAnalysis) -> str:
    title = f"{analysis.sender.name} – {analysis.document.subject}".strip(" –")
    return title if len(title) <= TITLE_MAX else title[: TITLE_MAX - 1].rstrip() + "…"


def field_values(analysis: LetterAnalysis) -> dict[str, Any]:
    """Values for the custom fields, by CustomFieldNames attribute. None clears the field."""
    payment = analysis.payment
    required = [a for a in analysis.requested_actions if a.obligation == "required"]
    reply_deadlines = sorted(a.deadline for a in required if a.deadline and a.action != "pay")
    pays = payment.direction == "recipient_pays"
    return {
        "payment_needed": any(a.action == "pay" for a in required),
        "amount": (Decimal(payment.amount), payment.currency)
        if pays and payment.amount and payment.currency
        else None,
        "due_date": payment.due_date if pays else None,
        "payee_iban": payment.payee_iban if pays else None,
        "payment_reference": payment.reference if pays else None,
        "reply_deadline": reply_deadlines[0] if reply_deadlines else None,
        "country": None if analysis.document.country == "unknown" else analysis.document.country,
    }


def convert(value: Any, data_type: str) -> Any:
    """Convert a value to the representation paperless expects for the field's data type."""
    if value is None:
        return None
    if isinstance(value, tuple):  # (amount, currency)
        amount, currency = value
        if data_type == "monetary":
            return f"{currency}{amount:.2f}"
        if data_type == "float":
            return float(amount)
        return f"{amount:.2f} {currency}"[:STRING_FIELD_MAX]
    if isinstance(value, bool):
        return value if data_type == "boolean" else ("yes" if value else "no")
    if isinstance(value, dt.date):
        return value.isoformat()
    return str(value)[:STRING_FIELD_MAX]


class Pipeline:
    def __init__(self, settings: Settings, paperless: PaperlessClient, analyzer: Analyzer) -> None:
        self.settings = settings
        self.paperless = paperless
        self.analyzer = analyzer
        self.context = settings.recipient_context()

    def run(self, document_id: int, *, force: bool = False) -> Outcome:
        doc = self.paperless.get_document(document_id)
        checksum = f"sha256:{doc.checksum}"
        if not force and existing_analysis(self.paperless.list_notes(doc.id), checksum):
            log.info("document %s: analysis for %s exists, skipping", doc.id, checksum)
            return Outcome(status="skipped")

        base_pdf = self._base_pdf(doc)
        analysis = self._analyze(doc, base_pdf, checksum)
        self._write_back(doc, base_pdf, analysis)
        log.info(
            "document %s: analyzed (%s, %d issues)",
            doc.id,
            analysis.validation.status,
            len(analysis.validation.issues),
        )
        return Outcome(status="analyzed", analysis=analysis)

    def mark_failed(self, document_id: int, error: str) -> None:
        """Make a permanent failure visible in paperless."""
        try:
            doc = self.paperless.get_document(document_id)
            tags = self._tags_with_failed(doc)
            if tags is not None:
                self.paperless.update_document(doc.id, {"tags": tags})
        except Exception:
            log.exception("document %s: could not tag failure", document_id)
        log.error("document %s: analysis failed permanently: %s", document_id, error)

    # --- steps -------------------------------------------------------------------------

    def _base_pdf(self, doc: DocumentInfo) -> bytes:
        """The root version as PDF with a text layer: the archive file if any, else the original."""
        if doc.has_archive:
            return self.paperless.download(doc.id, original=False)
        if doc.mime_type != "application/pdf":
            raise PaperlessError(
                f"document {doc.id}: no archive PDF and original is {doc.mime_type}"
            )
        return self.paperless.download(doc.id, original=True)

    def _analyze(self, doc: DocumentInfo, pdf: bytes, checksum: str) -> LetterAnalysis:
        s = self.settings
        all_pages = page_texts(pdf)
        prepared = prepare(all_pages, s.noise_profiles, s.max_pages)
        image_pages = [p.number for p in prepared.pages][: s.max_image_pages]
        images = render_pages(pdf, image_pages, s.image_dpi)

        system_prompt = SYSTEM_PROMPT
        context = recipient_context_text(self.context)
        if context:
            system_prompt += "\n" + context
        request = ModelRequest(
            system_prompt=system_prompt,
            text=letter_text([(p.number, p.text) for p in prepared.pages]),
            images=images,
        )

        started = time.monotonic()
        result = self.analyzer.analyze(request)
        issues = checks.check(result.content, prepared.text, self.context.own_ibans)
        usage = dict(result.usage)
        if issues:
            log.info("document %s: %d issues, retrying once", doc.id, len(issues))
            request.feedback = retry_text([f"{i.field}: {i.problem}" for i in issues])
            second = self.analyzer.analyze(request)
            second_issues = checks.check(second.content, prepared.text, self.context.own_ibans)
            for key, value in second.usage.items():
                usage[key] = usage.get(key, 0) + value
            if _score(second_issues) <= _score(issues):
                result, issues = second, second_issues
        log.info(
            "document %s: model %s, %.1fs, usage %s",
            doc.id,
            self.analyzer.model_id,
            time.monotonic() - started,
            usage,
        )
        return self._record(doc, checksum, result, issues, prepared, len(all_pages), images)

    def _record(
        self,
        doc: DocumentInfo,
        checksum: str,
        result: ModelResult,
        issues: list[ValidationIssue],
        prepared: PreparedInput,
        page_count: int,
        images: dict[int, bytes],
    ) -> LetterAnalysis:
        content: LetterContent = result.content
        return LetterAnalysis(
            **content.model_dump(),
            analyzer=AnalyzerInfo(
                model=self.analyzer.model_id,
                prompt_version=PROMPT_VERSION,
                tool_version=__version__,
                analyzed_at=dt.datetime.now(dt.UTC),
            ),
            source=Source(
                paperless_document_id=doc.id,
                paperless_url=f"{self.settings.link_base}/documents/{doc.id}/details",
                source_checksum=checksum,
                scan_job_id=prepared.scan_job_id,
                received_at=doc.added,
                pages=max(page_count, 1),
            ),
            input=InputInfo(
                pages_sent=len(prepared.pages),
                images_sent=bool(images),
                truncated=prepared.truncated,
                noise_pages_dropped=prepared.dropped_pages,
                page_text_quality=prepared.quality,
            ),
            validation=Validation(status=checks.status(issues), issues=issues),
        )

    def _write_back(self, doc: DocumentInfo, base_pdf: bytes, analysis: LetterAnalysis) -> None:
        s = self.settings
        title = title_for(analysis)
        update: dict[str, Any] = {}
        if s.set_title:
            update["title"] = title
        custom_fields = self._custom_fields(doc, analysis)
        if custom_fields is not None:
            update["custom_fields"] = custom_fields
        if s.remove_inbox_tags:
            update["remove_inbox_tags"] = True
        if analysis.validation.status == "failed":
            tags = self._tags_with_failed(doc)
            if tags is not None:
                update["tags"] = tags
        if update:
            self.paperless.update_document(doc.id, update)

        if s.write_version:
            stamped = stamp_xmp(base_pdf, analysis, title)
            task = self.paperless.upload_version(
                doc.id, f"{doc.id}-{s.version_label}.pdf", stamped, s.version_label
            )
            log.info("document %s: version upload queued (task %s)", doc.id, task)

        # The note is written last: it is the marker that the analysis is complete.
        self.paperless.add_note(doc.id, analysis.model_dump_json(indent=2))

    def _custom_fields(
        self, doc: DocumentInfo, analysis: LetterAnalysis
    ) -> list[dict[str, Any]] | None:
        names = self.settings.custom_fields.model_dump()
        found = self.paperless.custom_field_types(list(names.values()))
        if not found:
            return None
        values = field_values(analysis)
        ours: dict[int, Any] = {}
        for key, name in names.items():
            if name in found:
                field_id, data_type = found[name]
                ours[field_id] = convert(values[key], data_type)
            else:
                log.debug("custom field %r not found in paperless, skipped", name)
        # PATCH replaces the whole list: keep fields that are not ours.
        merged = [f for f in doc.custom_fields if f.get("field") not in ours]
        merged += [{"field": fid, "value": value} for fid, value in ours.items()]
        return merged

    def _tags_with_failed(self, doc: DocumentInfo) -> list[int] | None:
        """The document's tags plus the failure tag, or None if nothing changes."""
        tag_id = self.paperless.find_id("tags", self.settings.failed_tag)
        if tag_id is None:
            log.warning("tag %r does not exist in paperless; create it", self.settings.failed_tag)
            return None
        return None if tag_id in doc.tags else [*doc.tags, tag_id]


def _score(issues: list[ValidationIssue]) -> int:
    return sum(10 if i.severity == "error" else 1 for i in issues)
