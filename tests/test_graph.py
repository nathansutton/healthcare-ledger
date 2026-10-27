"""graph-build on a hand-made warehouse: associate-id roll-up, interval semantics, the hospital matching rules,
and the annual system tables from the AHRQ source loaded from hand-made Compendium files.

The graph reads only a few columns of five warehouse tables, so the fixture fills those columns directly
instead of loading CMS files; the tables themselves have the loaders' schemas, which the published views
(``core.published``) the graph reads are declared over.
"""
from datetime import date
from pathlib import Path

import duckdb
import pytest

from healthcare_ledger import sources
from healthcare_ledger.cli import main
from healthcare_ledger.core.errors import UserError
from healthcare_ledger.derived.graph import build_graph
from healthcare_ledger.sources.ahrq.load import load

Q = ["2020-01-01", "2020-04-01", "2020-07-01"]


@pytest.fixture
def warehouse(tmp_path: Path) -> Path:
    db = tmp_path / "warehouse"
    for name in ("nppes", "ppef", "pos"):     # each source's file, with its loader's empty tables
        sources.by_name(name).warehouse_cls(db).close()
    con = duckdb.connect()
    for name in ("nppes", "ppef", "pos"):
        con.execute(f"ATTACH '{db / name}.duckdb' AS {name}")
    con.executemany("INSERT INTO ppef.ppef_snapshot (snapshot_id, as_of_date, directory, loaded_at) VALUES (?, ?, ?, now())",
                    [(int(d.replace("-", "")), d, d) for d in Q])
    # the columns the graph reads, filled here and copied by name into the source files at the end
    con.execute("USE memory")
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
        # a skilled nursing facility at the hospital's address, and a home health agency
        ("OS1", Q[0], None, "6000000001", "S", "00-18", "MA"),
        ("OA1", Q[0], None, "7000000001", "A", "00-06", "MA"),
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
    con.execute("""CREATE TABLE medicare_additional_npi_version (enrollment_id VARCHAR, effective_date DATE, end_date DATE,
                   npi VARCHAR)""")
    # the hospital enrollment's further NPI lapses in Q1 and returns in Q2
    con.executemany("INSERT INTO medicare_additional_npi_version VALUES (?, ?, ?, ?)", [
        ("OH1", Q[0], Q[1], "3000000009"), ("OH1", Q[2], None, "3000000009")])
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
        ("6000000001", "2019-01-01", None, "A NURSING HOME", None, "100 HOSPITAL RD", "02114", "MA", "6175550100"),
        ("7000000001", "2019-01-01", None, "HOME CARE A", None, "3 OAK ST", "01003", "MA", "4135550000"),
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
        # a home health agency that CMS moves to the iQIES file in Q2
        ("227001", "2019-12-31", Q[2], "05", "00", "HOME CARE A", "01", "MA", "01003", "25", "015", None, "04", "4135550000", "3 OAK ST"),
    ])
    con.execute("""CREATE TABLE pos_facility_iqies_version (ccn VARCHAR, effective_date DATE, end_date DATE, prvdr_type_id VARCHAR,
                   prvdr_sbtyp_id VARCHAR, pgm_trmntn_cd VARCHAR, fac_name VARCHAR, state_cd VARCHAR, zip_cd VARCHAR,
                   fips_state_cd VARCHAR, fips_cnty_cd VARCHAR, crtfd_bed_cnt DOUBLE, gnrl_cntl_type_cd VARCHAR,
                   phne_num VARCHAR, st_adr VARCHAR)""")
    # the first iQIES file leaves the termination code of active agencies blank
    con.execute("INSERT INTO pos_facility_iqies_version VALUES ('227001', ?, NULL, '3', NULL, NULL, 'HOME CARE A', 'MA', "
                "'01003', '25', '015', NULL, NULL, '4135550000', '3 OAK ST')", [Q[2]])
    for name, tables in {"ppef": ("medicare_enrollment_version", "medicare_reassignment_version",
                                  "medicare_additional_npi_version"),
                         "nppes": ("provider_version",), "pos": ("pos_facility_version", "pos_facility_iqies_version")}.items():
        for t in tables:
            con.execute(f"INSERT INTO {name}.{t} BY NAME SELECT * FROM memory.{t}")
    con.close()
    return db


