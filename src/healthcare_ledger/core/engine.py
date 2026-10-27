"""The SCD Type 2 load, the same for every snapshot source.

A source is a subclass of ``VersionedWarehouse`` that declares its key, its streams and its
bookkeeping tables, and overrides the few hooks where its files need something the default does
not do. The skeleton of one snapshot (docs/METHODOLOGY.md sections 2 to 5):

1. Stage every present stream outside the transaction: read the CSV (``core.csvread``), map its
   headers onto the stream's columns (``map_headers``, ``remap``), build the canonical text of
   each column (``column_expr``), then hash rows and versions (``core.versioning``).
2. In one transaction, per stream: classify every key as ``new``, ``changed``, ``unchanged`` or
   gone (``absent``, or what ``absent_kind_sql`` says) against ``{stream}_state``; refuse when
   too many keys would be gone (``counted_absent``); close the open versions of changed and gone
   keys; insert the new versions (``typed_select``); rebuild the state table and fingerprint it.
3. Write the bookkeeping rows (``record``) and commit.

A stream that is not present in a snapshot is skipped entirely: nothing is closed, because
absence of the file is not evidence of absence of the data.

The module functions (``finalize``, ``rollback``, ``reset``, ``loaded`` and the fingerprints) take
the subclass itself as ``spec`` and work on any connection, so they serve the CLI and the export
without opening a loader.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, fields
from datetime import date
from pathlib import Path
from typing import Any, ClassVar, Protocol

import duckdb

from . import columns, filesources
from .columns import Column, header_key
from . import versioning
from .connect import configure, source_file
from .csvread import read_csv_into
from .sql import q, sql_type, ymd
from .versioning import fingerprint_sql, meta_columns, stage_hash_tables

log = logging.getLogger(__name__)


class SnapshotSpec(Protocol):
    as_of_date: date
    members: dict[str, Path]

    @property
    def snapshot_id(self) -> int: ...

    @property
    def label(self) -> str: ...

    def has(self, stream: str) -> bool: ...


@dataclass
class Stats:
    """What one stream of one snapshot did. ``record`` writes the fields its bookkeeping table has."""
    stream: str
    file: str = ""
    bytes: int | None = None
    sha256: str | None = None
    n_columns: int | None = None
    n_rows: int = 0
    n_keys: int = 0
    n_new: int = 0
    n_changed: int = 0
    n_unchanged: int = 0
    n_absent: int = 0
    n_moved: int = 0
    n_bad_dates: int = 0
    n_bad_numbers: int = 0
    csv_mode: str = ""
    content_fingerprint: str | None = None


@dataclass
class Staged:
    """Phase-1 output for one stream: the temp tables of ``stage_hash_tables`` plus parse facts."""
    stream: Any
    snapshot_id: int
    rows_table: str
    seq_table: str
    key_table: str
    n_rows: int
    n_keys: int
    n_columns: int
    csv_mode: str
    path: Path


class VersionedWarehouse:
    # ---- declarations every source sets
    source_label: ClassVar[str]                  # in messages: "PPEF snapshot ... already loaded"
    database: ClassVar[str]                      # its file in the warehouse directory: <warehouse>/<database>.duckdb
    key: ClassVar[str]                           # the key column of every stream
    key_noun: ClassVar[str] = "keys"             # in the absent-guard message
    streams: ClassVar[Sequence[Any]]
    snapshot_table: ClassVar[str]
    stream_table: ClassVar[str]
    single_row_streams: ClassVar[frozenset[str]] = frozenset()   # a repeated key there is worth a warning
    gone_kinds: ClassVar[tuple[str, ...]] = ("absent",)          # kinds absent_kind_sql can return
    ignored_headers: ClassVar[frozenset[str]] = frozenset()      # header keys that are read and dropped
    unmapped_hint: ClassVar[str] = ""
    reader_options: ClassVar[dict[str, Any]] = {}                # extra arguments for read_csv_into

    def __init__(self, warehouse: Path, *, threads: int | None = None, memory_limit: str | None = None,
                 work_dir: Path | None = None, max_absent_share: float = 0.05):
        """Open (creating it if needed) this source's own file, ``<warehouse>/<database>.duckdb``; nothing else
        in the warehouse directory is touched. ``work_dir`` defaults to ``work/`` next to the warehouse."""
        self.warehouse = Path(warehouse)
        self.db_path = source_file(self.warehouse, self.database)
        self.max_absent_share = max_absent_share
        self.work_dir = Path(work_dir) if work_dir else self.warehouse.parent / "work"
        self.warehouse.mkdir(parents=True, exist_ok=True)
        self.con = duckdb.connect(str(self.db_path))
        self.work_dir.mkdir(parents=True, exist_ok=True)
        configure(self.con, threads=threads, memory_limit=memory_limit, temp_dir=self.work_dir / "duckdb_tmp")
        self._scratch: list[Path] = []          # repaired or extracted copies, removed after each snapshot
        self.init_schema()

    # ---------------------------------------------------------------- hooks
    def init_bookkeeping(self, con: duckdb.DuckDBPyConnection) -> None:
        """Create ``snapshot_table`` and ``stream_table`` if they do not exist."""
        raise NotImplementedError

    def record(self, snap: SnapshotSpec, stats: list[Stats], seconds: float) -> None:
        """Write one ``stream_table`` row per stream and one ``snapshot_table`` row, by column name.

        A stream-table column is the ``Stats`` field of the same name (``snapshot_id`` is the snapshot's);
        the snapshot table has ``snapshot_id``, ``as_of_date``, ``directory`` (the snapshot's label),
        ``streams_present``, ``loaded_at`` and ``seconds``."""
        c = self.con
        names = self._columns(self.stream_table)
        have = {f.name for f in fields(Stats)}
        for out in stats:
            vals = [snap.snapshot_id if n == "snapshot_id" else getattr(out, n) if n in have else _missing(self.stream_table, n)
                    for n in names]
            c.execute(f"INSERT INTO {self.stream_table} ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})", vals)
        snap_vals = {"snapshot_id": snap.snapshot_id, "as_of_date": snap.as_of_date, "directory": snap.label,
                     "streams_present": [o.stream for o in stats], "seconds": seconds}
        names = [n for n in self._columns(self.snapshot_table) if n != "loaded_at"]
        vals = [snap_vals[n] if n in snap_vals else _missing(self.snapshot_table, n) for n in names]
        c.execute(f"INSERT INTO {self.snapshot_table} ({', '.join(names)}, loaded_at) "
                  f"VALUES ({', '.join('?' * len(names))}, now())", vals)

    def member_path(self, snap: SnapshotSpec, stream: Any) -> Path:
        """The CSV of ``stream`` in ``snap``. A copy made for the load goes on ``self._scratch``."""
        return snap.members[stream.name]

    def map_headers(self, stream: Any, headers: list[str]) -> tuple[dict[str, str | None], list[str]]:
        """Canonical column -> actual header (None when absent); plus headers that match nothing."""
        by_key = {header_key(h): h for h in headers}
        key_hdr = by_key.get(header_key(stream.key_header))
        if key_hdr is None:
            raise ValueError(f"file has no {stream.key_header} column")
        mapping: dict[str, str | None] = {self.key: key_hdr}
        used = {key_hdr}
        for col in stream.columns:
            hit = next((by_key[k] for k in col.keys() if k in by_key), None)
            mapping[col.name] = hit
            if hit is not None:
                used.add(hit)
        unmatched = [h for h in headers if h not in used and header_key(h) not in self.ignored_headers]
        return mapping, unmatched

    def remap(self, stream: Any, raw: str, mapping: dict[str, str | None], path: Path) -> dict[str, str | None]:
        """A chance to correct the header mapping from the content of the raw table ``raw``."""
        return mapping

    def column_expr(self, stream: Any, col: Column, src: str) -> str:
        """Canonical text of ``col`` from the quoted source column ``src``: trimmed, placeholders to NULL."""
        return columns.null_placeholders(src, columns.placeholder_tokens(col.name))

    def post_stage(self, stream: Any, stg: str) -> None:
        """Rewrite the canonical staged table ``stg`` in place before it is hashed."""

    def keep_empty(self, stream: Any) -> bool:
        """Load a file with no rows (closing every key as absent) instead of treating it as not present."""
        return stream.always_present

    def absent_kind_sql(self, stream: Any, staged: list[Staged]) -> str:
        """The kind of a key of the state table (alias ``o``) missing from this stream's file; one of gone_kinds."""
        return "'absent'"

    def counted_absent(self, st: Staged, delta: str, out: Stats) -> int:
        """How many gone keys the absent guard counts against ``max_absent_share``."""
        return out.n_absent if st.stream.always_present else 0

    def typed_select(self, stream: Any) -> tuple[list[str], dict[str, list[str]]]:
        """Insert expressions over the staged text columns, and per ``Stats`` field the count(*) expressions
        of values that did not type (summed into that field)."""
        return [c.name for c in stream.columns], {}

    # ---------------------------------------------------------------- schema
    def init_schema(self) -> None:
        c = self.con
        self.init_bookkeeping(c)
        meta = ", ".join(f"{n} {t}" for n, t in meta_columns(self.key))
        for s in self.streams:
            data = ", ".join(f"{col.name} {sql_type(col.kind)}" for col in s.columns)
            c.execute(f"CREATE TABLE IF NOT EXISTS {s.name}_version ({meta}, {data})")
            c.execute(f"CREATE TABLE IF NOT EXISTS {s.name}_state "
                      f"({self.key} VARCHAR NOT NULL, version_hash VARCHAR NOT NULL, effective_date DATE NOT NULL)")
            existing = set(self._columns(f"{s.name}_version"))
            for col in s.columns:
                if col.name not in existing:
                    log.info("adding column %s.%s to existing warehouse", s.name, col.name)
                    c.execute(f"ALTER TABLE {s.name}_version ADD COLUMN {col.name} {sql_type(col.kind)}")

    def _columns(self, table: str) -> list[str]:
        return [r[0] for r in self.con.execute(f"DESCRIBE {table}").fetchall()]

    def loaded_snapshot_ids(self) -> list[int]:
        return [r[0] for r in self.con.execute(f"SELECT snapshot_id FROM {self.snapshot_table} ORDER BY 1").fetchall()]

    def last_present_snapshot(self, stream: str) -> int | None:
        row = self.con.execute(f"SELECT max(snapshot_id) FROM {self.stream_table} WHERE stream = ?", [stream]).fetchone()
        return row[0] if row else None

    # ---------------------------------------------------------------- load
    def load_snapshot(self, snap: SnapshotSpec) -> list[Stats]:
        sid = snap.snapshot_id
        done = self.loaded_snapshot_ids()
        if sid in done:
            raise ValueError(f"{self.source_label} snapshot {sid} ({snap.label}) already loaded")
        if done and done[-1] > sid:
            raise ValueError(f"{self.source_label} snapshot {sid} ({snap.label}) is older than last loaded {done[-1]}; load in order")
        t0 = time.time()
        c = self.con
        staged: list[Staged] = []
        try:
            for s in self.streams:
                if not snap.has(s.name):
                    log.info("%s %s: stream %s not present, skipping", sid, snap.label, s.name)
                    continue
                path = self.member_path(snap, s)
                st = self._stage(s, path, sid)
                if st is None:
                    log.warning("%s: %s has no rows, treating as not present", sid, path.name)
                    continue
                staged.append(st)
            c.execute("BEGIN")
            try:
                stats = [self._apply(st, snap, staged) for st in staged]
                for out in stats:
                    log.info("%s %-30s rows=%-9d keys=%-9d new=%-8d changed=%-8d unchanged=%-9d absent=%-6d%s",
                             sid, out.stream, out.n_rows, out.n_keys, out.n_new, out.n_changed, out.n_unchanged, out.n_absent,
                             "".join(f" {k}={getattr(out, 'n_' + k)}" for k in ("moved", "bad_dates", "bad_numbers")
                                     if getattr(out, "n_" + k)))
                self.record(snap, stats, time.time() - t0)
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise
        finally:
            for st in staged:
                for tbl in (st.rows_table, st.seq_table, st.key_table):
                    c.execute(f"DROP TABLE IF EXISTS {tbl}")
            for p in self._scratch:
                p.unlink(missing_ok=True)
            self._scratch.clear()
        log.info("%s %s done in %.0fs", sid, snap.label, time.time() - t0)
        return stats

    def _stage(self, s: Any, path: Path, sid: int) -> Staged | None:
        c = self.con
        k = self.key
        raw = f"raw_{s.name}"
        csv_mode = read_csv_into(c, raw, path, work_dir=self.work_dir, scratch=self._scratch, **self.reader_options)
        headers = self._columns(raw)
        mapping, unmatched = self.map_headers(s, headers)
        if unmatched:
            raise ValueError(f"{path.name}: unmapped columns {unmatched}{self.unmapped_hint}")
        mapping = self.remap(s, raw, mapping, path)
        sel = [f"nullif(trim({q(mapping[k])}), '') AS {k}"]
        for col in s.columns:
            src = mapping[col.name]
            sel.append(f"NULL::VARCHAR AS {col.name}" if src is None else f"{self.column_expr(s, col, q(src))} AS {col.name}")
        stg = f"stg_{s.name}"
        c.execute(f"CREATE OR REPLACE TEMP TABLE {stg} AS SELECT {', '.join(sel)} FROM {raw} WHERE {k} IS NOT NULL")
        c.execute(f"DROP TABLE {raw}")
        self.post_stage(s, stg)
        n_rows, n_keys = c.execute(f"SELECT count(*), count(DISTINCT {k}) FROM {stg}").fetchone()
        if n_keys == 0 and not self.keep_empty(s):
            c.execute(f"DROP TABLE {stg}")
            return None
        if n_rows != n_keys and s.name in self.single_row_streams:
            log.warning("%s: %s has %d duplicate %s rows; they become row_seq > 1", sid, path.name, n_rows - n_keys, k)
        rows_t, seq_t, key_t = f"stg_h_{s.name}", f"stg_seq_{s.name}", f"stg_key_{s.name}"
        stage_hash_tables(c, stg, k, s.columns, rows_t, seq_t, key_t)
        return Staged(stream=s, snapshot_id=sid, rows_table=rows_t, seq_table=seq_t, key_table=key_t,
                      n_rows=n_rows, n_keys=n_keys, n_columns=len(headers), csv_mode=csv_mode, path=path)

    def _apply(self, st: Staged, snap: SnapshotSpec, staged: list[Staged]) -> Stats:
        c = self.con
        s, k, gone = st.stream, self.key, self.gone_kinds
        sid = snap.snapshot_id
        as_of = snap.as_of_date.isoformat()
        # hashing a 10 GB NPPES member costs seconds per snapshot; only for bookkeeping that records it
        sha256 = filesources.sha256_of(st.path) if "sha256" in self._columns(self.stream_table) else None
        out = Stats(stream=s.name, file=st.path.name, bytes=st.path.stat().st_size, sha256=sha256,
                    n_columns=st.n_columns, n_rows=st.n_rows, n_keys=st.n_keys, csv_mode=st.csv_mode)
        delta = f"delta_{s.name}"
        c.execute(f"""CREATE OR REPLACE TEMP TABLE {delta} AS
            SELECT coalesce(n.{k}, o.{k}) AS {k},
                   CASE WHEN o.{k} IS NULL THEN 'new' WHEN n.{k} IS NULL THEN {self.absent_kind_sql(s, staged)}
                        WHEN n.version_hash <> o.version_hash THEN 'changed' ELSE 'unchanged' END AS kind,
                   n.version_hash AS new_hash, o.effective_date AS old_effective_date
            FROM {st.key_table} n FULL OUTER JOIN {s.name}_state o ON n.{k} = o.{k}""")
        counts = dict(c.execute(f"SELECT kind, count(*) FROM {delta} GROUP BY kind").fetchall())
        out.n_new, out.n_changed, out.n_unchanged = counts.get("new", 0), counts.get("changed", 0), counts.get("unchanged", 0)
        out.n_absent, out.n_moved = counts.get("absent", 0), counts.get("moved", 0)
        prev = out.n_changed + out.n_unchanged + sum(counts.get(g, 0) for g in gone)
        counted = self.counted_absent(st, delta, out)
        if prev >= 1000 and counted > self.max_absent_share * prev:
            raise RuntimeError(f"{sid} {s.name}: {counted:,} of {prev:,} {self.key_noun} would be marked absent "
                               f"({counted / prev:.1%} > {self.max_absent_share:.0%}); refusing. Check the file, "
                               f"or raise --max-absent-share if this is genuine.")
        prev_sid = self.last_present_snapshot(s.name)
        closing = ", ".join(f"'{g}'" for g in ("changed", *gone))
        c.execute(f"""UPDATE {s.name}_version v
                      SET end_date = DATE '{as_of}', is_current = false,
                          last_snapshot_id = {prev_sid if prev_sid is not None else 'NULL'}, close_reason = d.kind
                      FROM {delta} d WHERE v.{k} = d.{k} AND v.is_current AND d.kind IN ({closing})""")
        typed, bad = self.typed_select(s)
        for stat, exprs in bad.items():
            if exprs:
                setattr(out, stat, sum(c.execute(f"SELECT {', '.join(exprs)} FROM {st.rows_table}").fetchone()))
        meta_names = ", ".join(n for n, _ in meta_columns(k))
        data_names = ", ".join(col.name for col in s.columns)
        c.execute(f"""INSERT INTO {s.name}_version ({meta_names}, {data_names})
            SELECT h.{k}, DATE '{as_of}', NULL, true, {sid}, {sid}, NULL, d.new_hash, q.row_seq, {', '.join(typed)}
            FROM {st.rows_table} h JOIN {st.seq_table} q ON q.rid = h.rowid JOIN {delta} d ON h.{k} = d.{k}
            WHERE d.kind IN ('new', 'changed')""")
        c.execute(f"""CREATE OR REPLACE TABLE {s.name}_state AS
            SELECT {k}, new_hash AS version_hash,
                   CASE WHEN kind = 'unchanged' THEN old_effective_date ELSE DATE '{as_of}' END AS effective_date
            FROM {delta} WHERE kind NOT IN ({', '.join(f"'{g}'" for g in gone)})""")
        c.execute(f"DROP TABLE {delta}")
        out.content_fingerprint = c.execute(fingerprint_sql(f"(SELECT {k} AS npi, version_hash FROM {s.name}_state)")).fetchone()[0]
        return out

    def finalize(self) -> None:
        finalize(self.con, type(self))

    def close(self) -> None:
        self.con.close()


def _missing(table: str, column: str):
    raise ValueError(f"{table}.{column}: no value for this bookkeeping column")


Spec = type[VersionedWarehouse]


def _tables(con: duckdb.DuckDBPyConnection) -> set[str]:
    return {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}


def loaded(con: duckdb.DuckDBPyConnection, spec: Spec) -> bool:
    """True when the warehouse holds at least one snapshot of ``spec``."""
    return (spec.snapshot_table in _tables(con)
            and con.execute(f"SELECT count(*) FROM {spec.snapshot_table}").fetchone()[0] > 0)


def finalize(con: duckdb.DuckDBPyConnection, spec: Spec) -> None:
    """Stamp last_snapshot_id on open versions with the latest snapshot in which their stream was present."""
    if not loaded(con, spec):
        return
    for s in spec.streams:
        row = con.execute(f"SELECT max(snapshot_id) FROM {spec.stream_table} WHERE stream = ?", [s.name]).fetchone()
        if row and row[0] is not None:
            con.execute(f"UPDATE {s.name}_version SET last_snapshot_id = {row[0]} WHERE is_current AND last_snapshot_id <> {row[0]}")


def rollback(con: duckdb.DuckDBPyConnection, spec: Spec, keep_through: int) -> dict[str, int]:
    """Remove every snapshot of ``spec`` newer than ``keep_through`` (a snapshot id, YYYYMMDD).

    Versions opened after that snapshot are deleted, versions closed after it are reopened, the
    bookkeeping rows are dropped and the state tables are rebuilt from the reopened versions. Use it to
    insert a newly found historical snapshot without a full rebuild: roll back to the one before it,
    then load again. Returns the versions removed per stream and the snapshots removed.
    """
    d = ymd(keep_through)
    k = spec.key
    removed: dict[str, int] = {}
    con.execute("BEGIN")
    try:
        for s in spec.streams:
            v = f"{s.name}_version"
            removed[s.name] = con.execute(f"SELECT count(*) FROM {v} WHERE first_snapshot_id > {keep_through}").fetchone()[0]
            con.execute(f"DELETE FROM {v} WHERE first_snapshot_id > {keep_through}")
            con.execute(f"UPDATE {v} SET end_date = NULL, is_current = true, close_reason = NULL WHERE end_date > DATE '{d}'")
            con.execute(f"""CREATE OR REPLACE TABLE {s.name}_state AS
                            SELECT {k}, version_hash, effective_date FROM {v} WHERE is_current AND row_seq = 1""")
        con.execute(f"DELETE FROM {spec.stream_table} WHERE snapshot_id > {keep_through}")
        removed["snapshots"] = con.execute(f"SELECT count(*) FROM {spec.snapshot_table} WHERE snapshot_id > {keep_through}").fetchone()[0]
        con.execute(f"DELETE FROM {spec.snapshot_table} WHERE snapshot_id > {keep_through}")
        finalize(con, spec)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return removed


def reset(con: duckdb.DuckDBPyConnection, spec: Spec) -> list[str]:
    """Drop every table of ``spec`` (versions, states and bookkeeping); returns the ones dropped."""
    tables = _tables(con)
    todo = [t for t in [spec.snapshot_table, spec.stream_table] + [f"{s.name}_{x}" for s in spec.streams for x in ("version", "state")]
            if t in tables]
    con.execute("BEGIN")
    try:
        for t in todo:
            con.execute(f"DROP TABLE {t}")
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return todo


def stored_fingerprints(con: duckdb.DuckDBPyConnection, spec: Spec) -> dict[tuple[int, str], str | None]:
    return versioning.stored_fingerprints(con, spec.stream_table) if loaded(con, spec) else {}


def recompute_fingerprints(con: duckdb.DuckDBPyConnection, spec: Spec) -> dict[tuple[int, str], str]:
    return versioning.recompute_fingerprints(con, spec.stream_table, spec.key) if loaded(con, spec) else {}
