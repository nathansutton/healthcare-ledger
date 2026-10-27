"""A source is one declaration: a fake fourth source registered at runtime reaches validate, the export
dictionary, the fingerprint gates and the continuity check without any change to those modules, and a fake
reference source is loaded, validated and shown by status but left out of the export and the release record."""

from __future__ import annotations

import csv
import dataclasses
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest

from conftest import catalog, main_row, write_zip
from healthcare_ledger import sources
from healthcare_ledger.cli import main
from healthcare_ledger.core import engine
from healthcare_ledger.core.errors import UserError
from healthcare_ledger.core.export import export_all
from healthcare_ledger.core.validate import validate
from healthcare_ledger.sources import NPPES

# the engine's module functions read only these names off a spec
SPEC = SimpleNamespace(snapshot_table="fake_snapshot", stream_table="fake_snapshot_stream", key="fid",
                       streams=(SimpleNamespace(name="fake_thing"),))


def fake_source() -> sources.Source:
    def snapshots_sql() -> str:
        return ("SELECT s.snapshot_id, s.as_of_date, ss.content_fingerprint AS fake_thing_fingerprint FROM fake_snapshot s "
                "JOIN fake_snapshot_stream ss USING (snapshot_id) ORDER BY 1")

    return dataclasses.replace(
        NPPES, name="fake", database="fake", title="FAKE", key="fid", streams=SPEC.streams, required=False,
        snapshots_file="fake_snapshots.csv", sources_file="fake_sources.csv", default_source_dir="data/fake",
        snapshot_noun="quarters", warehouse_cls=object, discover=lambda d: [],
        loaded=lambda con: engine.loaded(con, SPEC), finalize=lambda con: engine.finalize(con, SPEC),
        reset=lambda con: engine.reset(con, SPEC), check_problems=lambda con: [],
        stored_fingerprints=lambda con: engine.stored_fingerprints(con, SPEC),
        recompute_fingerprints=lambda con: engine.recompute_fingerprints(con, SPEC),
        export_tables=lambda: {"fake_thing": ("SELECT fid, effective_date, end_date, version_hash FROM fake_thing_version", False)},
        data_dictionary_rows=lambda: [("fake_thing", "fid", "VARCHAR", "FID", "Fake key")],
        snapshots_sql=snapshots_sql,
        validate_streams=lambda: [("fake_thing", "fid", "fake_snapshot_stream", False)],
        status=lambda con: {"quarters": 1}, check_sources=lambda con, releases: None,
        write_sources=lambda con, releases: 0, download=lambda a: 0, download_options=())


def build(source: Path, tmp_path: Path) -> Path:
    """An NPPES warehouse of one snapshot plus one fake quarter in its own file, its fingerprint stored as the loader would."""
    write_zip(source, "20200101", "20200112", [main_row("1000000001", entity_type="1", last_name="A")])
    db = tmp_path / "warehouse"
    assert main(["load", "--source", str(source), "--warehouse", str(db), "--work", str(tmp_path / "work")]) == 0
    con = duckdb.connect(str(db / "fake.duckdb"))
    con.execute("CREATE TABLE fake_snapshot (snapshot_id INTEGER, as_of_date DATE)")
    con.execute("CREATE TABLE fake_snapshot_stream (snapshot_id INTEGER, stream VARCHAR, n_keys BIGINT, n_new BIGINT, "
                "n_changed BIGINT, n_absent BIGINT, csv_mode VARCHAR, content_fingerprint VARCHAR)")
    con.execute("CREATE TABLE fake_thing_version (fid VARCHAR, effective_date DATE, end_date DATE, row_seq INTEGER, "
                "is_current BOOLEAN, close_reason VARCHAR, version_hash VARCHAR, first_snapshot_id INTEGER, last_snapshot_id INTEGER)")
    con.execute("CREATE TABLE fake_thing_state (fid VARCHAR, version_hash VARCHAR)")
    con.execute("INSERT INTO fake_snapshot VALUES (20200331, DATE '2020-03-31')")
    con.execute("INSERT INTO fake_thing_version VALUES ('F1', DATE '2020-03-31', NULL, 1, true, NULL, 'h1', 20200331, 20200331)")
    con.execute("INSERT INTO fake_thing_state VALUES ('F1', 'h1')")
    con.execute("INSERT INTO fake_snapshot_stream VALUES (20200331, 'fake_thing', 1, 1, 0, 0, 'strict', NULL)")
    fp = engine.recompute_fingerprints(con, SPEC)[(20200331, "fake_thing")]
    con.execute("UPDATE fake_snapshot_stream SET content_fingerprint = ?", [fp])
    con.close()
    return db


@pytest.fixture
def registered(monkeypatch) -> sources.Source:
    fake = fake_source()
    monkeypatch.setattr(sources, "SOURCES", (*sources.SOURCES, fake))
    return fake


def test_validate_checks_a_registered_source(source, tmp_path, registered):
    db = build(source, tmp_path)
    assert validate(db) == []
    con = catalog(db)
    con.execute("UPDATE fake_thing_version SET is_current = false")
    con.close()
    assert "fake_thing_version: is_current / end_date / close_reason inconsistency: got 1, expected 0" in validate(db)


