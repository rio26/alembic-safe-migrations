"""Tests for scripts/check_migration.py.

Each test feeds a small migration to the analyzer and asserts on the rule
codes it emits. They run with plain pytest and need no database.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "alembic-safe-migrations" / "scripts"))

import check_migration as cm  # noqa: E402

HEADER = """
import sqlalchemy as sa
from alembic import op

def upgrade():
    op.execute("SET lock_timeout = '3s'")
"""


def codes(body: str, header: str = HEADER) -> list[str]:
    source = header + "\n".join("    " + line for line in body.strip("\n").splitlines()) + "\n"
    return [f.code for f in cm.analyze_source(source)]


# -- indexes ---------------------------------------------------------------- #

def test_concurrent_index_outside_autocommit_is_an_error():
    assert "SM001" in codes('op.create_index("ix", "t", ["c"], postgresql_concurrently=True)')


def test_concurrent_index_inside_autocommit_is_clean():
    body = """
with op.get_context().autocommit_block():
    op.create_index("ix", "t", ["c"], postgresql_concurrently=True)
"""
    assert codes(body) == []


def test_plain_create_index_warns():
    assert codes('op.create_index("ix", "t", ["c"])') == ["SM002"]


def test_plain_drop_index_warns():
    assert codes('op.drop_index("ix", table_name="t")') == ["SM019"]


def test_raw_create_index_without_concurrently_warns():
    assert "SM014" in codes('op.execute("CREATE INDEX ix ON t (c)")')


def test_raw_concurrent_index_outside_autocommit_is_an_error():
    assert "SM001" in codes('op.execute("CREATE INDEX CONCURRENTLY ix ON t (c)")')


# -- columns ---------------------------------------------------------------- #

def test_not_null_column_without_default_is_an_error():
    assert "SM003" in codes('op.add_column("t", sa.Column("c", sa.Text(), nullable=False))')


def test_not_null_column_with_constant_default_is_fine():
    assert codes('op.add_column("t", sa.Column("c", sa.Integer(), nullable=False, server_default="0"))') == []


@pytest.mark.parametrize(
    "default",
    ['sa.text("now()")', "sa.func.now()", 'sa.text("gen_random_uuid()")', '"CURRENT_TIMESTAMP"'],
)
def test_volatile_default_warns(default):
    body = f'op.add_column("t", sa.Column("c", sa.DateTime(), server_default={default}))'
    assert "SM004" in codes(body)


def test_constant_string_default_is_not_volatile():
    assert codes('op.add_column("t", sa.Column("c", sa.Text(), server_default="pending"))') == []


def test_alter_column_set_not_null_warns():
    assert codes('op.alter_column("t", "c", nullable=False)') == ["SM005"]


def test_alter_column_drop_not_null_is_fine():
    assert codes('op.alter_column("t", "c", nullable=True)') == []


def test_alter_column_type_warns():
    assert codes('op.alter_column("t", "c", type_=sa.BigInteger())') == ["SM006"]


def test_rename_column_warns():
    assert codes('op.alter_column("t", "c", new_column_name="d")') == ["SM011"]


def test_rename_table_warns():
    assert codes('op.rename_table("a", "b")') == ["SM011"]


def test_drop_column_is_info_only():
    findings = cm.analyze_source(HEADER + '    op.drop_column("t", "c")\n')
    assert [(f.code, f.level) for f in findings] == [("SM010", "info")]


# -- constraints ------------------------------------------------------------ #

def test_foreign_key_without_not_valid_warns():
    assert codes('op.create_foreign_key("fk", "a", "b", ["b_id"], ["id"])') == ["SM007"]


def test_foreign_key_with_not_valid_is_fine():
    assert codes('op.create_foreign_key("fk", "a", "b", ["b_id"], ["id"], postgresql_not_valid=True)') == []


def test_unique_constraint_warns():
    assert codes('op.create_unique_constraint("uq", "t", ["c"])') == ["SM008"]


def test_check_constraint_without_not_valid_warns():
    assert codes('op.create_check_constraint("ck", "t", "c > 0")') == ["SM009"]


def test_check_constraint_with_not_valid_is_fine():
    assert codes('op.create_check_constraint("ck", "t", "c > 0", postgresql_not_valid=True)') == []


@pytest.mark.parametrize(
    "sql, expected",
    [
        ("ALTER TABLE a ADD CONSTRAINT fk FOREIGN KEY (b_id) REFERENCES b (id)", ["SM020"]),
        ("ALTER TABLE a ADD CONSTRAINT fk FOREIGN KEY (b_id) REFERENCES b (id) NOT VALID", []),
        ("ALTER TABLE a ADD CONSTRAINT uq UNIQUE (c)", ["SM020"]),
        ("ALTER TABLE a ADD CONSTRAINT uq UNIQUE USING INDEX uq_idx", []),
        ("ALTER TABLE a VALIDATE CONSTRAINT fk", []),
    ],
)
def test_raw_add_constraint(sql, expected):
    assert codes(f'op.execute("{sql}")') == expected


# -- raw SQL ---------------------------------------------------------------- #

def test_unbatched_update_warns():
    assert "SM013" in codes('op.execute("UPDATE t SET c = 1")')


def test_update_with_where_is_fine():
    assert codes('op.execute("UPDATE t SET c = 1 WHERE id BETWEEN 1 AND 5000")') == []


def test_cte_update_with_where_is_fine():
    sql = "WITH batch AS (SELECT id FROM t WHERE c IS NULL LIMIT 5000) UPDATE t SET c = 1 FROM batch WHERE t.id = batch.id"
    assert codes(f'op.execute("{sql}")') == []


def test_delete_without_where_warns():
    assert "SM013" in codes('op.execute("DELETE FROM t")')


def test_sql_via_text_wrapper_is_inspected():
    assert "SM013" in codes('op.execute(sa.text("UPDATE t SET c = 1"))')


def test_connection_execute_is_inspected_too():
    body = """
