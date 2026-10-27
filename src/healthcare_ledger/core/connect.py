"""DuckDB connections: the warehouse catalog for readers, and session settings for the batch jobs."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import duckdb

from . import published
from .errors import UserError


def source_file(warehouse: Path, database: str) -> Path:
    """The DuckDB file of one source in a warehouse directory."""
    return Path(warehouse) / f"{database}.duckdb"


def open_catalog(warehouse: Path, *, read_only: bool, require: Iterable[str] | None = None) -> duckdb.DuckDBPyConnection:
    """An in-memory connection with every source file of ``warehouse`` attached.

    Each ``<source>.duckdb`` present is attached under the source's name (``Source.database``) and
    ``search_path`` lists them in registry order after ``memory``, so reader SQL names tables without
    a catalog (table names are unique across sources) and any table a reader creates stays in memory.
    The tables each attached versioned source publishes (``Source.tables``, ``core.published``) are created as
    temporary views, so a reader queries ``provider`` or ``facility`` as the export has them, or the version tables
    underneath; a source file without its version tables publishes nothing.
    ``require`` names the sources that must be present (default: the ``required`` ones, i.e. NPPES); a
    missing one is a ``UserError`` with exit code 2. Writes go through to the attached files unless
    ``read_only``; DuckDB allows one transaction to write to one file only.
    """
    from .. import sources       # the registry imports this module
    warehouse = Path(warehouse)
    if require is None:
        require = [s.name for s in sources.SOURCES if s.required]
    present = [s for s in sources.SOURCES if source_file(warehouse, s.database).is_file()]
    missing = [s for s in (sources.by_name(n) for n in require) if s not in present]
    if missing:
        where = "has no " if warehouse.is_dir() else "does not exist, so it has no "
        raise UserError(f"warehouse {warehouse} {where}{', '.join(f'{s.database}.duckdb' for s in missing)}; build it with "
                        + ", ".join(f"`healthcare-ledger load --source-name {s.name} --warehouse {warehouse}`" for s in missing)
                        + ", or pass --warehouse with the right directory", exit_code=2)
    con = duckdb.connect()
    try:
        for s in present:
            con.execute(f"ATTACH '{source_file(warehouse, s.database)}' AS {s.database}{' (READ_ONLY)' if read_only else ''}")
        con.execute(f"SET search_path = '{','.join(['memory'] + [s.database for s in present])}'")
        tables = {(c, t) for c, t in con.execute("SELECT table_catalog, table_name FROM information_schema.tables").fetchall()}
        # a source file without the version tables (an empty or foreign file) publishes nothing
        published.create_views(con, [t for s in present if s.versioned
                                     and all((s.database, f"{st.name}_version") in tables for st in s.streams)
                                     for t in s.tables()])
    except BaseException:
        con.close()
        raise
    return con


def configure(con: duckdb.DuckDBPyConnection, *, threads: int | None = None, memory_limit: str | None = None,
              temp_dir: Path | None = None, preserve_insertion_order: bool = False) -> None:
    """Apply the thread and memory caps, drop insertion-order preservation (it costs memory on every large
    CREATE TABLE AS and nothing here relies on it) and point spilling at ``temp_dir``. Each is skipped when
    not given; ``preserve_insertion_order=True`` leaves DuckDB's default alone."""
    if threads:
        con.execute(f"SET threads = {int(threads)}")
    if memory_limit:
        con.execute(f"SET memory_limit = '{memory_limit}'")
    if not preserve_insertion_order:
        con.execute("SET preserve_insertion_order = false")
    if temp_dir:
        con.execute(f"SET temp_directory = '{temp_dir}'")
