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

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import duckdb

from ..core import engine, filesources
from ..core.columns import Column, header_key
from ..core.engine import VersionedWarehouse
from ..core.privacy import free_text_columns, has_token, redact_expr
from ..core.sql import q, sql_type
from ..core.errors import UserError
from ..core.versioning import meta_columns
from .base import Source, quarter_status, recorded_download, recorded_files_gate

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

META_COLUMNS = meta_columns(KEY)


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


# -------------------------------------------------------------------- warehouse
class PpefWarehouse(VersionedWarehouse):
    source_label = "PPEF"
    database = "ppef"
    key = KEY
    key_noun = "enrollments"
    streams = PPEF_STREAMS
    snapshot_table = "ppef_snapshot"
    stream_table = "ppef_snapshot_stream"
    single_row_streams = frozenset({ENROLLMENT.name})
    ignored_headers = frozenset(IGNORED_HEADERS)
    reader_options = {"repair": True}

    def init_bookkeeping(self, con: duckdb.DuckDBPyConnection) -> None:
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

    def remap(self, stream: PpefStream, raw: str, mapping: dict[str, str | None], path: Path) -> dict[str, str | None]:
        """Swap the state and ZIP headers when most values under the state header are ZIP codes and most under
        the ZIP header are state codes. NBER's copies of the August 2016 and January 2017 address files label
        the two columns the wrong way round; the files from April 2017 on have them right."""
        st, zp = mapping.get("state"), mapping.get("zip")
        if stream is not PRACTICE_LOCATION or st is None or zp is None:
            return mapping
        n, st_zip, zp_state = self.con.execute(
            f"SELECT count(*), count(*) FILTER (WHERE regexp_matches(trim({q(st)}), '^[0-9]{{5}}([0-9]{{4}})?$')), "
            f"count(*) FILTER (WHERE regexp_matches(trim({q(zp)}), '^[A-Z]{{2}}$')) FROM {raw}").fetchone()
        if not (n > 0 and st_zip > n / 2 and zp_state > n / 2):
            return mapping
        log.warning("%s: the STATE_CD and ZIP_CD headers are swapped (state codes under ZIP_CD); reading them by content",
                    path.name)
        return {**mapping, "state": zp, "zip": st}

    def column_expr(self, stream: PpefStream, col: Column, src: str) -> str:
        expr = super().column_expr(stream, col, src)
        if col.name in PERSON_NAME_COLUMNS:
            expr = f"CASE WHEN regexp_matches({expr}, '[0-9]') THEN NULL ELSE {expr} END"
        elif col.name in free_text_columns(stream):
            expr = redact_expr(expr, col.name)
        if col.name == "pac_id":
            # CMS's historical zips wrote the 10-digit PAC id from a number, dropping leading zeros (10% of
            # ids in 2016 to 2022); the current files keep them. Restore the width so ids match across quarters.
            expr = f"CASE WHEN regexp_matches({expr}, '^[0-9]{{1,9}}$') THEN lpad({expr}, 10, '0') ELSE {expr} END"
        return expr


def loaded(con: duckdb.DuckDBPyConnection) -> bool:
    """True when the warehouse holds at least one PPEF quarter."""
    return engine.loaded(con, PpefWarehouse)


def finalize(con: duckdb.DuckDBPyConnection) -> None:
    engine.finalize(con, PpefWarehouse)


def reset(con: duckdb.DuckDBPyConnection) -> list[str]:
    """Drop every PPEF table (versions, states and bookkeeping). The NPPES tables are not touched."""
    return engine.reset(con, PpefWarehouse)


def stored_fingerprints(con: duckdb.DuckDBPyConnection) -> dict[tuple[int, str], str | None]:
    return engine.stored_fingerprints(con, PpefWarehouse)


def recompute_fingerprints(con: duckdb.DuckDBPyConnection) -> dict[tuple[int, str], str]:
    return engine.recompute_fingerprints(con, PpefWarehouse)


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
META_EXPORT = [n for n, _ in META_COLUMNS]


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
            rows.append((s.name, c.name, sql_type(c.kind), c.cms_header, desc))
    return rows


