"""Medicare cost reports (HCRIS), a reference source: the chain and home-office identification every hospital,
nursing home, home health agency and hospice files each fiscal year, loaded wholesale.

CMS publishes the Healthcare Cost Report Information System files by form and fiscal year: a report table (one
row per cost report: the provider's CCN, fiscal period and status) and the worksheets' alphanumeric cells. Worksheet
S-2 (S-1 for hospices) asks whether the provider is a member of a chain organization and, if so, the chain home
office's PECOS-assigned home office number, name and address. That is a dated chain membership for every Medicare
facility back to fiscal 2010 (hospitals), 2011 (nursing homes), 2015 (hospices) and 2020 (home health, on the
1728-20 form), years before CMS's All Owners files. ``fetch`` downloads the zips ``releases/hcris_sources.csv`` pins;
``load`` rebuilds ``hcris.duckdb`` from them: ``hcris_report`` and ``hcris_home_office`` (``load.py``).
"""

from __future__ import annotations

import duckdb

from ..base import Source, recorded_download
from . import fetch
from .load import DATABASE, FORMS, TABLES, load

Con = duckdb.DuckDBPyConnection


def _tables(con: Con) -> set[str]:
    return {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}


def loaded(con: Con) -> bool:
    return "hcris_report" in _tables(con)


def reset(con: Con) -> list[str]:
    dropped = [t for t in TABLES if t in _tables(con)]
    for t in dropped:
        con.execute(f"DROP TABLE {t}")
    return dropped


def check_problems(con: Con) -> list[str]:
    fix = "reload with `healthcare-ledger load --source-name hcris`"
    have = _tables(con)
    problems = [f"{t}: missing; {fix}" for t in TABLES if t not in have]
    if problems:
        return problems
    forms = {r[0] for r in con.execute("SELECT DISTINCT form FROM hcris_report").fetchall()}
    missing = [f for f in FORMS if f not in forms]
    if missing:
        problems.append(f"hcris_report: no reports for {missing}; {fix}")
    bad = con.execute("SELECT count(*) FROM hcris_report WHERE ccn IS NULL OR length(ccn) <> 6").fetchone()[0]
    if bad:
        problems.append(f"hcris_report: {bad} reports without a six-character CCN; {fix}")
    return problems


def status(con: Con) -> dict:
    return {"reports": {r[0]: (r[1], r[2], r[3]) for r in con.execute(
        "SELECT form, count(*), min(fiscal_year), max(fiscal_year) FROM hcris_report GROUP BY 1 ORDER BY 1").fetchall()},
        "home_offices": con.execute("SELECT count(*), count(DISTINCT home_office_id) FROM hcris_home_office WHERE chain").fetchone()}


SOURCE = Source(
    name="hcris",
    database=DATABASE,
    title="HCRIS",
    kind="reference",
    required=False,
    sources_file=fetch.SOURCES_FILE,
    default_source_dir="data/hcris",
    loaded=loaded,
    reset=reset,
    check_problems=check_problems,
    status=status,
    download=recorded_download("HCRIS", fetch.download),
    load=load,
)