@pytest.fixture
def compendium(warehouse: Path, tmp_path: Path) -> Path:
    """The AHRQ source in the fixture warehouse, loaded from conftest.write_ahrq's tiny files (Latin-1 with bare CR
    line endings, multiple associate ids joined by ^, practices AHRQ lists outside any system)."""
    from conftest import write_ahrq
    load(write_ahrq(tmp_path / "ahrq"), warehouse, tmp_path / "work")
    return warehouse


@pytest.fixture
def medicaid(warehouse: Path) -> Path:
    """The Medicaid source in the fixture warehouse: 2019 Medicaid for P2 (NPI 1000000002), who bills more through H's
    group NPI than through G's; self-billing is ignored."""
    from healthcare_ledger.sources.medicaid.load import COLUMNS
    con = duckdb.connect(str(warehouse / "medicaid.duckdb"))
    con.execute("CREATE TABLE medicaid_spending (billing_npi VARCHAR, servicing_npi VARCHAR, hcpcs VARCHAR, month DATE, "
                "patients BIGINT, claim_lines BIGINT, paid DOUBLE)")
    assert [r[0] for r in con.execute("DESCRIBE medicaid_spending").fetchall()] == list(COLUMNS)
    con.executemany("INSERT INTO medicaid_spending VALUES (?, ?, '99213', ?, 12, 12, ?)", [
        ("3000000002", "1000000002", "2019-06-01", 500.0), ("2000000001", "1000000002", "2019-03-01", 100.0),
        ("1000000002", "1000000002", "2019-03-01", 9000.0)])
    con.close()
    return warehouse


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
    for t in ("practitioner", "organization", "hospital", "reassignment", "hospital_link", "post_acute_facility", "post_acute_link"):
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


def test_post_acute_nodes_follow_the_move_to_iqies(warehouse, tmp_path):
    out = tmp_path / "g"
    build_graph(warehouse, out)
    q = [date.fromisoformat(d) for d in Q]
    per_quarter = duckdb.sql(f"""SELECT s.as_of_date, f.ccn, f.facility_type FROM read_csv('{out}/quarters.csv') s
                                 JOIN '{out}/post_acute_facility.parquet' f ON f.effective_date <= s.as_of_date
                                  AND (f.end_date IS NULL OR f.end_date > s.as_of_date) ORDER BY ALL""").fetchall()
    # the agency is a node in every quarter: QIES until Q2, iQIES (blank termination code) from Q2
    assert per_quarter == [(d, c, t) for d in q for c, t in (("225000", "nursing_home"), ("227001", "home_health"))]
    subtypes = duckdb.sql(f"SELECT subtype FROM '{out}/post_acute_facility.parquet' WHERE ccn = '227001' ORDER BY effective_date").fetchall()
    assert subtypes == [("05-01",), ("iqies 3-",)]
    links = duckdb.sql(f"""SELECT enrollment_id, organization_pac_id, ccn, facility_type, match_rule, end_date
                           FROM '{out}/post_acute_link.parquet' ORDER BY 1""").fetchall()
    # the nursing home shares the hospital's address and phone; only facilities of the enrollment's type are candidates
    assert links == [("OA1", "A", "227001", "home_health", 1, None), ("OS1", "S", "225000", "nursing_home", 1, None)]


