"""Medicare Fee-For-Service Public Provider Enrollment files (PPEF) as quarterly SCD Type 2 tables.

CMS publishes a quarterly extract of PECOS, the Medicare enrollment system: every provider that is
approved to bill Medicare fee-for-service, keyed by *enrollment id* (one provider can hold several
enrollments). Each quarter is a set of CSVs:

    PPEF_Enrollment_Extract_<date>.csv            one row per enrollment: NPI, PAC id, type, state, name
    PPEF_Practice_Location_Extract_<date>.csv     city, state and ZIP of each practice location
    PPEF_Reassignment_Extract_<date>.csv          which group enrollment each individual reassigns benefits to
    PPEF_Secondary_Specialty_Extract_<date>.csv   further specialties (from the August 2016 extract)
    PPEF_Additional_NPIs_<date>.csv               further NPIs of one enrollment (from the October 2025 extract)

CMS keeps only recent quarters on data.cms.gov; the rest come from CMS's historical zips and
NBER's copies (docs/MAINTAINING.md). The source directory holds one subdirectory per quarter,
named as CMS labels it (``2024-Q2``), with the quarter's CSVs inside. The label is not the
as-of date: the *as-of date* of a quarter is the extract date in the enrollment file's name,
or, for the 2023 to 2024 files that CMS named ``PPEF_Enrollment_Extract_2023Q1.csv``, the date
in the quarter's practice-location file name.

The versioning rules are the NPPES ones (docs/METHODOLOGY.md sections 2 to 5) with the
enrollment id in place of the NPI: trimmed text, blanks and CMS placeholders to NULL, md5 row
hashes over every data column, a version hash per enrollment id, ``new`` / ``changed`` /
``unchanged`` / ``absent`` against the previous quarter, one transaction per quarter, and a
content fingerprint per stream and quarter. The tables are ``medicare_*_version`` and
``medicare_*_state`` next to the NPPES tables in the same warehouse, with their own
bookkeeping tables ``ppef_snapshot`` and ``ppef_snapshot_stream``.

One CMS column is not loaded: ``ENRLMT_SPECIALTY_DESC``, which appears in the April 2016 file
only and repeats ``PROVIDER_TYPE_DESC`` without its prefix. ``MULTIPLE_NPI_FLAG`` (from October
2025) and ``GNDR_SW`` (sex; in the files CMS served up to April 2019, dropped from the later files
and from the historical zips) are ``hash_optional``: a quarter in which the column is NULL for
everyone hashes as if the column did not exist, so only the quarters where it appears or
disappears version every enrollment. A PAC id shorter than ten
digits is left-padded with zeros: CMS's 2025 historical zips of the 2016 to 2022 quarters wrote the
id from a number and lost the leading zeros of one id in ten. A first, middle or last name that
contains a digit is NULL before hashing: a few dozen rows per file carry a nine-digit number, an
NPI or a birth date there, and CMS's own re-publication of the January 2026 extract
(``PPEF_Enrollment_Extract_2026.01.02_Clean.csv``) removed exactly those values. The loader
reads the original January 2026 file, which keeps ``MULTIPLE_NPI_FLAG``, and removes them itself.

The same historical zips are degraded in ways the loader cannot repair, and the versions built
from them carry the defects: organization names are cut to their first character, last names to
17 characters, first names to 11, middle names to 7 (to the initial from the December 2020 file),
provider type descriptions to 51 or 56 characters, and the sex column (``GNDR_SW``) is gone.
The files CMS served at the time, which NBER keeps for August 2016 to April 2019 at
``https://data.nber.org/ppef/``, have the full fields; use them where they exist
(docs/MAINTAINING.md).
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import duckdb

from . import schema
from .errors import UserError
from .loader import SEP, StreamStats, fingerprint_sql
from .schema import Column, header_key
from .scrub import free_text_columns, has_token, redact_expr

log = logging.getLogger(__name__)

KEY = "enrollment_id"
QUARTER_DIR_RE = re.compile(r"^(?P<year>\d{4})-Q(?P<q>[1-4])$")
EXTRACT_DATE_RE = re.compile(r"(\d{4})\.(\d{2})\.(\d{2})")
IGNORED_HEADERS = {header_key("ENRLMT_SPECIALTY_DESC")}


@dataclass(frozen=True)
class PpefStream:
    name: str
    file_pattern: str              # regex on the CSV basename
    key_header: str                # CMS header of the enrollment id in this file
    columns: tuple[Column, ...]
    description: str
    always_present: bool = False   # the enrollment file; its absence share is guarded

    @property
    def data_columns(self) -> list[str]:
        return [c.name for c in self.columns]


ENROLLMENT = PpefStream(
    name="medicare_enrollment",
    file_pattern=r"^PPEF_Enrollment_Extract_.*\.csv$",
    key_header="ENRLMT_ID",
    columns=(
        Column("npi", "NPI"),
        Column("multiple_npi_flag", "MULTIPLE_NPI_FLAG", hash_optional=True),
        Column("pac_id", "PECOS_ASCT_CNTL_ID"),
        Column("provider_type_code", "PROVIDER_TYPE_CD"),
        Column("provider_type_desc", "PROVIDER_TYPE_DESC"),
        Column("state", "STATE_CD"),
        Column("first_name", "FIRST_NAME"),
        Column("middle_name", "MDL_NAME"),
        Column("last_name", "LAST_NAME"),
        Column("org_name", "ORG_NAME"),
        Column("sex", "GNDR_SW", hash_optional=True),
    ),
    description="Medicare FFS enrollments (PPEF base file): one row per enrollment id with its NPI, PECOS associate control id, provider type, state and name. Sex (GNDR_SW) only in the files CMS served up to April 2019.",
    always_present=True,
)
PRACTICE_LOCATION = PpefStream(
    name="medicare_practice_location",
    file_pattern=r"^PPEF_Practice_Location_Extract_.*\.csv$",
    key_header="ENRLMT_ID",
    columns=(Column("city", "CITY_NAME"), Column("state", "STATE_CD"), Column("zip", "ZIP_CD")),
    description="Practice locations of an enrollment (PPEF address sub-file), zero or more per enrollment id.",
)
REASSIGNMENT = PpefStream(
    name="medicare_reassignment",
    file_pattern=r"^PPEF_Reassignment_Extract_.*\.csv$",
    key_header="REASGN_BNFT_ENRLMT_ID",
    columns=(Column("receiving_enrollment_id", "RCV_BNFT_ENRLMT_ID"),),
    description="Reassignment of benefits (PPEF reassignment sub-file): the group enrollment ids an individual enrollment reassigns to, zero or more per enrollment id.",
)
SECONDARY_SPECIALTY = PpefStream(
    name="medicare_secondary_specialty",
    file_pattern=r"^PPEF_Secondary_Specialty_Extract_.*\.csv$",
    key_header="ENRLMT_ID",
    columns=(Column("provider_type_code", "PROVIDER_TYPE_CD"), Column("provider_type_desc", "PROVIDER_TYPE_DESC")),
    description="Secondary specialties of an enrollment (PPEF sub-file), zero or more per enrollment id. Present from the August 2016 extract.",
)
ADDITIONAL_NPI = PpefStream(
    name="medicare_additional_npi",
    file_pattern=r"^PPEF_Additional_NPIs_.*\.csv$",
    key_header="ENRLMT_ID",
    columns=(Column("npi", "NPI"),),
    description="Further NPIs of one enrollment (PPEF sub-file), zero or more per enrollment id. Present from the October 2025 extract.",
)
PPEF_STREAMS: tuple[PpefStream, ...] = (ENROLLMENT, PRACTICE_LOCATION, REASSIGNMENT, SECONDARY_SPECIALTY, ADDITIONAL_NPI)
PPEF_STREAM_BY_NAME = {s.name: s for s in PPEF_STREAMS}

# free-text columns get the tax-id token rule of METHODOLOGY 2a, like the NPPES ones; a person's name that
# contains a digit is NULL (SSNs, NPIs and birth dates typed into the middle-name field; CMS's own
# re-publication of the January 2026 extract removed exactly those values)
PERSON_NAME_COLUMNS = {"first_name", "middle_name", "last_name"}

META_COLUMNS = [
    (KEY, "VARCHAR"), ("effective_date", "DATE"), ("end_date", "DATE"), ("is_current", "BOOLEAN"),
    ("first_snapshot_id", "INTEGER"), ("last_snapshot_id", "INTEGER"), ("close_reason", "VARCHAR"),
    ("version_hash", "VARCHAR"), ("row_seq", "INTEGER"),
]


# ------------------------------------------------------------------ discovery
@dataclass
class PpefSnapshot:
    directory: Path
    as_of_date: date
    members: dict[str, Path] = field(default_factory=dict)   # stream -> csv path

    @property
    def snapshot_id(self) -> int:
        return int(self.as_of_date.strftime("%Y%m%d"))

    @property
    def label(self) -> str:
        return self.directory.name

    def has(self, stream: str) -> bool:
        return stream in self.members and self.members[stream].stat().st_size > 0


def extract_date(name: str) -> date | None:
    m = EXTRACT_DATE_RE.search(name)
    return date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def inspect_quarter(directory: Path) -> PpefSnapshot:
    members: dict[str, Path] = {}
    for f in sorted(directory.iterdir()):
        if not f.is_file():
            continue
        for s in PPEF_STREAMS:
            if re.match(s.file_pattern, f.name, re.IGNORECASE):
                if s.name in members:
                    # CMS republished the January 2026 enrollment extract as *_Clean.csv; the clean one wins
                    prev = members[s.name]
                    if "clean" in f.name.lower() and "clean" not in prev.name.lower():
                        members[s.name] = f
                    elif "clean" in prev.name.lower() and "clean" not in f.name.lower():
                        pass
                    else:
                        raise ValueError(f"{directory.name}: two {s.name} files, {prev.name} and {f.name}")
                else:
                    members[s.name] = f
    if ENROLLMENT.name not in members:
        raise ValueError(f"{directory.name}: no PPEF_Enrollment_Extract_*.csv")
    as_of = extract_date(members[ENROLLMENT.name].name)
    if as_of is None and PRACTICE_LOCATION.name in members:
        as_of = extract_date(members[PRACTICE_LOCATION.name].name)
    if as_of is None:
        raise ValueError(f"{directory.name}: no extract date in the enrollment or practice-location file name")
    return PpefSnapshot(directory=directory, as_of_date=as_of, members=members)


def discover_ppef(source_dir: Path) -> list[PpefSnapshot]:
    """Quarter directories of ``source_dir`` in as-of order."""
    snaps = [inspect_quarter(d) for d in sorted(source_dir.iterdir()) if d.is_dir() and QUARTER_DIR_RE.match(d.name)]
    snaps.sort(key=lambda s: s.as_of_date)
    for a, b in zip(snaps, snaps[1:]):
        if a.as_of_date == b.as_of_date:
            raise ValueError(f"{a.label} and {b.label} are both as of {a.as_of_date}")
    return snaps


def map_headers(stream: PpefStream, headers: list[str]) -> tuple[dict[str, str | None], list[str]]:
    """Canonical column -> actual header (None when absent); plus headers that match nothing."""
    by_key = {header_key(h): h for h in headers}
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


def repair_utf8(src: Path, dest: Path) -> int:
    """Copy ``src`` to ``dest`` as UTF-8, decoding each line that is not valid UTF-8 as latin-1. Returns that count."""
    bad = 0
    with open(src, "rb") as fin, open(dest, "wb") as fout:
        for line in fin:
            try:
                line.decode("utf-8")
            except UnicodeDecodeError:
                bad += 1
                line = line.decode("latin-1").encode("utf-8")
            fout.write(line)
    return bad


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(16 << 20):
            h.update(chunk)
    return h.hexdigest()


def _sql_type(kind: str) -> str:
    return {"text": "VARCHAR", "date": "DATE", "int": "INTEGER"}[kind]


def _q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


# -------------------------------------------------------------------- warehouse
def loaded(con: duckdb.DuckDBPyConnection) -> bool:
    """True when the warehouse holds at least one PPEF quarter."""
    tables = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    return "ppef_snapshot" in tables and con.execute("SELECT count(*) FROM ppef_snapshot").fetchone()[0] > 0


def init_schema(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("""
        CREATE TABLE IF NOT EXISTS ppef_snapshot (
            snapshot_id INTEGER NOT NULL, as_of_date DATE NOT NULL, directory VARCHAR NOT NULL,
            streams_present VARCHAR[], loaded_at TIMESTAMP NOT NULL, seconds DOUBLE)""")
    con.execute("""
        CREATE TABLE IF NOT EXISTS ppef_snapshot_stream (
            snapshot_id INTEGER NOT NULL, stream VARCHAR NOT NULL, file VARCHAR NOT NULL,
            bytes BIGINT, sha256 VARCHAR, n_rows BIGINT, n_keys BIGINT,
            n_new BIGINT, n_changed BIGINT, n_unchanged BIGINT, n_absent BIGINT,
            csv_mode VARCHAR, content_fingerprint VARCHAR)""")
    for s in PPEF_STREAMS:
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
    stream: PpefStream
    rows_table: str
    seq_table: str
    key_table: str
    n_rows: int
    n_keys: int
    csv_mode: str
    path: Path


class PpefWarehouse:
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
        self._scratch: list[Path] = []          # repaired copies of CSVs, removed after each quarter
        init_schema(self.con)

    def loaded_snapshot_ids(self) -> list[int]:
        return [r[0] for r in self.con.execute("SELECT snapshot_id FROM ppef_snapshot ORDER BY 1").fetchall()]

    def last_present_snapshot(self, stream: str) -> int | None:
        row = self.con.execute("SELECT max(snapshot_id) FROM ppef_snapshot_stream WHERE stream = ?", [stream]).fetchone()
        return row[0] if row else None

    # ---------------------------------------------------------------- load
    def load_snapshot(self, snap: PpefSnapshot) -> list[StreamStats]:
        sid = snap.snapshot_id
        done = self.loaded_snapshot_ids()
        if sid in done:
            raise ValueError(f"PPEF snapshot {sid} ({snap.label}) already loaded")
        if done and done[-1] > sid:
            raise ValueError(f"PPEF snapshot {sid} ({snap.label}) is older than last loaded {done[-1]}; load in order")
        t0 = time.time()
        c = self.con
        staged: list[_Staged] = []
        try:
            for s in PPEF_STREAMS:
                if not snap.has(s.name):
                    log.info("%s %s: stream %s not present, skipping", sid, snap.label, s.name)
                    continue
                st = self._stage(s, snap.members[s.name], sid)
                if st is None:
                    log.warning("%s: %s has no rows, treating as not present", sid, snap.members[s.name].name)
                    continue
                staged.append(st)
            c.execute("BEGIN")
            try:
                stats = [self._apply(st, snap) for st in staged]
                for st, out in zip(staged, stats):
                    c.execute("INSERT INTO ppef_snapshot_stream VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                              [sid, out.stream, st.path.name, st.path.stat().st_size, _sha256(st.path), out.n_rows, out.n_npis,
                               out.n_new, out.n_changed, out.n_unchanged, out.n_absent, out.csv_mode, out.content_fingerprint])
                    log.info("%s %-30s rows=%-9d keys=%-9d new=%-8d changed=%-8d unchanged=%-9d absent=%-6d",
                             sid, out.stream, out.n_rows, out.n_npis, out.n_new, out.n_changed, out.n_unchanged, out.n_absent)
                c.execute("INSERT INTO ppef_snapshot VALUES (?,?,?,?,now(),?)",
                          [sid, snap.as_of_date, snap.label, [o.stream for o in stats], time.time() - t0])
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
        # The April 2025 file is UTF-8 with nine latin-1 bytes in it (a no-break space after a name, an
        # accented initial). DuckDB 1.5 fails on it in every mode, so repair it line by line into UTF-8.
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

    def _state_zip_swapped(self, raw: str, mapping: dict[str, str | None]) -> bool:
        """True when most values under the state header are ZIP codes and most under the ZIP header are
        state codes. NBER's copies of the August 2016 and January 2017 address files label the two
        columns the wrong way round; the files from April 2017 on have them right."""
        st, zp = mapping.get("state"), mapping.get("zip")
        if st is None or zp is None:
            return False
        n, st_zip, zp_state = self.con.execute(
            f"SELECT count(*), count(*) FILTER (WHERE regexp_matches(trim({_q(st)}), '^[0-9]{{5}}([0-9]{{4}})?$')), "
            f"count(*) FILTER (WHERE regexp_matches(trim({_q(zp)}), '^[A-Z]{{2}}$')) FROM {raw}").fetchone()
        return n > 0 and st_zip > n / 2 and zp_state > n / 2

    def _stage(self, s: PpefStream, path: Path, sid: int) -> _Staged | None:
        c = self.con
        raw = f"raw_{s.name}"
        csv_mode = self._read_csv(raw, path)
        headers = [r[0] for r in c.execute(f"DESCRIBE {raw}").fetchall()]
        mapping, unmatched = map_headers(s, headers)
        if unmatched:
            raise ValueError(f"{path.name}: unmapped columns {unmatched}")
        sel = [f"nullif(trim({_q(mapping[KEY])}), '') AS {KEY}"]
        free_text = set(free_text_columns(s))
        if s is PRACTICE_LOCATION and self._state_zip_swapped(raw, mapping):
            log.warning("%s: the STATE_CD and ZIP_CD headers are swapped (state codes under ZIP_CD); reading them by content",
                        path.name)
            mapping = {**mapping, "state": mapping["zip"], "zip": mapping["state"]}
        for col in s.columns:
            src = mapping[col.name]
            if src is None:
                sel.append(f"NULL::VARCHAR AS {col.name}")
                continue
            expr = f"trim({_q(src)})"
            for tok in ["''"] + [f"'{t}'" for t in schema.PLACEHOLDERS] + ([f"'{schema.NULL_WORD}'"] if schema.is_code_column(col.name) else []):
                expr = f"nullif({expr}, {tok})"
            if col.name in PERSON_NAME_COLUMNS:
                expr = f"CASE WHEN regexp_matches({expr}, '[0-9]') THEN NULL ELSE {expr} END"
            elif col.name in free_text:
                expr = redact_expr(expr, col.name)
            if col.name == "pac_id":
                # CMS's historical zips wrote the 10-digit PAC id from a number, dropping leading zeros (10% of
                # ids in 2016 to 2022); the current files keep them. Restore the width so ids match across quarters.
                expr = f"CASE WHEN regexp_matches({expr}, '^[0-9]{{1,9}}$') THEN lpad({expr}, 10, '0') ELSE {expr} END"
            sel.append(f"{expr} AS {col.name}")
        stg = f"stg_{s.name}"
        c.execute(f"CREATE OR REPLACE TEMP TABLE {stg} AS SELECT {', '.join(sel)} FROM {raw} WHERE {KEY} IS NOT NULL")
        c.execute(f"DROP TABLE {raw}")
        n_rows, n_keys = c.execute(f"SELECT count(*), count(DISTINCT {KEY}) FROM {stg}").fetchone()
        if n_keys == 0 and not s.always_present:
            c.execute(f"DROP TABLE {stg}")
            return None
        if s.always_present and n_rows != n_keys:
            log.warning("%s: enrollment file has %d duplicate enrollment-id rows; they become row_seq > 1", sid, n_rows - n_keys)
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
                       csv_mode=csv_mode, path=path)

    def _apply(self, st: _Staged, snap: PpefSnapshot) -> StreamStats:
        c = self.con
        s = st.stream
        sid = snap.snapshot_id
        as_of = snap.as_of_date.isoformat()
        out = StreamStats(stream=s.name, n_rows=st.n_rows, n_npis=st.n_keys, csv_mode=st.csv_mode)
        delta = f"delta_{s.name}"
        c.execute(f"""CREATE OR REPLACE TEMP TABLE {delta} AS
            SELECT coalesce(n.{KEY}, o.{KEY}) AS {KEY},
                   CASE WHEN o.{KEY} IS NULL THEN 'new' WHEN n.{KEY} IS NULL THEN 'absent'
                        WHEN n.version_hash <> o.version_hash THEN 'changed' ELSE 'unchanged' END AS kind,
                   n.version_hash AS new_hash, o.effective_date AS old_effective_date
            FROM {st.key_table} n FULL OUTER JOIN {s.name}_state o ON n.{KEY} = o.{KEY}""")
        counts = dict(c.execute(f"SELECT kind, count(*) FROM {delta} GROUP BY kind").fetchall())
        out.n_new, out.n_changed = counts.get("new", 0), counts.get("changed", 0)
        out.n_unchanged, out.n_absent = counts.get("unchanged", 0), counts.get("absent", 0)
        prev = out.n_changed + out.n_unchanged + out.n_absent
        if s.always_present and prev >= 1000 and out.n_absent > self.max_absent_share * prev:
            raise RuntimeError(f"{sid} {s.name}: {out.n_absent:,} of {prev:,} enrollments would be marked absent "
                               f"({out.n_absent / prev:.1%} > {self.max_absent_share:.0%}); refusing. Check the file, "
                               f"or raise --max-absent-share if this is genuine.")
        prev_sid = self.last_present_snapshot(s.name)
        c.execute(f"""UPDATE {s.name}_version v
                      SET end_date = DATE '{as_of}', is_current = false,
                          last_snapshot_id = {prev_sid if prev_sid is not None else 'NULL'}, close_reason = d.kind
                      FROM {delta} d WHERE v.{KEY} = d.{KEY} AND v.is_current AND d.kind IN ('changed', 'absent')""")
        meta_names = ", ".join(n for n, _ in META_COLUMNS)
        data_names = ", ".join(col.name for col in s.columns)
        c.execute(f"""INSERT INTO {s.name}_version ({meta_names}, {data_names})
            SELECT h.{KEY}, DATE '{as_of}', NULL, true, {sid}, {sid}, NULL, d.new_hash, q.row_seq, {data_names}
            FROM {st.rows_table} h JOIN {st.seq_table} q ON q.rid = h.rowid JOIN {delta} d ON h.{KEY} = d.{KEY}
            WHERE d.kind IN ('new', 'changed')""")
        c.execute(f"""CREATE OR REPLACE TABLE {s.name}_state AS
            SELECT {KEY}, new_hash AS version_hash,
                   CASE WHEN kind = 'unchanged' THEN old_effective_date ELSE DATE '{as_of}' END AS effective_date
            FROM {delta} WHERE kind <> 'absent'""")
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
    for s in PPEF_STREAMS:
        row = con.execute("SELECT max(snapshot_id) FROM ppef_snapshot_stream WHERE stream = ?", [s.name]).fetchone()
        if row and row[0] is not None:
            con.execute(f"UPDATE {s.name}_version SET last_snapshot_id = {row[0]} WHERE is_current AND last_snapshot_id <> {row[0]}")


def reset(con: duckdb.DuckDBPyConnection) -> list[str]:
    """Drop every PPEF table (versions, states and bookkeeping). The NPPES tables are not touched."""
    tables = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    todo = [t for t in ["ppef_snapshot", "ppef_snapshot_stream"] + [f"{s.name}_{k}" for s in PPEF_STREAMS for k in ("version", "state")]
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
            con.execute("SELECT snapshot_id, stream, content_fingerprint FROM ppef_snapshot_stream ORDER BY 1, 2").fetchall()}


def recompute_fingerprints(con: duckdb.DuckDBPyConnection) -> dict[tuple[int, str], str]:
    if not loaded(con):
        return {}
    out: dict[tuple[int, str], str] = {}
    for sid, stream in con.execute("SELECT snapshot_id, stream FROM ppef_snapshot_stream ORDER BY 1, 2").fetchall():
        d = f"{sid // 10000:04d}-{sid // 100 % 100:02d}-{sid % 100:02d}"
        out[(sid, stream)] = interval_fingerprint(con, stream, d)
    return out


# -------------------------------------------------------------------- checks
def privacy_checks(s: PpefStream) -> list[tuple[str, str]]:
    """(description, predicate) pairs that must count 0 in ``s``'s version table (METHODOLOGY 2a and 6)."""
    checks = [(f"{s.name}.{n} holds a tax-id token", has_token(n, n)) for n in free_text_columns(s)]
    checks += [(f"{s.name}.{n} contains a digit", f"regexp_matches({n}, '[0-9]')")
               for n in s.data_columns if n in PERSON_NAME_COLUMNS]
    return checks


