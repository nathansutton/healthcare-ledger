"""The exported files are scanned once more on disk, and a Zenodo deposit is built only from a clean, intact export."""

import csv
import hashlib
import json
import zipfile

import duckdb
import pytest

from conftest import main_row, write_zip
from nppes_over_time import audit
from nppes_over_time.cli import main
from nppes_over_time.errors import UserError
from nppes_over_time.package import package


def _export(source, tmp_path, csv_too=False):
    write_zip(source, "20190101", "20190113", [main_row("1000000001", entity_type="1", last_name="A", taxonomy_code_1="207Q00000X",
                                                        license_number_1="036123456", license_state_1="IL")])
    write_zip(source, "20200101", "20200112", [main_row("1000000001", entity_type="1", last_name="B", taxonomy_code_1="207Q00000X",
                                                        license_number_1="036123456", license_state_1="IL")])
    db, out = tmp_path / "w.duckdb", tmp_path / "export"
    assert main(["load", "--source", str(source), "--db", str(db), "--work", str(tmp_path / "work")]) == 0
    assert main(["export", "--db", str(db), "--out", str(out), "--releases", str(tmp_path / "releases")] + (["--csv"] if csv_too else [])) == 0
    return out


def test_audit_catches_what_the_warehouse_checks_cannot(tmp_path):
    # a table written by a buggy export: the warehouse was clean, the file is not
    out = tmp_path / "export"
    (out / "parquet").mkdir(parents=True)
    con = duckdb.connect()
    con.execute(f"""COPY (SELECT '1000000001' AS npi, 'ACME 123-45-6789' AS org_name, '606111234' AS postal_code,
                         '123456789' AS license_number, 'NY' AS license_state, DATE '2020-01-01' AS effective_date)
                    TO '{out}/parquet/provider.parquet'""")
    rows = {(r.table, r.column): r for r in audit.audit(out)}
    assert rows[("provider", "org_name")].status == "FAIL" and rows[("provider", "org_name")].ssn_formatted == 1
    assert rows[("provider", "postal_code")].status == "exempt" and rows[("provider", "postal_code")].nine_digit == 1
    assert rows[("provider", "license_number")].status == "FAIL"
    assert sorted(audit.failures(list(rows.values()))) == [
        "privacy audit: provider.license_number: 1 row(s) hold a possible SSN or tax id",
        "privacy audit: provider.org_name: 1 row(s) hold a possible SSN or tax id"]


def test_package_builds_a_verifiable_deposit(source, tmp_path):
    out = _export(source, tmp_path, csv_too=True)
    dest = tmp_path / "deposit"
    files = package(out, dest)
    names = {f["name"] for f in files}
    assert {"README.md", "CITATION.cff", "manifest.json", "SHA256SUMS", "data_dictionary.csv", "snapshots.csv",
            "privacy_audit.csv", "LICENSE-DATA.txt", "zenodo.json", "parquet_provider.zip", "parquet_provider_taxonomy.zip",
            "parquet_endpoint.zip", "csv_provider.zip", "csv_endpoint.zip"} <= names
    assert len(names) + 1 <= 100
    # PACKAGE_SHA256SUMS covers every other file
    sums = dict(reversed(line.split("  ")) for line in (dest / "PACKAGE_SHA256SUMS").read_text().splitlines())
    assert set(sums) == names
    for name, sha in sums.items():
        assert hashlib.sha256((dest / name).read_bytes()).hexdigest() == sha
    # unzipping next to SHA256SUMS restores the export's paths, and they verify
    unpacked = tmp_path / "unpacked"
    unpacked.mkdir()
    for name in names:
        if name.endswith(".zip"):
            with zipfile.ZipFile(dest / name) as z:
                assert all(i.compress_type == zipfile.ZIP_STORED for i in z.infolist())
                z.extractall(unpacked)
        else:
            (unpacked / name).write_bytes((dest / name).read_bytes())
    for line in (unpacked / "SHA256SUMS").read_text().splitlines():
        sha, path = line.split("  ")
        assert hashlib.sha256((unpacked / path).read_bytes()).hexdigest() == sha, path
    meta = json.loads((dest / "zenodo.json").read_text())["metadata"]
    manifest = json.loads((out / "manifest.json").read_text())
    assert meta["version"] == manifest["release_id"] and meta["license"] == "cc-zero" and meta["upload_type"] == "dataset"
    # --no-csv leaves the CSV copies out
    assert not any(f["name"].startswith("csv_") for f in package(out, tmp_path / "deposit2", include_csv=False))


def test_package_refuses_a_changed_or_unaudited_export(source, tmp_path):
    out = _export(source, tmp_path)
    with pytest.raises(UserError, match="not empty"):
        (tmp_path / "busy").mkdir()
        (tmp_path / "busy" / "x").write_text("x")
        package(out, tmp_path / "busy")
    # an audit failure recorded in the export
    with open(out / "privacy_audit.csv", newline="") as f:
        rows = list(csv.DictReader(f))
    tampered = [dict(r, status="FAIL") if r["column"] == "last_name" else r for r in rows]
    with open(out / "privacy_audit.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(tampered)
    with pytest.raises(UserError, match="provider.last_name"):
        package(out, tmp_path / "d1")
    (out / "privacy_audit.csv").unlink()
    with pytest.raises(UserError, match="privacy_audit.csv"):
        package(out, tmp_path / "d2")
    # a data file changed after export
    out = _export_again(source, tmp_path)
    victim = next((out / "parquet" / "provider").rglob("*.parquet"))
    victim.write_bytes(victim.read_bytes() + b"x")
    with pytest.raises(UserError, match="does not match manifest.json"):
        package(out, tmp_path / "d3")
    assert main(["package", "--out", str(out), "--dest", str(tmp_path / "d4")]) == 1


def _export_again(source, tmp_path):
    out = tmp_path / "export"
    assert main(["export", "--db", str(tmp_path / "w.duckdb"), "--out", str(out), "--releases", str(tmp_path / "releases")]) == 0
    return out