def test_crosswalk_reshapes_the_graph_tables(warehouse, compendium, tmp_path):
    out = tmp_path / "g"
    m = build_graph(warehouse, out, compendium=True)
    d = date.fromisoformat
    rows = duckdb.sql(f"""SELECT * FROM '{out}/crosswalk.parquet'
                          WHERE from_id IN ('1000000001', 'I1', 'I2', '2000000001', '2000000002', 'OG1', 'OG2',
                                            '3000000009', 'OH1', 'H', '220001', 'OX1')
                          ORDER BY ALL""").fetchall()
    assert m["tables"]["crosswalk"]["rows"] == duckdb.sql(f"SELECT count(*) FROM '{out}/crosswalk.parquet'").fetchone()[0]
    years = (2016, 2018, 2020, 2021, 2022, 2023)
    system = [(y, nxt, "carried_forward" if y == 2023 else "registry") for y, nxt in zip(years, (*years[1:], None))]
    assert rows == sorted([
        # one practitioner, two enrollments, one NPI
        ("enrollment", "I1", "pac", "P1", d(Q[0]), None, "ppef_enrollment", "registry"),
        ("enrollment", "I2", "pac", "P1", d(Q[0]), None, "ppef_enrollment", "registry"),
        ("npi", "1000000001", "enrollment", "I1", d(Q[0]), None, "ppef_enrollment", "registry"),
        ("npi", "1000000001", "enrollment", "I2", d(Q[0]), None, "ppef_enrollment", "registry"),
        # one organization with two NPIs
        ("enrollment", "OG1", "pac", "G", d(Q[0]), None, "ppef_enrollment", "registry"),
        ("enrollment", "OG2", "pac", "G", d(Q[0]), None, "ppef_enrollment", "registry"),
        ("npi", "2000000001", "enrollment", "OG1", d(Q[0]), None, "ppef_enrollment", "registry"),
        ("npi", "2000000002", "enrollment", "OG2", d(Q[0]), None, "ppef_enrollment", "registry"),
        # the hospital: its further NPI lapses and returns, its CCN by rule 1, its associate id and CCN in a system
        ("enrollment", "OH1", "ccn", "220001", d(Q[0]), None, "hospital_link", "match_rule_1"),
        ("enrollment", "OH1", "pac", "H", d(Q[0]), None, "ppef_enrollment", "registry"),
        ("npi", "3000000009", "enrollment", "OH1", d(Q[0]), d(Q[1]), "ppef_additional_npi", "registry"),
        ("npi", "3000000009", "enrollment", "OH1", d(Q[2]), None, "ppef_additional_npi", "registry"),
        *[("pac", "H", "system", "HSI1", date(y, 1, 1), nxt and date(nxt, 1, 1), "ahrq_group_practice_linkage", method)
          for y, nxt, method in system],
        *[("ccn", "220001", "system", "HSI1", date(y, 1, 1), nxt and date(nxt, 1, 1), "ahrq_hospital_linkage", "registry")
          for y, nxt, _ in system],
        # a change of provider type is a new organization row but the same edge
        ("enrollment", "OX1", "pac", "X", d(Q[0]), None, "ppef_enrollment", "registry"),
    ])
    cols = duckdb.sql(f"SELECT * FROM '{out}/crosswalk.parquet' LIMIT 0").columns
    assert cols == ["from_type", "from_id", "to_type", "to_id", "effective_date", "end_date", "source", "method"]
    # without --compendium, the same quarterly edges and no system edges
    build_graph(warehouse, tmp_path / "g2")
    assert duckdb.sql(f"""SELECT count(*) FROM (SELECT * FROM '{out}/crosswalk.parquet' WHERE to_type <> 'system'
                          EXCEPT ALL SELECT * FROM '{tmp_path}/g2/crosswalk.parquet')""").fetchone() == (0,)
    assert duckdb.sql(f"SELECT count(*) FROM '{tmp_path}/g2/crosswalk.parquet' WHERE to_type = 'system'").fetchone() == (0,)


def test_cli_graph_build(warehouse, tmp_path, capsys):
    out = tmp_path / "g"
    assert main(["graph-build", "--warehouse", str(warehouse), "--out", str(out), "--work", str(tmp_path / "w")]) == 0
    assert "3 quarters" in capsys.readouterr().out
    assert (out / "manifest.json").is_file()


def test_missing_tables_are_a_user_error(tmp_path):
    db = tmp_path / "warehouse"
    with pytest.raises(UserError, match="has no nppes.duckdb"):
        build_graph(db, tmp_path / "g")
    db.mkdir()
    duckdb.connect(str(db / "nppes.duckdb")).close()
    with pytest.raises(UserError, match="lacks"):
        build_graph(db, tmp_path / "g")


