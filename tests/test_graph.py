"""graph-build on a hand-made warehouse: associate-id roll-up, interval semantics, the hospital matching rules,
and the annual system tables from a hand-made AHRQ Compendium directory.

The graph reads only a few columns of five warehouse tables, so the fixture creates those columns directly
instead of loading CMS files.
"""
from datetime import date
from pathlib import Path

import duckdb
import pytest

from nppes_over_time import compendium as comp
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


GP_HEADER = ("tin_name,pecos_pac_ids,health_sys_id,health_sys_name,tin_name_md_ppas,state_md_ppas,md_do_md_ppas,"
             "np_pa_md_ppas,service_lines_md_ppas,ma_only_tin")


@pytest.fixture
def compendium(tmp_path: Path) -> Path:
    """Every file graph-build reads, one tiny CSV each, with AHRQ's quirks: Latin-1 with bare CR line endings,
    multiple associate ids joined by ^, and practices AHRQ lists outside any system."""
    d = tmp_path / "ahrq"
    for kind, files in comp.FILES.items():
        for year, name in files.items():
            if kind == "system":
                text = ("health_sys_id,health_sys_name,health_sys_city,health_sys_state,total_mds,prim_care_mds,grp_cnt,"
                        "hosp_cnt,acutehosp_cnt,sys_multistate,sys_beds\n"
                        "HSI1,General Health,Boston,MA,\"1,200\",300,4,1,1,0,400\n")
            elif kind == "group_practice":
                # H (the hospital's group) is in the system; G and the two-id practice Y^Z are not
                text = (f"{GP_HEADER}\nGENERAL HOSPITAL,H,HSI1,General Health,GENERAL HOSPITAL,MA,30,2,900,0\n"
                        f"GROUP PRACTICE,G,,,GROUP PRACTICE,MA,2,0,40,0\n"
                        f"CL\xc9MENT PARTNERS,Y^Z,,,CL\xc9MENT PARTNERS,MA,3,0,50,0\n")
            elif kind == "hospital":
                text = ("compendium_hospital_id,ccn,hospital_name,hospital_street,hospital_city,hospital_state,hospital_zip,"
                        "acutehosp_flag,health_sys_id,health_sys_name,health_sys_city,health_sys_state\n"
                        "CHSP1,220001,General Hospital,100 Hospital Rd,Boston,MA,02114,1,HSI1,General Health,Boston,MA\n"
                        "CHSP2,221001,Twin A,7 River Rd,Springfield,MA,01001,1,,,,\n")
            else:
                node, row_id, ccn, name_col = comp.LINKS[kind]
                text = (f"{row_id},onekey_id,{ccn},{name_col},health_sys_id,health_sys_name\n"
                        f"{kind[:2].upper()}1,1,225000,A FACILITY,,\n")
            path = d / "raw" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if year == 2022 and kind == "group_practice":
                path.write_bytes(text.replace("\n", "\r").encode("latin-1"))
            else:
                path.write_text(text)
    return d


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


def test_compendium_tables(warehouse, compendium, tmp_path):
    out = tmp_path / "g"
    m = build_graph(warehouse, out, compendium=compendium)
    assert m["compendium"]["years"]["group_practice"] == [2016, 2018, 2020, 2021, 2022]
    assert m["tables"]["system_link"]["annual"] is True
    system = duckdb.sql(f"SELECT DISTINCT health_sys_id, total_mds, sys_beds FROM '{out}/system.parquet'").fetchall()
    assert system == [("HSI1", 1200, 400.0)]                 # "1,200" read as a number
    links = duckdb.sql(f"""SELECT node_type, node_id, health_sys_id, md_ppas_physicians, name
                           FROM '{out}/system_link.parquet' WHERE year = 2022 ORDER BY 1, 2""").fetchall()
    assert links == [
        ("home_health", "225000", None, None, "A FACILITY"),
        ("hospital", "220001", "HSI1", None, "General Hospital"),
        ("hospital", "221001", None, None, "Twin A"),
        ("nursing_home", "225000", None, None, "A FACILITY"),
        ("organization", "G", None, 2, "GROUP PRACTICE"),
        ("organization", "H", "HSI1", 30, "GENERAL HOSPITAL"),
        ("organization", "Y", None, 3, "CLÉMENT PARTNERS"),     # Latin-1, CR line endings, two ids for one tax id
        ("organization", "Z", None, 3, "CLÉMENT PARTNERS"),
    ]
    ids = duckdb.sql(f"""SELECT count(DISTINCT compendium_id) FROM '{out}/system_link.parquet'
                         WHERE year = 2022 AND node_id IN ('Y', 'Z')""").fetchone()
    assert ids == (1,)
    years = duckdb.sql(f"SELECT DISTINCT year FROM '{out}/system_link.parquet' WHERE node_type = 'nursing_home' ORDER BY 1").fetchall()
    assert years == [(2020,), (2021,), (2022,), (2023,)]


