"""The nurse practitioner FPA study in research/np-full-practice-authority: its reference CSVs, and the SQL in its notebook.

The tables are built by the triple-quoted SQL passed to ``con.execute`` in the notebook's code cells. The tests
pull that SQL out of the notebook (conftest.notebook_sql) and run them, so they check the SQL a reader sees.
"""
import csv
import re
from datetime import date
from pathlib import Path

import duckdb
import pytest

from conftest import main_row, notebook_sql, sql_creating, write_zip
from nppes_over_time.cli import main

STUDY = Path(__file__).resolve().parent.parent / "research" / "np-full-practice-authority"
DATA = STUDY / "data"
NOTEBOOK = STUDY / "np_full_practice_authority.ipynb"


def _rows(name):
    with open(DATA / name, newline="") as f:
        return list(csv.DictReader(f))


STATES = [r["state"] for r in _rows("state_fips.csv")]


def _con() -> duckdb.DuckDBPyConnection:
    """An in-memory DuckDB that resolves data/ paths against the study directory."""
    con = duckdb.connect()
    con.execute("SET file_search_path = ?", [str(STUDY)])
    return con


def test_state_fips_is_the_50_states_and_dc():
    rows = _rows("state_fips.csv")
    assert len(rows) == 51 and len(set(STATES)) == 51 and "DC" in STATES
    fips = [r["fips"] for r in rows]
    assert len(set(fips)) == 51 and all(re.fullmatch(r"\d{2}", f) for f in fips)


def test_fpa_dates_cover_every_state_once():
    rows = _rows("np_fpa_dates.csv")
    assert sorted(r["state"] for r in rows) == sorted(STATES)
    for r in rows:
        assert r["coding"] in {"always", "statute", "never"}, r
        assert r["source"], f"{r['state']} has no source"
        if r["coding"] == "never":
            assert r["fpa_effective"] == "", r
        else:
            assert re.fullmatch(r"\d{4}-\d{2}", r["fpa_effective"]), r
        if r["coding"] == "always":
            assert r["fpa_effective"] == "1998-01", r
        assert r["partial"] in {"", "Y"}, r


def test_aanp_list_is_subset_of_states():
    states = [r["state"] for r in _rows("aanp_full_practice_2026.csv")]
    assert len(states) == len(set(states))
    assert set(states) <= set(STATES)
    assert len(states) == 28  # 27 states and DC in May 2026


def test_population_is_a_balanced_state_year_grid():
    rows = _rows("state_population.csv")
    years = sorted({int(r["year"]) for r in rows})
    assert years == list(range(2010, 2025))
    for st in STATES:
        assert {int(r["year"]) for r in rows if r["state"] == st} == set(years), st
    assert all(int(r["population"]) > 500_000 for r in rows)


def test_county_files_use_five_digit_fips():
    for name, cols in (("zcta_county.csv", ["zcta", "county_fips"]), ("state_border_county_pairs.csv", ["county_fips", "neighbor_fips"])):
        rows = _rows(name)
        assert rows
        for r in rows:
            for c in cols:
                assert re.fullmatch(r"\d{5}", r[c]), (name, r)
    zctas = [r["zcta"] for r in _rows("zcta_county.csv")]
    assert len(zctas) == len(set(zctas))
    assert all(r["county_fips"][:2] != r["neighbor_fips"][:2] for r in _rows("state_border_county_pairs.csv"))


def test_fpa_dates_sql():
    con = _con()
    con.execute(sql_creating(NOTEBOOK, "fpa_dates"))
    assert con.execute("SELECT count(*) FROM states").fetchone()[0] == 51
    assert con.execute("SELECT count(*) FROM fpa_dates WHERE fpa_effective IS NOT NULL").fetchone()[0] == \
        sum(r["coding"] != "never" for r in _rows("np_fpa_dates.csv"))
    assert con.execute("SELECT count(*) FROM fpa_dates WHERE aanp_full_practice").fetchone()[0] == 28
    assert con.execute("SELECT partial FROM fpa_dates WHERE state = 'FL'").fetchone()[0] is True
    assert con.execute("SELECT fpa_effective FROM fpa_dates WHERE state = 'MA'").fetchone()[0] == date(2021, 1, 1)


