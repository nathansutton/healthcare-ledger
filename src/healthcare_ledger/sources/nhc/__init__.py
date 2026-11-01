"""Nursing Home Compare ownership archives, a reference source: the owners CMS listed for every nursing home, one
monthly snapshot per quarter from 2014 to 2022, loaded wholesale.

CMS's Nursing Home Compare (now Care Compare) published, monthly, every certified nursing home's owners, officers and
managers from PECOS: the role (5% or greater direct or indirect ownership, operational or managerial control,
officer, director), the owner's type and name, and from 2016 its share and association date. The monthly archive
zips are no longer served to scripts, but the Internet Archive holds captures of them; ``fetch`` downloads the
``Ownership_Download.csv`` of the capture ``releases/nhc_sources.csv`` pins for one month per quarter. This is the
public owner record of nursing homes before CMS's All Owners files began in September 2022. ``load`` rebuilds
``nhc.duckdb`` from them: ``nhc_ownership`` (``load.py``).
"""

from __future__ import annotations

import duckdb

from ..base import Source, recorded_download
from . import fetch
from .load import DATABASE, TABLES, load

Con = duckdb.DuckDBPyConnection


def _tables(con: Con) -> set[str]:
    return {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}


def loaded(con: Con) -> bool:
    return "nhc_ownership" in _tables(con)


def reset(con: Con) -> list[str]:
    dropped = [t for t in TABLES if t in _tables(con)]
    for t in dropped:
        con.execute(f"DROP TABLE {t}")
    return dropped


def check_problems(con: Con) -> list[str]:
    fix = "reload with `healthcare-ledger load --source-name nhc`"
    if not loaded(con):
        return [f"nhc_ownership: missing; {fix}"]
    problems = []
    bad = con.execute("SELECT count(*) FROM nhc_ownership WHERE ccn IS NULL OR length(ccn) <> 6 OR as_of_date IS NULL").fetchone()[0]
    if bad:
        problems.append(f"nhc_ownership: {bad} rows without a six-character CCN or a date; {fix}")
    return problems


def status(con: Con) -> dict:
    return {"months": con.execute("SELECT count(DISTINCT as_of_date), min(as_of_date)::VARCHAR, max(as_of_date)::VARCHAR FROM nhc_ownership").fetchone(),
            "rows": con.execute("SELECT count(*) FROM nhc_ownership").fetchone()[0]}


SOURCE = Source(
    name="nhc",
    database=DATABASE,
    title="Nursing Home Compare",
    kind="reference",
    required=False,
    sources_file=fetch.SOURCES_FILE,
    default_source_dir="data/nhc",
    loaded=loaded,
    reset=reset,
    check_problems=check_problems,
    status=status,
    download=recorded_download("Nursing Home Compare", fetch.download),
    load=load,
)
