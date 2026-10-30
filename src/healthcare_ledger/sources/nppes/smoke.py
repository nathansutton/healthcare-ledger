"""The smoke test: one NPPES month taken end to end, for a reviewer who does not want 200 GB of zips.

``healthcare-ledger smoke`` picks the newest month pinned in ``releases/sources.csv`` (or ``--snapshot``),
downloads that one zip (sha256-checked, resumable, about 1.2 GB), and runs every step of the real
pipeline on it in a directory of its own (``data/smoke`` by default):

    <dir>/raw/        the zip; kept between runs, so a re-run downloads nothing
    <dir>/warehouse/  nppes.duckdb, built from scratch on every run
    <dir>/work/       scratch for the extracted CSVs (about 12 GB while loading)
    <dir>/export/     the release files, as `export` writes them
    <dir>/releases/   a copy of sources.csv for the export's gates, and the release record it writes

The steps are ``load``, ``validate``, ``verify`` (field by field against the zip), ``export`` and a
read-back of the export with ``healthcare_ledger.connect``: with one snapshot every version is
current, so the provider rows in the export must equal the NPIs the load counted as new. The repository's own
``data/warehouse``, ``export/`` and ``releases/`` are never touched. Any failure stops the run with
the step that failed; exit code 0 means the whole path works on this machine.
"""

from __future__ import annotations

import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from ...core.errors import UserError
from ...core.export import export_all
from ...core.validate import validate
from .download import fetch
from .loader import Warehouse
from .manifest import SOURCES_FILE, Source, digests, read_sources, sources_path
from .snapshots import inspect_zip
from .verify import verify

log = logging.getLogger(__name__)


@dataclass
class SmokeOptions:
    dir: Path
    releases: Path
    snapshot: int | None = None
    csv: bool = False
    threads: int | None = None
    memory: str | None = None
    changelog: Path | None = None


def pick(releases: Path, snapshot: int | None) -> Source:
    """The manifest row to test: ``snapshot`` if given, else the newest month with a public url."""
    path = sources_path(releases)
    rows = read_sources(path)
    if not rows:
        raise UserError(f"no source manifest at {path}; run `smoke` from the repository checkout or pass --releases", exit_code=2)
    if snapshot is not None:
        row = next((s for s in rows if s.snapshot_id == snapshot), None)
        if row is None:
            raise UserError(f"snapshot {snapshot} has no row in {path}; pick one of its snapshot ids", exit_code=2)
        if not row.url:
            raise UserError(f"snapshot {snapshot} ({row.file}) has no public source; pick another --snapshot", exit_code=2)
        return row
    public = [s for s in rows if s.url]
    if not public:
        raise UserError(f"no row of {path} has a url to download from", exit_code=2)
    return public[-1]


def ensure_zip(raw: Path, row: Source) -> Path:
    """``raw/<file>``, downloaded unless an intact copy (same bytes and sha256) is already there."""
    raw.mkdir(parents=True, exist_ok=True)
    target = raw / row.file
    if target.is_file():
        if target.stat().st_size == row.bytes and digests(target)[0] == row.sha256:
            log.info("%s already downloaded and intact", target)
            return target
        log.warning("%s differs from the recorded zip; downloading it again", target)
        target.unlink()
    log.info("downloading %s (%.0f MB) from %s", row.file, row.bytes / 1e6, row.url)
    if fetch(raw, row.file, row.bytes, url=row.url, sha256=row.sha256) is None:
        raise UserError(f"download of {row.url} failed after retries; run `smoke` again later (the partial file resumes)")
    return target


def _step(name: str, timings: list[tuple[str, float]], fn):
    t = time.time()
    log.info("smoke: %s", name)
    try:
        result = fn()
    except UserError as e:
        raise UserError(f"smoke test failed at {name}: {e}", exit_code=e.exit_code) from e
    except Exception as e:  # noqa: BLE001  every crash names its step
        raise UserError(f"smoke test failed at {name}: {type(e).__name__}: {e}", exit_code=1) from e
    timings.append((name, time.time() - t))
    return result


def _check(name: str, problems: list[str]) -> None:
    if problems:
        more = f" (+{len(problems) - 1} more)" if len(problems) > 1 else ""
        raise UserError(f"{problems[0]}{more}", exit_code=1)


def _load(zip_path: Path, row: Source, o: SmokeOptions, warehouse: Path, work: Path) -> int:
    snap = inspect_zip(zip_path)
    if snap.snapshot_id != row.snapshot_id:
        raise UserError(f"{zip_path.name} is as of {snap.snapshot_id}, but sources.csv records it as {row.snapshot_id}")
    loader = Warehouse(warehouse, threads=o.threads, memory_limit=o.memory, work_dir=work)
    try:
        loader.load_snapshot(snap)
        loader.finalize()
    finally:
        loader.close()
    return snap.snapshot_id


def _read_back(out: Path) -> dict[str, int]:
    """Counts from the export through the reader package; refuses an export that does not match its own file."""
    from ... import connect
    con = connect(out)
    try:
        tables = [r[0] for r in con.execute("SELECT view_name FROM duckdb_views() WHERE NOT internal ORDER BY 1").fetchall()]
        counts = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables}
        current = con.execute("SELECT count(*) FROM provider WHERE end_date IS NULL").fetchone()[0]
        loaded = con.execute("SELECT max(provider_new) FROM snapshots").fetchone()[0]
    finally:
        con.close()
    if not counts.get("provider"):
        raise UserError(f"{out} has no provider rows")
    if not (counts["provider"] == current == loaded):
        raise UserError(f"{out}: provider has {counts['provider']:,} rows and {current:,} current, but the load counted "
                        f"{loaded:,} new NPIs; with one snapshot all three must be equal")
    return counts


def run_smoke(o: SmokeOptions) -> int:
    t0 = time.time()
    row = pick(o.releases, o.snapshot)
    raw, warehouse, work, out, releases = (o.dir / d for d in ("raw", "warehouse", "work", "export", "releases"))
    print(f"smoke test: {row.file} (snapshot {row.snapshot_id}, {row.origin}) in {o.dir}")
    for d in (warehouse, out, releases):   # every run builds from scratch; only the zip is kept
        shutil.rmtree(d, ignore_errors=True)
    releases.mkdir(parents=True)
    shutil.copy2(o.releases / SOURCES_FILE, releases / SOURCES_FILE)

    timings: list[tuple[str, float]] = []
    zip_path = _step("download", timings, lambda: ensure_zip(raw, row))
    sid = _step("load", timings, lambda: _load(zip_path, row, o, warehouse, work))
    _step("validate", timings, lambda: _check("validate", validate(warehouse)))
    _step("verify", timings, lambda: _check("verify", verify(warehouse, raw, [sid], work, threads=o.threads,
                                                             memory_limit=o.memory)))
    rid = _step("export", timings, lambda: export_all(warehouse, out, csv=o.csv, threads=o.threads, memory_limit=o.memory,
                                                      releases=releases, changelog=o.changelog))
    counts = _step("read back", timings, lambda: _read_back(out))

    print(f"\nsmoke test passed in {time.time() - t0:.0f}s: release {rid} in {out}")
    for name, secs in timings:
        print(f"  {name:10} {secs:7.0f}s")
    print("tables:")
    for t, n in counts.items():
        print(f"  {t:28} {n:>12,}")
    print(f"read it with: python -c \"import healthcare_ledger; print(healthcare_ledger.connect('{out}')"
          f".sql('SELECT entity_type, count(*) FROM provider GROUP BY 1'))\"")
    return 0
