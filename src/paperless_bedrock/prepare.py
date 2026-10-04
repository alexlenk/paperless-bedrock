"""Input preparation: noise removal, noise pages, text quality."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from paperless_bedrock.pdf import Page


@dataclass(frozen=True)
class NoiseProfile:
    name: str
    line_patterns: list[re.Pattern[str]]
    # Pages matching this pattern (and having little other text) are separator pages;
    # group 0 of the match becomes source.scan_job_id.
    separator_pattern: re.Pattern[str] | None = None


# Patterns derived from real Postscan letters; extend via pull request when you see others.
NOISE_PROFILES: dict[str, NoiseProfile] = {
    "deutsche_post_postscan": NoiseProfile(
        name="deutsche_post_postscan",
        line_patterns=[
            re.compile(r"^\s*DV\s+\d{2}\.\d{2}\s+\d+,\d{2}\s+Deutsche\s+Post\s*$"),  # franking
            re.compile(r"^\s*PREMIUMADRESS\s*$"),
            re.compile(r"^\s*\*K\d{4}\*\s*$"),
            re.compile(r"^\s*[A-Z]{2}\s+\d{8}\s+\d{6}\s*$"),  # page stamp, e.g. MY 20260928 009264
        ],
        separator_pattern=re.compile(r"\b[A-Z]{2}\d{7}[A-Z]\s+[A-Z]{2}\d{5}-\d{6}\b"),
    ),
}

_WORD = re.compile(r"[^\W\d_]{2,}", re.UNICODE)
_TOKEN = re.compile(r"\S+")
MIN_WORDS = 8
SEPARATOR_MAX_WORDS = 60


def text_quality(text: str) -> float:
    """Share of whitespace-separated tokens that look like real words (0..1)."""
    tokens = _TOKEN.findall(text)
    if not tokens:
        return 0.0
    words = sum(1 for t in tokens if _WORD.fullmatch(t.strip(".,;:!?()\"'„“”‚‘’-")))
    return round(words / len(tokens), 3)


@dataclass
class PreparedInput:
    pages: list[Page]  # cleaned pages kept for the model
    dropped_pages: list[int] = field(default_factory=list)
    scan_job_id: str | None = None
    truncated: bool = False
    quality: dict[int, float] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return "\n\n".join(p.text for p in self.pages)


def prepare(pages: list[Page], profile_names: list[str], max_pages: int) -> PreparedInput:
    profiles = [NOISE_PROFILES[n] for n in profile_names]
    result = PreparedInput(pages=[])
    for page in pages:
        lines = [
            line
            for line in page.text.splitlines()
            if not any(p.match(line) for prof in profiles for p in prof.line_patterns)
        ]
        text = "\n".join(lines).strip()
        words = len(_WORD.findall(text))
        separator = next(
            (
                m
                for prof in profiles
                if prof.separator_pattern
                for m in [prof.separator_pattern.search(text)]
                if m
            ),
            None,
        )
        if separator and words <= SEPARATOR_MAX_WORDS:
            result.scan_job_id = result.scan_job_id or separator.group(0)
            result.dropped_pages.append(page.number)
            continue
        if words < MIN_WORDS and len(pages) > 1:
            result.dropped_pages.append(page.number)
            continue
        result.quality[page.number] = text_quality(text)
        result.pages.append(Page(number=page.number, text=text))

    if not result.pages and pages:  # never drop everything
        result.pages = [pages[0]]
        result.dropped_pages = [n for n in result.dropped_pages if n != pages[0].number]
    if len(result.pages) > max_pages:
        result.pages = result.pages[:max_pages]
        result.truncated = True
    return result
