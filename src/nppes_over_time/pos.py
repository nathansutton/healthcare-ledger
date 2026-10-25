"""CMS Provider of Services (POS) files as quarterly SCD Type 2 tables of Medicare-certified facilities.

The POS file is CMS's quarterly extract of its certification systems: every institutional provider
and supplier certified to take part in Medicare or Medicaid (hospitals, nursing homes, home health
agencies, hospices, dialysis facilities, ambulatory surgical centers, rural health clinics, FQHCs
and the rest), keyed by the *CMS Certification Number* (CCN, ``PRVDR_NUM``), with its name,
address, category, certification and termination dates, ownership, beds, staffing and services.
Clinical laboratories (the CLIA file) are a separate POS file and are not loaded.

CMS publishes two files per quarter since the end of 2023, and the loader keeps them as two streams
of the same shape, both keyed by CCN:

``pos_facility`` (the QIES file)
    ``Provider of Services File - Hospital & Non-Hospital Facilities``, renamed ``Quality Improvement
    and Evaluation System`` in 2025: annual at year end from December 2011, quarterly from March 2019.
    One row per CCN, 470 to 500 upper-case SAS-named columns that changed in 2013, 2014 and 2017.
``pos_facility_iqies`` (the iQIES file)
    ``Provider of Services File - Internet Quality Improvement and Evaluation System``, from the fourth
    quarter of 2023. CMS moves provider types out of the QIES file into this one as it migrates
    them: home health agencies, hospices and ambulatory surgical centers (2023-Q4), nursing homes
    (2025-Q3), dialysis facilities, ICF/IIDs, outpatient therapy, portable x-ray, CORFs and OPOs
    (2026-Q1), then rural health clinics, FQHCs, CMHCs and PRTFs (2026-Q3), leaving hospitals alone in
    the QIES file. Lower-case column names, a subset of the QIES names plus new ones, 102 to 186
    columns; a different coding of the provider type (``prvdr_type_id``, ``prvdr_sbtyp_id``).

A CCN that leaves one file and appears in the other in the same quarter is closed with
``close_reason = 'moved'`` in the stream it left, not ``'absent'``; the facility's history continues in
the other table. Not every CCN of a migrating type reaches the other file (one in five home health
agencies, hospices and ASCs of the 2023-Q3 file is in neither 2023-Q4 file, almost all of them
terminated years before); those close as ``'absent'``. The 5% absent guard against a truncated file
therefore counts only absent CCNs whose provider type still has rows in the file: a type whose rows
all left the file has changed systems, which is not evidence of a damaged file.

The source directory holds one subdirectory per quarter, named ``YYYY-QN``, with the quarter's CSV
(or two CSVs) inside under CMS's own file names, which are not systematic (``POS_OTHER_DEC19.csv``,
``other_data_Q2 2024.csv``, ``PQWB.POSQ.CLIA.DATA.MAR25.csv`` is the Q1 2025 hospital file). The
loader tells the two files apart by their header. The *as-of date* of a quarter is its last day
(the period data.cms.gov records for the dataset), so the snapshot id is ``YYYYMMDD`` of that day.

Rules, following docs/METHODOLOGY.md sections 2 to 5 with the CCN in place of the NPI: trimmed
text; blanks and placeholders to NULL (the iQIES files from 2024-Q4 print ``Not Available`` and
``Not Applicable`` where earlier files were blank); dates normalized to ISO before hashing, because
CMS writes them ``YYYYMMDD`` in the QIES file and either ``YYYY-MM-DD`` or ``MM/DD/YYYY`` in the
iQIES file depending on the quarter, and counts normalized the same way, because the files write
``01``, ``1``, ``1.00`` and ``.50`` for the same numbers in different quarters (a value that is not a
date or a number is kept in the hash as written, stored as NULL and counted in ``n_bad_dates`` or
``n_bad_numbers``); the tax-id token rule of 2a on the free-text columns (facility and
organization names, street address, the Medicaid vendor number, which holds nine-digit state ids);
md5 row hashes over every column with the columns that are not in every file of the stream
``hash_optional``; one transaction per quarter; a content fingerprint per stream and quarter. The
iQIES ``processing_date`` (the date the file was produced, one value per file) is not a facility
attribute and is not loaded. The column schema is ``pos_columns.csv`` next to this module, built from
the headers of every file CMS has published, with CMS's descriptions.
"""

from __future__ import annotations

import csv
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import duckdb

from . import filesources, schema
from .loader import SEP, fingerprint_sql
from .ppef import repair_utf8
from .schema import Column, header_key
from .scrub import has_token, redact_expr

log = logging.getLogger(__name__)

KEY = "ccn"
QUARTER_DIR_RE = re.compile(r"^(?P<year>\d{4})-Q(?P<q>[1-4])$")
COLUMNS_FILE = Path(__file__).with_name("pos_columns.csv")
# the iQIES files from 2024-Q4 print these where the earlier files, and the QIES file, are blank
POS_PLACEHOLDERS = ("Not Available", "Not Applicable", "N/A")
# columns the token rule of METHODOLOGY 2a applies to: names, the street address, the Medicaid vendor
# number (state ids, some of them nine digits) and the service descriptions of the 2011 to 2013 files
FREE_TEXT_RE = re.compile(r"(name|_desc)$|^(st_adr|mdcd_vndr_num)$")
DATE_FORMATS = ("%Y%m%d", "%Y-%m-%d", "%m/%d/%Y")
IGNORED_HEADERS = {header_key("processing_date")}


