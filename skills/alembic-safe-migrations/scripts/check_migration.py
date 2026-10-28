#!/usr/bin/env python3
"""Static safety checker for Alembic migrations that target PostgreSQL.

It parses the Python source of migration files, looks at every ``op.*``
call and every SQL string passed to ``execute(...)``, and reports
operations that take a heavy lock for a long time, that will fail inside
a transaction, or that break a rolling deploy.

It knows nothing about your table sizes. Treat a warning on a 200-row
lookup table as noise and on a 200-million-row table as a blocker.

Usage::

    check_migration.py [--strict] [--ignore SM002,SM012] PATH [PATH ...]
    check_migration.py --list-rules

Exit status is 1 when any error is found (or any warning with ``--strict``),
0 otherwise. Pure standard library; Python 3.9+.
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

RULES: dict[str, tuple[str, str]] = {
    "SM001": ("error", "CONCURRENTLY inside a transaction: wrap it in op.get_context().autocommit_block()"),
    "SM002": ("warning", "create_index without postgresql_concurrently=True takes a SHARE lock and blocks writes for the whole build"),
    "SM003": ("error", "add_column nullable=False without server_default fails on a non-empty table"),
    "SM004": ("warning", "volatile server_default (now(), gen_random_uuid(), random()) on add_column rewrites the whole table; add the column first, then SET DEFAULT"),
    "SM005": ("warning", "alter_column nullable=False scans the table under ACCESS EXCLUSIVE; add CHECK (col IS NOT NULL) NOT VALID, VALIDATE it, then SET NOT NULL"),
    "SM006": ("warning", "alter_column type_= usually rewrites the table under ACCESS EXCLUSIVE; use expand/contract unless the change is binary-compatible"),
    "SM007": ("warning", "create_foreign_key without postgresql_not_valid=True scans the referencing table; add NOT VALID, then VALIDATE CONSTRAINT"),
    "SM008": ("warning", "create_unique_constraint builds the index under ACCESS EXCLUSIVE; build a unique index concurrently, then ADD CONSTRAINT ... USING INDEX"),
    "SM009": ("warning", "create_check_constraint without postgresql_not_valid=True scans the table under ACCESS EXCLUSIVE"),
    "SM010": ("info", "drop_column/drop_table: the code that stops using it must ship in the previous release"),
    "SM011": ("warning", "rename in place breaks the old code still running during a rolling deploy; use expand/contract"),
    "SM012": ("info", "upgrade() never sets lock_timeout; a blocked DDL will queue every later query behind it"),
    "SM013": ("warning", "UPDATE/DELETE without WHERE: one giant transaction and a row lock on every row; batch by primary-key range"),
    "SM014": ("warning", "raw CREATE INDEX without CONCURRENTLY blocks writes for the whole build"),
    "SM015": ("warning", "raw SET NOT NULL scans the table under ACCESS EXCLUSIVE; use CHECK ... NOT VALID + VALIDATE first"),
    "SM016": ("error", "ALTER TYPE ... ADD VALUE inside a transaction: wrap it in autocommit_block()"),
    "SM017": ("warning", "raw ALTER COLUMN ... TYPE usually rewrites the table under ACCESS EXCLUSIVE; use expand/contract"),
    "SM018": ("warning", "VACUUM FULL / CLUSTER / REINDEX / TRUNCATE hold ACCESS EXCLUSIVE for the whole operation"),
    "SM019": ("warning", "drop_index without postgresql_concurrently=True requests ACCESS EXCLUSIVE and queues behind running queries"),
    "SM020": ("warning", "raw ADD CONSTRAINT without NOT VALID (FK/CHECK) or USING INDEX (UNIQUE/PK) scans or builds under a heavy lock"),
    "SM021": ("warning", "autocommit_block mixed with transactional operations in one upgrade(): a failure half-way leaves the revision partly applied and unrecorded"),
}

VOLATILE_DEFAULTS = re.compile(
    r"\b(?:now|clock_timestamp|statement_timestamp|transaction_timestamp|"
    r"gen_random_uuid|uuid_generate_v\d|random|nextval)\s*\(|"
    r"\bcurrent_(?:timestamp|date|time)\b|\blocaltime(?:stamp)?\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    code: str

    @property
    def level(self) -> str:
        return RULES[self.code][0]

    @property
    def message(self) -> str:
        return RULES[self.code][1]

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.level} {self.code} {self.message}"


# --------------------------------------------------------------------------- #
# AST helpers
# --------------------------------------------------------------------------- #

def _attr_chain(node: ast.AST) -> str:
    """Return a dotted name for ``a.b.c`` / ``a.b.c(...)`` style nodes, else ''."""
    if isinstance(node, ast.Call):
        return _attr_chain(node.func)
    if isinstance(node, ast.Attribute):
        base = _attr_chain(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _op_name(call: ast.Call) -> Optional[str]:
    """``op.create_index(...)`` -> ``create_index``; anything else -> None."""
    func = call.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "op":
        return func.attr
    return None


def _is_autocommit_block(expr: ast.AST) -> bool:
    return isinstance(expr, ast.Call) and _attr_chain(expr).endswith("autocommit_block")


def _is_false(node: Optional[ast.AST]) -> bool:
    return isinstance(node, ast.Constant) and node.value is False


def _is_truthy_const(node: Optional[ast.AST]) -> bool:
    return isinstance(node, ast.Constant) and bool(node.value)


def _string_value(node: Optional[ast.AST]) -> Optional[str]:
    """Best-effort string from a Constant, f-string, concatenation or text(...) call."""
    if node is None:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else " ? "
            for v in node.values
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _string_value(node.left), _string_value(node.right)
        if left is not None or right is not None:
            return (left or "") + (right or "")
    if isinstance(node, ast.Call) and _attr_chain(node).split(".")[-1] == "text" and node.args:
        return _string_value(node.args[0])
    return None


def _is_volatile_default(node: ast.AST) -> bool:
    """True for sa.func.now(), sa.text("now()"), "CURRENT_TIMESTAMP" and friends."""
    if isinstance(node, ast.Call) and VOLATILE_DEFAULTS.search(_attr_chain(node) + "("):
        return True  # sa.func.now(), func.gen_random_uuid()
    text = _string_value(node)
    return bool(text and VOLATILE_DEFAULTS.search(text))


# --------------------------------------------------------------------------- #
# SQL helpers
# --------------------------------------------------------------------------- #

_SQL_PATTERNS = {
    "create_index": re.compile(r"\bCREATE\s+(?:UNIQUE\s+)?INDEX\b", re.I),
    "drop_index": re.compile(r"\bDROP\s+INDEX\b", re.I),
    "concurrently": re.compile(r"\bCONCURRENTLY\b", re.I),
    "enum_add_value": re.compile(r"\bALTER\s+TYPE\b.*\bADD\s+VALUE\b", re.I | re.S),
    "set_not_null": re.compile(r"\bSET\s+NOT\s+NULL\b", re.I),
    "alter_type": re.compile(r"\bALTER\s+COLUMN\b.*?\b(?:SET\s+DATA\s+)?TYPE\b", re.I | re.S),
    "heavy": re.compile(r"\b(?:VACUUM\s+FULL|CLUSTER|TRUNCATE)\b", re.I),
    "reindex": re.compile(r"\bREINDEX\b", re.I),
    "add_constraint": re.compile(r"\bADD\s+CONSTRAINT\b", re.I),
    "fk_or_check": re.compile(r"\b(?:FOREIGN\s+KEY|CHECK)\b", re.I),
    "unique_or_pk": re.compile(r"\b(?:UNIQUE|PRIMARY\s+KEY)\b", re.I),
    "not_valid": re.compile(r"\bNOT\s+VALID\b", re.I),
    "using_index": re.compile(r"\bUSING\s+INDEX\b", re.I),
    "dml": re.compile(r"^\s*(?:WITH\b.*?\))?\s*(?:UPDATE|DELETE\s+FROM)\s+\S+", re.I | re.S),
    "where": re.compile(r"\bWHERE\b", re.I),
    "lock_timeout": re.compile(r"\block_timeout\b", re.I),
    "set_stmt": re.compile(r"^\s*SET\b", re.I),
    "validate": re.compile(r"\bVALIDATE\s+CONSTRAINT\b", re.I),
    "alter_table": re.compile(r"\bALTER\s+TABLE\s+(?:ONLY\s+)?(?:IF\s+EXISTS\s+)?\"?([\w.]+)\"?", re.I),
}


def _table_of(stmt: str) -> Optional[str]:
    match = _SQL_PATTERNS["alter_table"].search(stmt)
    return match.group(1).lower() if match else None


def _split_statements(sql: str) -> list[str]:
    return [s for s in (part.strip() for part in sql.split(";")) if s]


# --------------------------------------------------------------------------- #
# Analyzer
# --------------------------------------------------------------------------- #

class _Analyzer(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.findings: list[Finding] = []
        self.autocommit_depth = 0
        self.in_upgrade = False
        self.saw_lock_timeout = False
        self.saw_autocommit = False
        self.saw_transactional = False
        # Tables that had a CHECK constraint validated earlier in the same
        # function: SET NOT NULL on them is metadata-only on PostgreSQL 12+.
        self.validated_tables: set[str] = set()

    # -- bookkeeping -------------------------------------------------------- #

    def _add(self, code: str, line: int) -> None:
        self.findings.append(Finding(self.path, line, code))

    def _transactional(self) -> None:
        if self.autocommit_depth == 0:
            self.saw_transactional = True

    # -- visitors ----------------------------------------------------------- #

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.validated_tables = set()
        if node.name != "upgrade":
            self.generic_visit(node)
            return
        self.in_upgrade = True
        self.saw_lock_timeout = self.saw_autocommit = self.saw_transactional = False
        self.generic_visit(node)
        if not self.saw_lock_timeout:
            self._add("SM012", node.lineno)
        if self.saw_autocommit and self.saw_transactional:
            self._add("SM021", node.lineno)
        self.in_upgrade = False

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_With(self, node: ast.With) -> None:
        is_auto = any(_is_autocommit_block(item.context_expr) for item in node.items)
        if is_auto:
            self.autocommit_depth += 1
            self.saw_autocommit = True
        self.generic_visit(node)
        if is_auto:
            self.autocommit_depth -= 1

    def visit_Call(self, node: ast.Call) -> None:
        name = _op_name(node)
        if name:
            self._check_op(name, node)
        elif isinstance(node.func, ast.Attribute) and node.func.attr == "execute":
            self._check_sql(node)
        self.generic_visit(node)

    # -- op.* checks -------------------------------------------------------- #

    def _check_op(self, name: str, node: ast.Call) -> None:
        kw = {k.arg: k.value for k in node.keywords if k.arg}
        line = node.lineno

        if name in ("create_index", "drop_index"):
            concurrently = _is_truthy_const(kw.get("postgresql_concurrently"))
            if concurrently and self.autocommit_depth == 0:
                self._add("SM001", line)
            if not concurrently:
                self._add("SM002" if name == "create_index" else "SM019", line)
                self._transactional()

        elif name == "add_column":
            self._transactional()
            column = node.args[1] if len(node.args) > 1 else kw.get("column")
            if isinstance(column, ast.Call):
                ckw = {k.arg: k.value for k in column.keywords if k.arg}
                default = ckw.get("server_default")
                if _is_false(ckw.get("nullable")) and default is None:
                    self._add("SM003", line)
                if default is not None and _is_volatile_default(default):
                    self._add("SM004", line)

        elif name == "alter_column":
            self._transactional()
            table = node.args[0] if node.args else kw.get("table_name")
            table_name = table.value.lower() if isinstance(table, ast.Constant) and isinstance(table.value, str) else None
            if _is_false(kw.get("nullable")) and table_name not in self.validated_tables:
                self._add("SM005", line)
            if "type_" in kw:
                self._add("SM006", line)
            if "new_column_name" in kw:
                self._add("SM011", line)

        elif name == "create_foreign_key":
            self._transactional()
            if not _is_truthy_const(kw.get("postgresql_not_valid")):
                self._add("SM007", line)

        elif name == "create_unique_constraint":
            self._transactional()
            self._add("SM008", line)

        elif name == "create_check_constraint":
            self._transactional()
            if not _is_truthy_const(kw.get("postgresql_not_valid")):
                self._add("SM009", line)

        elif name in ("drop_column", "drop_table"):
            self._transactional()
            self._add("SM010", line)

        elif name == "rename_table":
            self._transactional()
            self._add("SM011", line)

        elif name == "execute":
            self._check_sql(node)

        elif name in ("create_table", "create_primary_key", "drop_constraint", "bulk_insert"):
            self._transactional()

    # -- raw SQL checks ----------------------------------------------------- #

    def _check_sql(self, node: ast.Call) -> None:
        sql = _string_value(node.args[0]) if node.args else None
        if not sql:
            return
        line = node.lineno
        p = _SQL_PATTERNS
        for stmt in _split_statements(sql):
            if p["lock_timeout"].search(stmt):
                self.saw_lock_timeout = True
            if p["set_stmt"].match(stmt):
                continue  # SET ... is harmless and should not count as transactional work

            concurrently = bool(p["concurrently"].search(stmt))
            if p["create_index"].search(stmt):
                if concurrently and self.autocommit_depth == 0:
                    self._add("SM001", line)
                elif not concurrently:
                    self._add("SM014", line)
                    self._transactional()
                continue
            if p["drop_index"].search(stmt):
                if concurrently and self.autocommit_depth == 0:
                    self._add("SM001", line)
                elif not concurrently:
                    self._add("SM019", line)
                    self._transactional()
                continue
            if p["enum_add_value"].search(stmt):
                if self.autocommit_depth == 0:
                    self._add("SM016", line)
                continue
            if p["reindex"].search(stmt):
                if not concurrently:
                    self._add("SM018", line)
                    self._transactional()
                continue
            if p["heavy"].search(stmt):
                self._add("SM018", line)
                self._transactional()
                continue
            if p["validate"].search(stmt):
                table = _table_of(stmt)
                if table:
                    self.validated_tables.add(table)
                self._transactional()
                continue
            if p["set_not_null"].search(stmt):
                if _table_of(stmt) not in self.validated_tables:
                    self._add("SM015", line)
                self._transactional()
                continue
            if p["alter_type"].search(stmt):
                self._add("SM017", line)
                self._transactional()
                continue
            if p["add_constraint"].search(stmt):
                self._transactional()
                if p["fk_or_check"].search(stmt) and not p["not_valid"].search(stmt):
                    self._add("SM020", line)
                elif p["unique_or_pk"].search(stmt) and not p["using_index"].search(stmt):
                    self._add("SM020", line)
                continue
            if p["dml"].match(stmt):
                if not p["where"].search(stmt):
                    self._add("SM013", line)
                self._transactional()
                continue
            if re.match(r"^\s*(?:ALTER|DROP|CREATE)\b", stmt, re.I):
                self._transactional()


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def analyze_source(source: str, path: str = "<string>") -> list[Finding]:
    tree = ast.parse(source, filename=path)
    analyzer = _Analyzer(path)
    analyzer.visit(tree)
    return sorted(analyzer.findings, key=lambda f: (f.line, f.code))


def analyze_file(path: Path) -> list[Finding]:
    return analyze_source(path.read_text(encoding="utf-8"), str(path))


def iter_migration_files(paths: Iterable[Path]) -> Iterable[Path]:
    for path in paths:
        if path.is_dir():
            for candidate in sorted(path.rglob("*.py")):
                if candidate.name not in ("env.py", "__init__.py"):
                    yield candidate
        elif path.suffix == ".py":
            yield path


def run(paths: Iterable[Path], ignore: Iterable[str] = ()) -> list[Finding]:
    ignored = set(ignore)
    findings: list[Finding] = []
    for file in iter_migration_files(paths):
        findings.extend(f for f in analyze_file(file) if f.code not in ignored)
    return findings


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("paths", nargs="*", type=Path, help="migration files or directories (e.g. alembic/versions)")
    parser.add_argument("--strict", action="store_true", help="exit 1 on warnings as well as errors")
    parser.add_argument("--ignore", default="", help="comma-separated rule codes to suppress, e.g. SM012,SM010")
    parser.add_argument("--list-rules", action="store_true", help="print every rule and exit")
    args = parser.parse_args(argv)

    if args.list_rules:
        for code, (level, message) in RULES.items():
            print(f"{code}  {level:<7} {message}")
        return 0
    if not args.paths:
        parser.error("at least one path is required")

    ignore = [c.strip().upper() for c in args.ignore.split(",") if c.strip()]
    unknown = [c for c in ignore if c not in RULES]
    if unknown:
        parser.error(f"unknown rule code(s): {', '.join(unknown)}")

    findings = run(args.paths, ignore)
    for finding in findings:
        print(finding)

    errors = sum(1 for f in findings if f.level == "error")
    warnings = sum(1 for f in findings if f.level == "warning")
    infos = len(findings) - errors - warnings
    print(f"\n{errors} error(s), {warnings} warning(s), {infos} info", file=sys.stderr)

    if errors or (args.strict and warnings):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