# -------------------------------------------------------------------- sources
SOURCES_FILE = "ppef_sources.csv"
SOURCE_COLUMNS = filesources.COLUMNS


def sources_problems(con: duckdb.DuckDBPyConnection, releases: Path | None) -> list[str]:
    """Loaded PPEF files that ``releases/ppef_sources.csv`` does not record with the same sha256."""
    if not loaded(con) or releases is None:
        return []
    loaded_files = con.execute("SELECT snapshot_id, file, sha256 FROM ppef_snapshot_stream ORDER BY 1, 2").fetchall()
    return filesources.manifest_problems(Path(releases) / SOURCES_FILE, loaded_files,
                                         "run `healthcare-ledger sources --source-name ppef` and fill in the urls")


def read_sources(releases: Path) -> list[dict[str, str]]:
    return filesources.read_manifest(Path(releases) / SOURCES_FILE, "PPEF")


def download_sources(releases: Path, dest: Path, *, check_only: bool = False, pace: float = 1.0) -> list[str]:
    """Rebuild the quarter directories of ``releases/ppef_sources.csv`` under ``dest``, sha256-checked
    (filesources.download_recorded). Returns the problems; empty means every file is present and matches."""
    return filesources.download_recorded(read_sources(releases), dest, check_only=check_only, pace=pace,
                                         manifest_name=SOURCES_FILE)


def write_sources(con: duckdb.DuckDBPyConnection, releases: Path) -> int:
    """Write or update ``releases/ppef_sources.csv`` from the warehouse, keeping origin/url/member already recorded.

    Returns the number of rows whose url is still empty (the maintainer fills those from
    data.cms.gov's dataset-resources listing or the historical zips; docs/MAINTAINING.md).
    """
    rows = con.execute(
        "SELECT ss.snapshot_id, s.directory, ss.file, ss.bytes, ss.sha256 FROM ppef_snapshot_stream ss "
        "JOIN ppef_snapshot s USING (snapshot_id) ORDER BY 1, 3").fetchall()
    return filesources.write_manifest(Path(releases) / SOURCES_FILE, rows)


def parse_ymd(s: str) -> date:
    return datetime.strptime(s, "%Y%m%d").date()


# --------------------------------------------------------------------- source
def discover_checked(source_dir: Path) -> list[PpefSnapshot]:
    """``discover_ppef``, with a UserError naming the fix when ``source_dir`` holds no quarter."""
    src = Path(source_dir)
    if not src.is_dir():
        raise UserError(f"PPEF source directory {src} does not exist; it should hold one subdirectory per quarter "
                        f"(2016-Q1, 2016-Q2, ...) with that quarter's PPEF_*.csv files (docs/MAINTAINING.md)", exit_code=2)
    snaps = discover_ppef(src)
    if not snaps:
        raise UserError(f"no quarter directories (YYYY-QN) with a PPEF_Enrollment_Extract_*.csv under {src}", exit_code=2)
    return snaps


SOURCE = Source(
    name="ppef",
    database=PpefWarehouse.database,
    title="PPEF",
    kind="versioned",
    key=KEY,
    streams=PPEF_STREAMS,
    required=False,
    snapshots_file=SNAPSHOTS_FILE,
    sources_file=SOURCES_FILE,
    default_source_dir="data/ppef",
    snapshot_noun="quarters",
    warehouse_cls=PpefWarehouse,
    discover=discover_checked,
    loaded=loaded,
    finalize=finalize,
    reset=reset,
    check_problems=check_problems,
    stored_fingerprints=stored_fingerprints,
    recompute_fingerprints=recompute_fingerprints,
    export_tables=export_tables,
    data_dictionary_rows=data_dictionary_rows,
    snapshots_sql=snapshots_sql,
    validate_streams=lambda: [(s.name, KEY, "ppef_snapshot_stream", False) for s in PPEF_STREAMS],
    status=quarter_status("ppef_snapshot"),
    check_sources=recorded_files_gate("ppef", "PPEF", SOURCES_FILE, sources_problems),
    write_sources=write_sources,
    download=recorded_download("PPEF", download_sources),
)
