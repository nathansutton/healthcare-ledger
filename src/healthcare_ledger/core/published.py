"""The tables a reader sees: one per kind of thing the registries describe, declared by each source over its version
tables.

The version tables (``<stream>_version``) are the load's record of the files: one per file CMS publishes, with the
bookkeeping that lets a release prove it reproduces them (snapshot ids, hashes, ``row_seq``). A published table is
what those files describe, shaped for a researcher: one table per kind of record however many files CMS splits it
across (the two Provider of Services files are one ``facility`` table), the codes a reader would otherwise look up
decoded (``entity_type = 'individual'``), and the interval columns ``effective_date``, ``end_date`` and
``close_reason`` only.

Each is a SELECT over the version tables of its source (``Source.tables``). ``core.connect.open_catalog`` creates it
as a view, so the warehouse, the export, the graph and a notebook use the same names and columns; ``export`` writes
the views out, one hive-partitioned Parquet directory per table, and takes the column types for
``data_dictionary.csv`` from them.
"""

from __future__ import annotations

from dataclasses import dataclass

import duckdb

INTERVAL = ("effective_date", "end_date", "close_reason")


@dataclass(frozen=True)
class Table:
    name: str
    key: tuple[str, ...]            # with effective_date, unique: one row per key per version
    description: str                # one line, for the export README and the docs
    sql: str                        # SELECT over the version tables; its columns are ``columns``, in order
    columns: tuple[tuple[str, str, str], ...]     # (column, CMS header, description)

    @property
    def column_names(self) -> list[str]:
        return [c for c, _, _ in self.columns]


def interval_columns(key: str, key_header: str, key_desc: str, date_desc: str,
                     gone_desc: str) -> tuple[tuple[str, str, str], ...]:
    """The key and interval columns that lead every published table. ``date_desc`` names a file's date ("As-of date"),
    ``gone_desc`` what an ``absent`` version left ("the NPI left the file")."""
    return (
        (key, key_header, key_desc),
        ("effective_date", "", f"{date_desc} of the first file that showed this version (inclusive)"),
        ("end_date", "", f"{date_desc} of the first file that no longer showed it (exclusive); NULL while current"),
        ("close_reason", "", f"Why the version ended: 'changed' (a new version followed) or 'absent' ({gone_desc}); "
                             f"NULL while current"),
    )


def create_views(con: duckdb.DuckDBPyConnection, tables: list[Table]) -> None:
    """Each table as a temporary view of the connection, resolved through its ``search_path``."""
    for t in tables:
        con.execute(f"CREATE OR REPLACE TEMP VIEW {t.name} AS {t.sql}")


def dictionary_rows(con: duckdb.DuckDBPyConnection, t: Table) -> list[tuple[str, str, str, str, str]]:
    """(table, column, type, CMS header, description) of every column of the view ``t``; refuses a view whose columns
    are not the declared ones."""
    types = con.execute(f"SELECT column_name, column_type FROM (DESCRIBE {t.name})").fetchall()
    if [c for c, _ in types] != t.column_names:
        raise ValueError(f"{t.name}: the view's columns {[c for c, _ in types]} are not the declared {t.column_names}")
    return [(t.name, c, ty, hdr, desc) for (c, ty), (_, hdr, desc) in zip(types, t.columns)]
