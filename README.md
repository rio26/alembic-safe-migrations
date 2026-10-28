# alembic-safe-migrations

An agent skill that stops Alembic migrations from locking up a production
PostgreSQL database, plus a static checker you can run in CI.

Most migration outages are not the DDL being slow. They are an `ALTER TABLE`
waiting for a lock while every query that arrives after it queues behind it.
This skill teaches your coding agent (Claude Code, Codex, Cursor, or anything
that reads `SKILL.md`) which statements take which lock for how long, and the
pattern that avoids each one. The checker catches the mistakes mechanically.

```
$ python skills/alembic-safe-migrations/scripts/check_migration.py examples/risky_migration.py
examples/risky_migration.py:14: info SM012 upgrade() never sets lock_timeout; a blocked DDL will queue every later query behind it
examples/risky_migration.py:16: error SM003 add_column nullable=False without server_default fails on a non-empty table
examples/risky_migration.py:18: warning SM004 volatile server_default (now(), gen_random_uuid(), random()) on add_column rewrites the whole table; add the column first, then SET DEFAULT
examples/risky_migration.py:20: warning SM002 create_index without postgresql_concurrently=True takes a SHARE lock and blocks writes for the whole build
examples/risky_migration.py:22: warning SM007 create_foreign_key without postgresql_not_valid=True scans the referencing table; add NOT VALID, then VALIDATE CONSTRAINT
examples/risky_migration.py:24: warning SM013 UPDATE/DELETE without WHERE: one giant transaction and a row lock on every row; batch by primary-key range
examples/risky_migration.py:26: warning SM005 alter_column nullable=False scans the table under ACCESS EXCLUSIVE; add CHECK (col IS NOT NULL) NOT VALID, VALIDATE it, then SET NOT NULL
examples/risky_migration.py:31: warning SM019 drop_index without postgresql_concurrently=True requests ACCESS EXCLUSIVE and queues behind running queries
examples/risky_migration.py:32: info SM010 drop_column/drop_table: the code that stops using it must ship in the previous release
examples/risky_migration.py:33: info SM010 drop_column/drop_table: the code that stops using it must ship in the previous release

1 error(s), 6 warning(s), 3 info
```

## What the skill knows

- **Lock levels and durations** for every `ALTER TABLE` form, index operation and
  enum change ([reference/postgres-locks.md](skills/alembic-safe-migrations/reference/postgres-locks.md)).
  The point it hammers on: `ACCESS EXCLUSIVE` for 2 ms is fine, `ACCESS EXCLUSIVE`
  for a table scan is an outage, and the lock *queue* is what takes the app down.
- **Twelve safe patterns** with working Alembic code
  ([reference/patterns.md](skills/alembic-safe-migrations/reference/patterns.md)):
  concurrent indexes in `autocommit_block()`, NOT NULL via `CHECK ... NOT VALID`
  then `VALIDATE`, batched backfills by primary-key range, foreign keys with
  `NOT VALID`, unique constraints `USING INDEX`, expand/contract for type changes
  and renames, enum values, and deploy ordering for rolling releases.
- **A review format**: one line per statement with its lock, duration and fix,
  then the release order. "Looks fine" is not a review.

## Install

**Claude Code**, as a plugin from this repository's marketplace:

```
/plugin marketplace add rio26/alembic-safe-migrations
/plugin install alembic-safe-migrations@rio26
```

**Claude Code**, as a plain project skill (no plugin system needed):

```bash
git clone https://github.com/rio26/alembic-safe-migrations /tmp/asm
cp -r /tmp/asm/skills/alembic-safe-migrations .claude/skills/
```

**Codex, Cursor, Gemini CLI and other agents** that follow the
[Agent Skills](https://agentskills.io) layout:

```bash
npx skills add rio26/alembic-safe-migrations
```

Then ask the agent to "write a migration that adds an index on orders.customer_id"
or "review alembic/versions/0042_add_region.py before it ships". The skill
triggers on its own whenever a migration touches an index, a NOT NULL column, a
type, a constraint, a backfill or a rename.

## The checker on its own

It is one file, standard library only, Python 3.9+. Copy it anywhere.

```bash
python skills/alembic-safe-migrations/scripts/check_migration.py alembic/versions/
python skills/alembic-safe-migrations/scripts/check_migration.py --strict --ignore SM012 alembic/versions/
python skills/alembic-safe-migrations/scripts/check_migration.py --list-rules
```

Exit status is 1 on any `error`, or on any `warning` with `--strict`.

As a pre-commit hook:

```yaml
- repo: local
  hooks:
    - id: alembic-safe-migrations
      name: alembic safe migrations
      entry: python tools/check_migration.py --strict
      language: system
      files: ^alembic/versions/.*\.py$
```

| Code | Level | Catches |
|---|---|---|
| SM001 | error | `CONCURRENTLY` (index or enum) outside `autocommit_block()` |
| SM002 / SM019 | warning | `create_index` / `drop_index` without `postgresql_concurrently=True` |
| SM003 | error | `add_column(nullable=False)` with no `server_default` |
| SM004 | warning | volatile `server_default` such as `now()` or `gen_random_uuid()` |
| SM005 / SM015 | warning | `SET NOT NULL` without a validated `CHECK` first |
| SM006 / SM017 | warning | column type change (table rewrite) |
| SM007 / SM009 / SM020 | warning | FK or CHECK constraint without `NOT VALID`, UNIQUE without `USING INDEX` |
| SM008 | warning | `create_unique_constraint` (builds the index under `ACCESS EXCLUSIVE`) |
| SM010 | info | `drop_column` / `drop_table` deploy-order reminder |
| SM011 | warning | rename in place |
| SM012 | info | `upgrade()` never sets `lock_timeout` |
| SM013 | warning | `UPDATE` / `DELETE` without `WHERE` |
| SM014 | warning | raw `CREATE INDEX` without `CONCURRENTLY` |
| SM016 | error | `ALTER TYPE ... ADD VALUE` inside a transaction |
| SM018 | warning | `VACUUM FULL`, `CLUSTER`, `REINDEX`, `TRUNCATE` |
| SM021 | warning | `autocommit_block()` mixed with transactional work in one file |

The checker recognises the safe NOT NULL pattern: once `VALIDATE CONSTRAINT`
has run on a table in the same function, `SET NOT NULL` on it is not flagged.

It does not know your table sizes. A warning on a 200-row lookup table is
noise; the same warning on a 200-million-row table is a blocker.

## Scope

PostgreSQL only. MySQL, SQLite and SQL Server lock differently and none of the
patterns transfer. Partitioned tables, logical replication and `pg_repack` are
out of scope. Widening a primary key from `int` to `bigint` is described but is
a project, not a migration.

## Development

```bash
python -m pytest tests/
```

Contributions that add a rule need a test for the positive and the negative
case, and a line in the table above.

## License

MIT
