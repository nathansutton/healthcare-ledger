"""Write the release files: Parquet (always) and gzipped CSV (optional).

Layout of ``--out``::

    README.md                          how to read and cite this release (generated, see core/release.py)
    CITATION.cff                       dataset citation carrying the release id
    manifest.json                      release id, counts, date range, every file with its SHA-256
    SHA256SUMS                         checksums of the data files only (the release id derives from it)
    data_dictionary.csv                every exported column with type and CMS header
    snapshots.csv                      one row per loaded snapshot with per-stream counts and fingerprints
    privacy_audit.csv                  independent scan of the written Parquet files for tax ids and SSNs (core.privacy)
    parquet/
      <table>/effective_year=YYYY/     one directory per published table (provider, provider_taxonomy, ...,
                                       medicare_enrollment, ..., facility)
      provider_monthly/                optional (--monthly-files): provider_YYYY-MM.parquet, one per snapshot
    csv/                               same tables, gzipped CSV (.csv.gz), same partitioning (no monthly files)

The tables are the views each source publishes (``Source.tables``, ``core.published``), every one
hive-partitioned by the year of ``effective_date`` so a researcher can fetch a slice without the whole
thing and reads every table with the same pattern. DuckDB, Arrow, pandas, polars and Spark all read a
partitioned directory as one table. ``*_snapshots.csv`` is declared with each source and the optional
monthly files in ``sources/nppes/export.py``.

Write order matters for the release id: data files, then ``SHA256SUMS`` (which fixes the
id), then README and CITATION (which quote the id), then ``manifest.json`` last. A directory
without a manifest is an incomplete export.
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from .. import sources
from ..sources.nppes import export as nppes_export
from . import privacy, published, release
from .connect import configure, open_catalog
from .errors import PrivacyRefusal, UserError

log = logging.getLogger(__name__)


def _copy(con: duckdb.DuckDBPyConnection, table: str, dest: Path, fmt: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    opts = "FORMAT PARQUET, COMPRESSION ZSTD" if fmt == "parquet" else "FORMAT CSV, HEADER, COMPRESSION GZIP"
    # Remove the previous export first: partitions that no longer exist would otherwise
    # survive and be double-counted by glob readers.
    shutil.rmtree(dest, ignore_errors=True)
    if con.execute(f"SELECT count(*) FROM (SELECT 1 FROM {table} LIMIT 1)").fetchone()[0]:
        con.execute(f"COPY (SELECT *, year(effective_date) AS effective_year FROM {table}) TO '{dest}' "
                    f"({opts}, PARTITION_BY (effective_year), OVERWRITE)")
    else:   # no rows, so no partition: one empty file keeps the table and its columns in the release
        dest.mkdir()
        con.execute(f"COPY {table} TO '{dest / ('empty' + ('.parquet' if fmt == 'parquet' else '.csv.gz'))}' ({opts})")


_HASH_CHUNK = 16 * 1024 * 1024


# Files that carry or depend on the release id; never part of SHA256SUMS.
NON_DATA_FILES = ("manifest.json", "SHA256SUMS", "README.md", "CITATION.cff")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(_HASH_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def _entry(out: Path, rel: str) -> dict:
    f = out / rel
    return {"path": rel, "bytes": f.stat().st_size, "sha256": _sha256(f)}


def _file_entries(out: Path, include_csv: bool, include_monthly: bool = False) -> list[dict]:
    """Path, size and SHA-256 of every data file under ``out`` (everything but NON_DATA_FILES)."""
    skip = set(NON_DATA_FILES)
    stale = []
    if not include_csv and (out / "csv").exists():
        log.warning("%s exists but this export is parquet-only; treating it as stale and leaving it "
                    "out of the manifest", out / "csv")
        stale.append("csv/")
    if not include_monthly and (out / "parquet" / nppes_export.MONTHLY).exists():
        log.warning("%s exists but this export has no monthly files; treating it as stale and leaving it "
                    "out of the manifest", out / "parquet" / nppes_export.MONTHLY)
        stale.append(f"parquet/{nppes_export.MONTHLY}/")
    entries = []
    for f in sorted(p for p in out.rglob("*") if p.is_file()):
        rel = f.relative_to(out).as_posix()
        if rel in skip or rel.startswith(tuple(stale)):
            continue
        entries.append(_entry(out, rel))
    entries.sort(key=lambda e: e["path"])
    return entries


def write_data_dictionary(path: Path, rows: list[tuple[str, str, str, str, str]]) -> None:
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["table", "column", "type", "cms_header", "description"])
        w.writerows(rows)


def export_all(warehouse: Path, out: Path, *, csv: bool = False, threads: int | None = None,
               memory_limit: str | None = None, releases: Path | None = None,
               new_edition: bool = False, changelog: Path | None = None, monthly_files: bool = False) -> str:
    """Write a release of the warehouse directory ``warehouse`` into ``out`` and return its release id.

    ``releases`` is the repository's ``releases/`` directory (edition number, earlier releases).
    With it, the export is blocked unless the warehouse still reproduces the latest release of the
    current edition (core/release.py), and the new release's metadata is recorded there. Without it the
    export is edition 1 and nothing is recorded. ``new_edition`` starts the next edition instead
    (a breaking correction; needs a dated change-log entry in ``changelog``). ``monthly_files``
    adds one wide provider file per snapshot (Parquet only; large, so opt-in).
    """
    csv_too = csv
    con = open_catalog(warehouse, read_only=False)   # finalize writes to each source's file
    out.mkdir(parents=True, exist_ok=True)
    try:
        present = [src for src in sources.SOURCES if src.versioned and src.present(con)]   # reference sources are not released
        for src in present:
            src.finalize(con)  # make open versions' last_snapshot_id current even after an interrupted load
        names = {src.name for src in present}
        configure(con, threads=threads, memory_limit=memory_limit)
        if con.execute("SELECT count(*) FROM snapshot").fetchone()[0] == 0:
            raise UserError(f"warehouse {warehouse} holds no NPPES snapshots; run `load` first", exit_code=2)

        # Hard gate: never write an export that carries a tax id or SSN.
        problems = [p for src in present for p in src.check_problems(con)]
        if problems:
            raise PrivacyRefusal(problems)

        # Gate 2: every snapshot's fingerprint from the version tables equals the one stored at load.
        log.info("recomputing content fingerprints from the version tables")
        recomputed = {k: v for src in present for k, v in src.recompute_fingerprints(con).items()}
        problems = release.fingerprint_problems(
            {k: v for src in present for k, v in src.stored_fingerprints(con).items()}, recomputed)
        if problems:
            raise UserError("export blocked; the warehouse does not match its load-time fingerprints: "
                            + "; ".join(problems[:5]) + (f" (+{len(problems) - 5} more)" if len(problems) > 5 else ""))

        # Gate 3: continuity with the latest release of the current edition.
        tables = [t for src in present for t in src.tables()]
        dictionary_rows = [r for t in tables for r in published.dictionary_rows(con, t)]
        # The monthly table is always part of the comparison: it is a format choice like --csv, and
        # leaving it out of one export must not block the next.
        dictionary = {(t, c, ty) for t, c, ty, _, _ in dictionary_rows + nppes_export.monthly_dictionary_rows()}
        edition = release.read_edition(releases)
        if new_edition:
            release.check_new_edition_allowed(release.latest_release(releases), changelog)
            edition += 1
            log.info("starting edition %d; continuity check skipped for this export", edition)
        else:
            prev = release.latest_release(releases, edition)
            if prev is None:
                log.info("no release of edition %d recorded%s; continuity check skipped", edition,
                         f" under {releases}" if releases is not None else "")
            else:
                problems = release.continuity_problems(recomputed, dictionary, prev)
                if problems:
                    raise UserError(f"export blocked; this warehouse no longer reproduces release {prev.id}: "
                                    + "; ".join(problems[:5]) + (f" (+{len(problems) - 5} more)" if len(problems) > 5 else "")
                                    + ". An intended correction must go through `export --new-edition`.")
                log.info("continuity with release %s: ok (%d fingerprints)", prev.id, len(recomputed))

        # Gate 4: every loaded file can be rebuilt from a fresh clone (each source's releases/<sources_file>).
        for src in present:
            src.check_sources(con, releases)

        # A directory without manifest.json is an incomplete export; the manifest is written last.
        # Removed only after the gates pass, so a refused export leaves a previous intact one alone.
        for name in NON_DATA_FILES:
            (out / name).unlink(missing_ok=True)

        for src in (s for s in sources.SOURCES if s.versioned):
            (out / src.snapshots_file).unlink(missing_ok=True)
            if src.name in names:
                con.execute(f"COPY ({src.snapshots_sql()}) TO '{out / src.snapshots_file}' (FORMAT CSV, HEADER)")
        write_data_dictionary(out / "data_dictionary.csv",
                              dictionary_rows + (nppes_export.monthly_dictionary_rows() if monthly_files else []))

        for src in (s for s in sources.SOURCES if s.versioned and s.name not in names):
            for t in src.tables():    # a stale table from an earlier export must not linger
                shutil.rmtree(out / "parquet" / t.name, ignore_errors=True)
                shutil.rmtree(out / "csv" / t.name, ignore_errors=True)
        counts: dict[str, int] = {}
        for t in tables:
            log.info("exporting %s", t.name)
            counts[t.name] = con.execute(f"SELECT count(*) FROM {t.name}").fetchone()[0]
            _copy(con, t.name, out / "parquet" / t.name, "parquet")
            if csv_too:
                _copy(con, t.name, out / "csv" / t.name, "csv")
        if monthly_files:
            counts[nppes_export.MONTHLY] = nppes_export._write_monthly(con, out / "parquet" / nppes_export.MONTHLY)

        # Gate 5: an independent scan of the files just written (core.privacy.audit). On failure the data files go, so
        # nothing half-checked is left behind to upload.
        audit_rows = privacy.audit(out)
        privacy.write_audit(out / privacy.AUDIT_FILE, audit_rows)
        problems = privacy.failures(audit_rows)
        if problems:
            for d in ("parquet", "csv"):
                shutil.rmtree(out / d, ignore_errors=True)
            raise PrivacyRefusal(problems)
        log.info("privacy audit of the exported files: ok (%d text columns, %s)", len(audit_rows), privacy.AUDIT_FILE)

        first, last, n = con.execute("SELECT min(as_of_date), max(as_of_date), count(*) FROM snapshot").fetchone()
        files = _file_entries(out, csv_too, monthly_files)
        sums = "".join(f"{e['sha256']}  {e['path']}\n" for e in files).encode()
        (out / "SHA256SUMS").write_bytes(sums)
        rid = release.release_id(edition, last, sums)

        ex = release.example_npi(con)
        ex_rows = release.example_history(con, ex) if ex else []
        ex_name = None
        if ex:
            row = con.execute("SELECT coalesce(org_name, last_name || ', ' || first_name, last_name) FROM provider "
                              "WHERE npi = ? AND entity_type IS NOT NULL ORDER BY effective_date DESC LIMIT 1", [ex]).fetchone()
            ex_name = row[0] if row else None
        latest_count = con.execute("SELECT n_npis FROM snapshot_stream WHERE stream = 'provider' ORDER BY snapshot_id DESC LIMIT 1").fetchone()[0]
        ppef_n, ppef_first, ppef_last = (con.execute("SELECT count(*), min(as_of_date)::VARCHAR, max(as_of_date)::VARCHAR FROM ppef_snapshot").fetchone()
                                         if "ppef" in names else (0, "", ""))
        pos_n, pos_first, pos_last = (con.execute("SELECT count(*), min(as_of_date)::VARCHAR, max(as_of_date)::VARCHAR FROM pos_snapshot").fetchone()
                                      if "pos" in names else (0, "", ""))
        generated_at = release.now_iso()
        ctx = release.ReadmeContext(
            tables=[(t.name, t.description) for t in tables],
            release_id=rid, edition=edition, generated_at=generated_at, snapshots=n,
            first_as_of=str(first), last_as_of=str(last),
            distinct_npis=con.execute("SELECT count(DISTINCT npi) FROM provider_version").fetchone()[0],
            row_counts=counts, formats=["parquet"] + (["csv"] if csv_too else []),
            example_npi=ex, example_rows=ex_rows, example_name=ex_name, latest_provider_count=latest_count,
            changelog=release.changelog_entries(changelog),
            ppef_snapshots=ppef_n, ppef_first_as_of=ppef_first or "", ppef_last_as_of=ppef_last or "",
            pos_snapshots=pos_n, pos_first_as_of=pos_first or "", pos_last_as_of=pos_last or "")
        (out / "README.md").write_text(release.render_readme(ctx))
        (out / "CITATION.cff").write_text(release.render_citation(ctx, datetime.now(timezone.utc).date()))

        manifest = {
            "release_id": rid,
            "edition": edition,
            "generator": ctx.generator,
            "generated_at": generated_at,
            "snapshots": n,
            "first_as_of_date": str(first),
            "last_as_of_date": str(last),
            "distinct_npis": ctx.distinct_npis,
            "row_counts": counts,
            "ppef_snapshots": ppef_n,
            "pos_snapshots": pos_n,
            "formats": ctx.formats,
            "example_npi": ex,
            "sha256sums_sha256": hashlib.sha256(sums).hexdigest(),
            "files": sorted(files + [_entry(out, "README.md"), _entry(out, "CITATION.cff")], key=lambda e: e["path"]),
        }
        (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        log.info("manifest: %s", json.dumps({k: v for k, v in manifest.items() if k != "files"}))
    finally:
        con.close()
    if releases is not None:
        release.write_release_record(releases, rid, out)
        if new_edition or not (releases / release.EDITION_FILE).exists():
            release.write_edition(releases, edition)
        log.info("release %s recorded under %s; commit it before the next monthly run", rid, releases / rid)
    return rid
