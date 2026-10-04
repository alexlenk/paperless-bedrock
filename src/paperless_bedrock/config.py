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


class RecipientContext(BaseModel):
    """Optional context so the model can recognise recipients and own accounts. No rules."""

    names: list[str] = []
    own_ibans: list[str] = []


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
    recipient_context_file: Path | None = None

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

    def recipient_context(self) -> RecipientContext:
        if self.recipient_context_file is None:
            return RecipientContext()
        with self.recipient_context_file.open("rb") as f:
            return RecipientContext.model_validate(tomllib.load(f))
