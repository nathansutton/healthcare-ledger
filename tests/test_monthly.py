"""The monthly run: nothing-new, new month, owed checks, rollback on every failure path."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import duckdb
import pytest

from conftest import main_row, write_zip
from healthcare_ledger.sources.nppes import monthly as monthly_mod
from healthcare_ledger.cli import main
from healthcare_ledger.core.errors import PrivacyRefusal

ORG = dict(entity_type="2", org_name="ACME", practice_city="DENVER", practice_state="CO")
M1 = [main_row("1000000001", **ORG), main_row("1000000002", entity_type="1", last_name="A")]
M2 = [main_row("1000000001", **{**ORG, "practice_city": "BOISE"}), main_row("1000000002", entity_type="1", last_name="A")]
M3 = M2 + [main_row("1000000003", entity_type="1", last_name="C")]


@pytest.fixture
def env(source, tmp_path):
    """A warehouse holding month 1 and a releases/ directory with EDITION, no release yet."""
    write_zip(source, "20200101", "20200112", M1)
    warehouse = tmp_path / "data" / "warehouse"
    assert main(["load", "--source", str(source), "--warehouse", str(warehouse), "--work", str(tmp_path / "work")]) == 0
    releases = tmp_path / "releases"
    releases.mkdir()
    (releases / "EDITION").write_text("1\n")
    return dict(source=source, warehouse=warehouse, releases=releases, out=tmp_path / "export", work=tmp_path / "work")


def run(e, *extra) -> int:
    return main(["monthly", "--source", str(e["source"]), "--warehouse", str(e["warehouse"]), "--work", str(e["work"]),
                 "--out", str(e["out"]), "--releases", str(e["releases"]), "--no-download", "--no-csv",
                 "--publish-root", "https://data.example.org/nppes", *extra])


def loaded(e) -> list[int]:
    con = duckdb.connect(str(e["warehouse"] / "nppes.duckdb"), read_only=True)
    try:
        return [r[0] for r in con.execute("SELECT snapshot_id FROM snapshot ORDER BY 1").fetchall()]
    finally:
        con.close()


def release_ids(e) -> list[str]:
    return sorted(p.name for p in e["releases"].iterdir() if p.name.startswith("e"))


def test_first_run_exports_and_prints_publish_target(env, capsys):
    assert run(env) == 0
    out = capsys.readouterr().out
    ids = release_ids(env)
    assert len(ids) == 1 and ids[0].startswith("e1-20200112-")
    assert f"release {ids[0]} written" in out
    assert f"copy {env['out']}/ to https://data.example.org/nppes/{ids[0]}/" in out
    assert json.loads((env["out"] / "manifest.json").read_text())["release_id"] == ids[0]
    assert (env["work"] / "monthly.log").exists()


def test_nothing_new_exits_zero_without_export(env, capsys):
    assert run(env) == 0
    manifest = (env["out"] / "manifest.json").read_bytes()
    capsys.readouterr()
    assert run(env) == 0
    assert "nothing to do; latest snapshot is 20200112" in capsys.readouterr().out
    assert (env["out"] / "manifest.json").read_bytes() == manifest
    assert len(release_ids(env)) == 1


def test_new_month_loads_checks_and_exports(env, capsys):
    assert run(env) == 0
    write_zip(env["source"], "20200201", "20200209", M2)
    assert run(env) == 0
    assert loaded(env) == [20200112, 20200209]
    ids = release_ids(env)
    assert len(ids) == 2 and ids[1].startswith("e1-20200209-")
    assert "release " + ids[1] in capsys.readouterr().out
    snaps = (env["out"] / "snapshots.csv").read_text().splitlines()
    assert len(snaps) == 3  # header + two snapshots


def test_owed_checks_run_when_warehouse_is_ahead_of_release(env, capsys):
    assert run(env) == 0
    # a crash after `load` committed, or a hand-run load: nothing new to load, but checks and export are owed
    write_zip(env["source"], "20200201", "20200209", M2)
    assert main(["load", "--source", str(env["source"]), "--warehouse", str(env["warehouse"]), "--work", str(env["work"])]) == 0
    capsys.readouterr()
    assert run(env) == 0
    err = capsys.readouterr().err
    assert "ahead of release" in err and "still owed" in err
    assert len(release_ids(env)) == 2


def test_validate_failure_rolls_back_to_before(env, monkeypatch, capsys):
    assert run(env) == 0
    write_zip(env["source"], "20200201", "20200209", M2)
    monkeypatch.setattr(monthly_mod, "validate", lambda db: ["provider_version: something: got 1, expected 0"])
    assert run(env) == 1
    err = capsys.readouterr().err
    assert "failed at validate" in err and "rolled back to snapshot 20200112 (1 snapshot(s) removed)" in err
    assert "monthly.log" in err
    assert loaded(env) == [20200112]
    assert len(release_ids(env)) == 1


def test_privacy_failure_in_validate_routes_to_maintainer(env, monkeypatch, capsys):
    write_zip(env["source"], "20200201", "20200209", M2)
    monkeypatch.setattr(monthly_mod, "validate", lambda db: ["privacy: other_id_1 holds a formatted EIN or SSN: 3 row(s)"])
    assert run(env) == 1
    err = capsys.readouterr().err
    assert "Do not publish; contact the maintainer" in err
    assert loaded(env) == [20200112]


def test_export_privacy_refusal_rolls_back(env, monkeypatch, capsys):
    assert run(env) == 0
    write_zip(env["source"], "20200201", "20200209", M2)

    def refuse(*a, **k):
        raise PrivacyRefusal(["privacy: provider.other_id_1 holds a formatted EIN or SSN: 1 row(s)"])

    monkeypatch.setattr(monthly_mod, "export_all", refuse)
    assert run(env) == 1
    err = capsys.readouterr().err
    assert "failed at export" in err and "Do not publish" in err
    assert loaded(env) == [20200112]


def test_verify_failure_rolls_back(env, monkeypatch, capsys):
    write_zip(env["source"], "20200201", "20200209", M2)
    monkeypatch.setattr(monthly_mod, "verify", lambda *a, **k: ["20200209 provider: 1 source rows missing from the warehouse"])
    assert run(env) == 1
    assert "failed at verify of 20200209" in capsys.readouterr().err
    assert loaded(env) == [20200112]


def test_continuity_failure_rolls_back_and_hints_new_edition(env, capsys):
    assert run(env) == 0
    rid = release_ids(env)[0]
    # simulate a code change that altered an earlier snapshot: the recorded fingerprint no longer matches
    snaps = env["releases"] / rid / "snapshots.csv"
    lines = snaps.read_text().splitlines()
    header = lines[0].split(",")
    i = header.index("provider_fingerprint")
    cells = lines[1].split(",")
    cells[i] = "0" * 32
    snaps.write_text("\n".join([lines[0], ",".join(cells)]) + "\n")
    write_zip(env["source"], "20200201", "20200209", M2)
    capsys.readouterr()
    assert run(env) == 1
    err = capsys.readouterr().err
    assert "failed at export" in err and "no longer reproduces release" in err and "--new-edition" in err
    assert loaded(env) == [20200112]
    assert len(release_ids(env)) == 1


def test_load_refusal_rolls_back_and_names_cause(env, capsys):
    assert run(env) == 0
    # a month whose main file drops every provider trips the absent-share guard only above 1000 NPIs,
    # so use an out-of-order month instead: it is refused before anything is loaded
    write_zip(env["source"], "20191201", "20191208", M1)
    assert run(env) == 1
    err = capsys.readouterr().err
    assert "older than the newest loaded snapshot 20200112" in err and "rollback" in err
    assert loaded(env) == [20200112]


def test_rerun_after_failure_starts_the_month_from_scratch(env, monkeypatch, capsys):
    assert run(env) == 0
    write_zip(env["source"], "20200201", "20200209", M2)
    monkeypatch.setattr(monthly_mod, "validate", lambda db: ["boom"])
    assert run(env) == 1
    monkeypatch.undo()
    capsys.readouterr()
    assert run(env) == 0
    assert loaded(env) == [20200112, 20200209] and len(release_ids(env)) == 2


def test_missing_warehouse_points_to_load(env, capsys):
    env["warehouse"] = env["warehouse"].with_name("nope")
    assert run(env) == 2
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and "no NPPES warehouse file at" in err and "load" in err
    assert not env["warehouse"].exists()


def test_missing_releases_dir_is_an_input_error(env, capsys):
    env["releases"] = env["releases"].with_name("elsewhere")
    assert run(env) == 2
    assert "EDITION not found" in capsys.readouterr().err
    assert loaded(env) == [20200112]


def test_concurrent_run_lock_is_one_line(env, tmp_path, capsys):
    ready = tmp_path / "ready"
    holder = subprocess.Popen([sys.executable, "-c",
                               f"import duckdb, time, pathlib; con = duckdb.connect({str(env['warehouse'] / 'nppes.duckdb')!r}); "
                               f"pathlib.Path({str(ready)!r}).touch(); time.sleep(60)"])
    try:
        for _ in range(200):
            if ready.exists():
                break
            time.sleep(0.05)
        assert ready.exists()
        assert run(env) == 2
        err = capsys.readouterr().err
        assert err.count("\n") == 1 and "another process holds" in err
    finally:
        holder.kill()
        holder.wait()


def test_check_lock_passes_when_free(env):
    monthly_mod.check_lock(env["warehouse"])  # no exception


def test_catch_up_verifies_every_new_snapshot(env, monkeypatch, capsys):
    assert run(env) == 0
    write_zip(env["source"], "20200201", "20200209", M2)
    write_zip(env["source"], "20200301", "20200308", M3)
    seen = []
    real = monthly_mod.verify
    monkeypatch.setattr(monthly_mod, "verify", lambda db, src, sids, *a, **k: seen.extend(sids) or real(db, src, sids, *a, **k))
    assert run(env) == 0
    assert seen == [20200209, 20200308]


def test_crash_in_validate_rolls_back(env, monkeypatch, capsys):
    assert run(env) == 0
    write_zip(env["source"], "20200201", "20200209", M2)

    def crash(db):
        raise duckdb.OutOfMemoryException("out of memory")

    monkeypatch.setattr(monthly_mod, "validate", crash)
    assert run(env) == 1
    err = capsys.readouterr().err
    assert "failed at validate: crashed: out of memory" in err and "rolled back to snapshot 20200112" in err
    assert loaded(env) == [20200112]
    assert not monthly_mod.pending_path(env["warehouse"]).exists()


def test_crash_in_verify_rolls_back(env, monkeypatch, capsys):
    write_zip(env["source"], "20200201", "20200209", M2)

    def crash(*a, **k):
        raise RuntimeError("needs the `unzip` command")

    monkeypatch.setattr(monthly_mod, "verify", crash)
    assert run(env) == 1
    assert "failed at verify of 20200209: crashed" in capsys.readouterr().err
    assert loaded(env) == [20200112]


def test_interrupted_run_before_first_release_still_rolls_back(env, monkeypatch, capsys):
    # a first monthly run loaded 20200209, then was killed during verify: the marker survives
    write_zip(env["source"], "20200201", "20200209", M2)
    assert main(["load", "--source", str(env["source"]), "--warehouse", str(env["warehouse"]), "--work", str(env["work"])]) == 0
    monthly_mod.pending_path(env["warehouse"]).write_text("20200112\n")
    monkeypatch.setattr(monthly_mod, "validate", lambda db: ["boom"])
    assert run(env) == 1
    err = capsys.readouterr().err
    assert "ahead of snapshot 20200112" in err and "rolled back to snapshot 20200112 (1 snapshot(s) removed)" in err
    assert loaded(env) == [20200112]
    assert not monthly_mod.pending_path(env["warehouse"]).exists()


def test_marker_cleared_after_success(env, capsys):
    write_zip(env["source"], "20200201", "20200209", M2)
    assert run(env) == 0
    assert not monthly_mod.pending_path(env["warehouse"]).exists()


def test_download_starts_at_newest_loaded_month(env, monkeypatch, capsys):
    from healthcare_ledger.sources.nppes import download as dl
    calls = []
    monkeypatch.setattr(dl, "fetch_missing", lambda dest, since=None, **k: calls.append(since) or [])
    args = ["monthly", "--source", str(env["source"]), "--warehouse", str(env["warehouse"]), "--work", str(env["work"]),
            "--out", str(env["out"]), "--releases", str(env["releases"]), "--no-csv"]
    assert main(args) == 0
    assert main(args + ["--download-since", "2019-06"]) == 0
    from datetime import date
    assert calls == [date(2020, 1, 1), date(2019, 6, 1)]


def test_download_since_must_be_a_month(env, capsys):
    assert run(env, "--download-since", "2025") == 2
    assert "use YYYY-MM" in capsys.readouterr().err
