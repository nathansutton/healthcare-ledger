import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import duckdb

from conftest import catalog, main_row, write_zip
from healthcare_ledger.cli import main


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
    db = tmp_path / "warehouse"
    assert main(["load", "--source", str(source), "--warehouse", str(db), "--work", str(tmp_path / "work")]) == 0
    assert main(["validate", "--warehouse", str(db)]) == 0
    out = tmp_path / "export"
    assert main(["export", "--warehouse", str(db), "--out", str(out), "--csv"]) == 0
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["snapshots"] == 2 and manifest["distinct_npis"] == 2
    assert manifest["row_counts"] == {"provider": 3, "provider_taxonomy": 3, "provider_practice_location": 0,
                                      "provider_other_name": 0, "provider_endpoint": 0}
    con = duckdb.connect()
    tax = con.execute(f"SELECT npi, effective_date, slot, taxonomy_code FROM read_parquet('{out}/parquet/provider_taxonomy/*/*.parquet') ORDER BY 1,2,3").fetchall()
    assert [t[2:] for t in tax] == [(1, "207Q00000X"), (1, "207Q00000X"), (2, "208D00000X")]
    prov = con.execute(f"SELECT count(*), count(DISTINCT effective_year) FROM read_parquet('{out}/parquet/provider/*/*.parquet', hive_partitioning=true)").fetchone()
    assert prov == (3, 1)
    assert (out / "csv" / "provider" / "effective_year=2020").exists()
    dd = (out / "data_dictionary.csv").read_text().splitlines()
    assert dd[0] == "table,column,type,cms_header,description" and any(",sex,VARCHAR,Provider Sex Code," in l for l in dd)
    assert "provider_other_name,created_date,DATE,Created Date," in (out / "data_dictionary.csv").read_text()
    assert "provider_rows" in (out / "snapshots.csv").read_text().splitlines()[0]
    # the published provider: codes decoded, the primary taxonomy alongside, interval columns only, no tax ids
    import healthcare_ledger
    lib = healthcare_ledger.connect(out)
    cols = [r[0] for r in lib.execute("DESCRIBE provider").fetchall()]
    assert cols[:6] == ["npi", "effective_date", "end_date", "close_reason", "entity_type", "primary_taxonomy_code"]
    assert not {"is_current", "version_hash", "first_snapshot_id", "row_seq", "ein", "parent_org_tin", "effective_year"} & set(cols)
    assert lib.execute("SELECT npi, entity_type, primary_taxonomy_code, close_reason FROM provider ORDER BY npi, effective_date").fetchall() == [
        ("1000000001", "individual", "207Q00000X", "changed"), ("1000000001", "individual", "207Q00000X", None),
        ("1000000002", "organization", None, None)]
    assert lib.execute("SELECT count(*) FROM snapshots").fetchone() == (2,)


def test_reexport_removes_stale_partitions_and_keeps_sparse_slots(source, tmp_path):
    import shutil
    from healthcare_ledger.core.export import export_all
    write_zip(source, "20190101", "20190113", [main_row("1000000001", entity_type="1", license_state_2="CO", other_id_issuer_5="BCBS")])
    write_zip(source, "20200101", "20200112", [main_row("1000000001", entity_type="1", license_state_2="TX", other_id_issuer_5="BCBS")])
    db = tmp_path / "warehouse"
    assert main(["load", "--source", str(source), "--warehouse", str(db), "--work", str(tmp_path / "work")]) == 0
    out = tmp_path / "export"
    export_all(db, out)
    con = duckdb.connect()
    # a slot populated only in license_state is kept
    assert con.execute(f"SELECT list(license_state ORDER BY effective_date) FROM read_parquet('{out}/parquet/provider_taxonomy/*/*.parquet')").fetchone() == (["CO", "TX"],)
    # rebuild with only the 2020 snapshot, re-export to the same dir: the 2019 partition must vanish
    shutil.rmtree(tmp_path / "work", ignore_errors=True)
    (source / "NPPES_Data_Dissemination_20190101.zip").unlink()
    db2 = tmp_path / "warehouse2"
    assert main(["load", "--source", str(source), "--warehouse", str(db2), "--work", str(tmp_path / "work")]) == 0
    export_all(db2, out)
    assert con.execute(f"SELECT count(*) FROM read_parquet('{out}/parquet/provider/*/*.parquet')").fetchone() == (1,)
    assert not (out / "parquet" / "provider" / "effective_year=2019").exists() or not any((out / "parquet" / "provider" / "effective_year=2019").iterdir())


