"""CMS's Medicare facility Enrollments and All Owners files as monthly SCD Type 2 tables, keyed by enrollment id.

From September 2022 CMS publishes on data.cms.gov, for six kinds of Part A facility, two files each month or
quarter, extracted from PECOS (the Medicare enrollment system):

``<Type>_Enrollments_<date>.csv``
    one row per enrollment: NPI, CCN, the PECOS associate id (PAC id) of the legal entity, name and doing-business-as
    name, incorporation, organization structure, proprietary or nonprofit, practice address, and per type some extras
    (hospital subgroups, the nursing home affiliated entity, the home health practice location type).
``<Type>_All_Owners_<date>.csv``
    one row per enrollment and owner or managing party: the owner's own PAC id, whether a person (``I``) or an
    organization (``O``), the role (direct owner, indirect owner, partner, officer, director, managing employee,
    operational or managerial control...), the date of the association, the owner's name and address, the share
    owned, and flags for what kind of organization the owner is (holding company, investment firm, private equity,
    REIT, chain home office...).

The types are hospitals (monthly from November 2022), skilled nursing facilities (monthly from September 2022),
home health agencies and hospices (quarterly from March 2023), FQHCs and rural health clinics (quarterly from
October 2023). Each file is one stream, ``ownership_<type>_enrollment`` or ``ownership_<type>_owner``; the six types
never share an enrollment id. A stream missing from a snapshot (most months carry only the hospital and SNF files)
closes nothing (core.engine).

The source directory holds one subdirectory per release date, ``YYYY-MM-DD``, with that release's CSVs under CMS's
file names. The *as-of date* is the directory name: the extract date in the file names, or for the first SNF and
hospital files, which carry none, the date data.cms.gov gives the release (``releases/ownership_sources.csv``
records each file's url). The loader tells the streams apart by the file name's prefix.

Rules (docs/METHODOLOGY.md sections 2 to 5 with the enrollment id in place of the NPI): trimmed text, blanks and
placeholders to NULL; dates normalized to ISO before hashing (CMS writes ``MM/DD/YYYY``); the ownership share as a
number; the tax-id token rule of 2a on every free-text column (names, addresses, titles, other-type text); a person's
first, middle or last name with a digit in it is NULL. Columns not in every file of a stream are ``hash_optional``.
The column list of every stream is ``ownership_columns.csv`` next to this module, built from the headers of every
file CMS has published.
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
from ...core.columns import Column
from ...core.engine import VersionedWarehouse
from ...core.errors import UserError
from ...core.privacy import free_text_columns, has_token, redact_expr
from ...core.published import INTERVAL, Table, interval_columns

log = logging.getLogger(__name__)

KEY = "enrollment_id"
SNAPSHOT_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
COLUMNS_FILE = Path(__file__).with_name("ownership_columns.csv")
# facility type -> (CMS's file-name prefix, what the file calls it)
FACILITY_TYPES = {
    "hospital": ("Hospital", "hospitals, critical access hospitals and rural emergency hospitals"),
    "snf": ("SNF", "skilled nursing facilities"),
    "hha": ("HHA", "home health agencies"),
    "hospice": ("Hospice", "hospices"),
    "fqhc": ("FQHC", "federally qualified health centers"),
    "rhc": ("RHC", "rural health clinics"),
}
FILE_KINDS = {"enrollment": "Enrollments", "owner": "All_Owners"}
PERSON_NAME_COLUMNS = {"owner_first_name", "owner_middle_name", "owner_last_name"}
PAC_COLUMNS = {"pac_id", "owner_pac_id"}
ZIP_COLUMNS = {"zip", "owner_zip"}
DATE_FORMATS = ("%m/%d/%Y", "%Y-%m-%d", "%Y%m%d")


@dataclass(frozen=True)
class OwnershipStream:
    name: str
    facility_type: str
    file_kind: str                 # enrollment | owner
    key_header: str
    columns: tuple[Column, ...]
    description: str

    @property
    def data_columns(self) -> list[str]:
        return [c.name for c in self.columns]

    def matches(self, filename: str) -> bool:
        prefix, _ = FACILITY_TYPES[self.facility_type]
        return bool(re.match(rf"^{prefix}_{FILE_KINDS[self.file_kind]}(_|\.csv$)", filename, re.IGNORECASE))


def _read_columns() -> dict[tuple[str, str], tuple[Column, ...]]:
    """(facility type, file kind) -> its columns, from ownership_columns.csv (one row per column and stream)."""
    out: dict[tuple[str, str], list[Column]] = {}
    with open(COLUMNS_FILE, newline="") as f:
        for r in csv.DictReader(f):
            out.setdefault((r["facility_type"], r["file_kind"]), []).append(
                Column(r["name"], r["cms_header"], tuple(a for a in r["aliases"].split("|") if a), kind=r["kind"],
                       hash_optional=r["hash_optional"] == "yes"))
    return {k: tuple(v) for k, v in out.items()}


def column_descriptions() -> dict[str, str]:
    with open(COLUMNS_FILE, newline="") as f:
        return {r["name"]: r["description"] for r in csv.DictReader(f)}


_COLUMNS = _read_columns()
OWNERSHIP_STREAMS: tuple[OwnershipStream, ...] = tuple(
    OwnershipStream(
        name=f"ownership_{t}_{kind}", facility_type=t, file_kind=kind, key_header="ENROLLMENT ID",
        columns=_COLUMNS[(t, kind)],
        description=(f"Medicare enrollments of {desc} (CMS {prefix} Enrollments file): NPI, CCN, PAC id, name, "
                     f"organization structure and address, one row per enrollment id" if kind == "enrollment" else
                     f"Owners and managing parties of the Medicare enrollments of {desc} (CMS {prefix} All Owners "
                     f"file): one row per enrollment id and owner, with the owner's PAC id, role, share and type"))
    for t, (prefix, desc) in FACILITY_TYPES.items() for kind in FILE_KINDS)
STREAM_BY_NAME = {s.name: s for s in OWNERSHIP_STREAMS}


# ------------------------------------------------------------------ discovery
@dataclass
class OwnershipSnapshot:
    directory: Path
    as_of_date: date
    members: dict[str, Path] = field(default_factory=dict)

    @property
    def snapshot_id(self) -> int:
        return int(self.as_of_date.strftime("%Y%m%d"))

    @property
    def label(self) -> str:
        return self.directory.name

    def has(self, stream: str) -> bool:
        return stream in self.members and self.members[stream].stat().st_size > 0


def classify(filename: str) -> OwnershipStream | None:
    return next((s for s in OWNERSHIP_STREAMS if s.matches(filename)), None)


def inspect_release(directory: Path) -> OwnershipSnapshot:
    members: dict[str, Path] = {}
    for f in sorted(directory.iterdir()):
        if not f.is_file() or f.suffix.lower() != ".csv":
            continue
        s = classify(f.name)
        if s is None:
            raise ValueError(f"{directory.name}/{f.name}: not an Enrollments or All Owners file of a known facility type")
        if s.name in members:
            raise ValueError(f"{directory.name}: two {s.name} files, {members[s.name].name} and {f.name}")
        members[s.name] = f
    if not members:
        raise ValueError(f"{directory.name}: no Enrollments or All Owners csv")
    return OwnershipSnapshot(directory=directory, as_of_date=date.fromisoformat(directory.name), members=members)


def discover_ownership(source_dir: Path) -> list[OwnershipSnapshot]:
    """Release directories of ``source_dir`` in as-of order."""
    return [inspect_release(d) for d in sorted(source_dir.iterdir()) if d.is_dir() and SNAPSHOT_DIR_RE.match(d.name)]


def discover_checked(source_dir: Path) -> list[OwnershipSnapshot]:
    src = Path(source_dir)
    if not src.is_dir():
        raise UserError(f"ownership source directory {src} does not exist; it should hold one subdirectory per release "
                        f"(YYYY-MM-DD) with that release's Enrollments and All Owners csv files; fetch them with "
                        f"`healthcare-ledger download --source-name ownership` (docs/MAINTAINING.md)", exit_code=2)
    snaps = discover_ownership(src)
    if not snaps:
        raise UserError(f"no release directories (YYYY-MM-DD) under {src}", exit_code=2)
    return snaps


def date_norm(expr: str) -> str:
    """ISO text of a date in any of CMS's formats; the raw text when it parses as none."""
    parsed = "coalesce(" + ", ".join(f"try_strptime({expr}, '{f}')" for f in DATE_FORMATS) + ")"
    return f"coalesce(strftime({parsed}, '%Y-%m-%d'), {expr})"


