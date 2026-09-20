import datetime as dt
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Index, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, new_id

if TYPE_CHECKING:
    from app.models.message import Message
    from app.models.user import User


class Chat(Base):
    __tablename__ = "chats"
    __table_args__ = (Index("chats_user_id_idx_6c952402", "user_id"),)

    id: Mapped[str] = mapped_column(primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", name="chats_user_id_fkey"))
    title: Mapped[str] = mapped_column(server_default=text("'New Chat'::text"))
    model: Mapped[str] = mapped_column(server_default=text("'claude-sonnet-4-6'::text"))
    system_prompt: Mapped[str | None]
    context_summary: Mapped[str | None]
    pinned: Mapped[bool] = mapped_column(server_default=text("false"))
    archived: Mapped[bool] = mapped_column(server_default=text("false"))
    meta: Mapped[dict] = mapped_column(server_default=text("'{}'::json"))
    created_at: Mapped[dt.datetime] = mapped_column(server_default=text("now()"))
    updated_at: Mapped[dt.datetime] = mapped_column(server_default=text("now()"))
    deleted_at: Mapped[dt.datetime | None]

    user: Mapped[User] = relationship(back_populates="chats")
    messages: Mapped[list[Message]] = relationship(back_populates="chat")