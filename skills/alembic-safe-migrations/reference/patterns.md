# Safe migration patterns for Alembic on PostgreSQL

Every recipe assumes the migration starts with a lock timeout:

```python
def upgrade():
    op.execute("SET lock_timeout = '3s'")
    ...
```

Put it in a helper in `alembic/env.py` or a shared module if you prefer,
but it must run inside the migration transaction (session-level `SET`
inside a transaction is scoped to that transaction).

## 1. Add an index

```python
# One migration file, nothing else in it.
def upgrade():
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_orders_customer_id")  # clears a failed earlier attempt
        op.create_index(
            "ix_orders_customer_id", "orders", ["customer_id"],
            postgresql_concurrently=True,
        )

def downgrade():
    with op.get_context().autocommit_block():
        op.drop_index("ix_orders_customer_id", table_name="orders",
                      postgresql_concurrently=True)
```

Why its own file: an `autocommit_block` commits the surrounding transaction.
Anything before it in the same file is committed even if the index build
fails, and Alembic will not record the revision, so the next run re-executes
the earlier statements.

## 2. Add a NOT NULL column to a big table

Four steps, usually across three migration files and two deploys.

```python
# Migration A: add the column nullable. Metadata only.
op.add_column("orders", sa.Column("region", sa.Text(), nullable=True))
```

Deploy code that writes `region` on every insert and update.

```python
# Migration B: backfill in batches. Runs outside the migration transaction.
def upgrade():
    conn = op.get_bind()
    with op.get_context().autocommit_block():
        last_id = 0
        while True:
            row = conn.execute(sa.text("""
                WITH batch AS (
                    SELECT id FROM orders
                    WHERE id > :last_id AND region IS NULL
                    ORDER BY id LIMIT 5000
                )
                UPDATE orders o SET region = 'unknown'
                FROM batch WHERE o.id = batch.id
                RETURNING o.id
            """), {"last_id": last_id}).fetchall()
            if not row:
                break
            last_id = max(r[0] for r in row)
            time.sleep(0.05)   # give the WAL sender and autovacuum some air
```

Batches by primary-key range, not `OFFSET`; `OFFSET` rescans from the
start every time. Each batch is its own transaction, so a failure leaves
the rows done so far done, and the loop is idempotent (`region IS NULL`).

```python
# Migration C: enforce NOT NULL without a scan under ACCESS EXCLUSIVE.
def upgrade():
    op.execute("SET lock_timeout = '3s'")
    op.create_check_constraint(
        "ck_orders_region_not_null", "orders", "region IS NOT NULL",
        postgresql_not_valid=True,                 # metadata only
    )
    op.execute("ALTER TABLE orders VALIDATE CONSTRAINT ck_orders_region_not_null")  # scan, SHARE UPDATE EXCLUSIVE
    op.alter_column("orders", "region", nullable=False)   # 12+: sees the valid CHECK, no scan
    op.drop_constraint("ck_orders_region_not_null", "orders")
```

If you need a default for new rows, add it as a constant `server_default`
in Migration A (metadata only on 11+). Never a volatile default such as
`now()` or `gen_random_uuid()` on `add_column`: that rewrites the table.
Set the volatile default with `ALTER COLUMN ... SET DEFAULT` after the
column exists (metadata only), then backfill.

## 3. Backfill data

See Migration B above. The rules:

- Batch by primary key range, 1k to 10k rows per batch depending on row width.
- Each batch its own transaction (`autocommit_block`), with a short sleep.
- Idempotent predicate (`WHERE col IS NULL`), so reruns are safe.
- Never in the same transaction as DDL on the same table.
- For very large tables, run the backfill as a separate job (a script or a
  one-off task), not as an Alembic migration at all; migrations should
  finish in minutes.
- `ANALYZE` the table afterwards if the filled column will be queried.

## 4. Add a foreign key

```python
def upgrade():
    op.execute("SET lock_timeout = '3s'")
    op.create_foreign_key(
        "fk_orders_customer_id", "orders", "customers",
        ["customer_id"], ["id"],
        postgresql_not_valid=True,     # SHARE ROW EXCLUSIVE on both tables, but no scan
    )

# Next migration (or the same one if the table is small):
    op.execute("ALTER TABLE orders VALIDATE CONSTRAINT fk_orders_customer_id")
```

`NOT VALID` still enforces the constraint for new and updated rows; it only
skips checking existing rows. Make sure an index exists on `orders.customer_id`
first, or every delete on `customers` will scan `orders`.

