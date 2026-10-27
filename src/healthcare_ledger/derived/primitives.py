"""Group primitives: the physician, reassignment-edge, group-size and main-group tables the consolidation literature shares.

They were first built in research/provider-networks; any study of physician groups (vertical integration, ownership,
market concentration) needs them with the same meaning, so ``graph-build`` writes them next to the graph and the
studies read the Parquet files. Each is one SQL file in ``derived/sql/`` (a SELECT; the table is ``CREATE TABLE
<name> AS`` it), run in order on an in-memory DuckDB with views over the graph directory's tables, as a notebook
opens them:

``physician_q`` (``physician_quarter.parquet``)
    a physician, a practitioner whose NPPES primary taxonomy is a physician code (``20*``), on each quarter's date.
``physician_edge``
    a physician's reassignments on each quarter's date, with ``k``, the number of organizations they reassign to.
``group_size``
    an organization's physicians on each quarter's date: ``headcount`` and ``physicians`` = the sum of ``1/k``.

With the Medicaid source (``graph-build --medicaid``), two more from its ``medicaid_spending`` table:

``medicaid_volume``
    Medicaid dollars an organization's NPIs billed for a physician's NPI in year Y, on the first quarter on or
    after 31 December Y.
``main_group``
    each physician's group by plurality of that volume (ties broken by organization PAC id).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import duckdb

from ..core.connect import configure

SQL_DIR = Path(__file__).parent / "sql"
# the graph tables the SQL reads, opened as views over the graph directory
GRAPH_TABLES = ("practitioner", "organization", "reassignment")


@dataclass(frozen=True)
class Primitive:
    name: str               # the table, and derived/sql/<name>.sql
    file: str               # the Parquet file in the graph directory, without .parquet
    key: tuple[str, ...]
    description: str


GROUP = (
    Primitive("physician_q", "physician_quarter", ("as_of_date", "pac_id"),
              "physicians (NPPES primary taxonomy 20*) on each quarter's date"),
    Primitive("physician_edge", "physician_edge", ("as_of_date", "practitioner_pac_id", "organization_pac_id"),
              "a physician's reassignments on each quarter's date, with k, the number of organizations"),
    Primitive("group_size", "group_size", ("as_of_date", "organization_pac_id"),
              "an organization's physicians on each quarter's date: headcount and the sum of 1/k"),
)
MEDICAID = (
    Primitive("medicaid_volume", "medicaid_volume", ("as_of_date", "practitioner_pac_id", "organization_pac_id"),
              "Medicaid paid by an organization's NPIs for a physician's NPI in year Y, on the first quarter on or "
              "after 31 December Y"),
    Primitive("main_group", "main_group", ("as_of_date", "practitioner_pac_id"),
              "each physician's group by plurality of Medicaid volume (ties broken by organization PAC id)"),
)
PRIMITIVES = GROUP + MEDICAID


def sql(name: str) -> str:
    """The SELECT of one primitive, as derived/sql/<name>.sql holds it."""
    return (SQL_DIR / f"{name}.sql").read_text()


def create(con: duckdb.DuckDBPyConnection, name: str) -> None:
    con.execute(f"CREATE TABLE {name} AS\n{sql(name)}")


def build(graph: Path, *, medicaid: Path | None = None, threads: int | None = None, memory_limit: str | None = None,
          work_dir: Path | None = None) -> dict[str, dict]:
    """Write the primitives' Parquet files into the graph directory ``graph``, which must already hold the graph's
    tables and quarters.csv; with ``medicaid`` (the Medicaid source's DuckDB file), also the two Medicaid tables.
    Returns the manifest entry of each file written."""
    con = duckdb.connect()
    try:
        configure(con, threads=threads, memory_limit=memory_limit, temp_dir=work_dir, preserve_insertion_order=True)
        for t in GRAPH_TABLES:
            con.execute(f"CREATE VIEW {t} AS SELECT * FROM read_parquet('{graph / t}.parquet')")
        con.execute(f"CREATE VIEW quarters AS SELECT * FROM read_csv('{graph / 'quarters.csv'}')")
        todo = GROUP
        if medicaid:
            con.execute(f"ATTACH '{medicaid}' AS medicaid (READ_ONLY)")
            con.execute("CREATE VIEW medicaid_spending AS "
                        "SELECT billing_npi, servicing_npi, month, paid FROM medicaid.medicaid_spending")
            todo += MEDICAID
        tables = {}
        for p in todo:
            create(con, p.name)
            path = graph / f"{p.file}.parquet"
            con.execute(f"COPY (SELECT * FROM {p.name} ORDER BY {', '.join(p.key)}) TO '{path}' "
                        f"(FORMAT parquet, COMPRESSION zstd)")
            n = con.execute(f"SELECT count(*) FROM '{path}'").fetchone()[0]
            tables[p.file] = {"rows": n, "key": list(p.key), "description": p.description,
                              "sql": f"derived/sql/{p.name}.sql"}
        return tables
    finally:
        con.close()