def test_np_events_from_synthetic_versions():
    """Entry, exit, move and sole-proprietor events from a hand-built version history."""
    con = _con()
    con.execute("""
    CREATE TABLE np_versions AS SELECT * FROM (VALUES
        -- A: RN in 2012, becomes NP in 2013 (entry), moves MD->PA in 2014, sole prop X->Y in 2015, deactivated via stub in 2016
        ('A', DATE '2012-01-08', DATE '2013-01-13', 'changed', 'MD', 'X', NULL, '163W00000X', false),
        ('A', DATE '2013-01-13', DATE '2014-01-12', 'changed', 'MD', 'X', NULL, '363L00000X', true),
        ('A', DATE '2014-01-12', DATE '2015-01-11', 'changed', 'PA', 'X', NULL, '363L00000X', true),
        ('A', DATE '2015-01-11', DATE '2016-01-10', 'changed', 'PA', 'Y', NULL, '363L00000X', true),
        ('A', DATE '2016-01-10', NULL, NULL, NULL, NULL, DATE '2016-01-02', NULL, NULL),
        -- B: NP from the start, switches away to a physician assistant code in 2015 (exit), never deactivated
        ('B', DATE '2012-01-08', DATE '2015-01-11', 'changed', 'NY', 'N', NULL, '363L00000X', true),
        ('B', DATE '2015-01-11', NULL, NULL, 'NY', 'N', NULL, '363A00000X', false),
        -- C: NP who left the file in 2014
        ('C', DATE '2012-01-08', DATE '2014-01-12', 'absent', 'TX', 'N', NULL, '363L00000X', true)
    ) v(npi, effective_date, end_date, close_reason, practice_state, sole_proprietor, deactivation_date, primary_taxonomy, is_np)
    """)
    con.execute(sql_creating(NOTEBOOK, "np_events"))

    def one(npi, event, col):
        hits = con.execute(f"SELECT {col} FROM np_events WHERE npi = ? AND event = ?", [npi, event]).fetchall()
        assert len(hits) == 1, (npi, event, hits)
        return hits[0][0]

    assert one("A", "entry", "date") == date(2013, 1, 13)
    assert one("A", "entry", "detail") == "switched_taxonomy"
    assert one("A", "move", "from_state") == "MD" and one("A", "move", "state") == "PA"
    assert one("A", "became_sole_proprietor", "date") == date(2015, 1, 11)
    assert one("A", "exit", "detail") == "deactivated" and one("A", "exit", "state") == "PA"  # state from the version before the stub
    assert one("B", "entry", "detail") == "new_npi"
    assert one("B", "exit", "detail") == "switched_taxonomy" and one("B", "exit", "state") == "NY"
    assert one("C", "exit", "detail") == "absent" and one("C", "exit", "date") == date(2014, 1, 12)
    counts = dict(con.execute("SELECT event, count(*) FROM np_events GROUP BY event").fetchall())
    assert counts == {"entry": 3, "exit": 3, "move": 1, "became_sole_proprietor": 1}


@pytest.fixture(scope="module")
def fixture_export(tmp_path_factory) -> Path:
    """A three-month export: one NP who moves from Maryland to Pennsylvania, one who stays in Texas."""
    tmp = tmp_path_factory.mktemp("np_fpa")
    src = tmp / "zips"
    src.mkdir()
    np_ = dict(entity_type="1", last_name="A", first_name="B", taxonomy_code_1="363L00000X", primary_taxonomy_1="Y", sole_proprietor="N")
    md = dict(np_, practice_state="MD", practice_postal_code="212011234")
    pa = dict(np_, practice_state="PA", practice_postal_code="171011234")
    tx = dict(np_, practice_state="TX", practice_postal_code="770021234")
    write_zip(src, "20160101", "20160110", [main_row("1000000001", **md), main_row("1000000002", **tx)])
    write_zip(src, "20160201", "20160214", [main_row("1000000001", **pa), main_row("1000000002", **tx)])
    write_zip(src, "20160301", "20160313", [main_row("1000000001", **pa), main_row("1000000002", **tx)])
    db = tmp / "w.duckdb"
    assert main(["load", "--source", str(src), "--db", str(db), "--work", str(tmp / "work")]) == 0
    out = tmp / "export"
    assert main(["export", "--db", str(db), "--out", str(out), "--releases", str(tmp / "releases")]) == 0
    return out


def test_notebook_sql_runs_end_to_end_on_a_fixture_export(fixture_export):
    con = _con()
    con.execute("SET VARIABLE export = ?", [str(fixture_export)])
    for block in notebook_sql(NOTEBOOK):
        con.execute(block)

    assert con.execute("SELECT count(*), count(DISTINCT npi) FROM np_versions").fetchone() == (3, 2)
    assert con.execute("SELECT event, state, from_state FROM np_events WHERE event = 'move'").fetchall() == [("move", "PA", "MD")]
    stock = con.execute("SELECT state, month, nps, move, move_out FROM np_state_month ORDER BY state, month").fetchall()
    assert stock == [
        ("MD", date(2016, 1, 1), 1, 0, 0),
        ("PA", date(2016, 2, 1), 1, 1, 0),
        ("PA", date(2016, 3, 1), 1, 0, 0),
        ("TX", date(2016, 1, 1), 1, 0, 0),
        ("TX", date(2016, 2, 1), 1, 0, 0),
        ("TX", date(2016, 3, 1), 1, 0, 0),
    ]
    # MD's law was in force on the move date; PA never adopted
    assert con.execute("SELECT destination, from_fpa_state FROM np_moves").fetchall() == [("into never-FPA state", True)]
    # the fixture's snapshots are all in 2016, inside the Census years, so nothing is carried forward
    assert con.execute("SELECT max(year) FROM state_population").fetchone()[0] == 2024
    for table in ("did_state_month", "did_state_quarter", "np_county_stock", "border_cohort_counties", "border_index"):
        con.execute(f"SELECT count(*) FROM {table}").fetchone()
