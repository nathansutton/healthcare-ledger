"""healthcare_ledger.connect() on a release as a researcher downloads it: in full, in part, or pointed at the wrong folder."""

from __future__ import annotations

import shutil
from pathlib import Path

import duckdb
import pytest

import healthcare_ledger
from healthcare_ledger.cli import main
from conftest import main_row, write_zip


@pytest.fixture(scope="module")
def release(tmp_path_factory) -> Path:
    tmp = tmp_path_factory.mktemp("read")
    src = tmp / "zips"
    src.mkdir()
    person = dict(entity_type="1", last_name="A", first_name="B", taxonomy_code_1="207Q00000X", primary_taxonomy_1="Y")
    write_zip(src, "20190101", "20190113", [main_row("1000000001", **person, practice_state="CO")])
    write_zip(src, "20200101", "20200112", [main_row("1000000001", **person, practice_state="TX")])
    db = tmp / "warehouse"
    assert main(["load", "--source", str(src), "--warehouse", str(db), "--work", str(tmp / "work")]) == 0
    out = tmp / "export"
    assert main(["export", "--warehouse", str(db), "--out", str(out), "--releases", str(tmp / "releases")]) == 0
    return out


@pytest.fixture
def partial(release, tmp_path) -> Path:
    """What a researcher has after downloading parquet_provider.zip and the metadata files: no other table."""
    d = tmp_path / "download"
    d.mkdir()
    for f in release.iterdir():
        if f.is_file():
            shutil.copy2(f, d / f.name)
    shutil.copytree(release / "parquet" / "provider", d / "parquet" / "provider")
    return d


def test_full_release_reads_every_table(release):
    con = healthcare_ledger.connect(release)
    assert con.execute("SELECT count(*) FROM provider").fetchone() == (2,)
    assert con.execute("SELECT count(*) FROM provider_taxonomy").fetchone() == (2,)


def test_a_table_not_downloaded_names_its_zip(partial):
    con = healthcare_ledger.connect(partial)
    assert con.execute("SELECT count(*) FROM provider").fetchone() == (2,)
    for sql in ("SELECT count(*) FROM provider_taxonomy",
                "SELECT npi, taxonomy_code FROM provider_taxonomy WHERE npi = '1000000001'"):
        with pytest.raises(duckdb.Error, match=r"provider_taxonomy is in release e1-\S+ but not downloaded: get "
                                               r"parquet_provider_taxonomy\.zip"):
            con.execute(sql).fetchall()


def test_metadata_only_still_opens(partial):
    shutil.rmtree(partial / "parquet")
    con = healthcare_ledger.connect(partial)
    assert con.execute("SELECT count(*) FROM snapshots").fetchone() == (2,)
    with pytest.raises(duckdb.Error, match="parquet_provider.zip"):
        con.execute("SELECT count(*) FROM provider").fetchall()


def test_the_parquet_folder_points_back_to_the_release(release):
    with pytest.raises(ValueError, match=rf"has no manifest\.json; did you mean {release}\?"):
        healthcare_ledger.connect(release / "parquet")


def test_a_parent_folder_points_down_to_the_release(release):
    with pytest.raises(ValueError, match=rf"did you mean {release}\?"):
        healthcare_ledger.connect(release.parent)


def test_no_folder_says_what_to_pass():
    with pytest.raises(ValueError, match=r'connect\(\) needs a directory: .*connect\("export"\)'):
        healthcare_ledger.connect()
