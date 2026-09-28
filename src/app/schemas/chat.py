from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ChatCreate(BaseModel):
    title: str | None = Field(default=None, max_length=255)
    model: str | None = Field(default=None, max_length=100)


class ChatUpdate(BaseModel):
    title: str = Field(min_length=1, max_length=120)

    @field_validator("title")
    @classmethod
    def strip_title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("title must not be empty after trimming whitespace")
        return value


class ChatOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    model: str
    pinned: bool
    archived: bool
    meta: dict
    created_at: datetime
    updated_at: datetime