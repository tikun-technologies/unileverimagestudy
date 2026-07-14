"""add layer templates

Revision ID: 20260713_layer_templates
Revises: 20260701_active_filters
Create Date: 2026-07-13

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260713_layer_templates"
down_revision: Union[str, Sequence[str], None] = "20260701_active_filters"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    template_status = postgresql.ENUM("draft", "published", name="template_status_enum", create_type=False)
    template_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "layer_templates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("normalized_title", sa.String(length=255), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM("draft", "published", name="template_status_enum", create_type=False),
            nullable=False,
            server_default="draft",
        ),
        sa.Column("aspect_ratio", sa.String(length=16), nullable=False),
        sa.Column("layer_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("preview_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("layer_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("element_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("normalized_title", name="uq_layer_templates_normalized_title"),
    )
    op.create_index("ix_layer_templates_id", "layer_templates", ["id"], unique=False)
    op.create_index("ix_layer_templates_status", "layer_templates", ["status"], unique=False)
    op.create_index("ix_layer_templates_created_by_id", "layer_templates", ["created_by_id"], unique=False)
    op.create_index("idx_layer_templates_status_updated", "layer_templates", ["status", "updated_at"], unique=False)
    op.create_index("idx_layer_templates_created_by_updated", "layer_templates", ["created_by_id", "updated_at"], unique=False)
    op.create_index("idx_layer_templates_title", "layer_templates", ["title"], unique=False)


def downgrade() -> None:
    op.drop_index("idx_layer_templates_title", table_name="layer_templates")
    op.drop_index("idx_layer_templates_created_by_updated", table_name="layer_templates")
    op.drop_index("idx_layer_templates_status_updated", table_name="layer_templates")
    op.drop_index("ix_layer_templates_created_by_id", table_name="layer_templates")
    op.drop_index("ix_layer_templates_status", table_name="layer_templates")
    op.drop_index("ix_layer_templates_id", table_name="layer_templates")
    op.drop_table("layer_templates")
    op.execute("DROP TYPE IF EXISTS template_status_enum")
