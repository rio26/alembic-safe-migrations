"""Add an index to a large, busy table without blocking writes.

One autocommit operation per migration file: nothing else goes in here.
"""
from alembic import op

revision = "0003_index_orders_region"
down_revision = "0002_add_region"


def upgrade():
    # Session-level, so it survives the commit that autocommit_block() performs.
    op.execute("SET lock_timeout = '3s'")
    with op.get_context().autocommit_block():
        # Clears an INVALID index left behind by an earlier failed build.
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_orders_region")
        op.create_index(
            "ix_orders_region",
            "orders",
            ["region"],
            postgresql_concurrently=True,
        )


def downgrade():
    with op.get_context().autocommit_block():
        op.drop_index("ix_orders_region", table_name="orders", postgresql_concurrently=True)
