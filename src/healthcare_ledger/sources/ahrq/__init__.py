"""The AHRQ Compendium of U.S. Health Systems, a reference source: dated editions, loaded wholesale.

AHRQ publishes, for 2016, 2018 and 2020-2023, a list of health systems (each with at least one general acute
hospital and a group of physicians under common ownership or management) and linkage files that place
hospitals, nursing homes and home health agencies (by CCN), physician group practices (by PECOS associate id)
and, for 2022-2023, outpatient sites in those systems. ``fetch`` downloads the files ``releases/ahrq_sources.csv``
pins; ``load`` rebuilds ``ahrq.duckdb`` from them, one ``ahrq_*`` table per file type. The editions are not
versioned and not exported: the graph's ``system`` and ``system_link`` tables are derived from them
(``derived/compendium.py``; docs/METHODOLOGY.md section 8a).
"""

from __future__ import annotations

import argparse
from pathlib import Path

import duckdb

from ..base import Source
from . import fetch
from .load import DATABASE, FILES, PREFIX, TABLES, load

Con = duckdb.DuckDBPyConnection


def _tables(con: Con) -> set[str]:
    return {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}


def loaded(con: Con) -> bool:
    return f"{PREFIX}system" in _tables(con)


def reset(con: Con) -> list[str]:
    dropped = [t for t in TABLES if t in _tables(con)]
    for t in dropped:
        con.execute(f"DROP TABLE {t}")
    return dropped


def check_problems(con: Con) -> list[str]:
    """Every table holds each of its years from the file ``FILES`` names for it."""
    have = _tables(con)
    problems = []
    for table, years in FILES.items():
        t = PREFIX + table
        if t not in have:
            problems.append(f"{t}: missing; reload with `healthcare-ledger load --source-name ahrq`")
            continue
        got = dict(con.execute(f"SELECT year, list(DISTINCT source_file) FROM {t} GROUP BY year").fetchall())
        want = {y: [n] for y, n in years.items()}
        if got != want:
            problems.append(f"{t}: years and files {dict(sorted(got.items()))} differ from FILES {want}; reload with "
                            f"`healthcare-ledger load --source-name ahrq`")
    return problems


def status(con: Con) -> dict:
    return {"tables": {t: con.execute(f"SELECT count(*), min(year), max(year) FROM {t}").fetchone()
                       for t in TABLES if t in _tables(con)}}


def _download(a: argparse.Namespace) -> int:
    problems = fetch.download(Path(a.releases), Path(a.dest), check_only=a.check)
    for p in problems:
        print("PROBLEM:", p)
    print("OK: every recorded AHRQ file is present and matches its sha256" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


SOURCE = Source(
    name="ahrq",
    database=DATABASE,
    title="AHRQ",
    kind="reference",
    required=False,
    sources_file=fetch.SOURCES_FILE,
    default_source_dir="data/ahrq",
    loaded=loaded,
    reset=reset,
    check_problems=check_problems,
    status=status,
    download=_download,
    load=load,
)
