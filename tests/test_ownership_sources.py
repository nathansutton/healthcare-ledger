"""The reference sources added for ownership before 2022 (irs, hcris, chow, nhc) and the evidence owner communities
draw from them (derived/owner_communities.extra_evidence), on handmade files."""
import importlib

import duckdb
import pytest

from conftest import write_chow, write_hcris, write_irs, write_nhc
from healthcare_ledger.derived import owner_communities as oc

# the loader modules (each package exports its ``load`` function under the same name)
chow = importlib.import_module("healthcare_ledger.sources.chow.load")
hcris = importlib.import_module("healthcare_ledger.sources.hcris.load")
irs = importlib.import_module("healthcare_ledger.sources.irs.load")
nhc = importlib.import_module("healthcare_ledger.sources.nhc.load")


def test_irs_load_reads_schedule_r_with_control_and_reciprocity(tmp_path):
    counts = irs.load(write_irs(tmp_path / "irs"), tmp_path / "wh", tmp_path / "work")
    assert counts["irs_return"] == 5 and counts["irs_bmf"] == 1
    con = duckdb.connect(str(tmp_path / "wh" / "irs.duckdb"), read_only=True)
    rel = {(r[0], r[1]): (r[2], r[3], r[4]) for r in con.execute(
        "SELECT ein, related_name, kind, related_ein, controlled FROM irs_related ORDER BY 1, 2").fetchall()}
    assert rel[("111111111", "HOSPITAL H")] == ("exempt", "222222222", True)
    assert rel[("111111111", "JOINT VENTURE J")] == ("partnership", "666666666", False)      # 40%: not controlled
    assert rel[("111111111", "CORPORATION C")] == ("corporation", "777777777", True)         # 100% and 512(b)(13)
    assert rel[("555555555", "U PROPERTIES LLC")] == ("disregarded", None, True)              # no EIN given
    assert con.execute("SELECT pct_own, control_512b13 FROM irs_related WHERE related_name = 'CORPORATION C'").fetchone() == (100.0, True)
    assert con.execute("SELECT exempt_type, zip5 FROM irs_return WHERE ein = '111111111'").fetchone() == ("501c3", "02101")
    fam = dict(con.execute("SELECT ein, family_id FROM irs_family").fetchall())
    # S, H, F and C are one family; the VEBA is not (a (c)(9)); R's listings of S and U are unreciprocated by Schedule R
    # filers, so R stands alone and U keeps its own family
    assert fam["111111111"] == fam["222222222"] == fam["333333333"] == fam["777777777"]
    assert fam["444444444"] != fam["111111111"] and fam["555555555"] != fam["111111111"] and fam["555555555"] != fam["444444444"]
    assert "999999999" not in fam and "666666666" not in fam
    assert con.execute("SELECT licensed, general_medical, teaching, er_24h, state_license FROM irs_hospital_facility").fetchone() == (True, True, False, True, "L1")


def test_hcris_load_reads_the_chain_home_office_of_each_form(tmp_path):
    counts = hcris.load(write_hcris(tmp_path / "hcris"), tmp_path / "wh", tmp_path / "work")
    assert counts == {"hcris_report": 4, "hcris_home_office": 4}
    con = duckdb.connect(str(tmp_path / "wh" / "hcris.duckdb"), read_only=True)
    rows = {r[0]: r[1:] for r in con.execute(
        "SELECT ccn, form, chain, home_office_number, home_office_id, home_office_name, state, zip5 FROM hcris_home_office").fetchall()}
    assert rows["010001"] == ("hospital", True, "HO1234", "H01234", "BIG CHAIN INC", "TN", "37203")     # the O read as zero
    assert rows["010002"] == ("hospital", False, None, None, None, None, None)
    assert rows["010003"] == ("hospital", True, None, "NAME:NAMESMALLCHAINLLC", "NAME: SMALL CHAIN, LLC", None, None)
    assert rows["015001"] == ("snf", True, "44H070", "44H070", "NH CHAIN", None, None)
    assert con.execute("SELECT fy_begin::VARCHAR, fy_end::VARCHAR FROM hcris_report WHERE ccn = '010002'").fetchone() == ("2021-07-01", "2022-06-30")
    assert hcris.normalize_id_sql("'hb 0813'") and duckdb.connect().execute(f"SELECT {hcris.normalize_id_sql(chr(39) + 'hb o813' + chr(39))}").fetchone()[0] == "HB0813"


