from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ChatCreate(BaseModel):
    title: str | None = Field(default=None, max_length=255)
    model: str | None = Field(default=None, max_length=100)


class ChatUpdate(BaseModel):
    title: str = Field(min_length=1, max_length=255)


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