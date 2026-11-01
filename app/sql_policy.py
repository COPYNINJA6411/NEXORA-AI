"""AST-based read-only SQL policy for the NEXORA relational demo schema."""

from __future__ import annotations

import re

import sqlglot
from sqlglot import exp
from sqlalchemy import inspect

from app.database.connection import make_database_engine

MAX_RESULT_ROWS = 500
MAX_QUERY_CHARS = 12_000
SAFE_FUNCTIONS = {
    "ABS", "AVG", "CAST", "COALESCE", "COUNT", "DATE", "DATE_TRUNC", "EXTRACT",
    "LOWER", "MAX", "MIN", "NULLIF", "ROUND", "STRFTIME", "SUBSTR", "SUM", "UPPER",
}
BLOCKED_FUNCTIONS = {"LOAD_EXTENSION", "READFILE", "WRITEFILE", "PG_READ_FILE", "PG_SLEEP", "PG_LS_DIR", "DBLINK"}


class SQLPolicyError(ValueError):
    """A generated query violated the read-only query policy."""


def schema_allowlist(dataset_id: str | None = None) -> dict[str, set[str]]:
    engine = make_database_engine(dataset_id=dataset_id, read_only=True)
    try:
        inspector = inspect(engine)
        names = set(inspector.get_table_names())
        allowed_names = {
            "dataset_info", "regions", "customers", "products", "employees", "orders",
            "order_items", "sales_targets", "marketing_expenses", "synthetic_events",
        }
        return {
            table_name: {column["name"].lower() for column in inspector.get_columns(table_name)}
            for table_name in sorted(names & allowed_names)
        }
    finally:
        engine.dispose()


def validate_select(sql: str, *, dataset_id: str | None = None, row_limit: int = MAX_RESULT_ROWS) -> str:
    if not isinstance(sql, str) or not sql.strip():
        raise SQLPolicyError("Provide one SQL SELECT statement.")
    if len(sql) > MAX_QUERY_CHARS:
        raise SQLPolicyError("The SQL statement is too long.")
    if row_limit < 1 or row_limit > MAX_RESULT_ROWS:
        raise SQLPolicyError(f"The maximum result limit is {MAX_RESULT_ROWS} rows.")
    if re.search(r"(--|/\*|\*/|#)", sql):
        raise SQLPolicyError("SQL comments are not allowed.")
    engine = make_database_engine(dataset_id=dataset_id)
    try:
        dialect = "postgres" if engine.dialect.name == "postgresql" else "sqlite"
    finally:
        engine.dispose()
    try:
        trees = sqlglot.parse(sql, read=dialect)
    except sqlglot.errors.ParseError as exc:
        raise SQLPolicyError("SQL could not be parsed.") from exc
    trees = [tree for tree in trees if tree is not None]
    if len(trees) != 1 or not isinstance(trees[0], exp.Select):
        raise SQLPolicyError("Only a single SELECT statement is allowed.")
    tree = trees[0]
    if tree.args.get("with"):
        raise SQLPolicyError("Common table expressions are not enabled in this demo.")
    if any(list(tree.find_all(node_type)) for node_type in (exp.Subquery, exp.Into, exp.Command)):
        raise SQLPolicyError("Subqueries and command clauses are not allowed.")
    if any(not isinstance(star.parent, exp.Count) for star in tree.find_all(exp.Star)):
        raise SQLPolicyError("Wildcard selection is blocked; select named columns.")
    tables = list(tree.find_all(exp.Table))
    if not tables:
        raise SQLPolicyError("The query must read an approved business table.")
    allowed = schema_allowlist(dataset_id)
    table_names = set()
    aliases: dict[str, str] = {}
    for table in tables:
        if table.db or table.catalog:
            raise SQLPolicyError("Cross-schema and cross-database references are blocked.")
        name = table.name.lower()
        if name not in allowed:
            raise SQLPolicyError(f"Table '{table.name}' is not approved.")
        table_names.add(name)
        aliases[name] = name
        if table.alias:
            aliases[table.alias.lower()] = name
    valid_columns = set().union(*(allowed[name] for name in table_names))
    projection_aliases = {node.alias.lower() for node in tree.find_all(exp.Alias)}
    for column in tree.find_all(exp.Column):
        name = column.name.lower()
        if name == "*":
            if not isinstance(column.parent, exp.Count):
                raise SQLPolicyError("Wildcard selection is blocked; select named columns.")
            continue
        if name in projection_aliases:
            continue
        if column.table and aliases.get(column.table.lower()) not in table_names:
            raise SQLPolicyError(f"Unknown table alias '{column.table}'.")
        if name not in valid_columns:
            raise SQLPolicyError(f"Column '{column.name}' is not approved for the selected tables.")
    for function in tree.find_all(exp.Func):
        function_name = function.sql_name().upper()
        if isinstance(function, exp.Anonymous):
            function_name = function.name.upper()
            if function_name not in SAFE_FUNCTIONS:
                raise SQLPolicyError(f"Function '{function_name}' is not approved.")
        if function_name in BLOCKED_FUNCTIONS:
            raise SQLPolicyError(f"Function '{function_name}' is not approved.")
    limit = tree.args.get("limit")
    if limit is None:
        tree = tree.limit(row_limit)
    else:
        amount = limit.expression
        if not isinstance(amount, exp.Literal) or not amount.is_number:
            raise SQLPolicyError("LIMIT must be a numeric constant.")
        if int(amount.this) > row_limit:
            limit.set("expression", exp.Literal.number(row_limit))
    return tree.sql(dialect=dialect)
