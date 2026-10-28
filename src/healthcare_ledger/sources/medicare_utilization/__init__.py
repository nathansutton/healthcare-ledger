"""CMS's Medicare Physician & Other Practitioners by Provider and Service files, a reference source: one file per
calendar year, loaded wholesale.

CMS publishes, for each year from 2013, every rendering NPI's Medicare Part B fee-for-service services by HCPCS code
and place of service (facility or office), with beneficiaries, services and average submitted, allowed, paid and
standardized amounts; cells under 11 beneficiaries are left out. It is the public source that says where a clinician
bills Medicare: the share of a physician's office visits billed in a facility setting is the service-location measure
of physician-hospital integration that Luo et al. (2024) compare their tax-id measure with, and the dollars weigh a
physician by Medicare volume. ``fetch`` downloads the files ``releases/medicare_utilization_sources.csv`` pins;
``load`` rebuilds ``medicare_utilization.duckdb`` from them, one table ``medicare_utilization_service``. The files are
annual totals, not snapshots of a registry, so the source is not versioned and not exported.
"""

from __future__ import annotations

import duckdb

from ..base import Source, recorded_download
from . import fetch
from .load import COLUMNS, DATABASE, TABLE, load

Con = duckdb.DuckDBPyConnection


def loaded(con: Con) -> bool:
    return bool(con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name = ?", [TABLE]).fetchone()[0])


def reset(con: Con) -> list[str]:
    if not loaded(con):
        return []
    con.execute(f"DROP TABLE {TABLE}")
    return [TABLE]


def check_problems(con: Con) -> list[str]:
    """The table has the columns ``COLUMNS`` names, in order, and rows for every year it holds."""
    fix = "reload with `healthcare-ledger load --source-name medicare_utilization`"
    if not loaded(con):
        return [f"{TABLE}: missing; {fix}"]
    cols = [r[0] for r in con.execute(f"DESCRIBE {TABLE}").fetchall()]
    want = ["year", *COLUMNS]
    if cols != want:
        return [f"{TABLE}: columns {cols} differ from {want}; {fix}"]
    if not con.execute(f"SELECT count(*) FROM (SELECT 1 FROM {TABLE} LIMIT 1)").fetchone()[0]:
        return [f"{TABLE}: no rows; {fix}"]
    bad = con.execute(f"SELECT count(*) FROM {TABLE} WHERE NOT regexp_matches(npi, '^[0-9]{{10}}$')").fetchone()[0]
    return [f"{TABLE}: {bad} row(s) whose npi is not ten digits"] if bad else []


def status(con: Con) -> dict:
    return {"tables": {TABLE: con.execute(f"SELECT count(*), min(year), max(year) FROM {TABLE}").fetchone()}}


SOURCE = Source(
    name="medicare_utilization",
    database=DATABASE,
    title="Medicare utilization",
    kind="reference",
    required=False,
    sources_file=fetch.SOURCES_FILE,
    default_source_dir="data/mup",
    loaded=loaded,
    reset=reset,
    check_problems=check_problems,
    status=status,
    download=recorded_download("Medicare utilization", fetch.download),
    load=load,
)
