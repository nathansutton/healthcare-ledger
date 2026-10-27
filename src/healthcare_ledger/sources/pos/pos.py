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
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import duckdb

from ...core import columns, engine, filesources
from ...core.columns import Column, header_key
from ...core.engine import Staged, Stats, VersionedWarehouse
from ...core.privacy import has_token, redact_expr
from ...core.published import Table, interval_columns
from ...core.errors import UserError

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
    y, quarter = int(m.group("year")), int(m.group("q"))
    month = 3 * quarter
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


def check_header_collisions(headers: list[str]) -> None:
    """Two different headers with the same key would map to one column; CMS's files must not have them."""
    by_key: dict[str, str] = {}
    for h in headers:
        k = header_key(h)
        if k in by_key and by_key[k] != h:
            raise ValueError(f"headers {by_key[k]!r} and {h!r} collide on key {k!r}")
        by_key[k] = h


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
class PosWarehouse(VersionedWarehouse):
    source_label = "POS"
    database = "pos"
    key = KEY
    key_noun = "CCNs"
    streams = POS_STREAMS
    snapshot_table = "pos_snapshot"
    stream_table = "pos_snapshot_stream"
    single_row_streams = frozenset(s.name for s in POS_STREAMS)
    gone_kinds = ("absent", "moved")
    ignored_headers = frozenset(IGNORED_HEADERS)
    unmapped_hint = "; add them to pos_columns.csv"
    reader_options = {"repair": True}

    def init_bookkeeping(self, con: duckdb.DuckDBPyConnection) -> None:
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

    def map_headers(self, stream: PosStream, headers: list[str]) -> tuple[dict[str, str | None], list[str]]:
        check_header_collisions(headers)
        return super().map_headers(stream, headers)

    def column_expr(self, stream: PosStream, col: Column, src: str) -> str:
        expr = columns.null_placeholders(src, ["", *columns.PLACEHOLDERS, *POS_PLACEHOLDERS])
        if col.kind == "date":
            return date_norm(expr)
        if col.kind == "number":
            return number_norm(expr)
        return redact_expr(expr, col.name) if col.name in free_text_columns(stream) else expr

    def keep_empty(self, stream: PosStream) -> bool:
        return False

    def absent_kind_sql(self, stream: PosStream, staged: list[Staged]) -> str:
        # a CCN missing from this file but in any file of this quarter has moved between CMS's systems,
        # not left the registry
        keys_any = " UNION ALL ".join(f"SELECT {KEY} FROM {st.key_table}" for st in staged)
        return f"CASE WHEN o.{KEY} IN ({keys_any}) THEN 'moved' ELSE 'absent' END"

    def counted_absent(self, st: Staged, delta: str, out: Stats) -> int:
        # Only absent CCNs of a provider type that still has rows in this file: when CMS moves a whole type
        # to the other system, the CCNs of that type it did not carry over are gone for a known reason.
        s, cat = st.stream, st.stream.category_column
        counted = self.con.execute(f"""SELECT count(*) FROM {delta} d
                                       JOIN {s.name}_version v ON v.{KEY} = d.{KEY} AND v.is_current AND v.row_seq = 1
                                       WHERE d.kind = 'absent' AND v.{cat} IN (SELECT DISTINCT {cat} FROM {st.rows_table} WHERE {cat} IS NOT NULL)""").fetchone()[0]
        if out.n_absent > counted:
            log.warning("%s %s: %d of %d absent CCNs belong to provider types with no rows left in this file (moved systems); "
                        "not counted by the absent guard", st.snapshot_id, s.name, out.n_absent - counted, out.n_absent)
        return counted

    def typed_select(self, stream: PosStream) -> tuple[list[str], dict[str, list[str]]]:
        typed, bad = [], {"n_bad_dates": [], "n_bad_numbers": []}
        for col in stream.columns:
            if col.kind in _TYPED:
                cast, failed = _TYPED[col.kind]
                typed.append(f"{cast.format(c=col.name)} AS {col.name}")
                bad[f"n_bad_{col.kind}s"].append(f"count(*) FILTER (WHERE {col.name} IS NOT NULL AND {failed.format(c=col.name)})")
            else:
                typed.append(col.name)
        return typed, bad


