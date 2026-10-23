"""The Medicaid ghost-physician study in research/medicaid-ghost-physicians: its code list, and the SQL in its notebook.

The tables are built by the triple-quoted SQL passed to ``con.execute`` in the notebook's code cells, and the
result tables by ``con.sql``. The tests pull that SQL out of the notebook (conftest.notebook_sql) and run it, so
they check the SQL a reader sees. The HHS files are stood in for by two small Parquet files; one test runs the
notebook's first cell on two small CSVs and checks that it writes the same columns and types.
"""
import csv
import json
import os
import re
from datetime import date
from pathlib import Path

import duckdb
import pytest

from conftest import main_row, notebook_sql, sql_creating, write_zip
from nppes_over_time.cli import main

STUDY = Path(__file__).resolve().parent.parent / "research" / "medicaid-ghost-physicians"
DATA = STUDY / "data"
NOTEBOOK = STUDY / "medicaid_ghost_physicians.ipynb"


def _con() -> duckdb.DuckDBPyConnection:
    """An in-memory DuckDB that resolves data/ paths against the study directory."""
    con = duckdb.connect()
    con.execute("SET file_search_path = ?", [str(STUDY)])
    return con


def test_specialty_codes_are_nucc_patterns():
    with open(DATA / "specialty_taxonomy.csv", newline="") as f:
        rows = list(csv.DictReader(f))
    assert {r["specialty"] for r in rows} == {"primary care", "cardiology", "dermatology", "ophthalmology", "psychiatry"}
    for r in rows:
        assert re.fullmatch(r"[0-9A-Z]{10}|[0-9A-Z]{4}%", r["pattern"]), r
        assert r["description"], r
    patterns = [r["pattern"] for r in rows]
    assert len(patterns) == len(set(patterns))


def test_spells_merge_touching_and_overlapping_segments():
    """Monthly segments become one spell; a gap of more than a day starts a new one; non-active statuses are dropped;
    a blank start date is an open start."""
    con = _con()
    con.execute("""
    CREATE VIEW segments AS SELECT * FROM (VALUES
        -- A in CO: three monthly segments, then a gap, then one more, then a termination record
        ('1000000001', 'CO', DATE '2019-01-01', DATE '2019-01-31', '03', '03'),
        ('1000000001', 'CO', DATE '2019-02-01', DATE '2019-02-28', '03', '03'),
        ('1000000001', 'CO', DATE '2019-03-01', DATE '2019-03-31', '03', '03'),
        ('1000000001', 'CO', DATE '2019-06-01', NULL, '03', '03'),
        ('1000000001', 'CO', DATE '2019-04-01', DATE '2019-05-31', '76', '03'),
        -- A in TX: overlapping segments, and a Medicaid and a CHIP row with the same dates
        ('1000000001', 'TX', DATE '2018-01-01', DATE '2020-12-31', '03', '03'),
        ('1000000001', 'TX', DATE '2018-01-01', DATE '2020-12-31', '03', '03'),
        ('1000000001', 'TX', DATE '2020-01-01', DATE '2021-06-30', '01', '03'),
        -- C: a blank start, kept as an open start
        ('1000000003', 'CO', NULL, NULL, '03', '03'),
        -- no NPI, and an inverted segment, both dropped
        (NULL, 'CO', DATE '2019-01-01', NULL, '03', '03'),
        ('1000000002', 'CO', DATE '2020-01-01', DATE '2019-01-01', '03', '03')
    ) v(npi, state, enrl_start, enrl_end, status_code, provider_type)
    """)
    con.execute(sql_creating(NOTEBOOK, "spells"))
    spells = con.execute("SELECT state, s, e, segments FROM spells WHERE npi = '1000000001' ORDER BY state, s").fetchall()
    assert spells == [
        ("CO", date(2019, 1, 1), date(2019, 3, 31), 3),
        ("CO", date(2019, 6, 1), date(2024, 12, 31), 1),
        ("TX", date(2018, 1, 1), date(2021, 6, 30), 3),
    ]
    assert con.execute("SELECT state, s, e FROM spells WHERE npi = '1000000003'").fetchall() == [("CO", date(1, 1, 1), date(2024, 12, 31))]
    assert con.execute("SELECT count(*) FROM spells WHERE npi IS NULL OR npi = '1000000002'").fetchone()[0] == 0