## 5. Add a unique constraint

```python
# Migration 1: build the index without blocking writes.
with op.get_context().autocommit_block():
    op.create_index("uq_users_email", "users", ["email"],
                    unique=True, postgresql_concurrently=True)

# Migration 2: promote it to a constraint. Metadata only.
op.execute("SET lock_timeout = '3s'")
op.execute("ALTER TABLE users ADD CONSTRAINT uq_users_email UNIQUE USING INDEX uq_users_email")
```

If duplicates exist the concurrent build fails and leaves an invalid
index; drop it, clean the data, retry.

## 6. Change a column type (expand / contract)

Binary-compatible changes (`varchar(50)` to `varchar(100)` or `text`,
`numeric(10,2)` to `numeric(12,2)`) are metadata only; just do them with
a lock timeout. Everything else rewrites the table and must go through
expand/contract:

1. **Expand**: add `amount_cents BIGINT NULL`.
2. Deploy code that writes both `amount` and `amount_cents`, reads `amount`.
3. Backfill `amount_cents` in batches.
4. Deploy code that reads `amount_cents`, still writes both.
5. **Contract**: deploy code that only uses `amount_cents`; next release,
   drop `amount`.

The same shape works for `int` to `bigint` on a non-key column. For a
primary key it also needs a trigger to keep the new column in sync during
the backfill, a concurrent unique index on the new column, and a final
`ACCESS EXCLUSIVE` swap of the constraint. Plan that as a project, with a
rehearsal on a copy of production.

## 7. Rename a column or table

An in-place `RENAME` is metadata only, but every running instance of the
old code breaks the moment it commits, and the new code breaks until it
commits. On a rolling deploy there is no moment when only one version
runs. Use expand/contract (pattern 6) with a trigger or dual-write, or
accept a short maintenance window. Views can hide a table rename:
`CREATE VIEW old_name AS SELECT * FROM new_name` keeps old readers alive.

## 8. Drop a column or table

1. Release N: remove every reference from the code and the SQLAlchemy
   model. A model that still lists the column makes every `SELECT` on it
   fail once the column is gone.
2. Release N+1: `op.drop_column(...)`, with a lock timeout.

Dropping is metadata only; disk space comes back on the next vacuum.
Keep the downgrade honest: re-adding the column cannot restore its data,
so say so in the docstring.

## 9. Add an enum value

```python
def upgrade():
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE order_status ADD VALUE IF NOT EXISTS 'refunded'")
```

Its own migration file. Downgrade cannot remove a value; the honest
downgrade is a no-op with a comment, or a full type swap if the value must
go.

## 10. Add a column with a default

```python
# Constant default: metadata only on 11+.
op.add_column("orders", sa.Column("priority", sa.Integer(), nullable=False, server_default="0"))

# Volatile default: NEVER on add_column.
op.add_column("orders", sa.Column("created_at", sa.DateTime(timezone=True), nullable=True))
op.execute("ALTER TABLE orders ALTER COLUMN created_at SET DEFAULT now()")   # metadata only
# then backfill in batches and go through pattern 2 for NOT NULL.
```

Note that `nullable=False` with a constant `server_default` is safe on
11+: PostgreSQL stores the default in the catalog and fills it on read.

## 11. Splitting migrations and ordering releases

- One `autocommit_block` operation per file.
- Additive migrations before the code that needs them; destructive
  migrations after the code that stopped needing them.
- Never put a backfill that might take more than a few minutes in the
  deploy path; run it as a job.
- Write the downgrade so it runs, even when it is a documented no-op.
- Rehearse against a restored copy of production for anything that
  touches a table over a few million rows, and time it.

## 12. Verifying before and after

```sql
-- Will this index be used? Run the real query:
EXPLAIN (ANALYZE, BUFFERS) SELECT ... ;

-- Invalid indexes left by failed concurrent builds:
SELECT indexrelid::regclass FROM pg_index WHERE NOT indisvalid;

-- Constraints still NOT VALID (forgotten VALIDATE):
SELECT conrelid::regclass, conname FROM pg_constraint WHERE NOT convalidated;

-- Table and index sizes:
SELECT relname, pg_size_pretty(pg_total_relation_size(oid))
FROM pg_class WHERE relkind IN ('r', 'i') ORDER BY pg_total_relation_size(oid) DESC LIMIT 20;
```
