"""Every error the operator can hit is one line on stderr, with cause and next step, and leaves no files behind."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from conftest import main_row, write_zip
from nppes_over_time import cli
from nppes_over_time.cli import main
from nppes_over_time.snapshots import extract_member_with_unzip


def _one_line(capsys) -> str:
    err = capsys.readouterr().err
    assert err.startswith("error: ") and err.count("\n") == 1, err
    assert "Traceback" not in err
    return err


@pytest.mark.parametrize("cmd", ["validate", "export", "status", "verify", "backfill-fingerprints"])
def test_missing_db_is_one_line_and_creates_nothing(cmd, tmp_path, source, capsys):
    db = tmp_path / "missing" / "x.duckdb"
    args = [cmd, "--db", str(db)]
    if cmd == "verify":
        write_zip(source, "20200101", "20200112", [main_row("1000000001", entity_type="1")])
        args += ["--source", str(source)]
    if cmd == "export":
        args += ["--out", str(tmp_path / "export")]
    assert main(args) == 2
    err = _one_line(capsys)
    assert str(db) in err and "not found" in err and "load" in err
    assert not db.exists() and not db.parent.exists()
    assert not (tmp_path / "export").exists()


def test_missing_source_dir_creates_no_db(tmp_path, capsys):
    db = tmp_path / "new.duckdb"
    assert main(["load", "--source", str(tmp_path / "nowhere"), "--db", str(db)]) == 2
    err = _one_line(capsys)
    assert "does not exist" in err and "download" in err
    assert not db.exists() and not (tmp_path / "work").exists()


def test_empty_source_dir_creates_no_db(tmp_path, source, capsys):
    db = tmp_path / "new.duckdb"
    (source / "NPPES_Data_Dissemination_20200101.zip.part").write_bytes(b"PK")  # a half download does not count
    assert main(["load", "--source", str(source), "--db", str(db), "--work", str(tmp_path / "work")]) == 2
    err = _one_line(capsys)
    assert "no NPPES_Data_Dissemination_*.zip files" in err and "download" in err
    assert not db.exists() and not (tmp_path / "work").exists()
    assert sorted(os.listdir(tmp_path)) == ["zips"]


def test_discover_empty_dir_is_an_error(source, capsys):
    assert main(["discover", "--source", str(source)]) == 2
    _one_line(capsys)


def test_html_saved_as_zip_is_one_line(source, tmp_path, capsys):
    write_zip(source, "20200101", "20200112", [main_row("1000000001", entity_type="1")])
    (source / "NPPES_Data_Dissemination_February_2020.zip").write_text("<HTML>Access Denied</HTML>")
    assert main(["discover", "--source", str(source)]) == 1
    err = _one_line(capsys)
    assert "not a zip file" in err and "re-download" in err


def test_missing_unzip_for_deflate64(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(RuntimeError, match="needs the `unzip` command"):
        extract_member_with_unzip(tmp_path / "NPPES_Data_Dissemination_20180401.zip", "npidata.csv", tmp_path / "out.csv")
    assert not (tmp_path / "out.csv").exists()


def test_loader_refusal_is_one_line(source, tmp_path, monkeypatch, capsys):
    write_zip(source, "20200101", "20200112", [main_row("1000000001", entity_type="1")])

    def boom(snapshot, stream, work_dir):
        raise RuntimeError(f"{snapshot.zip_path.name} needs the `unzip` command for member x (Deflate64)")

    monkeypatch.setattr("nppes_over_time.loader.extract_member", boom)
    assert main(["load", "--source", str(source), "--db", str(tmp_path / "w.duckdb"), "--work", str(tmp_path / "work")]) == 1
    assert "unzip" in _one_line(capsys)


def test_privacy_refusal_names_column_count_and_next_step(source, tmp_path, capsys):
    import duckdb
    write_zip(source, "20200101", "20200112", [main_row("1000000001", entity_type="1")])
    db = tmp_path / "w.duckdb"
    assert main(["load", "--source", str(source), "--db", str(db), "--work", str(tmp_path / "work")]) == 0
    con = duckdb.connect(str(db))
    con.execute("UPDATE provider_version SET ein = '12-3456789'")
    con.close()
    assert main(["export", "--db", str(db), "--out", str(tmp_path / "export"), "--releases", str(tmp_path / "rel")]) == 1
    err = _one_line(capsys)
    assert "ein is populated: 1 row(s)" in err
    assert "Do not publish" in err and "maintainer" in err and "MAINTAINING.md" in err
    assert not (tmp_path / "export" / "manifest.json").exists()
    assert not (tmp_path / "rel").exists()


def test_unexpected_exception_is_one_line_without_verbose(monkeypatch, capsys, tmp_path):
    def explode(a):
        raise ZeroDivisionError("division by zero")

    monkeypatch.setattr(cli, "cmd_status", explode)  # build_parser binds the command at each main() call
    db = tmp_path / "x.duckdb"
    db.write_bytes(b"")
    assert main(["status", "--db", str(db)]) == 1
    err = _one_line(capsys)
    assert "ZeroDivisionError" in err and "-v" in err
    with pytest.raises(ZeroDivisionError):
        main(["-v", "status", "--db", str(db)])


def test_help_names_defaults():
    p = cli.build_parser()
    text = p.format_help()
    assert "monthly" in text and "backfill-fingerprints" in text and "download" in text
    sub = p._subparsers._group_actions[0].choices
    assert "data/nppes.duckdb" in sub["export"].format_help()
    assert "data/raw" in sub["load"].format_help()
