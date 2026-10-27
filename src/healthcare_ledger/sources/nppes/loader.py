"""Load NPPES snapshots into a DuckDB warehouse as SCD Type 2 version tables.

Design
------
For every stream (``provider``, ``practice_location``, ``other_name``,
``endpoint``) the warehouse keeps two tables:

``{stream}_version``
    One row per (npi, version, row_seq). A *version* is the complete set of
    rows a stream holds for an NPI, fingerprinted by ``version_hash``. The
    version is valid from ``effective_date`` (inclusive) to ``end_date``
    (exclusive). Both are snapshot as-of dates, so reconstructing any
    snapshot is an exact interval lookup. The ``provider`` stream always has
    exactly one row per version (``row_seq`` = 1); the supplemental streams
    may have many.

``{stream}_state``
    The current fingerprint per NPI. It is rebuilt after every snapshot and
    is what the next snapshot is diffed against.

Processing a snapshot for a stream:

1. Read the member CSV with all columns as VARCHAR, map headers onto the
   canonical schema (missing columns become NULL), trim values and turn
   empty strings and CMS placeholder tokens into NULL.
2. Scrub identifiers this project never publishes (tax ids and SSNs that
   providers typed into free-form fields; see ``core/privacy.py``). This happens
   before hashing, so suppressed content never creates a version.
3. Hash every row over *all* canonical data columns in a fixed order, then
   hash the sorted multiset of row hashes per NPI into ``version_hash``.
4. Full-outer-join against ``{stream}_state`` to classify each NPI as new,
   changed, unchanged or absent.
5. Close the open version of changed and absent NPIs (``end_date`` =
   this snapshot's as-of date, ``close_reason`` = 'changed' | 'absent').
6. Insert a new version for new and changed NPIs.
7. Rebuild the state table.

Each snapshot is one DuckDB transaction, so a crash leaves the warehouse at
the previous snapshot and the run can simply be restarted.

A stream that is not present in a snapshot (supplemental files before 2019,
or CMS's zero-byte endpoint files) is skipped entirely: nothing is closed,
because absence of the file is not evidence of absence of the data.

The load itself is ``core.engine.VersionedWarehouse``; this module declares the NPPES streams
and bookkeeping and the hooks for the CMS zip: members are extracted from it, a file whose data
rows are wider than its header is re-read with explicit names, and step 2 (scrub, then
type-requires-name, then canonical slot order) runs as ``post_stage``.
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb

from ...core import engine
from ...core.columns import header_key
from ...core.engine import Stats, VersionedWarehouse
from ...core.sql import q
from ...core.versioning import meta_columns
from . import schema, scrub, slots
from .schema import STREAMS, Stream
from .snapshots import Snapshot, extract_member

log = logging.getLogger(__name__)

META_COLUMNS = meta_columns("npi")


def typed_columns(s: Stream) -> tuple[list[str], list[str]]:
    """(SELECT expressions that type each staged text column as stored, bad-date count expressions)."""
    typed, bad_date_checks = [], []
    for col in s.columns:
        if col.kind == "date":
            typed.append(f"try_strptime({col.name}, '%m/%d/%Y')::DATE AS {col.name}")
            bad_date_checks.append(f"count(*) FILTER (WHERE {col.name} IS NOT NULL AND try_strptime({col.name}, '%m/%d/%Y') IS NULL)")
        elif col.kind == "int":
            typed.append(f"try_cast({col.name} AS INTEGER) AS {col.name}")
        else:
            typed.append(col.name)
    return typed, bad_date_checks


def _lacks_npi_column(con: duckdb.DuckDBPyConnection, table: str) -> bool:
    return not any(header_key(r[0]) == schema.NPI_HEADER_KEY for r in con.execute(f"DESCRIBE {table}").fetchall())


class Warehouse(VersionedWarehouse):
    source_label = "NPPES"
    database = "nppes"
    key = "npi"
    key_noun = "NPIs"
    streams = STREAMS
    snapshot_table = "snapshot"
    stream_table = "snapshot_stream"
    single_row_streams = frozenset({"provider"})
    ignored_headers = frozenset(header_key(h) for h in schema.OTHER_ID_HEADERS)
    # Data rows wider than the header (CMS pl_pfile, Jul 2018) make DuckDB drop the header; re-read by name.
    reader_options = {"explicit_header_if": _lacks_npi_column}

    def init_bookkeeping(self, con: duckdb.DuckDBPyConnection) -> None:
        con.execute("""
            CREATE TABLE IF NOT EXISTS snapshot (
                snapshot_id INTEGER NOT NULL,
                as_of_date DATE NOT NULL,
                zip_file VARCHAR NOT NULL,
                zip_date DATE NOT NULL,
                main_csv VARCHAR NOT NULL,
                main_csv_bytes BIGINT,
                main_csv_columns INTEGER,
                streams_present VARCHAR[],
                loaded_at TIMESTAMP NOT NULL,
                seconds DOUBLE
            )""")
        con.execute("""
            CREATE TABLE IF NOT EXISTS snapshot_stream (
                snapshot_id INTEGER NOT NULL,
                stream VARCHAR NOT NULL,
                n_rows BIGINT, n_npis BIGINT,
                n_new BIGINT, n_changed BIGINT, n_unchanged BIGINT, n_absent BIGINT,
                n_bad_dates BIGINT,
                csv_mode VARCHAR,
                content_fingerprint VARCHAR
            )""")

    def record(self, snap: Snapshot, stats: list[Stats], seconds: float) -> None:
        c = self.con
        sid = snap.snapshot_id
        for o in stats:
            c.execute("INSERT INTO snapshot_stream VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      [sid, o.stream, o.n_rows, o.n_keys, o.n_new, o.n_changed, o.n_unchanged, o.n_absent,
                       o.n_bad_dates, o.csv_mode, o.content_fingerprint])
        main_cols = next((o.n_columns for o in stats if o.stream == "provider"), None)
        c.execute("INSERT INTO snapshot VALUES (?,?,?,?,?,?,?,?,now(),?)",
                  [sid, snap.as_of_date, snap.zip_path.name, snap.zip_date, Path(snap.members["provider"]).name,
                   snap.member_bytes.get("provider"), main_cols, [o.stream for o in stats], seconds])

    def member_path(self, snap: Snapshot, stream: Stream) -> Path:
        path = extract_member(snap, stream.name, self.work_dir)
        self._scratch.append(path)
        return path

    def map_headers(self, stream: Stream, headers: list[str]) -> tuple[dict[str, str | None], list[str]]:
        mapping, unmatched = schema.map_headers(stream, headers)
        return mapping, [h for h in unmatched if not h.startswith("_extra_") and header_key(h) not in self.ignored_headers]

    def remap(self, stream: Stream, raw: str, mapping: dict[str, str | None], path: Path) -> dict[str, str | None]:
        extras = [h for h in self._columns(raw) if h.startswith("_extra_")]
        if extras:
            sample = self.con.execute(f"SELECT {', '.join(q(h) for h in extras)} FROM {raw} WHERE {q(extras[0])} IS NOT NULL LIMIT 1").fetchone()
            log.warning("%s: %d unnamed trailing column(s) beyond the header are ignored (sample %s)", path.name, len(extras), sample)
        return mapping

    def post_stage(self, stream: Stream, stg: str) -> None:
        c = self.con
        # Suppress tax ids and SSNs before anything is hashed (scrub.py, core/privacy.py). A second pass over the
        # staged table keeps the regex CASEs out of the 129-column staging statement.
        scrub_sql = scrub.scrub_select(stream, stg)
        if scrub_sql is not None:
            c.execute(f"CREATE OR REPLACE TEMP TABLE {stg}_scrubbed AS {scrub_sql}")
            c.execute(f"DROP TABLE {stg}")
            c.execute(f"ALTER TABLE {stg}_scrubbed RENAME TO {stg}")
        # A type code whose name is NULL (after the scrub, which can blank a name) is NULL too.
        cols = set(stream.data_columns)
        for type_col, name_col in schema.TYPE_REQUIRES_NAME.items():
            if type_col in cols and name_col in cols:
                c.execute(f"UPDATE {stg} SET {type_col} = NULL WHERE {name_col} IS NULL AND {type_col} IS NOT NULL")
        # Repeating groups into canonical slot order (slots.py), after the scrub so a suppressed
        # value never moves; CMS's own slot order is not part of the version.
        canon_sql = slots.canonical_select(stream, stg)
        if canon_sql is not None:
            c.execute(f"CREATE OR REPLACE TEMP TABLE {stg}_canon AS {canon_sql}")
            c.execute(f"DROP TABLE {stg}")
            c.execute(f"ALTER TABLE {stg}_canon RENAME TO {stg}")

    def typed_select(self, stream: Stream) -> tuple[list[str], dict[str, list[str]]]:
        typed, bad_date_checks = typed_columns(stream)
        return typed, {"n_bad_dates": bad_date_checks}


def finalize(con: duckdb.DuckDBPyConnection) -> None:
    """Set last_snapshot_id on every open version to the latest snapshot in which its stream was present.

    Runs at the end of `load` and again at the start of `export`, so an export taken after an
    interrupted load is still internally consistent.
    """
    engine.finalize(con, Warehouse)


def rollback(con: duckdb.DuckDBPyConnection, keep_through: int) -> dict[str, int]:
    """Remove every snapshot newer than ``keep_through`` (engine.rollback). Use it to insert a newly found
    historical month without a full rebuild: roll back to the month before it, then ``load`` again."""
    return engine.rollback(con, Warehouse, keep_through)
