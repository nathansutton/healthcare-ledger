"""The AHRQ Compendium reference source: load rebuilds ahrq.duckdb from a hand-made raw directory, download
fetches and checks what releases/ahrq_sources.csv pins."""

from __future__ import annotations

import csv
import gzip
import hashlib
from pathlib import Path

import duckdb
import pytest

from conftest import catalog, write_ahrq
from healthcare_ledger.cli import main
from healthcare_ledger.core.errors import UserError
from healthcare_ledger.sources import ahrq
from healthcare_ledger.sources.ahrq import fetch
from healthcare_ledger.sources.ahrq.load import FILES, TABLES, load


@pytest.fixture
def loaded(tmp_path: Path) -> Path:
    write_ahrq(tmp_path / "ahrq")
    assert main(["load", "--source-name", "ahrq", "--source", str(tmp_path / "ahrq"), "--warehouse", str(tmp_path / "wh"),
                 "--work", str(tmp_path / "work")]) == 0
    return tmp_path / "wh"


def test_load_stacks_each_file_type_by_year(loaded):
    con = duckdb.connect(str(loaded / "ahrq.duckdb"), read_only=True)
    assert sorted(r[0] for r in con.execute("SHOW TABLES").fetchall()) == sorted(TABLES)
    gp = con.execute("SELECT year, source_file, tin_name, pecos_pac_ids, health_sys_id FROM ahrq_group_practice_linkage "
                     "WHERE year = 2022 ORDER BY tin_name").fetchall()
    assert gp == [(2022, "2022-group-practice-linkage-public-file.csv", "CLÉMENT PARTNERS", "Y^Z", None),  # Latin-1, CR
                  (2022, "2022-group-practice-linkage-public-file.csv", "GENERAL HOSPITAL", "H", "HSI1"),
                  (2022, "2022-group-practice-linkage-public-file.csv", "GROUP PRACTICE", "G", None)]  # blank row dropped
    # a zip member keeps its path as the source file; every column is text, "1,200" included
    assert con.execute("SELECT source_file, total_mds FROM ahrq_system WHERE year = 2016").fetchall() == [
        ("2016-compendium-files-2020update/chsp-compendium-2019update.csv", "1,200")]
    # columns are matched by name; a year without one has NULL there
    cols = [r[0] for r in con.execute("DESCRIBE ahrq_system").fetchall()]
    assert cols[:3] == ["year", "source_file", "health_sys_id"] and cols[-1] == "hhco_cnt"
    assert con.execute("SELECT year, hhco_cnt FROM ahrq_system WHERE year IN (2022, 2023) ORDER BY 1").fetchall() == [
        (2022, None), (2023, "2")]
    # .xlsx: header lower-cased, numbers as text, empty cells NULL, rows without an id dropped
    assert con.execute("SELECT year, compendium_os_id, os_mds, health_sys_id FROM ahrq_outpatient_site_linkage "
                       "ORDER BY 1, 2").fetchall() == [(2022, "OS1", "5", "HSI1"), (2022, "OS2", None, None),
                                                       (2023, "OS1", "5", "HSI1"), (2023, "OS2", None, None)]
    con.close()


def test_checks_status_and_reset(loaded, capsys):
    con = catalog(loaded, read_only=True)
    assert ahrq.check_problems(con) == []
    assert ahrq.status(con)["tables"]["ahrq_system"] == (6, 2016, 2023)
    con.close()
    con = duckdb.connect(str(loaded / "ahrq.duckdb"))
    con.execute("DELETE FROM ahrq_nursing_home_linkage WHERE year = 2021")
    con.execute("DROP TABLE ahrq_home_health_linkage")
    problems = ahrq.check_problems(con)
    con.close()
    assert len(problems) == 2 and problems[0].startswith("ahrq_nursing_home_linkage: years and files")
    assert problems[1].startswith("ahrq_home_health_linkage: missing")
    assert main(["reset", "--source-name", "ahrq", "--warehouse", str(loaded), "--yes"]) == 0
    assert "dropped 5 table(s)" in capsys.readouterr().out


