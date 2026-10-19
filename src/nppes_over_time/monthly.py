"""The one-command monthly run: download, load, validate, verify, continuity, export.

Contract (docs/MAINTAINING.md describes it for the operator):

* **Nothing new.** No month newer than the warehouse exists locally or at CMS, and the
  warehouse's latest snapshot is the one the latest recorded release ends with: print
  ``nothing to do`` and exit 0 without exporting.
* **Owed checks.** The warehouse is ahead of the latest release (a run crashed after
  ``load`` committed, or someone ran ``load`` by hand): the checks and the export run
  even though nothing new was loaded.
* **Check failure.** If load, validate, verify, the continuity check or the export fails
  or crashes, the warehouse is rolled back to the last *published* snapshot (the latest
  release's last as-of date, or the latest snapshot at start if nothing was released yet),
  the failing check and the log path are printed, and the exit code is 1. The warehouse
  never holds an unvalidated snapshot, so a re-run starts the month from scratch.
* **Interrupted run.** Before loading, the rollback target is written to
  ``<db>.monthly-pending`` and removed once the run ends (export or rollback). If a run is
  killed in between, the next run reads the marker, so a snapshot it loaded but never
  checked is still rolled back on failure, even before the first release exists.
* **Download.** Only months from the newest loaded snapshot's month onward are asked for,
  so an older month moved out of ``--source`` is not fetched again (``--download-since``
  overrides this to backfill).
* **Privacy.** ``validate`` runs the same tax-id checks the exporter enforces, so a leak
  fails there and takes the check-failure path. Fixing it is a maintainer task.
"""

from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import duckdb

from . import release
from .errors import UserError
from .export import export_all
from .loader import Warehouse, rollback
from .snapshots import ZIP_RE, discover
from .validate import validate
from .verify import verify

log = logging.getLogger(__name__)


@dataclass
class MonthlyOptions:
    source: Path
    db: Path
    out: Path
    work: Path
    releases: Path
    csv: bool = True
    threads: int | None = None
    memory: str | None = None
    download: bool = True
    download_since: date | None = None
    publish_root: str = "<host>"
    changelog: Path | None = None
    max_absent_share: float = 0.05


def check_lock(db: Path) -> None:
    """Translate DuckDB's file-lock error into one readable line before any work starts."""
    try:
        duckdb.connect(str(db)).close()
    except duckdb.IOException as e:
        if "lock" in str(e).lower():
            raise UserError(f"another process holds {db} (DuckDB file lock); wait for it to finish or stop it, "
                            f"then run again", exit_code=2) from e
        raise


def _loaded(db: Path) -> list[int]:
    con = duckdb.connect(str(db), read_only=True)
    try:
        return [r[0] for r in con.execute("SELECT snapshot_id FROM snapshot ORDER BY 1").fetchall()]
    finally:
        con.close()


def _preflight(o: MonthlyOptions) -> list[int]:
    if not o.db.is_file():
        raise UserError(f"no warehouse at {o.db}; the first build is `nppes-over-time load --source {o.source} --db {o.db}` "
                        f"(hours, see docs/MAINTAINING.md 'First run'), `monthly` only extends one", exit_code=2)
    if not o.source.is_dir():
        raise UserError(f"source directory {o.source} does not exist; create it (downloads land there) or pass --source",
                        exit_code=2)
    if not (o.releases / release.EDITION_FILE).is_file():
        raise UserError(f"{o.releases / release.EDITION_FILE} not found; run `monthly` from the repository checkout "
                        f"(it reads and writes releases/) or pass --releases", exit_code=2)
    check_lock(o.db)
    loaded = _loaded(o.db)
    if not loaded:
        raise UserError(f"{o.db} holds no snapshots; build it with `nppes-over-time load` first", exit_code=2)
    return loaded


def _rollback_to(db: Path, before: int) -> dict[str, int]:
    con = duckdb.connect(str(db))
    try:
        return rollback(con, before)
    finally:
        con.close()


