"""Release policy: content fingerprints, release ids, editions and the continuity check.

A *release* is one published export directory. Its id is::

    e<edition>-<YYYYMMDD of the last as-of date>-<first 8 hex of sha256(SHA256SUMS)>

The suffix is derived from the bytes actually published, so two exports can never share
an id unless their data files are byte-identical. Export bytes are not reproducible at
full scale (DuckDB writes rows in any order), so there is no revision counter to keep in
sync between machines.

An *edition* groups releases whose versioning rules never changed. Within an edition every
later release contains every earlier release exactly: filter the version tables to the
earlier release's last as-of date and you get its rows. The proof is per snapshot: each
loaded snapshot has one *content fingerprint* per stream, md5 over the ``npi:version_hash``
lines of the NPIs valid at the snapshot's as-of date, sorted by NPI (``core.versioning.fingerprint_sql``).
``snapshots.csv`` publishes them. Before writing anything, ``export``

* recomputes every fingerprint from the version tables and compares it with the value the
  loader stored at load time (a mismatch means the warehouse changed after load), and
* compares the fingerprints and the data dictionary with the latest release of the current
  edition, read from ``releases/<id>/`` in the repository checkout. A fingerprint that
  differs, a snapshot that disappeared, or a column that was removed, renamed or retyped
  blocks the export. Added columns pass.

A fix that changes any earlier fingerprint is a *breaking correction*: it starts a new
edition with ``export --new-edition``, which requires a dated entry in the METHODOLOGY
change log newer than the previous release and increments ``releases/EDITION``.

``releases/<id>/`` holds only the small metadata of a release (``manifest.json``,
``SHA256SUMS``, ``snapshots.csv``, ``data_dictionary.csv``); it is committed so a fresh
clone has everything the continuity check needs.
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import re
import shutil
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb

from .. import __version__, sources
from .errors import UserError

log = logging.getLogger(__name__)

EDITION_FILE = "EDITION"
# plus each source's snapshots_file (snapshots.csv always; medicare_snapshots.csv and pos_snapshots.csv when loaded)
RELEASE_FILES = ("manifest.json", "SHA256SUMS", "data_dictionary.csv", "privacy_audit.csv")
RELEASE_ID_RE = re.compile(r"^e(?P<edition>\d+)-(?P<asof>\d{8})-(?P<hash>[0-9a-f]{8})$")
CHANGELOG_ENTRY_RE = re.compile(r"^- (?P<date>\d{4}-\d{2}-\d{2})(?P<rest>.*)$")
CHANGELOG_HEADING_RE = re.compile(r"^##\s.*change log", re.IGNORECASE)
REPO_URL = "https://github.com/nathansutton/healthcare-ledger"
ISSUES_URL = REPO_URL + "/issues/new?template=data-problem.yml"


# ------------------------------------------------------------------ fingerprints
def fingerprint_problems(stored: dict[tuple[int, str], str | None], recomputed: dict[tuple[int, str], str]) -> list[str]:
    """Stored (load-time) versus recomputed (version-table) fingerprints; any difference is a problem."""
    problems = []
    for key, fp in recomputed.items():
        have = stored.get(key)
        if have is None:
            problems.append(f"snapshot {key[0]} {key[1]}: no stored fingerprint; the load did not finish, so roll it back and load it again")
        elif have != fp:
            problems.append(f"snapshot {key[0]} {key[1]}: stored fingerprint {have} but the version table gives {fp}; "
                            f"the warehouse changed after load")
    return problems


# ------------------------------------------------------------------- release ids
def release_id(edition: int, last_as_of: date | str, sha256sums: bytes) -> str:
    stamp = last_as_of.strftime("%Y%m%d") if isinstance(last_as_of, date) else str(last_as_of).replace("-", "")
    return f"e{edition}-{stamp}-{hashlib.sha256(sha256sums).hexdigest()[:8]}"


def read_edition(releases: Path | None) -> int:
    if releases is None or not (releases / EDITION_FILE).exists():
        return 1
    text = (releases / EDITION_FILE).read_text().strip()
    if not text.isdigit() or int(text) < 1:
        raise UserError(f"{releases / EDITION_FILE} must hold a positive integer, found {text!r}", exit_code=2)
    return int(text)


def write_edition(releases: Path, edition: int) -> None:
    releases.mkdir(parents=True, exist_ok=True)
    (releases / EDITION_FILE).write_text(f"{edition}\n")


@dataclass(frozen=True)
class ReleaseRef:
    id: str
    path: Path
    edition: int
    last_as_of: str          # YYYYMMDD
    generated_at: str        # ISO timestamp from manifest.json

    @property
    def last_snapshot_id(self) -> int:
        return int(self.last_as_of)


def list_releases(releases: Path | None, edition: int | None = None) -> list[ReleaseRef]:
    """Releases recorded under ``releases/``, oldest first (by last as-of date, then export time)."""
    if releases is None or not releases.exists():
        return []
    out = []
    for d in releases.iterdir():
        m = RELEASE_ID_RE.match(d.name)
        if not m or not (d / "manifest.json").exists():
            continue
        if edition is not None and int(m.group("edition")) != edition:
            continue
        manifest = json.loads((d / "manifest.json").read_text())
        out.append(ReleaseRef(d.name, d, int(m.group("edition")), m.group("asof"), manifest.get("generated_at", "")))
    out.sort(key=lambda r: (r.last_as_of, r.generated_at, r.id))
    return out


def latest_release(releases: Path | None, edition: int | None = None) -> ReleaseRef | None:
    rs = list_releases(releases, edition)
    return rs[-1] if rs else None


def read_release_fingerprints(ref: ReleaseRef) -> dict[tuple[int, str], str]:
    """Fingerprints a release published: each source's streams from its ``snapshots_file``
    (snapshots.csv, medicare_snapshots.csv, pos_snapshots.csv)."""
    out: dict[tuple[int, str], str] = {}
    for name, streams in ((src.snapshots_file, [s.name for s in src.streams]) for src in sources.SOURCES if src.versioned):
        if not (ref.path / name).exists():
            continue
        with open(ref.path / name, newline="") as f:
            for row in csv.DictReader(f):
                sid = int(row["snapshot_id"])
                for stream in streams:
                    fp = row.get(f"{stream}_fingerprint") or ""
                    if fp:
                        out[(sid, stream)] = fp
    return out


def read_release_dictionary(ref: ReleaseRef) -> set[tuple[str, str, str]]:
    with open(ref.path / "data_dictionary.csv", newline="") as f:
        return {(r["table"], r["column"], r["type"]) for r in csv.DictReader(f)}


def continuity_problems(recomputed: dict[tuple[int, str], str], dictionary: set[tuple[str, str, str]],
                        prev: ReleaseRef) -> list[str]:
    """Does this warehouse still contain release ``prev`` exactly, with a compatible table shape?"""
    problems = []
    for key, fp in sorted(read_release_fingerprints(prev).items()):
        have = recomputed.get(key)
        if have is None:
            problems.append(f"snapshot {key[0]} {key[1]}: in release {prev.id} but not in the warehouse")
        elif have != fp:
            problems.append(f"snapshot {key[0]} {key[1]}: fingerprint {have} differs from release {prev.id} ({fp})")
    new_cols = {(t, c) for t, c, _ in dictionary}
    for table, column, typ in sorted(read_release_dictionary(prev)):
        if (table, column, typ) in dictionary:
            continue
        if (table, column) in new_cols:
            now = next(ty for t, c, ty in dictionary if (t, c) == (table, column))
            problems.append(f"column {table}.{column} changed type from {typ} to {now} since release {prev.id}")
        else:
            problems.append(f"column {table}.{column} ({typ}) was in release {prev.id} but is no longer exported")
    return problems


# ------------------------------------------------------------------- change log
def changelog_entries(path: Path | None) -> list[tuple[date, str]]:
    """Dated entries (``- YYYY-MM-DD ...``) under the change-log heading of METHODOLOGY, oldest first."""
    if path is None or not path.exists():
        return []
    entries: list[tuple[date, str]] = []
    in_section = False
    for line in path.read_text().splitlines():
        if line.startswith("## "):
            in_section = bool(CHANGELOG_HEADING_RE.match(line))
            continue
        m = CHANGELOG_ENTRY_RE.match(line.rstrip()) if in_section else None
        if m:
            entries.append((date.fromisoformat(m.group("date")), line.rstrip()[2:]))
    entries.sort(key=lambda e: e[0])
    return entries


def check_new_edition_allowed(prev: ReleaseRef | None, changelog: Path | None) -> None:
    """``--new-edition`` needs a change-log entry dated after the previous release's export time."""
    if prev is None:
        return
    prev_date = date.fromisoformat(prev.generated_at[:10]) if prev.generated_at else None
    entries = changelog_entries(changelog)
    if prev_date is None or any(d > prev_date for d, _ in entries):
        return
    where = changelog if changelog is not None else "docs/METHODOLOGY.md"
    raise UserError(
        f"--new-edition needs a change-log entry in {where} dated after {prev_date} (the export date of "
        f"release {prev.id}); add '- YYYY-MM-DD: what changed and why' under the change log heading first",
        exit_code=2)


