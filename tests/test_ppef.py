"""Medicare PPEF quarterly extracts: discovery, header drift, the SCD rules keyed by enrollment id, and the export."""
import csv
import json
from datetime import date
from pathlib import Path

import duckdb
import pytest

from conftest import catalog, main_row, write_zip
from healthcare_ledger.cli import main
from healthcare_ledger.sources.ppef import discover_ppef, inspect_quarter

OLD_HEADER = "NPI,PECOS_ASCT_CNTL_ID,ENRLMT_ID,PROVIDER_TYPE_CD,PROVIDER_TYPE_DESC,STATE_CD,FIRST_NAME,MDL_NAME,LAST_NAME,ORG_NAME,ENRLMT_SPECIALTY_DESC"
NEW_HEADER = "NPI,MULTIPLE_NPI_FLAG,PECOS_ASCT_CNTL_ID,ENRLMT_ID,PROVIDER_TYPE_CD,PROVIDER_TYPE_DESC,STATE_CD,FIRST_NAME,MDL_NAME,LAST_NAME,ORG_NAME"


def _write(path: Path, header: str, rows: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\r\n".join([header, *rows]) + "\r\n")


@pytest.fixture
def ppef_source(tmp_path: Path) -> Path:
    src = tmp_path / "ppef"
    q1, q2 = src / "2020-Q1", src / "2020-Q2"
    # Q1: the 2016-style header with the extra ENRLMT_SPECIALTY_DESC column (ignored), a quoted name with a comma
    _write(q1 / "PPEF_Enrollment_Extract_2020.04.01.csv", OLD_HEADER, [
        '1000000001,8000000001,I001,14-08,PRACTITIONER - FAMILY PRACTICE,MD,ANN,,"SMITH, JR.",,FAMILY PRACTICE',
        "1000000002,80000002,I002,14-11,PRACTITIONER - INTERNAL MEDICINE,TX,BOB,Q,JONES,,INTERNAL MEDICINE",
        "1000000003,8000000003,O003,00-85,CLINIC/GROUP PRACTICE,MD,,,,GROUP LLC,",
        "1000000005,8000000005,I005,14-50,PRACTITIONER - NURSE PRACTITIONER,TX,DAN,831045555,ROE,,NURSE PRACTITIONER",
    ])
    _write(q1 / "PPEF_Practice_Location_Extract_2020.04.01.csv", "ENRLMT_ID,CITY_NAME,STATE_CD,ZIP_CD",
           ["I001,BALTIMORE,MD,212011234", "I001,TOWSON,MD,212040000", "I002,HOUSTON,TX,770020000"])
    _write(q1 / "PPEF_Reassignment_Extract_2020.04.01.csv", "REASGN_BNFT_ENRLMT_ID,RCV_BNFT_ENRLMT_ID", ["I001,O003"])
    _write(q1 / "PPEF_Secondary_Specialty_Extract_2020.04.01.csv", "ENRLMT_ID,PROVIDER_TYPE_CD,PROVIDER_TYPE_DESC",
           ["I001,14-11,PRACTITIONER - INTERNAL MEDICINE"])
    # Q2: enrollment file named without a date (as CMS did in 2023-2024), republished as _Clean; the stale one
    # still lists I002. The new header carries MULTIPLE_NPI_FLAG. I001 moved to PA, I002 left, I004 is new.
    _write(q2 / "PPEF_Enrollment_Extract_2020Q2.csv", NEW_HEADER, [
        "1000000001,N,8000000001,I001,14-08,PRACTITIONER - FAMILY PRACTICE,PA,ANN,,SMITH,",
        "1000000002,N,8000000002,I002,14-11,PRACTITIONER - INTERNAL MEDICINE,TX,BOB,Q,JONES,",
    ])
    _write(q2 / "PPEF_Enrollment_Extract_2020Q2_Clean.csv", NEW_HEADER, [
        '1000000001,N,8000000001,I001,14-08,PRACTITIONER - FAMILY PRACTICE,PA,ANN,,"SMITH, JR.",',
        "1000000003,N,8000000003,O003,00-85,CLINIC/GROUP PRACTICE,MD,,,,GROUP LLC",
        "1000000004,Y,8000000004,I004,14-16,PRACTITIONER - OBSTETRICS/GYNECOLOGY,PA,CAT,,LEE,",
        "1000000005,N,8000000005,I005,14-50,PRACTITIONER - NURSE PRACTITIONER,TX,DAN,,ROE,",
    ])
    _write(q2 / "PPEF_Practice_Location_Extract_2020.07.01.csv", "ENRLMT_ID,CITY_NAME,STATE_CD,ZIP_CD",
           ["I001,PHILADELPHIA,PA,191010000", "I004,PITTSBURGH,PA,152190000"])
    _write(q2 / "PPEF_Reassignment_Extract_2020.07.01.csv", "REASGN_BNFT_ENRLMT_ID,RCV_BNFT_ENRLMT_ID", ["I001,O003"])
    # no secondary specialty file this quarter: nothing is closed
    _write(q2 / "PPEF_Additional_NPIs_2020.07.01.csv", "ENRLMT_ID,NPI", ["I004,1000000044"])
    (src / "zips").mkdir()          # not a quarter directory; ignored
    return src


def test_discover_dates_quarters_from_the_files_and_prefers_the_clean_extract(ppef_source):
    snaps = discover_ppef(ppef_source)
    assert [(s.label, s.as_of_date, s.snapshot_id) for s in snaps] == [
        ("2020-Q1", date(2020, 4, 1), 20200401), ("2020-Q2", date(2020, 7, 1), 20200701)]
    assert snaps[1].members["medicare_enrollment"].name == "PPEF_Enrollment_Extract_2020Q2_Clean.csv"
    assert not snaps[1].has("medicare_secondary_specialty") and snaps[1].has("medicare_additional_npi")
    bad = ppef_source / "2021-Q1"
    _write(bad / "PPEF_Enrollment_Extract_2021Q1.csv", NEW_HEADER, [])
    with pytest.raises(ValueError, match="no extract date"):
        inspect_quarter(bad)


def _load_all(tmp_path: Path, ppef_source: Path) -> Path:
    zips = tmp_path / "zips"
    zips.mkdir()
    write_zip(zips, "20200101", "20200112", [main_row("1000000001", entity_type="1", last_name="SMITH", practice_state="MD")])
    db = tmp_path / "warehouse"
    assert main(["load", "--source", str(zips), "--warehouse", str(db), "--work", str(tmp_path / "work")]) == 0
    assert main(["load", "--source-name", "ppef", "--source", str(ppef_source), "--warehouse", str(db), "--work", str(tmp_path / "work")]) == 0
    return db


def test_load_versions_enrollments_by_enrollment_id(tmp_path, ppef_source):
    db = _load_all(tmp_path, ppef_source)
    con = catalog(db, read_only=True)
    assert con.execute("SELECT snapshot_id, directory FROM ppef_snapshot ORDER BY 1").fetchall() == [(20200401, "2020-Q1"), (20200701, "2020-Q2")]
    rows = con.execute("""SELECT enrollment_id, effective_date, end_date, close_reason, state, last_name, multiple_npi_flag
                          FROM medicare_enrollment_version ORDER BY enrollment_id, effective_date""").fetchall()
    assert rows == [
        ("I001", date(2020, 4, 1), date(2020, 7, 1), "changed", "MD", "SMITH, JR.", None),
        ("I001", date(2020, 7, 1), None, None, "PA", "SMITH, JR.", "N"),
        ("I002", date(2020, 4, 1), date(2020, 7, 1), "absent", "TX", "JONES", None),
        ("I004", date(2020, 7, 1), None, None, "PA", "LEE", "Y"),
        ("I005", date(2020, 4, 1), date(2020, 7, 1), "changed", "TX", "ROE", None),
        ("I005", date(2020, 7, 1), None, None, "TX", "ROE", "N"),
        # O003 is unchanged in content, but the flag CMS added is a new non-NULL value, so it versions once
        ("O003", date(2020, 4, 1), date(2020, 7, 1), "changed", "MD", None, None),
        ("O003", date(2020, 7, 1), None, None, "MD", None, "N"),
    ]
    stats = {(r[0], r[1]): r[2:] for r in con.execute(
        "SELECT snapshot_id, stream, n_rows, n_keys, n_new, n_changed, n_unchanged, n_absent FROM ppef_snapshot_stream").fetchall()}
    assert stats[(20200401, "medicare_enrollment")] == (4, 4, 4, 0, 0, 0)
    assert stats[(20200701, "medicare_enrollment")] == (4, 4, 1, 3, 0, 1)
    # the nine-digit middle name never reached the warehouse
    assert con.execute("SELECT count(*) FROM medicare_enrollment_version WHERE middle_name IS NOT NULL AND enrollment_id = 'I005'").fetchone() == (0,)
    assert stats[(20200401, "medicare_practice_location")] == (3, 2, 2, 0, 0, 0)
    assert stats[(20200701, "medicare_practice_location")] == (2, 2, 1, 1, 0, 1)
    assert stats[(20200701, "medicare_reassignment")] == (1, 1, 0, 0, 1, 0)
    assert (20200701, "medicare_secondary_specialty") not in stats
    assert stats[(20200701, "medicare_additional_npi")] == (1, 1, 1, 0, 0, 0)
    # the two-location version of I001 is one version with two rows, still open for the secondary specialty stream
    assert con.execute("SELECT count(*) FROM medicare_practice_location_version WHERE enrollment_id = 'I001' AND effective_date = DATE '2020-04-01'").fetchone() == (2,)
    # the PAC id written without its leading zeros is restored to ten digits
    assert con.execute("SELECT pac_id FROM medicare_enrollment_version WHERE enrollment_id = 'I002'").fetchone() == ("0080000002",)
    assert con.execute("SELECT is_current, last_snapshot_id FROM medicare_secondary_specialty_version").fetchall() == [(True, 20200401)]
    # every loaded file has its sha256 recorded
    assert con.execute("SELECT count(*) FROM ppef_snapshot_stream WHERE sha256 IS NULL OR length(sha256) <> 64").fetchone() == (0,)
    con.close()
    assert main(["validate", "--warehouse", str(db)]) == 0
    # re-running loads nothing new
    assert main(["load", "--source-name", "ppef", "--source", str(ppef_source), "--warehouse", str(db), "--work", str(tmp_path / "work")]) == 0
    # reset drops only the PPEF tables; a reload rebuilds the same versions
    assert main(["reset", "--source-name", "ppef", "--warehouse", str(db)]) == 2
    assert main(["reset", "--source-name", "ppef", "--warehouse", str(db), "--yes"]) == 0
    con = catalog(db, read_only=True)
    assert con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name LIKE 'medicare_%' OR table_name LIKE 'ppef_%'").fetchone() == (0,)
    assert con.execute("SELECT count(*) FROM provider_version").fetchone()[0] > 0
    con.close()
    assert main(["load", "--source-name", "ppef", "--source", str(ppef_source), "--warehouse", str(db), "--work", str(tmp_path / "work")]) == 0
    con = catalog(db, read_only=True)
    assert con.execute("SELECT count(*) FROM medicare_enrollment_version").fetchone() == (8,)
    con.close()


def test_export_carries_the_medicare_tables_and_records_their_sources(tmp_path, ppef_source):
    db = _load_all(tmp_path, ppef_source)
    releases = tmp_path / "releases"
    out = tmp_path / "export"
    # export refuses until the PPEF files are recorded
    assert main(["export", "--warehouse", str(db), "--out", str(out), "--releases", str(releases)]) == 1
    assert main(["sources", "--source-name", "ppef", "--warehouse", str(db), "--releases", str(releases)]) == 0
    with open(releases / "ppef_sources.csv", newline="") as f:
        src_rows = list(csv.DictReader(f))
    assert {r["file"] for r in src_rows} >= {"PPEF_Enrollment_Extract_2020Q2_Clean.csv", "PPEF_Additional_NPIs_2020.07.01.csv"}
    assert all(len(r["sha256"]) == 64 and r["url"] == "" for r in src_rows)
    # a recorded url survives a rewrite
    src_rows[0]["url"] = "https://example.invalid/x.csv"
    with open(releases / "ppef_sources.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(src_rows[0].keys()))
        w.writeheader()
        w.writerows(src_rows)
    assert main(["sources", "--source-name", "ppef", "--warehouse", str(db), "--releases", str(releases)]) == 0
    with open(releases / "ppef_sources.csv", newline="") as f:
        assert next(csv.DictReader(f))["url"] == "https://example.invalid/x.csv"

    assert main(["export", "--warehouse", str(db), "--out", str(out), "--releases", str(releases)]) == 0
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["ppef_snapshots"] == 2
    assert manifest["row_counts"]["medicare_enrollment"] == 8
    assert manifest["row_counts"]["medicare_practice_location"] == 5    # 2 + 1 in Q1, 1 + 1 in Q2
    assert manifest["row_counts"]["medicare_secondary_specialty"] == 1
    con = duckdb.connect()
    pq = con.execute(f"SELECT enrollment_id, state, effective_year FROM read_parquet('{out}/parquet/medicare_enrollment/*/*.parquet', hive_partitioning=true) ORDER BY 1, 2").fetchall()
    assert ("I001", "PA", 2020) in pq and len(pq) == 8
    with open(out / "medicare_snapshots.csv", newline="") as f:
        snaps = list(csv.DictReader(f))
    assert [s["directory"] for s in snaps] == ["2020-Q1", "2020-Q2"]
    assert snaps[1]["medicare_enrollment_file"] == "PPEF_Enrollment_Extract_2020Q2_Clean.csv"
    assert len(snaps[0]["medicare_enrollment_fingerprint"]) == 32 and snaps[1]["medicare_secondary_specialty_fingerprint"] == ""
    dd = (out / "data_dictionary.csv").read_text()
    assert "medicare_enrollment,enrollment_id,VARCHAR,ENRLMT_ID," in dd and "medicare_enrollment,sex,VARCHAR,GNDR_SW," in dd and "medicare_reassignment,receiving_enrollment_id,VARCHAR,RCV_BNFT_ENRLMT_ID," in dd
    readme = (out / "README.md").read_text()
    assert "## Medicare enrollment (PPEF)" in readme and "2 quarterly extracts dated 2020-04-01 to" in readme
    assert (releases / manifest["release_id"] / "medicare_snapshots.csv").exists()
    # a second export passes the continuity check against the first, including the PPEF fingerprints
    assert main(["export", "--warehouse", str(db), "--out", str(out), "--releases", str(releases)]) == 0


def test_swapped_state_and_zip_headers_are_read_by_content(tmp_path, ppef_source):
    # NBER's copies of the August 2016 and January 2017 address files label ZIP_CD and STATE_CD the wrong way round
    _write(ppef_source / "2020-Q1" / "PPEF_Practice_Location_Extract_2020.04.01.csv", "ENRLMT_ID,CITY_NAME,ZIP_CD,STATE_CD",
           ["I001,BALTIMORE,MD,212011234", "I001,TOWSON,MD,212040000", "I002,HOUSTON,TX,770020000"])
    db = _load_all(tmp_path, ppef_source)
    con = catalog(db, read_only=True)
    assert con.execute("SELECT city, state, zip FROM medicare_practice_location_version WHERE effective_date = DATE '2020-04-01' "
                       "ORDER BY city").fetchall() == [("BALTIMORE", "MD", "212011234"), ("HOUSTON", "TX", "770020000"),
                                                      ("TOWSON", "MD", "212040000")]
    con.close()
    assert main(["validate", "--warehouse", str(db)]) == 0


def test_medicare_checks_catch_tax_ids_and_misread_columns(tmp_path, ppef_source):
    from healthcare_ledger.sources.ppef import check_problems
    _write(ppef_source / "2020-Q1" / "PPEF_Enrollment_Extract_2020.04.01.csv", OLD_HEADER, [
        "1000000003,8000000003,O003,00-85,CLINIC/GROUP PRACTICE,MD,,,,GROUP LLC 12-3456789,"])
    db = _load_all(tmp_path, ppef_source)
    con = catalog(db)
    assert con.execute("SELECT org_name FROM medicare_enrollment_version WHERE effective_date = DATE '2020-04-01'").fetchone() == (
        "GROUP LLC [REDACTED]",)
    assert check_problems(con) == []
    con.execute("UPDATE medicare_practice_location_version SET state = '212011234' WHERE city = 'BALTIMORE'")
    con.execute("UPDATE medicare_enrollment_version SET first_name = 'ANN 2'")
    problems = check_problems(con)
    con.close()
    assert any(p.startswith("shape: medicare_practice_location.state") for p in problems)
    assert any(p.startswith("privacy: medicare_enrollment.first_name contains a digit") for p in problems)
    assert main(["validate", "--warehouse", str(db)]) == 1
