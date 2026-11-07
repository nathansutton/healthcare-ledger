"""Read a release as one DuckDB database: a view per table, under the names the docs use.

``connect(export)`` gives an in-memory connection on which ``provider``, ``facility``,
``medicare_enrollment`` and the rest are plain table names, read
lazily from the Parquet files (nothing is copied). The hive partition column ``effective_year`` is left out; it
repeats the year of ``effective_date``. The CSV files of a release (``snapshots``, ``data_dictionary``, ...) are
views too.

A release downloaded in part (some of its per-table zips) still opens: a table that ``manifest.json`` lists but
whose files are not there is a view with the table's columns that raises, when queried, which zip to download.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import duckdb


def _lit(value: Path | str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _no_manifest(root: Path, what: str, fix: str) -> ValueError:
    """The error for a directory without manifest.json, naming a parent or child directory that has one."""
    children = sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []
    near = [d for d in [root.parent, *children] if (d / "manifest.json").is_file()]
    hint = f"did you mean {near[0]}? " if near else ""
    return ValueError(f"{root} is not a complete {what}: it has no manifest.json; {hint}{fix}")


def _columns(root: Path) -> dict[str, list[tuple[str, str]]]:
    """Each table's (column, type) from the release's data_dictionary.csv, if it was downloaded."""
    cols: dict[str, list[tuple[str, str]]] = {}
    path = root / "data_dictionary.csv"
    if path.is_file():
        with open(path, newline="") as f:
            for r in csv.DictReader(f):
                cols.setdefault(r["table"], []).append((r["column"], r["type"]))
    return cols


def connect(export: str | Path | None = None) -> duckdb.DuckDBPyConnection:
    """An in-memory connection with a view for every table of the export directory ``export``."""
    if export is None:
        raise ValueError('connect() needs a directory: pass the folder that holds the release\'s manifest.json, '
                         'e.g. connect("export")')
    con = duckdb.connect()
    views: dict[str, str] = {}

    def add(name: str, sql: str, where: Path) -> None:
        if name in views:
            raise ValueError(f"{name} is in more than one of the directories given ({views[name]}, {where})")
        views[name] = str(where)
        con.execute(f"CREATE VIEW {name} AS {sql}")

    root = Path(export).resolve()
    if not (root / "manifest.json").is_file():
        raise _no_manifest(root, "export", "pass the folder you unzipped the release into (every zip goes into "
                                           "that one folder, next to manifest.json and SHA256SUMS)")
    parquet = root / "parquet"
    for d in sorted(p for p in parquet.iterdir() if p.is_dir()) if parquet.is_dir() else ():
        if any(d.glob("effective_year=*")):
            add(d.name, f"SELECT * EXCLUDE (effective_year) FROM read_parquet({_lit(d / '*' / '*.parquet')}, "
                        f"hive_partitioning = true)", d)
        elif any(d.glob("*.parquet")):
            add(d.name, f"SELECT * FROM read_parquet({_lit(d / '*.parquet')})", d)
    for f in sorted(root.glob("*.csv")):
        add(f.stem, f"SELECT * FROM read_csv({_lit(f)})", f)
    manifest = json.loads((root / "manifest.json").read_text())
    columns = _columns(root)
    for table in manifest.get("row_counts", {}):
        if table in views:
            continue
        msg = (f"{table} is in release {manifest.get('release_id', '?')} but not downloaded: get "
               f"parquet_{table}.zip from the release's Zenodo record and unzip it in {root}")
        err = f"error({_lit(msg)})"
        # every column is the error, so a query that names a column raises before any filter can fold it away;
        # the error in the filter raises for count(*). The columns come from data_dictionary.csv, so DESCRIBE
        # and column names bind as on the real table.
        cols = ", ".join(f"CAST({err} AS {t}) AS \"{c}\"" for c, t in columns.get(table, [])) or f"{err} AS not_downloaded"
        con.execute(f"CREATE VIEW {table} AS SELECT {cols} FROM range(1) WHERE {err} IS NULL")
        views[table] = "(not downloaded)"
    return con
