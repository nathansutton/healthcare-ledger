"""CMS's Doctors and Clinicians National Downloadable File (formerly Physician Compare), 2014 to 2022, as an SCD Type 2
table keyed by NPI.

The file lists every clinician enrolled in Medicare (from PECOS) with one row per group practice the clinician
reassigns benefits to and practice address: NPI, the clinician's PAC id and enrollment id, name, credential, school,
specialties, the group's legal name, PAC id and member count, the practice address and phone, up to five hospitals
the clinician is affiliated with (CCN and legal business name, from Medicare claims) and participation flags
(Medicare assignment, the quality and EHR programs of the year). It is the claims-filtered clinician -> group and
clinician -> hospital map the provider graph uses.

CMS replaced the file roughly quarterly (monthly in 2021) and keeps the old ones in yearly archive folders of
data.cms.gov, which no longer serves them; the copies come from the Internet Archive (``releases/
doctors_clinicians_sources.csv``). The source directory holds one folder per year with CMS's zips in it, under CMS's
names: ``2014/doc_archive_03_2014.zip``, ``2020/doctors_and_clinicians_archive_08_2020.zip``,
``2021/doctors_and_clinicians_01_2021.zip``. Each zip is one snapshot; its *as-of date* is the first day of the month
in its name. A zip counts only once its download finished (a ``<zip>.done`` marker next to it); the yearly bundles
without a month in the name repeat the quarterly zips and are skipped. ``download`` rebuilds the folders with the
CSV of each zip extracted as ``<year>/<zip stem>.csv``, which discovery reads the same way.

Vintage quirks the loader absorbs:

* the March and June 2014 and December 2016 files have no header row, and the October 2016 file's header leaves out a
  field its rows have; the loader writes the header of the layout of the same width (``LAYOUTS``: the September 2014
  and July 2016 headers) in front of the extracted copy, or in place of the short header;
* the headers change from the 2014 to 2019 long names (``Group Practice PAC ID``, renamed again in July 2016:
  ``Hospital affiliation CCN 1``, ``Reported Quality Measures``) to the 2020 short ones (``org_pac_id``);
  ``dac_columns.csv`` maps them all onto one column list, and columns not in every vintage (the DBA name, the phone,
  the program flags of a year, telehealth, the address id) are ``hash_optional``;
* the CSV inside the zip changes name (``National_Downloadable_File.csv``, ``Refresh_Data_Archive_10_2016.csv``,
  ``mj5m-pzi6.csv``, ``DAC_NationalDownloadableFile.csv``) and from 2016 sits next to quality-reporting files;
* some files are latin-1 and others UTF-8 (``core.csvread`` falls back);
* the 2014 files write a missing member count as ``.``;
* every file repeats some rows exactly (``post_stage``);
* a few dozen rows a file have an unescaped comma or quote in the group name, which shifts the fields after it; they
  are kept as CMS wrote them and stay under the shape checks' tolerance.

The member count is the group's, so a group gaining or losing one clinician is a new version of every NPI in it; with
the claims-based hospital affiliations, which CMS recomputes each refresh, it accounts for most of the changed NPIs of
a quarter (of 618,769 changed in October 2015, 275,425 changed in the count alone). That is what the files say and is
kept.

Rules (docs/METHODOLOGY.md sections 2 to 5 with the NPI as key and many rows per key): trimmed text, blanks and
placeholders to NULL; the graduation year and the member count as canonical integers; a PAC id that lost its leading
zeros (eight or nine digits) padded back to ten; the credential NULL when it holds nine digits; a person's first,
middle or last name with a digit in it NULL; the tax-id token rule of 2a on every other free-text column. The rows of
an NPI are versioned together, so a clinician joining or leaving a group, moving or changing hospital is one new
version of that NPI.
"""

from __future__ import annotations

import csv
import hashlib
import logging
import re
import subprocess
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import IO

import duckdb

from ...core import columns, engine, filesources
from ...core.columns import Column, header_key
from ...core.engine import Staged, Stats, VersionedWarehouse
from ...core.errors import UserError
from ...core.privacy import credential_bad, free_text_columns, has_token, redact_expr
from ...core.published import INTERVAL, Table, interval_columns

log = logging.getLogger(__name__)