def test_compendium_tables(warehouse, compendium, tmp_path):
    out = tmp_path / "g"
    m = build_graph(warehouse, out, compendium=True)
    assert m["compendium"]["years"]["ahrq_group_practice_linkage"] == [2016, 2018, 2020, 2021, 2022]
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
    carried = duckdb.sql(f"""SELECT node_id, health_sys_id, via, md_ppas_physicians FROM '{out}/system_link.parquet'
                             WHERE year = 2023 AND node_type = 'organization' ORDER BY 1""").fetchall()
    assert carried[:2] == [("G", None, "carried forward from 2022", None), ("H", "HSI1", "carried forward from 2022", None)]
    years = duckdb.sql(f"SELECT DISTINCT year FROM '{out}/system_link.parquet' WHERE node_type = 'nursing_home' ORDER BY 1").fetchall()
    assert years == [(2020,), (2021,), (2022,), (2023,)]


def test_compendium_needs_the_ahrq_source(warehouse, tmp_path):
    with pytest.raises(UserError, match="no ahrq.duckdb.*load --source-name ahrq"):
        build_graph(warehouse, tmp_path / "g", compendium=True)
    duckdb.connect(str(warehouse / "ahrq.duckdb")).close()       # the file, but no tables
    with pytest.raises(UserError, match="--compendium needs the AHRQ source.*load --source-name ahrq"):
        build_graph(warehouse, tmp_path / "g", compendium=True)
    assert not (tmp_path / "g").exists()                        # checked before the build starts


STUDY = Path(__file__).resolve().parent.parent / "research" / "provider-networks"
NOTEBOOK = STUDY / "provider_networks.ipynb"


def test_group_primitives(warehouse, medicaid, tmp_path):
    """graph-build writes the group primitives from derived/sql/; the Medicaid ones only with --medicaid."""
    from healthcare_ledger.derived import primitives
    out = tmp_path / "g"
    m = build_graph(warehouse, out)
    assert [f for f in m["tables"] if f in {p.file for p in primitives.PRIMITIVES}] == \
        ["physician_quarter", "physician_edge", "group_size"]
    assert not (out / "main_group.parquet").exists()
    m = build_graph(warehouse, out, medicaid=True)
    assert m["tables"]["main_group"] == {"rows": 2, "key": ["as_of_date", "practitioner_pac_id"],
                                         "description": primitives.MEDICAID[1].description,
                                         "sql": "derived/sql/main_group.sql"}
    # each file is its SQL run on the graph's tables
    con = duckdb.connect()
    for t in ("practitioner", "organization", "reassignment"):
        con.execute(f"CREATE VIEW {t} AS SELECT * FROM read_parquet('{out}/{t}.parquet')")
    con.execute(f"CREATE VIEW quarters AS SELECT * FROM read_csv('{out}/quarters.csv')")
    con.execute(f"ATTACH '{medicaid / 'medicaid.duckdb'}' AS medicaid (READ_ONLY)")
    con.execute("CREATE VIEW medicaid_spending AS SELECT * FROM medicaid.medicaid_spending")
    for p in primitives.PRIMITIVES:
        primitives.create(con, p.name)
        for a, b in ((p.name, f"'{out}/{p.file}.parquet'"), (f"'{out}/{p.file}.parquet'", p.name)):
            assert con.execute(f"SELECT count(*) FROM (FROM {a} EXCEPT ALL FROM {b})").fetchone()[0] == 0, p.name
    # P2 reassigns to G and H in Q0 and Q2 and only to H in Q1
    assert con.execute(f"SELECT organization_pac_id, k FROM '{out}/physician_edge.parquet' "
                       f"WHERE practitioner_pac_id = 'P2' AND as_of_date = '{Q[1]}'").fetchall() == [("H", 1)]


