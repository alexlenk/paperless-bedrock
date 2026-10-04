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
- with a consistent **correspondent** and **document type**: chosen from what already exists,
  anchored on the sender's own identifiers (creditor ID, VAT ID, register number), and duplicates
  are **merged automatically** every night (logged, reversible),
- with **person tags** for the household members a letter is addressed to,
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
   Document view + change, Note view + add, Custom field view, Tag view,
   Correspondent view + add + change + delete, Document type view + add + change + delete
   (needed to create and merge them). **No document delete.** Create an API token for it.
3. **paperless objects:** create the tag `analysis-failed` (plus the tax and person tags from your
   context file, if any), and the custom fields you want filled
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
container (add `--force` to re-analyse), or queued in bulk with `paperless-bedrock enqueue`
(see [Importing an existing archive](#importing-an-existing-archive)).

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
| `CONTEXT_FILE` | none | TOML with persons, own IBANs/identifiers and tax scopes ([example](examples/context.toml)) |
| `ASSIGN_CORRESPONDENT` / `ASSIGN_DOCUMENT_TYPE` | `true` / `true` | Let the analyst set (and create) them |
| `JUDGE_MODEL_ID` | `BEDROCK_MODEL_ID` | Model for "same sender?" checks (short calls, no letter text) |
| `CONSOLIDATE_HOUR` | `3` | Local hour (container `TZ`) of the nightly consolidation; `off` disables it |
| `MAX_JUDGE_CALLS_PER_CONSOLIDATION` | `50` | Cost cap per nightly run |
| `FAILED_TAG` | `analysis-failed` | Tag for failed analyses |
| `REMOVE_INBOX_TAGS` | `false` | Remove inbox tags after analysis |
| `SET_TITLE` | `true` | Set the title to "Sender – Subject" |
| `WRITE_VERSION` | `true` | Add a PDF version with XMP metadata |
| `VERSION_LABEL` | `analysis-v1` | Label of that version |
| `CUSTOM_FIELDS__<KEY>` | see table above | Rename a custom field, e.g. `CUSTOM_FIELDS__AMOUNT=Betrag` |
| `MAX_ATTEMPTS` | `3` | Attempts per document before it is tagged as failed |
| `DATA_DIR` | `/data` | Job queue and knowledge index (SQLite) |

## Correspondents, document types and clean-up

The analyst sees the existing correspondents and document types and picks one; it creates a new
one only if nothing fits. Code then checks the choice before anything is created:

1. **Sender identifiers** decide: the creditor ID (SEPA), VAT ID or commercial register number of
   the sender maps to exactly one correspondent → that one is used, whatever the spelling.
   Tax, customer and contract numbers or IBANs never decide the sender (they are often yours, or a
   payment provider's); they are used as hints: the analyst sees the last related letters.
2. **Exact name or known alias** (old spellings of merged correspondents).
3. **Similar name** → a short model call compares the two (names, addresses, identifiers, recent
   subjects, no letter text) and maps only when confident.
4. Otherwise a new correspondent / document type is created.

Every night (`CONSOLIDATE_HOUR`) the same rules run over all correspondents and document types
and **merge duplicates automatically**: documents move, the duplicate is deleted, its name becomes
an alias. Objects created by a person in paperless are never merged away. Nothing needs you; if a
merge was wrong:

```sh
paperless-bedrock merges            # list recent merges
paperless-bedrock undo 17           # restore, and never merge that pair again
paperless-bedrock consolidate --dry-run
```

The knowledge index (`DATA_DIR/knowledge.sqlite3`) is only a lookup table derived from paperless;
`paperless-bedrock reindex` rebuilds it from the analysis notes, and paperless' current
assignments always win.

## Importing an existing archive

Example: 1,900 files exported from another DMS.

1. In the paperless workflow trigger, add the filter *does not have tags* = `import` so the import
   does not fire 1,900 webhooks.
2. Copy the files into `consume/import/` (with `PAPERLESS_CONSUMER_SUBDIRS_AS_TAGS=true` every
   document gets the tag `import`). paperless skips byte-identical duplicates.
3. When paperless is done, queue them in batches, newest first. Batch jobs run only when no new
   letter is waiting; `--light` skips the title change and the PDF version:

   ```sh
   docker exec paperless-bedrock paperless-bedrock enqueue --tag import --light \
       --created-after 2023-01-01 --limit 300
   ```

Cost: roughly 2–3 US cents per page with Claude Sonnet on Bedrock.

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
