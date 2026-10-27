"""The shared primitives in core/: SQL helpers, the CSV reader fallbacks, hashing and fingerprints, the warehouse catalog."""

from __future__ import annotations

import hashlib

import duckdb
import pytest

from healthcare_ledger.core.connect import open_catalog
from healthcare_ledger.core.csvread import read_csv_into, repair_utf8
from healthcare_ledger.core.errors import UserError
from healthcare_ledger.core.sql import q, sql_type, ymd
from healthcare_ledger.core.versioning import (fingerprint_sql, interval_fingerprint, recompute_fingerprints,
                                             row_hash_expr, stage_hash_tables, stored_fingerprints)
from healthcare_ledger.core.columns import Column


def test_sql_helpers():
    assert [sql_type(k) for k in ("text", "date", "int", "number")] == ["VARCHAR", "DATE", "INTEGER", "DOUBLE"]
    assert ymd(20261011) == "2026-10-11"
    assert q('Provider "Name" (Legal)') == '"Provider ""Name"" (Legal)"'


@pytest.mark.parametrize(("data", "mode", "rows"), [
    (b"A,B\r\n1,x\r\n2,\xc3\x89\r\n", "strict", [("1", "x"), ("2", "É")]),
    (b"A,B,C\r\n1,2,3\r\n4,5\r\n6,7,8\r\n", "lenient", [("1", "2", "3"), ("4", "5", None), ("6", "7", "8")]),
    (b"A,B\r\n1,caf\xe9\r\n", "strict-latin1", [("1", "café")]),
    # UTF-8 with one stray latin-1 byte: every DuckDB reader fails, the line-by-line repair does not
    (b"A,B\r\n1,\xc3\x89LAN\r\n2,KELLI\xa0\r\n3,\xc3\x89\r\n", "repaired-utf8+1",
     [("1", "ÉLAN"), ("2", "KELLI "), ("3", "É")]),
])
def test_read_csv_into_modes(tmp_path, data, mode, rows):
    path = tmp_path / "f.csv"
    path.write_bytes(data)
    con, scratch = duckdb.connect(), []
    assert read_csv_into(con, "t", path, work_dir=tmp_path, scratch=scratch, repair=True) == mode
    assert con.execute("SELECT * FROM t").fetchall() == rows
    assert [p.name for p in scratch] == (["f.csv.utf8.csv"] if mode.startswith("repaired") else [])


def test_read_csv_into_without_repair_gives_up(tmp_path):
    path = tmp_path / "f.csv"
    path.write_bytes(b"A,B\r\n1,\xc3\x89LAN\r\n2,KELLI\xa0\r\n")
    with pytest.raises(RuntimeError, match="could not parse"):
        read_csv_into(duckdb.connect(), "t", path)


def test_read_csv_into_explicit_header(tmp_path):
    # data rows wider than the header: the predicate asks for a re-read with the header as explicit names
    path = tmp_path / "f.csv"
    path.write_bytes(b"NPI,City\r\n1,DENVER,x,y\r\n2,BOISE,,\r\n")
    con = duckdb.connect()
    mode = read_csv_into(con, "t", path, explicit_header_if=lambda c, t: True)
    assert mode == "explicit-header+2extra"
    assert [r[0] for r in con.execute("DESCRIBE t").fetchall()] == ["NPI", "City", "_extra_1", "_extra_2"]


def test_repair_utf8_decodes_only_the_bad_lines(tmp_path):
    src, dest = tmp_path / "a.csv", tmp_path / "a.utf8.csv"
    src.write_bytes(b"H,N\r\n1,KELLI\xa0\r\n2,\xc3\x89LAN\r\n3,\xc9LAN\r\n")
    assert repair_utf8(src, dest) == 2
    assert dest.read_bytes() == "H,N\r\n1,KELLI \r\n2,ÉLAN\r\n3,ÉLAN\r\n".encode()


def test_row_hash_ignores_a_null_hash_optional_column():
    con = duckdb.connect()
    con.execute("CREATE TABLE t AS SELECT * FROM (VALUES ('A', 'x', NULL), ('B', 'x', 'F')) v(k, name, sex)")
    base = row_hash_expr([Column("name", "NAME")])
    with_optional = row_hash_expr([Column("name", "NAME"), Column("sex", "SEX", hash_optional=True)])
    rows = con.execute(f"SELECT k, md5({base}) = md5({with_optional}) FROM t ORDER BY k").fetchall()
    assert rows == [("A", True), ("B", False)]