def test_export_publishes_and_gates_a_registered_source(source, tmp_path, registered):
    db = build(source, tmp_path)
    releases = tmp_path / "releases"
    out = tmp_path / "export"
    rid = export_all(db, out, releases=releases)
    with open(out / "data_dictionary.csv", newline="") as f:
        assert ("fake_thing", "fid", "VARCHAR") in {(r["table"], r["column"], r["type"]) for r in csv.DictReader(f)}
    assert (out / "parquet" / "fake_thing.parquet").exists()
    assert (releases / rid / "fake_snapshots.csv").exists()          # in the release record, so continuity sees it

    # the load-time fingerprint gate covers it
    con = catalog(db)
    con.execute("UPDATE fake_thing_version SET version_hash = 'h2'")
    con.close()
    with pytest.raises(UserError, match="snapshot 20200331 fake_thing: stored fingerprint"):
        export_all(db, out, releases=releases)

    # and so does continuity with the release just recorded
    con = catalog(db)
    con.execute("UPDATE fake_thing_state SET version_hash = 'h2'")
    con.execute("UPDATE fake_snapshot_stream SET content_fingerprint = ?",
                [engine.recompute_fingerprints(con, SPEC)[(20200331, "fake_thing")]])
    con.close()
    with pytest.raises(UserError, match=f"snapshot 20200331 fake_thing: fingerprint .* differs from release {rid}"):
        export_all(db, out, releases=releases)



def test_status_prints_a_registered_source(source, tmp_path, registered, capsys):
    db = build(source, tmp_path)
    assert main(["status", "--warehouse", str(db)]) == 0
    assert '"fake"' in capsys.readouterr().out


def fake_reference(calls: list) -> sources.Source:
    """A reference source like AHRQ's: one table in its own file, rebuilt wholesale by ``load``; the export
    must never ask it for versioned things."""
    def load(source_dir: Path, warehouse: Path, work: Path) -> dict[str, int]:
        calls.append(("load", source_dir.name))
        warehouse.mkdir(parents=True, exist_ok=True)
        con = duckdb.connect(str(warehouse / "ref.duckdb"))
        con.execute("CREATE OR REPLACE TABLE ref_thing AS SELECT 2020 AS year, 'R1' AS rid")
        con.close()
        return {"ref_thing": 1}

    def loaded(con) -> bool:
        return bool(con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name = 'ref_thing'").fetchone()[0])

    def check_problems(con) -> list[str]:
        calls.append(("check_problems",))
        return [f"ref_thing: {n} row(s) without a year" for n, in con.execute(
            "SELECT count(*) FROM ref_thing WHERE year IS NULL").fetchall() if n]

    return sources.Source(
        name="ref", database="ref", title="REF", kind="reference", required=False, sources_file="ref_sources.csv",
        default_source_dir="data/ref", loaded=loaded, reset=lambda con: [], check_problems=check_problems,
        status=lambda con: {"tables": {"ref_thing": con.execute("SELECT count(*) FROM ref_thing").fetchone()[0]}},
        download=lambda a: 0, load=load)


def test_a_source_declares_the_fields_of_its_kind():
    ref = fake_reference([])
    with pytest.raises(ValueError, match="reference source ref: lacks load"):
        dataclasses.replace(ref, load=None)
    with pytest.raises(ValueError, match="sets snapshots_file, which only the other kind has"):
        dataclasses.replace(ref, snapshots_file="ref_snapshots.csv")
    with pytest.raises(ValueError, match="versioned source nppes: lacks discover"):
        dataclasses.replace(NPPES, discover=None)
    with pytest.raises(ValueError, match="kind must be"):
        dataclasses.replace(ref, kind="other")


def test_a_reference_source_is_loaded_validated_and_shown_but_not_released(source, tmp_path, monkeypatch, capsys):
    calls: list = []
    ref = fake_reference(calls)
    monkeypatch.setattr(sources, "SOURCES", (*sources.SOURCES, ref))
    write_zip(source, "20200101", "20200112", [main_row("1000000001", entity_type="1", last_name="A")])
    db = tmp_path / "warehouse"
    assert main(["load", "--source", str(source), "--warehouse", str(db), "--work", str(tmp_path / "work")]) == 0
    assert main(["load", "--source-name", "ref", "--source", str(tmp_path / "refdir"), "--warehouse", str(db)]) == 0
    assert ("load", "refdir") in calls and "ref_thing 1 rows" in capsys.readouterr().out
    assert main(["load", "--source-name", "ref", "--limit", "1", "--warehouse", str(db)]) == 2
    assert "reference source" in capsys.readouterr().err
    assert main(["sources", "--source-name", "ref", "--warehouse", str(db)]) == 2
    assert "by hand" in capsys.readouterr().err

    assert main(["status", "--warehouse", str(db)]) == 0
    assert '"ref"' in capsys.readouterr().out
    calls.clear()
    assert validate(db) == []
    assert calls == [("check_problems",)]
    con = catalog(db)
    con.execute("UPDATE ref_thing SET year = NULL")
    con.close()
    assert validate(db) == ["ref_thing: 1 row(s) without a year"]

    # the export, its gates and the release record leave it out; NPPES alone is released
    releases = tmp_path / "releases"
    out = tmp_path / "export"
    calls.clear()
    rid = export_all(db, out, releases=releases)
    assert calls == []
    with open(out / "data_dictionary.csv", newline="") as f:
        assert not any(r["table"].startswith("ref") for r in csv.DictReader(f))
    assert not (out / "parquet" / "ref_thing").exists() and not (out / "parquet" / "ref_thing.parquet").exists()
    assert not [p.name for p in (releases / rid).iterdir() if "ref" in p.name]
    assert export_all(db, out, releases=releases)     # continuity holds with it loaded
