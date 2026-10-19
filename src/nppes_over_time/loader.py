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
   providers typed into free-form fields; see ``scrub.py``). This happens
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
"""

from __future__ import annotations

import csv
import logging
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import duckdb

from . import schema, scrub, slots
from .schema import STREAMS, Stream
from .snapshots import Snapshot, extract_member

log = logging.getLogger(__name__)

META_COLUMNS = [
    ("npi", "VARCHAR"),
    ("effective_date", "DATE"),
    ("end_date", "DATE"),
    ("is_current", "BOOLEAN"),
    ("first_snapshot_id", "INTEGER"),
    ("last_snapshot_id", "INTEGER"),
    ("close_reason", "VARCHAR"),
    ("version_hash", "VARCHAR"),
    ("row_seq", "INTEGER"),
]

SEP = "chr(31)"  # ASCII unit separator between fields inside the row hash


def fingerprint_sql(rows: str) -> str:
    """SQL for the content fingerprint of a set of (npi, version_hash) rows.

    ``rows`` is a table name or a parenthesised subquery with ``npi`` and ``version_hash``
    columns, one row per NPI. The fingerprint is md5 over the lines ``npi:version_hash``
    joined by newline in NPI order; an empty set fingerprints as md5('').
    """
    return f"SELECT md5(coalesce(string_agg(npi || ':' || version_hash, chr(10) ORDER BY npi), '')) FROM {rows}"


def _sql_type(kind: str) -> str:
    return {"text": "VARCHAR", "date": "DATE", "int": "INTEGER"}[kind]


def _q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


@dataclass
class StreamStats:
    stream: str
    n_rows: int = 0
    n_npis: int = 0
    n_new: int = 0
    n_changed: int = 0
    n_unchanged: int = 0
    n_absent: int = 0
    n_bad_dates: int = 0
    csv_mode: str = ""
    content_fingerprint: str | None = None


@dataclass
class Staged:
    """Phase-1 output for one stream: temp tables plus parse facts."""
    stream: Stream
    rows_table: str
    seq_table: str
    npi_table: str
    n_rows: int
    n_npis: int
    n_columns: int
    csv_mode: str


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


class Warehouse:
    def __init__(self, db_path: Path, *, threads: int | None = None,
                 memory_limit: str | None = None, work_dir: Path | None = None,
                 max_absent_share: float = 0.05):
        self.db_path = Path(db_path)
        # Tripwire on the main file: a truncated or garbled npidata file that still parses would mark
        # a large share of NPIs 'absent'. Refuse unless the caller raises the threshold (--max-absent-share).
        self.max_absent_share = max_absent_share
        self.work_dir = Path(work_dir) if work_dir else self.db_path.parent / "work"
        self.con = duckdb.connect(str(self.db_path))
        if threads:
            self.con.execute(f"SET threads = {int(threads)}")
        if memory_limit:
            self.con.execute(f"SET memory_limit = '{memory_limit}'")
        self.con.execute("SET preserve_insertion_order = false")
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.con.execute(f"SET temp_directory = '{self.work_dir / 'duckdb_tmp'}'")
        self.init_schema()

    # ------------------------------------------------------------------ schema
    def init_schema(self) -> None:
        c = self.con
        c.execute("""
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
        c.execute("""
            CREATE TABLE IF NOT EXISTS snapshot_stream (
                snapshot_id INTEGER NOT NULL,
                stream VARCHAR NOT NULL,
                n_rows BIGINT, n_npis BIGINT,
                n_new BIGINT, n_changed BIGINT, n_unchanged BIGINT, n_absent BIGINT,
                n_bad_dates BIGINT,
                csv_mode VARCHAR,
                content_fingerprint VARCHAR
            )""")
        # Schema evolution for warehouses built before fingerprints existed; `backfill-fingerprints`
        # fills the column from the version tables.
        if "content_fingerprint" not in {r[0] for r in c.execute("DESCRIBE snapshot_stream").fetchall()}:
            log.info("adding column snapshot_stream.content_fingerprint to existing warehouse")
            c.execute("ALTER TABLE snapshot_stream ADD COLUMN content_fingerprint VARCHAR")
        for s in STREAMS:
            cols = ", ".join(f"{n} {t}" for n, t in META_COLUMNS)
            data = ", ".join(f"{col.name} {_sql_type(col.kind)}" for col in s.columns)
            c.execute(f"CREATE TABLE IF NOT EXISTS {s.name}_version ({cols}, {data})")
            c.execute(f"""CREATE TABLE IF NOT EXISTS {s.name}_state (
                npi VARCHAR NOT NULL, version_hash VARCHAR NOT NULL, effective_date DATE NOT NULL)""")
            # Schema evolution: CMS adds columns over time (e.g. Created Date in the Apr 2026
            # other-name file). New canonical columns are appended to an existing warehouse as NULL;
            # earlier versions simply never had the field, exactly like older main-file vintages.
            existing = {r[0] for r in c.execute(f"DESCRIBE {s.name}_version").fetchall()}
            for col in s.columns:
                if col.name not in existing:
                    log.info("adding column %s.%s (%s) to existing warehouse", s.name, col.name, col.kind)
                    c.execute(f"ALTER TABLE {s.name}_version ADD COLUMN {col.name} {_sql_type(col.kind)}")

    def loaded_snapshot_ids(self) -> list[int]:
        return [r[0] for r in self.con.execute("SELECT snapshot_id FROM snapshot ORDER BY 1").fetchall()]

    def last_present_snapshot(self, stream: str) -> int | None:
        row = self.con.execute(
            "SELECT max(snapshot_id) FROM snapshot_stream WHERE stream = ?", [stream]).fetchone()
        return row[0] if row else None

    # ------------------------------------------------------------------- load
    def load_snapshot(self, snap: Snapshot) -> list[StreamStats]:
        """Load one snapshot.

        Phase 1 (outside any transaction): extract and parse every present
        stream into temp tables. Parsing may need several reader attempts,
        and DuckDB aborts a transaction on the first failed statement, so this
        must not run inside one.

        Phase 2 (one transaction): classify against state, close and open
        versions, rebuild state, record bookkeeping. All durable writes.
        """
        sid = snap.snapshot_id
        if sid in self.loaded_snapshot_ids():
            raise ValueError(f"snapshot {sid} already loaded")
        prev = self.loaded_snapshot_ids()
        if prev and prev[-1] > sid:
            raise ValueError(f"snapshot {sid} is older than last loaded {prev[-1]}; load in order")

        t0 = time.time()
        c = self.con
        staged: list[Staged] = []
        extracted: list[Path] = []
        try:
            for s in STREAMS:
                if not snap.has(s.name):
                    log.info("%s: stream %s not present, skipping", sid, s.name)
                    continue
                t_x = time.time()
                path = extract_member(snap, s.name, self.work_dir)
                extracted.append(path)
                log.debug("%s %s extract    %.1fs", sid, s.name, time.time() - t_x)
                st = self._stage_stream(s, path, snap)
                if st is None:
                    log.warning("%s: stream %s member has no rows, treating as not present", sid, s.name)
                    continue
                staged.append(st)

            c.execute("BEGIN")
            try:
                stats = [self._apply_stream(st, snap) for st in staged]
                for st in stats:
                    c.execute("INSERT INTO snapshot_stream VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                              [sid, st.stream, st.n_rows, st.n_npis, st.n_new, st.n_changed,
                               st.n_unchanged, st.n_absent, st.n_bad_dates, st.csv_mode, st.content_fingerprint])
                    log.info("%s %-18s rows=%-9d npis=%-9d new=%-8d changed=%-8d unchanged=%-9d absent=%-6d bad_dates=%d",
                             sid, st.stream, st.n_rows, st.n_npis, st.n_new, st.n_changed, st.n_unchanged,
                             st.n_absent, st.n_bad_dates)
                main_cols = next((st.n_columns for st in staged if st.stream.name == "provider"), None)
                c.execute("INSERT INTO snapshot VALUES (?,?,?,?,?,?,?,?,now(),?)",
                          [sid, snap.as_of_date, snap.zip_path.name, snap.zip_date,
                           Path(snap.members["provider"]).name, snap.member_bytes.get("provider"),
                           main_cols, [st.stream for st in stats], time.time() - t0])
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise
        finally:
            for st in staged:
                for tbl in (st.rows_table, st.seq_table, st.npi_table):
                    c.execute(f"DROP TABLE IF EXISTS {tbl}")
            for p in extracted:
                p.unlink(missing_ok=True)
        log.info("%s done in %.0fs", sid, time.time() - t0)
        return stats

    # -------------------------------------------------------------- internals
    def _read_csv_into(self, table: str, path: Path) -> str:
        """Create a temp table from the CSV, trying progressively looser parsers.

        Must be called outside a transaction: DuckDB aborts the transaction on
        the first failed statement, which would defeat the fallback chain.
        """
        base = f"'{path}', header=true, all_varchar=true, delim=',', quote='\"', escape='\"'"
        lenient = "strict_mode=false, null_padding=true, parallel=false, max_line_size=20000000"
        # UTF-8 readers come before latin-1 ones: a latin-1 fallback on a UTF-8 file would silently
        # mojibake every multibyte character and create spurious versions.
        attempts = [
            ("strict", f"read_csv({base})"),
            ("lenient", f"read_csv({base}, {lenient})"),
            ("strict-latin1", f"read_csv({base}, encoding='latin-1')"),
            ("lenient-latin1", f"read_csv({base}, {lenient}, encoding='latin-1')"),
        ]
        last: Exception | None = None
        for mode, reader in attempts:
            try:
                self.con.execute(f"CREATE OR REPLACE TEMP TABLE {table} AS SELECT * FROM {reader}")
            except duckdb.Error as e:  # noqa: PERF203
                last = e
                log.debug("%s reader failed on %s: %s", mode, path.name, str(e).splitlines()[0])
                continue
            if not self._has_npi_column(table):
                # Data rows wider than the header row (CMS pl_pfile, Jul 2018: two stray trailing
                # fields). DuckDB then stops trusting the header; re-read with explicit names.
                mode = self._read_with_explicit_header(table, path)
            if mode != "strict":
                log.warning("%s parsed with %s reader", path.name, mode)
            return mode
        raise RuntimeError(f"could not parse {path}") from last

    def _has_npi_column(self, table: str) -> bool:
        return any(schema.header_key(r[0]) == schema.NPI_HEADER_KEY for r in self.con.execute(f"DESCRIBE {table}").fetchall())

    def _read_with_explicit_header(self, table: str, path: Path) -> str:
        with open(path, newline="", encoding="utf-8", errors="replace") as fh:
            header = next(csv.reader(fh))
        opts = "header=false, skip=1, all_varchar=true, null_padding=true, strict_mode=false, parallel=false, max_line_size=20000000"
        width = len(self.con.execute(f"DESCRIBE SELECT * FROM read_csv('{path}', {opts})").fetchall())
        extra = max(0, width - len(header))
        names = header + [f"_extra_{i}" for i in range(1, extra + 1)]
        names_sql = "[" + ", ".join("'" + n.replace("'", "''") + "'" for n in names) + "]"
        self.con.execute(f"CREATE OR REPLACE TEMP TABLE {table} AS SELECT * FROM read_csv('{path}', {opts}, names={names_sql})")
        return f"explicit-header+{extra}extra"

    def _stage_stream(self, s: Stream, path: Path, snap: Snapshot) -> "Staged | None":
        """Phase 1: parse, canonicalize and fingerprint one stream into temp tables."""
        c = self.con
        sid = snap.snapshot_id
        t = [time.time()]

        def lap(step: str) -> None:
            t.append(time.time())
            log.debug("%s %s %-10s %.1fs", sid, s.name, step, t[-1] - t[-2])

        raw = f"raw_{s.name}"
        csv_mode = self._read_csv_into(raw, path)
        lap("read_csv")
        headers = [r[0] for r in c.execute(f"DESCRIBE {raw}").fetchall()]
        mapping, unmatched = schema.map_headers(s, headers)
        extras = [h for h in unmatched if h.startswith("_extra_")]
        if extras:
            sample = c.execute(f"SELECT {', '.join(_q(h) for h in extras)} FROM {raw} WHERE {_q(extras[0])} IS NOT NULL LIMIT 1").fetchone()
            log.warning("%s: %d unnamed trailing column(s) beyond the header are ignored (sample %s)", path.name, len(extras), sample)
        unmatched = [h for h in unmatched if not h.startswith("_extra_")]
        if unmatched:
            raise ValueError(f"{path.name}: unmapped columns {unmatched}")

        # canonical staging table: trimmed text; '' and CMS placeholders -> NULL; missing columns NULL.
        # Written as nested nullif() calls on purpose: the equivalent CASE WHEN trim(x) IN (...) over
        # 329 columns makes DuckDB's planner explode past 80 GB; the nullif chain costs nothing extra.
        sel = [f"nullif(trim({_q(mapping['npi'])}), '') AS npi"]
        for col in s.columns:
            src = mapping[col.name]
            if src is None:
                sel.append("NULL::VARCHAR AS " + col.name)
                continue
            tokens = ["''"] + [f"'{t}'" for t in schema.PLACEHOLDERS]
            if schema.is_code_column(col.name):
                tokens.append(f"'{schema.NULL_WORD}'")
            expr = f"trim({_q(src)})"
            for tok in tokens:
                expr = f"nullif({expr}, {tok})"
            sel.append(f"{expr} AS {col.name}")
        stg = f"stg_{s.name}"
        c.execute(f"CREATE OR REPLACE TEMP TABLE {stg} AS SELECT {', '.join(sel)} FROM {raw} WHERE npi IS NOT NULL")
        c.execute(f"DROP TABLE {raw}")
        lap("stage")
        # Suppress tax ids and SSNs before anything is hashed (scrub.py). A second pass over the
        # staged table keeps the regex CASEs out of the 329-column nullif statement above.
        scrub_sql = scrub.scrub_select(s, stg)
        if scrub_sql is not None:
            c.execute(f"CREATE OR REPLACE TEMP TABLE {stg}_scrubbed AS {scrub_sql}")
            c.execute(f"DROP TABLE {stg}")
            c.execute(f"ALTER TABLE {stg}_scrubbed RENAME TO {stg}")
            lap("scrub")
        # A type code whose name is NULL (after the scrub, which can blank a name) is NULL too.
        cols = set(s.data_columns)
        for type_col, name_col in schema.TYPE_REQUIRES_NAME.items():
            if type_col in cols and name_col in cols:
                c.execute(f"UPDATE {stg} SET {type_col} = NULL WHERE {name_col} IS NULL AND {type_col} IS NOT NULL")
        # Repeating groups into canonical slot order (slots.py), after the scrub so a suppressed
        # value never moves; CMS's own slot order is not part of the version.
        canon_sql = slots.canonical_select(s, stg)
        if canon_sql is not None:
            c.execute(f"CREATE OR REPLACE TEMP TABLE {stg}_canon AS {canon_sql}")
            c.execute(f"DROP TABLE {stg}")
            c.execute(f"ALTER TABLE {stg}_canon RENAME TO {stg}")
            lap("slots")
        n_rows, n_npis = c.execute(f"SELECT count(*), count(DISTINCT npi) FROM {stg}").fetchone()
        if n_npis == 0 and not s.always_present:
            c.execute(f"DROP TABLE {stg}")
            return None
        if s.name == "provider" and n_rows != n_npis:
            log.warning("%s: main file has %d duplicate NPI rows; they become row_seq > 1", sid, n_rows - n_npis)

        # row hash over every baseline data column; concat_ws is a single pass, coalesce keeps NULL positions stable.
        # Columns flagged hash_optional (added after the baseline) are appended as chr(30) || 'name=' || value
        # only when non-NULL, so a NULL one leaves the hash byte-identical to what it was before the column existed.
        hash_expr = (f"concat_ws({SEP}, " + ", ".join(f"coalesce({col.name}, '')" for col in s.columns if not col.hash_optional) + ")"
                     + "".join(f" || coalesce(chr(30) || '{col.name}=' || {col.name}, '')" for col in s.columns if col.hash_optional))
        rows_t, seq_t, npi_t = f"stg_h_{s.name}", f"stg_seq_{s.name}", f"stg_npi_{s.name}"
        c.execute(f"CREATE OR REPLACE TEMP TABLE {rows_t} AS SELECT *, md5({hash_expr}) AS row_hash FROM {stg}")
        c.execute(f"DROP TABLE {stg}")
        # row_seq and the per-NPI version hash on a narrow projection so the sort never moves wide rows
        c.execute(f"""CREATE OR REPLACE TEMP TABLE {seq_t} AS
                      SELECT rowid AS rid, npi, row_hash,
                             row_number() OVER (PARTITION BY npi ORDER BY row_hash) AS row_seq
                      FROM {rows_t}""")
        c.execute(f"""CREATE OR REPLACE TEMP TABLE {npi_t} AS
                      SELECT npi,
                             CASE WHEN count(*) = 1 THEN any_value(row_hash)
                                  ELSE md5(string_agg(row_hash, ',' ORDER BY row_hash)) END AS version_hash,
                             count(*) AS n
                      FROM {seq_t} GROUP BY npi""")
        lap("hash")
        return Staged(stream=s, rows_table=rows_t, seq_table=seq_t, npi_table=npi_t,
                      n_rows=n_rows, n_npis=n_npis, n_columns=len(headers), csv_mode=csv_mode)

    def _apply_stream(self, st: "Staged", snap: Snapshot) -> StreamStats:
        """Phase 2 (inside the snapshot transaction): diff against state and write versions."""
        c = self.con
        s = st.stream
        sid = snap.snapshot_id
        as_of = snap.as_of_date.isoformat()
        t = [time.time()]

        def lap(step: str) -> None:
            t.append(time.time())
            log.debug("%s %s %-10s %.1fs", sid, s.name, step, t[-1] - t[-2])

        out = StreamStats(stream=s.name, n_rows=st.n_rows, n_npis=st.n_npis, csv_mode=st.csv_mode)
        delta = f"delta_{s.name}"
        c.execute(f"""CREATE OR REPLACE TEMP TABLE {delta} AS
            SELECT coalesce(n.npi, o.npi) AS npi,
                   CASE WHEN o.npi IS NULL THEN 'new'
                        WHEN n.npi IS NULL THEN 'absent'
                        WHEN n.version_hash <> o.version_hash THEN 'changed'
                        ELSE 'unchanged' END AS kind,
                   n.version_hash AS new_hash,
                   o.effective_date AS old_effective_date
            FROM {st.npi_table} n FULL OUTER JOIN {s.name}_state o ON n.npi = o.npi""")
        counts = dict(c.execute(f"SELECT kind, count(*) FROM {delta} GROUP BY kind").fetchall())
        out.n_new, out.n_changed = counts.get("new", 0), counts.get("changed", 0)
        out.n_unchanged, out.n_absent = counts.get("unchanged", 0), counts.get("absent", 0)
        prev_npis = out.n_changed + out.n_unchanged + out.n_absent
        # Only the main file is guarded: a truncated or garbled npidata file would silently close
        # millions of histories. The supplemental streams are small and genuinely volatile (a third
        # of NPIs with endpoints left the file between May and June 2019) and their absence never
        # touches provider history.
        if s.always_present and prev_npis >= 1000 and out.n_absent > self.max_absent_share * prev_npis:
            raise RuntimeError(
                f"{sid} {s.name}: {out.n_absent:,} of {prev_npis:,} NPIs would be marked absent "
                f"({out.n_absent / prev_npis:.1%} > {self.max_absent_share:.0%}); refusing. "
                f"Check the file, or raise --max-absent-share if this is genuine.")
        lap("classify")

        prev_sid = self.last_present_snapshot(s.name)
        c.execute(f"""UPDATE {s.name}_version v
                      SET end_date = DATE '{as_of}', is_current = false,
                          last_snapshot_id = {prev_sid if prev_sid is not None else 'NULL'},
                          close_reason = d.kind
                      FROM {delta} d
                      WHERE v.npi = d.npi AND v.is_current AND d.kind IN ('changed', 'absent')""")
        lap("close")

        typed, bad_date_checks = typed_columns(s)
        if bad_date_checks:
            out.n_bad_dates = sum(c.execute(f"SELECT {', '.join(bad_date_checks)} FROM {st.rows_table}").fetchone())
        meta_names = ", ".join(n for n, _ in META_COLUMNS)
        data_names = ", ".join(col.name for col in s.columns)
        c.execute(f"""INSERT INTO {s.name}_version ({meta_names}, {data_names})
            SELECT h.npi, DATE '{as_of}', NULL, true, {sid}, {sid}, NULL, d.new_hash, q.row_seq,
                   {', '.join(typed)}
            FROM {st.rows_table} h
            JOIN {st.seq_table} q ON q.rid = h.rowid
            JOIN {delta} d ON h.npi = d.npi
            WHERE d.kind IN ('new', 'changed')""")
        lap("insert")

        c.execute(f"""CREATE OR REPLACE TABLE {s.name}_state AS
            SELECT npi, new_hash AS version_hash,
                   CASE WHEN kind = 'unchanged' THEN old_effective_date ELSE DATE '{as_of}' END AS effective_date
            FROM {delta} WHERE kind <> 'absent'""")
        c.execute(f"DROP TABLE {delta}")
        # Content fingerprint of this snapshot for this stream: the state table is exactly the
        # (npi, version_hash) set the snapshot holds. Export recomputes it from the version table
        # by interval lookup and refuses to publish if the two differ (release.py).
        out.content_fingerprint = c.execute(fingerprint_sql(f"{s.name}_state")).fetchone()[0]
        lap("state")
        return out

    # ----------------------------------------------------------------- finish
    def finalize(self) -> None:
        """Stamp last_snapshot_id on open versions. Safe to run repeatedly."""
        finalize(self.con)

    def close(self) -> None:
        self.con.close()