def test_chow_load_reads_events_and_owners_in_latin_1(tmp_path):
    counts = chow.load(write_chow(tmp_path / "chow"), tmp_path / "wh", tmp_path / "work")
    assert counts == {"chow_event": 2, "chow_owner": 4}
    con = duckdb.connect(str(tmp_path / "wh" / "chow.duckdb"), read_only=True)
    assert con.execute("SELECT facility_type, chow_type_code, effective_date::VARCHAR, buyer_pac_id, seller_pac_id, buyer_ccn FROM chow_event WHERE facility_type = 'snf'").fetchone() \
        == ("snf", "CH", "2019-03-15", "B1", "S1", "440001")
    rows = con.execute("SELECT owner_pac_id, owner_type, owner_role_code, ownership_pct, owner_last_name, owner_organization_name, owner_association_date::VARCHAR, owner_for_profit "
                       "FROM chow_owner WHERE facility_type = 'hospital' ORDER BY 1").fetchall()
    assert rows == [("P8", "I", "43", None, "MUÑOZ", None, "2017-01-01", None), ("P9", "O", "34", 60.5, None, "PARENT CO", "2017-01-01", "Y")]


def test_nhc_load_maps_three_layouts_to_one_table(tmp_path):
    counts = nhc.load(write_nhc(tmp_path / "nhc"), tmp_path / "wh", tmp_path / "work")
    assert counts == {"nhc_ownership": 5}
    con = duckdb.connect(str(tmp_path / "wh" / "nhc.duckdb"), read_only=True)
    rows = con.execute("SELECT as_of_date::VARCHAR, owner_type, owner_name, ownership_pct, association_date::VARCHAR, role FROM nhc_ownership ORDER BY 1, 3").fetchall()
    assert rows == [("2014-03-01", "Organization", "BURNS HOLDINGS LLC", None, None, "5% OR MORE OWNERSHIP INTEREST"),
                    ("2014-03-01", "Individual", "DEARMAN, LARRY", None, None, "5% OR MORE OWNERSHIP INTEREST"),
                    ("2017-09-01", "Individual", "DEARMAN, LARRY", 10.0, "1969-09-01", "5% OR GREATER DIRECT OWNERSHIP INTEREST"),
                    ("2017-09-01", "Organization", "MUÑOZ CARE LLC", None, "2015-01-02", "OPERATIONAL/MANAGERIAL CONTROL"),
                    ("2021-12-01", "Organization", "BURNS HOLDINGS LLC", 100.0, "2020-03-01", "5% OR GREATER INDIRECT OWNERSHIP INTEREST")]