def number_norm(expr: str) -> str:
    """Canonical text of a share (``100``, ``100.0`` and ``100.00`` agree); the raw text when it is not a number."""
    return f"coalesce(CAST(try_cast({expr} AS DOUBLE) AS VARCHAR), {expr})"


_TYPED = {"date": ("try_strptime({c}, '%Y-%m-%d')::DATE", "try_strptime({c}, '%Y-%m-%d') IS NULL"),
          "number": ("try_cast({c} AS DOUBLE)", "try_cast({c} AS DOUBLE) IS NULL")}


# -------------------------------------------------------------------- warehouse
class OwnershipWarehouse(VersionedWarehouse):
    source_label = "ownership"
    database = "ownership"
    key = KEY
    key_noun = "enrollments"
    streams = OWNERSHIP_STREAMS
    snapshot_table = "ownership_snapshot"
    stream_table = "ownership_snapshot_stream"
    single_row_streams = frozenset(s.name for s in OWNERSHIP_STREAMS if s.file_kind == "enrollment")
    unmapped_hint = "; add them to ownership_columns.csv"
    reader_options = {"repair": True}

    def init_bookkeeping(self, con: duckdb.DuckDBPyConnection) -> None:
        con.execute("""
            CREATE TABLE IF NOT EXISTS ownership_snapshot (
                snapshot_id INTEGER NOT NULL, as_of_date DATE NOT NULL, directory VARCHAR NOT NULL,
                streams_present VARCHAR[], loaded_at TIMESTAMP NOT NULL, seconds DOUBLE)""")
        con.execute("""
            CREATE TABLE IF NOT EXISTS ownership_snapshot_stream (
                snapshot_id INTEGER NOT NULL, stream VARCHAR NOT NULL, file VARCHAR NOT NULL,
                bytes BIGINT, sha256 VARCHAR, n_columns INTEGER, n_rows BIGINT, n_keys BIGINT,
                n_new BIGINT, n_changed BIGINT, n_unchanged BIGINT, n_absent BIGINT, n_bad_dates BIGINT,
                n_bad_numbers BIGINT, csv_mode VARCHAR, content_fingerprint VARCHAR)""")

    def column_expr(self, stream: OwnershipStream, col: Column, src: str) -> str:
        expr = columns.null_placeholders(src, columns.placeholder_tokens(col.name))
        if col.kind == "date":
            return date_norm(expr)
        if col.kind == "number":
            # a share of 0 is how some releases write the share the next one leaves blank: not reported
            return f"CASE WHEN try_cast({expr} AS DOUBLE) = 0 THEN NULL ELSE {number_norm(expr)} END"
        if col.name in PAC_COLUMNS:
            # some releases write the 10-digit PAC id from a number and lose its leading zeros (the 2022-12 hospital
            # owners file, for 104,000 rows); the width is restored so the id matches across releases and sources
            return f"CASE WHEN regexp_matches({expr}, '^[0-9]{{1,9}}$') THEN lpad({expr}, 10, '0') ELSE {expr} END"
        if col.name in ZIP_COLUMNS:
            # ZIP codes lose their leading zero the same way: 4 digits -> ZIP5, 8 digits -> ZIP+4
            return (f"CASE WHEN regexp_matches({expr}, '^[0-9]{{3,4}}$') THEN lpad({expr}, 5, '0') "
                    f"WHEN regexp_matches({expr}, '^[0-9]{{7,8}}$') THEN lpad({expr}, 9, '0') ELSE {expr} END")
        if col.name in PERSON_NAME_COLUMNS:
            return f"CASE WHEN regexp_matches({expr}, '[0-9]') THEN NULL ELSE {expr} END"
        if col.name in free_text_columns(stream):
            return redact_expr(expr, col.name)
        return expr

    def keep_empty(self, stream: OwnershipStream) -> bool:
        return False

    def counted_absent(self, st, delta: str, out) -> int:
        # every stream is a whole file of its type: an enrollment missing from it has left the file
        return out.n_absent

    def typed_select(self, stream: OwnershipStream) -> tuple[list[str], dict[str, list[str]]]:
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
    return engine.loaded(con, OwnershipWarehouse)