@dataclass(frozen=True)
class PosStream:
    name: str
    key_header: str                # CMS header of the CCN in this file
    signature: str                 # header key that only this file has
    category_column: str           # the provider type: a type whose rows all leave the file has moved systems
    columns: tuple[Column, ...]
    description: str

    @property
    def data_columns(self) -> list[str]:
        return [c.name for c in self.columns]


def _read_columns() -> dict[str, tuple[Column, ...]]:
    out: dict[str, list[Column]] = {"qies": [], "iqies": []}
    with open(COLUMNS_FILE, newline="") as f:
        for r in csv.DictReader(f):
            out[r["stream"]].append(Column(r["name"], r["cms_header"], kind=r["kind"], hash_optional=r["hash_optional"] == "yes"))
    return {k: tuple(v) for k, v in out.items()}


def column_spans() -> dict[tuple[str, str], tuple[str, str]]:
    """(stream, column) -> (first quarter, last quarter) the column appears in CMS's files."""
    with open(COLUMNS_FILE, newline="") as f:
        return {(r["stream"], r["name"]): (r["first_quarter"], r["last_quarter"]) for r in csv.DictReader(f)}


def column_descriptions() -> dict[tuple[str, str], str]:
    with open(COLUMNS_FILE, newline="") as f:
        return {(r["stream"], r["name"]): r["description"] for r in csv.DictReader(f)}


_COLUMNS = _read_columns()
QIES = PosStream(
    name="pos_facility",
    key_header="PRVDR_NUM",
    signature=header_key("PRVDR_CTGRY_CD"),
    category_column="prvdr_ctgry_cd",
    columns=_COLUMNS["qies"],
    description="Medicare-certified facilities in the POS Hospital & Non-Hospital Facilities (QIES) file: one row per CCN with category, "
                "address, certification and termination, ownership, beds, staffing and services. Annual at year end 2011 to 2018, "
                "quarterly from 2019. Provider types leave this file for the iQIES file from 2023-Q4 (close_reason 'moved').",
)
IQIES = PosStream(
    name="pos_facility_iqies",
    key_header="prvdr_num",
    signature=header_key("prvdr_type_id"),
    category_column="prvdr_type_id",
    columns=_COLUMNS["iqies"],
    description="Medicare-certified facilities in the POS iQIES file, from 2023-Q4: the provider types CMS has migrated to iQIES "
                "(home health, hospice and ASC first; nursing homes from 2025-Q3; dialysis, ICF/IID, therapy, x-ray, CORF and OPO "
                "from 2026-Q1; RHC, FQHC, CMHC and PRTF from 2026-Q3). One row per CCN; prvdr_type_id codes the type.",
)
POS_STREAMS: tuple[PosStream, ...] = (QIES, IQIES)
POS_STREAM_BY_NAME = {s.name: s for s in POS_STREAMS}
STREAM_KEY = {"qies": QIES, "iqies": IQIES}

META_COLUMNS = [
    (KEY, "VARCHAR"), ("effective_date", "DATE"), ("end_date", "DATE"), ("is_current", "BOOLEAN"),
    ("first_snapshot_id", "INTEGER"), ("last_snapshot_id", "INTEGER"), ("close_reason", "VARCHAR"),
    ("version_hash", "VARCHAR"), ("row_seq", "INTEGER"),
]


def free_text_columns(s: PosStream) -> list[str]:
    return [c.name for c in s.columns if c.kind == "text" and FREE_TEXT_RE.search(c.name)]


# ------------------------------------------------------------------ discovery
@dataclass
class PosSnapshot:
    directory: Path
    as_of_date: date
    members: dict[str, Path] = field(default_factory=dict)   # stream name -> csv path

    @property
    def snapshot_id(self) -> int:
        return int(self.as_of_date.strftime("%Y%m%d"))

    @property
    def label(self) -> str:
        return self.directory.name

    def has(self, stream: str) -> bool:
        return stream in self.members and self.members[stream].stat().st_size > 0


def quarter_end(label: str) -> date:
    """``2019-Q1`` -> 2019-03-31: the last day of the quarter is the as-of date of its POS file."""
    m = QUARTER_DIR_RE.match(label)
    if not m:
        raise ValueError(f"{label!r} is not a quarter directory name (YYYY-QN)")
    y, q = int(m.group("year")), int(m.group("q"))
    month = 3 * q
    last = {3: 31, 6: 30, 9: 30, 12: 31}[month]
    return date(y, month, last)


def read_header(path: Path) -> list[str]:
    with open(path, newline="", encoding="utf-8", errors="replace") as fh:
        return [h.lstrip("﻿") for h in next(csv.reader(fh))]


