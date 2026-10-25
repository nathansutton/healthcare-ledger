"""graph-build on a hand-made warehouse: associate-id roll-up, interval semantics, and the hospital matching rules.

The graph reads only a few columns of five warehouse tables, so the fixture creates those columns directly
instead of loading CMS files.
"""
from datetime import date
from pathlib import Path

import duckdb
import pytest

from nppes_over_time.cli import main
from nppes_over_time.errors import UserError
from nppes_over_time.graph import build_graph

Q = ["2020-01-01", "2020-04-01", "2020-07-01"]


@pytest.fixture
def warehouse(tmp_path: Path) -> Path:
    db = tmp_path / "wh.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE ppef_snapshot (snapshot_id INTEGER, as_of_date DATE)")
    con.executemany("INSERT INTO ppef_snapshot VALUES (?, ?)", [(int(d.replace("-", "")), d) for d in Q])
    con.execute("""CREATE TABLE medicare_enrollment_version (enrollment_id VARCHAR, effective_date DATE, end_date DATE,
                   npi VARCHAR, pac_id VARCHAR, provider_type_code VARCHAR, state VARCHAR)""")
    enr = [
        # practitioner P1: two state enrollments, both reassign to group G (two enrollments) -> one edge
        ("I1", Q[0], None, "1000000001", "P1", "14-08", "MA"),
        ("I2", Q[0], None, "1000000001", "P1", "14-08", "NH"),
        # practitioner P2 reassigns to G in Q0 and Q2 only (lapse in Q1 -> two edge rows)
        ("I3", Q[0], None, "1000000002", "P2", "14-06", "MA"),
        ("OG1", Q[0], None, "2000000001", "G", "12-70", "MA"),
        ("OG2", Q[0], None, "2000000002", "G", "12-70", "NH"),
        # hospital system H: a hospital enrollment and a group enrollment under one associate id
        ("OH1", Q[0], None, "3000000001", "H", "00-09", "MA"),
        ("OH2", Q[0], None, "3000000002", "H", "12-70", "MA"),
        # a second hospital whose address matches two POS hospitals equally -> not linked
        ("OT1", Q[0], None, "4000000001", "T", "00-09", "MA"),
        # an organization that changes provider type in Q2 -> two organization rows
        ("OX1", Q[0], Q[2], "5000000001", "X", "12-70", "MA"),
        ("OX1", Q[2], None, "5000000001", "X", "12-Z1", "MA"),
    ]
    con.executemany("INSERT INTO medicare_enrollment_version VALUES (?, ?, ?, ?, ?, ?, ?)", enr)
    con.execute("""CREATE TABLE medicare_reassignment_version (enrollment_id VARCHAR, effective_date DATE, end_date DATE,
                   receiving_enrollment_id VARCHAR)""")
    con.executemany("INSERT INTO medicare_reassignment_version VALUES (?, ?, ?, ?)", [
        ("I1", Q[0], None, "OG1"), ("I2", Q[0], None, "OG2"),
        ("I3", Q[0], Q[1], "OG1"), ("I3", Q[2], None, "OG1"),
        ("I3", Q[0], None, "OH2"),
        ("I1", Q[0], None, "I3"),            # receiving side is an individual: dropped
        ("I9", Q[0], None, "OG1"),           # reassigning enrollment not in the file: dropped
    ])
    con.execute("""CREATE TABLE provider_version (npi VARCHAR, effective_date DATE, end_date DATE, org_name VARCHAR,
                   taxonomy_code_1 VARCHAR, practice_address_1 VARCHAR, practice_postal_code VARCHAR,
                   practice_state VARCHAR, practice_phone VARCHAR)""")
    con.executemany("INSERT INTO provider_version VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", [
        ("1000000001", "2019-01-01", None, None, "207Q00000X", "1 MAIN ST", "021381234", "MA", "6175550001"),
        ("1000000002", "2019-01-01", "2020-03-01", None, "207RC0000X", "2 MAIN ST", "02138", "MA", None),
        ("1000000002", "2020-03-01", None, None, "207RC0000X", "9 ELM ST", "02139", "MA", None),
        ("2000000001", "2019-01-01", None, "GROUP PRACTICE", None, "5 PARK AVE", "02138", "MA", None),
        ("3000000001", "2019-01-01", None, "GENERAL HOSPITAL", None, "100 HOSPITAL RD", "02114", "MA", "(617) 555-0100"),
        ("4000000001", "2019-01-01", None, "TWIN HOSPITAL", None, "7 RIVER RD", "01001", "MA", None),
        ("5000000001", "2019-01-01", None, "X LLC", None, "8 HILL ST", "01002", "MA", None),
    ])
    con.execute("""CREATE TABLE pos_facility_version (ccn VARCHAR, effective_date DATE, end_date DATE, prvdr_ctgry_cd VARCHAR,
                   pgm_trmntn_cd VARCHAR, fac_name VARCHAR, prvdr_ctgry_sbtyp_cd VARCHAR, state_cd VARCHAR, zip_cd VARCHAR,
                   fips_state_cd VARCHAR, fips_cnty_cd VARCHAR, crtfd_bed_cnt DOUBLE, gnrl_cntl_type_cd VARCHAR,
                   phne_num VARCHAR, st_adr VARCHAR)""")
    con.executemany("INSERT INTO pos_facility_version VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [
        ("220001", "2019-12-31", None, "01", "00", "GENERAL HOSP", "01", "MA", "02114", "25", "025", 400, "04", "6175550100", "100 HOSPITAL ROAD"),
        # a terminated hospital at the same address: never a candidate
        ("220999", "2019-12-31", None, "01", "07", "OLD GENERAL", "01", "MA", "02114", "25", "025", 0, "04", "6175550100", "100 HOSPITAL RD"),
        ("221001", "2019-12-31", None, "01", "00", "TWIN A", "01", "MA", "01001", "25", "013", 50, "04", None, "7 RIVER RD"),
        ("221002", "2019-12-31", None, "01", "00", "TWIN B", "11", "MA", "01001", "25", "013", 25, "04", None, "7 RIVER RD"),
        ("225000", "2019-12-31", None, "04", "00", "A NURSING HOME", None, "MA", "02114", "25", "025", 90, "04", "6175550100", "100 HOSPITAL RD"),
    ])
    con.close()
    return db