def _loaded_db(source, tmp_path):
    write_zip(source, "20200101", "20200112", [
        main_row("1000000001", entity_type="1", last_name="A", taxonomy_code_1="207Q00000X", primary_taxonomy_1="Y"),
        main_row("1000000002", entity_type="2", org_name="B"),
    ])
    db = tmp_path / "warehouse"
    assert main(["load", "--source", str(source), "--warehouse", str(db), "--work", str(tmp_path / "work")]) == 0
    return db


def test_manifest_lists_checksummed_files(source, tmp_path):
    db = _loaded_db(source, tmp_path)
    out = tmp_path / "export"
    assert main(["export", "--warehouse", str(db), "--out", str(out), "--csv"]) == 0
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
    from healthcare_ledger.core import export
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
    from healthcare_ledger.core.export import export_all
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
    from healthcare_ledger.core.export import export_all
    db = _loaded_db(source, tmp_path)
    out = tmp_path / "export"
    export_all(db, out)
    before = {n: (out / n).read_bytes() for n in ("manifest.json", "SHA256SUMS")}
    con = catalog(db)
    con.execute("UPDATE provider_version SET ein = '12-3456789'")
    con.close()
    with pytest.raises(RuntimeError, match="privacy"):
        export_all(db, out)
    for n, data in before.items():
        assert (out / n).read_bytes() == data


def _load_three(source, tmp_path):
    from healthcare_ledger.sources.nppes.schema import OTHER_NAME
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
    db = tmp_path / "warehouse"
    work = tmp_path / "work"
    assert main(["load", "--source", str(source), "--warehouse", str(db), "--work", str(work)]) == 0
    return db, work


def test_verify_reproduces_every_snapshot(source, tmp_path):
    db, work = _load_three(source, tmp_path)
    assert main(["verify", "--warehouse", str(db), "--source", str(source), "--work", str(work), "--all"]) == 0
    assert main(["verify", "--warehouse", str(db), "--source", str(source), "--work", str(work)]) == 0
    assert not (work / "verify_scratch").exists()


def test_verify_detects_edited_warehouse(source, tmp_path):
    from healthcare_ledger.sources.nppes.verify import verify
    db, work = _load_three(source, tmp_path)
    con = catalog(db)
    con.execute("UPDATE provider_version SET last_name = 'TAMPERED' WHERE npi = '1000000001' AND effective_date = DATE '2020-01-12'")
    con.close()
    problems = verify(db, source, [20200112], work)
    assert len(problems) == 1 and "provider" in problems[0]
    assert main(["verify", "--warehouse", str(db), "--source", str(source), "--work", str(work), "--snapshot", "20200112"]) == 1
    assert not (work / "verify_scratch").exists()


def test_verify_reports_missing_zip(source, tmp_path):
    from healthcare_ledger.sources.nppes.verify import verify
    db, work = _load_three(source, tmp_path)
    (source / "NPPES_Data_Dissemination_20200201.zip").unlink()
    problems = verify(db, source, [20200209], work)
    assert len(problems) == 1 and "no zip" in problems[0]


def test_monthly_files_one_per_snapshot(source, tmp_path):
    from healthcare_ledger.core.export import export_all
    db, _ = _load_three(source, tmp_path)
    out = tmp_path / "export"
    export_all(db, out, monthly_files=True)
    d = out / "parquet" / "provider_monthly"
    assert sorted(f.name for f in d.iterdir()) == ["provider_2020-01.parquet", "provider_2020-02.parquet", "provider_2020-03.parquet"]
    con = duckdb.connect()
    rows = con.execute(f"SELECT snapshot_id, as_of_date::VARCHAR, npi, effective_date::VARCHAR, last_name, org_name "
                       f"FROM read_parquet('{d}/*.parquet') ORDER BY 1, 3").fetchall()
    assert rows == [
        (20200112, "2020-01-12", "1000000001", "2020-01-12", "A", None),
        (20200112, "2020-01-12", "1000000002", "2020-01-12", None, "B"),
        (20200209, "2020-02-09", "1000000001", "2020-02-09", "A2", None),  # new version
        (20200209, "2020-02-09", "1000000002", "2020-01-12", None, "B"),   # unchanged: same version
        (20200308, "2020-03-08", "1000000001", "2020-02-09", "A2", None),  # 1000000002 left the file
        (20200308, "2020-03-08", "1000000003", "2020-03-08", "C", None),
    ]
    # wide, in CMS column order, one value of snapshot_id per file
    cols = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM '{d}/provider_2020-01.parquet'").fetchall()]
    assert cols[:5] == ["snapshot_id", "as_of_date", "npi", "effective_date", "entity_type"]
    assert "taxonomy_code_15" in cols and "other_id_1" not in cols and "ein" not in cols and len(cols) == 131
    # each month equals the as-of query over the version table
    for m, as_of in (("2020-01", "2020-01-12"), ("2020-02", "2020-02-09"), ("2020-03", "2020-03-08")):
        asof = con.execute(f"SELECT npi, last_name FROM read_parquet('{out}/parquet/provider/*/*.parquet') "
                           f"WHERE effective_date <= DATE '{as_of}' AND (end_date IS NULL OR end_date > DATE '{as_of}') ORDER BY 1").fetchall()
        joined = con.execute(f"SELECT m.npi, p.last_name FROM '{d}/provider_{m}.parquet' m JOIN "
                             f"read_parquet('{out}/parquet/provider/*/*.parquet') p USING (npi, effective_date) ORDER BY 1").fetchall()
        assert joined == asof
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["row_counts"]["provider_monthly"] == 6
    assert "parquet/provider_monthly/provider_2020-02.parquet" in (out / "SHA256SUMS").read_text()
    assert "provider_monthly,taxonomy_code_1,VARCHAR,Healthcare Provider Taxonomy Code_1," in (out / "data_dictionary.csv").read_text()
    readme = (out / "README.md").read_text()
    assert "## One file per month" in readme and "provider_2020-03.parquet" in readme


