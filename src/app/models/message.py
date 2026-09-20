import datetime as dt
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Index, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, new_id

if TYPE_CHECKING:
    from app.models.chat import Chat


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        Index("messages_chat_id_idx_5eb3a8f3", "chat_id"),
        Index("messages_parent_id_idx_ab33b399", "parent_id"),
    )

    id: Mapped[str] = mapped_column(primary_key=True, default=new_id)
    chat_id: Mapped[str] = mapped_column(ForeignKey("chats.id", name="messages_chat_id_fkey"))
    parent_id: Mapped[str | None] = mapped_column(
        ForeignKey("messages.id", name="messages_parent_id_fkey")
    )
    role: Mapped[str]
    content: Mapped[str]
    input_tokens: Mapped[int | None]
    output_tokens: Mapped[int | None]
    credits_cost: Mapped[Decimal | None]
    is_error: Mapped[bool] = mapped_column(server_default=text("false"))
    meta: Mapped[dict] = mapped_column(server_default=text("'{}'::json"))
    created_at: Mapped[dt.datetime] = mapped_column(server_default=text("now()"))

    chat: Mapped[Chat] = relationship(back_populates="messages")
    parent: Mapped[Message | None] = relationship(
        remote_side=[id], back_populates="replies"
    )
    replies: Mapped[list[Message]] = relationship(back_populates="parent")
    attachments: Mapped[list[Attachment]] = relationship(back_populates="message")


class Attachment(Base):
    __tablename__ = "attachments"
    __table_args__ = (Index("attachments_message_id_idx_8a7ff1ba", "message_id"),)

    id: Mapped[str] = mapped_column(primary_key=True, default=new_id)
    message_id: Mapped[str] = mapped_column(
        ForeignKey("messages.id", name="attachments_message_id_fkey")
    )
    file_name: Mapped[str]
    file_type: Mapped[str]
    file_url: Mapped[str]
    size_bytes: Mapped[int]
    created_at: Mapped[dt.datetime] = mapped_column(server_default=text("now()"))

    message: Mapped[Message] = relationship(back_populates="attachments")