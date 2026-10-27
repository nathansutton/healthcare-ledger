"""The HHS Medicaid spending reference source: load rebuilds medicaid.duckdb from a 20-row zipped CSV, download
fetches and checks what releases/medicaid_sources.csv pins."""

from __future__ import annotations

import csv
import hashlib
import zipfile
from datetime import date
from pathlib import Path

import duckdb
import pytest

from conftest import MEDICAID_HEADER, catalog, medicaid_rows, write_medicaid
from healthcare_ledger import sources
from healthcare_ledger.cli import main
from healthcare_ledger.core import filesources
from healthcare_ledger.core.errors import UserError
from healthcare_ledger.sources import medicaid
from healthcare_ledger.sources.medicaid import fetch
from healthcare_ledger.sources.medicaid.load import COLUMNS, MEMBER, TABLE, ZIP, load


@pytest.fixture
def loaded(tmp_path: Path) -> Path:
    write_medicaid(tmp_path / "medicaid")
    assert main(["load", "--source-name", "medicaid", "--source", str(tmp_path / "medicaid"), "--warehouse",
                 str(tmp_path / "wh"), "--work", str(tmp_path / "work")]) == 0
    return tmp_path / "wh"


def test_load_keeps_every_column_typed(loaded, tmp_path):
    con = duckdb.connect(str(loaded / "medicaid.duckdb"), read_only=True)
    assert [r[0] for r in con.execute("SHOW TABLES").fetchall()] == [TABLE]
    assert [(r[0], r[1]) for r in con.execute(f"DESCRIBE {TABLE}").fetchall()] == [
        ("billing_npi", "VARCHAR"), ("servicing_npi", "VARCHAR"), ("hcpcs", "VARCHAR"), ("month", "DATE"),
        ("patients", "BIGINT"), ("claim_lines", "BIGINT"), ("paid", "DOUBLE")]
    # the same count and sum(paid) as a direct read of the CSV; an empty NPI is NULL, codes keep leading zeros
    assert con.execute(f"SELECT count(*), sum(paid), sum(patients), sum(claim_lines) FROM {TABLE}").fetchone() == (
        20, sum(100.25 * i for i in range(20)), sum(12 + i for i in range(20)), sum(20 + 2 * i for i in range(20)))
    assert con.execute(f"SELECT billing_npi, servicing_npi, hcpcs, month FROM {TABLE} WHERE servicing_npi = '2000000000'"
                       ).fetchone() == (None, "2000000000", "00000", date(2018, 1, 1))
    assert con.execute(f"SELECT min(month), max(month) FROM {TABLE}").fetchone() == (date(2018, 1, 1), date(2024, 7, 1))
    con.close()
    assert not list((tmp_path / "work").rglob(MEMBER))           # the extracted CSV is deleted


def test_checks_status_and_reset(loaded, capsys):
    con = catalog(loaded, read_only=True)
    assert medicaid.loaded(con) and medicaid.check_problems(con) == []
    assert medicaid.status(con)["tables"][TABLE] == (20, "2018-01-01", "2024-07-01")
    con.close()
    con = duckdb.connect(str(loaded / "medicaid.duckdb"))
    con.execute(f"DELETE FROM {TABLE}")
    assert medicaid.check_problems(con) == [f"{TABLE}: no rows; reload with `healthcare-ledger load --source-name medicaid`"]
    con.execute(f"ALTER TABLE {TABLE} DROP COLUMN hcpcs")
    assert medicaid.check_problems(con)[0].startswith(f"{TABLE}: columns")
    con.close()
    assert main(["reset", "--source-name", "medicaid", "--warehouse", str(loaded), "--yes"]) == 0
    assert "dropped 1 table(s): medicaid_spending" in capsys.readouterr().out
    con = duckdb.connect(str(loaded / "medicaid.duckdb"))
    assert medicaid.check_problems(con)[0].startswith(f"{TABLE}: missing")
    con.close()


