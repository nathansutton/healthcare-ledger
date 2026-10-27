"""The SQL and Python blocks in the export README and the repository README run against a fixture export.

Each ```sql block runs statement by statement in DuckDB from the export directory; a leading
``-- returns N rows`` comment is checked. Each ```python block is executed as written. R and
Stata blocks are not executed (no interpreter in CI), only checked for the paths they name.
"""

from __future__ import annotations

import re
from pathlib import Path

import duckdb
import pytest

from conftest import main_row, write_zip
from healthcare_ledger.cli import main

REPO = Path(__file__).resolve().parents[1]
BLOCK_RE = re.compile(r"```(?P<lang>\w+)\n(?P<body>.*?)```", re.DOTALL)
RETURNS_RE = re.compile(r"--\s*returns (\d+) rows?")


def blocks(text: str, lang: str) -> list[str]:
    return [m.group("body") for m in BLOCK_RE.finditer(text) if m.group("lang") == lang]


@pytest.fixture(scope="module")
def export_dir(tmp_path_factory) -> Path:
    tmp = tmp_path_factory.mktemp("snippets")
    src = tmp / "zips"
    src.mkdir()
    org = dict(entity_type="2", org_name="BANNER HEALTH SERVICES", practice_address_1="711 W BROAD ST",
               practice_city="FORNEY", practice_state="TX", taxonomy_code_1="207Q00000X", primary_taxonomy_1="Y")
    write_zip(src, "20190101", "20190113", [main_row("1144225327", **org), main_row("1000000002", entity_type="1", last_name="A", first_name="B")])
    write_zip(src, "20200101", "20200112", [main_row("1144225327", **{**org, "practice_address_1": "104 E US HIGHWAY 80"}),
                                            main_row("1000000002", entity_type="1", last_name="A", first_name="B")])
    db = tmp / "warehouse"
    assert main(["load", "--source", str(src), "--warehouse", str(db), "--work", str(tmp / "work")]) == 0
    out = tmp / "export"
    assert main(["export", "--warehouse", str(db), "--out", str(out), "--csv", "--releases", str(tmp / "releases")]) == 0
    return out


def _run_sql(sql: str, cwd: Path) -> list[list[tuple]]:
    con = duckdb.connect()
    con.execute(f"SET file_search_path = '{cwd}'")
    results = []
    for stmt in [s.strip() for s in sql.split(";") if s.strip() and not all(l.strip().startswith("--") for l in s.strip().splitlines())]:
        results.append(con.execute(stmt).fetchall())
    return results


def test_export_readme_sql_blocks_run_and_match_stated_counts(export_dir, monkeypatch):
    monkeypatch.chdir(export_dir)
    text = (export_dir / "README.md").read_text()
    sql_blocks = blocks(text, "sql")
    assert len(sql_blocks) >= 2
    for sql in sql_blocks:
        results = _run_sql(sql, export_dir)
        m = RETURNS_RE.search(sql)
        if m:
            assert len(results) == 1 and len(results[0]) == int(m.group(1)), sql
    # the stated as-of count is the real one
    n = re.search(r"returns 1 row: ([\d,]+) providers as of (\d{4}-\d{2}-\d{2})", text)
    assert n and n.group(2) == "2020-01-12"
    got = duckdb.connect().execute(
        "SELECT count(*) FROM read_parquet('parquet/provider/*/*.parquet', hive_partitioning=true) "
        "WHERE effective_date <= DATE '2020-01-12' AND (end_date IS NULL OR end_date > DATE '2020-01-12')").fetchone()[0]
    assert got == int(n.group(1).replace(",", ""))


def test_export_readme_python_blocks_run(export_dir, monkeypatch, capsys):
    monkeypatch.chdir(export_dir)
    py_blocks = blocks((export_dir / "README.md").read_text(), "python")
    assert py_blocks
    for code in py_blocks:
        exec(compile(code, "<readme>", "exec"), {})  # noqa: S102
    assert "effective_date" in capsys.readouterr().out


def test_export_readme_r_and_stata_blocks_name_real_paths(export_dir):
    text = (export_dir / "README.md").read_text()
    for lang in ("r", "stata"):
        for code in blocks(text, lang):
            for rel in re.findall(r"""['"](parquet/[^'"]+)['"]""", code):
                assert list(export_dir.glob(rel)) or (export_dir / rel).exists(), rel


def test_export_readme_stata_recipe_sql_runs(export_dir, tmp_path, monkeypatch):
    # the SQL inside the Stata block's python: section must run and keep every as-of provider (LEFT JOIN)
    monkeypatch.chdir(tmp_path)
    stata = blocks((export_dir / "README.md").read_text(), "stata")
    assert len(stata) == 1
    sql = re.search(r'"""(.*?)"""', stata[0], re.DOTALL).group(1)
    con = duckdb.connect()
    con.execute(f"SET file_search_path = '{export_dir}'")
    con.execute(sql)
    out = next(tmp_path.glob("registry_*.csv"))
    rows = con.execute(f"SELECT npi, taxonomy_code FROM read_csv('{out}', all_varchar=true) ORDER BY npi").fetchall()
    assert rows == [("1000000002", None), ("1144225327", "207Q00000X")]


def test_export_readme_example_table_matches_data(export_dir):
    text = (export_dir / "README.md").read_text()
    npi = re.search(r"WHERE npi = '(\d{10})'", text).group(1)
    rows = duckdb.connect().execute(
        f"SELECT effective_date, end_date, practice_address_1, practice_city, practice_state FROM "
        f"read_parquet('{export_dir}/parquet/provider/*/*.parquet', hive_partitioning=true) WHERE npi = '{npi}' ORDER BY 1").fetchall()
    assert npi == "1144225327" and len(rows) == 2
    for r in rows:
        line = "| " + " | ".join("" if v is None else str(v) for v in r) + " |"
        assert line in text, line


def test_repository_readme_snippets_run(export_dir, tmp_path, monkeypatch):
    # the repository README reads from ./export/, so run it from a directory holding the fixture export
    (tmp_path / "export").symlink_to(export_dir, target_is_directory=True)
    monkeypatch.chdir(tmp_path)
    text = (REPO / "README.md").read_text()
    sql_blocks = blocks(text, "sql")
    assert sql_blocks, "README lost its SQL examples"
    for sql in sql_blocks:
        _run_sql(sql, tmp_path)
    for code in blocks(text, "python"):
        exec(compile(code, "<repo-readme>", "exec"), {})  # noqa: S102
    assert "FROM provider\n" not in text and "FROM provider " not in text, "README SQL must read the Parquet files, not a bare table"
