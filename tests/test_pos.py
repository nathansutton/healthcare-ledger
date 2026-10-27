"""Provider of Services quarters: discovery by header, the two streams keyed by CCN, 'moved' between the QIES and
iQIES files, date and placeholder normalization, the type-aware absent guard, and the export."""
import csv
import json
from datetime import date
from pathlib import Path

import duckdb
import pytest

from conftest import catalog, main_row, write_zip
from healthcare_ledger import sources
from healthcare_ledger.cli import main
from healthcare_ledger.sources.pos import pos
from healthcare_ledger.sources.pos.pos import IQIES, QIES, PosWarehouse, classify, discover_pos, inspect_quarter, quarter_end

QIES_HEADER = ("PRVDR_CTGRY_SBTYP_CD,PRVDR_CTGRY_CD,CITY_NAME,CRTFCTN_DT,FAC_NAME,MDCD_VNDR_NUM,PRVDR_NUM,STATE_CD,ST_ADR,"
               "PHNE_NUM,PGM_TRMNTN_CD,TRMNTN_EXPRTN_DT,ZIP_CD,BED_CNT")
IQIES_HEADER = ("prvdr_num,fac_name,prvdr_type_id,mdcd_vndr_num,orgnl_prtcptn_dt,st_adr,city_name,zip_cd,phne_num,"
                "crtfctn_dt,trmntn_exprtn_dt,state_cd,pgm_trmntn_cd,chow_sw,rn_cnt,processing_date")


def _write(path: Path, header: str, rows: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join([header, *rows]) + "\n")


@pytest.fixture
def pos_source(tmp_path: Path) -> Path:
    src = tmp_path / "pos"
    # Q1: the QIES file alone. 010001 has a nine-digit Medicaid vendor number; 010002 a date and a count that are not one.
    _write(src / "2020-Q1" / "POS_OTHER_MAR20.csv", QIES_HEADER, [
        '01,01,DOTHAN,20000608,"SOUTHEAST HOSPITAL",123456789,010001,AL,"1108 ROSS CLARK CIRCLE",3347938701,00,,36301,420',
        "01,01,BRIDGEPORT,20010316,NORTH JACKSON HOSPITAL,,010002,AL,47005 U S HIGHWAY 72,2564372101,01,99999999,35740,x64",
        "01,05,PHOENIX,20220407,AGAVE HOME HEALTH,,017001,AZ,3240 EAST UNION HILLS DRIVE,6028553500,00,,85050,",
        "01,16,TEMPE,20220617,SAMARITAN HOSPICE INC,,017002,AZ,64 EAST BROADWAY ROAD,6029228233,00,,85282,",
    ])
    # Q2: the HHA and the hospice leave the QIES file for the iQIES file (ISO dates, 'Not Available' placeholders);
    # 010002 changes city, 010003 is new. The iQIES file carries a new hospice too.
    _write(src / "2020-Q2" / "other_data_Q2 2020.csv", QIES_HEADER, [
        '01,01,DOTHAN,20000608,"SOUTHEAST HOSPITAL",123456789,010001,AL,"1108 ROSS CLARK CIRCLE",3347938701,00,,36301,420',
        "01,01,STEVENSON,20010316,NORTH JACKSON HOSPITAL,,010002,AL,47005 U S HIGHWAY 72,2564372101,01,99999999,35740,x64",
        "01,01,MOBILE,20200401,NEW BAY HOSPITAL,,010003,AL,1 BAY ROAD,2515550100,00,,36602,100",
    ])
    _write(src / "2020-Q2" / "iQIES HHA_HOSPICE data Q2 2020.csv", IQIES_HEADER, [
        "017001,AGAVE HOME HEALTH,3,Not Available,2022-04-07,3240 EAST UNION HILLS DRIVE,PHOENIX,85050,6028553500,2022-04-07,Not Available,AZ,00 ,No,2.00,2020-07-01",
        "017002,SAMARITAN HOSPICE INC,12,Not Available,2022-06-17,64 EAST BROADWAY ROAD,TEMPE,85282,6029228233,2022-06-17,Not Available,AZ,00 ,No,Not Applicable,2020-07-01",
        "018001,NEW HOSPICE LLC,12,987654321 ,2020-05-01,9 MAIN ST,MESA,85201,4805550199,2020-05-01,Not Available,AZ,00 ,No,.50,2020-07-01",
    ])
    # Q3: 010003 leaves both files; the hospice 017002 leaves both files; the iQIES file writes its dates MM/DD/YYYY
    # and its counts without trailing zeros (the same values) and a new processing date, none of which is a change.
    # The QIES file writes 010001's bed count with a leading zero (the same value).
    _write(src / "2020-Q3" / "POS_OTHER_SEP20.csv", QIES_HEADER, [
        '01,01,DOTHAN,20000608,"SOUTHEAST HOSPITAL",123456789,010001,AL,"1108 ROSS CLARK CIRCLE",3347938701,00,,36301,0420',
        "01,01,STEVENSON,20010316,NORTH JACKSON HOSPITAL,,010002,AL,47005 U S HIGHWAY 72,2564372101,01,99999999,35740,x64",
    ])
    _write(src / "2020-Q3" / "POS_File_iQIES_Q3_2020.csv", IQIES_HEADER, [
        "017001,AGAVE HOME HEALTH,3,Not Available,04/07/2022,3240 EAST UNION HILLS DRIVE,PHOENIX,85050,6028553500,04/07/2022,Not Applicable,AZ,00 ,No,2,10/01/2020",
        "018001,NEW HOSPICE LLC,12,987654321,05/01/2020,9 MAIN ST,MESA,85201,4805550199,05/01/2020,Not Available,AZ,00 ,No,0.5,10/01/2020",
    ])
    (src / "zips").mkdir()      # not a quarter directory; ignored
    return src