def classify(header: list[str]) -> PosStream | None:
    """Which POS file a header belongs to, by the columns only that file has; None for neither."""
    keys = {header_key(h) for h in header}
    for s in POS_STREAMS:
        if header_key(s.key_header) in keys and s.signature in keys:
            return s
    return None


def inspect_quarter(directory: Path) -> PosSnapshot:
    members: dict[str, Path] = {}
    for f in sorted(directory.iterdir()):
        if not f.is_file() or f.suffix.lower() != ".csv":
            continue
        s = classify(read_header(f))
        if s is None:
            raise ValueError(f"{directory.name}/{f.name}: not a POS file (no {QIES.key_header} with PRVDR_CTGRY_CD, nor prvdr_num with prvdr_type_id)")
        if s.name in members:
            raise ValueError(f"{directory.name}: two {s.name} files, {members[s.name].name} and {f.name}")
        members[s.name] = f
    if not members:
        raise ValueError(f"{directory.name}: no POS csv file")
    return PosSnapshot(directory=directory, as_of_date=quarter_end(directory.name), members=members)


def discover_pos(source_dir: Path) -> list[PosSnapshot]:
    """Quarter directories of ``source_dir`` in as-of order."""
    snaps = [inspect_quarter(d) for d in sorted(source_dir.iterdir()) if d.is_dir() and QUARTER_DIR_RE.match(d.name)]
    snaps.sort(key=lambda s: s.as_of_date)
    return snaps


def map_headers(stream: PosStream, headers: list[str]) -> tuple[dict[str, str | None], list[str]]:
    """Canonical column -> actual header (None when absent); plus headers that match nothing."""
    by_key: dict[str, str] = {}
    for h in headers:
        k = header_key(h)
        if k in by_key and by_key[k] != h:
            raise ValueError(f"headers {by_key[k]!r} and {h!r} collide on key {k!r}")
        by_key[k] = h
    key_hdr = by_key.get(header_key(stream.key_header))
    if key_hdr is None:
        raise ValueError(f"file has no {stream.key_header} column")
    mapping: dict[str, str | None] = {KEY: key_hdr}
    used = {key_hdr}
    for col in stream.columns:
        hit = next((by_key[k] for k in col.keys() if k in by_key), None)
        mapping[col.name] = hit
        if hit is not None:
            used.add(hit)
    unmatched = [h for h in headers if h not in used and header_key(h) not in IGNORED_HEADERS]
    return mapping, unmatched


def _q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def _sql_type(kind: str) -> str:
    return {"text": "VARCHAR", "date": "DATE", "number": "DOUBLE"}[kind]


def date_norm(expr: str) -> str:
    """ISO text of a date written in any of CMS's three formats; the raw text when it parses as none."""
    parsed = "coalesce(" + ", ".join(f"try_strptime({expr}, '{f}')" for f in DATE_FORMATS) + ")"
    return f"coalesce(strftime({parsed}, '%Y-%m-%d'), {expr})"


def number_norm(expr: str) -> str:
    """Canonical text of a count (``01``, ``1`` and ``1.00`` agree); the raw text when it is not a number."""
    return f"coalesce(CAST(try_cast({expr} AS DOUBLE) AS VARCHAR), {expr})"


# how a normalized staged value is typed on insert, and the test that it did not parse
_TYPED = {"date": ("try_strptime({c}, '%Y-%m-%d')::DATE", "try_strptime({c}, '%Y-%m-%d') IS NULL"),
          "number": ("try_cast({c} AS DOUBLE)", "try_cast({c} AS DOUBLE) IS NULL")}


# -------------------------------------------------------------------- warehouse
@dataclass
class PosStats:
    stream: str
    file: str
    bytes: int
    sha256: str
    n_columns: int
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


def loaded(con: duckdb.DuckDBPyConnection) -> bool:
    """True when the warehouse holds at least one POS quarter."""
    tables = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    return "pos_snapshot" in tables and con.execute("SELECT count(*) FROM pos_snapshot").fetchone()[0] > 0


