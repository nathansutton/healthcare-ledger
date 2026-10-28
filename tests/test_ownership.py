"""CMS facility Enrollments and All Owners files: discovery by file name, one stream per file type keyed by enrollment id,
streams missing from a release closing nothing, the owner rows of an enrollment versioned together, date and share
normalization, the privacy rules on owner names, and the two published tables."""
import csv
from datetime import date
from pathlib import Path

import pytest

from conftest import catalog, main_row, write_zip
from healthcare_ledger.cli import main
from healthcare_ledger.sources.ownership import ownership
from healthcare_ledger.sources.ownership.ownership import classify, discover_ownership, inspect_release

def test_discovery_by_file_name(ownership_source):
    assert classify("Hospital_All_Owners_2026.07.31_update.csv").name == "ownership_hospital_owner"
    assert classify("SNF_Enrollments.csv").name == "ownership_snf_enrollment"
    assert classify("HHA_Enrollments_2024.01.05.csv").name == "ownership_hha_enrollment"
    assert classify("Hospital_CHOW_2026.09.29.csv") is None
    snaps = discover_ownership(ownership_source)
    assert [(s.label, s.snapshot_id, sorted(s.members)) for s in snaps] == [
        ("2023-01-01", 20230101, ["ownership_snf_enrollment", "ownership_snf_owner"]),
        ("2023-02-01", 20230201, ["ownership_snf_enrollment"]),
        ("2023-03-01", 20230301, ["ownership_snf_enrollment", "ownership_snf_owner"])]
    (ownership_source / "2023-04-01").mkdir()
    (ownership_source / "2023-04-01" / "Hospital_CHOW_2023.04.01.csv").write_text("x\n")
    with pytest.raises(ValueError, match="not an Enrollments or All Owners file"):
        inspect_release(ownership_source / "2023-04-01")


def test_load_versions_owners_by_enrollment(tmp_path, ownership_source):
    db = tmp_path / "warehouse"
    zips = tmp_path / "zips"      # validate needs the NPPES source every warehouse holds
    zips.mkdir()
    write_zip(zips, "20230101", "20230108", [main_row("1000000001", entity_type="1", last_name="SMITH")])
    assert main(["load", "--source", str(zips), "--warehouse", str(db), "--work", str(tmp_path / "work")]) == 0
    assert main(["load", "--source-name", "ownership", "--source", str(ownership_source), "--warehouse", str(db),
                 "--work", str(tmp_path / "work")]) == 0
    con = catalog(db, read_only=True)
    assert con.execute("SELECT snapshot_id, streams_present FROM ownership_snapshot ORDER BY 1").fetchall() == [
        (20230101, ["ownership_snf_enrollment", "ownership_snf_owner"]), (20230201, ["ownership_snf_enrollment"]),
        (20230301, ["ownership_snf_enrollment", "ownership_snf_owner"])]
    enr = con.execute("""SELECT enrollment_id, effective_date, end_date, close_reason, city, incorporation_date
                         FROM ownership_snf_enrollment_version ORDER BY 1, 2""").fetchall()
    assert enr == [
        ("O20000101000001", date(2023, 1, 1), date(2023, 3, 1), "absent", "BOSTON", date(2001, 1, 2)),
        ("O20000101000002", date(2023, 1, 1), date(2023, 2, 1), "changed", "BOSTON", date(2001, 1, 2)),
        ("O20000101000002", date(2023, 2, 1), None, None, "CAMBRIDGE", date(2001, 1, 2))]
    # the owners of an enrollment are one version: the February release had no owner file and closed nothing; in
    # March the first enrollment left the file and the second changed hands
    own = con.execute("""SELECT enrollment_id, effective_date, end_date, close_reason, row_seq, owner_pac_id, owner_type,
                                ownership_pct, owner_first_name, owner_org_name, owner_holding_company_flag
                         FROM ownership_snf_owner_version ORDER BY 1, 2, owner_pac_id""").fetchall()
    assert own == [
        ("O20000101000001", date(2023, 1, 1), date(2023, 3, 1), "absent", 1, "9999999999", "O", 100.0, None,
         "CHAIN HOLDINGS INC [REDACTED]", "Y"),
        ("O20000101000002", date(2023, 1, 1), date(2023, 3, 1), "changed", 1, "3333333333", "I", 60.0, "ANN", None, None),
        ("O20000101000002", date(2023, 1, 1), date(2023, 3, 1), "changed", 2, "4444444444", "I", 40.0, None, None, None),
        ("O20000101000002", date(2023, 3, 1), None, None, 1, "9999999999", "O", 100.0, None, "CHAIN HOLDINGS INC", None)]
    # the published tables put the six types in one table each, with the type named
    assert con.execute("SELECT facility_type, count(*) FROM medicare_facility_enrollment GROUP BY 1").fetchall() == [("snf", 3)]
    cols = [r[0] for r in con.execute("DESCRIBE medicare_facility_enrollment").fetchall()]
    assert cols[:5] == ["enrollment_id", "effective_date", "end_date", "close_reason", "facility_type"]
    assert {"subgroup_general_flag", "affiliation_entity_id", "phone"} <= set(cols)
    assert con.execute("SELECT count(*) FROM medicare_facility_owner WHERE owner_pac_id = '9999999999'").fetchone() == (2,)
    assert ownership.check_problems(con) == []
    con.close()
    assert main(["validate", "--warehouse", str(db)]) == 0
