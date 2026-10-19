import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import duckdb

from conftest import main_row, write_zip
from nppes_over_time.cli import main


def test_cli_round_trip(source, tmp_path):
    write_zip(source, "20200101", "20200112", [
        main_row("1000000001", entity_type="1", last_name="A", taxonomy_code_1="207Q00000X", primary_taxonomy_1="Y",
                 other_id_3="12345", other_id_type_3="05", other_id_state_3="CO"),
        main_row("1000000002", entity_type="2", org_name="B"),
    ])
    write_zip(source, "20200201", "20200209", [
        main_row("1000000001", entity_type="1", last_name="A", taxonomy_code_1="207Q00000X", primary_taxonomy_1="Y",
                 taxonomy_code_2="208D00000X", other_id_3="12345", other_id_type_3="05", other_id_state_3="CO"),
        main_row("1000000002", entity_type="2", org_name="B"),
    ])
    db = tmp_path / "w.duckdb"
    assert main(["load", "--source", str(source), "--db", str(db), "--work", str(tmp_path / "work")]) == 0
    assert main(["validate", "--db", str(db)]) == 0
    out = tmp_path / "export"
    assert main(["export", "--db", str(db), "--out", str(out), "--csv"]) == 0
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["snapshots"] == 2 and manifest["distinct_npis"] == 2
    assert manifest["row_counts"] == {"provider": 3, "provider_taxonomy": 3, "provider_other_identifier": 2,
                                      "practice_location": 0, "other_name": 0, "endpoint": 0}
    con = duckdb.connect()
    tax = con.execute(f"SELECT npi, effective_date, slot, taxonomy_code FROM read_parquet('{out}/parquet/provider_taxonomy/*/*.parquet') ORDER BY 1,2,3").fetchall()
    assert [t[2:] for t in tax] == [(1, "207Q00000X"), (1, "207Q00000X"), (2, "208D00000X")]
    prov = con.execute(f"SELECT count(*), count(DISTINCT effective_year) FROM read_parquet('{out}/parquet/provider/*/*.parquet', hive_partitioning=true)").fetchone()
    assert prov == (3, 1)
    assert (out / "csv" / "provider" / "effective_year=2020").exists()
    dd = (out / "data_dictionary.csv").read_text().splitlines()
    assert dd[0] == "table,column,type,cms_header,description" and any(",sex,VARCHAR,Provider Sex Code," in l for l in dd)
    assert "other_name,created_date,DATE,Created Date," in (out / "data_dictionary.csv").read_text()
    assert "provider_rows" in (out / "snapshots.csv").read_text().splitlines()[0]


def test_reexport_removes_stale_partitions_and_keeps_sparse_slots(source, tmp_path):
    import shutil
    from nppes_over_time.export import export_all
    write_zip(source, "20190101", "20190113", [main_row("1000000001", entity_type="1", license_state_2="CO", other_id_issuer_5="BCBS")])
    write_zip(source, "20200101", "20200112", [main_row("1000000001", entity_type="1", license_state_2="TX", other_id_issuer_5="BCBS")])
    db = tmp_path / "w.duckdb"
    assert main(["load", "--source", str(source), "--db", str(db), "--work", str(tmp_path / "work")]) == 0
    out = tmp_path / "export"
    export_all(db, out)
    con = duckdb.connect()
    # a slot populated only in license_state / issuer is kept
    assert con.execute(f"SELECT list(license_state ORDER BY effective_date) FROM read_parquet('{out}/parquet/provider_taxonomy/*/*.parquet')").fetchone() == (["CO", "TX"],)
    assert con.execute(f"SELECT count(*) FROM read_parquet('{out}/parquet/provider_other_identifier/*/*.parquet')").fetchone() == (2,)
    # rebuild with only the 2020 snapshot, re-export to the same dir: the 2019 partition must vanish
    shutil.rmtree(tmp_path / "work", ignore_errors=True)
    (source / "NPPES_Data_Dissemination_20190101.zip").unlink()
    db2 = tmp_path / "w2.duckdb"
    assert main(["load", "--source", str(source), "--db", str(db2), "--work", str(tmp_path / "work")]) == 0
    export_all(db2, out)
    assert con.execute(f"SELECT count(*) FROM read_parquet('{out}/parquet/provider/*/*.parquet')").fetchone() == (1,)
    assert not (out / "parquet" / "provider" / "effective_year=2019").exists() or not any((out / "parquet" / "provider" / "effective_year=2019").iterdir())


def _loaded_db(source, tmp_path):
    write_zip(source, "20200101", "20200112", [
        main_row("1000000001", entity_type="1", last_name="A", taxonomy_code_1="207Q00000X", primary_taxonomy_1="Y"),
        main_row("1000000002", entity_type="2", org_name="B"),
    ])
    db = tmp_path / "w.duckdb"
    assert main(["load", "--source", str(source), "--db", str(db), "--work", str(tmp_path / "work")]) == 0
    return db


