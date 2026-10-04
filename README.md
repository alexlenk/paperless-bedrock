# paperless-bedrock

Neutral, evidence-backed letter analysis for [paperless-ngx](https://docs.paperless-ngx.com/),
using Amazon Bedrock.

> **Status: alpha, not yet tested against a real Bedrock account.** The pipeline is complete and
> covered by tests with a fake paperless server and a fake model. The first real runs, and an
> answer key to measure quality against, are still to come.

## What it does

When paperless-ngx adds a document, a workflow webhook calls paperless-bedrock. It reads the
letter (text and page images), asks a model on Amazon Bedrock for a structured analysis, checks
the result with deterministic rules, and stores it:

- in the **PDF itself** as a new document version with XMP metadata (standard Dublin Core fields
  plus the full analysis), so the analysis survives a change of document management system,
- as a **paperless note** (full JSON), **custom fields** (amount, due date, IBAN, ...) and a
  readable **title**,
- optionally with a **tax classification**: which of your tax returns (e.g. German, US, a US LLC)
  the document matters for, the tax year and the category — as tags and custom fields.

The analysis answers *what the letter says, who sent it, who it is for and what it asks for* —
with a verbatim quote for every key statement. It deliberately does **not** decide what to do;
that is left to you, a script or another agent, which can decide from the analysis alone.

## Principles

- **One letter in, one analysis out.** The model has no tools and sees only that one letter.
- **Evidence, not trust.** Quotes, amounts, IBANs and dates are checked against the letter text;
  on problems the model gets one retry with the list of issues.
- **No silent failures.** A failed or uncertain analysis is stored with its issues and tagged.
- **Least privilege.** A paperless service user without delete rights; an AWS identity that can only
  invoke one model.
- **Measured.** Changes to prompt or model are checked against your own answer key.

## Setup

Requirements: paperless-ngx 3.x (document versions API), Docker, an AWS account with access to
Claude on Amazon Bedrock.

1. **AWS:** create an IAM user (or role) with only the permissions in
   [`examples/iam-policy.json`](examples/iam-policy.json). Check the exact inference profile and
   model ARNs in the Bedrock console for your region. A dedicated AWS account keeps billing and
   access separate.
2. **paperless service user** (e.g. `svc-bedrock`, not an admin): permissions
   Document view + change, Note view + add, Custom field view, Tag view, Correspondent / Document
   type view. Create an API token for it.
3. **paperless objects:** create the tag `analysis-failed`, and the custom fields you want filled
   (any subset; missing ones are skipped):

   | Default name | Type |
   |---|---|
   | Payment needed | Boolean |
   | Amount | Monetary |
   | Due date | Date |
   | Payee IBAN | Text |
   | Payment reference | Text |
   | Reply deadline | Date |
   | Country | Text |
   | Tax year | Integer |
   | Tax categories | Text |
4. **Container:** add the service from [`examples/docker-compose.yml`](examples/docker-compose.yml)
   to the compose project that runs paperless (same network, no published ports).
5. **paperless workflow:** trigger *Document Added*, action *Webhook*:
   - URL `http://paperless-bedrock:8080/analyze`
   - *Use parameters* on, *Send as JSON* on, parameter `document_id` = `{{ doc_id }}`
   - Header `Authorization` = `Bearer <WEBHOOK_TOKEN>`

   Webhooks to internal addresses must be allowed (`PAPERLESS_WEBHOOKS_ALLOW_INTERNAL_REQUESTS`,
   default `true`).

Existing documents can be analysed with `paperless-bedrock analyze <document id>` inside the
container (add `--force` to re-analyse).

## Configuration

All settings are environment variables.

| Variable | Default | Meaning |
|---|---|---|
| `PAPERLESS_URL` | required | Internal paperless URL, e.g. `http://webserver:8000` |
| `PAPERLESS_TOKEN` | required | API token of the service user |
| `PAPERLESS_PUBLIC_URL` | `PAPERLESS_URL` | Base URL for links inside the analysis |
| `WEBHOOK_TOKEN` | none | If set, `/analyze` requires `Authorization: Bearer <token>` |
| `BEDROCK_MODEL_ID` | `eu.anthropic.claude-sonnet-5-5` | Model or inference profile |
| `AWS_REGION` | `eu-central-1` | Bedrock region; credentials via the usual AWS variables |
| `MAX_OUTPUT_TOKENS` | `8000` | Output limit per model call |
| `MAX_PAGES` / `MAX_IMAGE_PAGES` | `20` / `20` | Pages sent as text / as images; more pages set `input.truncated` |
| `IMAGE_DPI` | `130` | Resolution of the page images |
| `NOISE_PROFILES` | `["deutsche_post_postscan"]` | JSON list of noise profiles (see `prepare.py`) |
| `CONTEXT_FILE` | none | TOML with names, own IBANs and tax scopes ([example](examples/context.toml)) |
| `FAILED_TAG` | `analysis-failed` | Tag for failed analyses |
| `REMOVE_INBOX_TAGS` | `false` | Remove inbox tags after analysis |
| `SET_TITLE` | `true` | Set the title to "Sender – Subject" |
| `WRITE_VERSION` | `true` | Add a PDF version with XMP metadata |
| `VERSION_LABEL` | `analysis-v1` | Label of that version |
| `CUSTOM_FIELDS__<KEY>` | see table above | Rename a custom field, e.g. `CUSTOM_FIELDS__AMOUNT=Betrag` |
| `MAX_ATTEMPTS` | `3` | Attempts per document before it is tagged as failed |
| `DATA_DIR` | `/data` | Job queue (SQLite) |

## Tax classification

Configure tax scopes in the context file ([example](examples/context.toml)): one scope per tax
return or entity, each with a paperless tag, plus short household facts for special cases (home
office, rental property, an LLC, children, foreign accounts). For every document the model then
decides `yes` / `no` / `unclear`, the scopes, the tax year (the year the income or expense belongs
to, not the letter date) and categories such as `Anlage V` or `Schedule C expense`. When in doubt it
says `yes`; `unclear` is reserved for cases that depend on a fact that is missing.

In paperless this becomes one tag per relevant scope (e.g. `tax-DE`, `tax-LLC`), `tax-unclear` for
unclear cases, and the custom fields `Tax year` and `Tax categories`. Create the tags first.
Re-analysing a document replaces its tax tags. The tax classification is kept out of the PDF's XMP
metadata because it depends on your household context, not on the letter alone.

To hand documents to a tax adviser: filter by tag `tax-DE` and `Tax year` = 2025, select all,
download.

## Where to read the analysis

- **paperless note** on the document: the full JSON ([schema](schema/letter-analysis-v1.schema.json)).
- **PDF version** `analysis-v1`: XMP namespace
  `https://github.com/alexlenk/paperless-bedrock/ns/letter/1.0/` (prefix `letter:`), with flat
  fields such as `letter:amountDue`, `letter:dueDate` and the full JSON in `letter:analysisJSON`;
  plus Dublin Core title, creator, description and subject. Readable with e.g.
  `exiftool -XMP-letter:all file.pdf` (after declaring the namespace) or any XMP library.

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
