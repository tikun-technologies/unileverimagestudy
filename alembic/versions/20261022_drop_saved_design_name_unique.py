"""Allow more than one saved combination to use the same name.

Revision ID: 20261022_design_names
Revises: 20261006_option_quotas
Create Date: 2026-10-22
"""
from alembic import op


revision = "20261022_design_names"
down_revision = "20261006_option_quotas"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "uq_study_saved_designs_study_type_name",
        "study_saved_designs",
        type_="unique",
    )


def downgrade() -> None:
    op.create_unique_constraint(
        "uq_study_saved_designs_study_type_name",
        "study_saved_designs",
        ["study_id", "design_type", "normalized_name"],
    )
