# PostgreSQL DDL lock reference

Lock levels from weakest to strongest. Each level conflicts with every level
listed in its "blocks" column; `ACCESS EXCLUSIVE` conflicts with everything,
including plain `SELECT`.

| Level | Taken by (typical) | Blocks |
|---|---|---|
| `ACCESS SHARE` | `SELECT` | only `ACCESS EXCLUSIVE` |
| `ROW SHARE` | `SELECT ... FOR UPDATE/SHARE` | `EXCLUSIVE`, `ACCESS EXCLUSIVE` |
| `ROW EXCLUSIVE` | `INSERT`, `UPDATE`, `DELETE` | `SHARE` and above |
| `SHARE UPDATE EXCLUSIVE` | `VACUUM`, `ANALYZE`, `CREATE INDEX CONCURRENTLY`, `VALIDATE CONSTRAINT`, `ALTER TABLE ... SET (params)` | other DDL; not reads or writes |
| `SHARE` | `CREATE INDEX` (non-concurrent) | all writes |
| `SHARE ROW EXCLUSIVE` | `ADD FOREIGN KEY` (on both tables), `CREATE TRIGGER` | writes and `SHARE` |
| `EXCLUSIVE` | `REFRESH MATERIALIZED VIEW CONCURRENTLY` | everything except plain `SELECT` |
| `ACCESS EXCLUSIVE` | most `ALTER TABLE`, `DROP TABLE`, `TRUNCATE`, `VACUUM FULL`, `CLUSTER`, `REINDEX` (non-concurrent), `DROP INDEX` (non-concurrent) | everything, including `SELECT` |

## The lock queue is the real hazard

PostgreSQL grants locks in request order. If a transaction holds
`ACCESS SHARE` (a long-running `SELECT`, an idle-in-transaction session, a
report) and an `ALTER TABLE` requests `ACCESS EXCLUSIVE`, the `ALTER` waits.
Every `SELECT` that arrives after it also waits, because it conflicts with
the *pending* `ACCESS EXCLUSIVE` request. The table is effectively down
until the first transaction finishes.

`SET lock_timeout = '3s'` makes the `ALTER` give up instead of queueing.
Retry it; do not raise the timeout.

To see who is blocking whom while it happens:

```sql
SELECT pid, state, wait_event_type, now() - xact_start AS xact_age, left(query, 80)
FROM pg_stat_activity
WHERE cardinality(pg_blocking_pids(pid)) > 0 OR pid = ANY (SELECT unnest(pg_blocking_pids(pid)) FROM pg_stat_activity)
ORDER BY xact_start;
```

## ALTER TABLE forms: lock and duration

Lock level alone does not tell you the risk. An `ACCESS EXCLUSIVE` lock
held for 2 ms is harmless with a lock timeout. The second column is what
matters.

| Statement | Lock | Duration |
|---|---|---|
| `ADD COLUMN` (nullable, no default) | `ACCESS EXCLUSIVE` | metadata only |
| `ADD COLUMN ... DEFAULT <constant>` | `ACCESS EXCLUSIVE` | metadata only on PostgreSQL 11+; full rewrite before 11 |
| `ADD COLUMN ... DEFAULT <volatile>` (`now()`, `gen_random_uuid()`, `random()`) | `ACCESS EXCLUSIVE` | **full table rewrite**, every version |
| `ADD COLUMN ... NOT NULL` without default | `ACCESS EXCLUSIVE` | fails immediately on a non-empty table |
| `ALTER COLUMN ... SET NOT NULL` | `ACCESS EXCLUSIVE` | **full scan**, unless a valid `CHECK (col IS NOT NULL)` already exists (12+), then metadata only |
| `ALTER COLUMN ... DROP NOT NULL` | `ACCESS EXCLUSIVE` | metadata only |
| `ALTER COLUMN ... SET DEFAULT` / `DROP DEFAULT` | `ACCESS EXCLUSIVE` | metadata only |
| `ALTER COLUMN ... TYPE` (binary-compatible: `varchar(n)` to larger `n` or `text`, `numeric` precision increase, `timestamp` precision increase) | `ACCESS EXCLUSIVE` | metadata only |
| `ALTER COLUMN ... TYPE` (anything else: `int` to `bigint`, `text` to `jsonb`, `varchar` to `int`) | `ACCESS EXCLUSIVE` | **full rewrite plus index rebuild** |
| `ADD CONSTRAINT ... CHECK` | `ACCESS EXCLUSIVE` | **full scan** |
| `ADD CONSTRAINT ... CHECK ... NOT VALID` | `ACCESS EXCLUSIVE` | metadata only |
| `ADD CONSTRAINT ... FOREIGN KEY` | `SHARE ROW EXCLUSIVE` on both tables | **full scan** of the referencing table |
| `ADD CONSTRAINT ... FOREIGN KEY ... NOT VALID` | `SHARE ROW EXCLUSIVE` on both tables | metadata only |
| `VALIDATE CONSTRAINT` | `SHARE UPDATE EXCLUSIVE` (plus `ROW SHARE` on the referenced table) | full scan, but reads and writes continue |
| `ADD CONSTRAINT ... UNIQUE` | `ACCESS EXCLUSIVE` | **builds the index under the lock** |
| `ADD CONSTRAINT ... UNIQUE USING INDEX` | `ACCESS EXCLUSIVE` | metadata only (index already built) |
| `ADD CONSTRAINT ... PRIMARY KEY USING INDEX` | `ACCESS EXCLUSIVE` | metadata only, but implies `SET NOT NULL` scan unless already not null |
| `DROP COLUMN` | `ACCESS EXCLUSIVE` | metadata only (space reclaimed later by vacuum) |
| `DROP CONSTRAINT` | `ACCESS EXCLUSIVE` | metadata only |
| `RENAME COLUMN` / `RENAME TO` | `ACCESS EXCLUSIVE` | metadata only, but breaks running code (see patterns) |
| `SET (fillfactor = ...)` and other storage params | `SHARE UPDATE EXCLUSIVE` | metadata only |
| `ATTACH PARTITION` | `SHARE UPDATE EXCLUSIVE` on parent | scan of the new partition for the constraint, unless a matching `CHECK` exists |
| `DETACH PARTITION` | `ACCESS EXCLUSIVE` on parent (`CONCURRENTLY` on 14+ lowers this) | metadata only |

