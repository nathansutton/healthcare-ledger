"""What NPPES publishes (``core.published``): the provider tables, and the optional monthly files.

``provider`` is the main file's record without its 15 taxonomy slots, which ``provider_taxonomy`` holds long (one
row per version and slot), with the entity type decoded and the primary taxonomy alongside; the supplemental files
are ``provider_practice_location``, ``provider_other_name`` and ``provider_endpoint``. The tax-id columns, NULL
by core.privacy rule 1, are left out of all of them.

The monthly files are the same provider data the other way around: one file per snapshot holding
every NPI in that month's CMS file, wide, in the CMS column layout without the 200
other-identifier columns, which the loader does not read, and the two tax-id columns (core.privacy rule 1).
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
from ...core.published import INTERVAL, Table, interval_columns
from ...core.privacy import SUPPRESSED_COLUMNS, free_text_columns
from .schema import ENDPOINT, OTHER_NAME, PRACTICE_LOCATION, PROVIDER, STREAMS, Stream

log = logging.getLogger(__name__)

INTERVAL_SELECT = ["npi", *INTERVAL]
REDACT_DESC = "Formatted SSNs and EINs and nine-digit numbers in the text are replaced by [REDACTED] (METHODOLOGY 2a)"
SCRUB_DESC = {
    "license_number": ("NULL when it contains a formatted SSN, is a formatted EIN, or is a nine-digit number that is not a "
                       "known license format of its state or recurs under another state (METHODOLOGY 2a)"),
    "credential": "NULL when it contains a formatted SSN or EIN or a nine-digit number (METHODOLOGY 2a)",
    "other_credential": "NULL when it contains a formatted SSN or EIN or a nine-digit number (METHODOLOGY 2a)",
    "ao_credential": "NULL when it contains a formatted SSN or EIN or a nine-digit number (METHODOLOGY 2a)",
    **{c: REDACT_DESC for c in free_text_columns(PROVIDER)},
}

TAXONOMY_FIELDS = ["taxonomy_code", "license_number", "license_state", "primary_taxonomy", "taxonomy_group"]
# the provider's scalar columns: not the 15 taxonomy slots, which provider_taxonomy holds long, and not the tax ids,
# which are always NULL (core.privacy rule 1)
SCALAR_PROVIDER = [c for c in PROVIDER.columns
                   if not any(c.name.startswith(f + "_") for f in TAXONOMY_FIELDS) and c.name not in SUPPRESSED_COLUMNS]


def _interval(key_desc: str = "National Provider Identifier (10 digits, text)"):
    return interval_columns("npi", "NPI", key_desc, "As-of date", "the NPI left the file")


def _provider() -> Table:
    cols = []
    for c in SCALAR_PROVIDER:
        if c.name == "entity_type":
            cols.append(("CASE entity_type WHEN '1' THEN 'individual' WHEN '2' THEN 'organization' END AS entity_type",
                         (c.name, c.cms_header, "'individual' (CMS code 1) or 'organization' (2); NULL on a deactivated "
                                                "NPI's record, which CMS blanks")))
            cols.append(("taxonomy_code_1 AS primary_taxonomy_code",
                         ("primary_taxonomy_code", "Healthcare Provider Taxonomy Code_N",
                          "The taxonomy flagged primary, or when none is flagged the first in canonical order: slot 1 "
                          "of provider_taxonomy (METHODOLOGY 3)")))
        else:
            cols.append((c.name, (c.name, c.cms_header, SCRUB_DESC.get(c.name, ""))))
    return Table(
        "provider", ("npi",),
        "one row per NPI per version: the NPPES record of a clinician or an organization",
        f"SELECT {', '.join(INTERVAL_SELECT)}, {', '.join(e for e, _ in cols)} FROM provider_version",
        _interval() + tuple(c for _, c in cols))


def _taxonomy() -> Table:
    parts = []
    for i in range(1, 16):
        fields = ", ".join(f"{f}_{i} AS {f}" for f in TAXONOMY_FIELDS)
        any_set = " OR ".join(f"{f}_{i} IS NOT NULL" for f in TAXONOMY_FIELDS)
        parts.append(f"SELECT {', '.join(INTERVAL_SELECT)}, {i} AS slot, {fields} FROM provider_version WHERE {any_set}")
    headers = {"taxonomy_code": "Healthcare Provider Taxonomy Code_N", "license_number": "Provider License Number_N",
               "license_state": "Provider License Number State Code_N",
               "primary_taxonomy": "Healthcare Provider Primary Taxonomy Switch_N",
               "taxonomy_group": "Healthcare Provider Taxonomy Group_N"}
    slot_desc = "Slot N of 15 unpivoted in canonical order (primary taxonomy first); slots where all five fields are blank are dropped"
    return Table(
        "provider_taxonomy", ("npi", "slot"),
        "one row per NPI per version per taxonomy: code, license, license state, primary switch, group",
        " UNION ALL ".join(parts),
        _interval() + (("slot", "", "Position 1..N in canonical order (empty slots dropped, entries sorted by content, "
                                    "primary taxonomy first; METHODOLOGY 3), not the position in the CMS file"),)
        + tuple((f, headers[f], slot_desc + ("; " + SCRUB_DESC[f] if f in SCRUB_DESC else "")) for f in TAXONOMY_FIELDS))


def _supplemental(s: Stream, name: str, description: str) -> Table:
    redacted = set(free_text_columns(s))
    return Table(
        name, ("npi",), description,
        f"SELECT {', '.join(INTERVAL_SELECT)}, {', '.join(s.data_columns)} FROM {s.name}_version",
        _interval() + tuple((c.name, c.cms_header, REDACT_DESC if c.name in redacted else "") for c in s.columns))


def tables() -> tuple[Table, ...]:
    """What NPPES publishes. A multi-row table has one row per row of the CMS file, so a version is all the rows
    of an NPI with the same effective_date."""
    return (
        _provider(),
        _taxonomy(),
        _supplemental(PRACTICE_LOCATION, "provider_practice_location",
                      "one row per NPI per version per secondary practice location, from July 2018"),
        _supplemental(OTHER_NAME, "provider_other_name",
                      "one row per organization NPI per version per other name, from July 2018"),
        _supplemental(ENDPOINT, "provider_endpoint",
                      "one row per NPI per version per electronic endpoint (Direct address, FHIR server), from March 2019"),
    )


MONTHLY = "provider_monthly"
MONTHLY_META = ["snapshot_id", "as_of_date", "npi", "effective_date"]
MONTHLY_DESC = {
    "snapshot_id": "Snapshot id (YYYYMMDD of as-of date) this file is a copy of; one value per file",
    "as_of_date": "As-of date of the snapshot; one value per file",
    "npi": "National Provider Identifier (10 digits, text)",
    "effective_date": "effective_date of the provider version in force on as_of_date (joins to provider)",
}


MONTHLY_COLUMNS = [c for c in PROVIDER.columns if c.name not in SUPPRESSED_COLUMNS]


def _monthly_sql(snapshot_id: int, as_of: str) -> str:
    """Every NPI in snapshot ``snapshot_id``, wide, one row each (the version in force on ``as_of``)."""
    cols = ", ".join(c.name for c in MONTHLY_COLUMNS)
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


def monthly_dictionary_rows() -> list[tuple[str, str, str, str, str]]:
    """Dictionary rows of the optional monthly files (same CMS columns as the raw file, wide)."""
    rows = [(MONTHLY, m, {"snapshot_id": "INTEGER", "as_of_date": "DATE", "effective_date": "DATE"}.get(m, "VARCHAR"),
             "", MONTHLY_DESC[m]) for m in MONTHLY_META]
    for c in MONTHLY_COLUMNS:
        field, _, n = c.name.rpartition("_")
        if n.isdigit() and field in TAXONOMY_FIELDS:
            desc = ("Slot N in canonical order, as in provider_taxonomy"
                    + ("; " + SCRUB_DESC[field] if field in SCRUB_DESC else ""))
        else:
            desc = SCRUB_DESC.get(c.name, "")
        rows.append((MONTHLY, c.name, {"date": "DATE", "int": "INTEGER"}.get(c.kind, "VARCHAR"), c.cms_header, desc))
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