def test_manifest_lists_checksummed_files(source, tmp_path):
    db = _loaded_db(source, tmp_path)
    out = tmp_path / "export"
    assert main(["export", "--db", str(db), "--out", str(out), "--csv"]) == 0
    manifest = json.loads((out / "manifest.json").read_text())
    files = manifest["files"]
    assert files
    paths = [e["path"] for e in files]
    assert paths == sorted(paths)
    assert "data_dictionary.csv" in paths and "snapshots.csv" in paths
    assert any(p.startswith("parquet/provider/effective_year=2020/") for p in paths)
    assert "manifest.json" not in paths and "SHA256SUMS" not in paths
    # the manifest lists README and CITATION with checksums; SHA256SUMS covers the data files only
    assert "README.md" in paths and "CITATION.cff" in paths
    sums = (out / "SHA256SUMS").read_text()
    assert "README.md" not in sums and "CITATION.cff" not in sums and "manifest.json" not in sums
    assert sorted(l.split("  ", 1)[1] for l in sums.splitlines()) == [p for p in paths if p not in ("README.md", "CITATION.cff")]
    for e in files:
        f = out / e["path"]
        assert e["bytes"] == f.stat().st_size
        assert e["sha256"] == hashlib.sha256(f.read_bytes()).hexdigest()
    assert manifest["sha256sums_sha256"] == hashlib.sha256((out / "SHA256SUMS").read_bytes()).hexdigest()
    # release id: e<edition>-<last as-of>-<first 8 hex of sha256(SHA256SUMS)>
    assert manifest["release_id"] == f"e1-20200112-{manifest['sha256sums_sha256'][:8]}"
    assert manifest["edition"] == 1
    assert manifest["release_id"] in (out / "README.md").read_text()
    assert f'version: "{manifest["release_id"]}"' in (out / "CITATION.cff").read_text()
    if shutil.which("sha256sum"):
        assert subprocess.run(["sha256sum", "-c", "SHA256SUMS"], cwd=out).returncode == 0


def test_failed_export_leaves_no_manifest(source, tmp_path, monkeypatch):
    import pytest
    from nppes_over_time import export
    db = _loaded_db(source, tmp_path)
    out = tmp_path / "export"
    export.export_all(db, out)
    assert (out / "manifest.json").exists()

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(export, "_copy", boom)
    with pytest.raises(RuntimeError, match="boom"):
        export.export_all(db, out)
    assert not (out / "manifest.json").exists()
    assert not (out / "SHA256SUMS").exists()


def test_parquet_only_export_ignores_stale_csv(source, tmp_path):
    from nppes_over_time.export import export_all
    db = _loaded_db(source, tmp_path)
    out = tmp_path / "export"
    export_all(db, out, csv=True)
    export_all(db, out, csv=False)
    manifest = json.loads((out / "manifest.json").read_text())
    assert not any(e["path"].startswith("csv/") for e in manifest["files"])
    assert manifest["formats"] == ["parquet"]
    assert (out / "csv").exists()


def test_refused_export_keeps_previous_manifest(source, tmp_path):
    import pytest
    from nppes_over_time.export import export_all
    db = _loaded_db(source, tmp_path)
    out = tmp_path / "export"
    export_all(db, out)
    before = {n: (out / n).read_bytes() for n in ("manifest.json", "SHA256SUMS")}
    con = duckdb.connect(str(db))
    con.execute("UPDATE provider_version SET ein = '12-3456789'")
    con.close()
    with pytest.raises(RuntimeError, match="privacy"):
        export_all(db, out)
    for n, data in before.items():
        assert (out / n).read_bytes() == data


def _load_three(source, tmp_path):
    from nppes_over_time.schema import OTHER_NAME
    on_h = ["NPI"] + [c.cms_header for c in OTHER_NAME.columns]
    on = lambda npi, name, typ: {"NPI": npi, on_h[1]: name, on_h[2]: typ, on_h[3]: ""}
    a = dict(entity_type="1", last_name="A", enumeration_date="05/23/2005", taxonomy_code_1="207Q00000X", primary_taxonomy_1="Y")
    write_zip(source, "20200101", "20200112", [
        main_row("1000000001", **a),
        main_row("1000000002", entity_type="2", org_name="B", enumeration_date="01/02/2010"),
    ])
    write_zip(source, "20200201", "20200209", [
        main_row("1000000001", **{**a, "last_name": "A2"}),
        main_row("1000000002", entity_type="2", org_name="B", enumeration_date="01/02/2010"),
    ], othername=[on("1000000002", "B DBA", "3")])
    write_zip(source, "20200301", "20200308", [
        main_row("1000000001", **{**a, "last_name": "A2"}),
        main_row("1000000003", entity_type="1", last_name="C", enumeration_date="03/04/2020"),
    ])
    db = tmp_path / "w.duckdb"
    work = tmp_path / "work"
    assert main(["load", "--source", str(source), "--db", str(db), "--work", str(work)]) == 0
    return db, work


def test_verify_reproduces_every_snapshot(source, tmp_path):
    db, work = _load_three(source, tmp_path)
    assert main(["verify", "--db", str(db), "--source", str(source), "--work", str(work), "--all"]) == 0
    assert main(["verify", "--db", str(db), "--source", str(source), "--work", str(work)]) == 0
    assert not (work / "verify_scratch.duckdb").exists()


def test_verify_detects_edited_warehouse(source, tmp_path):
    from nppes_over_time.verify import verify
    db, work = _load_three(source, tmp_path)
    con = duckdb.connect(str(db))
    con.execute("UPDATE provider_version SET last_name = 'TAMPERED' WHERE npi = '1000000001' AND effective_date = DATE '2020-01-12'")
    con.close()
    problems = verify(db, source, [20200112], work)
    assert len(problems) == 1 and "provider" in problems[0]
    assert main(["verify", "--db", str(db), "--source", str(source), "--work", str(work), "--snapshot", "20200112"]) == 1
    assert not (work / "verify_scratch.duckdb").exists()


def test_verify_reports_missing_zip(source, tmp_path):
    from nppes_over_time.verify import verify
    db, work = _load_three(source, tmp_path)
    (source / "NPPES_Data_Dissemination_20200201.zip").unlink()
    problems = verify(db, source, [20200209], work)
    assert len(problems) == 1 and "no zip" in problems[0]