def test_a_failed_load_keeps_the_previous_file(loaded, tmp_path):
    before = hashlib.sha256((loaded / "ahrq.duckdb").read_bytes()).hexdigest()
    (tmp_path / "ahrq" / "raw" / FILES["outpatient_site_linkage"][2023]).write_bytes(b"not a workbook")   # read last
    with pytest.raises(Exception):
        load(tmp_path / "ahrq", loaded, tmp_path / "work")
    assert hashlib.sha256((loaded / "ahrq.duckdb").read_bytes()).hexdigest() == before
    assert not (loaded / "ahrq.duckdb.part").exists()


def test_missing_files_are_a_user_error(tmp_path):
    write_ahrq(tmp_path / "ahrq")
    (tmp_path / "ahrq" / "raw" / "2016-compendium-files-2020update.zip").unlink()
    with pytest.raises(UserError, match="lacks 1 file.*2016-compendium-files-2020update.zip.*download --source-name ahrq"):
        load(tmp_path / "ahrq", tmp_path / "wh", tmp_path / "work")
    assert not (tmp_path / "wh" / "ahrq.duckdb").exists()


def _pin(releases: Path, rows: list[tuple[str, str, str, bytes]]) -> None:
    releases.mkdir(parents=True, exist_ok=True)
    with open(releases / fetch.SOURCES_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fetch.COLUMNS)
        w.writeheader()
        for name, origin, url, data in rows:
            w.writerow({"name": name, "origin": origin, "url": url, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})


def test_download_falls_back_to_the_archive_and_checks_sha256(tmp_path, monkeypatch):
    a, b, c = b"x,y\n1,2\n", b"b,c\n3,4\n", b"c,d\n5,6\n"
    _pin(tmp_path / "releases", [("a.csv", "ahrq", fetch.BASE + "a.csv", a), ("b.csv", "ahrq", fetch.BASE + "b.csv", b),
                                 ("c.csv", "wayback", "https://web.archive.org/web/2024id_/" + fetch.BASE + "c.csv", c),
                                 ("d.csv", "ahrq", fetch.BASE + "d.csv", b"what was published")])
    calls = []

    def fake_get(url, timeout=300):
        calls.append(url)
        if url == fetch.BASE + "a.csv":
            return 200, a
        if url == fetch.WAYBACK + fetch.BASE + "b.csv":
            return 200, gzip.compress(b)                          # a capture stored with its gzip encoding
        if url.startswith(fetch.BASE):
            return (200, b"changed upstream") if url.endswith("d.csv") else (202, b"")   # ahrq.gov's answer to scripts
        if url.endswith("2024id_/" + fetch.BASE + "c.csv"):
            return 200, c
        return 404, b""

    monkeypatch.setattr(fetch, "_get", fake_get)
    problems = fetch.download(tmp_path / "releases", tmp_path / "ahrq", pause=0)
    assert [p.split(":")[0] for p in problems] == ["raw/d.csv"] and "upstream file changed" in problems[0]
    raw = tmp_path / "ahrq" / "raw"
    assert (raw / "a.csv").read_bytes() == a and (raw / "b.csv").read_bytes() == b and (raw / "c.csv").read_bytes() == c
    assert (raw / "d.csv.mismatch").exists() and not (raw / "d.csv").exists()
    assert fetch.WAYBACK + fetch.BASE + "c.csv" not in calls       # a wayback row has no further fallback
    # a second run fetches only what is still missing, and --check fetches nothing
    calls.clear()
    fetch.download(tmp_path / "releases", tmp_path / "ahrq", pause=0)
    assert calls == [fetch.BASE + "d.csv"]
    calls.clear()
    assert fetch.download(tmp_path / "releases", tmp_path / "ahrq", check_only=True) == ["raw/d.csv: missing"]
    assert calls == []


def test_the_pinned_files_are_the_files_load_reads():
    with open(Path(__file__).resolve().parent.parent / "releases" / fetch.SOURCES_FILE, newline="") as f:
        rows = list(csv.DictReader(f))
    names = {n.split("/", 1)[0] + ".zip" if "/" in n else n for files in FILES.values() for n in files.values()}
    assert sorted(r["name"] for r in rows) == sorted(names)
    assert {r["origin"] for r in rows} <= {"ahrq", "wayback"}
    assert all(r["url"].endswith(r["name"]) and len(r["sha256"]) == 64 and int(r["bytes"]) > 0 for r in rows)
