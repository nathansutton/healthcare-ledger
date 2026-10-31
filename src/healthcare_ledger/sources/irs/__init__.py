"""IRS Form 990 e-file returns (Schedules R and H) and the Exempt Organizations Business Master File, a reference
source: loaded wholesale, not versioned, not exported.

Every tax-exempt organization above the postcard threshold files a Form 990; Schedule R lists the organizations it
is related to by control (parents, subsidiaries, brother-sister organizations, supporting organizations and the
taxable entities it owns) with their EINs, and Schedule H lists a hospital filer's facilities. The IRS publishes
the e-filed returns as XML; the Nonprofit Open Data Collective (Urban Institute's NCCS) flattens them to one
parquet table per form part and tax year, which ``fetch`` downloads as ``releases/irs_sources.csv`` pins, with the
IRS's own BMF extract. ``load`` rebuilds ``irs.duckdb`` from them (``load.py``): returns, related entities,
hospital facilities, per-year EIN families and the BMF. The owner communities (docs/METHODOLOGY.md section 8f)
use the families as a hard link between organizations whose EINs are known.
"""

from __future__ import annotations

import duckdb

from ..base import Source, recorded_download
from . import fetch
from .load import DATABASE, TABLES, YEARS, load

Con = duckdb.DuckDBPyConnection


def _tables(con: Con) -> set[str]:
    return {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}


def loaded(con: Con) -> bool:
    return "irs_return" in _tables(con)


def reset(con: Con) -> list[str]:
    dropped = [t for t in TABLES if t in _tables(con)]
    for t in dropped:
        con.execute(f"DROP TABLE {t}")
    return dropped


def check_problems(con: Con) -> list[str]:
    fix = "reload with `healthcare-ledger load --source-name irs`"
    have = _tables(con)
    problems = [f"{t}: missing; {fix}" for t in TABLES if t not in have]
    if problems:
        return problems
    years = [r[0] for r in con.execute("SELECT DISTINCT tax_year FROM irs_return ORDER BY 1").fetchall()]
    if not years or any(y not in YEARS for y in years):
        problems.append(f"irs_return: tax years {years} are not within {YEARS[0]}-{YEARS[-1]}; {fix}")
    bad = con.execute("SELECT count(*) FROM irs_related WHERE related_ein IS NOT NULL AND NOT regexp_matches(related_ein, '^[0-9]{9}$')").fetchone()[0]
    if bad:
        problems.append(f"irs_related: {bad} related EINs are not nine digits; {fix}")
    if not con.execute("SELECT count(*) FROM irs_bmf").fetchone()[0]:
        problems.append(f"irs_bmf: no rows; {fix}")
    return problems


def status(con: Con) -> dict:
    return {"tables": {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TABLES if t in _tables(con)},
            "tax_years": con.execute("SELECT min(tax_year), max(tax_year) FROM irs_return").fetchone()}


SOURCE = Source(
    name="irs",
    database=DATABASE,
    title="IRS",
    kind="reference",
    required=False,
    sources_file=fetch.SOURCES_FILE,
    default_source_dir="data/irs",
    loaded=loaded,
    reset=reset,
    check_problems=check_problems,
    status=status,
    download=recorded_download("IRS", fetch.download),
    load=load,
)