KEY = "npi"
COLUMNS_FILE = Path(__file__).with_name("dac_columns.csv")
YEAR_DIR_RE = re.compile(r"^\d{4}$")
# doc_archive_03_2014, doctors_and_clinicians_archive_08_2020, doctors_and_clinicians_12_2022: a month and a year at
# the end; a yearly bundle (doctors_and_clinicians_archive_2016) has no month and does not match
SNAPSHOT_NAME_RE = re.compile(r"^(?P<prefix>[a-z_]+?)_(?P<month>0[1-9]|1[0-2])_(?P<year>\d{4})$", re.IGNORECASE)
PERSON_NAME_COLUMNS = {"first_name", "middle_name", "last_name"}
CREDENTIAL_COLUMNS = {"credential"}
PAC_ID_COLUMNS = {"ind_pac_id", "org_pac_id"}

_PERSON = ("NPI", "PAC ID", "Professional Enrollment ID", "Last Name", "First Name", "Middle Name", "Suffix", "Gender",
           "Credential", "Medical school name", "Graduation year", "Primary specialty", "Secondary specialty 1",
           "Secondary specialty 2", "Secondary specialty 3", "Secondary specialty 4", "All secondary specialties",
           "Organization legal name", "Group Practice PAC ID", "Number of Group Practice members",
           "Line 1 Street Address", "Line 2 Street Address", "Marker of address line 2 suppression", "City", "State",
           "Zip Code")
# The header of the September 2014 file, the first with a header row. The March and June 2014 files have these 40
# fields in this order and no header.
HEADER_2014 = (*_PERSON, *(f"Claims based hospital affiliation {x} {i}" for i in range(1, 6) for x in ("CCN", "LBN")),
               "Professional accepts Medicare Assignment", "Participating in eRx", "Participating in PQRS",
               "Participating in EHR")
# The header of the July 2016 file. The December 2016 file has these 42 fields and no header; the October 2016 file's
# header leaves out the Maintenance of Certification field its rows still have.
HEADER_2016 = (*_PERSON, "Phone Number", *(f"Hospital affiliation {x} {i}" for i in range(1, 6) for x in ("CCN", "LBN")),
               "Professional accepts Medicare Assignment", "Reported Quality Measures", "Used electronic health records",
               "Participated in the Medicare Maintenance of Certification Program.",
               "Committed to heart health through the Million Hearts initiative.")
# a file whose header is missing or does not match its rows is read with the layout of the same width
LAYOUTS = {len(HEADER_2014): HEADER_2014, len(HEADER_2016): HEADER_2016}


def _read_columns() -> tuple[Column, ...]:
    with open(COLUMNS_FILE, newline="") as f:
        return tuple(Column(r["name"], r["cms_header"], tuple(a for a in r["aliases"].split("|") if a), kind=r["kind"],
                            hash_optional=r["hash_optional"] == "yes") for r in csv.DictReader(f))


def column_descriptions() -> dict[str, str]:
    with open(COLUMNS_FILE, newline="") as f:
        return {r["name"]: r["description"] for r in csv.DictReader(f)}


@dataclass(frozen=True)
class DacStream:
    name: str
    key_header: str
    columns: tuple[Column, ...]
    description: str
    always_present: bool = True        # every snapshot is the whole file: an NPI missing from it has left it

    @property
    def data_columns(self) -> list[str]:
        return [c.name for c in self.columns]


CLINICIAN = DacStream(
    name="dac_clinician", key_header="NPI", columns=_read_columns(),
    description=("Clinicians enrolled in Medicare (CMS Doctors and Clinicians National Downloadable File): one row per "
                 "NPI, group practice and practice address, with the group's PAC id and size and up to five "
                 "affiliated hospitals"))
DAC_STREAMS: tuple[DacStream, ...] = (CLINICIAN,)


# ------------------------------------------------------------------ discovery
@dataclass
class DacSnapshot:
    path: Path                     # the zip, or the CSV ``download`` extracted from it
    as_of_date: date
    member: str | None             # the CSV inside the zip; None for a CSV
    header: tuple[str, ...] | None = None    # the header the loader writes for the file's own (``LAYOUTS``), when needed
    header_missing: bool = False   # the file's first line is data: ``header`` goes in front; else it replaces line 1
    members: dict[str, Path] = field(default_factory=dict)

    @property
    def snapshot_id(self) -> int:
        return int(self.as_of_date.strftime("%Y%m%d"))

    @property
    def stem(self) -> str:
        return self.path.name.rsplit(".", 1)[0]

    @property
    def label(self) -> str:
        return f"{self.path.parent.name}/{self.path.name}"

    @property
    def headerless(self) -> bool:
        return self.header_missing

    def has(self, stream: str) -> bool:
        return stream in self.members


