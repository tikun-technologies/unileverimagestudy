"""add billing tables

Revision ID: 20260527_billing
Revises: 20260517_done_by_id
Create Date: 2026-05-27

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260527_billing"
down_revision: Union[str, Sequence[str], None] = "20260517_done_by_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

plan_type_enum = postgresql.ENUM("free", "pro", "enterprise", name="billing_plan_type_enum", create_type=False)
subscription_status_enum = postgresql.ENUM(
    "none",
    "active",
    "trialing",
    "past_due",
    "canceled",
    "incomplete",
    name="billing_subscription_status_enum",
    create_type=False,
)
study_payment_status_enum = postgresql.ENUM(
    "pending",
    "succeeded",
    "failed",
    "expired",
    "canceled",
    name="study_payment_status_enum",
    create_type=False,
)


def upgrade() -> None:
    plan_type_enum.create(op.get_bind(), checkfirst=True)
    subscription_status_enum.create(op.get_bind(), checkfirst=True)
    study_payment_status_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "user_billing_profiles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("stripe_customer_id", sa.String(length=255), nullable=True),
        sa.Column("stripe_subscription_id", sa.String(length=255), nullable=True),
        sa.Column("plan", plan_type_enum, nullable=False, server_default="free"),
        sa.Column("subscription_status", subscription_status_enum, nullable=False, server_default="none"),
        sa.Column("current_period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("user_id", name="uq_user_billing_profiles_user_id"),
        sa.UniqueConstraint("stripe_customer_id", name="uq_user_billing_profiles_stripe_customer_id"),
        sa.UniqueConstraint("stripe_subscription_id", name="uq_user_billing_profiles_stripe_subscription_id"),
    )
    op.create_index("ix_user_billing_profiles_user_id", "user_billing_profiles", ["user_id"])
    op.create_index("idx_user_billing_profiles_plan_status", "user_billing_profiles", ["plan", "subscription_status"])

    op.create_table(
        "study_payments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("study_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("studies.id", ondelete="SET NULL"), nullable=True),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("platform_fee_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("panel_cost_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cint_cpi_cents", sa.Numeric(10, 2), nullable=False),
        sa.Column("respondent_count", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=10), nullable=False, server_default="usd"),
        sa.Column("audience_snapshot", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("quote_hash", sa.String(length=64), nullable=False),
        sa.Column("stripe_checkout_session_id", sa.String(length=255), nullable=True),
        sa.Column("stripe_payment_intent_id", sa.String(length=255), nullable=True),
        sa.Column("status", study_payment_status_enum, nullable=False, server_default="pending"),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("stripe_checkout_session_id", name="uq_study_payments_checkout_session_id"),
        sa.UniqueConstraint("stripe_payment_intent_id", name="uq_study_payments_payment_intent_id"),
    )
    op.create_index("ix_study_payments_user_id", "study_payments", ["user_id"])
    op.create_index("ix_study_payments_study_id", "study_payments", ["study_id"])
    op.create_index("ix_study_payments_quote_hash", "study_payments", ["quote_hash"])
    op.create_index("idx_study_payments_user_study_status", "study_payments", ["user_id", "study_id", "status"])

    op.create_table(
        "billing_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("stripe_event_id", sa.String(length=255), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("stripe_event_id", name="uq_billing_events_stripe_event_id"),
    )
    op.create_index("ix_billing_events_stripe_event_id", "billing_events", ["stripe_event_id"])


def downgrade() -> None:
    op.drop_index("ix_billing_events_stripe_event_id", table_name="billing_events")
    op.drop_table("billing_events")
    op.drop_index("idx_study_payments_user_study_status", table_name="study_payments")
    op.drop_index("ix_study_payments_quote_hash", table_name="study_payments")
    op.drop_index("ix_study_payments_study_id", table_name="study_payments")
    op.drop_index("ix_study_payments_user_id", table_name="study_payments")
    op.drop_table("study_payments")
    op.drop_index("idx_user_billing_profiles_plan_status", table_name="user_billing_profiles")
    op.drop_index("ix_user_billing_profiles_user_id", table_name="user_billing_profiles")
    op.drop_table("user_billing_profiles")
    study_payment_status_enum.drop(op.get_bind(), checkfirst=True)
    subscription_status_enum.drop(op.get_bind(), checkfirst=True)
    plan_type_enum.drop(op.get_bind(), checkfirst=True)
