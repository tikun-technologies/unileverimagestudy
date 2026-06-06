"""add design_type to study_saved_designs

Revision ID: 20260607_saved_designs_type
Revises: 20260606_saved_designs
Create Date: 2026-06-07

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260607_saved_designs_type"
down_revision: Union[str, Sequence[str], None] = "20260606_saved_designs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(bind, table_name: str) -> bool:
    return table_name in sa.inspect(bind).get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    if not _table_exists(bind, "study_saved_designs"):
        return

    inspector = sa.inspect(bind)
    columns = {col["name"] for col in inspector.get_columns("study_saved_designs")}

    if "design_type" not in columns:
        op.add_column(
            "study_saved_designs",
            sa.Column("design_type", sa.String(length=30), nullable=False, server_default="configurator"),
        )

    unique_constraints = {
        uc["name"]: set(uc["column_names"])
        for uc in inspector.get_unique_constraints("study_saved_designs")
    }
    for name, cols in list(unique_constraints.items()):
        if cols == {"study_id", "normalized_name"}:
            op.drop_constraint(name, "study_saved_designs", type_="unique")

    if "uq_study_saved_designs_study_type_name" not in unique_constraints:
        op.create_unique_constraint(
            "uq_study_saved_designs_study_type_name",
            "study_saved_designs",
            ["study_id", "design_type", "normalized_name"],
        )

    index_names = {idx["name"] for idx in inspector.get_indexes("study_saved_designs")}
    if "idx_study_saved_designs_study_type_created" not in index_names:
        op.create_index(
            "idx_study_saved_designs_study_type_created",
            "study_saved_designs",
            ["study_id", "design_type", "created_at"],
            unique=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    if not _table_exists(bind, "study_saved_designs"):
        return

    inspector = sa.inspect(bind)
    index_names = {idx["name"] for idx in inspector.get_indexes("study_saved_designs")}
    if "idx_study_saved_designs_study_type_created" in index_names:
        op.drop_index("idx_study_saved_designs_study_type_created", table_name="study_saved_designs")

    unique_constraints = {uc["name"] for uc in inspector.get_unique_constraints("study_saved_designs")}
    if "uq_study_saved_designs_study_type_name" in unique_constraints:
        op.drop_constraint("uq_study_saved_designs_study_type_name", "study_saved_designs", type_="unique")

    columns = {col["name"] for col in inspector.get_columns("study_saved_designs")}
    if "design_type" in columns:
        op.drop_column("study_saved_designs", "design_type")

    unique_constraints = {
        uc["name"]: set(uc["column_names"])
        for uc in sa.inspect(bind).get_unique_constraints("study_saved_designs")
    }
    has_study_name_unique = any(cols == {"study_id", "normalized_name"} for cols in unique_constraints.values())
    if not has_study_name_unique:
        op.create_unique_constraint(
            "uq_study_saved_designs_study_name",
            "study_saved_designs",
            ["study_id", "normalized_name"],
        )