def test_quarter_end_and_header_classification(pos_source):
    assert quarter_end("2019-Q1") == date(2019, 3, 31) and quarter_end("2024-Q4") == date(2024, 12, 31)
    with pytest.raises(ValueError):
        quarter_end("2019-12")
    assert classify(QIES_HEADER.split(",")) is QIES and classify(IQIES_HEADER.split(",")) is IQIES
    assert classify(["NPI", "Entity Type Code"]) is None
    snaps = discover_pos(pos_source)
    assert [(s.label, s.as_of_date, s.snapshot_id) for s in snaps] == [
        ("2020-Q1", date(2020, 3, 31), 20200331), ("2020-Q2", date(2020, 6, 30), 20200630), ("2020-Q3", date(2020, 9, 30), 20200930)]
    assert not snaps[0].has("pos_facility_iqies") and snaps[1].has("pos_facility_iqies")
    assert snaps[1].members["pos_facility"].name == "other_data_Q2 2020.csv"
    bad = pos_source / "2021-Q1"
    _write(bad / "two.csv", QIES_HEADER, [])
    _write(bad / "one.csv", QIES_HEADER, [])
    with pytest.raises(ValueError, match="two pos_facility files"):
        inspect_quarter(bad)
    _write(pos_source / "2021-Q2" / "clia.csv", "PRVDR_NUM,CLIA_ID,FAC_NAME", [])
    with pytest.raises(ValueError, match="not a POS file"):
        inspect_quarter(pos_source / "2021-Q2")


def _load_all(tmp_path: Path, pos_source: Path) -> Path:
    zips = tmp_path / "zips"
    zips.mkdir()
    write_zip(zips, "20200101", "20200112", [main_row("1000000001", entity_type="1", last_name="SMITH", practice_state="MD")])
    db = tmp_path / "warehouse"
    assert main(["load", "--source", str(zips), "--warehouse", str(db), "--work", str(tmp_path / "work")]) == 0
    assert main(["load", "--source-name", "pos", "--source", str(pos_source), "--warehouse", str(db), "--work", str(tmp_path / "work")]) == 0
    return db


