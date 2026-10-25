"""Command line interface.

Build and check:  discover, download, load, validate, verify, rollback, status
Medicare PPEF:    ppef-download, ppef-load, ppef-sources, ppef-reset (quarterly enrollment extracts, docs/MAINTAINING.md)
Publish:          export, package, backfill-fingerprints, monthly (download, load, validate, verify,
                  continuity check and export in one command; see docs/MAINTAINING.md)

Every error the operator can fix is one line on stderr naming the problem, its cause and the
next step. Input checks run before any file is created, so a bad argument leaves nothing
behind. Tracebacks are printed only with -v.

Default paths match the Makefile and the docs: zips in data/raw, warehouse at
data/nppes.duckdb, scratch in data/work, export in export/, release records in releases/.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import traceback
from pathlib import Path

import duckdb

from . import __version__
from .errors import UserError
from .snapshots import ZIP_RE, discover

log = logging.getLogger("nppes_over_time")

DEFAULT_SOURCE = "data/raw"
DEFAULT_DB = "data/nppes.duckdb"
DEFAULT_OUT = "export"
DEFAULT_RELEASES = "releases"
DEFAULT_CHANGELOG = "docs/METHODOLOGY.md"
DEFAULT_PPEF_SOURCE = "data/ppef"


# ----------------------------------------------------------------- input checks
def require_db(path: str) -> Path:
    p = Path(path)
    if not p.is_file():
        raise UserError(f"database {p} not found; build it with `nppes-over-time load --source <zips> --db {p}`, "
                        f"or pass --db with the right path", exit_code=2)
    return p


def require_source(path: str) -> Path:
    p = Path(path)
    if not p.is_dir():
        raise UserError(f"source directory {p} does not exist; it should hold NPPES_Data_Dissemination_*.zip files "
                        f"(pass --source, or `nppes-over-time download --dest {p}` to fetch the source zips)", exit_code=2)
    if not any(ZIP_RE.search(f.name) and not f.name.endswith(".part") for f in p.iterdir()):
        raise UserError(f"no NPPES_Data_Dissemination_*.zip files in {p}; nothing to do. Run "
                        f"`nppes-over-time download --dest {p}` to fetch every recorded source zip "
                        f"(docs/MAINTAINING.md), or point --source at your archive", exit_code=2)
    return p


def _work(a: argparse.Namespace) -> Path:
    return Path(a.work) if a.work else Path(a.db).parent / "work"


# --------------------------------------------------------------------- commands
def cmd_discover(a: argparse.Namespace) -> int:
    snaps = discover(require_source(a.source))
    for s in snaps:
        present = ",".join(k for k in ("provider", "practice_location", "other_name", "endpoint") if s.has(k))
        print(f"{s.snapshot_id}  zip={s.zip_path.name}  main_bytes={s.member_bytes['provider']:>12,}  streams={present}")
    print(f"{len(snaps)} unique snapshots", file=sys.stderr)
    return 0


def cmd_download(a: argparse.Namespace) -> int:
    from .download import run
    return run(a)


def cmd_load(a: argparse.Namespace) -> int:
    from .loader import Warehouse
    snaps = discover(require_source(a.source))
    wh = Warehouse(Path(a.db), threads=a.threads, memory_limit=a.memory, work_dir=Path(a.work) if a.work else None,
                   max_absent_share=a.max_absent_share)
    try:
        done = set(wh.loaded_snapshot_ids())
        todo = [s for s in snaps if s.snapshot_id not in done]
        if a.until:
            todo = [s for s in todo if s.snapshot_id <= a.until]
        if a.limit:
            todo = todo[: a.limit]
        log.info("%d snapshots already loaded, %d to go", len(done), len(todo))
        for s in todo:
            wh.load_snapshot(s)
        wh.finalize()
    finally:
        wh.close()
    return 0


def cmd_ppef_load(a: argparse.Namespace) -> int:
    from .ppef import PpefWarehouse, discover_ppef
    src = Path(a.source)
    if not src.is_dir():
        raise UserError(f"PPEF source directory {src} does not exist; it should hold one subdirectory per quarter "
                        f"(2016-Q1, 2016-Q2, ...) with that quarter's PPEF_*.csv files (docs/MAINTAINING.md)", exit_code=2)
    snaps = discover_ppef(src)
    if not snaps:
        raise UserError(f"no quarter directories (YYYY-QN) with a PPEF_Enrollment_Extract_*.csv under {src}", exit_code=2)
    wh = PpefWarehouse(Path(a.db), threads=a.threads, memory_limit=a.memory, work_dir=_work(a),
                       max_absent_share=a.max_absent_share)
    try:
        done = set(wh.loaded_snapshot_ids())
        todo = [s for s in snaps if s.snapshot_id not in done]
        if a.until:
            todo = [s for s in todo if s.snapshot_id <= a.until]
        if a.limit:
            todo = todo[: a.limit]
        log.info("%d PPEF quarters already loaded, %d to go", len(done), len(todo))
        for s in todo:
            wh.load_snapshot(s)
        wh.finalize()
    finally:
        wh.close()
    return 0


def cmd_ppef_reset(a: argparse.Namespace) -> int:
    from .ppef import reset
    if not a.yes:
        raise UserError("ppef-reset drops every medicare_* table and the PPEF bookkeeping (the NPPES tables are not "
                        "touched); pass --yes to confirm", exit_code=2)
    con = duckdb.connect(str(require_db(a.db)))
    try:
        dropped = reset(con)
    finally:
        con.close()
    print(f"dropped {len(dropped)} table(s): {', '.join(dropped)}")
    return 0


def cmd_ppef_download(a: argparse.Namespace) -> int:
    from .ppef import download_sources
    problems = download_sources(Path(a.releases), Path(a.dest), check_only=a.check)
    for p in problems:
        print("PROBLEM:", p)
    print("OK: every recorded PPEF file is present and matches its sha256" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


def cmd_package(a: argparse.Namespace) -> int:
    from .package import package
    files = package(Path(a.out), Path(a.dest), include_csv=not a.no_csv)
    print(f"{a.dest}: {len(files) + 1} files, {sum(f['bytes'] for f in files) / 1e9:.1f} GB")
    return 0


def cmd_ppef_sources(a: argparse.Namespace) -> int:
    from .ppef import loaded, write_sources
    con = duckdb.connect(str(require_db(a.db)), read_only=True)
    try:
        if not loaded(con):
            raise UserError(f"{a.db} holds no PPEF quarters; run `nppes-over-time ppef-load` first", exit_code=2)
        empty = write_sources(con, Path(a.releases))
    finally:
        con.close()
    print(f"{Path(a.releases) / 'ppef_sources.csv'} written; {empty} row(s) still need a url")
    return 0


def cmd_export(a: argparse.Namespace) -> int:
    from .export import export_all
    require_db(a.db)
    rid = export_all(Path(a.db), Path(a.out), csv=a.csv, threads=a.threads, memory_limit=a.memory,
                     releases=Path(a.releases), new_edition=a.new_edition, changelog=Path(a.changelog),
                     monthly_files=a.monthly_files)
    print(f"release {rid}")
    return 0


def cmd_validate(a: argparse.Namespace) -> int:
    from .validate import validate
    problems = validate(require_db(a.db))
    for p in problems:
        print("PROBLEM:", p)
    print("OK" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


def cmd_verify(a: argparse.Namespace) -> int:
    from .verify import verify
    db = require_db(a.db)
    source = require_source(a.source)
    ids = None
    if a.all:
        con = duckdb.connect(str(db), read_only=True)
        ids = [r[0] for r in con.execute("SELECT snapshot_id FROM snapshot ORDER BY 1").fetchall()]
        con.close()
    elif a.snapshot:
        ids = a.snapshot
    problems = verify(db, source, ids, _work(a), threads=a.threads, memory_limit=a.memory)
    for p in problems:
        print("PROBLEM:", p)
    print("OK" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


def cmd_rollback(a: argparse.Namespace) -> int:
    from .loader import rollback
    con = duckdb.connect(str(require_db(a.db)))
    try:
        removed = rollback(con, a.to)
    finally:
        con.close()
    print(json.dumps({"kept_through": a.to, "removed": removed}, indent=2))
    return 0


def cmd_status(a: argparse.Namespace) -> int:
    from .ppef import loaded as ppef_loaded
    con = duckdb.connect(str(require_db(a.db)), read_only=True)
    try:
        print(json.dumps({
            "snapshots": con.execute("SELECT count(*), min(as_of_date)::VARCHAR, max(as_of_date)::VARCHAR FROM snapshot").fetchone(),
            "versions": {t: con.execute(f"SELECT count(*), count(DISTINCT npi), sum(is_current::INT) FROM {t}_version").fetchone()
                         for t in ("provider", "practice_location", "other_name", "endpoint")},
            "fingerprints_missing": (con.execute("SELECT count(*) FROM snapshot_stream WHERE content_fingerprint IS NULL").fetchone()[0]
                                    if "content_fingerprint" in {r[0] for r in con.execute("DESCRIBE snapshot_stream").fetchall()}
                                    else "all (run backfill-fingerprints)"),
            "ppef_quarters": (con.execute("SELECT count(*), min(as_of_date)::VARCHAR, max(as_of_date)::VARCHAR FROM ppef_snapshot").fetchone()
                              if ppef_loaded(con) else 0),
        }, indent=2, default=str))
    finally:
        con.close()
    return 0


def cmd_backfill_fingerprints(a: argparse.Namespace) -> int:
    from .release import backfill_fingerprints
    con = duckdb.connect(str(require_db(a.db)))
    try:
        if a.threads:
            con.execute(f"SET threads={int(a.threads)}")
        if a.memory:
            con.execute(f"SET memory_limit='{a.memory}'")
        n = backfill_fingerprints(con)
    finally:
        con.close()
    print(f"{n} fingerprint(s) filled")
    return 0


def cmd_monthly(a: argparse.Namespace) -> int:
    from .download import month_arg
    from .monthly import MonthlyOptions, run_monthly
    since = month_arg(a.download_since) if a.download_since else None
    return run_monthly(MonthlyOptions(
        source=Path(a.source), db=Path(a.db), out=Path(a.out), work=_work(a), releases=Path(a.releases),
        csv=not a.no_csv, monthly_files=a.monthly_files, threads=a.threads, memory=a.memory, download=not a.no_download, download_since=since,
        publish_root=a.publish_root, changelog=Path(a.changelog), max_absent_share=a.max_absent_share))


# ------------------------------------------------------------------------- main
def _resources(p: argparse.ArgumentParser) -> None:
    p.add_argument("--threads", type=int, help="DuckDB threads (default: all cores)")
    p.add_argument("--memory", help="DuckDB memory limit, e.g. 32GB (default: 80%% of RAM)")


def _db_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument("--db", default=DEFAULT_DB, help=f"DuckDB warehouse file (default {DEFAULT_DB})")


def _source_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument("--source", default=DEFAULT_SOURCE,
                   help=f"directory of NPPES_Data_Dissemination_*.zip files (default {DEFAULT_SOURCE})")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="nppes-over-time", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging and full tracebacks")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("discover", help="list snapshots found in a directory of CMS zips")
    _source_arg(d)
    d.set_defaults(fn=cmd_discover)

    dl = sub.add_parser("download", help="fetch the source zips --dest lacks (recorded in releases/sources.csv, then new CMS months); --probe, --check")
    from .download import add_arguments
    add_arguments(dl)
    dl.set_defaults(fn=cmd_download)

    l = sub.add_parser("load", help="load snapshots into the DuckDB warehouse, in as-of order")
    _source_arg(l)
    _db_arg(l)
    l.add_argument("--work", help="scratch directory for extracted CSVs, needs ~12 GB free (default: <db dir>/work)")
    l.add_argument("--limit", type=int, help="load at most N new snapshots")
    l.add_argument("--until", type=int, help="stop after snapshot id YYYYMMDD")
    _resources(l)
    l.add_argument("--max-absent-share", type=float, default=0.05,
                   help="refuse a snapshot whose main file would mark more than this share of NPIs absent (default 0.05)")
    l.set_defaults(fn=cmd_load)

    pl = sub.add_parser("ppef-load", help="load Medicare PPEF quarterly extracts (one directory per quarter) into the warehouse, in extract-date order")
    pl.add_argument("--source", default=DEFAULT_PPEF_SOURCE,
                    help=f"directory of quarter directories (2016-Q1, ...) holding the PPEF_*.csv files (default {DEFAULT_PPEF_SOURCE})")
    _db_arg(pl)
    pl.add_argument("--work", help="scratch directory for DuckDB temp files (default: <db dir>/work)")
    pl.add_argument("--limit", type=int, help="load at most N new quarters")
    pl.add_argument("--until", type=int, help="stop after snapshot id YYYYMMDD")
    _resources(pl)
    pl.add_argument("--max-absent-share", type=float, default=0.05,
                    help="refuse a quarter whose enrollment file would mark more than this share of enrollments absent (default 0.05)")
    pl.set_defaults(fn=cmd_ppef_load)

    prs = sub.add_parser("ppef-reset", help="drop the Medicare PPEF tables so the quarters can be loaded again from scratch (NPPES tables untouched)")
    _db_arg(prs)
    prs.add_argument("--yes", action="store_true", help="confirm")
    prs.set_defaults(fn=cmd_ppef_reset)

    pdl = sub.add_parser("ppef-download", help="fetch every PPEF file releases/ppef_sources.csv records into --dest/<quarter>/, sha256-checked; --check only verifies")
    pdl.add_argument("--dest", default=DEFAULT_PPEF_SOURCE)
    pdl.add_argument("--releases", default=DEFAULT_RELEASES)
    pdl.add_argument("--check", action="store_true", help="download nothing; report files that are missing or differ")
    pdl.set_defaults(fn=cmd_ppef_download)

    pk = sub.add_parser("package", help="turn a finished export into a Zenodo deposit: one zip per table, metadata, checksums")
    pk.add_argument("--out", default=DEFAULT_OUT, help="the export directory")
    pk.add_argument("--dest", default="deposit", help="empty directory for the deposit")
    pk.add_argument("--no-csv", action="store_true", help="leave the gzipped CSV copies out of the deposit")
    pk.set_defaults(fn=cmd_package)

    psrc = sub.add_parser("ppef-sources", help="write releases/ppef_sources.csv (file, size, sha256 of every loaded PPEF file; keeps recorded urls)")
    _db_arg(psrc)
    psrc.add_argument("--releases", default=DEFAULT_RELEASES, help=f"release records directory (default {DEFAULT_RELEASES})")
    psrc.set_defaults(fn=cmd_ppef_sources)

    e = sub.add_parser("export", help="write the release files (Parquet, optional CSV, README, CITATION, manifest)")
    _db_arg(e)
    e.add_argument("--out", default=DEFAULT_OUT, help=f"export directory (default {DEFAULT_OUT})")
    e.add_argument("--csv", action="store_true", help="also write gzipped CSV for Stata/R users")
    e.add_argument("--monthly-files", action="store_true",
                   help="also write one wide provider Parquet file per snapshot month (about 100 GB in all)")
    e.add_argument("--releases", default=DEFAULT_RELEASES,
                   help=f"release records directory in the repository checkout (default {DEFAULT_RELEASES})")
    e.add_argument("--new-edition", action="store_true",
                   help="start the next edition (breaking correction): skips the continuity check once, needs a dated change-log entry")
    e.add_argument("--changelog", default=DEFAULT_CHANGELOG, help=f"file holding the change log (default {DEFAULT_CHANGELOG})")
    _resources(e)
    e.set_defaults(fn=cmd_export)

    v = sub.add_parser("validate", help="check SCD invariants, snapshot reconstruction counts and privacy rules")
    _db_arg(v)
    v.set_defaults(fn=cmd_validate)

    vf = sub.add_parser("verify", help="re-stage snapshots from the raw zips and check the warehouse reproduces them field for field")
    _db_arg(vf)
    _source_arg(vf)
    grp = vf.add_mutually_exclusive_group()
    grp.add_argument("--snapshot", type=int, action="append", help="snapshot id YYYYMMDD to check (repeatable); default is the latest")
    grp.add_argument("--all", action="store_true", help="check every loaded snapshot")
    vf.add_argument("--work", help="scratch directory (default: <db dir>/work)")
    _resources(vf)
    vf.set_defaults(fn=cmd_verify)

    r = sub.add_parser("rollback", help="remove every snapshot newer than --to (YYYYMMDD) so earlier months can be inserted")
    _db_arg(r)
    r.add_argument("--to", type=int, required=True, help="last snapshot id to keep")
    r.set_defaults(fn=cmd_rollback)

    s = sub.add_parser("status", help="print warehouse summary")
    _db_arg(s)
    s.set_defaults(fn=cmd_status)

    bf = sub.add_parser("backfill-fingerprints",
                        help="one-time: compute content fingerprints for snapshots loaded before fingerprints existed")
    _db_arg(bf)
    _resources(bf)
    bf.set_defaults(fn=cmd_backfill_fingerprints)

    m = sub.add_parser("monthly", help="extend the warehouse by the new month(s) and produce the release: "
                                       "download, load, validate, verify, continuity check, export")
    _source_arg(m)
    _db_arg(m)
    m.add_argument("--out", default=DEFAULT_OUT, help=f"export directory (default {DEFAULT_OUT})")
    m.add_argument("--work", help="scratch directory (default: <db dir>/work)")
    m.add_argument("--releases", default=DEFAULT_RELEASES, help=f"release records directory (default {DEFAULT_RELEASES})")
    m.add_argument("--changelog", default=DEFAULT_CHANGELOG, help=argparse.SUPPRESS)
    m.add_argument("--no-csv", action="store_true", help="skip the gzipped CSV mirror")
    m.add_argument("--monthly-files", action="store_true",
                   help="also write one wide provider Parquet file per snapshot month (about 100 GB in all)")
    m.add_argument("--no-download", action="store_true", help="do not contact CMS; load only what --source already holds")
    m.add_argument("--download-since", help="first month to ask CMS for, YYYY-MM (default: the newest loaded snapshot's month; "
                        "earlier months are only useful for an out-of-order insert)")
    m.add_argument("--publish-root", default="<host>", help="where releases are published; printed with the release id")
    m.add_argument("--max-absent-share", type=float, default=0.05, help=argparse.SUPPRESS)
    _resources(m)
    m.set_defaults(fn=cmd_monthly)
    return p


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    try:
        return a.fn(a)
    except UserError as e:
        print(f"error: {e}", file=sys.stderr)
        if a.verbose:
            traceback.print_exc()
        return e.exit_code
    except duckdb.IOException as e:
        if "lock" in str(e).lower():
            print(f"error: another process holds the database (DuckDB file lock); wait for it to finish or stop it. "
                  f"({str(e).splitlines()[0]})", file=sys.stderr)
        else:
            print(f"error: {str(e).splitlines()[0]}", file=sys.stderr)
        if a.verbose:
            traceback.print_exc()
        return 2
    except (RuntimeError, ValueError) as e:
        # loader refusals (absent share, out-of-order month, unmapped columns, bad zip) already carry cause and fix
        print(f"error: {str(e).splitlines()[0]}", file=sys.stderr)
        if a.verbose:
            traceback.print_exc()
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as e:  # noqa: BLE001
        if a.verbose:
            raise
        print(f"error: {type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''} (rerun with -v for the traceback)",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
