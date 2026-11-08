"""Strict schema for the provider-authored prose portion of a staff reply."""

from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator
from typing import Annotated


class AIReplyContent(BaseModel):
    """Only acknowledgement prose is accepted; prices are assembled by the app."""

    model_config = ConfigDict(extra="forbid", strict=True)

    body: Annotated[str, StringConstraints(min_length=1, max_length=4000)]

    @field_validator("body")
    @classmethod
    def body_must_have_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Reply text cannot be blank.")
        return value.strip()