def test_medicaid_needs_the_medicaid_source(warehouse, tmp_path):
    with pytest.raises(UserError, match="no medicaid.duckdb.*load --source-name medicaid"):
        build_graph(warehouse, tmp_path / "g", medicaid=True)
    duckdb.connect(str(warehouse / "medicaid.duckdb")).close()       # the file, but no table
    with pytest.raises(UserError, match="--medicaid needs the Medicaid source.*load --source-name medicaid"):
        build_graph(warehouse, tmp_path / "g", medicaid=True)
    assert not (tmp_path / "g").exists()


def test_notebook_shows_each_primitive_sql():
    """The markdown cell above each use of a group primitive shows its derived/sql file as it is, and no code cell
    builds one."""
    import json
    from healthcare_ledger.derived import primitives
    cells = json.loads(NOTEBOOK.read_text())["cells"]
    markdown = "".join("".join(c["source"]) for c in cells if c["cell_type"] == "markdown")
    code = "".join("".join(c["source"]) for c in cells if c["cell_type"] == "code")
    for p in primitives.PRIMITIVES:
        assert f"`src/healthcare_ledger/derived/sql/{p.name}.sql`, written to `{p.file}.parquet`:\n\n```sql\n" \
               f"{primitives.sql(p.name)}```" in markdown, p.name
        assert f"CREATE TABLE {p.name} AS" not in code, p.name


def test_notebook_sql_runs_on_a_fixture_graph(warehouse, compendium, medicaid, tmp_path):
    """Every SQL block of the provider-networks notebook, in order, against the fixture warehouse's graph, with the
    group primitives read from the Parquet files graph-build writes, as the notebook reads them."""
    from conftest import notebook_sql, study_con
    from healthcare_ledger.derived import primitives
    out = tmp_path / "g"
    build_graph(warehouse, out, compendium=True, medicaid=True)
    con = study_con(STUDY)
    for t in ("practitioner", "organization", "hospital", "reassignment", "hospital_link", "post_acute_facility", "post_acute_link",
              "system", "system_link"):
        con.execute(f"CREATE VIEW {t} AS SELECT * FROM read_parquet('{out}/{t}.parquet')")
    for p in primitives.PRIMITIVES:
        con.execute(f"CREATE VIEW {p.name} AS SELECT * FROM read_parquet('{out}/{p.file}.parquet')")
    con.execute(f"CREATE VIEW quarters AS SELECT * FROM read_csv('{out}/quarters.csv')")
    con.execute("CREATE TABLE hrr AS SELECT * FROM read_csv('../../reference/zip_hrr_2019.csv', all_varchar = true)")
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
    assert routes == [("H", "carried forward from 2022"), ("H", "group practice file"), ("H", "hospital file")]
    # section 7: with systems as firms, H's physicians belong to HSI1; G stays its own tax id
    firms = con.execute("SELECT DISTINCT firm_tax_id, firm_system FROM firm_share WHERE year = 2022 ORDER BY 1").fetchall()
    assert firms == [("G", "G"), ("H", "HSI1")]
    assert con.execute("SELECT count(*) FROM hrr_hhi WHERE firm_def = 'system'").fetchone()[0] > 0
    shares = con.execute("""SELECT practitioner_pac_id, health_sys_id FROM ahrq_edge
                            WHERE year = 2022 ORDER BY ALL""").fetchall()
    assert shares == [("P1", None), ("P2", "HSI1"), ("P2", None)]


def test_reference_data():
    from conftest import study_rows
    enr = study_rows(STUDY, "hospital_enrollments_2026-09-29.csv")
    assert len({r["enrollment_id"] for r in enr}) == len(enr) > 9_000
    assert {r["provider_type_code"] for r in enr} == {"00-09", "00-85", "00-24"}
    for name, code, n in (("snf_enrollments_2026-09-29.csv", "00-18", 14_000), ("hha_enrollments_2026-09-29.csv", "00-06", 11_000)):
        rows = study_rows(STUDY, name)
        assert len({r["enrollment_id"] for r in rows}) == len(rows) > n and {r["provider_type_code"] for r in rows} == {code}

