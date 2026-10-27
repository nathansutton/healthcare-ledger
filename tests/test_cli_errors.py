"""Every error the operator can hit is one line on stderr, with cause and next step, and leaves no files behind."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from conftest import catalog, main_row, write_zip
from healthcare_ledger import cli
from healthcare_ledger.cli import main
from healthcare_ledger.sources.nppes.snapshots import extract_member_with_unzip


def _one_line(capsys) -> str:
    err = capsys.readouterr().err
    assert err.startswith("error: ") and err.count("\n") == 1, err
    assert "Traceback" not in err
    return err


@pytest.mark.parametrize("cmd", ["validate", "export", "status", "verify"])
def test_missing_db_is_one_line_and_creates_nothing(cmd, tmp_path, source, capsys):
    db = tmp_path / "missing" / "warehouse"
    args = [cmd, "--warehouse", str(db)]
    if cmd == "verify":
        write_zip(source, "20200101", "20200112", [main_row("1000000001", entity_type="1")])
        args += ["--source", str(source)]
    if cmd == "export":
        args += ["--out", str(tmp_path / "export")]
    assert main(args) == 2
    err = _one_line(capsys)
    assert str(db) in err and "has no nppes.duckdb" in err and "load" in err
    assert not db.exists() and not db.parent.exists()
    assert not (tmp_path / "export").exists()


def test_missing_source_dir_creates_no_db(tmp_path, capsys):
    db = tmp_path / "warehouse"
    assert main(["load", "--source", str(tmp_path / "nowhere"), "--warehouse", str(db)]) == 2
    err = _one_line(capsys)
    assert "does not exist" in err and "download" in err
    assert not db.exists() and not (tmp_path / "work").exists()


def test_empty_source_dir_creates_no_db(tmp_path, source, capsys):
    db = tmp_path / "warehouse"
    (source / "NPPES_Data_Dissemination_20200101.zip.part").write_bytes(b"PK")  # a half download does not count
    assert main(["load", "--source", str(source), "--warehouse", str(db), "--work", str(tmp_path / "work")]) == 2
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

    monkeypatch.setattr("healthcare_ledger.sources.nppes.loader.extract_member", boom)
    assert main(["load", "--source", str(source), "--warehouse", str(tmp_path / "warehouse"), "--work", str(tmp_path / "work")]) == 1
    assert "unzip" in _one_line(capsys)


def test_privacy_refusal_names_column_count_and_next_step(source, tmp_path, capsys):
    import duckdb
    write_zip(source, "20200101", "20200112", [main_row("1000000001", entity_type="1")])
    db = tmp_path / "warehouse"
    assert main(["load", "--source", str(source), "--warehouse", str(db), "--work", str(tmp_path / "work")]) == 0
    con = catalog(db)
    con.execute("UPDATE provider_version SET ein = '12-3456789'")
    con.close()
    assert main(["export", "--warehouse", str(db), "--out", str(tmp_path / "export"), "--releases", str(tmp_path / "rel")]) == 1
    err = _one_line(capsys)
    assert "ein is populated: 1 row(s)" in err
    assert "Do not publish" in err and "maintainer" in err and "MAINTAINING.md" in err
    assert not (tmp_path / "export" / "manifest.json").exists()
    assert not (tmp_path / "rel").exists()


def test_unexpected_exception_is_one_line_without_verbose(monkeypatch, capsys, tmp_path):
    def explode(a):
        raise ZeroDivisionError("division by zero")

    monkeypatch.setattr(cli, "cmd_status", explode)  # build_parser binds the command at each main() call
    db = tmp_path / "warehouse"
    db.mkdir()
    assert main(["status", "--warehouse", str(db)]) == 1
    err = _one_line(capsys)
    assert "ZeroDivisionError" in err and "-v" in err
    with pytest.raises(ZeroDivisionError):
        main(["-v", "status", "--warehouse", str(db)])


def test_help_names_defaults():
    p = cli.build_parser()
    text = p.format_help()
    assert "monthly" in text and "download" in text
    sub = p._subparsers._group_actions[0].choices
    assert "data/warehouse" in sub["export"].format_help()
    assert "data/raw" in sub["load"].format_help()


def test_load_help_names_every_source(capsys):
    with pytest.raises(SystemExit) as e:
        main(["load", "--source-name", "ppef", "--help"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert "--source-name {nppes,ppef,pos,ahrq,medicaid}" in out


def test_download_refuses_an_option_of_another_source(tmp_path, capsys):
    assert main(["download", "--source-name", "pos", "--dest", str(tmp_path / "pos"), "--probe"]) == 2
    err = _one_line(capsys)
    assert "--probe" in err and "--source-name pos" in err
    assert not (tmp_path / "pos").exists()


def test_reset_needs_a_source_name(tmp_path):
    with pytest.raises(SystemExit) as e:
        main(["reset", "--warehouse", str(tmp_path / "warehouse"), "--yes"])
    assert e.value.code == 2


def test_missing_quarter_dir_is_one_line(tmp_path, capsys):
    db = tmp_path / "warehouse"
    assert main(["load", "--source-name", "ppef", "--source", str(tmp_path / "nope"), "--warehouse", str(db)]) == 2
    err = _one_line(capsys)
    assert "PPEF source directory" in err and "does not exist" in err
    assert not db.exists()
