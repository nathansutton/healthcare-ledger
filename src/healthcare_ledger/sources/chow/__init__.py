"""CMS's Change of Ownership (CHOW) files for hospitals and skilled nursing facilities, a reference source: every
Medicare change of ownership, acquisition, merger or consolidation since 2016, with buyer and seller, loaded
wholesale.

data.cms.gov publishes, quarterly and cumulative from 1 January 2016, one row per CHOW transaction of a hospital or
SNF enrollment: the buyer's and seller's enrollment id, CCN, PECOS associate (PAC) id, NPI and names, the type of
change and its effective date; and a companion file of the owners of each party at the time. CMS keeps CHOW files
for these two facility types only. ``fetch`` downloads the files ``releases/chow_sources.csv`` pins; ``load`` rebuilds
``chow.duckdb`` from them: ``chow_event`` and ``chow_owner`` (``load.py``). The owner communities use the events as
dated changes of control to check the communities' own mergers against (docs/METHODOLOGY.md section 8f).
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
    return "chow_event" in _tables(con)


def reset(con: Con) -> list[str]:
    dropped = [t for t in TABLES if t in _tables(con)]
    for t in dropped:
        con.execute(f"DROP TABLE {t}")
    return dropped


def check_problems(con: Con) -> list[str]:
    fix = "reload with `healthcare-ledger load --source-name chow`"
    have = _tables(con)
    problems = [f"{t}: missing; {fix}" for t in TABLES if t not in have]
    if problems:
        return problems
    types = {r[0] for r in con.execute("SELECT DISTINCT facility_type FROM chow_event").fetchall()}
    if types != {"hospital", "snf"}:
        problems.append(f"chow_event: facility types {sorted(types)} are not hospital and snf; {fix}")
    bad = con.execute("SELECT count(*) FROM chow_event WHERE effective_date IS NULL").fetchone()[0]
    if bad:
        problems.append(f"chow_event: {bad} events without an effective date; {fix}")
    return problems


def status(con: Con) -> dict:
    return {"events": {r[0]: (r[1], str(r[2]), str(r[3])) for r in con.execute(
        "SELECT facility_type, count(*), min(effective_date), max(effective_date) FROM chow_event GROUP BY 1 ORDER BY 1").fetchall()},
        "owners": con.execute("SELECT count(*) FROM chow_owner").fetchone()[0]}


SOURCE = Source(
    name="chow",
    database=DATABASE,
    title="CHOW",
    kind="reference",
    required=False,
    sources_file=fetch.SOURCES_FILE,
    default_source_dir="data/chow",
    loaded=loaded,
    reset=reset,
    check_problems=check_problems,
    status=status,
    download=recorded_download("CHOW", fetch.download),
    load=load,
)