def init_schema(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("""
        CREATE TABLE IF NOT EXISTS pos_snapshot (
            snapshot_id INTEGER NOT NULL, as_of_date DATE NOT NULL, directory VARCHAR NOT NULL,
            streams_present VARCHAR[], loaded_at TIMESTAMP NOT NULL, seconds DOUBLE)""")
    con.execute("""
        CREATE TABLE IF NOT EXISTS pos_snapshot_stream (
            snapshot_id INTEGER NOT NULL, stream VARCHAR NOT NULL, file VARCHAR NOT NULL,
            bytes BIGINT, sha256 VARCHAR, n_columns INTEGER, n_rows BIGINT, n_keys BIGINT,
            n_new BIGINT, n_changed BIGINT, n_unchanged BIGINT, n_absent BIGINT, n_moved BIGINT, n_bad_dates BIGINT,
            n_bad_numbers BIGINT, csv_mode VARCHAR, content_fingerprint VARCHAR)""")
    for s in POS_STREAMS:
        cols = ", ".join(f"{n} {t}" for n, t in META_COLUMNS)
        data = ", ".join(f"{c.name} {_sql_type(c.kind)}" for c in s.columns)
        con.execute(f"CREATE TABLE IF NOT EXISTS {s.name}_version ({cols}, {data})")
        con.execute(f"CREATE TABLE IF NOT EXISTS {s.name}_state ({KEY} VARCHAR NOT NULL, version_hash VARCHAR NOT NULL, effective_date DATE NOT NULL)")
        existing = {r[0] for r in con.execute(f"DESCRIBE {s.name}_version").fetchall()}
        for c in s.columns:
            if c.name not in existing:
                log.info("adding column %s.%s to existing warehouse", s.name, c.name)
                con.execute(f"ALTER TABLE {s.name}_version ADD COLUMN {c.name} {_sql_type(c.kind)}")


@dataclass
class _Staged:
    stream: PosStream
    rows_table: str
    seq_table: str
    key_table: str
    n_rows: int
    n_keys: int
    n_columns: int
    csv_mode: str
    path: Path


class PosWarehouse:
    def __init__(self, db_path: Path, *, threads: int | None = None, memory_limit: str | None = None,
                 work_dir: Path | None = None, max_absent_share: float = 0.05):
        self.db_path = Path(db_path)
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
        self._scratch: list[Path] = []
        init_schema(self.con)

    def loaded_snapshot_ids(self) -> list[int]:
        return [r[0] for r in self.con.execute("SELECT snapshot_id FROM pos_snapshot ORDER BY 1").fetchall()]

    def last_present_snapshot(self, stream: str) -> int | None:
        row = self.con.execute("SELECT max(snapshot_id) FROM pos_snapshot_stream WHERE stream = ?", [stream]).fetchone()
        return row[0] if row else None

    # ---------------------------------------------------------------- load
    def load_snapshot(self, snap: PosSnapshot) -> list[PosStats]:
        sid = snap.snapshot_id
        done = self.loaded_snapshot_ids()
        if sid in done:
            raise ValueError(f"POS snapshot {sid} ({snap.label}) already loaded")
        if done and done[-1] > sid:
            raise ValueError(f"POS snapshot {sid} ({snap.label}) is older than last loaded {done[-1]}; load in order")
        t0 = time.time()
        c = self.con
        staged: list[_Staged] = []
        keys_any = "stg_keys_any"
        try:
            for s in POS_STREAMS:
                if not snap.has(s.name):
                    log.info("%s %s: stream %s not present, skipping", sid, snap.label, s.name)
                    continue
                st = self._stage(s, snap.members[s.name], sid)
                if st is None:
                    log.warning("%s: %s has no rows, treating as not present", sid, snap.members[s.name].name)
                    continue
                staged.append(st)
            # every CCN in any file of this quarter: a CCN missing from one file but present in the other
            # has moved between CMS's systems, not left the registry
            c.execute(f"CREATE OR REPLACE TEMP TABLE {keys_any} AS "
                      + (" UNION ".join(f"SELECT {KEY} FROM {st.key_table}" for st in staged) if staged else f"SELECT NULL::VARCHAR AS {KEY} WHERE false"))
            c.execute("BEGIN")
            try:
                stats = [self._apply(st, snap, keys_any) for st in staged]
                for out in stats:
                    c.execute("INSERT INTO pos_snapshot_stream VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                              [sid, out.stream, out.file, out.bytes, out.sha256, out.n_columns, out.n_rows, out.n_keys, out.n_new, out.n_changed, out.n_unchanged, out.n_absent,
                               out.n_moved, out.n_bad_dates, out.n_bad_numbers, out.csv_mode, out.content_fingerprint])
                    log.info("%s %-20s rows=%-7d keys=%-7d new=%-6d changed=%-6d unchanged=%-7d absent=%-5d moved=%-5d bad_dates=%d bad_numbers=%d",
                             sid, out.stream, out.n_rows, out.n_keys, out.n_new, out.n_changed, out.n_unchanged, out.n_absent,
                             out.n_moved, out.n_bad_dates, out.n_bad_numbers)
                c.execute("INSERT INTO pos_snapshot VALUES (?,?,?,?,now(),?)",
                          [sid, snap.as_of_date, snap.label, [o.stream for o in stats], time.time() - t0])
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise
        finally:
            for st in staged:
                for tbl in (st.rows_table, st.seq_table, st.key_table):
                    c.execute(f"DROP TABLE IF EXISTS {tbl}")
            c.execute(f"DROP TABLE IF EXISTS {keys_any}")
            for p in self._scratch:
                p.unlink(missing_ok=True)
            self._scratch.clear()
        log.info("%s %s done in %.0fs", sid, snap.label, time.time() - t0)
        return stats

    def _read_csv(self, table: str, path: Path) -> str:
        base = f"'{path}', header=true, all_varchar=true, delim=',', quote='\"', escape='\"'"
        lenient = "strict_mode=false, null_padding=true, parallel=false, max_line_size=20000000"
        attempts = [("strict", f"read_csv({base})"), ("lenient", f"read_csv({base}, {lenient})"),
                    ("strict-latin1", f"read_csv({base}, encoding='latin-1')"),
                    ("lenient-latin1", f"read_csv({base}, {lenient}, encoding='latin-1')")]
        last: Exception | None = None
        for mode, reader in attempts:
            try:
                self.con.execute(f"CREATE OR REPLACE TEMP TABLE {table} AS SELECT * FROM {reader}")
            except duckdb.Error as e:  # noqa: PERF203
                last = e
                log.debug("%s reader failed on %s: %s", mode, path.name, str(e).splitlines()[0])
                continue
            if mode != "strict":
                log.warning("%s parsed with %s reader", path.name, mode)
            return mode
        repaired = self.work_dir / f"{path.name}.utf8.csv"
        n = repair_utf8(path, repaired)
        self._scratch.append(repaired)
        try:
            self.con.execute(f"CREATE OR REPLACE TEMP TABLE {table} AS SELECT * FROM read_csv('{repaired}', header=true, "
                             f"all_varchar=true, delim=',', quote='\"', escape='\"')")
        except duckdb.Error as e:
            raise RuntimeError(f"could not parse {path}, even after repairing {n} non-UTF-8 line(s)") from (last or e)
        log.warning("%s parsed after repairing %d non-UTF-8 line(s) (decoded as latin-1)", path.name, n)
        return f"repaired-utf8+{n}"

    def _stage(self, s: PosStream, path: Path, sid: int) -> _Staged | None:
        c = self.con
        raw = f"raw_{s.name}"
        csv_mode = self._read_csv(raw, path)
        headers = [r[0] for r in c.execute(f"DESCRIBE {raw}").fetchall()]
        mapping, unmatched = map_headers(s, headers)
        if unmatched:
            raise ValueError(f"{path.name}: unmapped columns {unmatched}; add them to pos_columns.csv")
        sel = [f"nullif(trim({_q(mapping[KEY])}), '') AS {KEY}"]
        free_text = set(free_text_columns(s))
        for col in s.columns:
            src = mapping[col.name]
            if src is None:
                sel.append(f"NULL::VARCHAR AS {col.name}")
                continue
            expr = f"trim({_q(src)})"
            for tok in ["''"] + [f"'{t}'" for t in schema.PLACEHOLDERS + POS_PLACEHOLDERS]:
                expr = f"nullif({expr}, {tok})"
            if col.kind == "date":
                expr = date_norm(expr)
            elif col.kind == "number":
                expr = number_norm(expr)
            elif col.name in free_text:
                expr = redact_expr(expr, col.name)
            sel.append(f"{expr} AS {col.name}")
        stg = f"stg_{s.name}"
        c.execute(f"CREATE OR REPLACE TEMP TABLE {stg} AS SELECT {', '.join(sel)} FROM {raw} WHERE {KEY} IS NOT NULL")
        c.execute(f"DROP TABLE {raw}")
        n_rows, n_keys = c.execute(f"SELECT count(*), count(DISTINCT {KEY}) FROM {stg}").fetchone()
        if n_keys == 0:
            c.execute(f"DROP TABLE {stg}")
            return None
        if n_rows != n_keys:
            log.warning("%s: %s has %d duplicate CCN rows; they become row_seq > 1", sid, path.name, n_rows - n_keys)
        hash_expr = (f"concat_ws({SEP}, " + ", ".join(f"coalesce({col.name}, '')" for col in s.columns if not col.hash_optional) + ")"
                     + "".join(f" || coalesce(chr(30) || '{col.name}=' || {col.name}, '')" for col in s.columns if col.hash_optional))
        rows_t, seq_t, key_t = f"stg_h_{s.name}", f"stg_seq_{s.name}", f"stg_key_{s.name}"
        c.execute(f"CREATE OR REPLACE TEMP TABLE {rows_t} AS SELECT *, md5({hash_expr}) AS row_hash FROM {stg}")
        c.execute(f"DROP TABLE {stg}")
        c.execute(f"""CREATE OR REPLACE TEMP TABLE {seq_t} AS
                      SELECT rowid AS rid, {KEY}, row_hash, row_number() OVER (PARTITION BY {KEY} ORDER BY row_hash) AS row_seq
                      FROM {rows_t}""")
        c.execute(f"""CREATE OR REPLACE TEMP TABLE {key_t} AS
                      SELECT {KEY}, CASE WHEN count(*) = 1 THEN any_value(row_hash)
                                         ELSE md5(string_agg(row_hash, ',' ORDER BY row_hash)) END AS version_hash
                      FROM {seq_t} GROUP BY {KEY}""")
        return _Staged(stream=s, rows_table=rows_t, seq_table=seq_t, key_table=key_t, n_rows=n_rows, n_keys=n_keys,
                       n_columns=len(headers), csv_mode=csv_mode, path=path)

    def _apply(self, st: _Staged, snap: PosSnapshot, keys_any: str) -> PosStats:
        c = self.con
        s = st.stream
        sid = snap.snapshot_id
        as_of = snap.as_of_date.isoformat()
        out = PosStats(stream=s.name, file=st.path.name, bytes=st.path.stat().st_size, sha256=filesources.sha256_of(st.path),
                       n_columns=st.n_columns, n_rows=st.n_rows, n_keys=st.n_keys, csv_mode=st.csv_mode)
        delta = f"delta_{s.name}"
        c.execute(f"""CREATE OR REPLACE TEMP TABLE {delta} AS
            SELECT coalesce(n.{KEY}, o.{KEY}) AS {KEY},
                   CASE WHEN o.{KEY} IS NULL THEN 'new'
                        WHEN n.{KEY} IS NULL THEN (CASE WHEN a.{KEY} IS NULL THEN 'absent' ELSE 'moved' END)
                        WHEN n.version_hash <> o.version_hash THEN 'changed' ELSE 'unchanged' END AS kind,
                   n.version_hash AS new_hash, o.effective_date AS old_effective_date
            FROM {st.key_table} n FULL OUTER JOIN {s.name}_state o ON n.{KEY} = o.{KEY}
            LEFT JOIN {keys_any} a ON a.{KEY} = o.{KEY}""")
        counts = dict(c.execute(f"SELECT kind, count(*) FROM {delta} GROUP BY kind").fetchall())
        out.n_new, out.n_changed = counts.get("new", 0), counts.get("changed", 0)
        out.n_unchanged, out.n_absent, out.n_moved = counts.get("unchanged", 0), counts.get("absent", 0), counts.get("moved", 0)
        prev = out.n_changed + out.n_unchanged + out.n_absent + out.n_moved
        # The guard counts only absent CCNs of a provider type that still has rows in this file: when CMS moves a
        # whole type to the other system, the CCNs of that type it did not carry over are gone for a known reason.
        cat = s.category_column
        counted = c.execute(f"""SELECT count(*) FROM {delta} d
                                JOIN {s.name}_version v ON v.{KEY} = d.{KEY} AND v.is_current AND v.row_seq = 1
                                WHERE d.kind = 'absent' AND v.{cat} IN (SELECT DISTINCT {cat} FROM {st.rows_table} WHERE {cat} IS NOT NULL)""").fetchone()[0]
        if out.n_absent > counted:
            log.warning("%s %s: %d of %d absent CCNs belong to provider types with no rows left in this file (moved systems); "
                        "not counted by the absent guard", sid, s.name, out.n_absent - counted, out.n_absent)
        if prev >= 1000 and counted > self.max_absent_share * prev:
            raise RuntimeError(f"{sid} {s.name}: {counted:,} of {prev:,} CCNs would be marked absent "
                               f"({counted / prev:.1%} > {self.max_absent_share:.0%}); refusing. Check the file, "
                               f"or raise --max-absent-share if this is genuine.")
        prev_sid = self.last_present_snapshot(s.name)
        c.execute(f"""UPDATE {s.name}_version v
                      SET end_date = DATE '{as_of}', is_current = false,
                          last_snapshot_id = {prev_sid if prev_sid is not None else 'NULL'}, close_reason = d.kind
                      FROM {delta} d WHERE v.{KEY} = d.{KEY} AND v.is_current AND d.kind IN ('changed', 'absent', 'moved')""")
        typed, bad = [], {"date": [], "number": []}
        for col in s.columns:
            if col.kind in _TYPED:
                cast, failed = _TYPED[col.kind]
                typed.append(f"{cast.format(c=col.name)} AS {col.name}")
                bad[col.kind].append(f"count(*) FILTER (WHERE {col.name} IS NOT NULL AND {failed.format(c=col.name)})")
            else:
                typed.append(col.name)
        for kind, exprs in bad.items():
            if exprs:
                n = sum(c.execute(f"SELECT {', '.join(exprs)} FROM {st.rows_table}").fetchone())
                if kind == "date":
                    out.n_bad_dates = n
                else:
                    out.n_bad_numbers = n
        meta_names = ", ".join(n for n, _ in META_COLUMNS)
        data_names = ", ".join(col.name for col in s.columns)
        c.execute(f"""INSERT INTO {s.name}_version ({meta_names}, {data_names})
            SELECT h.{KEY}, DATE '{as_of}', NULL, true, {sid}, {sid}, NULL, d.new_hash, q.row_seq, {', '.join(typed)}
            FROM {st.rows_table} h JOIN {st.seq_table} q ON q.rid = h.rowid JOIN {delta} d ON h.{KEY} = d.{KEY}
            WHERE d.kind IN ('new', 'changed')""")
        c.execute(f"""CREATE OR REPLACE TABLE {s.name}_state AS
            SELECT {KEY}, new_hash AS version_hash,
                   CASE WHEN kind = 'unchanged' THEN old_effective_date ELSE DATE '{as_of}' END AS effective_date
            FROM {delta} WHERE kind NOT IN ('absent', 'moved')""")
        c.execute(f"DROP TABLE {delta}")
        out.content_fingerprint = c.execute(fingerprint_sql(f"(SELECT {KEY} AS npi, version_hash FROM {s.name}_state)")).fetchone()[0]
        return out

    def finalize(self) -> None:
        finalize(self.con)

    def close(self) -> None:
        self.con.close()


