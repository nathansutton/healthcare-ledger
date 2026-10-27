"""SQL text helpers shared by the loaders."""

from __future__ import annotations

# Column kinds of core.columns.Column to DuckDB types: NPPES and PPEF use text/date/int, POS text/date/number.
SQL_TYPES = {"text": "VARCHAR", "date": "DATE", "int": "INTEGER", "number": "DOUBLE"}


def q(ident: str) -> str:
    """Quote an identifier (CMS headers carry spaces, parentheses and quotes)."""
    return '"' + ident.replace('"', '""') + '"'


def sql_type(kind: str) -> str:
    return SQL_TYPES[kind]


def ymd(snapshot_id: int) -> str:
    """Snapshot id ``YYYYMMDD`` -> ISO date text ``YYYY-MM-DD``."""
    return f"{snapshot_id // 10000:04d}-{snapshot_id // 100 % 100:02d}-{snapshot_id % 100:02d}"