# -------------------------------------------------------------- release record
def write_release_record(releases: Path, rid: str, out: Path) -> Path:
    dest = releases / rid
    dest.mkdir(parents=True, exist_ok=True)
    for name in RELEASE_FILES:
        shutil.copyfile(out / name, dest / name)
    for src in (s for s in sources.SOURCES if s.versioned):
        name = src.snapshots_file
        if src.required or (out / name).exists():
            shutil.copyfile(out / name, dest / name)
        else:
            (dest / name).unlink(missing_ok=True)
    return dest


# ------------------------------------------------------------- example history
EXAMPLE_COLUMNS = ["effective_date", "end_date", "practice_address_1", "practice_city", "practice_state"]


def example_npi(con: duckdb.DuckDBPyConnection) -> str | None:
    """Pick the README example by a fixed rule: an organization with 8 to 15 versions and the most
    distinct practice addresses (ties: more versions, then lowest NPI). Fall back to the NPI with
    the most distinct addresses, then the most versions, if the warehouse is too small for the rule."""
    base = "SELECT npi FROM provider_version GROUP BY npi"
    for where in ("HAVING bool_or(entity_type = '2') AND count(*) BETWEEN 8 AND 15", ""):
        row = con.execute(f"{base} {where} ORDER BY count(DISTINCT practice_address_1) DESC, count(*) DESC, npi LIMIT 1").fetchone()
        if row:
            return row[0]
    return None


