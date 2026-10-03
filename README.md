# paperless-bedrock

Neutral, evidence-backed letter analysis for [paperless-ngx](https://docs.paperless-ngx.com/),
using Amazon Bedrock.

> **Status: pre-alpha.** The design and the analysis schema exist; the analyzer itself is not
> implemented yet. Not usable yet.

## What it does

When paperless-ngx adds a document, a workflow webhook calls paperless-bedrock. It reads the
letter (text and page images), asks a model on Amazon Bedrock for a structured analysis, checks
the result with deterministic rules, and stores it:

- in the **PDF itself** as XMP metadata (standard Dublin Core fields plus the full analysis), so the
  analysis survives a change of document management system,
- as a **paperless note** (full JSON) and **custom fields** (amount, due date, IBAN, ...).

The analysis answers *what the letter says, who sent it, who it is for and what it asks for* —
with a verbatim quote for every key statement. It deliberately does **not** decide what to do;
that is left to you, a script or another agent, which can decide from the analysis alone.

## Principles

- **One letter in, one analysis out.** The model has no tools and sees only that one letter.
- **Evidence, not trust.** Quotes, amounts, IBANs and dates are checked against the letter text.
- **No silent failures.** A failed or uncertain analysis is stored and flagged, never dropped.
- **Least privilege.** A paperless service user without delete rights; an AWS identity that can only
  invoke one model.
- **Measured.** Changes to prompt or model are checked against your own answer key.

## Documentation

- [Design](docs/design.md)
- [Analysis schema v1 (JSON Schema)](schema/letter-analysis-v1.schema.json) —
  print it with `paperless-bedrock schema`.

## Development

```sh
uv sync
uv run pytest
uv run ruff check && uv run ruff format --check && uv run mypy src tests
```

After changing `src/paperless_bedrock/schema.py`, regenerate the committed schema:

```sh
uv run paperless-bedrock schema > schema/letter-analysis-v1.schema.json
```

## License

[Apache-2.0](LICENSE)
