"""The post-Dobbs OB-GYN workforce study in research/dobbs-obgyn-workforce: its reference CSVs, and the SQL in its notebook.

The tables are built by the triple-quoted SQL passed to ``con.execute`` in the notebook's code cells. The tests
pull that SQL out of the notebook (conftest.notebook_sql) and run it, so they check the SQL a reader sees.
"""
import csv
import re
from datetime import date
from pathlib import Path

import duckdb
import pytest

from conftest import main_row, notebook_sql, study_con, study_rows, write_zip
from healthcare_ledger.cli import main

STUDY = Path(__file__).resolve().parent.parent / "research" / "dobbs-obgyn-workforce"
NOTEBOOK = STUDY / "dobbs_obgyn_workforce.ipynb"


def _rows(name):
    return study_rows(STUDY, name)


def _con() -> duckdb.DuckDBPyConnection:
    return study_con(STUDY)


def test_states_match_the_paper():
    rows = {r["state"]: r for r in _rows("states.csv")}
    with open(STUDY.parent.parent / "reference" / "state_fips.csv", newline="") as f:
        assert sorted(rows) == sorted(r["state"] for r in csv.DictReader(f))  # names come from reference/state_fips.csv
    groups = {g: sorted(s for s, r in rows.items() if r["study_group"] == g) for g in ("restrictive", "control")}
    assert groups["restrictive"] == ["AL", "AR", "ID", "KY", "LA", "MO", "MS", "OK", "SD", "TN", "TX", "WV"]
    assert groups["control"] == ["DE", "FL", "IA", "KS", "MI", "MT", "NH", "NV", "PA", "RI", "UT", "VA", "WI", "WY"]
    assert rows["IN"]["study_group"] == ""
    for s, r in rows.items():
        assert int(r["women_15_49"]) > 0, s
        assert (r["etable2_women_15_49"] != "") == (r["study_group"] != ""), s
    # eTable 2 equals the ACS except for two swapped pairs of alphabetical neighbours
    differ = sorted(s for s, r in rows.items() if r["etable2_women_15_49"] and r["etable2_women_15_49"] != r["women_15_49"])
    assert differ == ["MO", "MS", "NH", "NV"]
    for a, b in (("MS", "MO"), ("NV", "NH")):
        assert rows[a]["etable2_women_15_49"] == rows[b]["women_15_49"]
        assert rows[b]["etable2_women_15_49"] == rows[a]["women_15_49"]
    # Guttmacher's July 2024 rating: every restrictive state is rated most restrictive, and the protected-access
    # group of section 6 is 18 states and DC, two of them (MI, MT) in the paper's comparison group
    ratings = {"Most Restrictive", "Very Restrictive", "Restrictive", "Some Restrictions/Protections",
               "Protective", "Very Protective", "Most Protective"}
    assert {r["guttmacher_july_2024"] for r in rows.values()} == ratings
    assert all(rows[s]["guttmacher_july_2024"] == "Most Restrictive" for s in groups["restrictive"])
    protected = sorted(s for s, r in rows.items() if r["guttmacher_july_2024"].endswith("Protective"))
    assert len(protected) == 19 and "DC" in protected
    assert [s for s in protected if rows[s]["study_group"]] == ["MI", "MT"]


def test_population_by_year_covers_every_state_and_year():
    states = {r["state"] for r in _rows("states.csv")}
    rows = _rows("population_by_year.csv")
    assert {(r["state"], int(r["year"])) for r in rows} == {(s, y) for s in states for y in range(2018, 2026)}
    assert len(rows) == len(states) * 8
    acs = {r["state"]: int(r["women_15_49"]) for r in _rows("states.csv")}
    for r in rows:
        assert r["estimate"] == ("intercensal 2010-2020" if int(r["year"]) < 2020 else "vintage 2025"), r
        # the estimates are close to the 2017-2021 ACS average in every state and year
        assert 0.85 < int(r["women_15_49"]) / acs[r["state"]] < 1.15, r


def test_taxonomy_codes_are_etable_1():
    rows = _rows("taxonomy.csv")
    for r in rows:
        assert re.fullmatch(r"[0-9A-Z]{9}X", r["code"]), r
        assert r["name"], r
    by_set = {}
    for r in rows:
        by_set.setdefault(r["code_set"], []).append(r["code"])
    assert set(by_set) == {"obgyn", "family_medicine", "np_obstetric", "primary_care"}
    assert len(by_set["obgyn"]) == 11 and {"176B00000X", "367A00000X", "207V00000X"} <= set(by_set["obgyn"])
    assert by_set["family_medicine"] == ["207Q00000X"]
    assert len(by_set["primary_care"]) == 7
    assert all(c.startswith("363L") for c in by_set["np_obstetric"])
    for s, codes in by_set.items():
        assert len(codes) == len(set(codes)), s