def example_history(con: duckdb.DuckDBPyConnection, npi: str) -> list[tuple]:
    return con.execute(f"SELECT {', '.join(EXAMPLE_COLUMNS)} FROM provider_version WHERE npi = ? ORDER BY effective_date", [npi]).fetchall()


def _md_table(headers: list[str], rows: list[tuple]) -> str:
    def cell(v) -> str:
        return "" if v is None else str(v)
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(cell(v) for v in r) + " |" for r in rows]
    return "\n".join(lines)


# ---------------------------------------------------------------- export README
@dataclass
class ReadmeContext:
    release_id: str
    edition: int
    generated_at: str
    snapshots: int
    first_as_of: str
    last_as_of: str
    distinct_npis: int
    row_counts: dict[str, int]
    tables: list[tuple[str, str]]    # (name, description) of every published table, in order
    formats: list[str]
    example_npi: str | None
    example_rows: list[tuple]
    example_name: str | None
    latest_provider_count: int
    changelog: list[tuple[date, str]]
    generator: str = f"healthcare-ledger {__version__}"
    ppef_snapshots: int = 0          # Medicare PPEF quarters loaded (0: the medicare_* tables are not in this release)
    ppef_first_as_of: str = ""
    ppef_last_as_of: str = ""
    pos_snapshots: int = 0           # Provider of Services quarters loaded (0: the pos_* tables are not in this release)
    pos_first_as_of: str = ""
    pos_last_as_of: str = ""