@pytest.fixture(scope="module")
def fixture_dirs(tmp_path_factory) -> tuple[Path, Path]:
    """A two-snapshot export with three physicians and a student, and HHS files that enroll the physicians."""
    tmp = tmp_path_factory.mktemp("ghosts")
    src = tmp / "zips"
    src.mkdir()
    pcp = dict(entity_type="1", last_name="A", first_name="B", taxonomy_code_1="207Q00000X", primary_taxonomy_1="Y", enumeration_date="01/01/2010")
    pcp_md = dict(pcp, practice_state="MD", practice_postal_code="212011234")
    pcp_pa = dict(pcp, practice_state="PA", practice_postal_code="171011234")
    pcp_tx = dict(pcp, practice_state="TX", practice_postal_code="770021234")
    psych_tx = dict(pcp_tx, taxonomy_code_1="2084P0800X")
    student = dict(pcp_md, taxonomy_code_2="390200000X")
    rows = lambda one: [main_row("1000000001", **one), main_row("1000000002", **psych_tx), main_row("1000000003", **student), main_row("1000000004", **pcp_tx)]
    write_zip(src, "20210701", "20210711", rows(pcp_md))
    write_zip(src, "20220701", "20220710", rows(pcp_pa))
    db = tmp / "w.duckdb"
    assert main(["load", "--source", str(src), "--db", str(db), "--work", str(tmp / "work")]) == 0
    out = tmp / "export"
    assert main(["export", "--db", str(db), "--out", str(out), "--releases", str(tmp / "releases")]) == 0

    hhs = tmp / "hhs" / "parquet"
    hhs.mkdir(parents=True)
    con = duckdb.connect()
    con.execute(f"""
    COPY (SELECT * FROM (VALUES
        ('1000000001', 'MD', DATE '2015-01-01', NULL,              '03', 'ACTIVE - ACTIVE', '1', 'MEDICAID', '03', 'INDIVIDUAL'),
        ('1000000001', 'PA', DATE '2022-03-01', NULL,              '03', 'ACTIVE - ACTIVE', '1', 'MEDICAID', '03', 'INDIVIDUAL'),
        ('1000000002', 'TX', DATE '2018-01-01', DATE '2021-06-30', '03', 'ACTIVE - ACTIVE', '1', 'MEDICAID', '03', 'INDIVIDUAL'),
        ('1000000002', 'TX', DATE '2021-07-01', NULL,              '76', 'TERM - NO CLAIMS ACTIVITY', '1', 'MEDICAID', '03', 'INDIVIDUAL'),
        ('1000000004', 'TX', DATE '2018-01-01', NULL,              '03', 'ACTIVE - ACTIVE', '1', 'MEDICAID', '03', 'INDIVIDUAL'),
        (NULL,         'OR', DATE '2018-01-01', NULL,              '03', 'ACTIVE - ACTIVE', '1', 'MEDICAID', '03', 'INDIVIDUAL')
    ) v(npi, state, enrl_start, enrl_end, status_code, status_desc, plan_code, plan_desc, provider_type, provider_type_desc))
    TO '{hhs / "enrollment_segments.parquet"}' (FORMAT parquet)""")
    con.execute(f"""
    COPY (SELECT * FROM (VALUES
        ('1000000009', '1000000001', '99213', DATE '2021-03-01', 40::BIGINT, 55::BIGINT, 4000.0::DOUBLE),
        ('1000000009', '1000000001', '99214', DATE '2021-04-01', 120::BIGINT, 130::BIGINT, 15000.0::DOUBLE),
        ('1000000009', '1000000001', '993AB', DATE '2021-05-01', 500::BIGINT, 500::BIGINT, 900.0::DOUBLE),
        ('1000000002', NULL,         '90837', DATE '2021-05-01', 12::BIGINT, 12::BIGINT, 1200.0::DOUBLE)
    ) v(billing_npi, servicing_npi, hcpcs, month, patients, claim_lines, paid))
    TO '{hhs / "spending.parquet"}' (FORMAT parquet)""")
    return out, hhs.parent