def test_reassignments_roll_up_to_associate_ids_with_intervals(warehouse, tmp_path):
    out = tmp_path / "g"
    build_graph(warehouse, out)
    rows = duckdb.sql(f"SELECT practitioner_pac_id, organization_pac_id, effective_date, end_date "
                      f"FROM '{out}/reassignment.parquet' ORDER BY ALL").fetchall()
    d = date.fromisoformat
    assert rows == [
        ("P1", "G", d(Q[0]), None),            # two enrollment pairs, one edge
        ("P2", "G", d(Q[0]), d(Q[1])),         # lapses in Q1 ...
        ("P2", "G", d(Q[2]), None),            # ... and returns as a new row
        ("P2", "H", d(Q[0]), None),
    ]


def test_quarter_counts_and_dropped_rows(warehouse, tmp_path):
    out = tmp_path / "g"
    m = build_graph(warehouse, out)
    q = duckdb.sql(f"SELECT as_of_date, n_reassignment, n_reassignment_rows, n_reassignment_dropped FROM read_csv('{out}/quarters.csv') ORDER BY 1").fetchall()
    assert [r[1:] for r in q] == [(3, 6, 2), (2, 5, 2), (3, 6, 2)]
    assert m["quarters"] == 3 and m["last_as_of_date"] == Q[2]
    # every table reproduces each quarter's count from its intervals
    for t in ("practitioner", "organization", "hospital", "reassignment", "hospital_link"):
        got = duckdb.sql(f"""SELECT count(t.effective_date) FROM read_csv('{out}/quarters.csv') s
                             LEFT JOIN '{out}/{t}.parquet' t ON t.effective_date <= s.as_of_date
                              AND (t.end_date IS NULL OR t.end_date > s.as_of_date)
                             GROUP BY s.as_of_date ORDER BY s.as_of_date""").fetchall()
        want = duckdb.sql(f"SELECT n_{t} FROM read_csv('{out}/quarters.csv') ORDER BY as_of_date").fetchall()
        assert got == want, t


def test_nodes_carry_nppes_attributes_as_of_each_quarter(warehouse, tmp_path):
    out = tmp_path / "g"
    build_graph(warehouse, out)
    p2 = duckdb.sql(f"SELECT practice_zip5, taxonomy_code, effective_date FROM '{out}/practitioner.parquet' "
                    f"WHERE pac_id = 'P2' ORDER BY effective_date").fetchall()
    assert [(z, t) for z, t, _ in p2] == [("02138", "207RC0000X"), ("02139", "207RC0000X")]
    p1 = duckdb.sql(f"SELECT npi, enrollment_states, practice_zip5 FROM '{out}/practitioner.parquet' WHERE pac_id = 'P1'").fetchall()
    assert p1 == [("1000000001", ["MA", "NH"], "02138")]
    orgs = {r[0]: r[1:] for r in duckdb.sql(
        f"SELECT pac_id, hospital, n_enrollments, org_name FROM '{out}/organization.parquet' WHERE end_date IS NULL").fetchall()}
    assert orgs["H"] == (True, 2, "GENERAL HOSPITAL")
    assert orgs["G"] == (False, 2, "GROUP PRACTICE")
    assert orgs["X"][0] is True                       # a hospital department enrollment from Q2
    x = duckdb.sql(f"SELECT hospital, effective_date FROM '{out}/organization.parquet' WHERE pac_id = 'X' ORDER BY 2").fetchall()
    assert [h for h, _ in x] == [False, True]