# Shape of the code-valued columns. A failure means a column was read under the wrong header.
SHAPE_CHECKS: dict[str, list[tuple[str, str]]] = {
    "medicare_enrollment": [("npi", "^[0-9]{10}$"), ("pac_id", "^[0-9]{10}$"), ("state", "^[A-Z]{2}$")],
    "medicare_practice_location": [("state", "^[A-Z]{2}$"), ("zip", "^[0-9]{5}([0-9]{4})?$")],
    "medicare_additional_npi": [("npi", "^[0-9]{10}$")],
}


def check_problems(con: duckdb.DuckDBPyConnection) -> list[str]:
    """Privacy and shape checks over every loaded medicare_* version table; returns the failures."""
    if not loaded(con):
        return []
    problems: list[str] = []
    for s in PPEF_STREAMS:
        checks = [(f"privacy: {d}", p) for d, p in privacy_checks(s)]
        checks += [(f"shape: {s.name}.{c} does not match {rx}", f"NOT regexp_matches({c}, '{rx}')")
                   for c, rx in SHAPE_CHECKS.get(s.name, [])]
        if not checks:
            continue
        row = con.execute(f"SELECT {', '.join(f'count(*) FILTER (WHERE {p})' for _, p in checks)} FROM {s.name}_version").fetchone()
        problems += [f"{d}: {n} row(s)" for (d, _), n in zip(checks, row) if n]
    return problems


