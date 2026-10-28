"""A migration that looks reasonable and would take a busy table down.

Run the checker on it:

    python skills/alembic-safe-migrations/scripts/check_migration.py examples/risky_migration.py
"""
import sqlalchemy as sa
from alembic import op

revision = "0002_add_region"
down_revision = "0001_initial"


def upgrade():
    # Fails outright on a non-empty table (no default) ...
    op.add_column("orders", sa.Column("region", sa.Text(), nullable=False))
    # ... and this one rewrites every row to fill in now().
    op.add_column("orders", sa.Column("seen_at", sa.DateTime(), server_default=sa.text("now()")))
    # SHARE lock: every INSERT/UPDATE on orders waits for the whole build.
    op.create_index("ix_orders_region", "orders", ["region"])
    # Scans the whole referencing table under a SHARE ROW EXCLUSIVE lock.
    op.create_foreign_key("fk_orders_customer", "orders", "customers", ["customer_id"], ["id"])
    # One transaction, a row lock on every row, hours of WAL.
    op.execute("UPDATE orders SET region = 'unknown'")
    # Full scan under ACCESS EXCLUSIVE.
    op.alter_column("orders", "region", nullable=False)


def downgrade():
    op.drop_constraint("fk_orders_customer", "orders")
    op.drop_index("ix_orders_region", table_name="orders")
    op.drop_column("orders", "seen_at")
    op.drop_column("orders", "region")
