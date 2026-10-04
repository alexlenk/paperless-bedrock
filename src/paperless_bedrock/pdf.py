"""PDF reading (page text, page images) and XMP stamping."""

from __future__ import annotations

import io
import json
from dataclasses import dataclass
from typing import Any

import pikepdf
import pypdfium2 as pdfium
from pikepdf.models.metadata import PdfMetadata

from paperless_bedrock.schema import LetterAnalysis

XMP_NAMESPACE = "https://github.com/alexlenk/paperless-bedrock/ns/letter/1.0/"
XMP_PREFIX = "letter"

PdfMetadata.register_xml_namespace(XMP_NAMESPACE, XMP_PREFIX)


@dataclass(frozen=True)
class Page:
    number: int  # 1-based
    text: str


def page_texts(pdf: bytes) -> list[Page]:
    doc = pdfium.PdfDocument(pdf)
    try:
        pages = []
        for i in range(len(doc)):
            textpage = doc[i].get_textpage()
            pages.append(Page(number=i + 1, text=textpage.get_text_bounded()))
        return pages
    finally:
        doc.close()


def render_pages(pdf: bytes, numbers: list[int], dpi: int) -> dict[int, bytes]:
    """Render the given 1-based pages as JPEG images."""
    doc = pdfium.PdfDocument(pdf)
    try:
        images = {}
        for n in numbers:
            bitmap = doc[n - 1].render(scale=dpi / 72, grayscale=True)
            buf = io.BytesIO()
            bitmap.to_pil().save(buf, format="JPEG", quality=85)
            images[n] = buf.getvalue()
        return images
    finally:
        doc.close()


# --- XMP ----------------------------------------------------------------------------------

_FLAT_PROPERTIES = {
    "schemaVersion": "Analysis schema version",
    "amountDue": "Amount the recipient is asked to pay (decimal string)",
    "currency": "ISO 4217 currency of amountDue",
    "dueDate": "Payment due date (ISO 8601)",
    "payeeIBAN": "IBAN to pay to",
    "paymentDirection": "recipient_pays | recipient_receives | none | unknown",
    "paymentMethod": "Payment method stated in the letter",
    "actionRequired": "True if the letter requires an action",
    "nextDeadline": "Earliest deadline of a required action (ISO 8601)",
    "escalationLevel": "Escalation level (reminder, dunning, ...)",
    "analysisJSON": "Complete analysis as JSON (paperless-bedrock schema)",
}


def _extension_schema() -> str:
    """PDF/A extension schema declaration for the letter namespace (ISO 19005-1, 6.6.2.3)."""
    props = "".join(
        f'<rdf:li rdf:parseType="Resource"><pdfaProperty:name>{name}</pdfaProperty:name>'
        "<pdfaProperty:valueType>Text</pdfaProperty:valueType>"
        "<pdfaProperty:category>external</pdfaProperty:category>"
        f"<pdfaProperty:description>{desc}</pdfaProperty:description></rdf:li>"
        for name, desc in _FLAT_PROPERTIES.items()
    )
    return (
        '<rdf:Description rdf:about="" '
        'xmlns:pdfaExtension="http://www.aiim.org/pdfa/ns/extension/" '
        'xmlns:pdfaSchema="http://www.aiim.org/pdfa/ns/schema#" '
        'xmlns:pdfaProperty="http://www.aiim.org/pdfa/ns/property#">'
        '<pdfaExtension:schemas><rdf:Bag><rdf:li rdf:parseType="Resource">'
        "<pdfaSchema:schema>paperless-bedrock letter analysis</pdfaSchema:schema>"
        f"<pdfaSchema:namespaceURI>{XMP_NAMESPACE}</pdfaSchema:namespaceURI>"
        f"<pdfaSchema:prefix>{XMP_PREFIX}</pdfaSchema:prefix>"
        f"<pdfaSchema:property><rdf:Seq>{props}</rdf:Seq></pdfaSchema:property>"
        "</rdf:li></rdf:Bag></pdfaExtension:schemas></rdf:Description>"
    )


def flat_fields(analysis: LetterAnalysis) -> dict[str, str]:
    payment = analysis.payment
    required = [a for a in analysis.requested_actions if a.obligation == "required"]
    deadlines = sorted(a.deadline for a in required if a.deadline)
    fields = {
        "schemaVersion": analysis.schema_version,
        "paymentDirection": payment.direction,
        "paymentMethod": payment.method_stated,
        "actionRequired": "True" if required else "False",
        "escalationLevel": analysis.escalation_level,
    }
    if payment.direction == "recipient_pays" and payment.amount:
        fields["amountDue"] = payment.amount
        if payment.currency:
            fields["currency"] = payment.currency
    if payment.due_date:
        fields["dueDate"] = payment.due_date.isoformat()
    if payment.payee_iban:
        fields["payeeIBAN"] = payment.payee_iban
    if deadlines:
        fields["nextDeadline"] = deadlines[0].isoformat()
    return fields


def keywords(analysis: LetterAnalysis) -> list[str]:
    words: list[str] = [analysis.document.type, analysis.sender.sender_type]
    words += analysis.recipients.known_person_match
    if any(a.action == "pay" and a.obligation == "required" for a in analysis.requested_actions):
        words.append("payment-required")
    if analysis.payment.due_date:
        words.append(f"due-{analysis.payment.due_date.isoformat()}")
    return words


def stamp_xmp(pdf: bytes, analysis: LetterAnalysis, title: str) -> bytes:
    """Return the PDF with Dublin Core and letter:* XMP metadata (PDF/A extension declared)."""
    with pikepdf.open(io.BytesIO(pdf)) as doc:
        with doc.open_metadata(set_pikepdf_as_editor=False) as meta:
            meta["dc:title"] = title
            meta["dc:creator"] = [analysis.sender.name]
            meta["dc:description"] = analysis.document.summary
            meta["dc:subject"] = set(keywords(analysis))
            meta["dc:identifier"] = str(analysis.source.paperless_document_id)
            if analysis.document.date:
                meta["dc:date"] = [analysis.document.date.isoformat()]
            if analysis.document.language != "unknown":
                meta["dc:language"] = {analysis.document.language}
            for key, value in flat_fields(analysis).items():
                meta[f"{{{XMP_NAMESPACE}}}{key}"] = value
            meta[f"{{{XMP_NAMESPACE}}}analysisJSON"] = analysis.model_dump_json()
        xml = doc.Root.Metadata.read_bytes().decode("utf-8")
        if f"<pdfaSchema:namespaceURI>{XMP_NAMESPACE}<" not in xml:
            xml = xml.replace("</rdf:RDF>", _extension_schema() + "</rdf:RDF>", 1)
            doc.Root.Metadata = doc.make_stream(xml.encode("utf-8"))
            doc.Root.Metadata.Type = pikepdf.Name.Metadata
            doc.Root.Metadata.Subtype = pikepdf.Name.XML
        out = io.BytesIO()
        doc.save(out)
        return out.getvalue()


def read_xmp_analysis(pdf: bytes) -> dict[str, Any] | None:
    """Read the analysis JSON back from a stamped PDF."""
    with pikepdf.open(io.BytesIO(pdf)) as doc:
        meta = doc.open_metadata()
        raw = meta.get(f"{{{XMP_NAMESPACE}}}analysisJSON")
        return json.loads(raw) if raw else None
