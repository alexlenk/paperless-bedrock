import datetime as dt
import io

import pikepdf
from conftest import content_dict

from paperless_bedrock.pdf import (
    XMP_NAMESPACE,
    page_texts,
    read_xmp_analysis,
    render_pages,
    stamp_xmp,
)
from paperless_bedrock.schema import LetterAnalysis, TaxClassification


def analysis() -> LetterAnalysis:
    return LetterAnalysis.model_validate(
        {
            **content_dict(),
            "analyzer": {
                "model": "test",
                "prompt_version": "1",
                "tool_version": "0",
                "analyzed_at": dt.datetime(2026, 10, 3, tzinfo=dt.UTC),
            },
            "source": {
                "paperless_document_id": 7,
                "source_checksum": "sha256:" + "a" * 64,
                "pages": 1,
            },
            "input": {"pages_sent": 1, "images_sent": True, "truncated": False},
            "validation": {"status": "passed"},
        }
    )


def test_page_texts_and_render(letter_pdf: bytes) -> None:
    pages = page_texts(letter_pdf)
    assert len(pages) == 1 and "447,19 EUR" in pages[0].text
    images = render_pages(letter_pdf, [1], dpi=72)
    assert images[1][:2] == b"\xff\xd8"  # JPEG


def test_xmp_round_trip(letter_pdf: bytes) -> None:
    a = analysis()
    stamped = stamp_xmp(letter_pdf, a, "Finanzamt Musterstadt – ESt 2024")
    assert read_xmp_analysis(stamped) == a.model_dump(mode="json", exclude={"tax"})
    with pikepdf.open(io.BytesIO(stamped)) as pdf:
        meta = pdf.open_metadata()
        assert meta["dc:title"] == "Finanzamt Musterstadt – ESt 2024"
        assert meta[f"{{{XMP_NAMESPACE}}}amountDue"] == "447.19"
        assert meta[f"{{{XMP_NAMESPACE}}}dueDate"] == "2026-10-26"
        assert pdf.docinfo["/Title"] == "Finanzamt Musterstadt – ESt 2024"
        xml = pdf.Root.Metadata.read_bytes().decode()
        assert xml.count(f"<pdfaSchema:namespaceURI>{XMP_NAMESPACE}<") == 1
    # stamping twice does not duplicate the extension schema
    twice = stamp_xmp(stamped, a, "x")
    with pikepdf.open(io.BytesIO(twice)) as pdf:
        assert pdf.Root.Metadata.read_bytes().decode().count("<pdfaSchema:namespaceURI>") == 1


def test_tax_classification_is_not_written_to_xmp(letter_pdf: bytes) -> None:
    a = analysis()
    a.tax = TaxClassification(relevance="yes", scopes=["de_personal"], year=2025, reason="ESt")
    stamped = read_xmp_analysis(stamp_xmp(letter_pdf, a, "t"))
    assert stamped is not None and "tax" not in stamped


def test_text_survives_stamping(letter_pdf: bytes) -> None:
    stamped = stamp_xmp(letter_pdf, analysis(), "t")
    assert page_texts(stamped)[0].text == page_texts(letter_pdf)[0].text
