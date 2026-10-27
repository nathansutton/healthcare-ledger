"""Release policy: fingerprints, release ids, continuity check, editions, release records."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import shutil
from pathlib import Path

import duckdb
import pytest

from conftest import catalog, main_row, write_zip
from healthcare_ledger.core import release
from healthcare_ledger.cli import main
from healthcare_ledger.core.errors import UserError
from healthcare_ledger.core.export import export_all
from healthcare_ledger.core.versioning import fingerprint_sql
from healthcare_ledger import sources
from healthcare_ledger.sources import NPPES


def _rows(variant: str = "a"):
    s1 = [main_row("1000000001", entity_type="2", org_name="ACME", practice_address_1="1 MAIN ST", practice_city="DENVER", practice_state="CO"),
          main_row("1000000002", entity_type="1", last_name="A" if variant == "a" else "B", first_name="X")]
    s2 = [main_row("1000000001", entity_type="2", org_name="ACME", practice_address_1="2 ELM ST", practice_city="BOISE", practice_state="ID"),
          main_row("1000000002", entity_type="1", last_name="A" if variant == "a" else "B", first_name="X")]
    return s1, s2


def build(source: Path, tmp_path: Path, name: str = "w", variant: str = "a", months: int = 2) -> Path:
    s1, s2 = _rows(variant)
    write_zip(source, "20200101", "20200112", s1)
    if months >= 2:
        write_zip(source, "20200201", "20200209", s2)
    if months >= 3:
        write_zip(source, "20200301", "20200308", s2 + [main_row("1000000003", entity_type="1", last_name="C")])
    db = tmp_path / f"{name}.duckdb"
    assert main(["load", "--source", str(source), "--warehouse", str(db), "--work", str(tmp_path / "work")]) == 0
    return db


@pytest.fixture
def releases(tmp_path: Path) -> Path:
    d = tmp_path / "releases"
    d.mkdir()
    (d / "EDITION").write_text("1\n")
    return d


# ------------------------------------------------------------------ fingerprints
def test_stored_fingerprint_equals_recomputed_after_load(source, tmp_path):
    db = build(source, tmp_path)
    con = catalog(db, read_only=True)
    stored = NPPES.stored_fingerprints(con)
    recomputed = NPPES.recompute_fingerprints(con)
    assert stored and None not in stored.values()
    assert stored == recomputed
    assert release.fingerprint_problems(stored, recomputed) == []
    # the two snapshots differ in content, so their fingerprints differ
    assert stored[(20200112, "provider")] != stored[(20200209, "provider")]
    con.close()


def test_hand_edited_version_row_blocks_export(source, tmp_path, releases):
    db = build(source, tmp_path)
    con = catalog(db)
    con.execute("UPDATE provider_version SET version_hash = 'deadbeef' WHERE npi = '1000000001' AND end_date IS NOT NULL")
    con.close()
    with pytest.raises(UserError, match="load-time fingerprints"):
        export_all(db, tmp_path / "export", releases=releases)
    assert not (tmp_path / "export" / "manifest.json").exists()


def test_missing_fingerprint_blocks_export(source, tmp_path):
    db = build(source, tmp_path)
    con = catalog(db)
    con.execute("UPDATE snapshot_stream SET content_fingerprint = NULL")
    con.close()
    with pytest.raises(UserError, match="no stored fingerprint"):
        export_all(db, tmp_path / "export")


def test_fingerprint_sql_definition():
    con = duckdb.connect()
    con.execute("CREATE TABLE t (npi VARCHAR, version_hash VARCHAR)")
    con.execute("INSERT INTO t VALUES ('2', 'b'), ('1', 'a')")
    got = con.execute(fingerprint_sql("t")).fetchone()[0]
    assert got == hashlib.md5(b"1:a\n2:b").hexdigest()
    assert con.execute(fingerprint_sql("(SELECT * FROM t WHERE false)")).fetchone()[0] == hashlib.md5(b"").hexdigest()


# -------------------------------------------------------------------- release id
def test_release_id_derived_from_sha256sums():
    sums = b"abc  parquet/x.parquet\n"
    rid = release.release_id(1, "2026-10-11", sums)
    assert rid == "e1-20261011-" + hashlib.sha256(sums).hexdigest()[:8]
    assert release.RELEASE_ID_RE.match(rid)
    assert release.release_id(1, "2026-10-11", sums + b"x") != rid
    assert release.release_id(2, "2026-10-11", sums).startswith("e2-")


def test_two_exports_with_different_bytes_get_different_ids(source, tmp_path, releases):
    db = build(source, tmp_path)
    rid1 = export_all(db, tmp_path / "e1", releases=releases)
    db2 = build(source, tmp_path, name="w2", variant="b")
    rid2 = export_all(db2, tmp_path / "e2")  # no releases dir: not checked, not recorded
    assert rid1 != rid2 and rid1[:12] == rid2[:12] == "e1-20200209-"


# --------------------------------------------------------------- release record
def test_release_record_written_and_listed(source, tmp_path, releases):
    db = build(source, tmp_path)
    out = tmp_path / "export"
    rid = export_all(db, out, releases=releases)
    rec = releases / rid
    assert sorted(p.name for p in rec.iterdir()) == sorted(release.RELEASE_FILES + ("snapshots.csv",))
    for name in release.RELEASE_FILES + ("snapshots.csv",):
        assert (rec / name).read_bytes() == (out / name).read_bytes()
    refs = release.list_releases(releases)
    assert [r.id for r in refs] == [rid] and refs[0].edition == 1 and refs[0].last_snapshot_id == 20200209
    assert release.latest_release(releases, 1).id == rid and release.latest_release(releases, 2) is None
    assert json.loads((rec / "manifest.json").read_text())["release_id"] == rid
    fps = release.read_release_fingerprints(refs[0])
    assert set(fps) == {(20200112, "provider"), (20200209, "provider")}


# ------------------------------------------------------------------- continuity
def test_continuity_skipped_without_prior_release(source, tmp_path, releases, caplog):
    db = build(source, tmp_path)
    with caplog.at_level("INFO"):
        export_all(db, tmp_path / "export", releases=releases)
    assert "continuity check skipped" in caplog.text


def test_second_export_of_same_warehouse_passes_continuity(source, tmp_path, releases, caplog):
    db = build(source, tmp_path)
    export_all(db, tmp_path / "export", releases=releases)
    with caplog.at_level("INFO"):
        export_all(db, tmp_path / "export", releases=releases)
    assert "continuity with release" in caplog.text and ": ok" in caplog.text


def test_changed_earlier_snapshot_blocks_export(source, tmp_path, releases):
    db = build(source, tmp_path)
    export_all(db, tmp_path / "export", releases=releases)
    # a rebuild whose first snapshot has different content (as a hashing or scrub change would)
    db2 = build(source, tmp_path, name="w2", variant="b")
    with pytest.raises(UserError, match="no longer reproduces release .*fingerprint .* differs.*--new-edition"):
        export_all(db2, tmp_path / "export2", releases=releases)
    assert not (tmp_path / "export2" / "manifest.json").exists()


def test_rolled_back_snapshot_blocks_export(source, tmp_path, releases):
    db = build(source, tmp_path)
    export_all(db, tmp_path / "export", releases=releases)
    assert main(["rollback", "--warehouse", str(db), "--to", "20200112"]) == 0
    with pytest.raises(UserError, match="in release .* but not in the warehouse"):
        export_all(db, tmp_path / "export", releases=releases)


def _with_provider(monkeypatch, select: str, columns):
    """NPPES publishing ``provider`` as ``select`` over its usual view (``{}`` stands for it), with ``columns(cols)``."""
    def tables():
        return tuple(dataclasses.replace(t, sql=select.format(f"({t.sql})"), columns=columns(t.columns))
                     if t.name == "provider" else t for t in NPPES.tables())
    changed = dataclasses.replace(NPPES, tables=tables)
    monkeypatch.setattr(sources, "SOURCES", tuple(changed if s is NPPES else s for s in sources.SOURCES))


def _without_practice_city(monkeypatch):
    _with_provider(monkeypatch, "SELECT * EXCLUDE (practice_city) FROM {}",
                   lambda cols: tuple(c for c in cols if c[0] != "practice_city"))


def test_dictionary_added_column_passes(source, tmp_path, releases, monkeypatch):
    db = build(source, tmp_path)
    export_all(db, tmp_path / "export", releases=releases)
    _with_provider(monkeypatch, "SELECT *, NULL::VARCHAR AS brand_new FROM {}", lambda cols: cols + (("brand_new", "", ""),))
    rid = export_all(db, tmp_path / "export", releases=releases)
    assert rid.startswith("e1-")


def test_dictionary_removed_column_blocks(source, tmp_path, releases, monkeypatch):
    db = build(source, tmp_path)
    export_all(db, tmp_path / "export", releases=releases)
    _without_practice_city(monkeypatch)
    with pytest.raises(UserError, match="provider.practice_city .* no longer exported"):
        export_all(db, tmp_path / "export", releases=releases)


def test_dictionary_retyped_column_blocks(source, tmp_path, releases, monkeypatch):
    db = build(source, tmp_path)
    export_all(db, tmp_path / "export", releases=releases)
    _with_provider(monkeypatch, "SELECT * REPLACE (NULL::INTEGER AS practice_city) FROM {}", lambda cols: cols)
    with pytest.raises(UserError, match="practice_city changed type from VARCHAR to INTEGER"):
        export_all(db, tmp_path / "export", releases=releases)


# ---------------------------------------------------------------------- editions
def test_new_edition_requires_dated_changelog_then_bumps(source, tmp_path, releases, monkeypatch):
    db = build(source, tmp_path)
    rid1 = export_all(db, tmp_path / "export", releases=releases)
    _without_practice_city(monkeypatch)
    log = tmp_path / "METHODOLOGY.md"
    log.write_text("# M\n\n## 9. Change log\n\n- 2020-01-01: too old\n")
    with pytest.raises(UserError, match="change-log entry .* dated after"):
        export_all(db, tmp_path / "export", releases=releases, new_edition=True, changelog=log)
    assert (releases / "EDITION").read_text().strip() == "1"
    log.write_text("# M\n\n## 9. Change log\n\n- 2020-01-01: too old\n- 2099-01-01 (edition 2): dropped practice_city\n")
    rid2 = export_all(db, tmp_path / "export", releases=releases, new_edition=True, changelog=log)
    assert rid2.startswith("e2-20200209-") and rid1.startswith("e1-")
    assert (releases / "EDITION").read_text().strip() == "2"
    assert release.latest_release(releases, 2).id == rid2
    assert "dropped practice_city" in (tmp_path / "export" / "README.md").read_text()
    # the next ordinary export is checked against edition 2's release, not edition 1's
    export_all(db, tmp_path / "export", releases=releases)


def test_changelog_parsing(tmp_path):
    log = tmp_path / "m.md"
    log.write_text("## 1. Other\n\n- 2001-01-01: not a change-log entry\n\n## 9. Change log\n\n- 2026-10-18 (edition 1): first release\n- 2027-01-01: later\nprose\n")
    entries = release.changelog_entries(log)
    assert [(d.isoformat(), t.split(":")[0]) for d, t in entries] == [("2026-10-18", "2026-10-18 (edition 1)"), ("2027-01-01", "2027-01-01")]
    assert release.changelog_entries(None) == [] and release.changelog_entries(tmp_path / "missing.md") == []


def test_cli_export_records_release_and_prints_id(source, tmp_path, releases, capsys):
    db = build(source, tmp_path)
    assert main(["export", "--warehouse", str(db), "--out", str(tmp_path / "export"), "--releases", str(releases)]) == 0
    rid = capsys.readouterr().out.strip().split()[-1]
    assert (releases / rid / "manifest.json").exists()


def test_example_npi_rule_prefers_organizations_with_history(source, tmp_path):
    db = build(source, tmp_path, months=3)
    con = catalog(db, read_only=True)
    # the rule's first clause (8-15 versions) finds nothing in a fixture; the fallback picks the NPI
    # with the most distinct addresses, which is the organization that moved
    assert release.example_npi(con) == "1000000001"
    assert [r[2] for r in release.example_history(con, "1000000001")] == ["1 MAIN ST", "2 ELM ST"]
    con.close()


def test_export_readme_states_rows_and_files(source, tmp_path, releases):
    db = build(source, tmp_path)
    out = tmp_path / "export"
    export_all(db, out, csv=True, releases=releases)
    readme = (out / "README.md").read_text()
    assert "-- returns 2 rows" in readme and "WHERE npi = '1000000001'" in readme
    assert "2 providers as of 2020-02-09" in readme
    assert "`csv/` mirrors" in readme
    export_all(db, out, csv=False, releases=releases)
    assert "`csv/` mirrors" not in (out / "README.md").read_text()
    shutil.rmtree(out)