# --------------------------------------------------------------------- export
SNAPSHOTS_FILE = "medicare_snapshots.csv"
META_EXPORT = [KEY, "effective_date", "end_date", "is_current", "first_snapshot_id", "last_snapshot_id", "close_reason", "version_hash", "row_seq"]


def export_tables() -> dict[str, tuple[str, bool]]:
    """name -> (select sql, partition by year), in the shape export.tables() uses."""
    return {s.name: (f"SELECT {', '.join(META_EXPORT)}, {', '.join(s.data_columns)} FROM {s.name}_version", True)
            for s in PPEF_STREAMS}


def snapshots_sql() -> str:
    """One row per PPEF quarter with per-stream counts and fingerprints, for medicare_snapshots.csv."""
    joins, cols = [], []
    for s in PPEF_STREAMS:
        a = s.name.replace("medicare_", "")
        joins.append(f"LEFT JOIN ppef_snapshot_stream {a} ON {a}.snapshot_id = s.snapshot_id AND {a}.stream = '{s.name}'")
        cols.append(f"{a}.file AS {s.name}_file, {a}.n_rows AS {s.name}_rows, {a}.n_keys AS {s.name}_keys, "
                    f"{a}.n_new AS {s.name}_new, {a}.n_changed AS {s.name}_changed, {a}.n_absent AS {s.name}_absent, "
                    f"{a}.content_fingerprint AS {s.name}_fingerprint")
    return (f"SELECT s.snapshot_id, s.as_of_date, s.directory, array_to_string(s.streams_present, ',') AS streams_present, "
            + ", ".join(cols) + " FROM ppef_snapshot s " + " ".join(joins) + " ORDER BY s.snapshot_id")