def test_monthly_files_canonical_slots_and_suppression(source, tmp_path):
    from healthcare_ledger.core.export import export_all
    # CMS lists the primary taxonomy second; the monthly file puts it in slot 1, like provider_taxonomy
    write_zip(source, "20200101", "20200112", [main_row("1000000001", entity_type="1", taxonomy_code_1="363L00000X",
                                                        taxonomy_code_2="207Q00000X", primary_taxonomy_2="Y")])
    db = tmp_path / "warehouse"
    assert main(["load", "--source", str(source), "--warehouse", str(db), "--work", str(tmp_path / "work")]) == 0
    out = tmp_path / "export"
    export_all(db, out, monthly_files=True)
    con = duckdb.connect()
    f = out / "parquet" / "provider_monthly" / "provider_2020-01.parquet"
    assert con.execute(f"SELECT taxonomy_code_1, primary_taxonomy_1, taxonomy_code_2 FROM '{f}'").fetchone() == \
        ("207Q00000X", "Y", "363L00000X")
    # the tax-id columns, always NULL (core.privacy rule 1), are not published at all
    assert not {"ein", "parent_org_tin"} & {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM '{f}'").fetchall()}
    assert con.execute(f"SELECT primary_taxonomy_code FROM read_parquet('{out}/parquet/provider/*/*.parquet')").fetchone() == ("207Q00000X",)
    tax = con.execute(f"SELECT slot, taxonomy_code FROM read_parquet('{out}/parquet/provider_taxonomy/*/*.parquet') ORDER BY 1").fetchall()
    assert tax == [(1, "207Q00000X"), (2, "363L00000X")]


def test_export_without_monthly_files_ignores_stale_ones_and_keeps_continuity(source, tmp_path):
    from healthcare_ledger.core.export import export_all
    db = _loaded_db(source, tmp_path)
    out, releases = tmp_path / "export", tmp_path / "releases"
    export_all(db, out, monthly_files=True, releases=releases)
    # dropping the monthly files is a format choice, not a removed column: continuity still passes
    export_all(db, out, releases=releases)
    manifest = json.loads((out / "manifest.json").read_text())
    assert not any(e["path"].startswith("parquet/provider_monthly/") for e in manifest["files"])
    assert "provider_monthly" not in manifest["row_counts"]
    assert "provider_monthly" not in (out / "data_dictionary.csv").read_text()
    assert (out / "parquet" / "provider_monthly").exists()
    # and adding them back is fine too
    export_all(db, out, monthly_files=True, releases=releases)


def test_monthly_files_refuse_two_snapshots_in_one_month(source, tmp_path):
    import pytest
    from healthcare_ledger.core.errors import UserError
    from healthcare_ledger.core.export import export_all
    write_zip(source, "20200101", "20200105", [main_row("1000000001", entity_type="1")])
    write_zip(source, "20200115", "20200119", [main_row("1000000001", entity_type="1")])
    db = tmp_path / "warehouse"
    assert main(["load", "--source", str(source), "--warehouse", str(db), "--work", str(tmp_path / "work")]) == 0
    with pytest.raises(UserError, match="2020-01"):
        export_all(db, tmp_path / "export", monthly_files=True)