def test_compendium_missing_files_are_a_user_error(warehouse, compendium, tmp_path):
    (compendium / "raw" / comp.FILES["hospital"][2023]).unlink()
    with pytest.raises(UserError, match="compendium-download"):
        build_graph(warehouse, tmp_path / "g", compendium=compendium)
    assert not (tmp_path / "g").exists()                        # checked before the build starts


def test_compendium_download_falls_back_to_the_archive(tmp_path, monkeypatch):
    import gzip
    import json
    index = json.dumps([["original", "timestamp"],
                        [comp.BASE + "a.csv", "20240101000000"], [comp.BASE + "b.csv", "20250101000000"]]).encode()
    calls = []

    def fake_get(url, timeout=300):
        calls.append(url)
        if url.startswith("https://web.archive.org/cdx/"):
            return 200, index
        if url == comp.BASE + "a.csv":
            return 200, b"x,y\n1,2\n"
        if url.startswith(comp.BASE):
            return 202, b""                                     # ahrq.gov's answer to scripts, sometimes
        if "20250101000000id_/" + comp.BASE + "b.csv" in url:
            return 200, gzip.compress(b"b,c\n3,4\n")             # a capture stored with its gzip encoding
        return 404, b""

    monkeypatch.setattr(comp, "_get", fake_get)
    monkeypatch.setattr(comp, "KNOWN_FILES", ("never-captured.csv",))
    rows = {r["name"]: r for r in comp.download(tmp_path, pause=0)}
    assert (tmp_path / "raw" / "a.csv").read_bytes() == b"x,y\n1,2\n"
    assert (tmp_path / "raw" / "b.csv").read_bytes() == b"b,c\n3,4\n"
    assert rows["b.csv"]["source"].startswith("web.archive.org 20250101000000")
    assert rows["never-captured.csv"]["bytes"] == 0
    assert (tmp_path / "manifest.csv").is_file()
    # a second run keeps what is there
    calls.clear()
    comp.download(tmp_path, pause=0)
    assert not any(c.endswith(("a.csv", "b.csv")) for c in calls)


STUDY = Path(__file__).resolve().parent.parent / "research" / "provider-networks"
NOTEBOOK = STUDY / "provider_networks.ipynb"


def test_notebook_sql_runs_on_a_fixture_graph(warehouse, compendium, tmp_path):
    """Every SQL block of the provider-networks notebook, in order, against the fixture warehouse's graph."""
    from conftest import notebook_sql, study_con
    out = tmp_path / "g"
    build_graph(warehouse, out, compendium=compendium)
    con = study_con(STUDY)
    for t in ("practitioner", "organization", "hospital", "reassignment", "hospital_link", "system", "system_link"):
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
    # group size counts each physician once: in Q1, P2 reassigns only to H; in Q0 and Q2 G has P1 and half of P2
    sizes = con.execute("SELECT as_of_date, organization_pac_id, headcount, physicians FROM group_size ORDER BY ALL").fetchall()
    assert (date.fromisoformat(Q[0]), "G", 2, 1.5) in sizes and (date.fromisoformat(Q[1]), "G", 1, 1.0) in sizes
    # section 6: each Compendium year reads the nearest quarter; H is in a system through its own tax id and,
    # through its hospital enrollment's CCN, through the hospital file too; G is not
    routes = con.execute("SELECT DISTINCT pac_id, route FROM system_org ORDER BY ALL").fetchall()
    assert routes == [("H", "group practice file"), ("H", "hospital file")]
    shares = con.execute("""SELECT practitioner_pac_id, health_sys_id FROM ahrq_edge
                            WHERE year = 2022 ORDER BY ALL""").fetchall()
    assert shares == [("P1", None), ("P2", "HSI1"), ("P2", None)]


def test_reference_data():
    from conftest import study_rows
    hrr = study_rows(STUDY, "zip_hrr_2019.csv")
    assert len(hrr) > 40_000 and len({r["hrr"] for r in hrr}) == 306
    assert all(len(r["zip5"]) == 5 for r in hrr)
    enr = study_rows(STUDY, "hospital_enrollments_2026-09-29.csv")
    assert len({r["enrollment_id"] for r in enr}) == len(enr) > 9_000
    assert {r["provider_type_code"] for r in enr} == {"00-09", "00-85", "00-24"}
