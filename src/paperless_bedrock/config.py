"""Configuration from environment variables."""

from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class CustomFieldNames(BaseModel):
    """Names of the paperless custom fields to fill. Fields missing in paperless are skipped."""

    payment_needed: str = "Payment needed"
    amount: str = "Amount"
    due_date: str = "Due date"
    payee_iban: str = "Payee IBAN"
    payment_reference: str = "Payment reference"
    reply_deadline: str = "Reply deadline"
    country: str = "Country"
    tax_year: str = "Tax year"
    tax_categories: str = "Tax categories"


class TaxScope(BaseModel):
    """One tax return or entity a document can be relevant for, e.g. a personal German return."""

    id: str = Field(pattern=r"^[a-z0-9_]+$")
    jurisdiction: str = Field(description="Country code, e.g. DE or US.")
    description: str = Field(description="Who files it and what it covers, in plain words.")
    tag: str = Field(description="paperless tag set on documents relevant for this scope.")


class TaxContext(BaseModel):
    scopes: list[TaxScope] = []
    facts: list[str] = Field(
        default=[], description="Household facts that decide special cases. Facts, not rules."
    )
    unclear_tag: str = "tax-unclear"


class Person(BaseModel):
    name: str
    tag: str | None = Field(default=None, description="paperless tag for letters to this person.")
    aliases: list[str] = []


class Context(BaseModel):
    """Optional context so the model can recognise recipients, own accounts and tax scopes."""

    names: list[str] = Field(default=[], description="People without a tag (shorthand).")
    persons: list[Person] = []
    own_ibans: list[str] = []
    own_identifiers: list[str] = Field(
        default=[], description="Own VAT IDs, creditor IDs etc. (e.g. of an own company)."
    )
    tax: TaxContext = TaxContext()

    @property
    def all_persons(self) -> list[Person]:
        return [*self.persons, *(Person(name=n) for n in self.names)]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_nested_delimiter="__", extra="ignore")

    paperless_url: str = Field(description="Internal URL, e.g. http://webserver:8000")
    paperless_token: str
    paperless_public_url: str | None = Field(
        default=None,
        description="URL used in links inside the analysis; defaults to paperless_url.",
    )

    bedrock_model_id: str = "eu.anthropic.claude-sonnet-5-5"
    aws_region: str = "eu-central-1"
    max_output_tokens: int = 8000

    data_dir: Path = Path("/data")
    webhook_token: str | None = Field(
        default=None, description="If set, POST /analyze requires 'Authorization: Bearer <token>'."
    )

    max_pages: int = 20
    max_image_pages: int = 20
    image_dpi: int = 130
    noise_profiles: list[str] = ["deutsche_post_postscan"]
    context_file: Path | None = None

    failed_tag: str = "analysis-failed"
    remove_inbox_tags: bool = False
    set_title: bool = True
    set_created_date: bool = Field(
        default=True, description="Set the paperless document date to the date of the letter."
    )
    write_version: bool = True
    version_label: str = "analysis-v1"
    custom_fields: CustomFieldNames = CustomFieldNames()

    max_attempts: int = 3

    judge_model_id: str | None = Field(
        default=None, description="Model for same-entity checks; defaults to bedrock_model_id."
    )
    assign_correspondent: bool = True
    assign_document_type: bool = True
    consolidate_hour: int | None = Field(
        default=3, ge=0, le=23, description="Local hour for the nightly consolidation; None = off."
    )
    max_judge_calls_per_consolidation: int = 50

    @field_validator("consolidate_hour", mode="before")
    @classmethod
    def _hour_off(cls, value: object) -> object:
        if isinstance(value, str) and value.strip().lower() in ("", "off", "none", "false"):
            return None
        return value

    @property
    def link_base(self) -> str:
        return (self.paperless_public_url or self.paperless_url).rstrip("/")

    def context(self) -> Context:
        if self.context_file is None:
            return Context()
        with self.context_file.open("rb") as f:
            return Context.model_validate(tomllib.load(f))