def finalize(con: duckdb.DuckDBPyConnection) -> None:
    """Stamp last_snapshot_id on open versions with the latest quarter in which their stream was present."""
    if not loaded(con):
        return
    for s in POS_STREAMS:
        row = con.execute("SELECT max(snapshot_id) FROM pos_snapshot_stream WHERE stream = ?", [s.name]).fetchone()
        if row and row[0] is not None:
            con.execute(f"UPDATE {s.name}_version SET last_snapshot_id = {row[0]} WHERE is_current AND last_snapshot_id <> {row[0]}")


def reset(con: duckdb.DuckDBPyConnection) -> list[str]:
    """Drop every POS table (versions, states and bookkeeping). Nothing else is touched."""
    tables = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    todo = [t for t in ["pos_snapshot", "pos_snapshot_stream"] + [f"{s.name}_{k}" for s in POS_STREAMS for k in ("version", "state")]
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


def interval_fingerprint(con: duckdb.DuckDBPyConnection, stream: str, as_of: str) -> str:
    rows = (f"(SELECT {KEY} AS npi, version_hash FROM {stream}_version WHERE row_seq = 1 "
            f"AND effective_date <= DATE '{as_of}' AND (end_date IS NULL OR end_date > DATE '{as_of}'))")
    return con.execute(fingerprint_sql(rows)).fetchone()[0]


def stored_fingerprints(con: duckdb.DuckDBPyConnection) -> dict[tuple[int, str], str | None]:
    if not loaded(con):
        return {}
    return {(sid, stream): fp for sid, stream, fp in
            con.execute("SELECT snapshot_id, stream, content_fingerprint FROM pos_snapshot_stream ORDER BY 1, 2").fetchall()}


def recompute_fingerprints(con: duckdb.DuckDBPyConnection) -> dict[tuple[int, str], str]:
    if not loaded(con):
        return {}
    out: dict[tuple[int, str], str] = {}
    for sid, stream in con.execute("SELECT snapshot_id, stream FROM pos_snapshot_stream ORDER BY 1, 2").fetchall():
        d = f"{sid // 10000:04d}-{sid // 100 % 100:02d}-{sid % 100:02d}"
        out[(sid, stream)] = interval_fingerprint(con, stream, d)
    return out


# -------------------------------------------------------------------- checks
# Shape of the key and state columns. A column read under the wrong header fails for most rows; a stray malformed
# CMS row (the 2024-Q1 file has one with tabs in the name and a one-digit state) is tolerated up to SHAPE_TOLERANCE.
SHAPE_CHECKS: dict[str, list[tuple[str, str]]] = {
    s.name: [(KEY, "^[0-9A-Z]{6}([0-9A-Z]{4})?$"), ("state_cd", "^[A-Z]{2}$")] for s in POS_STREAMS
}
SHAPE_TOLERANCE = 0.0001    # share of rows


def check_problems(con: duckdb.DuckDBPyConnection) -> list[str]:
    """Privacy (METHODOLOGY 2a, zero tolerance) and shape checks over every loaded pos_* version table; returns the failures."""
    if not loaded(con):
        return []
    problems: list[str] = []
    for s in POS_STREAMS:
        checks = [(f"privacy: {s.name}.{n} holds a tax-id token", has_token(n, n), 0) for n in free_text_columns(s)]
        checks += [(f"shape: {s.name}.{c} does not match {rx}", f"NOT regexp_matches({c}, '{rx}')", SHAPE_TOLERANCE)
                   for c, rx in SHAPE_CHECKS[s.name]]
        row = con.execute(f"SELECT count(*), {', '.join(f'count(*) FILTER (WHERE {p})' for _, p, _ in checks)} FROM {s.name}_version").fetchone()
        total = row[0]
        problems += [f"{d}: {n} row(s)" for (d, _, tol), n in zip(checks, row[1:]) if n > tol * total]
    return problems


# --------------------------------------------------------------------- export
SNAPSHOTS_FILE = "pos_snapshots.csv"
META_EXPORT = [KEY, "effective_date", "end_date", "is_current", "first_snapshot_id", "last_snapshot_id", "close_reason", "version_hash", "row_seq"]


def export_tables() -> dict[str, tuple[str, bool]]:
    """name -> (select sql, partition by year), in the shape export.tables() uses."""
    return {s.name: (f"SELECT {', '.join(META_EXPORT)}, {', '.join(s.data_columns)} FROM {s.name}_version", True)
            for s in POS_STREAMS}


def snapshots_sql() -> str:
    """One row per POS quarter with per-stream file, counts and fingerprints, for pos_snapshots.csv."""
    joins, cols = [], []
    for s in POS_STREAMS:
        a = s.name.replace("pos_facility", "q").replace("_", "")
        joins.append(f"LEFT JOIN pos_snapshot_stream {a} ON {a}.snapshot_id = s.snapshot_id AND {a}.stream = '{s.name}'")
        cols.append(f"{a}.file AS {s.name}_file, {a}.n_columns AS {s.name}_columns, {a}.n_rows AS {s.name}_rows, {a}.n_keys AS {s.name}_keys, "
                    f"{a}.n_new AS {s.name}_new, {a}.n_changed AS {s.name}_changed, {a}.n_absent AS {s.name}_absent, "
                    f"{a}.n_moved AS {s.name}_moved, {a}.n_bad_dates AS {s.name}_bad_dates, {a}.n_bad_numbers AS {s.name}_bad_numbers, "
                    f"{a}.content_fingerprint AS {s.name}_fingerprint")
    return (f"SELECT s.snapshot_id, s.as_of_date, s.directory, array_to_string(s.streams_present, ',') AS streams_present, "
            + ", ".join(cols) + " FROM pos_snapshot s " + " ".join(joins) + " ORDER BY s.snapshot_id")


META_DESC = {
    KEY: "CMS Certification Number (PRVDR_NUM): six or ten characters, the key of every POS file",
    "effective_date": "Last day of the first quarter whose POS file showed this version (inclusive)",
    "end_date": "Last day of the first quarter whose POS file no longer showed this version (exclusive); NULL while current",
    "is_current": "True if this is the latest version and the CCN was present in the latest quarter's file",
    "first_snapshot_id": "Snapshot id (YYYYMMDD of the quarter end) that first showed this version",
    "last_snapshot_id": "Snapshot id that last showed this version",
    "close_reason": "'changed' if superseded by a new version, 'absent' if the CCN left both POS files, 'moved' if it left this file "
                    "for the other POS file in the same quarter (its history continues there), NULL while current",
    "version_hash": "MD5 fingerprint of all data columns of this version",
    "row_seq": "Row number within a multi-row version, ordered by row hash (1 unless CMS repeated a CCN)",
}
REDACT_DESC = "Formatted SSNs and EINs and nine-digit numbers are replaced by [REDACTED] (METHODOLOGY 2a)"


def data_dictionary_rows() -> list[tuple[str, str, str, str, str]]:
    rows = []
    types = {"effective_date": "DATE", "end_date": "DATE", "is_current": "BOOLEAN", "first_snapshot_id": "INTEGER",
             "last_snapshot_id": "INTEGER", "row_seq": "INTEGER"}
    spans, descs = column_spans(), column_descriptions()
    for s, key in ((QIES, "qies"), (IQIES, "iqies")):
        for m in META_EXPORT:
            rows.append((s.name, m, types.get(m, "VARCHAR"), s.key_header if m == KEY else "", META_DESC[m]))
        redacted = set(free_text_columns(s))
        for c in s.columns:
            first, last = spans[(key, c.name)]
            notes = [descs[(key, c.name)]] if descs[(key, c.name)] else []
            if c.hash_optional:
                notes.append(f"In the CMS files from {first} to {last}; NULL otherwise and outside the version hash while NULL")
            if c.name in redacted:
                notes.append(REDACT_DESC)
            if c.kind == "date":
                notes.append("Date as CMS wrote it (YYYYMMDD, YYYY-MM-DD or MM/DD/YYYY), typed; NULL when it does not parse")
            elif c.kind == "number":
                notes.append("Count as CMS wrote it, typed (01, 1 and 1.00 are the same value); NULL when it is not a number")
            rows.append((s.name, c.name, _sql_type(c.kind), c.cms_header, "; ".join(notes)))
    return rows


# -------------------------------------------------------------------- sources
SOURCES_FILE = "pos_sources.csv"


def sources_problems(con: duckdb.DuckDBPyConnection, releases: Path | None) -> list[str]:
    """Loaded POS files that ``releases/pos_sources.csv`` does not record with the same sha256."""
    if not loaded(con) or releases is None:
        return []
    loaded_files = con.execute("SELECT snapshot_id, file, sha256 FROM pos_snapshot_stream ORDER BY 1, 2").fetchall()
    return filesources.manifest_problems(Path(releases) / SOURCES_FILE, loaded_files,
                                         "run `nppes-over-time pos-sources` and fill in the urls")


def read_sources(releases: Path) -> list[dict[str, str]]:
    return filesources.read_manifest(Path(releases) / SOURCES_FILE, "POS")


def download_sources(releases: Path, dest: Path, *, check_only: bool = False, pace: float = 1.0) -> list[str]:
    """Rebuild the quarter directories of ``releases/pos_sources.csv`` under ``dest``, sha256-checked."""
    return filesources.download_recorded(read_sources(releases), dest, check_only=check_only, pace=pace,
                                         manifest_name=SOURCES_FILE)


def write_sources(con: duckdb.DuckDBPyConnection, releases: Path) -> int:
    """Write or update ``releases/pos_sources.csv`` from the warehouse, keeping origin/url/member already recorded.
    Returns the number of rows whose url is still empty (data.cms.gov lists every quarter; docs/MAINTAINING.md)."""
    rows = con.execute(
        "SELECT ss.snapshot_id, s.directory, ss.file, ss.bytes, ss.sha256 FROM pos_snapshot_stream ss "
        "JOIN pos_snapshot s USING (snapshot_id) ORDER BY 1, 3").fetchall()
    return filesources.write_manifest(Path(releases) / SOURCES_FILE, rows)
