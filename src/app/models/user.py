import datetime as dt
from typing import TYPE_CHECKING

from sqlalchemy import text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, new_id

if TYPE_CHECKING:
    from app.models.billing import CreditTransaction, CreditWallet, PaymentTopup
    from app.models.chat import Chat


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(primary_key=True, default=new_id)
    kinde_id: Mapped[str] = mapped_column(unique=True)
    email: Mapped[str] = mapped_column(unique=True)
    first_name: Mapped[str | None]
    last_name: Mapped[str | None]
    created_at: Mapped[dt.datetime] = mapped_column(server_default=text("now()"))
    updated_at: Mapped[dt.datetime] = mapped_column(server_default=text("now()"))

    chats: Mapped[list[Chat]] = relationship(back_populates="user")
    credit_transactions: Mapped[list[CreditTransaction]] = relationship(
        back_populates="user", foreign_keys="CreditTransaction.user_id"
    )
    payment_topups: Mapped[list[PaymentTopup]] = relationship(
        back_populates="user", foreign_keys="PaymentTopup.user_id"
    )
    wallet: Mapped[CreditWallet | None] = relationship(
        back_populates="user", uselist=False, foreign_keys="CreditWallet.user_id"
    )