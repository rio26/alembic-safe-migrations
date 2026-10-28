"""Enforce NOT NULL on an existing column without scanning it under ACCESS EXCLUSIVE.

Assumes the column was added nullable in an earlier migration and has
already been backfilled in batches (see reference/patterns.md, pattern 2).
On PostgreSQL 12+ the final SET NOT NULL sees the validated CHECK constraint
and skips the table scan; the checker recognises the VALIDATE step and
does not warn about it.
"""
from alembic import op

revision = "0005_orders_region_not_null"
down_revision = "0004_backfill_orders_region"


def upgrade():
    op.execute("SET lock_timeout = '3s'")
    op.create_check_constraint(
        "ck_orders_region_not_null",
        "orders",
        "region IS NOT NULL",
        postgresql_not_valid=True,
    )
    # Full scan, but under SHARE UPDATE EXCLUSIVE: reads and writes continue.
    op.execute("ALTER TABLE orders VALIDATE CONSTRAINT ck_orders_region_not_null")
    # Metadata only now that a validated CHECK exists.
    op.alter_column("orders", "region", nullable=False)
    op.drop_constraint("ck_orders_region_not_null", "orders")


def downgrade():
    op.execute("SET lock_timeout = '3s'")
    op.alter_column("orders", "region", nullable=True)
