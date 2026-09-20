import datetime as dt
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Index, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, new_id

if TYPE_CHECKING:
    from app.models.user import User


class CreditWallet(Base):
    __tablename__ = "credit_wallets"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", name="credit_wallets_user_id_fkey"), primary_key=True
    )
    balance: Mapped[Decimal] = mapped_column(server_default=text("'0'::numeric"))
    updated_at: Mapped[dt.datetime] = mapped_column(server_default=text("now()"))

    user: Mapped[User] = relationship(back_populates="wallet")


class CreditTransaction(Base):
    __tablename__ = "credit_transactions"
    __table_args__ = (Index("credit_transactions_user_id_idx_6c952402", "user_id"),)

    id: Mapped[str] = mapped_column(primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", name="credit_transactions_user_id_fkey")
    )
    type: Mapped[str]
    amount: Mapped[Decimal]
    balance_after: Mapped[Decimal]
    reference_type: Mapped[str | None]
    reference_id: Mapped[str | None]
    provider: Mapped[str | None]
    provider_ref: Mapped[str | None]
    meta: Mapped[dict] = mapped_column(server_default=text("'{}'::json"))
    created_at: Mapped[dt.datetime] = mapped_column(server_default=text("now()"))

    user: Mapped[User] = relationship(back_populates="credit_transactions")


class PaymentTopup(Base):
    __tablename__ = "payment_topups"
    __table_args__ = (Index("payment_topups_user_id_idx_6c952402", "user_id"),)

    id: Mapped[str] = mapped_column(primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", name="payment_topups_user_id_fkey")
    )
    provider: Mapped[str]
    provider_payment_id: Mapped[str] = mapped_column(unique=True)
    amount_fiat: Mapped[Decimal]
    currency: Mapped[str] = mapped_column(server_default=text("'USD'::text"))
    credits_purchased: Mapped[Decimal]
    status: Mapped[str] = mapped_column(server_default=text("'pending'::text"))
    created_at: Mapped[dt.datetime] = mapped_column(server_default=text("now()"))
    completed_at: Mapped[dt.datetime | None]

    user: Mapped[User] = relationship(back_populates="payment_topups")