def test_extra_evidence_adds_chain_and_nhc_keys_and_irs_pairs():
    con = duckdb.connect()
    con.execute("CREATE TEMP TABLE oc_org AS SELECT * FROM (VALUES ('A'), ('B'), ('C'), ('D'), ('E')) t(pac_id)")
    con.execute("CREATE TEMP TABLE oc_key (pac_id VARCHAR, kind VARCHAR, key VARCHAR)")
    con.execute("CREATE TEMP TABLE oc_fac_ccn AS SELECT * FROM (VALUES ('A', '010001'), ('B', '010002'), ('C', '015001'), ('D', '015002')) t(pac_id, ccn)")
    # cost reports: A's and B's fiscal 2019 reports name the same home office; C's latest report is too old; D's is in force
    con.execute("""CREATE TEMP TABLE hcris_home_office AS SELECT * FROM (VALUES
        ('010001', DATE '2019-01-01', DATE '2019-12-31', TRUE, 'HB0001', '1'), ('010001', DATE '2018-01-01', DATE '2018-12-31', TRUE, 'HB9999', '0'),
        ('010002', DATE '2018-07-01', DATE '2019-06-30', TRUE, 'HB0001', '2'),
        ('015001', DATE '2016-01-01', DATE '2016-12-31', TRUE, 'HB0001', '3'),
        ('015002', DATE '2019-01-01', DATE '2019-12-31', FALSE, NULL, '4')) t(ccn, fy_begin, fy_end, chain, home_office_id, rpt_rec_num)""")
    con.execute("""CREATE TEMP TABLE nhc_ownership AS SELECT * FROM (VALUES
        ('015001', DATE '2019-06-01', '5% OR GREATER DIRECT OWNERSHIP INTEREST', 'Organization', 'CHAIN LLC'),
        ('015002', DATE '2019-06-01', '5% OR GREATER INDIRECT OWNERSHIP INTEREST', 'Organization', 'CHAIN LLC'),
        ('015002', DATE '2019-06-01', 'OPERATIONAL/MANAGERIAL CONTROL', 'Organization', 'MANAGER LLC'),
        ('015001', DATE '2018-12-01', '5% OR GREATER DIRECT OWNERSHIP INTEREST', 'Organization', 'OLD OWNER')) t(ccn, as_of_date, role, owner_type, owner_name)""")
    # Schedule R for tax year 2018: S lists H (mutual) and E's entity (one-sided). 2019 holds one return against three
    # for 2018, so it is not yet complete: a 2020 quarter reads 2018 too
    con.execute("CREATE TEMP TABLE irs_return AS SELECT * FROM (VALUES (2018, '111', '501c3'), (2018, '222', '501c3'), (2018, '333', '501c3'), (2019, '111', '501c3')) t(tax_year, ein, exempt_type)")
    con.execute("""CREATE TEMP TABLE irs_related AS SELECT * FROM (VALUES
        (2018, '111', '222', 'exempt', '501(C)(3)', TRUE), (2018, '222', '111', 'exempt', '501(C)(3)', TRUE), (2018, '111', '555', 'corporation', NULL, TRUE),
        (2018, '111', '999', 'exempt', '501(C)(9)', TRUE), (2019, '111', '333', 'exempt', '501(C)(3)', TRUE)) t(tax_year, ein, related_ein, kind, exempt_code, controlled)""")
    con.execute("CREATE TEMP TABLE oc_ein_pac AS SELECT * FROM (VALUES ('111', 'A'), ('222', 'B'), ('555', 'E'), ('999', 'D'), ('333', 'C')) t(ein, pac_id)")
    oc.extra_evidence(con, "2019-07-02", ("hcris", "nhc", "irs"))
    keys = sorted(con.execute("SELECT pac_id, kind, key FROM oc_key").fetchall())
    assert keys == [("A", "chain", "HB0001"), ("B", "chain", "HB0001"), ("C", "nhc", "Organization|CHAIN LLC"), ("D", "nhc", "Organization|CHAIN LLC")]
    pairs = sorted(con.execute("SELECT x, y, kind, rank, size FROM oc_pair_extra").fetchall())
    assert pairs == [("A", "B", "irs", 1, "mutual"), ("A", "E", "irs", 2, "listed")]
    # evidence() folds the extra pairs in: the IRS pair is the strongest kind of A-B
    con.execute("UPDATE oc_org SET pac_id = pac_id")
    con.execute("ALTER TABLE oc_org ADD COLUMN org_name VARCHAR DEFAULT 'ORG'")
    con.execute("ALTER TABLE oc_org ADD COLUMN state VARCHAR DEFAULT 'MA'")
    oc.evidence(con)
    assert con.execute("SELECT kinds, kind1, size FROM oc_pair WHERE x = 'A' AND y = 'B'").fetchone() == ("chain+irs", "irs", "mutual")
    assert con.execute("SELECT kinds, size FROM oc_pair WHERE x = 'C' AND y = 'D'").fetchone() == ("nhc", "2")
    # a 2020 quarter reads 2018 while 2019 is partial, and 2019 once it is complete
    con.execute("DELETE FROM oc_key")
    oc.extra_evidence(con, "2020-07-02", ("irs",))
    assert sorted(con.execute("SELECT x, y, size FROM oc_pair_extra").fetchall()) == [("A", "B", "mutual"), ("A", "E", "listed")]
    con.execute("INSERT INTO irs_return VALUES (2019, '222', '501c3'), (2019, '333', '501c3')")      # now complete: C's listing counts
    oc.extra_evidence(con, "2020-07-02", ("irs",))
    assert sorted(con.execute("SELECT x, y, size FROM oc_pair_extra").fetchall()) == [("A", "C", "listed")]
