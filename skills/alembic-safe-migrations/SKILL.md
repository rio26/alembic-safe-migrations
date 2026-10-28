---
name: alembic-safe-migrations
description: Write and review Alembic migrations that will not lock up a production PostgreSQL database. Use whenever a migration adds an index, adds a NOT NULL column, changes a column type, adds a foreign key or unique constraint, backfills data, renames anything, or touches a table that is large or hot. Also use to review an existing migration before it ships, and to explain why a migration hung in production.
---

# Alembic safe migrations (PostgreSQL)

A migration is "unsafe" when it takes a heavy lock for longer than a few
hundred milliseconds on a table the application is using. The lock itself
is not the problem; the *queue* behind it is. Once an `ACCESS EXCLUSIVE`
request is waiting, every later `SELECT` on that table queues behind it too,
so one slow DDL statement stalls the whole application.

Work through the four questions below for every migration. The reference
files hold the detail; the checker catches the common mistakes mechanically.

## 1. How big and how hot is the table?

Ask, or find out, before writing anything:

```sql
SELECT pg_size_pretty(pg_total_relation_size('public.my_table')),
       n_live_tup
FROM pg_stat_user_tables WHERE relname = 'my_table';
```

Rule of thumb: under ~100k rows and not written in a hot path, plain
Alembic ops are fine. Above that, or on any table the request path writes
to, every step below applies. When unsure, assume it is big.

## 2. Which lock does each statement take, and for how long?

Open `reference/postgres-locks.md` and classify every statement in the
migration. The three questions per statement:

- **Lock level**: `ACCESS EXCLUSIVE` blocks reads and writes; `SHARE`
  blocks writes; `SHARE UPDATE EXCLUSIVE` blocks only other DDL.
- **Duration**: metadata-only (milliseconds) or proportional to table size
  (a scan or a rewrite). Both can be `ACCESS EXCLUSIVE`; only the second
  one is dangerous.
- **Transactional**: `CREATE INDEX CONCURRENTLY`, `DROP INDEX CONCURRENTLY`
  and `ALTER TYPE ... ADD VALUE` cannot run inside a transaction block, and
  Alembic wraps each migration in one by default. They need
  `op.get_context().autocommit_block()`.

## 3. Rewrite each dangerous step as its safe pattern

`reference/patterns.md` has a recipe for every common case. The ones that
matter most:

| Intent | Unsafe | Safe |
|---|---|---|
| Add index | `op.create_index(...)` | `postgresql_concurrently=True` inside `autocommit_block()`, in its own migration |
| Add NOT NULL column | `nullable=False` with a default | add nullable, backfill in batches, `CHECK (col IS NOT NULL) NOT VALID`, `VALIDATE`, then `SET NOT NULL` |
| Backfill | one `UPDATE table SET ...` | batches by primary-key range, outside the migration transaction, idempotent |
| Add foreign key | `op.create_foreign_key(...)` | `postgresql_not_valid=True`, then `VALIDATE CONSTRAINT` in a later migration |
| Add unique constraint | `op.create_unique_constraint(...)` | unique index concurrently, then `ADD CONSTRAINT ... UNIQUE USING INDEX` |
| Change column type | `op.alter_column(type_=...)` | new column, dual-write, backfill, swap (expand/contract) |
| Rename column or table | `new_column_name=` | expand/contract across two deploys; never rename in place on a live table |
| Drop column | `op.drop_column` right away | ship code that stops reading it first, drop in the next release |

Always start `upgrade()` with a lock timeout so a blocked DDL fails fast
instead of building a queue:

```python
op.execute("SET lock_timeout = '3s'")
op.execute("SET statement_timeout = '0'")   # DDL may legitimately be slow once it has the lock
```

A migration that fails on `lock_timeout` is a success: rerun it. A
migration that waits is the outage.

## 4. Check the ordering against the rolling deploy

During a rolling deploy old and new application code run at the same time
against the same schema. Every migration must therefore be compatible with
**both** the previous and the next version of the code:

- Additive changes (new nullable column, new table, new index) go out
  **before** the code that uses them.
- Destructive changes (drop column, drop table, rename) go out **after**
  the code that stopped using them, in a later release.
- SQLAlchemy models that still list a dropped column break `SELECT` on the
  whole model, so the model change and the drop cannot ship together.

Put one autocommit operation per migration file. Mixing transactional and
non-transactional steps in one file means a failure half-way leaves the
database in a state Alembic cannot describe.

## Run the checker

```bash
python scripts/check_migration.py alembic/versions/
python scripts/check_migration.py --strict alembic/versions/2024_*.py   # warnings fail too
```

It is a static analyser over the Python source of migration files: it
parses `op.*` calls and raw SQL in `op.execute(...)`, and reports
`error` for things that will fail or stall, `warning` for things that take
a heavy lock on a big table, and `info` for ordering reminders. It cannot
know table sizes; treat warnings on tiny tables as noise and on big tables
as blockers. `--list-rules` prints every rule with its rationale.

## When reviewing someone else's migration

Report in this shape, one line per statement that matters:

```
L23 op.create_index(...)        SHARE lock, full scan  → concurrently + autocommit_block
L31 op.add_column(nullable=False) ACCESS EXCLUSIVE + rewrite → nullable, backfill, CHECK NOT VALID
L40 op.execute("UPDATE ...")    row locks on every row  → batch by id range, outside txn
```

Then state the deploy order (which release the code change goes in, which
release each migration goes in). A review that says "looks fine" without
the lock column has not been done.

## What this skill does not cover

MySQL, SQLite and SQL Server have different locking models; the patterns
here are PostgreSQL-specific. Partitioned tables, logical replication
slots and `pg_repack` are out of scope. Primary-key type widening
(`int` to `bigint`) is described in `reference/patterns.md` but is a
project in itself, not a migration.