def as_of_from_name(name: str) -> date | None:
    """The first day of the month in a CMS file name (``doc_archive_03_2014.zip`` -> 2014-03-01); None without one."""
    m = SNAPSHOT_NAME_RE.match(name.rsplit(".", 1)[0])
    return date(int(m["year"]), int(m["month"]), 1) if m else None


@contextmanager
def open_member(zip_path: Path, member: str) -> Iterator[IO[bytes]]:
    """A binary stream of one zip member. Deflate64 members, which zipfile rejects, go through the ``unzip`` command."""
    with zipfile.ZipFile(zip_path) as z:
        if z.getinfo(member).compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            with z.open(member) as f:
                yield f
            return
    proc = subprocess.Popen(["unzip", "-p", str(zip_path), member], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        yield proc.stdout
    finally:
        proc.stdout.close()
        if proc.wait() != 0:
            raise RuntimeError(f"unzip failed on {zip_path.name}:{member}: {proc.stderr.read().decode(errors='replace')[:200]}")


def csv_member(zip_path: Path) -> str:
    """The National Downloadable File inside a CMS zip: its only CSV, or the one whose name says so (from 2016 the zips
    also carry the quality-reporting files; the 2020 ones name it after the data.cms.gov dataset id, mj5m-pzi6)."""
    with zipfile.ZipFile(zip_path) as z:
        csvs = [n for n in z.namelist() if n.lower().endswith(".csv") and not n.startswith("__MACOSX")]
    if len(csvs) > 1:
        csvs = [n for n in csvs if any(k in header_key(Path(n).name) for k in ("nationaldownloadablefile", "mj5mpzi6"))]
    if len(csvs) != 1:
        raise ValueError(f"{zip_path.parent.name}/{zip_path.name}: expected one National Downloadable File csv, found {csvs}")
    return csvs[0]


def header_fix(first: bytes, second: bytes, where: str) -> tuple[tuple[str, ...] | None, bool]:
    """What the loader must do about the header, from the first two lines of a file: ``(None, False)`` when it is fine;
    ``(layout, True)`` when the first line is data (no header; the March, June and December 2014 and 2016 files), the
    layout chosen by its width; ``(layout, False)`` when the rows are wider than the header (October 2016) and every
    header name is in the layout of their width, which then replaces it. Anything else is an error."""
    head, row = (next(csv.reader([line.decode("latin-1")]), []) for line in (first, second))
    if head and header_key(head[0]) == "npi":
        if not row or len(row) <= len(head):
            return None, False
        layout = LAYOUTS.get(len(row))
        if layout is None or not {header_key(h) for h in head} <= {header_key(h) for h in layout}:
            raise ValueError(f"{where}: rows of {len(row)} fields under a header of {len(head)}, and no known layout fits")
        return layout, False
    if len(head) in LAYOUTS:
        return LAYOUTS[len(head)], True
    raise ValueError(f"{where}: the first line is neither a header starting with NPI nor a headerless row of "
                     f"{' or '.join(map(str, LAYOUTS))} fields ({len(head)} fields)")


def inspect_file(path: Path) -> DacSnapshot:
    as_of = as_of_from_name(path.name)
    assert as_of is not None
    where = f"{path.parent.name}/{path.name}"
    if path.suffix.lower() == ".zip":
        member = csv_member(path)
        with open_member(path, member) as f:
            first, second = f.readline(), f.readline()
    else:
        member = None
        with open(path, "rb") as f:
            first, second = f.readline(), f.readline()
    header, missing = header_fix(first, second, where)
    snap = DacSnapshot(path=path, as_of_date=as_of, member=member, header=header, header_missing=missing)
    snap.members[CLINICIAN.name] = path
    return snap


def discover_dac(source_dir: Path) -> list[DacSnapshot]:
    """One snapshot per finished zip (or extracted CSV) in the year folders of ``source_dir``, in as-of order.

    A zip without its ``.done`` marker is still downloading and is left out; a file without a month in its name (a
    yearly bundle) is skipped. Two files of one month are an error."""
    snaps: dict[date, DacSnapshot] = {}
    for d in sorted(p for p in Path(source_dir).iterdir() if p.is_dir() and YEAR_DIR_RE.match(p.name)):
        for f in sorted(d.iterdir()):
            ext = f.suffix.lower()
            if not f.is_file() or ext not in (".zip", ".csv"):
                continue
            if ext == ".zip" and not f.with_name(f.name + ".done").exists():
                log.info("%s/%s: no .done marker (still downloading?), skipping", d.name, f.name)
                continue
            as_of = as_of_from_name(f.name)
            if as_of is None:
                log.info("%s/%s: no month in the name (a yearly bundle), skipping", d.name, f.name)
                continue
            if as_of in snaps:
                raise ValueError(f"two files for {as_of:%Y-%m}: {snaps[as_of].label} and {d.name}/{f.name}")
            snaps[as_of] = inspect_file(f)
    return [snaps[k] for k in sorted(snaps)]


def discover_checked(source_dir: Path) -> list[DacSnapshot]:
    src = Path(source_dir)
    if not src.is_dir():
        raise UserError(f"Doctors and Clinicians source directory {src} does not exist; it should hold one folder per year "
                        f"with CMS's archive zips (doc_archive_MM_YYYY.zip, doctors_and_clinicians_MM_YYYY.zip); fetch "
                        f"them with `healthcare-ledger download --source-name doctors_clinicians`", exit_code=2)
    snaps = discover_dac(src)
    if not snaps:
        raise UserError(f"no Doctors and Clinicians files (YYYY/<name>_MM_YYYY.zip with a .done marker, or .csv) under {src}",
                        exit_code=2)
    return snaps


# ------------------------------------------------------------------ normalization
def int_norm(expr: str) -> str:
    """Canonical text of a whole number (``0420``, ``420`` and ``420.0`` agree); NULL for ``.``, the missing value the
    2014 files write for a member count (SAS's); the raw text when it is not a number."""
    return (f"CASE WHEN {expr} = '.' THEN NULL WHEN regexp_matches({expr}, '^[0-9]+(\\.0*)?$') "
            f"THEN CAST(CAST(CAST({expr} AS DOUBLE) AS BIGINT) AS VARCHAR) ELSE {expr} END")


def pac_id_norm(expr: str) -> str:
    """A PAC id written from a number (eight or nine digits, its leading zeros lost) padded back to ten digits. Shorter
    digit runs are fields shifted by an unescaped comma, not ids, and are left alone for the shape check."""
    return f"CASE WHEN regexp_matches({expr}, '^[0-9]{{8,9}}$') THEN lpad({expr}, 10, '0') ELSE {expr} END"


# Snapshots at which CMS changed who is in the file, with the share of the previous snapshot's NPIs the absent guard lets
# go there and why. Anything above that share still stops the load.
COVERAGE_BREAKS: dict[int, tuple[float, str]] = {
    20180401: (0.10, "the April 2018 file, which CMS rebuilt in August 2019, leaves out 95,199 of the 1,056,035 NPIs of "
                     "December 2017 (9.0%) across every state and specialty and adds 56,733; 17,258 of the 95,199 are "
                     "back in July 2018"),
}


# -------------------------------------------------------------------- warehouse
class DacWarehouse(VersionedWarehouse):
    source_label = "Doctors and Clinicians"
    database = "doctors_clinicians"
    key = KEY
    key_noun = "NPIs"
    streams = DAC_STREAMS
    snapshot_table = "dac_snapshot"
    stream_table = "dac_snapshot_stream"
    unmapped_hint = "; add them to dac_columns.csv"
    reader_options = {"repair": True}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._member_facts: dict[str, tuple[int, str]] = {}     # stream -> (bytes, sha256) of the CSV as CMS wrote it

    def init_bookkeeping(self, con: duckdb.DuckDBPyConnection) -> None:
        con.execute("""
            CREATE TABLE IF NOT EXISTS dac_snapshot (
                snapshot_id INTEGER NOT NULL, as_of_date DATE NOT NULL, directory VARCHAR NOT NULL,
                streams_present VARCHAR[], loaded_at TIMESTAMP NOT NULL, seconds DOUBLE)""")
        con.execute("""
            CREATE TABLE IF NOT EXISTS dac_snapshot_stream (
                snapshot_id INTEGER NOT NULL, stream VARCHAR NOT NULL, file VARCHAR NOT NULL,
                bytes BIGINT, sha256 VARCHAR, n_columns INTEGER, n_rows BIGINT, n_keys BIGINT,
                n_new BIGINT, n_changed BIGINT, n_unchanged BIGINT, n_absent BIGINT, n_bad_dates BIGINT,
                n_bad_numbers BIGINT, csv_mode VARCHAR, content_fingerprint VARCHAR)""")

    def member_path(self, snap: DacSnapshot, stream: DacStream) -> Path:
        """The CSV of ``snap`` on local disk as ``<zip stem>.csv`` (the name the bookkeeping and the sources manifest
        record): extracted from the zip, with ``snap.header`` written in front of a file that has none or in place of
        a header that does not match the rows. The size and sha256 recorded are those of the CSV as CMS wrote it."""
        if snap.member is None and snap.header is None:
            return snap.path
        out = self.work_dir / f"{snap.stem}.csv"
        self._scratch.append(out)
        h, n = hashlib.sha256(), 0
        with open(out, "wb") as dst, \
                (open_member(snap.path, snap.member) if snap.member else open(snap.path, "rb")) as src:
            if snap.header is not None:
                if not snap.header_missing:
                    line = src.readline()
                    h.update(line)
                    n += len(line)
                log.info("%s: %s; reading it with the %d-field layout", snap.label,
                         "no header row" if snap.header_missing else "the header is narrower than the rows", len(snap.header))
                dst.write((",".join(snap.header) + "\n").encode())
            while chunk := src.read(64 << 20):
                h.update(chunk)
                n += len(chunk)
                dst.write(chunk)
        self._member_facts[stream.name] = (n, h.hexdigest())
        return out

    def record(self, snap: DacSnapshot, stats: list[Stats], seconds: float) -> None:
        for out in stats:
            if out.stream in self._member_facts:
                out.bytes, out.sha256 = self._member_facts[out.stream]
        self._member_facts.clear()
        super().record(snap, stats, seconds)

    def column_expr(self, stream: DacStream, col: Column, src: str) -> str:
        expr = columns.null_placeholders(src, columns.placeholder_tokens(col.name))
        if col.kind == "int":
            return int_norm(expr)
        if col.name in PAC_ID_COLUMNS:
            return pac_id_norm(expr)
        if col.name in PERSON_NAME_COLUMNS:
            return f"CASE WHEN regexp_matches({expr}, '[0-9]') THEN NULL ELSE {expr} END"
        if col.name in CREDENTIAL_COLUMNS:
            return f"CASE WHEN {credential_bad(expr)} THEN NULL ELSE {expr} END"
        if col.name in free_text_columns(stream):
            return redact_expr(expr, col.name)
        return expr

    def post_stage(self, stream: DacStream, stg: str) -> None:
        c = self.con
        # The files repeat some rows exactly (10 thousand in March 2014, 200 thousand in May 2016, two in July 2016), an
        # artifact of the joins CMS built them with: a repeat says nothing, and keeping it would make a clinician's
        # rows differ between quarters only in how often one is repeated. Each distinct row is loaded once. (A row with
        # a blank NPI, the first of the June 2014 file, never gets here: the engine drops rows without a key.)
        before = c.execute(f"SELECT count(*) FROM {stg}").fetchone()[0]
        c.execute(f"CREATE OR REPLACE TEMP TABLE {stg} AS SELECT DISTINCT * FROM {stg}")
        after = c.execute(f"SELECT count(*) FROM {stg}").fetchone()[0]
        if after < before:
            log.info("%s: %d exact duplicate row(s) loaded once", stream.name, before - after)

    def keep_empty(self, stream: DacStream) -> bool:
        return False

    def counted_absent(self, st: Staged, delta: str, out: Stats) -> int:
        # Each file is the whole list, so every NPI missing from it counts, except at a coverage break CMS made
        # (COVERAGE_BREAKS), where the share it is known to drop is let through, and only that share.
        allowed = COVERAGE_BREAKS.get(st.snapshot_id)
        prev = out.n_changed + out.n_unchanged + out.n_absent
        if allowed is not None and prev and out.n_absent <= allowed[0] * prev:
            log.warning("%s %s: %d of %d NPIs absent (%.1f%%), a known coverage break: %s", st.snapshot_id, st.stream.name,
                        out.n_absent, prev, 100 * out.n_absent / prev, allowed[1])
            return 0
        return out.n_absent

    def typed_select(self, stream: DacStream) -> tuple[list[str], dict[str, list[str]]]:
        typed, bad = [], {"n_bad_numbers": []}
        for col in stream.columns:
            if col.kind == "int":
                typed.append(f"try_cast({col.name} AS INTEGER) AS {col.name}")
                bad["n_bad_numbers"].append(f"count(*) FILTER (WHERE {col.name} IS NOT NULL AND try_cast({col.name} AS INTEGER) IS NULL)")
            else:
                typed.append(col.name)
        return typed, bad


def loaded(con: duckdb.DuckDBPyConnection) -> bool:
    return engine.loaded(con, DacWarehouse)


def finalize(con: duckdb.DuckDBPyConnection) -> None:
    engine.finalize(con, DacWarehouse)


def reset(con: duckdb.DuckDBPyConnection) -> list[str]:
    return engine.reset(con, DacWarehouse)


def stored_fingerprints(con: duckdb.DuckDBPyConnection) -> dict[tuple[int, str], str | None]:
    return engine.stored_fingerprints(con, DacWarehouse)


def recompute_fingerprints(con: duckdb.DuckDBPyConnection) -> dict[tuple[int, str], str]:
    return engine.recompute_fingerprints(con, DacWarehouse)


# -------------------------------------------------------------------- checks
# Shape of the id columns. The rows CMS shifted with an unescaped comma (a few dozen a file) fail them and are tolerated
# up to SHAPE_TOLERANCE; more means a column was read under the wrong header.
SHAPE_CHECKS = [(KEY, "^[0-9]{10}$"), ("ind_pac_id", "^[0-9]{10}$"), ("org_pac_id", "^[0-9]{10}$"),
                ("enrollment_id", "^I[0-9]{14}$")] + [(f"hosp_afl_{i}_ccn", "^[0-9A-Z]{6}$") for i in range(1, 6)]
SHAPE_TOLERANCE = 0.0001    # share of rows


def check_problems(con: duckdb.DuckDBPyConnection) -> list[str]:
    """Privacy (METHODOLOGY 2a, zero tolerance) and shape checks over dac_clinician_version; returns the failures."""
    if not loaded(con):
        return []
    s = CLINICIAN
    checks = [(f"privacy: {s.name}.{n} holds a tax-id token", has_token(n, n), 0) for n in free_text_columns(s)]
    checks += [(f"privacy: {s.name}.{n} contains a digit", f"regexp_matches({n}, '[0-9]')", 0)
               for n in s.data_columns if n in PERSON_NAME_COLUMNS]
    checks += [(f"privacy: {s.name}.{n} holds nine digits", credential_bad(n), 0) for n in CREDENTIAL_COLUMNS]
    checks += [(f"shape: {s.name}.{c} does not match {rx}", f"NOT regexp_matches({c}, '{rx}')", SHAPE_TOLERANCE)
               for c, rx in SHAPE_CHECKS]
    row = con.execute(f"SELECT count(*), {', '.join(f'count(*) FILTER (WHERE {p})' for _, p, _ in checks)} "
                      f"FROM {s.name}_version").fetchone()
    return [f"{d}: {n} row(s)" for (d, _, tol), n in zip(checks, row[1:]) if n > tol * row[0]]


# --------------------------------------------------------------------- export
SNAPSHOTS_FILE = "doctors_clinicians_snapshots.csv"


def snapshots_sql() -> str:
    return ("SELECT s.snapshot_id, s.as_of_date, s.directory, c.file, c.n_rows AS rows, c.n_keys AS npis, c.n_new AS new, "
            "c.n_changed AS changed, c.n_absent AS absent, c.content_fingerprint AS dac_clinician_fingerprint "
            "FROM dac_snapshot s LEFT JOIN dac_snapshot_stream c ON c.snapshot_id = s.snapshot_id AND c.stream = 'dac_clinician' "
            "ORDER BY s.snapshot_id")


def tables() -> tuple[Table, ...]:
    """One table, the clinician rows as loaded: one row per NPI, group and practice address of each version."""
    desc = column_descriptions()
    lead = interval_columns(KEY, "NPI", "NPI of the clinician", "Snapshot date (the month of CMS's file)",
                            "the NPI left the file")
    cols = lead + tuple((c.name, c.cms_header, desc.get(c.name, "")) for c in CLINICIAN.columns)
    sql = f"SELECT {KEY}, {', '.join(INTERVAL)}, {', '.join(CLINICIAN.data_columns)} FROM {CLINICIAN.name}_version"
    return (Table("doctors_clinicians", (KEY,),
                  "Clinicians enrolled in Medicare with their group practices (PAC id, size), practice addresses and up to "
                  "five claims-based hospital affiliations, from CMS's Doctors and Clinicians (Physician Compare) National "
                  "Downloadable File, 2014 to 2022; several rows per NPI", sql, cols),)


# -------------------------------------------------------------------- sources
SOURCES_FILE = "doctors_clinicians_sources.csv"


def sources_problems(con: duckdb.DuckDBPyConnection, releases: Path | None) -> list[str]:
    if not loaded(con) or releases is None:
        return []
    loaded_files = con.execute("SELECT snapshot_id, file, sha256 FROM dac_snapshot_stream ORDER BY 1, 2").fetchall()
    return filesources.manifest_problems(Path(releases) / SOURCES_FILE, loaded_files,
                                         "run `healthcare-ledger sources --source-name doctors_clinicians` and fill in the urls")


def read_sources(releases: Path) -> list[dict[str, str]]:
    return filesources.read_manifest(Path(releases) / SOURCES_FILE, "Doctors and Clinicians")


def download_sources(releases: Path, dest: Path, *, check_only: bool = False, pace: float = 1.0) -> list[str]:
    """Rebuild ``<year>/<zip stem>.csv`` under ``dest`` from the zips the manifest records (filesources.download_recorded)."""
    return filesources.download_recorded(read_sources(releases), dest, check_only=check_only, pace=pace,
                                         manifest_name=SOURCES_FILE)


def write_sources(con: duckdb.DuckDBPyConnection, releases: Path) -> int:
    """Write or update ``releases/doctors_clinicians_sources.csv`` from the warehouse; ``directory`` is the year folder."""
    rows = con.execute(
        "SELECT ss.snapshot_id, split_part(s.directory, '/', 1), ss.file, ss.bytes, ss.sha256 FROM dac_snapshot_stream ss "
        "JOIN dac_snapshot s USING (snapshot_id) ORDER BY 1, 3").fetchall()
    return filesources.write_manifest(Path(releases) / SOURCES_FILE, rows)


def pin_downloads(raw_dir: Path, releases: Path) -> int:
    """Write ``releases/doctors_clinicians_sources.csv`` from the downloader's ``manifest.tsv`` (``name, bytes,
    sha256, url`` per finished zip, the url an Internet Archive capture) without loading anything: one row per zip, with
    the size and sha256 of its CSV member, which is what the loader records, and that member's path inside the zip.
    Rows already in the file are kept. Returns the number of rows written."""
    raw_dir, path = Path(raw_dir), Path(releases) / SOURCES_FILE
    old: dict[tuple[int, str], dict[str, str]] = {}
    if path.exists():
        with open(path, newline="") as f:
            old = {(int(r["snapshot_id"]), r["file"]): r for r in csv.DictReader(f)}
    with open(raw_dir / "manifest.tsv", newline="") as f:
        urls = {r[0]: r[3] for r in csv.reader(f, delimiter="\t") if len(r) >= 4}
    rows = dict(old)
    for snap in discover_dac(raw_dir):
        if snap.member is None:
            continue
        file, sid = f"{snap.stem}.csv", snap.snapshot_id
        if (sid, file) in rows:
            continue
        h, n = hashlib.sha256(), 0
        with open_member(snap.path, snap.member) as src:
            while chunk := src.read(64 << 20):
                h.update(chunk)
                n += len(chunk)
        url = urls.get(snap.label, "")
        rows[(sid, file)] = {"snapshot_id": sid, "directory": snap.path.parent.name, "file": file, "bytes": n,
                             "sha256": h.hexdigest(), "origin": "wayback" if "web.archive.org" in url else "",
                             "url": url, "member": snap.member}
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=filesources.COLUMNS, lineterminator="\n")
        w.writeheader()
        w.writerows(rows[k] for k in sorted(rows))
    return len(rows)