META_DESC = {
    KEY: "PECOS enrollment id (the key of every PPEF file; one provider can hold several)",
    "effective_date": "Extract date of the first quarterly file in which this version was observed (inclusive)",
    "end_date": "Extract date of the first quarterly file in which this version was no longer observed (exclusive); NULL while current",
    "is_current": "True if this is the latest version and the enrollment id was present in the latest quarterly file",
    "first_snapshot_id": "Snapshot id (YYYYMMDD of the extract date) that first showed this version",
    "last_snapshot_id": "Snapshot id that last showed this version",
    "close_reason": "'changed' if superseded by a new version, 'absent' if the enrollment id disappeared from the file, NULL while current",
    "version_hash": "MD5 fingerprint of all data columns of this version (set-level for multi-row streams)",
    "row_seq": "Row number within a multi-row version, ordered by row hash (no meaning beyond uniqueness)",
}


def data_dictionary_rows() -> list[tuple[str, str, str, str, str]]:
    rows = []
    types = {"effective_date": "DATE", "end_date": "DATE", "is_current": "BOOLEAN", "first_snapshot_id": "INTEGER",
             "last_snapshot_id": "INTEGER", "row_seq": "INTEGER"}
    for s in PPEF_STREAMS:
        for m in META_EXPORT:
            rows.append((s.name, m, types.get(m, "VARCHAR"), s.key_header if m == KEY else "", META_DESC[m]))
        for c in s.columns:
            desc = ("NULL when it contains a digit (METHODOLOGY 6)" if c.name in PERSON_NAME_COLUMNS
                    else "Formatted SSNs and EINs and nine-digit numbers are replaced by [REDACTED] (METHODOLOGY 2a)"
                    if c.name in free_text_columns(s) else "")
            if c.hash_optional:
                desc = (desc + "; " if desc else "") + "Added by CMS later; NULL in earlier quarters and outside the version hash while NULL"
            rows.append((s.name, c.name, _sql_type(c.kind), c.cms_header, desc))
    return rows