## Indexes

| Statement | Lock | Duration |
|---|---|---|
| `CREATE INDEX` | `SHARE` | full scan; **all writes blocked** for the whole build |
| `CREATE INDEX CONCURRENTLY` | `SHARE UPDATE EXCLUSIVE` | two full scans plus waiting for every transaction that started before each scan; reads and writes continue |
| `DROP INDEX` | `ACCESS EXCLUSIVE` | metadata only, but the lock request still queues |
| `DROP INDEX CONCURRENTLY` | `SHARE UPDATE EXCLUSIVE` | waits for in-flight transactions |
| `REINDEX` | `ACCESS EXCLUSIVE` on the index (`SHARE` on the table) | full rebuild |
| `REINDEX CONCURRENTLY` (12+) | `SHARE UPDATE EXCLUSIVE` | full rebuild, non-blocking |

`CREATE INDEX CONCURRENTLY` can fail and leave an `INVALID` index behind.
The index then costs write overhead and is never used. Always:

```sql
DROP INDEX CONCURRENTLY IF EXISTS ix_orders_customer_id;
CREATE INDEX CONCURRENTLY ix_orders_customer_id ON orders (customer_id);
```

and check afterwards:

```sql
SELECT indexrelid::regclass FROM pg_index WHERE NOT indisvalid;
```

Neither statement can run inside a transaction block. Alembic runs each
migration in one, so they need:

```python
with op.get_context().autocommit_block():
    op.create_index("ix_orders_customer_id", "orders", ["customer_id"],
                    postgresql_concurrently=True)
```

## Enums

| Statement | Notes |
|---|---|
| `ALTER TYPE ... ADD VALUE` | Cannot run inside a transaction block before 12. On 12+ it can, but the new value cannot be *used* in the same transaction. Use `autocommit_block()` either way. |
| `ALTER TYPE ... RENAME VALUE` | Transactional, metadata only (10+). |
| Removing a value | Not supported. Create a new type, `ALTER COLUMN ... TYPE new USING col::text::new` (full rewrite), drop the old type. |

## Other statements that surprise people

- `TRUNCATE`: `ACCESS EXCLUSIVE`, and not MVCC-safe; concurrent readers
  see an empty table once it commits.
- `VACUUM FULL` and `CLUSTER`: `ACCESS EXCLUSIVE` for a full rewrite. Use
  `pg_repack` instead on a live system.
- `UPDATE table SET col = x` with no `WHERE`: no DDL lock, but a row lock on
  every row, one giant transaction, massive WAL, and replication lag. Batch
  it.
- `CREATE TRIGGER`: `SHARE ROW EXCLUSIVE`, metadata only, but the trigger
  runs on every write from then on.
- `ANALYZE` after a big backfill or index build: `SHARE UPDATE EXCLUSIVE`,
  cheap, and without it the planner may ignore the new index.
