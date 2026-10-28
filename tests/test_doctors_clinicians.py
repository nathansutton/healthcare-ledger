"""Doctors and Clinicians National Downloadable File: discovery of finished zips and their as-of dates, the headerless
2014 files, the header aliases of the 2014 to 2020 vintages, formatting-only differences that make no version, group
moves and absences, the privacy rules, the sources manifest and the published table."""
import csv
import hashlib
import io
import shutil
import zipfile
from datetime import date
from pathlib import Path

import pytest

from conftest import A, B, C, D, FIELDS, H2015, H2020, TO_2020, _csv, _row, _zip, catalog, main_row, write_zip
from healthcare_ledger.cli import main
from healthcare_ledger.sources.doctors_clinicians import dac
from healthcare_ledger.sources.doctors_clinicians.dac import HEADER_2014, as_of_from_name, discover_dac

def test_discovery_and_as_of_dates(dac_source):
    assert as_of_from_name("doc_archive_03_2014.zip") == date(2014, 3, 1)
    assert as_of_from_name("doctors_and_clinicians_12_2022.zip") == date(2022, 12, 1)
    assert as_of_from_name("doctors_and_clinicians_archive_2016.zip") is None
    snaps = discover_dac(dac_source)
    assert [(s.label, s.snapshot_id, s.headerless, s.member) for s in snaps] == [
        ("2014/doc_archive_03_2014.zip", 20140301, True, "National_Downloadable_File.csv"),
        ("2015/doc_archive_04_2015.zip", 20150401, False, "National_Downloadable_File.csv"),
        ("2020/doctors_and_clinicians_archive_08_2020.zip", 20200801, False, "National_Downloadable_File.csv")]
    _zip(dac_source / "2021" / "doctors_and_clinicians_01_2021.zip", b"Last,First\nA,B\n")
    with pytest.raises(ValueError, match="neither a header starting with NPI"):
        discover_dac(dac_source)


def test_header_aliases_map_every_vintage():
    w = dac.DacWarehouse.__new__(dac.DacWarehouse)
    for header in (list(HEADER_2014), H2015, H2020):
        mapping, unmatched = dac.DacWarehouse.map_headers(w, dac.CLINICIAN, header)
        assert unmatched == []
        assert all(mapping[c] is not None for c in ("ind_pac_id", "org_pac_id", "num_org_mem", "hosp_afl_5_ccn", "zip"))
    assert mapping["phone"] == "phn_numbr" and mapping["erx_flag"] is None


def test_load_versions_by_npi(tmp_path, dac_source):
    db = tmp_path / "warehouse"
    zips = tmp_path / "zips"      # validate wants the NPPES file a warehouse always holds
    zips.mkdir()
    write_zip(zips, "20200101", "20200112", [main_row(A, entity_type="1", last_name="SMITH")])
    assert main(["load", "--source", str(zips), "--warehouse", str(db), "--work", str(tmp_path / "work")]) == 0
    assert main(["load", "--source-name", "doctors_clinicians", "--source", str(dac_source), "--warehouse", str(db),
                 "--work", str(tmp_path / "work")]) == 0
    con = catalog(db, read_only=True)
    stats = con.execute("SELECT snapshot_id, file, n_rows, n_keys, n_new, n_changed, n_unchanged, n_absent "
                        "FROM dac_snapshot_stream ORDER BY 1").fetchall()
    assert stats == [(20140301, "doc_archive_03_2014.csv", 4, 3, 3, 0, 0, 0),
                     (20150401, "doc_archive_04_2015.csv", 4, 3, 0, 0, 3, 0),
                     (20200801, "doctors_and_clinicians_archive_08_2020.csv", 3, 3, 1, 2, 0, 1)]
    # the recorded sha256 is the member's as CMS wrote it, not the copy with the header line in front
    with zipfile.ZipFile(dac_source / "2014" / "doc_archive_03_2014.zip") as z:
        member = z.read("National_Downloadable_File.csv")
    assert con.execute("SELECT bytes, sha256 FROM dac_snapshot_stream WHERE snapshot_id = 20140301").fetchone() == (
        len(member), hashlib.sha256(member).hexdigest())
    v = con.execute("""SELECT npi, effective_date, end_date, close_reason, ind_pac_id, org_pac_id, num_org_mem,
                              graduation_year, zip, erx_flag, phone
                       FROM dac_clinician_version ORDER BY npi, effective_date, org_pac_id""").fetchall()
    assert v[:3] == [
        # the 2015 formatting (lost zeros, 070, 1994.0) made no version; the group move in 2020 did
        (A, date(2014, 3, 1), date(2020, 8, 1), "changed", "0042100026", "0113900655", 70, 1994, "021142621", "N", None),
        (A, date(2020, 8, 1), None, None, "0042100026", "9935112143", 70, 1994, "021142621", None, "7605551234"),
        # B's two groups are one version, which closes as absent when B leaves the file
        (B, date(2014, 3, 1), date(2020, 8, 1), "absent", "2163334699", "7113900655", 936, 1994, "021142621", "N", None)]
    assert con.execute(f"SELECT count(*), count(DISTINCT row_seq) FROM dac_clinician_version WHERE npi = '{B}'").fetchone() == (2, 2)
    # privacy: the digit in the first name and the tax id in the credential are NULL, the SSN in the group name redacted
    assert con.execute(f"SELECT DISTINCT first_name, credential, org_name FROM dac_clinician_version WHERE npi = '{C}'"
                       ).fetchall() == [(None, None, "ACME [REDACTED] LLC")]
    assert con.execute(f"SELECT hosp_afl_1_ccn FROM dac_clinician_version WHERE npi = '{D}'").fetchone() == ("05A045",)
    # the published table
    cols = [r[0] for r in con.execute("DESCRIBE doctors_clinicians").fetchall()]
    assert cols[:4] == ["npi", "effective_date", "end_date", "close_reason"]
    assert {"org_pac_id", "num_org_mem", "hosp_afl_1_ccn", "hosp_afl_5_lbn", "assignment_flag", "phone"} <= set(cols)
    assert "row_seq" not in cols and "version_hash" not in cols
    assert con.execute("SELECT count(*) FROM doctors_clinicians WHERE end_date IS NULL").fetchone() == (3,)
    assert dac.check_problems(con) == []
    con.close()
    assert main(["validate", "--warehouse", str(db)]) == 0