# -------------------------------------------------------------------- sources
SOURCES_FILE = "ppef_sources.csv"
SOURCE_COLUMNS = ["snapshot_id", "directory", "file", "bytes", "sha256", "origin", "url", "member"]


def sources_problems(con: duckdb.DuckDBPyConnection, releases: Path | None) -> list[str]:
    """Loaded PPEF files that ``releases/ppef_sources.csv`` does not record with the same sha256."""
    import csv

    if not loaded(con) or releases is None:
        return []
    path = Path(releases) / SOURCES_FILE
    if not path.exists():
        return [f"{path} does not exist; run `nppes-over-time ppef-sources` and fill in the urls"]
    with open(path, newline="") as f:
        recorded = {(int(r["snapshot_id"]), r["file"]): r["sha256"] for r in csv.DictReader(f)}
    problems = []
    for sid, file, sha in con.execute("SELECT snapshot_id, file, sha256 FROM ppef_snapshot_stream ORDER BY 1, 2").fetchall():
        have = recorded.get((sid, file))
        if have is None:
            problems.append(f"{sid} {file}: no row in {path.name}")
        elif have != sha:
            problems.append(f"{sid} {file}: {path.name} records sha256 {have[:12]}..., the loaded file is {sha[:12]}...")
    return problems


def read_sources(releases: Path) -> list[dict[str, str]]:
    import csv

    path = Path(releases) / SOURCES_FILE
    if not path.exists():
        raise UserError(f"{path} does not exist; nothing records where the PPEF files come from", exit_code=2)
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def _fetch_url(url: str, target: Path, tries: int = 4) -> None:
    """Stream ``url`` into ``target`` (via ``target.part``), retrying with a growing pause."""
    import urllib.error
    import urllib.request

    from .download import UA

    tmp = target.with_name(target.name + ".part")
    for attempt in range(1, tries + 1):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=300) as r:
                if "html" in (r.headers.get("Content-Type") or "").lower():
                    raise ValueError("the server answered with a web page, not the file")
                with open(tmp, "wb") as f:
                    while chunk := r.read(16 << 20):
                        f.write(chunk)
            tmp.rename(target)
            return
        except (urllib.error.URLError, OSError, ValueError) as e:
            tmp.unlink(missing_ok=True)
            if attempt == tries:
                raise UserError(f"could not download {url}: {e}; try again later (data.cms.gov throttles bursts)") from None
            log.warning("attempt %d/%d for %s failed: %s; retrying", attempt, tries, url, e)
            time.sleep(60 * attempt)