@pytest.fixture(scope="module")
def fixture_export(tmp_path_factory) -> Path:
    """Two December snapshots (2021, 2022) of individuals in a restrictive state (TX) and a comparison state (PA)."""
    tmp = tmp_path_factory.mktemp("dobbs")
    src = tmp / "zips"
    src.mkdir()
    person = dict(entity_type="1", last_name="A", first_name="B", enumeration_date="01/01/2010")
    tx = dict(person, practice_state="TX", practice_postal_code="770021234")
    pa = dict(person, practice_state="PA", practice_postal_code="171011234")
    ob = dict(taxonomy_code_1="207V00000X", primary_taxonomy_1="Y")
    rows_2021 = [
        main_row("1000000001", **tx, **ob),                                           # OB-GYN, moves TX -> PA in 2022
        main_row("1000000002", **pa, taxonomy_code_1="176B00000X", primary_taxonomy_1="Y"),   # midwife
        main_row("1000000003", **pa, taxonomy_code_1="207Q00000X", primary_taxonomy_1="Y",    # family physician with an OB secondary
                 taxonomy_code_2="207V00000X", primary_taxonomy_2="N"),
        main_row("1000000004", **tx, taxonomy_code_1="363LX0001X", primary_taxonomy_1="Y"),   # obstetric NP
        main_row("1000000005", **tx, taxonomy_code_1="207R00000X", primary_taxonomy_1="Y"),   # internist
        main_row("1000000006", **dict(tx, entity_type="2"), **ob),                     # an organization: not counted
        main_row("1000000007", **dict(person, practice_state="CA", practice_postal_code="900011234"), **ob),  # protected-access only
        main_row("1000000008", **tx, taxonomy_code_1="207V00000X", primary_taxonomy_1="N",  # no taxonomy flagged primary
                 taxonomy_code_2="207R00000X", primary_taxonomy_2="N"),
        main_row("1000000009", **tx, taxonomy_code_1="207R00000X", primary_taxonomy_1="Y",  # an internist with an OB secondary
                 taxonomy_code_2="207V00000X", primary_taxonomy_2="N"),
    ]
    rows_2022 = [main_row("1000000001", **pa, **ob)] + rows_2021[1:]
    write_zip(src, "20211201", "20211212", rows_2021)
    write_zip(src, "20221201", "20221211", rows_2022)
    db = tmp / "warehouse"
    assert main(["load", "--source", str(src), "--warehouse", str(db), "--work", str(tmp / "work")]) == 0
    out = tmp / "export"
    assert main(["export", "--warehouse", str(db), "--out", str(out), "--releases", str(tmp / "releases")]) == 0
    return out


def test_notebook_sql_runs_end_to_end_on_a_fixture_export(fixture_export):
    con = _con()
    con.execute("SET VARIABLE export = ?", [str(fixture_export)])
    for block in notebook_sql(NOTEBOOK, queries=True):
        con.execute(block)

    assert con.execute("SELECT count(*) FROM states WHERE study_group IS NOT NULL").fetchone()[0] == 26
    # the organization and the record with no primary taxonomy are not study versions; the California physician
    # is one, for section 6, but not in the paper's panel
    assert con.execute("SELECT npi FROM study_versions GROUP BY npi ORDER BY npi").fetchall() == [
        ("1000000001",), ("1000000002",), ("1000000003",), ("1000000004",), ("1000000005",), ("1000000007",), ("1000000009",)]
    # an OB-GYN code counts only as the primary taxonomy, and as a secondary only for family physicians
    assert con.execute("SELECT obgyn, family_medicine_ob, primary_care FROM study_versions WHERE npi = '1000000009'").fetchall() == [
        (False, False, True)]
    assert con.execute("SELECT panel, year, as_of_date FROM panel_dates ORDER BY panel, year").fetchall() == [
        ("extended", 2021, date(2021, 12, 12)), ("extended", 2022, date(2022, 12, 11)),
        ("paper", 2021, date(2021, 12, 12)), ("paper", 2022, date(2022, 12, 11)),
    ]
    cells = {(r[0], r[1]): r[2:] for r in con.execute("""
        SELECT state, year, obgyn, obgyn_fm_np, primary_care FROM state_year
        WHERE panel = 'paper' AND state IN ('TX', 'PA')""").fetchall()}
    # 2021: the OB-GYN in TX and the midwife in PA; the NP adds to TX and the family physician to PA, where
    # (207Q00000X, on eTable 1's primary care list too) they also count as primary care; TX has two internists
    assert cells[("TX", 2021)] == (1, 2, 2)
    assert cells[("PA", 2021)] == (1, 2, 1)
    # 2022: the OB-GYN has moved to PA
    assert cells[("TX", 2022)] == (0, 1, 2)
    assert cells[("PA", 2022)] == (2, 3, 1)
    # every study state has a row in every year, with zeros where nobody practises
    assert con.execute("SELECT count(*) FROM state_year WHERE panel = 'paper'").fetchone()[0] == 52
    assert con.execute("SELECT obgyn FROM monthly WHERE state = 'PA' ORDER BY as_of_date").fetchall() == [(1,), (2,)]
    assert con.execute("SELECT count(*) FROM state_year WHERE state = 'CA'").fetchone()[0] == 0

    # section 6: both December files are in every December panel, neither is a quarterly file
    assert con.execute("SELECT panel, count(*) FROM alt_dates GROUP BY panel ORDER BY panel").fetchall() == [
        ("december_2018_2023", 2), ("december_2018_2024", 2), ("extended", 2), ("paper", 2)]
    # 26 study states and 17 protected-access states outside them, in each panel and file
    assert con.execute("SELECT count(*) FROM alt_panel").fetchone()[0] == 4 * 2 * 43
    assert con.execute("SELECT obgyn FROM alt_panel WHERE state = 'CA' AND panel = 'paper' ORDER BY as_of_date").fetchall() == [(1,), (1,)]
    # the same counts as state_year, divided by the population of the file's year
    assert con.execute("""
        SELECT count(*) FROM alt_panel a JOIN state_year y USING (panel, as_of_date, state)
        WHERE a.obgyn <> y.obgyn OR a.women <> y.women""").fetchone()[0] == 0
    tx = con.execute("""
        SELECT year(as_of_date), women_that_year FROM alt_panel WHERE state = 'TX' AND panel = 'paper' ORDER BY 1""").fetchall()
    population = {(r["state"], int(r["year"])): int(r["women_15_49"]) for r in _rows("population_by_year.csv")}
    assert tx == [(2021, population[("TX", 2021)]), (2022, population[("TX", 2022)])]
