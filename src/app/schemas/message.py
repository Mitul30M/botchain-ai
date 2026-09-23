from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class MessageCreate(BaseModel):
    content: str = Field(min_length=1)
    parent_id: str | None = None


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    chat_id: str
    parent_id: str | None
    role: str
    content: str
    is_error: bool
    meta: dict
    created_at: datetime


class ApproveRequest(BaseModel):
    approved: bool = True
    feedback: str | None = Field(default=None, max_length=2000)