def test_a_failed_load_keeps_the_previous_file(loaded, tmp_path):
    before = hashlib.sha256((loaded / "medicaid.duckdb").read_bytes()).hexdigest()
    with zipfile.ZipFile(tmp_path / "medicaid" / "raw" / ZIP, "w") as z:
        z.writestr(MEMBER, MEDICAID_HEADER + "\n" + medicaid_rows()[1].replace("2019-02", "not a month") + "\n")
    with pytest.raises(Exception):
        load(tmp_path / "medicaid", loaded, tmp_path / "work")
    assert hashlib.sha256((loaded / "medicaid.duckdb").read_bytes()).hexdigest() == before
    assert not (loaded / "medicaid.duckdb.part").exists()


def test_missing_zip_and_a_changed_layout_are_user_errors(tmp_path):
    with pytest.raises(UserError, match=f"lacks raw/{ZIP}.*download --source-name medicaid"):
        load(tmp_path / "medicaid", tmp_path / "wh", tmp_path / "work")
    raw = tmp_path / "medicaid" / "raw"
    raw.mkdir(parents=True)
    with zipfile.ZipFile(raw / ZIP, "w") as z:
        z.writestr(MEMBER, MEDICAID_HEADER.replace("TOTAL_PAID", "TOTAL_AMOUNT") + "\n")
    with pytest.raises(UserError, match="HHS changed the file's layout"):
        load(tmp_path / "medicaid", tmp_path / "wh", tmp_path / "work")
    assert not (tmp_path / "wh" / "medicaid.duckdb").exists()


def test_medicaid_is_a_reference_source_without_published_tables():
    src = sources.by_name("medicaid")
    assert src is medicaid.SOURCE and src.kind == "reference" and not src.versioned and not src.required
    assert src.tables is None and src.snapshots_file is None and src.database == "medicaid"
    assert list(COLUMNS) == ["billing_npi", "servicing_npi", "hcpcs", "month", "patients", "claim_lines", "paid"]


def test_download_streams_the_pinned_zip_and_checks_sha256(tmp_path, monkeypatch):
    data = b"the zip HHS published"
    (tmp_path / "releases").mkdir()
    with open(tmp_path / "releases" / fetch.SOURCES_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fetch.COLUMNS)
        w.writeheader()
        w.writerow({"name": ZIP, "origin": "hhs", "url": "https://example.org/" + ZIP, "bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest()})
    served = {"body": b"changed upstream"}
    calls = []

    def fake_fetch(url, target, tries=4):
        calls.append(url)
        target.write_bytes(served["body"])

    monkeypatch.setattr(filesources, "fetch_url", fake_fetch)
    monkeypatch.setattr(filesources.time, "sleep", lambda s: None)
    problems = fetch.download(tmp_path / "releases", tmp_path / "medicaid")
    assert len(problems) == 1 and problems[0].startswith(f"raw/{ZIP}:") and "upstream file changed" in problems[0]
    assert (tmp_path / "medicaid" / "raw" / f"{ZIP}.mismatch").exists()
    served["body"] = data
    assert fetch.download(tmp_path / "releases", tmp_path / "medicaid") == []
    assert (tmp_path / "medicaid" / "raw" / ZIP).read_bytes() == data
    calls.clear()
    assert fetch.download(tmp_path / "releases", tmp_path / "medicaid", check_only=True) == [] and calls == []


def test_the_pinned_file_is_the_file_load_reads():
    with open(Path(__file__).resolve().parent.parent / "releases" / fetch.SOURCES_FILE, newline="") as f:
        rows = list(csv.DictReader(f))
    assert [r["name"] for r in rows] == [ZIP]
    r = rows[0]
    assert len(r["sha256"]) == 64 and int(r["bytes"]) > 0
    assert (r["origin"], r["url"].rsplit("/", 1)[-1]) == ("hhs", ZIP) or (r["origin"], r["url"]) == ("archive", "")
