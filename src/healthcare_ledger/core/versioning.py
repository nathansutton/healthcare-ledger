"""Row and version hashing and content fingerprints, the same for every SCD Type 2 source.

A row hash covers every data column in schema order; a key's version hash covers the sorted multiset
of its row hashes; a stream's content fingerprint covers the (key, version_hash) set of one snapshot
(docs/METHODOLOGY.md sections 3 and 5). The fingerprint is stored at load time from the state table
and recomputed at export by interval lookup over the version table, and the two must agree.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

import duckdb

from .sql import ymd

if TYPE_CHECKING:
    from ..schema import Column

SEP = "chr(31)"  # ASCII unit separator between fields inside the row hash


def meta_columns(key: str) -> list[tuple[str, str]]:
    """(name, type) of the interval and version columns that lead every ``{stream}_version`` table."""
    return [
        (key, "VARCHAR"), ("effective_date", "DATE"), ("end_date", "DATE"), ("is_current", "BOOLEAN"),
        ("first_snapshot_id", "INTEGER"), ("last_snapshot_id", "INTEGER"), ("close_reason", "VARCHAR"),
        ("version_hash", "VARCHAR"), ("row_seq", "INTEGER"),
    ]


def fingerprint_sql(rows: str) -> str:
    """SQL for the content fingerprint of a set of (npi, version_hash) rows.

    ``rows`` is a table name or a parenthesised subquery with ``npi`` and ``version_hash``
    columns, one row per NPI. The fingerprint is md5 over the lines ``npi:version_hash``
    joined by newline in NPI order; an empty set fingerprints as md5('').
    """
    return f"SELECT md5(coalesce(string_agg(npi || ':' || version_hash, chr(10) ORDER BY npi), '')) FROM {rows}"


def row_hash_expr(columns: Iterable[Column]) -> str:
    """The text the row hash is the md5 of.

    concat_ws over every baseline column is a single pass, and coalesce keeps NULL positions stable.
    Columns flagged hash_optional (added after the baseline) are appended as chr(30) || 'name=' || value
    only when non-NULL, so a NULL one leaves the hash byte-identical to what it was before the column existed.
    """
    columns = list(columns)
    return (f"concat_ws({SEP}, " + ", ".join(f"coalesce({col.name}, '')" for col in columns if not col.hash_optional) + ")"
            + "".join(f" || coalesce(chr(30) || '{col.name}=' || {col.name}, '')" for col in columns if col.hash_optional))


def stage_hash_tables(con: duckdb.DuckDBPyConnection, stg: str, key: str, columns: Iterable[Column],
                      rows_t: str, seq_t: str, key_t: str) -> None:
    """From the canonical staged table ``stg`` (dropped afterwards) build three temp tables:
    ``rows_t`` (every row plus its ``row_hash``), ``seq_t`` (``rid``, key, ``row_hash``, ``row_seq``) and
    ``key_t`` (key, ``version_hash``)."""
    con.execute(f"CREATE OR REPLACE TEMP TABLE {rows_t} AS SELECT *, md5({row_hash_expr(columns)}) AS row_hash FROM {stg}")
    con.execute(f"DROP TABLE {stg}")
    # row_seq and the per-key version hash on a narrow projection so the sort never moves wide rows
    con.execute(f"""CREATE OR REPLACE TEMP TABLE {seq_t} AS
                    SELECT rowid AS rid, {key}, row_hash, row_number() OVER (PARTITION BY {key} ORDER BY row_hash) AS row_seq
                    FROM {rows_t}""")
    con.execute(f"""CREATE OR REPLACE TEMP TABLE {key_t} AS
                    SELECT {key}, CASE WHEN count(*) = 1 THEN any_value(row_hash)
                                       ELSE md5(string_agg(row_hash, ',' ORDER BY row_hash)) END AS version_hash
                    FROM {seq_t} GROUP BY {key}""")


def interval_fingerprint(con: duckdb.DuckDBPyConnection, version_table: str, key: str, as_of: str) -> str:
    """Fingerprint of ``version_table`` as of ``as_of`` (ISO date), by interval lookup."""
    rows = (f"(SELECT {key} AS npi, version_hash FROM {version_table} WHERE row_seq = 1 "
            f"AND effective_date <= DATE '{as_of}' AND (end_date IS NULL OR end_date > DATE '{as_of}'))")
    return con.execute(fingerprint_sql(rows)).fetchone()[0]


def stored_fingerprints(con: duckdb.DuckDBPyConnection, stream_table: str) -> dict[tuple[int, str], str | None]:
    """(snapshot_id, stream) -> the fingerprint recorded at load time in the bookkeeping table ``stream_table``."""
    return {(sid, stream): fp for sid, stream, fp in
            con.execute(f"SELECT snapshot_id, stream, content_fingerprint FROM {stream_table} ORDER BY 1, 2").fetchall()}


def recompute_fingerprints(con: duckdb.DuckDBPyConnection, stream_table: str, key: str) -> dict[tuple[int, str], str]:
    """Fingerprint of every (snapshot, stream) in ``stream_table``, from the ``{stream}_version`` tables."""
    return {(sid, stream): interval_fingerprint(con, f"{stream}_version", key, ymd(sid)) for sid, stream in
            con.execute(f"SELECT snapshot_id, stream FROM {stream_table} ORDER BY 1, 2").fetchall()}