def test_sources_manifest_pins_members(tmp_path, dac_source):
    db, releases = tmp_path / "warehouse", tmp_path / "releases"
    url = "https://web.archive.org/web/2021id_/https://data.cms.gov/x/doc_archive_03_2014.zip"
    (dac_source / "manifest.tsv").write_text(f"2014/doc_archive_03_2014.zip\t1\tabc\t{url}\n")
    assert dac.pin_downloads(dac_source, releases) == 3
    assert main(["load", "--source-name", "doctors_clinicians", "--source", str(dac_source), "--warehouse", str(db),
                 "--work", str(tmp_path / "work")]) == 0
    con = catalog(db, read_only=True)
    assert dac.sources_problems(con, releases) == []
    con.close()
    rows = dac.read_sources(releases)
    assert rows[0]["directory"] == "2014" and rows[0]["file"] == "doc_archive_03_2014.csv"
    assert (rows[0]["origin"], rows[0]["url"], rows[0]["member"]) == ("wayback", url, "National_Downloadable_File.csv")
    # a download rebuilds <year>/<stem>.csv from the zip, and discovery reads it as the same snapshot
    dest = tmp_path / "fresh"
    (dest / "zips").mkdir(parents=True)
    shutil.copy(dac_source / "2014" / "doc_archive_03_2014.zip", dest / "zips" / "doc_archive_03_2014.zip")
    problems = dac.download_sources(releases, dest)
    assert len(problems) == 2 and all("no url recorded" in p for p in problems)
    assert (dest / "2014" / "doc_archive_03_2014.csv").exists()
    [snap] = [s for s in discover_dac(dest) if s.snapshot_id == 20140301]
    assert (snap.member, snap.headerless) == (None, True)


def test_header_fix_by_layout_width():
    def lines(fields):
        return (",".join(fields) + "\n").encode()
    row40, row42 = lines(["1003000126"] + [""] * 39), lines(["1003000126"] + [""] * 41)
    assert dac.header_fix(lines(dac.HEADER_2014), row40, "x") == (None, False)
    assert dac.header_fix(row40, row40, "x") == (dac.HEADER_2014, True)          # March 2014: no header
    assert dac.header_fix(row42, row42, "x") == (dac.HEADER_2016, True)          # December 2016: no header
    # October 2016: the header leaves out the Maintenance of Certification field the rows have
    short = [h for h in dac.HEADER_2016 if "Maintenance" not in h]
    assert dac.header_fix(lines(short), row42, "x") == (dac.HEADER_2016, False)
    with pytest.raises(ValueError, match="no known layout fits"):
        dac.header_fix(lines(["NPI", "Other"]), row42, "x")