def test_load_versions_facilities_by_ccn_and_marks_moves(tmp_path, pos_source):
    db = _load_all(tmp_path, pos_source)
    con = catalog(db, read_only=True)
    assert con.execute("SELECT snapshot_id, directory, streams_present FROM pos_snapshot ORDER BY 1").fetchall() == [
        (20200331, "2020-Q1", ["pos_facility"]), (20200630, "2020-Q2", ["pos_facility", "pos_facility_iqies"]),
        (20200930, "2020-Q3", ["pos_facility", "pos_facility_iqies"])]
    rows = con.execute("""SELECT ccn, effective_date, end_date, close_reason, city_name, prvdr_ctgry_cd, mdcd_vndr_num, trmntn_exprtn_dt
                          FROM pos_facility_version ORDER BY ccn, effective_date""").fetchall()
    assert rows == [
        ("010001", date(2020, 3, 31), None, None, "DOTHAN", "01", "[REDACTED]", None),
        ("010002", date(2020, 3, 31), date(2020, 6, 30), "changed", "BRIDGEPORT", "01", None, None),   # 99999999 is not a date
        ("010002", date(2020, 6, 30), None, None, "STEVENSON", "01", None, None),
        ("010003", date(2020, 6, 30), date(2020, 9, 30), "absent", "MOBILE", "01", None, None),
        ("017001", date(2020, 3, 31), date(2020, 6, 30), "moved", "PHOENIX", "05", None, None),
        ("017002", date(2020, 3, 31), date(2020, 6, 30), "moved", "TEMPE", "16", None, None),
    ]
    # the iQIES stream: placeholders are NULL, dates are typed whatever CMS's format, the processing date is not loaded,
    # and the quarter that only changed the date format created no version
    iq = con.execute("""SELECT ccn, effective_date, end_date, close_reason, prvdr_type_id, crtfctn_dt, mdcd_vndr_num, trmntn_exprtn_dt
                        FROM pos_facility_iqies_version ORDER BY ccn, effective_date""").fetchall()
    assert iq == [
        ("017001", date(2020, 6, 30), None, None, "3", date(2022, 4, 7), None, None),
        ("017002", date(2020, 6, 30), date(2020, 9, 30), "absent", "12", date(2022, 6, 17), None, None),
        ("018001", date(2020, 6, 30), None, None, "12", date(2020, 5, 1), "[REDACTED]", None),
    ]
    assert "processing_date" not in {r[0] for r in con.execute("DESCRIBE pos_facility_iqies_version").fetchall()}
    # counts are numbers: 420 and 0420 are the same value, N/A is a placeholder, 2.00 and 2 agree
    assert con.execute("SELECT bed_cnt FROM pos_facility_version WHERE ccn = '010001'").fetchall() == [(420.0,)]
    assert con.execute("SELECT DISTINCT bed_cnt FROM pos_facility_version WHERE ccn = '010002'").fetchall() == [(None,)]
    assert con.execute("SELECT ccn, rn_cnt FROM pos_facility_iqies_version ORDER BY ccn").fetchall() == [("017001", 2.0), ("017002", None), ("018001", 0.5)]
    assert con.execute("SELECT typeof(bed_cnt), typeof(crtfctn_dt), typeof(prvdr_ctgry_cd) FROM pos_facility_version LIMIT 1").fetchone() == ("DOUBLE", "DATE", "VARCHAR")
    stats = {(r[0], r[1]): r[2:] for r in con.execute(
        "SELECT snapshot_id, stream, n_rows, n_keys, n_new, n_changed, n_unchanged, n_absent, n_moved, n_bad_dates, n_bad_numbers FROM pos_snapshot_stream").fetchall()}
    assert stats[(20200331, "pos_facility")] == (4, 4, 4, 0, 0, 0, 0, 1, 1)
    assert stats[(20200630, "pos_facility")] == (3, 3, 1, 1, 1, 0, 2, 1, 1)
    assert stats[(20200630, "pos_facility_iqies")] == (3, 3, 3, 0, 0, 0, 0, 0, 0)
    assert stats[(20200930, "pos_facility")] == (2, 2, 0, 0, 2, 1, 0, 1, 1)
    assert stats[(20200930, "pos_facility_iqies")] == (2, 2, 0, 0, 2, 1, 0, 0, 0)
    assert con.execute("SELECT count(*) FROM pos_snapshot_stream WHERE sha256 IS NULL OR length(sha256) <> 64 OR file IS NULL").fetchone() == (0,)
    assert con.execute("SELECT n_columns FROM pos_snapshot_stream WHERE snapshot_id = 20200630 ORDER BY stream").fetchall() == [(14,), (16,)]
    con.close()
    assert main(["validate", "--warehouse", str(db)]) == 0
    # re-running loads nothing new; reset drops only the POS tables; a reload rebuilds the same versions
    assert main(["load", "--source-name", "pos", "--source", str(pos_source), "--warehouse", str(db), "--work", str(tmp_path / "work")]) == 0
    assert main(["reset", "--source-name", "pos", "--warehouse", str(db)]) == 2
    assert main(["reset", "--source-name", "pos", "--warehouse", str(db), "--yes"]) == 0
    con = catalog(db, read_only=True)
    assert con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name LIKE 'pos_%'").fetchone() == (0,)
    assert con.execute("SELECT count(*) FROM provider_version").fetchone()[0] > 0
    con.close()
    assert main(["load", "--source-name", "pos", "--source", str(pos_source), "--warehouse", str(db), "--work", str(tmp_path / "work")]) == 0
    con = catalog(db, read_only=True)
    assert con.execute("SELECT count(*) FROM pos_facility_version").fetchone() == (6,)
    con.close()