conn = op.get_bind()
conn.execute(sa.text("UPDATE t SET c = 1"))
"""
    assert "SM013" in codes(body)


def test_raw_set_not_null_warns():
    assert codes('op.execute("ALTER TABLE t ALTER COLUMN c SET NOT NULL")') == ["SM015"]


def test_set_not_null_after_validate_on_same_table_is_fine():
    body = """
op.create_check_constraint("ck", "t", "c IS NOT NULL", postgresql_not_valid=True)
op.execute("ALTER TABLE t VALIDATE CONSTRAINT ck")
op.alter_column("t", "c", nullable=False)
op.execute("ALTER TABLE t ALTER COLUMN c SET NOT NULL")
"""
    assert codes(body) == []


def test_validate_on_another_table_does_not_excuse_set_not_null():
    body = """
op.execute("ALTER TABLE other VALIDATE CONSTRAINT ck")
op.alter_column("t", "c", nullable=False)
"""
    assert codes(body) == ["SM005"]


def test_validate_does_not_leak_into_the_next_function():
    source = """
import sqlalchemy as sa
from alembic import op

def upgrade():
    op.execute("SET lock_timeout = '3s'")
    op.execute("ALTER TABLE t VALIDATE CONSTRAINT ck")

def downgrade():
    op.alter_column("t", "c", nullable=False)
"""
    assert "SM005" in [f.code for f in cm.analyze_source(source)]


def test_raw_alter_type_warns():
    assert codes('op.execute("ALTER TABLE t ALTER COLUMN c TYPE bigint")') == ["SM017"]


def test_enum_add_value_outside_autocommit_is_an_error():
    assert "SM016" in codes("op.execute(\"ALTER TYPE status ADD VALUE 'refunded'\")")


def test_enum_add_value_inside_autocommit_is_fine():
    body = """
with op.get_context().autocommit_block():
    op.execute("ALTER TYPE status ADD VALUE IF NOT EXISTS 'refunded'")