def finalize(con: duckdb.DuckDBPyConnection) -> None:
    engine.finalize(con, OwnershipWarehouse)


def reset(con: duckdb.DuckDBPyConnection) -> list[str]:
    return engine.reset(con, OwnershipWarehouse)


def stored_fingerprints(con: duckdb.DuckDBPyConnection) -> dict[tuple[int, str], str | None]:
    return engine.stored_fingerprints(con, OwnershipWarehouse)


def recompute_fingerprints(con: duckdb.DuckDBPyConnection) -> dict[tuple[int, str], str]:
    return engine.recompute_fingerprints(con, OwnershipWarehouse)


# -------------------------------------------------------------------- checks
SHAPE_CHECKS = {"enrollment": [(KEY, "^O[0-9]{14}$"), ("npi", "^[0-9]{10}$"), ("pac_id", "^[0-9]{10}$")],
                "owner": [(KEY, "^O[0-9]{14}$"), ("pac_id", "^[0-9]{10}$"), ("owner_pac_id", "^[0-9]{10}$")]}
SHAPE_TOLERANCE = 0.0001


def check_problems(con: duckdb.DuckDBPyConnection) -> list[str]:
    """Privacy (METHODOLOGY 2a, zero tolerance) and shape checks over every loaded ownership_* version table."""
    if not loaded(con):
        return []
    problems: list[str] = []
    for s in OWNERSHIP_STREAMS:
        checks = [(f"privacy: {s.name}.{n} holds a tax-id token", has_token(n, n), 0) for n in free_text_columns(s)]
        checks += [(f"privacy: {s.name}.{n} contains a digit", f"regexp_matches({n}, '[0-9]')", 0)
                   for n in s.data_columns if n in PERSON_NAME_COLUMNS]
        checks += [(f"shape: {s.name}.{c} does not match {rx}", f"NOT regexp_matches({c}, '{rx}')", SHAPE_TOLERANCE)
                   for c, rx in SHAPE_CHECKS[s.file_kind] if c == KEY or c in s.data_columns]
        row = con.execute(f"SELECT count(*), {', '.join(f'count(*) FILTER (WHERE {p})' for _, p, _ in checks)} "
                          f"FROM {s.name}_version").fetchone()
        problems += [f"{d}: {n} row(s)" for (d, _, tol), n in zip(checks, row[1:]) if n > tol * row[0]]
    return problems