def run_monthly(o: MonthlyOptions) -> int:
    t0 = time.time()
    loaded = _preflight(o)
    o.work.mkdir(parents=True, exist_ok=True)
    log_path = o.work / "monthly.log"
    handler = logging.FileHandler(log_path)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(handler)
    try:
        return _run(o, loaded, log_path, t0)
    finally:
        logging.getLogger().removeHandler(handler)
        handler.close()


def _fail(o: MonthlyOptions, step: str, problems: list[str], before: int, log_path: Path, hint: str = "") -> None:
    removed = _rollback_to(o.db, before)
    n = removed.get("snapshots", 0)
    first = problems[0] if problems else "unknown problem"
    more = f" (+{len(problems) - 1} more in the log)" if len(problems) > 1 else ""
    back = (f"warehouse rolled back to snapshot {before} ({n} snapshot(s) removed)" if n
            else f"warehouse unchanged at snapshot {before}")
    log.error("%s failed: %s", step, "; ".join(problems))
    raise UserError(f"monthly run failed at {step}: {first}{more}. {back}. Nothing exported.{hint} Log: {log_path}")


def pending_path(db: Path) -> Path:
    return db.with_name(db.name + ".monthly-pending")


def _read_pending(db: Path) -> int | None:
    p = pending_path(db)
    text = p.read_text().strip() if p.is_file() else ""
    return int(text) if text.isdigit() else None


def _run(o: MonthlyOptions, loaded: list[int], log_path: Path, t0: float) -> int:
    edition = release.read_edition(o.releases)
    prev = release.latest_release(o.releases, edition)
    latest = loaded[-1]
    before = latest
    if prev is not None and prev.last_snapshot_id in loaded and prev.last_snapshot_id < latest:
        before = prev.last_snapshot_id
    pending = _read_pending(o.db)
    if pending is not None and pending < before:
        log.info("%s: an earlier run stopped after loading past %s", pending_path(o.db), pending)
        before = pending
    if before < latest:
        owed = [s for s in loaded if s > before]
        what = f"release {prev.id}" if prev is not None and before == prev.last_snapshot_id else f"snapshot {before}"
        print(f"warehouse is ahead of {what} by {len(owed)} unchecked snapshot(s) ({owed[0]}..{owed[-1]}); "
              f"their checks and export are still owed", file=sys.stderr)

    if o.download:
        log.info("checking CMS for months missing from %s", o.source)
        try:
            fetched = release_download(o, latest)
        except UserError as e:
            raise UserError(f"download failed, nothing loaded: {e}", exit_code=1) from e
        if fetched:
            log.info("fetched %s", ", ".join(fetched))

    if not any(ZIP_RE.search(p.name) for p in o.source.iterdir()):
        raise UserError(f"no NPPES_Data_Dissemination_*.zip files in {o.source}; the warehouse's own zips are needed "
                        f"for `verify` (and new months to load). Point --source at the archive.", exit_code=2)
    snaps = discover(o.source)
    todo = [s for s in snaps if s.snapshot_id not in loaded]
    older = [s for s in todo if s.snapshot_id < latest]
    if older:
        s = older[0]
        keep = max([x for x in loaded if x < s.snapshot_id], default=None)
        raise UserError(
            f"{s.zip_path.name} is as of {s.as_of_date}, older than the newest loaded snapshot {latest}, and cannot be "
            f"appended. To insert it: `nppes-over-time rollback --db {o.db} --to {keep}` then `load` "
            f"(docs/MAINTAINING.md 'Out-of-order month'), or move the file out of {o.source}.")

    marker = todo or before < latest
    if marker:
        pending_path(o.db).write_text(f"{before}\n")
    try:
        rc = _check_and_export(o, todo, prev, latest, before, log_path, t0)
    except UserError:  # raised by _fail, after the rollback to `before` succeeded
        if marker:
            pending_path(o.db).unlink(missing_ok=True)
        raise
    # Anything else (a crash in rollback, Ctrl-C) leaves the marker for the next run.
    if marker:
        pending_path(o.db).unlink(missing_ok=True)
    return rc