def finalize(con: duckdb.DuckDBPyConnection) -> None:
    """Set last_snapshot_id on every open version to the latest snapshot in which its stream was present.

    Runs at the end of `load` and again at the start of `export`, so an export taken after an
    interrupted load is still internally consistent.
    """
    for s in STREAMS:
        row = con.execute("SELECT max(snapshot_id) FROM snapshot_stream WHERE stream = ?", [s.name]).fetchone()
        if row and row[0] is not None:
            con.execute(f"UPDATE {s.name}_version SET last_snapshot_id = {row[0]} WHERE is_current AND last_snapshot_id <> {row[0]}")


def rollback(con: duckdb.DuckDBPyConnection, keep_through: int) -> dict[str, int]:
    """Remove every snapshot newer than ``keep_through`` (a snapshot id, YYYYMMDD).

    Versions opened after that snapshot are deleted, versions closed after it are
    reopened, the bookkeeping rows are dropped and the state tables are rebuilt from
    the reopened versions. Use it to insert a newly found historical month without a
    full rebuild: roll back to the month before it, then ``load`` again.
    """
    d = f"{keep_through // 10000:04d}-{keep_through // 100 % 100:02d}-{keep_through % 100:02d}"
    removed: dict[str, int] = {}
    con.execute("BEGIN")
    try:
        for s in STREAMS:
            v = f"{s.name}_version"
            removed[s.name] = con.execute(f"SELECT count(*) FROM {v} WHERE first_snapshot_id > {keep_through}").fetchone()[0]
            con.execute(f"DELETE FROM {v} WHERE first_snapshot_id > {keep_through}")
            con.execute(f"""UPDATE {v} SET end_date = NULL, is_current = true, close_reason = NULL
                            WHERE end_date > DATE '{d}'""")
            con.execute(f"""CREATE OR REPLACE TABLE {s.name}_state AS
                            SELECT npi, version_hash, effective_date FROM {v} WHERE is_current AND row_seq = 1""")
        con.execute(f"DELETE FROM snapshot_stream WHERE snapshot_id > {keep_through}")
        removed["snapshots"] = con.execute(f"SELECT count(*) FROM snapshot WHERE snapshot_id > {keep_through}").fetchone()[0]
        con.execute(f"DELETE FROM snapshot WHERE snapshot_id > {keep_through}")
        finalize(con)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return removed