# --------------------------------------------------------------------- export
SNAPSHOTS_FILE = "ownership_snapshots.csv"


def snapshots_sql() -> str:
    joins, cols = [], []
    for s in OWNERSHIP_STREAMS:
        a = s.name.removeprefix("ownership_")
        joins.append(f"LEFT JOIN ownership_snapshot_stream {a} ON {a}.snapshot_id = s.snapshot_id AND {a}.stream = '{s.name}'")
        cols.append(f"{a}.file AS {a}_file, {a}.n_rows AS {a}_rows, {a}.n_keys AS {a}_keys, {a}.n_new AS {a}_new, "
                    f"{a}.n_changed AS {a}_changed, {a}.n_absent AS {a}_absent, {a}.content_fingerprint AS {a}_fingerprint")
    return (f"SELECT s.snapshot_id, s.as_of_date, s.directory, array_to_string(s.streams_present, ',') AS streams_present, "
            + ", ".join(cols) + " FROM ownership_snapshot s " + " ".join(joins) + " ORDER BY s.snapshot_id")


def _published(kind: str, name: str, description: str, gone: str) -> Table:
    """One table over the six streams of a file kind, with ``facility_type`` naming the stream a row comes from.

    A column a type's file lacks is NULL for that type. The enrollment ids of the six types are disjoint, so the
    union has one history per enrollment id."""
    streams = [s for s in OWNERSHIP_STREAMS if s.file_kind == kind]
    order: list[str] = []
    for s in streams:
        order += [c for c in s.data_columns if c not in order]
    headers = {c.name: c.cms_header for s in streams for c in s.columns}
    desc = column_descriptions()
    selects = [f"SELECT {KEY}, {', '.join(INTERVAL)}, '{s.facility_type}' AS facility_type, "
               + ", ".join(c if c in s.data_columns else f"NULL AS {c}" for c in order) + f" FROM {s.name}_version"
               for s in streams]
    lead = interval_columns(KEY, "ENROLLMENT ID", "PECOS enrollment id of the facility", "Release date", gone)
    lead += (("facility_type", "", "The file the row comes from: " + ", ".join(
        f"'{t}' ({d})" for t, (_, d) in FACILITY_TYPES.items())),)
    return Table(name, (KEY,) if kind == "enrollment" else (KEY,), description, " UNION ALL ".join(selects),
                 lead + tuple((c, headers[c], desc.get(c, "")) for c in order))