def render_readme(ctx: ReadmeContext) -> str:
    """The README that ships inside the export directory.

    Templated from this export: the release id, counts, dates, file list, the example NPI with its
    expected output, and the change log (copied from docs/METHODOLOGY.md). Everything else is
    static text that describes the model and how to read it.
    """
    csv_too = "csv" in ctx.formats
    ex = ctx.example_npi
    n_ex = len(ctx.example_rows)
    shown = ctx.example_rows[:15]
    who = f" ({ctx.example_name})" if ctx.example_name else ""
    example_block = ""
    if ex:
        example_block = f"""## Start here: one provider's history

Every version of NPI {ex}{who} since the first snapshot. Run this in [DuckDB](https://duckdb.org)
from inside this directory; it reads only the rows it needs and answers in about a second.

```sql
-- returns {n_ex} rows
SELECT effective_date, end_date, practice_address_1, practice_city, practice_state
FROM read_parquet('parquet/provider/*/*.parquet', hive_partitioning = true)
WHERE npi = '{ex}' ORDER BY effective_date;
```

Expected output{f" (first 15 of {n_ex} rows)" if n_ex > 15 else ""}:

{_md_table(EXAMPLE_COLUMNS, shown)}

Each row is one *version*: the provider's record as CMS published it, valid from `effective_date`
(inclusive) until `end_date` (exclusive). A NULL `end_date` is the current record.
"""
    changes = ""
    if ctx.changelog:
        changes = "\n".join(f"- {d.isoformat()}{t[10:]}" for d, t in ctx.changelog)
    else:
        changes = "- No corrections recorded. Edition 1 is the first public edition."

    monthly = ctx.row_counts.get("provider_monthly")
    monthly_row = monthly_section = ""
    if monthly is not None:
        monthly_row = (f"| `parquet/provider_monthly/provider_YYYY-MM.parquet` | one file per snapshot month: every NPI in "
                       f"that month's file, one row each, in the CMS wide layout ({monthly:,} rows in all) |\n")
        monthly_section = f"""
## One file per month

`parquet/provider_monthly/` holds the same data as a monthly panel: `provider_YYYY-MM.parquet`
is the registry as CMS published it that month, one row per NPI, in the CMS file's wide layout
(taxonomy slots 1 to 15 in canonical slot order; the 200 other-identifier columns are not published). Each file
carries `snapshot_id` and `as_of_date`, so the files stack into an NPI-by-month panel, and
`effective_date`, which joins a row to its version in `parquet/provider/`.

```sql
-- returns 1 row: {ctx.latest_provider_count:,} providers
SELECT count(*) FROM read_parquet('parquet/provider_monthly/provider_{ctx.last_as_of[:7]}.parquet');
-- the whole panel
SELECT as_of_date, count(*) FROM read_parquet('parquet/provider_monthly/*.parquet') GROUP BY 1 ORDER BY 1;
```

These files are large (hundreds of MB per month) and say nothing the version tables do not;
download only the months you need.
"""
    rc = ctx.row_counts
    table_rows = "".join(f"| `parquet/{name}/` | {desc} ({rc.get(name, 0):,} rows) |\n" for name, desc in ctx.tables)
    snapshot_rows = "| `snapshots.csv` | one row per monthly NPPES snapshot: dates, source file, per-stream counts and content fingerprints |\n"
    medicare_section = pos_section = ""
    if ctx.ppef_snapshots:
        snapshot_rows += "| `medicare_snapshots.csv` | one row per PPEF quarter: extract date, files, per-file counts and content fingerprints |\n"
        medicare_section = f"""
## Medicare enrollment (PPEF)

The `medicare_*` tables are the same kind of history for CMS's quarterly *Medicare Fee-For-Service
Public Provider Enrollment* files (PECOS), {ctx.ppef_snapshots} quarterly extracts dated {ctx.ppef_first_as_of} to
{ctx.ppef_last_as_of}. Their key is the PECOS `enrollment_id`, not the NPI: one provider can hold
several enrollments, and `medicare_enrollment.npi` joins them to `provider`. `enrollee_type` says whether
an enrollment is a clinician's or an organization's. `effective_date` and `end_date` are extract dates.
The other `medicare_*` tables join to `medicare_enrollment` on `enrollment_id` and a date: take the rows
of each that hold on the date, because each table is versioned on its own.
"""
    if ctx.pos_snapshots:
        snapshot_rows += "| `pos_snapshots.csv` | one row per POS quarter: as-of date, files, per-file counts and content fingerprints |\n"
        pos_section = f"""
## Medicare-certified facilities (POS)

`facility` is the same kind of history for CMS's quarterly *Provider of Services* files,
{ctx.pos_snapshots} quarters dated {ctx.pos_first_as_of} to {ctx.pos_last_as_of} (annual at year end before 2019). Its key is
the CMS Certification Number `ccn`, the id a hospital, nursing home, home health agency, hospice, dialysis
facility or clinic bills Medicare under; `effective_date` and `end_date` are the last days of quarters.
`facility_type` names the kind of facility (`hospital`, `nursing_home`, `home_health`, ...) and `active`
whether it is still certified. Since the end of 2023 CMS has been moving provider types from its old
certification system (QIES) to a new one (iQIES), each with its own file; `facility` follows a CCN across
the move, and `pos_file` says which file a version comes from. The two files code some columns
differently (`prvdr_ctgry_cd` against `prvdr_type_id`); `data_dictionary.csv` describes every column.
"""

    csv_section = ""
    if csv_too:
        csv_section = """
`csv/` mirrors every Parquet table (except the monthly files) as gzipped CSV with identical columns and the same
`effective_year=YYYY` folders. It exists for tools that cannot read Parquet. The provider
table alone is tens of millions of rows, so do not try to append all of it in Stata; filter
first (see the Stata recipe) or load one `effective_year` folder at a time.
"""

    return f"""<!-- Generated by {ctx.generator} on {ctx.generated_at}. Numbers, the example and the
     change log come from this export; the prose is static. Do not edit by hand. -->
# NPPES provider history, release {ctx.release_id}

A monthly, field-complete history of every provider in the CMS National Plan and Provider
Enumeration System (NPPES), built from {ctx.snapshots} monthly snapshots dated {ctx.first_as_of} to
{ctx.last_as_of}. Every one of the {ctx.distinct_npis:,} NPIs that ever appeared in a monthly file is here,
with one row per *version* of its record and the dates that version was valid. The registry as CMS
published it on any snapshot date can be reconstructed exactly from these files.

Produced by [healthcare-ledger]({REPO_URL}) and published by the National Bureau of Economic Research
as the successor to its NPI/NPPES cumulative collection. Release id: **`{ctx.release_id}`** (see *How to cite*).

{example_block}
## The registry as of a date

Interval endpoints are snapshot as-of dates, so filtering on them reproduces a monthly file:

```sql
-- returns 1 row: {ctx.latest_provider_count:,} providers as of {ctx.last_as_of}
SELECT count(*) AS providers
FROM read_parquet('parquet/provider/*/*.parquet', hive_partitioning = true)
WHERE effective_date <= DATE '{ctx.last_as_of}' AND (end_date IS NULL OR end_date > DATE '{ctx.last_as_of}');
```

Use any date between {ctx.first_as_of} and {ctx.last_as_of}. Between two snapshots the result is the
earlier snapshot. `snapshots.csv` lists every snapshot date.
{monthly_section}
## Files

| Path | What it is |
|---|---|
{table_rows}{monthly_row}{snapshot_rows}| `data_dictionary.csv` | every exported column with its type, CMS header and any suppression note |
| `privacy_audit.csv` | per text column: rows scanned for formatted SSNs and EINs and nine-digit numbers, and the verdict |
| `manifest.json` | release id, counts, and the size and SHA-256 of every file |
| `SHA256SUMS` | checksums of the data files; `sha256sum -c SHA256SUMS` verifies a download |
| `CITATION.cff` | citation metadata for this release |
{csv_section}
Every table is a directory hive-partitioned by `effective_year` (the year of `effective_date`), so every
table reads the same way: `read_parquet('parquet/<table>/*/*.parquet', hive_partitioning = true)`.
DuckDB, Arrow, pandas, polars, R `arrow` and Spark read such a directory as one table.
{medicare_section}{pos_section}
## Versions

Every table starts with its key and the same three columns:

| Column | Meaning |
|---|---|
| `npi`, `enrollment_id` or `ccn` | the key, as text |
| `effective_date` | date of the first file that showed this version (inclusive) |
| `end_date` | date of the first file in which it was gone (exclusive); NULL while current |
| `close_reason` | `changed` (a new version followed) or `absent` (the key left the file); NULL while current |

A new version is created when *any* CMS column changes, including cosmetic edits and CMS-wide
reformatting (three 2008 and 2009 snapshots re-version almost every provider). A table with several rows
per key (`provider_taxonomy`, `provider_practice_location`, ...) holds every row of the version, and joins
to `provider` on `(npi, effective_date)`. The `slot` of `provider_taxonomy` is a canonical position, not the
position in the CMS file: empty slots are dropped, entries are sorted by content, and the slot CMS
flagged as primary is slot 1, which `provider.primary_taxonomy_code` repeats. A month in which
CMS only reshuffled slots therefore creates no version. Deactivated providers remain as stubs with
only `npi` and `deactivation_date` populated.

## Reading the data

**Python** (DuckDB, then pandas or polars):

```python
import duckdb
con = duckdb.connect()
history = con.execute(\"\"\"
    SELECT effective_date, end_date, practice_city, practice_state
    FROM read_parquet('parquet/provider/*/*.parquet', hive_partitioning = true)
    WHERE npi = ?\"\"\", ['{ex or "1000000001"}']).df()          # .pl() for polars, .arrow() for Arrow
print(history)
```

**R** (`arrow` and `dplyr`; `duckdb` works the same way):

```r
library(arrow); library(dplyr)
provider <- open_dataset("parquet/provider")
asof <- as.Date("{ctx.last_as_of}")
provider |> filter(effective_date <= asof, is.na(end_date) | end_date > asof) |>
  select(npi, entity_type, org_name, last_name, practice_state) |> collect()
```

**Stata** (16 or later, with `pip install duckdb` in Stata's Python): filter in DuckDB, then
import only the rows you need.

```stata
python:
import duckdb
duckdb.connect().execute(\"\"\"
    COPY (SELECT npi, entity_type, org_name, last_name, first_name, practice_state, primary_taxonomy_code
          FROM read_parquet('parquet/provider/*/*.parquet', hive_partitioning = true)
          WHERE effective_date <= DATE '{ctx.last_as_of}' AND (end_date IS NULL OR end_date > DATE '{ctx.last_as_of}'))
    TO 'registry_{ctx.last_as_of.replace("-", "")}.csv' (HEADER)\"\"\")
end
import delimited registry_{ctx.last_as_of.replace("-", "")}.csv, clear stringcols(_all)
```

Without Python, `gunzip` one `csv/provider/effective_year=YYYY/*.csv.gz` at a time and
`import delimited` it; all column names are 20 characters or fewer. Read NPIs and codes as strings.

## What is not here

* **No tax ids or SSNs.** CMS withholds the EIN and SSN from their own fields but providers type them
  into free-form fields. The rules fail closed: the 50 "Other Provider Identifier" slots are not
  published at all; a license number is removed when it contains a formatted SSN or EIN, or is a
  nine-digit number that is not a known license format of its state; a credential with a nine-digit
  number in it is removed; and in names, addresses and other text every formatted SSN or EIN and every
  nine-digit number is replaced by `[REDACTED]`. All of this happens before anything is hashed, the
  exporter refuses to write if any check fails, and `privacy_audit.csv` records an independent scan of
  the published files. `data_dictionary.csv` names every column a rule touches. If you find a tax id or
  SSN anyway, report it (below) and do not redistribute it.
* **Weekly files.** The model consumes CMS's monthly full replacements only, so a change is dated to the
  first monthly snapshot that showed it. Before April 2011 the archive has two to five files a year
  (and May 2013 is missing), so a version's start date is then an upper bound on when the change happened.
* **Files before November 2007** do not exist in any archive we know of.
* **No standardization.** Values are exactly as CMS published them, trimmed, with CMS placeholder tokens
  (`<UNAVAIL>` and the like) stored as NULL. No case folding, address or name cleaning, or code lookups.

## How to cite

Cite the release id so a reader can get exactly these rows. `CITATION.cff` in this directory carries
the metadata (dataset type, release `{ctx.release_id}`, CC0 1.0). Please also cite CMS NPPES as the source.

> Sutton, Nathan. *NPPES over time: monthly history of the CMS NPPES provider registry*, release
> {ctx.release_id}. Distributed by the National Bureau of Economic Research. Underlying data: Centers
> for Medicare & Medicaid Services, NPPES Data Dissemination.

## Releases and editions

A release id is `e<edition>-<last as-of date>-<checksum>`. Within an edition the versioning rules never
change, so **a later release of the same edition contains every earlier one exactly**: the exporter
refuses to write a release unless the content fingerprint of every earlier file (`*_fingerprint` in
`snapshots.csv` and the other `*_snapshots.csv`) is unchanged. A correction
that changes any earlier snapshot starts a new edition and is recorded below. This release is edition {ctx.edition}.

Change log:

{changes}

## Reporting a problem

Open an issue with the *Data problem* form at {ISSUES_URL}
(release id, NPI, snapshot date, what you expected and what you got). Documentation for the build,
including the full methodology, is in the repository: {REPO_URL}.
"""