def test_hospital_link_uses_active_hospitals_and_drops_ties(warehouse, tmp_path):
    out = tmp_path / "g"
    build_graph(warehouse, out)
    links = duckdb.sql(f"SELECT enrollment_id, organization_pac_id, ccn, match_rule, effective_date, end_date "
                       f"FROM '{out}/hospital_link.parquet'").fetchall()
    # phone and street number match the active hospital; the terminated one at the same address and the
    # nursing home are not candidates; the twin hospitals tie on rule 2 and are left unlinked
    assert links == [("OH1", "H", "220001", 1, date.fromisoformat(Q[0]), None)]
    hosp = duckdb.sql(f"SELECT ccn FROM '{out}/hospital.parquet' ORDER BY 1").fetchall()
    assert hosp == [("220001",), ("221001",), ("221002",)]


def test_cli_graph_build(warehouse, tmp_path, capsys):
    out = tmp_path / "g"
    assert main(["graph-build", "--db", str(warehouse), "--out", str(out), "--work", str(tmp_path / "w")]) == 0
    assert "3 quarters" in capsys.readouterr().out
    assert (out / "manifest.json").is_file()


def test_missing_tables_are_a_user_error(tmp_path):
    db = tmp_path / "empty.duckdb"
    duckdb.connect(str(db)).close()
    with pytest.raises(UserError, match="lacks"):
        build_graph(db, tmp_path / "g")


STUDY = Path(__file__).resolve().parent.parent / "research" / "provider-networks"
NOTEBOOK = STUDY / "provider_networks.ipynb"


def test_notebook_sql_runs_on_a_fixture_graph(warehouse, tmp_path):
    """Every SQL block of the provider-networks notebook, in order, against the fixture warehouse's graph."""
    from conftest import notebook_sql, study_con
    out = tmp_path / "g"
    build_graph(warehouse, out)
    con = study_con(STUDY)
    for t in ("practitioner", "organization", "hospital", "reassignment", "hospital_link"):
        con.execute(f"CREATE VIEW {t} AS SELECT * FROM read_parquet('{out}/{t}.parquet')")
    con.execute(f"CREATE VIEW quarters AS SELECT * FROM read_csv('{out}/quarters.csv')")
    con.execute("CREATE TABLE hrr AS SELECT * FROM read_csv('data/zip_hrr_2019.csv', all_varchar = true)")
    # 2019 Medicaid: P2 (NPI 1000000002) bills more through H's group NPI than through G's; self-billing is ignored
    con.execute("CREATE TABLE medicaid_spending (billing_npi VARCHAR, servicing_npi VARCHAR, month DATE, paid DOUBLE)")
    con.executemany("INSERT INTO medicaid_spending VALUES (?, ?, ?, ?)", [
        ("3000000002", "1000000002", "2019-06-01", 500.0), ("2000000001", "1000000002", "2019-03-01", 100.0),
        ("1000000002", "1000000002", "2019-03-01", 9000.0)])
    blocks = notebook_sql(NOTEBOOK, queries=True)
    assert len(blocks) >= 10
    for sql in blocks:
        con.execute(sql)
    # the fixture's physicians: P1 (family practice) in G only, P2 (cardiology) in G and H in Q0 and Q2
    edges = con.execute("SELECT as_of_date, practitioner_pac_id, organization_pac_id, k FROM physician_edge ORDER BY ALL").fetchall()
    assert [(p, o, k) for d, p, o, k in edges if d == date.fromisoformat(Q[1])] == [("P1", "G", 1), ("P2", "H", 1)]
    shares = con.execute("""SELECT "9 or fewer" FROM size_shares WHERE weighting = 'fractional' ORDER BY as_of_date""").fetchall()
    assert all(s == (100.0,) for s in shares)
    hhi = con.execute("SELECT DISTINCT specialty FROM market_hhi ORDER BY 1").fetchall()
    assert hhi == [("cardiology",), ("primary care",)]
    # 2019 volume ranks the reassignments of the first quarter on or after 2019-12-31: P2 goes to H with 5/6 of it
    main = con.execute("SELECT as_of_date, practitioner_pac_id, organization_pac_id, share FROM main_group ORDER BY ALL").fetchall()
    assert main == [(date.fromisoformat(Q[0]), "P1", "G", 1.0), (date.fromisoformat(Q[0]), "P2", "H", 500 / 600)]
    weights = con.execute("""SELECT weighting, practitioner_pac_id, organization_pac_id, wt FROM weighted_edge
                             WHERE weighting LIKE 'Medicaid%' ORDER BY ALL""").fetchall()
    assert [w for w in weights if w[0] == "Medicaid plurality"] == [("Medicaid plurality", "P1", "G", 1.0),
                                                                   ("Medicaid plurality", "P2", "H", 1.0)]


def test_reference_data():
    from conftest import study_rows
    hrr = study_rows(STUDY, "zip_hrr_2019.csv")
    assert len(hrr) > 40_000 and len({r["hrr"] for r in hrr}) == 306
    assert all(len(r["zip5"]) == 5 for r in hrr)
    enr = study_rows(STUDY, "hospital_enrollments_2026-09-29.csv")
    assert len({r["enrollment_id"] for r in enr}) == len(enr) > 9_000
    assert {r["provider_type_code"] for r in enr} == {"00-09", "00-85", "00-24"}
