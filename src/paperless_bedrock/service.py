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
from paperless_bedrock.identifiers import IDENTITY_KINDS, Reference, extract, normalize
from paperless_bedrock.index import KnowledgeIndex, norm_name
from paperless_bedrock.model import Analyzer, Judge, ModelRequest, ModelResult
from paperless_bedrock.paperless import DocumentInfo, PaperlessClient, PaperlessError
from paperless_bedrock.pdf import page_texts, render_pages, stamp_xmp
from paperless_bedrock.prepare import PreparedInput, prepare
from paperless_bedrock.prompt import (
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    catalog_text,
    context_text,
    letter_text,
    related_text,
    retry_text,
)
from paperless_bedrock.resolve import Resolver
from paperless_bedrock.schema import (
    SCHEMA_VERSION,
    InputInfo,
    LetterAnalysis,
    LetterContent,
    PaperlessAssignment,
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


def latest_analysis(notes: list[str]) -> LetterAnalysis | None:
    """The newest valid analysis among a document's notes (notes are returned oldest first)."""
    for note in reversed(notes):
        try:
            return LetterAnalysis.model_validate_json(note)
        except ValueError:
            continue
    return None


def title_for(analysis: LetterAnalysis) -> str:
    title = f"{analysis.sender.name} – {analysis.document.subject}".strip(" –")
    return title if len(title) <= TITLE_MAX else title[: TITLE_MAX - 1].rstrip() + "…"


def field_values(analysis: LetterAnalysis) -> dict[str, Any]:
    """Values for the custom fields, by CustomFieldNames attribute. None clears the field."""
    payment = analysis.payment
    required = [a for a in analysis.requested_actions if a.obligation == "required"]
    reply_deadlines = sorted(a.deadline for a in required if a.deadline and a.action != "pay")
    pays = payment.direction == "recipient_pays"
    tax = analysis.tax
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
        "tax_year": tax.year if tax and tax.relevance != "no" else None,
        "tax_categories": "; ".join(tax.categories) if tax and tax.relevance != "no" else None,
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
    if isinstance(value, int):
        return value if data_type in ("integer", "float") else str(value)
    if isinstance(value, dt.date):
        return value.isoformat()
    return str(value)[:STRING_FIELD_MAX]


class Pipeline:
    def __init__(
        self,
        settings: Settings,
        paperless: PaperlessClient,
        analyzer: Analyzer,
        judge: Judge,
        index: KnowledgeIndex | None = None,
    ) -> None:
        self.settings = settings
        self.paperless = paperless
        self.analyzer = analyzer
        self.context = settings.context()
        self.index = index or KnowledgeIndex(settings.data_dir / "knowledge.sqlite3")
        self.resolver = Resolver(paperless, self.index, judge)
        self._scope_ids = [scope.id for scope in self.context.tax.scopes]
        self._own_refs = {
            normalize(v) for v in [*self.context.own_ibans, *self.context.own_identifiers]
        }

    @property
    def _classify(self) -> bool:
        return self.settings.assign_correspondent or self.settings.assign_document_type

    def run(self, document_id: int, *, force: bool = False, light: bool = False) -> Outcome:
        """Analyse one document. `light`: no PDF version (bulk imports of older documents)."""
        doc = self.paperless.get_document(document_id)
        checksum = f"sha256:{doc.checksum}"
        if not force and existing_analysis(self.paperless.list_notes(doc.id), checksum):
            log.info("document %s: analysis for %s exists, skipping", doc.id, checksum)
            return Outcome(status="skipped")

        base_pdf = self._base_pdf(doc)
        analysis, refs = self._analyze(doc, base_pdf, checksum)
        analysis.paperless = self._assign(analysis)
        self._write_back(doc, base_pdf, analysis, light=light)
        self._index_document(doc, analysis, refs)
        log.info(
            "document %s: analyzed (%s, %d issues)",
            doc.id,
            analysis.validation.status,
            len(analysis.validation.issues),
        )
        return Outcome(status="analyzed", analysis=analysis)

    def reindex(self) -> int:
        """Rebuild the knowledge index from the analysis notes; paperless assignments win."""
        self.index.clear_documents()
        count = 0
        for row in self.paperless.document_fields(["id", "correspondent", "document_type"]):
            analysis = latest_analysis(self.paperless.list_notes(row["id"]))
            if analysis is None:
                continue
            refs: list[Reference] = []
            for ident in analysis.sender.identifiers:
                value = normalize(ident.value)
                if value and value not in self._own_refs:
                    refs.append(Reference(ident.kind, value))
            refs += [
                Reference("reference", normalize(r.value))
                for r in analysis.references_previous
                if normalize(r.value)
            ]
            self._record_index(
                row["id"], analysis, refs, row.get("correspondent"), row.get("document_type")
            )
            count += 1
        return count

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

    def _analyze(
        self, doc: DocumentInfo, pdf: bytes, checksum: str
    ) -> tuple[LetterAnalysis, list[Reference]]:
        s = self.settings
        all_pages = page_texts(pdf)
        prepared = prepare(all_pages, s.noise_profiles, s.max_pages)
        image_pages = [p.number for p in prepared.pages][: s.max_image_pages]
        images = render_pages(pdf, image_pages, s.image_dpi)

        system_prompt = SYSTEM_PROMPT + "\n" + context_text(self.context)
        if self._classify:
            system_prompt += "\n" + self._catalog_prompt()
        refs = [r for r in extract(prepared.text) if r.value not in self._own_refs]
        related = self.index.related_documents(refs, exclude=doc.id)
        text = letter_text([(p.number, p.text) for p in prepared.pages])
        if related:
            text += "\n\n" + related_text(related)
        request = ModelRequest(system_prompt=system_prompt, text=text, images=images)

        started = time.monotonic()
        result = self.analyzer.analyze(request)
        issues = checks.check(
            result.content, prepared.text, self.context.own_ibans, self._scope_ids
        )
        usage = dict(result.usage)
        if issues:
            log.info("document %s: %d issues, retrying once", doc.id, len(issues))
            request.feedback = retry_text([f"{i.field}: {i.problem}" for i in issues])
            second = self.analyzer.analyze(request)
            second_issues = checks.check(
                second.content, prepared.text, self.context.own_ibans, self._scope_ids
            )
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
        analysis = self._record(doc, checksum, result, issues, prepared, len(all_pages), images)
        return analysis, refs

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

    def _catalog_prompt(self) -> str:
        aliases = self.index.aliases("correspondent")
        correspondents = [
            (o.name, aliases.get(o.id, [])) for o in self.resolver.catalog("correspondent")
        ]
        types = [o.name for o in self.resolver.catalog("document_type")]
        return catalog_text(correspondents, types)

    def _sender_identity_refs(self, analysis: LetterAnalysis) -> list[Reference]:
        return [
            Reference(i.kind, normalize(i.value))
            for i in analysis.sender.identifiers
            if i.kind in IDENTITY_KINDS
            and normalize(i.value)
            and normalize(i.value) not in self._own_refs
        ]

    def _assign(self, analysis: LetterAnalysis) -> PaperlessAssignment:
        assignment = PaperlessAssignment(persons=self._matched_persons(analysis))
        classification = analysis.classification
        if classification is None:
            return assignment
        if self.settings.assign_correspondent and classification.correspondent.strip():
            assignment.correspondent = self.resolver.resolve(
                "correspondent",
                classification.correspondent,
                identity_refs=self._sender_identity_refs(analysis),
                profile={
                    "address": analysis.sender.address,
                    "sender_type": analysis.sender.sender_type,
                    "subject": analysis.document.subject,
                },
            )
        if self.settings.assign_document_type and classification.document_type.strip():
            assignment.document_type = self.resolver.resolve(
                "document_type",
                classification.document_type,
                profile={"example_subject": analysis.document.subject},
            )
        return assignment

    def _matched_persons(self, analysis: LetterAnalysis) -> list[str]:
        matched = {norm_name(n) for n in analysis.recipients.known_person_match}
        return [
            p.name
            for p in self.context.all_persons
            if matched & {norm_name(n) for n in [p.name, *p.aliases]}
        ]

    def _index_document(
        self, doc: DocumentInfo, analysis: LetterAnalysis, refs: list[Reference]
    ) -> None:
        identity = self._sender_identity_refs(analysis)
        # Identity-like numbers found in the text but not confirmed as the sender's are kept as
        # plain references (hints), never as identity.
        hints = [r if not r.identity else Reference("reference", r.value) for r in refs]
        assignment = analysis.paperless or PaperlessAssignment()
        self._record_index(
            doc.id,
            analysis,
            [*identity, *hints],
            assignment.correspondent.id if assignment.correspondent else doc.correspondent,
            assignment.document_type.id if assignment.document_type else doc.document_type,
        )

    def _record_index(
        self,
        document_id: int,
        analysis: LetterAnalysis,
        refs: list[Reference],
        correspondent_id: int | None,
        document_type_id: int | None,
    ) -> None:
        payment = analysis.payment
        self.index.record_document(
            document_id,
            correspondent_id=correspondent_id,
            document_type_id=document_type_id,
            sender_name=analysis.sender.name,
            sender_address=analysis.sender.address,
            subject=analysis.document.subject,
            document_date=analysis.document.date.isoformat() if analysis.document.date else None,
            summary={
                "document_id": document_id,
                "date": analysis.document.date.isoformat() if analysis.document.date else None,
                "sender": analysis.sender.name,
                "type": analysis.document.type,
                "subject": analysis.document.subject,
                "amount": payment.amount,
                "currency": payment.currency,
                "due_date": payment.due_date.isoformat() if payment.due_date else None,
                "escalation_level": analysis.escalation_level,
            },
            refs=refs,
        )

    def _write_back(
        self, doc: DocumentInfo, base_pdf: bytes, analysis: LetterAnalysis, *, light: bool = False
    ) -> None:
        s = self.settings
        title = title_for(analysis)
        update: dict[str, Any] = {}
        if s.set_title:
            update["title"] = title
        assignment = analysis.paperless
        if (
            assignment
            and assignment.correspondent
            and assignment.correspondent.id != doc.correspondent
        ):
            update["correspondent"] = assignment.correspondent.id
        if (
            assignment
            and assignment.document_type
            and assignment.document_type.id != doc.document_type
        ):
            update["document_type"] = assignment.document_type.id
        custom_fields = self._custom_fields(doc, analysis)
        if custom_fields is not None:
            update["custom_fields"] = custom_fields
        if s.remove_inbox_tags:
            update["remove_inbox_tags"] = True
        tags = self._tags(doc, analysis)
        if tags != doc.tags:
            update["tags"] = tags
        if update:
            self.paperless.update_document(doc.id, update)

        if s.write_version and not light:
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

    def _tag_id(self, name: str) -> int | None:
        tag_id = self.paperless.find_id("tags", name)
        if tag_id is None:
            log.warning("tag %r does not exist in paperless; create it", name)
        return tag_id

    def _tags_with_failed(self, doc: DocumentInfo) -> list[int] | None:
        """The document's tags plus the failure tag, or None if nothing changes."""
        tag_id = self._tag_id(self.settings.failed_tag)
        if tag_id is None or tag_id in doc.tags:
            return None
        return [*doc.tags, tag_id]

    def _tags(self, doc: DocumentInfo, analysis: LetterAnalysis) -> list[int]:
        """Document tags after the analysis: tax tags replaced, failure tag added if needed."""
        wanted: list[str] = []
        managed: list[str] = []
        tax_context = self.context.tax
        if tax_context.scopes:
            managed = [scope.tag for scope in tax_context.scopes] + [tax_context.unclear_tag]
            tax = analysis.tax
            if tax and tax.relevance == "yes":
                wanted = [s.tag for s in tax_context.scopes if s.id in tax.scopes]
            elif tax is None or tax.relevance == "unclear":
                wanted = [tax_context.unclear_tag]
        person_tags = {p.name: p.tag for p in self.context.all_persons if p.tag}
        managed += list(person_tags.values())
        assigned = analysis.paperless.persons if analysis.paperless else []
        wanted += [person_tags[name] for name in assigned if name in person_tags]
        if analysis.validation.status == "failed":
            wanted.append(self.settings.failed_tag)
        ids = {name: self._tag_id(name) for name in {*managed, *wanted}}
        managed_ids = {ids[name] for name in managed}
        tags = [t for t in doc.tags if t not in managed_ids]
        for name in wanted:
            tag_id = ids[name]
            if tag_id is not None and tag_id not in tags:
                tags.append(tag_id)
        return tags


def _score(issues: list[ValidationIssue]) -> int:
    return sum(10 if i.severity == "error" else 1 for i in issues)