def test_unmapped_header_stops_the_load(tmp_path, pos_source):
    _write(pos_source / "2020-Q3" / "POS_OTHER_SEP20.csv", QIES_HEADER + ",BRAND_NEW_CD", [
        "01,01,DOTHAN,20000608,SOUTHEAST HOSPITAL,,010001,AL,1108 ROSS CLARK CIRCLE,3347938701,00,,36301,420,X"])
    zips = tmp_path / "zips"
    zips.mkdir()
    write_zip(zips, "20200101", "20200112", [main_row("1000000001", entity_type="1", last_name="SMITH")])
    db = tmp_path / "warehouse"
    assert main(["load", "--source", str(zips), "--warehouse", str(db), "--work", str(tmp_path / "work")]) == 0
    assert main(["load", "--source-name", "pos", "--source", str(pos_source), "--warehouse", str(db), "--work", str(tmp_path / "work")]) == 1
    con = catalog(db, read_only=True)
    assert con.execute("SELECT max(snapshot_id) FROM pos_snapshot").fetchone() == (20200630,)   # the first two quarters stayed
    con.close()


def _hospitals(n: int, start: int = 0, state: str = "AL") -> list[str]:
    return [f"01,01,DOTHAN,20000608,HOSPITAL {i},,{i:06d},{state},{i} MAIN ST,3347938701,00,,36301,10" for i in range(start, start + n)]


def _agencies(n: int, start: int = 500000) -> list[str]:
    return [f"01,05,PHOENIX,20100101,AGENCY {i},,{i:06d},AZ,{i} ELM ST,6025550100,00,,85050," for i in range(start, start + n)]


def test_absent_guard_ignores_provider_types_that_left_the_file(tmp_path):
    src = tmp_path / "pos"
    _write(src / "2020-Q1" / "q1.csv", QIES_HEADER, _hospitals(1000) + _agencies(300))
    # every home health agency leaves the file and none reaches the iQIES file: 23% absent, but all of one type
    _write(src / "2020-Q2" / "q2.csv", QIES_HEADER, _hospitals(1000))
    db = tmp_path / "warehouse"
    assert main(["load", "--source-name", "pos", "--source", str(src), "--warehouse", str(db), "--work", str(tmp_path / "work")]) == 0
    con = catalog(db, read_only=True)
    assert con.execute("SELECT n_absent, n_moved FROM pos_snapshot_stream WHERE snapshot_id = 20200630").fetchone() == (300, 0)
    con.close()
    # 100 of 1000 hospitals gone while hospitals are still in the file: refused, and the quarter is not loaded
    _write(src / "2020-Q3" / "q3.csv", QIES_HEADER, _hospitals(900))
    assert main(["load", "--source-name", "pos", "--source", str(src), "--warehouse", str(db), "--work", str(tmp_path / "work")]) == 1
    con = catalog(db, read_only=True)
    assert con.execute("SELECT max(snapshot_id) FROM pos_snapshot").fetchone() == (20200630,)
    con.close()
    assert main(["load", "--source-name", "pos", "--source", str(src), "--warehouse", str(db), "--work", str(tmp_path / "work"), "--max-absent-share", "0.2"]) == 0