def loaded(con: duckdb.DuckDBPyConnection) -> bool:
    """True when the warehouse holds at least one POS quarter."""
    return engine.loaded(con, PosWarehouse)


def finalize(con: duckdb.DuckDBPyConnection) -> None:
    engine.finalize(con, PosWarehouse)


def reset(con: duckdb.DuckDBPyConnection) -> list[str]:
    """Drop every POS table (versions, states and bookkeeping). Nothing else is touched."""
    return engine.reset(con, PosWarehouse)


def stored_fingerprints(con: duckdb.DuckDBPyConnection) -> dict[tuple[int, str], str | None]:
    return engine.stored_fingerprints(con, PosWarehouse)


def recompute_fingerprints(con: duckdb.DuckDBPyConnection) -> dict[tuple[int, str], str]:
    return engine.recompute_fingerprints(con, PosWarehouse)


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


REDACT_DESC = "Formatted SSNs and EINs and nine-digit numbers are replaced by [REDACTED] (METHODOLOGY 2a)"
FACILITY_TYPES_FILE = Path(__file__).with_name("facility_types.csv")


def facility_types() -> list[dict[str, str]]:
    """The provider types of both files under one name (``facility_types.csv``): a QIES ``prvdr_ctgry_cd`` and the
    iQIES ``prvdr_type_id`` CMS moved it to. The iQIES file codes the four nursing home categories as one type."""
    with open(FACILITY_TYPES_FILE, newline="") as f:
        return list(csv.DictReader(f))


def _facility_type_sql() -> str:
    def case(col: str) -> str:
        pairs = sorted({(r[col], r["facility_type"]) for r in facility_types() if r[col]})
        return f"CASE {col} " + " ".join(f"WHEN '{code}' THEN '{name}'" for code, name in pairs) + " END"
    return f"CASE pos_file WHEN 'qies' THEN {case('prvdr_ctgry_cd')} ELSE {case('prvdr_type_id')} END"


