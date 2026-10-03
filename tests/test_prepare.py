from paperless_bedrock.pdf import Page
from paperless_bedrock.prepare import prepare, text_quality

BODY = "Sehr geehrte Damen und Herren, anbei erhalten Sie die Rechnung für den Monat September."


def test_postscan_noise_lines_are_removed() -> None:
    page = Page(
        1, f"DV 09.26 0,95 Deutsche Post\nPREMIUMADRESS\n*K4000*\n{BODY}\nMY 20260928 009264"
    )
    result = prepare([page], ["deutsche_post_postscan"], max_pages=20)
    assert result.pages[0].text == BODY


def test_separator_page_is_dropped_and_job_id_kept() -> None:
    pages = [Page(1, "Deutsche Post Scan PK6039423L MA26271-007744"), Page(2, BODY)]
    result = prepare(pages, ["deutsche_post_postscan"], max_pages=20)
    assert result.scan_job_id == "PK6039423L MA26271-007744"
    assert result.dropped_pages == [1]
    assert [p.number for p in result.pages] == [2]


def test_empty_page_is_dropped_but_never_everything() -> None:
    assert prepare([Page(1, BODY), Page(2, "  ")], [], 20).dropped_pages == [2]
    single = prepare([Page(1, "")], [], 20)
    assert [p.number for p in single.pages] == [1]


def test_truncation_is_flagged() -> None:
    result = prepare([Page(i, BODY) for i in range(1, 6)], [], max_pages=3)
    assert result.truncated and len(result.pages) == 3


def test_text_quality() -> None:
    assert text_quality(BODY) > 0.9
    assert text_quality("§$% 1§2 ~~ ##") == 0.0
