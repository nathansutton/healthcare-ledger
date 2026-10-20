"""Build the study database by running the SQL in sql/, in order, against an nppes-over-time export.

    uv run python pipeline.py                    # export from $NPPES_EXPORT, or the repo's export/
    uv run python pipeline.py /path/to/export

Writes build/np_fpa.duckdb. Every table in it is made by one of the SQL files in STEPS, so the SQL is
the whole data pipeline; the notebook only estimates and plots. The same files run in the DuckDB CLI
from this directory, in the order of STEPS, after

    SET VARIABLE export = '/path/to/export';
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import duckdb

HERE = Path(__file__).resolve().parent
DB = HERE / "build" / "np_fpa.duckdb"

STEPS = [
    "export_views.sql",        # provider, provider_taxonomy, snapshots (temp views over the export)
    "reference_data.sql",      # states, monthly_snapshots, fpa_dates, state_population, zcta_county, border_county_pairs
    "np_versions.sql",         # np_versions: every version of every individual who was ever an NP
    "np_events.sql",           # np_events: entry, move, sole-proprietor switches, exit
    "np_state_panel.sql",      # np_state_stock, np_state_month, did_state_month, did_state_quarter
    "np_moves.sql",            # np_moves: one row per cross-state move
    "np_border_counties.sql",  # np_county_stock, fpa_cohorts, border_cohort_counties, border_index
]


def export_dir(export: str | Path | None = None) -> Path:
    """The export directory: the argument, ``$NPPES_EXPORT``, or the repository's ``export/``."""
    path = Path(export or os.environ.get("NPPES_EXPORT") or HERE.parents[1] / "export").resolve()
    if not (path / "manifest.json").exists():
        raise FileNotFoundError(f"{path} has no manifest.json. Point NPPES_EXPORT at a complete export directory.")
    return path


def build(export: str | Path | None = None, db: str | Path = DB) -> duckdb.DuckDBPyConnection:
    """Run every step into ``db`` and return the open connection."""
    db = Path(db)
    db.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db))
    con.execute("SET VARIABLE export = ?", [str(export_dir(export))])
    con.execute("SET file_search_path = ?", [str(HERE)])  # so the SQL can say read_csv('data/...')
    for step in STEPS:
        con.execute((HERE / "sql" / step).read_text())
    return con


if __name__ == "__main__":
    con = build(sys.argv[1] if len(sys.argv) > 1 else None)
    for (name,) in con.execute("SELECT table_name FROM duckdb_tables() WHERE NOT temporary ORDER BY table_name").fetchall():
        print(f"{name:24s} {con.table(name).count('*').fetchone()[0]:>12,}")