def test_export_carries_the_pos_tables_and_records_their_sources(tmp_path, pos_source):
    db = _load_all(tmp_path, pos_source)
    releases = tmp_path / "releases"
    out = tmp_path / "export"
    assert main(["export", "--warehouse", str(db), "--out", str(out), "--releases", str(releases)]) == 1     # files not recorded yet
    assert main(["sources", "--source-name", "pos", "--warehouse", str(db), "--releases", str(releases)]) == 0
    with open(releases / "pos_sources.csv", newline="") as f:
        src_rows = list(csv.DictReader(f))
    assert [(r["directory"], r["file"]) for r in src_rows] == [
        ("2020-Q1", "POS_OTHER_MAR20.csv"), ("2020-Q2", "iQIES HHA_HOSPICE data Q2 2020.csv"), ("2020-Q2", "other_data_Q2 2020.csv"),
        ("2020-Q3", "POS_File_iQIES_Q3_2020.csv"), ("2020-Q3", "POS_OTHER_SEP20.csv")]
    assert all(len(r["sha256"]) == 64 and r["url"] == "" for r in src_rows)
    src_rows[0]["url"] = "https://example.invalid/POS_OTHER_MAR20.csv"
    src_rows[0]["origin"] = "cms"
    with open(releases / "pos_sources.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(src_rows[0].keys()))
        w.writeheader()
        w.writerows(src_rows)
    assert main(["sources", "--source-name", "pos", "--warehouse", str(db), "--releases", str(releases)]) == 0
    with open(releases / "pos_sources.csv", newline="") as f:
        first = next(csv.DictReader(f))
    assert (first["url"], first["origin"]) == ("https://example.invalid/POS_OTHER_MAR20.csv", "cms")
    assert main(["download", "--source-name", "pos", "--dest", str(pos_source), "--releases", str(releases), "--check"]) == 0

    assert main(["export", "--warehouse", str(db), "--out", str(out), "--releases", str(releases), "--csv"]) == 0
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["pos_snapshots"] == 3
    assert manifest["row_counts"]["facility"] == 9 and not {"pos_facility", "pos_facility_iqies"} & set(manifest["row_counts"])
    # one table: a CCN's history runs through the QIES file and on into the iQIES file, with the type under one name
    import healthcare_ledger
    lib = healthcare_ledger.connect(out)
    rows = lib.execute("SELECT ccn, effective_date, end_date, close_reason, facility_type, active, pos_file "
                       "FROM facility ORDER BY ccn, effective_date").fetchall()
    q1, q2, q3 = date(2020, 3, 31), date(2020, 6, 30), date(2020, 9, 30)
    assert rows == [
        ("010001", q1, None, None, "hospital", True, "qies"),
        ("010002", q1, q2, "changed", "hospital", False, "qies"),
        ("010002", q2, None, None, "hospital", False, "qies"),
        ("010003", q2, q3, "absent", "hospital", True, "qies"),
        ("017001", q1, q2, "changed", "home_health", True, "qies"),     # moved to the iQIES file
        ("017001", q2, None, None, "home_health", True, "iqies"),
        ("017002", q1, q2, "changed", "hospice", True, "qies"),
        ("017002", q2, q3, "absent", "hospice", True, "iqies"),
        ("018001", q2, None, None, "hospice", True, "iqies"),
    ]
    assert lib.execute("SELECT prvdr_ctgry_cd, prvdr_type_id, rn_cnt FROM facility WHERE ccn = '017001' ORDER BY effective_date").fetchall() == [
        ("05", None, None), (None, "3", 2.0)]
    assert (out / "csv" / "facility" / "effective_year=2020").exists()
    with open(out / "pos_snapshots.csv", newline="") as f:
        snaps = list(csv.DictReader(f))
    assert [s["directory"] for s in snaps] == ["2020-Q1", "2020-Q2", "2020-Q3"]
    assert snaps[1]["pos_facility_file"] == "other_data_Q2 2020.csv" and snaps[1]["pos_facility_moved"] == "2"
    assert snaps[0]["pos_facility_iqies_fingerprint"] == "" and len(snaps[2]["pos_facility_iqies_fingerprint"]) == 32
    dd = (out / "data_dictionary.csv").read_text()
    assert "facility,ccn,VARCHAR,PRVDR_NUM," in dd and "facility,prvdr_type_id,VARCHAR,prvdr_type_id," in dd
    assert "facility,crtfctn_dt,DATE,CRTFCTN_DT," in dd and "facility,mdcd_vndr_num,VARCHAR,MDCD_VNDR_NUM," in dd
    assert "facility,bed_cnt,DOUBLE,BED_CNT," in dd and "facility,rn_cnt,DOUBLE,RN_CNT," in dd
    assert "facility,facility_type,VARCHAR," in dd and "facility,active,BOOLEAN," in dd
    readme = (out / "README.md").read_text()
    assert "## Medicare-certified facilities (POS)" in readme and "3 quarters dated 2020-03-31 to 2020-09-30" in readme
    assert (releases / manifest["release_id"] / "pos_snapshots.csv").exists()
    audit = (out / "privacy_audit.csv").read_text()
    assert "facility,phne_num," in audit and ",FAIL" not in audit
    # a second export passes the continuity check against the first, including the POS fingerprints
    assert main(["export", "--warehouse", str(db), "--out", str(out), "--releases", str(releases)]) == 0


def test_pos_checks_catch_tax_ids_and_misread_columns(tmp_path, pos_source):
    from healthcare_ledger.sources.pos.pos import check_problems
    _write(pos_source / "2020-Q1" / "POS_OTHER_MAR20.csv", QIES_HEADER, [
        '01,01,DOTHAN,20000608,"SOUTHEAST HOSPITAL 12-3456789",,010001,AL,"1108 ROSS CLARK CIRCLE",3347938701,00,,36301,420'])
    db = _load_all(tmp_path, pos_source)
    con = catalog(db)
    assert con.execute("SELECT fac_name FROM pos_facility_version WHERE effective_date = DATE '2020-03-31' AND ccn = '010001'").fetchone() == (
        "SOUTHEAST HOSPITAL [REDACTED]",)
    assert check_problems(con) == []
    con.execute("UPDATE pos_facility_version SET state_cd = '36301' WHERE ccn = '010001'")
    con.execute("UPDATE pos_facility_iqies_version SET st_adr = '123456789 MAIN ST' WHERE ccn = '018001'")
    problems = check_problems(con)
    con.close()
    assert any(p.startswith("shape: pos_facility.state_cd") for p in problems)
    assert any(p.startswith("privacy: pos_facility_iqies.st_adr holds a tax-id token") for p in problems)
    assert main(["validate", "--warehouse", str(db)]) == 1


def test_schema_file_is_consistent():
    """Every column name is unique within its stream, fits Stata's 32 characters, and the key is not among them."""
    for s in (QIES, IQIES):
        names = [c.name for c in s.columns]
        assert len(names) == len(set(names)) and all(len(n) <= 32 for n in names)
        assert "ccn" not in names and "prvdr_num" not in names and "processing_date" not in names
        assert all(c.kind == "date" for c in s.columns if c.name.endswith("_dt"))
        assert all(c.kind == "number" for c in s.columns if c.name.endswith("_cnt"))
        assert all(c.kind == "text" for c in s.columns if c.name.endswith(("_cd", "_sw", "_name")))
    assert len(QIES.columns) > 500 and len(IQIES.columns) > 150


def test_facility_prefers_the_iqies_row_when_a_ccn_is_in_both_files(tmp_path):
    """CMS listed the organ procurement organizations in both files in 2025-Q4 before dropping them from the QIES file:
    the published facility takes the iQIES version from the quarter it starts and cuts the QIES one there."""
    from healthcare_ledger.core.connect import open_catalog
    db = tmp_path / "warehouse"
    sources.by_name("nppes").warehouse_cls(db).close()
    PosWarehouse(db).close()
    con = duckdb.connect(str(db / "pos.duckdb"))
    con.execute("INSERT INTO pos_snapshot (snapshot_id, as_of_date, directory, loaded_at) VALUES (20260630, '2026-06-30', '2026-Q2', now())")
    q3, q4, q1 = date(2025, 9, 30), date(2025, 12, 31), date(2026, 3, 31)
    con.executemany("INSERT INTO pos_facility_version (ccn, effective_date, end_date, close_reason, prvdr_ctgry_cd) "
                    "VALUES (?, ?, ?, ?, '17')", [("05P001", q3, q1, "moved"), ("05P002", q4, q1, "moved")])
    con.executemany("INSERT INTO pos_facility_iqies_version (ccn, effective_date, end_date, close_reason, prvdr_type_id) "
                    "VALUES (?, ?, NULL, NULL, '13')", [("05P001", q4), ("05P002", q4)])
    con.close()
    con = open_catalog(db, read_only=True)
    assert con.execute("SELECT ccn, effective_date, end_date, close_reason, facility_type, pos_file FROM facility "
                       "ORDER BY ALL").fetchall() == [
        ("05P001", q3, q4, "changed", "organ_procurement", "qies"),
        ("05P001", q4, None, None, "organ_procurement", "iqies"),
        ("05P002", q4, None, None, "organ_procurement", "iqies")]     # its QIES version lies inside the iQIES one
    assert pos.check_problems(con) == []
    con.close()
    # a CCN that went back to the QIES file after its iQIES versions ended would lose that QIES version: refused
    con = duckdb.connect(str(db / "pos.duckdb"))
    con.execute("UPDATE pos_facility_iqies_version SET end_date = ?, close_reason = 'absent' WHERE ccn = '05P002'", [q1])
    con.execute("INSERT INTO pos_facility_version (ccn, effective_date, prvdr_ctgry_cd) VALUES ('05P002', ?, '17')", [date(2026, 6, 30)])
    con.close()
    con = open_catalog(db, read_only=True)
    assert pos.check_problems(con) == ["facility: 1 QIES version(s) start after their CCN moved to the iQIES file, outside "
                                       "any iQIES version, and are left out"]