def _facility() -> Table:
    """Both POS files as one table of facilities. A CCN's history runs through the QIES file until CMS moves its
    provider type and through the iQIES file after, so the version that ends at the move closes as 'changed'. When a
    CCN is in both files at once (the 58 organ procurement organizations of 2025-Q4) the iQIES row stands and the
    QIES version is cut where the iQIES one starts."""
    spans, descs = column_spans(), column_descriptions()
    names = QIES.data_columns + [c for c in IQIES.data_columns if c not in set(QIES.data_columns)]
    by_name = {c.name: c for s in (IQIES, QIES) for c in s.columns}        # QIES's header where both have the column
    redacted = set(free_text_columns(QIES)) | set(free_text_columns(IQIES))
    cols = []
    for n in names:
        c = by_name[n]
        notes = [descs.get(("qies", n)) or descs.get(("iqies", n)) or ""]
        notes.append("; ".join(f"in the {label} file {spans[(k, n)][0]} to {spans[(k, n)][1]}"
                               for k, label in (("qies", "QIES"), ("iqies", "iQIES")) if (k, n) in spans))
        if n in redacted:
            notes.append(REDACT_DESC)
        if c.kind == "date":
            notes.append("Date as CMS wrote it (YYYYMMDD, YYYY-MM-DD or MM/DD/YYYY), typed; NULL when it does not parse")
        elif c.kind == "number":
            notes.append("Count as CMS wrote it, typed (01, 1 and 1.00 are the same value); NULL when it is not a number")
        cols.append((n, c.cms_header, "; ".join(x for x in notes if x)))
    until = "coalesce(v.end_date, DATE '9999-12-31')"
    sql = f"""
        WITH moved AS (SELECT ccn, min(effective_date) AS iqies_from FROM pos_facility_iqies_version GROUP BY ccn),
        both_files AS (
            SELECT 'qies' AS pos_file, v.ccn, v.effective_date,
                   CASE WHEN m.iqies_from < {until} THEN m.iqies_from ELSE v.end_date END AS end_date,
                   CASE WHEN m.iqies_from < {until} OR v.close_reason = 'moved' THEN 'changed' ELSE v.close_reason END
                       AS close_reason,
                   {', '.join(QIES.data_columns)}
            FROM pos_facility_version v LEFT JOIN moved m USING (ccn)
            WHERE m.iqies_from IS NULL OR v.effective_date < m.iqies_from
            UNION ALL BY NAME
            SELECT 'iqies' AS pos_file, ccn, effective_date, end_date, close_reason, {', '.join(IQIES.data_columns)}
            FROM pos_facility_iqies_version)
        SELECT ccn, effective_date, end_date, close_reason, {_facility_type_sql()} AS facility_type,
               -- the first iQIES file (2023-Q4) leaves the termination code blank for active facilities
               CASE pos_file WHEN 'iqies' THEN coalesce(pgm_trmntn_cd, '00') = '00'
                    ELSE coalesce(pgm_trmntn_cd = '00', false) END AS active,
               pos_file, {', '.join(names)}
        FROM both_files"""
    lead = interval_columns(KEY, QIES.key_header, "CMS Certification Number: six or ten characters", "Last day of the quarter",
                            "the CCN left both POS files")
    lead += (
        ("facility_type", "PRVDR_CTGRY_CD / prvdr_type_id",
         "The provider type under one coding for both files: " + ", ".join(sorted({r["facility_type"] for r in facility_types()}))
         + " (facility_types.csv)"),
        ("active", "PGM_TRMNTN_CD", "True when the termination code is 00 (active provider); in the iQIES file a blank code "
                                   "counts as active, because the first iQIES file leaves it blank for active facilities"),
        ("pos_file", "", "The POS file this version is from: 'qies' (Hospital & Non-Hospital Facilities, renamed QIES in 2025) "
                         "or 'iqies' (from 2023-Q4, the provider types CMS has moved; METHODOLOGY 7). The two files code "
                         "the provider type differently (prvdr_ctgry_cd against prvdr_type_id)"),
    )
    return Table("facility", (KEY,),
                 "one row per CCN per version: a Medicare- or Medicaid-certified hospital, nursing home, home health "
                 "agency, hospice, dialysis facility, clinic or other institutional provider",
                 sql, lead + tuple(cols))


def tables() -> tuple[Table, ...]:
    return (_facility(),)


# -------------------------------------------------------------------- sources
SOURCES_FILE = "pos_sources.csv"


def sources_problems(con: duckdb.DuckDBPyConnection, releases: Path | None) -> list[str]:
    """Loaded POS files that ``releases/pos_sources.csv`` does not record with the same sha256."""
    if not loaded(con) or releases is None:
        return []
    loaded_files = con.execute("SELECT snapshot_id, file, sha256 FROM pos_snapshot_stream ORDER BY 1, 2").fetchall()
    return filesources.manifest_problems(Path(releases) / SOURCES_FILE, loaded_files,
                                         "run `healthcare-ledger sources --source-name pos` and fill in the urls")


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


def discover_checked(source_dir: Path) -> list[PosSnapshot]:
    """``discover_pos``, with a UserError naming the fix when ``source_dir`` holds no quarter."""
    src = Path(source_dir)
    if not src.is_dir():
        raise UserError(f"POS source directory {src} does not exist; it should hold one subdirectory per quarter "
                        f"(2011-Q4, 2019-Q1, ...) with that quarter's POS csv file(s) (docs/MAINTAINING.md)", exit_code=2)
    snaps = discover_pos(src)
    if not snaps:
        raise UserError(f"no quarter directories (YYYY-QN) with a POS csv under {src}", exit_code=2)
    return snaps