def test_stage_hash_tables_and_fingerprints():
    con = duckdb.connect()
    con.execute("CREATE TEMP TABLE stg AS SELECT * FROM (VALUES ('1', 'a'), ('1', 'b'), ('2', 'c')) v(k, val)")
    stage_hash_tables(con, "stg", "k", [Column("val", "VAL")], "h", "seq", "keys")
    assert "stg" not in {r[0] for r in con.execute("SELECT table_name FROM duckdb_tables()").fetchall()}
    a, b = (con.execute(f"SELECT md5(concat_ws(chr(31), '{v}'))").fetchone()[0] for v in "ab")
    two = con.execute("SELECT version_hash FROM keys WHERE k = '1'").fetchone()[0]
    assert two == con.execute(f"SELECT md5('{min(a, b)},{max(a, b)}')").fetchone()[0]
    assert con.execute("SELECT max(row_seq) FROM seq WHERE k = '1'").fetchone() == (2,)

    # a version table with one interval per key: stored (from state) and recomputed (by interval) agree
    con.execute("""CREATE TABLE s_version AS SELECT k, version_hash, 1 AS row_seq, DATE '2020-01-01' AS effective_date,
                   NULL::DATE AS end_date FROM keys""")
    con.execute(f"""CREATE TABLE s_stream AS SELECT 20200101 AS snapshot_id, 's' AS stream,
                    ({fingerprint_sql("(SELECT k AS npi, version_hash FROM keys)")}) AS content_fingerprint""")
    assert stored_fingerprints(con, "s_stream") == recompute_fingerprints(con, "s_stream", "k")
    assert interval_fingerprint(con, "s_version", "k", "2019-12-31") == hashlib.md5(b"").hexdigest()


def _source_file(warehouse, name: str, table: str) -> None:
    con = duckdb.connect(str(warehouse / f"{name}.duckdb"))
    con.execute(f"CREATE TABLE {table} AS SELECT 1 AS snapshot_id")
    con.close()


def test_open_catalog_attaches_the_source_files_present(tmp_path):
    _source_file(tmp_path, "nppes", "snapshot")
    _source_file(tmp_path, "pos", "pos_snapshot")
    _source_file(tmp_path, "other", "other_snapshot")   # not a registered source: ignored
    con = open_catalog(tmp_path, read_only=True)
    attached = {r[0] for r in con.execute("SELECT database_name FROM duckdb_databases() WHERE NOT internal").fetchall()}
    assert attached == {"memory", "nppes", "pos"}
    assert con.execute("SELECT count(*) FROM snapshot JOIN pos_snapshot USING (snapshot_id)").fetchone() == (1,)
    con.execute("CREATE TABLE scratch AS SELECT 1 AS x")     # a reader's own table stays in memory
    assert con.execute("SELECT database_name FROM duckdb_tables() WHERE table_name = 'scratch'").fetchone() == ("memory",)
    with pytest.raises(duckdb.Error, match="read-only"):
        con.execute("UPDATE snapshot SET snapshot_id = 2")
    con.close()
    con = open_catalog(tmp_path, read_only=False)
    con.execute("UPDATE pos_snapshot SET snapshot_id = 2")      # writes reach the source's own file
    con.close()
    assert duckdb.connect(str(tmp_path / "pos.duckdb")).execute("SELECT snapshot_id FROM pos_snapshot").fetchone() == (2,)


def test_open_catalog_refuses_a_missing_required_source(tmp_path):
    with pytest.raises(UserError, match="has no nppes.duckdb") as e:
        open_catalog(tmp_path, read_only=True)
    assert e.value.exit_code == 2
    with pytest.raises(UserError, match="does not exist"):
        open_catalog(tmp_path / "nowhere", read_only=True)
    _source_file(tmp_path, "nppes", "snapshot")
    with pytest.raises(UserError, match="has no ppef.duckdb.*--source-name ppef") as e:
        open_catalog(tmp_path, read_only=True, require=["nppes", "ppef"])
    assert e.value.exit_code == 2
    open_catalog(tmp_path, read_only=True).close()
    assert not (tmp_path / "nowhere").exists()
