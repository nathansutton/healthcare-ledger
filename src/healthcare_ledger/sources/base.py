"""What a source is: one dataset the warehouse holds, declared once.

Everything that spans sources (``validate``, the gates and files of ``export``, the release record,
``status`` and the generic CLI commands) loops over ``sources.SOURCES`` and asks each ``Source`` for
what it needs. Adding a source means writing ``sources/<name>/`` and listing its ``SOURCE`` in
``sources/__init__.py``; nothing else changes.

A source is one of two kinds. A **versioned** source (NPPES, PPEF, POS) loads snapshots in as-of order
into SCD2 version tables, one per file, and publishes ``tables``, views over them shaped for a reader
(``core.published``); its fingerprints, continuity and published tables make up a release. A
**reference** source (the AHRQ Compendium, HHS's Medicaid spending extract) is published as dated editions and loaded wholesale: ``load``
rebuilds its file from scratch, and the export, its gates and the release record leave it out. Both kinds
pin their input files in ``releases/<sources_file>``, and ``validate`` and ``status`` cover both.

A ``Source`` holds names and callables, not state. Each source has its own DuckDB file in the
warehouse directory (``database``), which only its loader writes. The callables take a connection on
which the source's tables resolve by bare name: the catalog of ``core.connect.open_catalog``, which
attaches every source file; the ones that read tables (``check_problems``, the fingerprints, ``status``) are only
called when ``loaded`` is true or the source is ``required``.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import duckdb

from ..core.errors import UserError
from ..core.published import Table

Con = duckdb.DuckDBPyConnection
Fingerprints = dict[tuple[int, str], str | None]
Kind = Literal["versioned", "reference"]


@dataclass(frozen=True)
class Source:
    name: str                       # CLI name (`load --source-name nppes`)
    database: str                   # its file in the warehouse directory: <warehouse>/<database>.duckdb
    title: str                      # for log lines and messages: NPPES, PPEF, POS, AHRQ
    kind: Kind                      # "versioned" (SCD2 snapshots, gated and exported) or "reference" (rebuilt wholesale)
    required: bool                  # True only for NPPES: a warehouse always holds it
    sources_file: str               # releases/<file>: the input files, pinned by sha256
    default_source_dir: str         # where `load` and `download` look by default
    loaded: Callable[[Con], bool]
    reset: Callable[[Con], list[str]]                     # drop every table of the source; returns them
    check_problems: Callable[[Con], list[str]]            # privacy and shape checks over its tables
    status: Callable[[Con], dict]
    download: Callable[[argparse.Namespace], int]         # `download --source-name <name>`
    download_options: tuple[str, ...] = ()                # `download` options beyond --dest/--releases/--check it takes

    # a versioned source: snapshots loaded in as-of order into version tables, fingerprinted, continuity-checked
    # and exported
    key: str | None = None                                # the key column of every stream
    streams: tuple[Any, ...] = ()                         # stream declarations; each has a ``name``
    snapshots_file: str | None = None                     # one row per snapshot in a release, with the stream fingerprints
    snapshot_noun: str = "snapshots"                      # "snapshots", "quarters"
    warehouse_cls: type | None = None                     # the core.engine.VersionedWarehouse subclass that loads it
    discover: Callable[[Path], list] | None = None        # snapshots under a source directory, in as-of order
    finalize: Callable[[Con], None] | None = None
    stored_fingerprints: Callable[[Con], Fingerprints] | None = None     # (snapshot_id, stream) -> fingerprint stored at load
    recompute_fingerprints: Callable[[Con], Fingerprints] | None = None  # the same, recomputed from the version tables
    tables: Callable[[], tuple[Table, ...]] | None = None    # what a reader sees (core.published), views over the versions
    snapshots_sql: Callable[[], str] | None = None        # the rows of ``snapshots_file``
    validate_streams: Callable[[], list[tuple[str, str, str, bool]]] | None = None  # (stream, key, stream table, one row per key)
    check_sources: Callable[[Con, Path | None], None] | None = None  # export gate: raises UserError when a loaded file is not pinned
    write_sources: Callable[[Con, Path], int] | None = None    # write releases/<sources_file>; returns the rows still lacking a url

    # a reference source: published as dated editions and loaded wholesale, with no versions, fingerprints or
    # export tables; ``load`` rebuilds its file from a source directory and returns its tables' row counts
    load: Callable[[Path, Path, Path], dict[str, int]] | None = None   # (source dir, warehouse dir, work dir)

    def __post_init__(self) -> None:
        need = VERSIONED_FIELDS if self.kind == "versioned" else REFERENCE_FIELDS if self.kind == "reference" else None
        if need is None:
            raise ValueError(f"source {self.name}: kind must be 'versioned' or 'reference', not {self.kind!r}")
        missing = [f for f in need if getattr(self, f) is None]
        extra = [f for f in (VERSIONED_FIELDS + REFERENCE_FIELDS) if f not in need and getattr(self, f) is not None]
        if missing or extra:
            raise ValueError(f"{self.kind} source {self.name}: "
                             + "; ".join(([f"lacks {', '.join(missing)}"] if missing else [])
                                         + ([f"sets {', '.join(extra)}, which only the other kind has"] if extra else [])))

    @property
    def versioned(self) -> bool:
        return self.kind == "versioned"

    def present(self, con: Con) -> bool:
        """Whether validate, status and (for a versioned source) the export include this source."""
        return self.required or self.loaded(con)


VERSIONED_FIELDS = ("key", "snapshots_file", "warehouse_cls", "discover", "finalize", "stored_fingerprints",
                    "recompute_fingerprints", "tables", "snapshots_sql", "validate_streams",
                    "check_sources", "write_sources")
REFERENCE_FIELDS = ("load",)


def quarter_status(snapshot_table: str) -> Callable[[Con], dict]:
    """``status`` of a quarterly source: count and date range of its quarters."""
    def status(con: Con) -> dict:
        return {"quarters": con.execute(f"SELECT count(*), min(as_of_date)::VARCHAR, max(as_of_date)::VARCHAR "
                                        f"FROM {snapshot_table}").fetchone()}
    return status


def recorded_files_gate(name: str, title: str, sources_file: str,
                        sources_problems: Callable[[Con, Path | None], list[str]]) -> Callable[[Con, Path | None], None]:
    """``check_sources`` of a source whose files ``filesources`` pins: every loaded file in releases/<sources_file>."""
    def check(con: Con, releases: Path | None) -> None:
        problems = sources_problems(con, releases)
        if problems:
            raise UserError(f"export blocked; {len(problems)} loaded {title} file(s) are not recorded in releases/{sources_file}: "
                            + "; ".join(problems[:5]) + (f" (+{len(problems) - 5} more)" if len(problems) > 5 else "")
                            + f". Run `healthcare-ledger sources --source-name {name}` and fill in the urls (docs/MAINTAINING.md)")
    return check


def recorded_download(title: str, download_sources: Callable[..., list[str]]) -> Callable[[argparse.Namespace], int]:
    """``download`` of a source whose files ``filesources`` pins: fetch what releases/<sources_file> records."""
    def run(a: argparse.Namespace) -> int:
        problems = download_sources(Path(a.releases), Path(a.dest), check_only=a.check)
        for p in problems:
            print("PROBLEM:", p)
        print(f"OK: every recorded {title} file is present and matches its sha256" if not problems else f"{len(problems)} problem(s)")
        return 1 if problems else 0
    return run
