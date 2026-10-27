"""What NPPES contributes to a release: the version tables as published, the optional monthly files,
and their rows of ``data_dictionary.csv``.

``provider`` is published without its 15 taxonomy slots, which ``provider_taxonomy`` holds long (one
row per version and slot); the supplemental streams are published as loaded. Big tables are
partitioned by ``effective_year``.

The monthly files are the same provider data the other way around: one file per snapshot holding
every NPI in that month's CMS file, wide, in the CMS column layout without the 200
other-identifier columns, which the loader does not read (core.privacy rule 1).
They are rebuilt from the version table by the as-of lookup the release fingerprints use
(core.versioning.interval_fingerprint), so a month here is exactly the month the release vouches for.
Opt-in because they are large: about 0.3 to 0.8 GB per month, 100 GB for the full history.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

import duckdb

from ...core.errors import UserError
from ...core.privacy import SUPPRESSED_COLUMNS, free_text_columns
from .schema import ENDPOINT, OTHER_NAME, PRACTICE_LOCATION, PROVIDER, STREAMS, Stream

log = logging.getLogger(__name__)

META_EXPORT = ["npi", "effective_date", "end_date", "is_current", "first_snapshot_id",
               "last_snapshot_id", "close_reason", "version_hash"]
META_DESC = {
    "npi": "National Provider Identifier (10 digits, text)",
    "effective_date": "As-of date of the first snapshot in which this version was observed (inclusive)",
    "end_date": "As-of date of the first snapshot in which this version was no longer observed (exclusive); NULL while current",
    "is_current": "True if this is the latest version and the NPI was present in the latest snapshot",
    "first_snapshot_id": "Snapshot id (YYYYMMDD of as-of date) that first showed this version",
    "last_snapshot_id": "Snapshot id that last showed this version",
    "close_reason": "'changed' if superseded by a new version, 'absent' if the NPI disappeared from the file, NULL while current",
    "version_hash": "MD5 fingerprint of all data columns of this version (set-level for multi-row streams)",
    "row_seq": "Row number within a multi-row version, ordered by row hash (no meaning beyond uniqueness)",
    "slot": "Canonical position 1..N in the repeating group (empty slots dropped, entries sorted by content, primary taxonomy first; METHODOLOGY 3), not the position in the CMS file",
    "effective_year": "Partition key: year(effective_date)",
}
SUPPRESSED_DESC = "Suppressed: never published by this project, whatever the CMS file contains (see METHODOLOGY 2a)"
REDACT_DESC = "Formatted SSNs and EINs and nine-digit numbers in the text are replaced by [REDACTED] (METHODOLOGY 2a)"
SCRUB_DESC = {
    "license_number": ("NULL when it contains a formatted SSN, is a formatted EIN, or is a nine-digit number that is not a "
                       "known license format of its state or recurs under another state (METHODOLOGY 2a)"),
    "credential": "NULL when it contains a formatted SSN or EIN or a nine-digit number (METHODOLOGY 2a)",
    "other_credential": "NULL when it contains a formatted SSN or EIN or a nine-digit number (METHODOLOGY 2a)",
    "ao_credential": "NULL when it contains a formatted SSN or EIN or a nine-digit number (METHODOLOGY 2a)",
    **{c: REDACT_DESC for c in free_text_columns(PROVIDER)},
}

SCALAR_PROVIDER = [c for c in PROVIDER.columns
                   if not any(c.name.startswith(p) for p in ("taxonomy_code_", "license_number_", "license_state_",
                                                              "primary_taxonomy_", "taxonomy_group_"))]
TAXONOMY_FIELDS = ["taxonomy_code", "license_number", "license_state", "primary_taxonomy", "taxonomy_group"]


def _taxonomy_sql() -> str:
    parts = []
    for i in range(1, 16):
        cols = ", ".join(f"{f}_{i} AS {f}" for f in TAXONOMY_FIELDS)
        any_set = " OR ".join(f"{f}_{i} IS NOT NULL" for f in TAXONOMY_FIELDS)
        parts.append(f"SELECT {', '.join(META_EXPORT)}, {i} AS slot, {cols} FROM provider_version WHERE {any_set}")
    return " UNION ALL ".join(parts)


def _supp_sql(s: Stream) -> str:
    return f"SELECT {', '.join(META_EXPORT)}, row_seq, {', '.join(s.data_columns)} FROM {s.name}_version"


def tables() -> dict[str, tuple[str, bool]]:
    """name -> (select sql, partition by year)."""
    core = ", ".join(c.name for c in SCALAR_PROVIDER)
    return {
        "provider": (f"SELECT {', '.join(META_EXPORT)}, {core} FROM provider_version", True),
        "provider_taxonomy": (_taxonomy_sql(), True),
        "practice_location": (_supp_sql(PRACTICE_LOCATION), False),
        "other_name": (_supp_sql(OTHER_NAME), False),
        "endpoint": (_supp_sql(ENDPOINT), False),
    }


MONTHLY = "provider_monthly"
MONTHLY_META = ["snapshot_id", "as_of_date", "npi", "effective_date"]
MONTHLY_DESC = {
    "snapshot_id": "Snapshot id (YYYYMMDD of as-of date) this file is a copy of; one value per file",
    "as_of_date": "As-of date of the snapshot; one value per file",
    "npi": META_DESC["npi"],
    "effective_date": "effective_date of the provider version in force on as_of_date (joins to provider)",
}


def _monthly_sql(snapshot_id: int, as_of: str) -> str:
    """Every NPI in snapshot ``snapshot_id``, wide, one row each (the version in force on ``as_of``)."""
    cols = ", ".join(PROVIDER.data_columns)
    return (f"SELECT {snapshot_id}::INTEGER AS snapshot_id, DATE '{as_of}' AS as_of_date, npi, effective_date, {cols} "
            f"FROM provider_version WHERE effective_date <= DATE '{as_of}' "
            f"AND (end_date IS NULL OR end_date > DATE '{as_of}') ORDER BY npi")


def _write_monthly(con: duckdb.DuckDBPyConnection, dest: Path) -> int:
    """One Parquet file per snapshot, named by its month; returns the total row count."""
    snaps = con.execute("SELECT snapshot_id, as_of_date FROM snapshot ORDER BY snapshot_id").fetchall()
    months = [d.strftime("%Y-%m") for _, d in snaps]
    dupes = sorted({m for m in months if months.count(m) > 1})
    if dupes:
        raise UserError(f"cannot write one file per month: more than one snapshot in {', '.join(dupes)}")
    shutil.rmtree(dest, ignore_errors=True)  # a month dropped by a rollback must not survive
    dest.mkdir(parents=True)
    total = 0
    for (sid, as_of), month in zip(snaps, months):
        log.info("exporting %s %s", MONTHLY, month)
        f = dest / f"provider_{month}.parquet"
        con.execute(f"COPY ({_monthly_sql(sid, as_of.isoformat())}) TO '{f}' (FORMAT PARQUET, COMPRESSION ZSTD)")
        total += con.execute(f"SELECT count(*) FROM read_parquet('{f}')").fetchone()[0]
    return total


def _dd_type(kind: str) -> str:
    return {"date": "DATE", "int": "INTEGER"}.get(kind, "VARCHAR")


def data_dictionary_rows() -> list[tuple[str, str, str, str, str]]:
    """(table, column, type, cms_header, description) for every exported column."""
    rows = []

    def meta(table: str, extra: list[str] = ()) -> None:
        for m in META_EXPORT + list(extra):
            t = {"effective_date": "DATE", "end_date": "DATE", "is_current": "BOOLEAN",
                 "first_snapshot_id": "INTEGER", "last_snapshot_id": "INTEGER", "row_seq": "INTEGER", "slot": "INTEGER"}.get(m, "VARCHAR")
            rows.append((table, m, t, "", META_DESC[m]))

    meta("provider")
    for c in SCALAR_PROVIDER:
        desc = SUPPRESSED_DESC if c.name in SUPPRESSED_COLUMNS else SCRUB_DESC.get(c.name, "")
        rows.append(("provider", c.name, _dd_type(c.kind), c.cms_header, desc))
    meta("provider_taxonomy", ["slot"])
    for f in TAXONOMY_FIELDS:
        hdr = {"taxonomy_code": "Healthcare Provider Taxonomy Code_N", "license_number": "Provider License Number_N",
               "license_state": "Provider License Number State Code_N",
               "primary_taxonomy": "Healthcare Provider Primary Taxonomy Switch_N",
               "taxonomy_group": "Healthcare Provider Taxonomy Group_N"}[f]
        desc = "Slot N of 15 unpivoted in canonical order (primary taxonomy first); slots where all five fields are blank are dropped"
        rows.append(("provider_taxonomy", f, "VARCHAR", hdr, desc + ("; " + SCRUB_DESC[f] if f in SCRUB_DESC else "")))
    for s in (PRACTICE_LOCATION, OTHER_NAME, ENDPOINT):
        meta(s.name, ["row_seq"])
        redacted = set(free_text_columns(s))
        for c in s.columns:
            rows.append((s.name, c.name, _dd_type(c.kind), c.cms_header, REDACT_DESC if c.name in redacted else ""))
    return rows


def monthly_dictionary_rows() -> list[tuple[str, str, str, str, str]]:
    """Dictionary rows of the optional monthly files (same CMS columns as the raw file, wide)."""
    rows = [(MONTHLY, m, {"snapshot_id": "INTEGER", "as_of_date": "DATE", "effective_date": "DATE"}.get(m, "VARCHAR"),
             "", MONTHLY_DESC[m]) for m in MONTHLY_META]
    for c in PROVIDER.columns:
        field, _, n = c.name.rpartition("_")
        if n.isdigit() and field in TAXONOMY_FIELDS:
            desc = ("Slot N in canonical order, as in provider_taxonomy"
                    + ("; " + SCRUB_DESC[field] if field in SCRUB_DESC else ""))
        else:
            desc = SUPPRESSED_DESC if c.name in SUPPRESSED_COLUMNS else SCRUB_DESC.get(c.name, "")
        rows.append((MONTHLY, c.name, _dd_type(c.kind), c.cms_header, desc))
    return rows


SNAPSHOTS_FILE = "snapshots.csv"


def snapshots_sql() -> str:
    """One row per snapshot with dates, source file, per-stream counts and fingerprints, for snapshots.csv."""
    aliases = {"provider": "p", "practice_location": "pl", "other_name": "onm", "endpoint": "ep"}
    fp_cols = ", ".join(f"{aliases[s.name]}.content_fingerprint AS {s.name}_fingerprint" for s in STREAMS)
    return f"""
        SELECT s.snapshot_id, s.as_of_date, s.zip_file, s.zip_date, s.main_csv, s.main_csv_bytes, s.main_csv_columns,
               array_to_string(s.streams_present, ',') AS streams_present,
               p.n_rows AS provider_rows, p.n_new AS provider_new, p.n_changed AS provider_changed,
               p.n_unchanged AS provider_unchanged, p.n_absent AS provider_absent, p.n_bad_dates AS provider_bad_dates,
               pl.n_rows AS practice_location_rows, onm.n_rows AS other_name_rows, ep.n_rows AS endpoint_rows,
               {fp_cols}
        FROM snapshot s
        LEFT JOIN snapshot_stream p ON p.snapshot_id = s.snapshot_id AND p.stream = 'provider'
        LEFT JOIN snapshot_stream pl ON pl.snapshot_id = s.snapshot_id AND pl.stream = 'practice_location'
        LEFT JOIN snapshot_stream onm ON onm.snapshot_id = s.snapshot_id AND onm.stream = 'other_name'
        LEFT JOIN snapshot_stream ep ON ep.snapshot_id = s.snapshot_id AND ep.stream = 'endpoint'
        ORDER BY s.snapshot_id"""