def test_notebook_sql_runs_end_to_end_on_fixtures(fixture_dirs):
    export, hhs = fixture_dirs
    con = _con()
    con.execute("SET VARIABLE export = ?", [str(export)])
    con.execute("SET VARIABLE hhs = ?", [str(hhs)])
    for block in notebook_sql(NOTEBOOK, queries=True):
        con.execute(block)

    # the student is excluded; the three physicians are in both July snapshots
    assert con.execute("SELECT count(*), count(DISTINCT npi) FROM physician_year").fetchone() == (6, 3)
    assert con.execute("SELECT specialty FROM physician_year WHERE npi = '1000000002' LIMIT 1").fetchone() == ("psychiatry",)
    # Oregon has no NPIs; the three states with physicians pass both screens
    assert con.execute("SELECT state FROM state_screen WHERE NOT usable ORDER BY state").fetchall() == [("OR",)]
    assert con.execute("SELECT count(*) FROM usable_states").fetchone()[0] == 3
    # 2021: the PCP is enrolled all year in MD with 160 visible E&M patients (the 993AB cell is not E&M);
    # the psychiatrist's spell ends mid-year
    rows = {r[0]: r[1:] for r in con.execute("""
        SELECT e.npi, e.enrolled_full_year, e.enrolled_anywhere, p.patient_cell_sum
        FROM enrollment_by_year e JOIN participation p USING (npi, year) WHERE year = 2021""").fetchall()}
    assert rows["1000000001"] == (True, True, 160)
    assert rows["1000000002"] == (False, True, None)
    for table in ("segment_status", "state_npi_coverage", "visible_em"):
        con.execute(f"SELECT count(*) FROM {table}").fetchone()


def test_first_cell_converts_the_hhs_csvs(fixture_dirs, tmp_path, monkeypatch):
    """The notebook's first cell writes Parquet with the fixture's columns and types, and rebuilds only when a CSV is newer."""
    pytest.importorskip("matplotlib")
    export, fixture_hhs = fixture_dirs
    hhs = tmp_path / "hhs"
    hhs.mkdir()
    (hhs / "medicaid-provider-enrollment-segments.csv").write_text(
        "NPI,STATE_CD,ENRL_START_DT,ENRL_END_DT,ENRL_STATUS_CD,ENRL_STATUS_DESC,ENRL_PLAN_CD,ENRL_PLAN_DESC,PRVDR_TYPE,PRVDR_TYPE_DESC\n"
        "1000000001,MD,2015-01-01,,03,ACTIVE - ACTIVE,1,MEDICAID,03,\"INDIVIDUAL, PRACTITIONER\"\n"
        "1000000002,TX,not a date,2021-06-30,76,TERM - NO CLAIMS ACTIVITY,1,MEDICAID,03,INDIVIDUAL\n")
    (hhs / "medicaid-provider-spending.csv").write_text(
        "BILLING_PROVIDER_NPI_NUM,SERVICING_PROVIDER_NPI_NUM,HCPCS_CODE,CLAIM_FROM_MONTH,TOTAL_PATIENTS,TOTAL_CLAIM_LINES,TOTAL_PAID\n"
        "1000000009,1000000001,99213,2021-03,40,55,4000.5\n"
        ",1000000002,0001U,2021-04,12,12,10\n")
    monkeypatch.setenv("NPPES_EXPORT", str(export))
    monkeypatch.setenv("HHS_MEDICAID", str(hhs))
    [first] = [c for c in json.loads(NOTEBOOK.read_text())["cells"] if c["cell_type"] == "code" and "HHS_MEDICAID" in "".join(c["source"])]

    def run_first_cell() -> duckdb.DuckDBPyConnection:
        scope: dict = {}
        exec("".join(first["source"]), scope)
        return scope["con"]

    con = run_first_cell()
    for name in ("enrollment_segments", "spending"):
        made, fixture = (con.execute(f"DESCRIBE SELECT * FROM read_parquet('{d}/parquet/{name}.parquet')").fetchall()
                         for d in (hhs, fixture_hhs))
        assert [r[:2] for r in made] == [r[:2] for r in fixture], name
    assert not list((hhs / "parquet").glob("*.tmp"))
    assert con.execute("SELECT npi, enrl_start, enrl_end, provider_type_desc FROM segments ORDER BY npi").fetchall() == [
        ("1000000001", date(2015, 1, 1), None, "INDIVIDUAL, PRACTITIONER"),
        ("1000000002", None, date(2021, 6, 30), "INDIVIDUAL"),
    ]
    assert con.execute("SELECT billing_npi, month, paid FROM spending ORDER BY month").fetchall() == [
        ("1000000009", date(2021, 3, 1), 4000.5), (None, date(2021, 4, 1), 10.0)]

    spending = hhs / "parquet" / "spending.parquet"
    built = spending.stat().st_mtime_ns
    run_first_cell()
    assert spending.stat().st_mtime_ns == built
    csv_path = hhs / "medicaid-provider-spending.csv"
    later = spending.stat().st_mtime + 60
    os.utime(csv_path, (later, later))
    run_first_cell()
    assert spending.stat().st_mtime_ns > built