def render_citation(ctx: ReadmeContext, released: date) -> str:
    medicare = (f", and {ctx.ppef_snapshots} quarterly Medicare fee-for-service enrollment extracts (PPEF, "
                f"{ctx.ppef_first_as_of} to {ctx.ppef_last_as_of})" if ctx.ppef_snapshots else "")
    medicare += (f", and {ctx.pos_snapshots} quarterly Provider of Services files of Medicare-certified facilities (POS, "
                 f"{ctx.pos_first_as_of} to {ctx.pos_last_as_of})" if ctx.pos_snapshots else "")
    return f"""cff-version: 1.2.0
message: "If you use this data, please cite this release by its id, and cite CMS NPPES as the underlying source."
type: dataset
title: "NPPES over time: monthly history of the CMS NPPES provider registry, release {ctx.release_id}"
version: "{ctx.release_id}"
date-released: {released.isoformat()}
license: CC0-1.0
repository-code: {REPO_URL}
abstract: >-
  Slowly Changing Dimension Type 2 panel of every provider in the CMS National Plan and Provider
  Enumeration System, built from {ctx.snapshots} monthly snapshots ({ctx.first_as_of} to {ctx.last_as_of}):
  {ctx.distinct_npis:,} NPIs and {ctx.row_counts.get('provider', 0):,} provider versions, plus taxonomy,
  practice-location, other-name and endpoint versions{medicare}. Tax ids and SSNs removed.
  Edition {ctx.edition}. Generated by {ctx.generator}.
authors:
  - family-names: Sutton
    given-names: Nathan
    email: nasutton@gmail.com
identifiers:
  - type: other
    value: "{ctx.release_id}"
    description: "NPPES over time release id: edition, last as-of date, and the first 8 hex digits of sha256(SHA256SUMS)"
keywords:
  - NPPES
  - NPI
  - CMS
  - healthcare providers
  - panel data
  - slowly changing dimension
references:
  - type: software
    title: "healthcare-ledger"
    version: "{__version__}"
    repository-code: {REPO_URL}
    authors:
      - family-names: Sutton
        given-names: Nathan
  - type: data
    title: "NPPES Data Dissemination"
    authors:
      - name: "Centers for Medicare & Medicaid Services"
    url: https://download.cms.gov/nppes/NPI_Files.html
  - type: data
    title: "NPI/NPPES Cumulative Collection (preliminary)"
    authors:
      - name: "National Bureau of Economic Research"
    url: https://www.nber.org/research/data/npinppes-cumulative-collection-preliminary
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
