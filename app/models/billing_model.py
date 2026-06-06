from sqlalchemy import (
    Column,
    String,
    Integer,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    UniqueConstraint,
    Numeric,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
import uuid

from app.db.base import Base

plan_type_enum = Enum("free", "pro", "enterprise", name="billing_plan_type_enum")
subscription_status_enum = Enum(
    "none",
    "active",
    "trialing",
    "past_due",
    "canceled",
    "incomplete",
    name="billing_subscription_status_enum",
)
study_payment_status_enum = Enum(
    "pending",
    "succeeded",
    "failed",
    "expired",
    "canceled",
    name="study_payment_status_enum",
)


class UserBillingProfile(Base):
    __tablename__ = "user_billing_profiles"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    stripe_customer_id = Column(String(255), nullable=True, unique=True, index=True)
    stripe_subscription_id = Column(String(255), nullable=True, unique=True, index=True)
    plan = Column(plan_type_enum, nullable=False, server_default="free")
    subscription_status = Column(
        subscription_status_enum, nullable=False, server_default="none"
    )
    current_period_start = Column(DateTime(timezone=True), nullable=True)
    current_period_end = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    user = relationship("User", backref="billing_profile", lazy="noload")

    __table_args__ = (
        Index("idx_user_billing_profiles_plan_status", "plan", "subscription_status"),
    )


class StudyPayment(Base):
    __tablename__ = "study_payments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    study_id = Column(
        UUID(as_uuid=True), ForeignKey("studies.id", ondelete="SET NULL"), nullable=True, index=True
    )
    amount_cents = Column(Integer, nullable=False)
    platform_fee_cents = Column(Integer, nullable=False, server_default="0")
    panel_cost_cents = Column(Integer, nullable=False, server_default="0")
    cint_cpi_cents = Column(Numeric(10, 2), nullable=False)
    respondent_count = Column(Integer, nullable=False)
    currency = Column(String(10), nullable=False, server_default="usd")
    audience_snapshot = Column(JSONB, nullable=False, server_default="{}")
    quote_hash = Column(String(64), nullable=False, index=True)
    stripe_checkout_session_id = Column(String(255), nullable=True, unique=True, index=True)
    stripe_payment_intent_id = Column(String(255), nullable=True, unique=True, index=True)
    status = Column(study_payment_status_enum, nullable=False, server_default="pending")
    paid_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    user = relationship("User", backref="study_payments", lazy="noload")
    study = relationship("Study", backref="payments", lazy="noload")

    __table_args__ = (
        Index("idx_study_payments_user_study_status", "user_id", "study_id", "status"),
    )


class BillingEvent(Base):
    __tablename__ = "billing_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    stripe_event_id = Column(String(255), nullable=False, unique=True, index=True)
    event_type = Column(String(128), nullable=False)
    payload = Column(JSONB, nullable=True)
    processed_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (UniqueConstraint("stripe_event_id", name="uq_billing_events_stripe_event_id"),)
