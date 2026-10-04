"""Configuration from environment variables."""

from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, Field
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


class Context(BaseModel):
    """Optional context so the model can recognise recipients, own accounts and tax scopes."""

    names: list[str] = []
    own_ibans: list[str] = []
    tax: TaxContext = TaxContext()


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
    write_version: bool = True
    version_label: str = "analysis-v1"
    custom_fields: CustomFieldNames = CustomFieldNames()

    max_attempts: int = 3

    @property
    def link_base(self) -> str:
        return (self.paperless_public_url or self.paperless_url).rstrip("/")

    def context(self) -> Context:
        if self.context_file is None:
            return Context()
        with self.context_file.open("rb") as f:
            return Context.model_validate(tomllib.load(f))