def _check_and_export(o: MonthlyOptions, todo: list, prev, latest: int, before: int, log_path: Path, t0: float) -> int:
    """Load ``todo``, then validate, verify and export; any failure or crash rolls back to ``before``
    and raises ``UserError`` (via ``_fail``)."""
    if todo:
        log.info("loading %d new snapshot(s): %s", len(todo), ", ".join(str(s.snapshot_id) for s in todo))
        wh = Warehouse(o.db, threads=o.threads, memory_limit=o.memory, work_dir=o.work, max_absent_share=o.max_absent_share)
        try:
            for s in todo:
                wh.load_snapshot(s)
            wh.finalize()
        except Exception as e:  # loader refusals (absent share, parse) and crashes alike
            wh.close()
            _fail(o, f"load of {todo[0].snapshot_id}" if len(todo) == 1 else "load", [str(e)], before, log_path)
        wh.close()
    elif prev is not None and latest == prev.last_snapshot_id == before:
        print(f"nothing to do; latest snapshot is {latest} (release {prev.id})")
        return 0
    else:
        log.info("nothing new to load; running the checks and export the warehouse still owes")

    log.info("validate")
    try:
        problems = validate(o.db)
    except Exception as e:
        _fail(o, "validate", [f"crashed: {e}"], before, log_path)
    if problems:
        hint = " Do not publish; contact the maintainer." if any(p.startswith("privacy") for p in problems) else ""
        _fail(o, "validate", problems, before, log_path, hint)

    now = _loaded(o.db)
    unchecked = [s for s in now if s > before] or now[-1:]  # first run: nothing new, check the base snapshot
    label = f"verify of {unchecked[0]}" if len(unchecked) == 1 else f"verify of {unchecked[0]}..{unchecked[-1]}"
    log.info("verify snapshot(s) %s against their zips", ", ".join(map(str, unchecked)))
    try:
        problems = verify(o.db, o.source, unchecked, o.work, threads=o.threads, memory_limit=o.memory)
    except Exception as e:
        _fail(o, label, [f"crashed: {e}"], before, log_path)
    if problems:
        _fail(o, label, problems, before, log_path)

    log.info("export to %s", o.out)
    try:
        rid = export_all(o.db, o.out, csv=o.csv, threads=o.threads, memory_limit=o.memory,
                         releases=o.releases, changelog=o.changelog)
    except UserError as e:
        hint = ""
        if "privacy" in str(e):
            hint = " Do not publish; contact the maintainer."
        elif "no longer reproduces" in str(e):
            hint = " An intended correction must go through `export --new-edition` (maintainer task)."
        _fail(o, "export", [str(e)], before, log_path, hint)
    except Exception as e:
        _fail(o, "export", [f"crashed: {e}"], before, log_path)

    target = f"{o.publish_root.rstrip('/')}/{rid}/"
    print(f"release {rid} written to {o.out} in {time.time() - t0:.0f}s")
    print(f"publish:  copy {o.out}/ to {target}")
    print(f"record:   commit {o.releases / rid}/ and {o.releases / 'sources.csv'}, and merge them before the next monthly run")
    return 0


def release_download(o: MonthlyOptions, latest: int) -> list[str]:
    """Fetch months CMS serves that ``--source`` lacks, from the newest loaded snapshot's month on.

    Older gaps are deliberately not filled here: they could not be appended anyway (see the
    out-of-order refusal), and re-fetching one the operator moved aside would loop forever.
    """
    from .download import fetch_missing
    since = o.download_since or date(latest // 10000, latest // 100 % 100, 1)
    return fetch_missing(o.source, since=since, releases=o.releases)