def tables() -> tuple[Table, ...]:
    return (
        _published("enrollment", "medicare_facility_enrollment",
                   "Medicare Part A facility enrollments (hospital, SNF, HHA, hospice, FQHC, RHC) with NPI, CCN and PAC id, "
                   "from CMS's monthly Enrollments files, 2022-09 on", "the enrollment left the file"),
        _published("owner", "medicare_facility_owner",
                   "Owners and managing parties of Medicare Part A facility enrollments, with the owner's PAC id, role, "
                   "share and type, from CMS's monthly All Owners files, 2022-09 on; several rows per enrollment",
                   "the enrollment left the file"),
    )


# -------------------------------------------------------------------- sources
SOURCES_FILE = "ownership_sources.csv"


def sources_problems(con: duckdb.DuckDBPyConnection, releases: Path | None) -> list[str]:
    if not loaded(con) or releases is None:
        return []
    loaded_files = con.execute("SELECT snapshot_id, file, sha256 FROM ownership_snapshot_stream ORDER BY 1, 2").fetchall()
    return filesources.manifest_problems(Path(releases) / SOURCES_FILE, loaded_files,
                                         "run `healthcare-ledger sources --source-name ownership` and fill in the urls")


def read_sources(releases: Path) -> list[dict[str, str]]:
    return filesources.read_manifest(Path(releases) / SOURCES_FILE, "ownership")


def download_sources(releases: Path, dest: Path, *, check_only: bool = False, pace: float = 1.0) -> list[str]:
    return filesources.download_recorded(read_sources(releases), dest, check_only=check_only, pace=pace,
                                         manifest_name=SOURCES_FILE)


def write_sources(con: duckdb.DuckDBPyConnection, releases: Path) -> int:
    rows = con.execute(
        "SELECT ss.snapshot_id, s.directory, ss.file, ss.bytes, ss.sha256 FROM ownership_snapshot_stream ss "
        "JOIN ownership_snapshot s USING (snapshot_id) ORDER BY 1, 3").fetchall()
    return filesources.write_manifest(Path(releases) / SOURCES_FILE, rows)
