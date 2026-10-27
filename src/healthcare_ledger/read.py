"""Read a release and a graph as one DuckDB database: a view per table, under the names the docs use.

``connect(export, graph)`` gives an in-memory connection on which ``provider``, ``facility``,
``medicare_enrollment``, the graph's ``practitioner`` or ``crosswalk`` and the rest are plain table names, read
lazily from the Parquet files (nothing is copied). The hive partition column ``effective_year`` is left out; it
repeats the year of ``effective_date``. The CSV files of a release (``snapshots``, ``data_dictionary``, ...) and of a
graph (``quarters``) are views too.
"""

from __future__ import annotations

from pathlib import Path

import duckdb


def _lit(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def connect(export: str | Path | None = None, graph: str | Path | None = None) -> duckdb.DuckDBPyConnection:
    """An in-memory connection with a view for every table of the export directory ``export`` and of the graph
    directory ``graph``; either may be left out. A table in both would be ambiguous, so it is refused."""
    con = duckdb.connect()
    views: dict[str, str] = {}

    def add(name: str, sql: str, where: Path) -> None:
        if name in views:
            raise ValueError(f"{name} is in more than one of the directories given ({views[name]}, {where})")
        views[name] = str(where)
        con.execute(f"CREATE VIEW {name} AS {sql}")

    if export is not None:
        root = Path(export).resolve()
        if not (root / "manifest.json").is_file():
            raise ValueError(f"{root} is not a complete export: it has no manifest.json")
        for d in sorted(p for p in (root / "parquet").iterdir() if p.is_dir()):
            if any(d.glob("effective_year=*")):
                add(d.name, f"SELECT * EXCLUDE (effective_year) FROM read_parquet({_lit(d / '*' / '*.parquet')}, "
                            f"hive_partitioning = true)", d)
            else:
                add(d.name, f"SELECT * FROM read_parquet({_lit(d / '*.parquet')})", d)
        for f in sorted(root.glob("*.csv")):
            add(f.stem, f"SELECT * FROM read_csv({_lit(f)})", f)
    if graph is not None:
        root = Path(graph).resolve()
        if not (root / "manifest.json").is_file():
            raise ValueError(f"{root} is not a complete graph: it has no manifest.json")
        for f in sorted(root.glob("*.parquet")):
            add(f.stem, f"SELECT * FROM read_parquet({_lit(f)})", f)
        for f in sorted(root.glob("*.csv")):
            add(f.stem, f"SELECT * FROM read_csv({_lit(f)})", f)
    return con