def download_sources(releases: Path, dest: Path, *, check_only: bool = False, pace: float = 1.0) -> list[str]:
    """Rebuild the quarter directories of ``releases/ppef_sources.csv`` under ``dest``, sha256-checked.

    Each row is a file the release was built from: ``dest/<directory>/<file>``. A file already there
    with the recorded sha256 is kept. Otherwise it is fetched from ``url``, or, when ``member`` is set,
    extracted from the zip at ``url`` (cached once in ``dest/zips/``). ``check_only`` downloads nothing
    and reports the files that are missing or differ. Returns the problems; empty means every file
    is present and matches.
    """
    import zipfile

    rows = read_sources(releases)
    dest = Path(dest)
    problems: list[str] = []
    zips: dict[str, Path] = {}
    for r in rows:
        target = dest / r["directory"] / r["file"]
        if target.is_file() and target.stat().st_size == int(r["bytes"]) and _sha256(target) == r["sha256"]:
            continue
        if check_only:
            problems.append(f"{r['directory']}/{r['file']}: {'differs from the recorded sha256' if target.exists() else 'missing'}")
            continue
        if not r["url"]:
            problems.append(f"{r['directory']}/{r['file']}: no url recorded in {SOURCES_FILE}")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if r["member"]:
            zpath = zips.get(r["url"])
            if zpath is None:
                zpath = dest / "zips" / r["url"].rsplit("/", 1)[-1]
                if not zpath.exists():
                    zpath.parent.mkdir(parents=True, exist_ok=True)
                    log.info("downloading %s", r["url"])
                    _fetch_url(r["url"], zpath)
                    time.sleep(pace)
                zips[r["url"]] = zpath
            log.info("extracting %s from %s", r["member"], zpath.name)
            tmp = target.with_name(target.name + ".part")
            with zipfile.ZipFile(zpath) as z, z.open(r["member"]) as src, open(tmp, "wb") as out:
                while chunk := src.read(16 << 20):
                    out.write(chunk)
            tmp.rename(target)
        else:
            log.info("downloading %s", r["url"])
            _fetch_url(r["url"], target)
            time.sleep(pace)
        got = _sha256(target)
        if got != r["sha256"]:
            target.rename(target.with_name(target.name + ".mismatch"))
            problems.append(f"{r['directory']}/{r['file']}: {r['url']} now serves sha256 {got[:12]}..., the release was built "
                            f"from {r['sha256'][:12]}...; the upstream file changed (kept as .mismatch)")
    return problems


