"""The smoke test: newest recorded month downloaded, loaded, checked, exported and read back in its own directory."""

from __future__ import annotations

import csv
import hashlib
from pathlib import Path

from conftest import main_row, write_zip
from healthcare_ledger.cli import main

ROWS = [main_row("1000000001", entity_type="2", org_name="ACME", practice_city="DENVER", practice_state="CO"),
        main_row("1000000002", entity_type="1", last_name="A")]


def _releases(tmp_path: Path, zips: list[tuple[int, Path]]) -> Path:
    """A releases/ directory pinning each zip under a file:// url, as sources.csv pins the real ones."""
    releases = tmp_path / "releases"
    releases.mkdir()
    with open(releases / "sources.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["snapshot_id", "file", "bytes", "sha256", "origin", "url"])
        for sid, z in zips:
            w.writerow([sid, z.name, z.stat().st_size, hashlib.sha256(z.read_bytes()).hexdigest(), "nber", z.as_uri()])
    return releases


def test_smoke_takes_the_newest_month_end_to_end(source, tmp_path, capsys):
    old = write_zip(source, "20200101", "20200112", ROWS)
    new = write_zip(source, "20200201", "20200209", ROWS + [main_row("1000000003", entity_type="1", last_name="C")])
    releases = _releases(tmp_path, [(20200112, old), (20200209, new)])
    smoke = tmp_path / "smoke"
    assert main(["smoke", "--dir", str(smoke), "--releases", str(releases)]) == 0
    out = capsys.readouterr().out
    assert "smoke test passed" in out and "provider" in out
    assert [p.name for p in (smoke / "raw").iterdir()] == [new.name]
    assert (smoke / "export" / "manifest.json").is_file()
    assert sorted(p.name for p in releases.iterdir()) == ["sources.csv"]   # the real releases/ is only read

    # a re-run reuses the intact zip and rebuilds the rest from scratch
    new.unlink()
    assert main(["smoke", "--dir", str(smoke), "--releases", str(releases)]) == 0


def test_smoke_refuses_a_zip_that_is_not_the_recorded_one(source, tmp_path, capsys):
    z = write_zip(source, "20200101", "20200112", ROWS)
    releases = _releases(tmp_path, [(20200112, z)])
    rows = (releases / "sources.csv").read_text().replace(hashlib.sha256(z.read_bytes()).hexdigest(), "0" * 64)
    (releases / "sources.csv").write_text(rows)
    assert main(["smoke", "--dir", str(tmp_path / "smoke"), "--releases", str(releases)]) == 1
    err = capsys.readouterr().err
    assert "smoke test failed at download" in err and "sha256" in err
    assert not (tmp_path / "smoke" / "warehouse").exists()


def test_smoke_unknown_snapshot_is_an_input_error(source, tmp_path, capsys):
    releases = _releases(tmp_path, [(20200112, write_zip(source, "20200101", "20200112", ROWS))])
    assert main(["smoke", "--dir", str(tmp_path / "smoke"), "--releases", str(releases), "--snapshot", "20990101"]) == 2
    assert "20990101 has no row" in capsys.readouterr().err
