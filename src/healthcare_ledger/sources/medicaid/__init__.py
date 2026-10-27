"""HHS's Medicaid Provider Spending extract, a reference source: one release, loaded wholesale.

HHS publishes on opendata.hhs.gov a provider-level extract of T-MSIS outpatient and professional claims: Medicaid
paid, patients and claim lines by billing NPI, servicing NPI, HCPCS code and month, 2018 to 2024, with cells under
12 patients or claim lines suppressed and no state. It is the one public source that links a billing organization
to a servicing clinician with dollars. ``fetch`` downloads the zip ``releases/medicaid_sources.csv`` pins; ``load``
rebuilds ``medicaid.duckdb`` from it, one table ``medicaid_spending``. The release is not versioned and not
exported; research/provider-networks reads it for its section 4b.
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
    """The table has the columns ``COLUMNS`` names, in order, and rows."""
    fix = "reload with `healthcare-ledger load --source-name medicaid`"
    if not loaded(con):
        return [f"{TABLE}: missing; {fix}"]
    cols = [r[0] for r in con.execute(f"DESCRIBE {TABLE}").fetchall()]
    if cols != list(COLUMNS):
        return [f"{TABLE}: columns {cols} differ from {list(COLUMNS)}; {fix}"]
    if not con.execute(f"SELECT count(*) FROM (SELECT 1 FROM {TABLE} LIMIT 1)").fetchone()[0]:
        return [f"{TABLE}: no rows; {fix}"]
    return []


def status(con: Con) -> dict:
    return {"tables": {TABLE: con.execute(f"SELECT count(*), min(month)::VARCHAR, max(month)::VARCHAR FROM {TABLE}").fetchone()}}


SOURCE = Source(
    name="medicaid",
    database=DATABASE,
    title="Medicaid",
    kind="reference",
    required=False,
    sources_file=fetch.SOURCES_FILE,
    default_source_dir="data/medicaid",
    loaded=loaded,
    reset=reset,
    check_problems=check_problems,
    status=status,
    download=recorded_download("HHS Medicaid spending", fetch.download),
    load=load,
)
