"""CMS NPPES, the monthly National Plan and Provider Enumeration System files, keyed by NPI.

``loader`` versions the zips (``snapshots`` finds them, ``schema`` and ``slots`` declare their
columns, ``scrub`` applies the privacy rules), ``export`` declares what a release publishes,
``manifest`` and ``download`` pin and fetch the zips, and ``verify`` and ``monthly`` are the
NPPES-only checks and the monthly routine.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import duckdb

from ...core import engine
from ...core.errors import UserError
from ..base import Source
from . import download, export, manifest, scrub
from .loader import Warehouse, finalize
from .schema import STREAMS
from .snapshots import discover, require_source

log = logging.getLogger(__name__)


def _discover(source_dir: Path) -> list:
    return discover(require_source(source_dir))


def _validate_streams() -> list[tuple[str, str, str, bool]]:
    return [(s.name, "npi", "snapshot_stream", s.name == "provider") for s in STREAMS]


def _status(con: duckdb.DuckDBPyConnection) -> dict:
    return {
        "snapshots": con.execute("SELECT count(*), min(as_of_date)::VARCHAR, max(as_of_date)::VARCHAR FROM snapshot").fetchone(),
        "versions": {s.name: con.execute(f"SELECT count(*), count(DISTINCT npi), sum(is_current::INT) FROM {s.name}_version").fetchone()
                     for s in STREAMS},
        "fingerprints_missing": con.execute("SELECT count(*) FROM snapshot_stream WHERE content_fingerprint IS NULL").fetchone()[0],
    }


def _check_sources(con: duckdb.DuckDBPyConnection, releases: Path | None) -> None:
    """Every snapshot can be rebuilt from a fresh clone: each has a row in releases/sources.csv (manifest.py)."""
    path = manifest.sources_path(releases)
    if path is None or not path.exists():
        return
    ids = [r[0] for r in con.execute("SELECT snapshot_id FROM snapshot ORDER BY 1").fetchall()]
    lacking = manifest.missing_sources(manifest.read_sources(path), ids)
    if lacking:
        raise UserError(f"export blocked; {len(lacking)} snapshot(s) have no row in {path}, so a fresh "
                        f"clone could not rebuild them: {', '.join(map(str, lacking[:10]))}. `healthcare-ledger "
                        f"download` records the months it fetches; for a zip obtained elsewhere run "
                        f"`healthcare-ledger download --record <zip> [--url <where it is public>]`")
    log.info("sources: every snapshot has a recorded source zip (%s)", path)


def _write_sources(con: duckdb.DuckDBPyConnection, releases: Path) -> int:
    raise UserError(f"releases/{manifest.SOURCES_FILE} is written by `healthcare-ledger download` as it fetches each month; "
                    f"for a zip obtained elsewhere run `healthcare-ledger download --record <zip> [--url <where it is public>]`",
                    exit_code=2)


def _download(a: argparse.Namespace) -> int:
    return download.run(a)


SOURCE = Source(
    name="nppes",
    database=Warehouse.database,
    title="NPPES",
    kind="versioned",
    key="npi",
    streams=STREAMS,
    required=True,
    snapshots_file=export.SNAPSHOTS_FILE,
    sources_file=manifest.SOURCES_FILE,
    default_source_dir="data/raw",
    snapshot_noun="snapshots",
    warehouse_cls=Warehouse,
    discover=_discover,
    loaded=lambda con: engine.loaded(con, Warehouse),
    finalize=finalize,
    reset=lambda con: engine.reset(con, Warehouse),
    check_problems=scrub.privacy_problems,
    stored_fingerprints=lambda con: engine.stored_fingerprints(con, Warehouse),
    recompute_fingerprints=lambda con: engine.recompute_fingerprints(con, Warehouse),
    tables=export.tables,
    snapshots_sql=export.snapshots_sql,
    validate_streams=_validate_streams,
    status=_status,
    check_sources=_check_sources,
    write_sources=_write_sources,
    download=_download,
    download_options=("since", "until", "cdx", "probe", "mirror", "record", "url"),
)
