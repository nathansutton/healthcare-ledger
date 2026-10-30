"""Command line interface.

Any source:       download, load, sources, reset with --source-name nppes (default), ppef (Medicare PPEF
                  quarterly enrollment extracts), pos (quarterly Provider of Services files), ahrq (the AHRQ
                  Compendium of U.S. Health Systems) or medicaid (HHS's Medicaid provider spending extract), the
                  last two reference sources loaded wholesale; docs/MAINTAINING.md
Build and check:  validate, status (every loaded source); discover, verify, rollback (NPPES)
Derived:          graph-build (practitioner, organization and hospital graph by PPEF quarter, docs/METHODOLOGY.md section 8)
Publish:          export, package, monthly (download, load, validate, verify,
                  continuity check and export in one command; see docs/MAINTAINING.md)
Try it:           smoke (one NPPES month end to end: download, load, validate, verify, export, read back)

Every error the operator can fix is one line on stderr naming the problem, its cause and the
next step. Input checks run before any file is created, so a bad argument leaves nothing
behind. Tracebacks are printed only with -v.

Default paths match the Makefile and the docs: zips in data/raw, the warehouse in data/warehouse
(one DuckDB file per source: nppes.duckdb, ppef.duckdb, pos.duckdb, ahrq.duckdb, medicaid.duckdb),
scratch in data/work, export in export/, release records in releases/.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import traceback
from pathlib import Path

import duckdb

from . import __version__, sources
from .core.connect import open_catalog, source_file
from .core.errors import UserError
from .sources.nppes.snapshots import discover, require_source

log = logging.getLogger("healthcare_ledger")

DEFAULT_SOURCE = "data/raw"
DEFAULT_WAREHOUSE = "data/warehouse"
DEFAULT_OUT = "export"
DEFAULT_RELEASES = "releases"
DEFAULT_CHANGELOG = "docs/METHODOLOGY.md"
DEFAULT_GRAPH = "graph"


# ----------------------------------------------------------------- input checks
def require_source_file(warehouse: str, src: sources.Source) -> Path:
    """The existing DuckDB file of ``src`` in the warehouse directory, for the commands that write to it."""
    p = source_file(Path(warehouse), src.database)
    if not p.is_file():
        raise UserError(f"warehouse {warehouse} has no {p.name}; build it with `healthcare-ledger load --source-name {src.name} "
                        f"--warehouse {warehouse}`, or pass --warehouse with the right directory", exit_code=2)
    return p


def _work(a: argparse.Namespace) -> Path:
    return Path(a.work) if a.work else Path(a.warehouse).parent / "work"


# --------------------------------------------------------------------- commands
def cmd_discover(a: argparse.Namespace) -> int:
    snaps = discover(require_source(a.source))
    for s in snaps:
        present = ",".join(k for k in ("provider", "practice_location", "other_name", "endpoint") if s.has(k))
        print(f"{s.snapshot_id}  zip={s.zip_path.name}  main_bytes={s.member_bytes['provider']:>12,}  streams={present}")
    print(f"{len(snaps)} unique snapshots", file=sys.stderr)
    return 0


def cmd_download(a: argparse.Namespace) -> int:
    src = sources.by_name(a.source_name)
    others = {o for s in sources.SOURCES for o in s.download_options} - set(src.download_options)
    given = sorted(o for o in others if getattr(a, o, None))
    if given:
        raise UserError(f"{', '.join('--' + o for o in given)}: not an option of `download --source-name {src.name}`", exit_code=2)
    a.dest = a.dest or src.default_source_dir
    return src.download(a)


def cmd_load(a: argparse.Namespace) -> int:
    src = sources.by_name(a.source_name)
    if not src.versioned:
        given = [f"--{o.replace('_', '-')}" for o in ("limit", "until") if getattr(a, o)]
        if given:
            raise UserError(f"{', '.join(given)}: {src.title} is a reference source, loaded wholesale", exit_code=2)
        counts = src.load(Path(a.source or src.default_source_dir), Path(a.warehouse), _work(a))
        print(f"{source_file(Path(a.warehouse), src.database)}: " + ", ".join(f"{t} {n:,} rows" for t, n in counts.items()))
        return 0
    snaps = src.discover(Path(a.source or src.default_source_dir))
    loader = src.warehouse_cls(Path(a.warehouse), threads=a.threads, memory_limit=a.memory, work_dir=_work(a),
                               max_absent_share=a.max_absent_share)
    try:
        done = set(loader.loaded_snapshot_ids())
        todo = [s for s in snaps if s.snapshot_id not in done]
        if a.until:
            todo = [s for s in todo if s.snapshot_id <= a.until]
        if a.limit:
            todo = todo[: a.limit]
        log.info("%d %s %s already loaded, %d to go", len(done), src.title, src.snapshot_noun, len(todo))
        for s in todo:
            loader.load_snapshot(s)
        loader.finalize()
    finally:
        loader.close()
    return 0


def cmd_reset(a: argparse.Namespace) -> int:
    src = sources.by_name(a.source_name)
    if not a.yes:
        raise UserError(f"reset drops every {src.title} table and its bookkeeping (the other sources are not touched); "
                        f"pass --yes to confirm", exit_code=2)
    con = duckdb.connect(str(require_source_file(a.warehouse, src)))
    try:
        dropped = src.reset(con)
    finally:
        con.close()
    print(f"dropped {len(dropped)} table(s): {', '.join(dropped)}")
    return 0


def cmd_sources(a: argparse.Namespace) -> int:
    src = sources.by_name(a.source_name)
    if not src.versioned:
        raise UserError(f"releases/{src.sources_file} pins the {src.title} files by hand: one row per file `load --source-name "
                        f"{src.name}` reads (docs/MAINTAINING.md)", exit_code=2)
    con = open_catalog(Path(a.warehouse), read_only=True, require=[src.name])
    try:
        if not src.loaded(con):
            raise UserError(f"warehouse {a.warehouse} holds no {src.title} {src.snapshot_noun}; run "
                            f"`healthcare-ledger load --source-name {src.name}` first", exit_code=2)
        empty = src.write_sources(con, Path(a.releases))
    finally:
        con.close()
    print(f"{Path(a.releases) / src.sources_file} written; {empty} row(s) still need a url")
    return 0


def cmd_graph_build(a: argparse.Namespace) -> int:
    from .derived.graph import build_graph
    m = build_graph(Path(a.warehouse), Path(a.out), threads=a.threads, memory_limit=a.memory, work_dir=_work(a),
                    limit=a.limit, compendium=a.compendium, medicaid=a.medicaid,
                    ownership=a.ownership, doctors_clinicians=a.doctors_clinicians,
                    medicare_utilization=a.medicare_utilization)
    print(f"{a.out}: {m['quarters']} quarters {m['first_as_of_date']} to {m['last_as_of_date']}; "
          + ", ".join(f"{k} {v['rows']:,} rows" for k, v in m["tables"].items()))
    return 0


def cmd_package(a: argparse.Namespace) -> int:
    from .core.package import package
    files = package(Path(a.out), Path(a.dest), include_csv=not a.no_csv)
    print(f"{a.dest}: {len(files) + 1} files, {sum(f['bytes'] for f in files) / 1e9:.1f} GB")
    return 0


def cmd_export(a: argparse.Namespace) -> int:
    from .core.export import export_all
    rid = export_all(Path(a.warehouse), Path(a.out), csv=a.csv, threads=a.threads, memory_limit=a.memory,
                     releases=Path(a.releases), new_edition=a.new_edition, changelog=Path(a.changelog),
                     monthly_files=a.monthly_files)
    print(f"release {rid}")
    return 0


def cmd_validate(a: argparse.Namespace) -> int:
    from .core.validate import validate
    problems = validate(Path(a.warehouse))
    for p in problems:
        print("PROBLEM:", p)
    print("OK" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


def cmd_verify(a: argparse.Namespace) -> int:
    from .sources.nppes.verify import verify
    warehouse = Path(a.warehouse)
    open_catalog(warehouse, read_only=True).close()   # a missing warehouse is reported before the source directory
    source = require_source(a.source)
    ids = a.snapshot or None
    if a.all:
        con = open_catalog(warehouse, read_only=True)
        ids = [r[0] for r in con.execute("SELECT snapshot_id FROM snapshot ORDER BY 1").fetchall()]
        con.close()
    problems = verify(warehouse, source, ids, _work(a), threads=a.threads, memory_limit=a.memory)
    for p in problems:
        print("PROBLEM:", p)
    print("OK" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


def cmd_rollback(a: argparse.Namespace) -> int:
    from .core import engine
    from .sources.nppes.loader import Warehouse
    con = duckdb.connect(str(require_source_file(a.warehouse, sources.by_name("nppes"))))
    try:
        removed = engine.rollback(con, Warehouse, a.to)
    finally:
        con.close()
    print(json.dumps({"kept_through": a.to, "removed": removed}, indent=2))
    return 0


def cmd_status(a: argparse.Namespace) -> int:
    con = open_catalog(Path(a.warehouse), read_only=True)
    try:
        print(json.dumps({src.name: src.status(con) for src in sources.SOURCES if src.present(con)}, indent=2, default=str))
    finally:
        con.close()
    return 0


def cmd_monthly(a: argparse.Namespace) -> int:
    from .sources.nppes.download import month_arg
    from .sources.nppes.monthly import MonthlyOptions, run_monthly
    since = month_arg(a.download_since) if a.download_since else None
    return run_monthly(MonthlyOptions(
        source=Path(a.source), warehouse=Path(a.warehouse), out=Path(a.out), work=_work(a), releases=Path(a.releases),
        csv=not a.no_csv, monthly_files=a.monthly_files, threads=a.threads, memory=a.memory, download=not a.no_download, download_since=since,
        publish_root=a.publish_root, changelog=Path(a.changelog), max_absent_share=a.max_absent_share))


def cmd_smoke(a: argparse.Namespace) -> int:
    from .sources.nppes.smoke import SmokeOptions, run_smoke
    return run_smoke(SmokeOptions(dir=Path(a.dir), releases=Path(a.releases), snapshot=a.snapshot, csv=a.csv,
                                  threads=a.threads, memory=a.memory, changelog=Path(a.changelog)))


# ------------------------------------------------------------------------- main
def _resources(p: argparse.ArgumentParser) -> None:
    p.add_argument("--threads", type=int, help="DuckDB threads (default: all cores)")
    p.add_argument("--memory", help="DuckDB memory limit, e.g. 32GB (default: 80%% of RAM)")


def _warehouse_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument("--warehouse", default=DEFAULT_WAREHOUSE,
                   help=f"warehouse directory, one DuckDB file per source (default {DEFAULT_WAREHOUSE})")


def _source_name_arg(p: argparse.ArgumentParser, *, required: bool = False) -> None:
    names = [s.name for s in sources.SOURCES]
    if required:
        p.add_argument("--source-name", choices=names, required=True, help="the source")
    else:
        p.add_argument("--source-name", choices=names, default="nppes", help="the source (default nppes)")


def _source_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument("--source", default=DEFAULT_SOURCE,
                   help=f"directory of NPPES_Data_Dissemination_*.zip files (default {DEFAULT_SOURCE})")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="healthcare-ledger", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging and full tracebacks")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("discover", help="list snapshots found in a directory of CMS zips")
    _source_arg(d)
    d.set_defaults(fn=cmd_discover)

    dl = sub.add_parser("download", help="fetch the source files --dest lacks, sha256-checked, from releases/<sources file> "
                                         "(NPPES: then new CMS months; --probe, --record); --check only verifies")
    _source_name_arg(dl)
    dl.add_argument("--dest", help="directory to fill (default: the source's directory, data/raw for NPPES)")
    dl.add_argument("--releases", default=DEFAULT_RELEASES, help=f"directory holding the sources files (default {DEFAULT_RELEASES})")
    dl.add_argument("--check", action="store_true", help="download nothing; sha256 the local files against the sources file")
    from .sources.nppes.download import add_arguments
    add_arguments(dl)
    dl.set_defaults(fn=cmd_download)

    l = sub.add_parser("load", help="load a source's snapshots (NPPES months, PPEF or POS quarters) into the DuckDB warehouse, "
                                    "in as-of order; a reference source (ahrq, medicaid) is rebuilt wholesale")
    _source_name_arg(l)
    l.add_argument("--source", help="directory of the source's files (default: data/raw for NPPES zips, data/ppef and data/pos "
                                    "for one directory per quarter, data/ahrq and data/medicaid with their files in raw/)")
    _warehouse_arg(l)
    l.add_argument("--work", help="scratch directory for extracted CSVs and DuckDB temp files; NPPES needs ~12 GB free "
                                  "(default: work/ next to the warehouse directory)")
    l.add_argument("--limit", type=int, help="load at most N new snapshots")
    l.add_argument("--until", type=int, help="stop after snapshot id YYYYMMDD")
    _resources(l)
    l.add_argument("--max-absent-share", type=float, default=0.05,
                   help="refuse a snapshot whose file would mark more than this share of keys absent (default 0.05)")
    l.set_defaults(fn=cmd_load)

    rs = sub.add_parser("reset", help="drop one source's tables so it can be loaded again from scratch (other sources untouched)")
    _source_name_arg(rs, required=True)
    _warehouse_arg(rs)
    rs.add_argument("--yes", action="store_true", help="confirm")
    rs.set_defaults(fn=cmd_reset)

    sr = sub.add_parser("sources", help="write releases/<sources file> of a quarterly source (file, size, sha256 of every loaded "
                                        "file; keeps recorded urls). NPPES's is written by download, AHRQ's and Medicaid's by hand")
    _source_name_arg(sr, required=True)
    _warehouse_arg(sr)
    sr.add_argument("--releases", default=DEFAULT_RELEASES, help=f"release records directory (default {DEFAULT_RELEASES})")
    sr.set_defaults(fn=cmd_sources)

    gb = sub.add_parser("graph-build", help="derive the provider graph (practitioners, organizations, hospitals; "
                                           "reassignment and hospital-link edges) from the NPPES, PPEF and POS tables")
    _warehouse_arg(gb)
    gb.add_argument("--out", default=DEFAULT_GRAPH, help=f"output directory for the Parquet tables (default {DEFAULT_GRAPH})")
    gb.add_argument("--work", help="scratch directory for DuckDB temp files (default: work/ next to the warehouse directory)")
    gb.add_argument("--limit", type=int, help="build only the first N PPEF quarters (for a quick look)")
    gb.add_argument("--compendium", action="store_true",
                    help="also derive the annual system and system_link tables from the AHRQ Compendium source, which "
                         "must be loaded (`load --source-name ahrq`)")
    gb.add_argument("--ownership", action="store_true",
                    help="also derive control, firm_member, firm and chain (firms as connected components of ownership, "
                         "ASPE's hospital and nursing home chains) from the ownership source, which must be loaded "
                         "(`load --source-name ownership`)")
    gb.add_argument("--doctors-clinicians", action="store_true",
                    help="also derive listed_reassignment and hospital_affiliation (CMS's claims-filtered group roster "
                         "and hospital affiliations) from the doctors_clinicians source, on the quarters a vintage covers")
    gb.add_argument("--medicare-utilization", action="store_true",
                    help="also derive medicare_volume (each physician's Medicare billing and office visits by setting) "
                         "from the medicare_utilization source")
    gb.add_argument("--medicaid", action="store_true",
                    help="also derive medicaid_volume and main_group (each physician's main group by Medicaid volume) "
                         "from the Medicaid source, which must be loaded (`load --source-name medicaid`)")
    _resources(gb)
    gb.set_defaults(fn=cmd_graph_build)

    pk = sub.add_parser("package", help="turn a finished export into a Zenodo deposit: one zip per table, metadata, checksums")
    pk.add_argument("--out", default=DEFAULT_OUT, help="the export directory")
    pk.add_argument("--dest", default="deposit", help="empty directory for the deposit")
    pk.add_argument("--no-csv", action="store_true", help="leave the gzipped CSV copies out of the deposit")
    pk.set_defaults(fn=cmd_package)

    e = sub.add_parser("export", help="write the release files (Parquet, optional CSV, README, CITATION, manifest)")
    _warehouse_arg(e)
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
    _warehouse_arg(v)
    v.set_defaults(fn=cmd_validate)

    vf = sub.add_parser("verify", help="re-stage snapshots from the raw zips and check the warehouse reproduces them field for field")
    _warehouse_arg(vf)
    _source_arg(vf)
    grp = vf.add_mutually_exclusive_group()
    grp.add_argument("--snapshot", type=int, action="append", help="snapshot id YYYYMMDD to check (repeatable); default is the latest")
    grp.add_argument("--all", action="store_true", help="check every loaded snapshot")
    vf.add_argument("--work", help="scratch directory (default: work/ next to the warehouse directory)")
    _resources(vf)
    vf.set_defaults(fn=cmd_verify)

    r = sub.add_parser("rollback", help="remove every snapshot newer than --to (YYYYMMDD) so earlier months can be inserted")
    _warehouse_arg(r)
    r.add_argument("--to", type=int, required=True, help="last snapshot id to keep")
    r.set_defaults(fn=cmd_rollback)

    s = sub.add_parser("status", help="print a summary of every loaded source")
    _warehouse_arg(s)
    s.set_defaults(fn=cmd_status)

    m = sub.add_parser("monthly", help="extend the warehouse by the new month(s) and produce the release: "
                                       "download, load, validate, verify, continuity check, export")
    _source_arg(m)
    _warehouse_arg(m)
    m.add_argument("--out", default=DEFAULT_OUT, help=f"export directory (default {DEFAULT_OUT})")
    m.add_argument("--work", help="scratch directory (default: work/ next to the warehouse directory)")
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

    sm = sub.add_parser("smoke", help="test the whole NPPES path on one month: download the newest recorded zip, then "
                                      "load, validate, verify, export and read back, all under --dir")
    sm.add_argument("--dir", default="data/smoke", help="directory for the zip, warehouse and export (default data/smoke)")
    sm.add_argument("--releases", default=DEFAULT_RELEASES, help=f"directory holding sources.csv, only read "
                                                                 f"(default {DEFAULT_RELEASES})")
    sm.add_argument("--snapshot", type=int, help="snapshot id YYYYMMDD from sources.csv (default: the newest)")
    sm.add_argument("--csv", action="store_true", help="also write the gzipped CSV copies")
    sm.add_argument("--changelog", default=DEFAULT_CHANGELOG, help=argparse.SUPPRESS)
    _resources(sm)
    sm.set_defaults(fn=cmd_smoke)
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