def write_sources(con: duckdb.DuckDBPyConnection, releases: Path) -> int:
    """Write or update ``releases/ppef_sources.csv`` from the warehouse, keeping origin/url/member already recorded.

    Returns the number of rows whose url is still empty (the maintainer fills those from
    data.cms.gov's dataset-resources listing or the historical zips; docs/MAINTAINING.md).
    """
    import csv

    path = Path(releases) / SOURCES_FILE
    old: dict[tuple[int, str], dict] = {}
    if path.exists():
        with open(path, newline="") as f:
            old = {(int(r["snapshot_id"]), r["file"]): r for r in csv.DictReader(f)}
    rows = []
    for sid, directory, file, nbytes, sha in con.execute(
            "SELECT ss.snapshot_id, s.directory, ss.file, ss.bytes, ss.sha256 FROM ppef_snapshot_stream ss "
            "JOIN ppef_snapshot s USING (snapshot_id) ORDER BY 1, 3").fetchall():
        prev = old.get((sid, file), {})
        rows.append({"snapshot_id": sid, "directory": directory, "file": file, "bytes": nbytes, "sha256": sha,
                     "origin": prev.get("origin", ""), "url": prev.get("url", ""), "member": prev.get("member", "")})
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=SOURCE_COLUMNS, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    return sum(1 for r in rows if not r["url"])


def parse_ymd(s: str) -> date:
    return datetime.strptime(s, "%Y%m%d").date()