"""
    assert codes(body) == []


@pytest.mark.parametrize("sql", ["VACUUM FULL t", "CLUSTER t USING ix", "TRUNCATE t", "REINDEX TABLE t"])
def test_heavy_maintenance_statements_warn(sql):
    assert codes(f'op.execute("{sql}")') == ["SM018"]


def test_reindex_concurrently_is_fine():
    assert codes('op.execute("REINDEX INDEX CONCURRENTLY ix")') == []


def test_multiple_statements_in_one_execute_are_each_checked():
    sql = "CREATE INDEX a ON t (x); UPDATE t SET y = 1"
    assert sorted(codes(f'op.execute("{sql}")')) == ["SM013", "SM014"]


# -- structure --------------------------------------------------------------- #

def test_missing_lock_timeout_is_reported_once_for_upgrade():
    source = """
from alembic import op

def upgrade():
    op.add_column("t", sa.Column("c", sa.Text()))

def downgrade():
    op.drop_column("t", "c")
"""
    found = [f.code for f in cm.analyze_source(source)]
    assert found.count("SM012") == 1
    assert "SM010" in found  # downgrade bodies are still inspected


def test_lock_timeout_via_text_wrapper_counts():
    source = """
import sqlalchemy as sa
from alembic import op

def upgrade():
    op.execute(sa.text("SET LOCAL lock_timeout = '2s'"))
    op.add_column("t", sa.Column("c", sa.Text()))
"""
    assert "SM012" not in [f.code for f in cm.analyze_source(source)]


def test_mixing_autocommit_and_transactional_work_warns():
    body = """
op.add_column("t", sa.Column("c", sa.Text()))
with op.get_context().autocommit_block():
    op.create_index("ix", "t", ["c"], postgresql_concurrently=True)
"""
    assert codes(body) == ["SM021"]


def test_findings_are_sorted_by_line():
    body = """
op.create_unique_constraint("uq", "t", ["c"])
op.create_index("ix", "t", ["c"])
"""
    findings = cm.analyze_source(HEADER + "\n".join("    " + l for l in body.strip().splitlines()))
    assert [f.line for f in findings] == sorted(f.line for f in findings)


# -- CLI --------------------------------------------------------------------- #

def test_cli_walks_directories_and_skips_env(tmp_path, capsys):
    versions = tmp_path / "versions"
    versions.mkdir()
    (versions / "001_bad.py").write_text(HEADER + '    op.create_index("ix", "t", ["c"])\n')
    (versions / "env.py").write_text('op.execute("TRUNCATE everything")\n')
    code = cm.main([str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0  # SM002 is a warning
    assert "001_bad.py" in out and "SM002" in out
    assert "env.py" not in out


def test_cli_strict_fails_on_warnings(tmp_path):
    f = tmp_path / "m.py"
    f.write_text(HEADER + '    op.create_index("ix", "t", ["c"])\n')
    assert cm.main([str(f)]) == 0
    assert cm.main(["--strict", str(f)]) == 1


def test_cli_errors_fail_without_strict(tmp_path):
    f = tmp_path / "m.py"
    f.write_text(HEADER + '    op.add_column("t", sa.Column("c", sa.Text(), nullable=False))\n')
    assert cm.main([str(f)]) == 1


def test_cli_ignore_suppresses_rules(tmp_path):
    f = tmp_path / "m.py"
    f.write_text(HEADER + '    op.add_column("t", sa.Column("c", sa.Text(), nullable=False))\n')
    assert cm.main(["--ignore", "SM003", str(f)]) == 0


def test_cli_rejects_unknown_rule(tmp_path):
    with pytest.raises(SystemExit):
        cm.main(["--ignore", "SM999", str(tmp_path)])


def test_list_rules_prints_every_code(capsys):
    assert cm.main(["--list-rules"]) == 0
    out = capsys.readouterr().out
    for code in cm.RULES:
        assert code in out


# -- shipped examples -------------------------------------------------------- #

def test_risky_example_is_rejected():
    findings = cm.analyze_file(ROOT / "examples" / "risky_migration.py")
    assert any(f.level == "error" for f in findings)
    assert len(findings) >= 6


def test_safe_examples_have_no_errors_or_warnings():
    for name in ("safe_add_index.py", "safe_not_null_column.py"):
        findings = cm.analyze_file(ROOT / "examples" / name)
        assert [f.code for f in findings if f.level != "info"] == [], name
