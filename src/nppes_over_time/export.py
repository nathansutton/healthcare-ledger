"""Write the release files: Parquet (always) and gzipped CSV (optional).

Layout of ``--out``::

    README.md                          how to read and cite this release (generated, see release.py)
    CITATION.cff                       dataset citation carrying the release id
    manifest.json                      release id, counts, date range, every file with its SHA-256
    SHA256SUMS                         checksums of the data files only (the release id derives from it)
    data_dictionary.csv                every exported column with type and CMS header
    snapshots.csv                      one row per loaded snapshot with per-stream counts and fingerprints
    parquet/
      provider/effective_year=YYYY/    core provider versions (scalar columns)
      provider_taxonomy/...            long: one row per (version, taxonomy slot)
      provider_other_identifier/...    long: one row per (version, identifier slot)
      practice_location.parquet        secondary practice location versions
      other_name.parquet               organization other-name versions
      endpoint.parquet                 endpoint versions
      provider_monthly/                optional (--monthly-files): provider_YYYY-MM.parquet, one per snapshot
    csv/                               same tables, gzipped CSV (.csv.gz), same partitioning (no monthly files)

Big tables are hive-partitioned by the year of ``effective_date`` so a
researcher can fetch a slice without the whole thing. DuckDB, Arrow, pandas,
polars and Spark all read a partitioned directory as one table.

The monthly files are the same provider data the other way around: one file per snapshot holding
every NPI in that month's CMS file, wide, with the 330-column layout researchers know from CMS.
They are rebuilt from the version table by the as-of lookup the release fingerprints use
(release.interval_fingerprint), so a month here is exactly the month the release vouches for.
Opt-in because they are large: about 0.3 to 0.8 GB per month, 100 GB for the full history.

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

from . import __version__, ppef, release, sources
from .errors import PrivacyRefusal, UserError
from .loader import finalize
from .schema import ENDPOINT, OTHER_NAME, PRACTICE_LOCATION, PROVIDER, STREAMS, Stream
from .scrub import SUPPRESSED_COLUMNS, privacy_problems

log = logging.getLogger(__name__)

META_EXPORT = ["npi", "effective_date", "end_date", "is_current", "first_snapshot_id",
               "last_snapshot_id", "close_reason", "version_hash"]
META_DESC = {
    "npi": "National Provider Identifier (10 digits, text)",
    "effective_date": "As-of date of the first snapshot in which this version was observed (inclusive)",
    "end_date": "As-of date of the first snapshot in which this version was no longer observed (exclusive); NULL while current",
    "is_current": "True if this is the latest version and the NPI was present in the latest snapshot",
    "first_snapshot_id": "Snapshot id (YYYYMMDD of as-of date) that first showed this version",
    "last_snapshot_id": "Snapshot id that last showed this version",
    "close_reason": "'changed' if superseded by a new version, 'absent' if the NPI disappeared from the file, NULL while current",
    "version_hash": "MD5 fingerprint of all data columns of this version (set-level for multi-row streams)",
    "row_seq": "Row number within a multi-row version, ordered by row hash (no meaning beyond uniqueness)",
    "slot": "Canonical position 1..N in the repeating group (empty slots dropped, entries sorted by content, primary taxonomy first; METHODOLOGY 3), not the position in the CMS file",
    "effective_year": "Partition key: year(effective_date)",
}
SUPPRESSED_DESC = "Suppressed: never published by this project, whatever the CMS file contains (see METHODOLOGY 2a)"
SCRUB_DESC = {
    "other_id": "NULL when the value is a formatted EIN or SSN, or has nine digits under an issuer that names a tax id or SSN (METHODOLOGY 2a)",
    "other_id_issuer": "Formatted EINs or SSNs embedded in the text are replaced by [REDACTED] (METHODOLOGY 2a)",
    "license_number": "NULL when the value is a formatted EIN or SSN (METHODOLOGY 2a)",
}

SCALAR_PROVIDER = [c for c in PROVIDER.columns
                   if not any(c.name.startswith(p) for p in ("taxonomy_code_", "license_number_", "license_state_",
                                                              "primary_taxonomy_", "taxonomy_group_", "other_id_"))]
TAXONOMY_FIELDS = ["taxonomy_code", "license_number", "license_state", "primary_taxonomy", "taxonomy_group"]
OTHER_ID_FIELDS = ["other_id", "other_id_type", "other_id_state", "other_id_issuer"]


def _taxonomy_sql() -> str:
    parts = []
    for i in range(1, 16):
        cols = ", ".join(f"{f}_{i} AS {f}" for f in TAXONOMY_FIELDS)
        any_set = " OR ".join(f"{f}_{i} IS NOT NULL" for f in TAXONOMY_FIELDS)
        parts.append(f"SELECT {', '.join(META_EXPORT)}, {i} AS slot, {cols} FROM provider_version WHERE {any_set}")
    return " UNION ALL ".join(parts)


def _other_id_sql() -> str:
    parts = []
    for i in range(1, 51):
        cols = ", ".join(f"{f}_{i} AS {f}" for f in OTHER_ID_FIELDS)
        any_set = " OR ".join(f"{f}_{i} IS NOT NULL" for f in OTHER_ID_FIELDS)
        parts.append(f"SELECT {', '.join(META_EXPORT)}, {i} AS slot, {cols} FROM provider_version WHERE {any_set}")
    return " UNION ALL ".join(parts)


def _supp_sql(s: Stream) -> str:
    return f"SELECT {', '.join(META_EXPORT)}, row_seq, {', '.join(s.data_columns)} FROM {s.name}_version"


def tables() -> dict[str, tuple[str, bool]]:
    """name -> (select sql, partition by year)."""
    # suppressed columns are exported as NULL even from a warehouse built before the rule existed
    core = ", ".join(f"NULL::VARCHAR AS {c.name}" if c.name in SUPPRESSED_COLUMNS else c.name for c in SCALAR_PROVIDER)
    return {
        "provider": (f"SELECT {', '.join(META_EXPORT)}, {core} FROM provider_version", True),
        "provider_taxonomy": (_taxonomy_sql(), True),
        "provider_other_identifier": (_other_id_sql(), True),
        "practice_location": (_supp_sql(PRACTICE_LOCATION), False),
        "other_name": (_supp_sql(OTHER_NAME), False),
        "endpoint": (_supp_sql(ENDPOINT), False),
    }


def _copy(con: duckdb.DuckDBPyConnection, sql: str, dest: Path, fmt: str, partition: bool) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    opts = "FORMAT PARQUET, COMPRESSION ZSTD" if fmt == "parquet" else "FORMAT CSV, HEADER, COMPRESSION GZIP"
    if partition:
        # Remove the previous export first: partitions that no longer exist would otherwise
        # survive and be double-counted by glob readers.
        shutil.rmtree(dest, ignore_errors=True)
        sql = f"SELECT *, year(effective_date) AS effective_year FROM ({sql})"
        con.execute(f"COPY ({sql}) TO '{dest}' ({opts}, PARTITION_BY (effective_year), OVERWRITE)")
    else:
        suffix = ".parquet" if fmt == "parquet" else ".csv.gz"
        con.execute(f"COPY ({sql}) TO '{dest.with_suffix(suffix)}' ({opts})")


MONTHLY = "provider_monthly"
MONTHLY_META = ["snapshot_id", "as_of_date", "npi", "effective_date"]
MONTHLY_DESC = {
    "snapshot_id": "Snapshot id (YYYYMMDD of as-of date) this file is a copy of; one value per file",
    "as_of_date": "As-of date of the snapshot; one value per file",
    "npi": META_DESC["npi"],
    "effective_date": "effective_date of the provider version in force on as_of_date (joins to provider)",
}


def _monthly_sql(snapshot_id: int, as_of: str) -> str:
    """Every NPI in snapshot ``snapshot_id``, wide, one row each (the version in force on ``as_of``)."""
    cols = ", ".join(f"NULL::VARCHAR AS {c.name}" if c.name in SUPPRESSED_COLUMNS else c.name for c in PROVIDER.columns)
    return (f"SELECT {snapshot_id}::INTEGER AS snapshot_id, DATE '{as_of}' AS as_of_date, npi, effective_date, {cols} "
            f"FROM provider_version WHERE effective_date <= DATE '{as_of}' "
            f"AND (end_date IS NULL OR end_date > DATE '{as_of}') ORDER BY npi")


def _write_monthly(con: duckdb.DuckDBPyConnection, dest: Path) -> int:
    """One Parquet file per snapshot, named by its month; returns the total row count."""
    snaps = con.execute("SELECT snapshot_id, as_of_date FROM snapshot ORDER BY snapshot_id").fetchall()
    months = [d.strftime("%Y-%m") for _, d in snaps]
    dupes = sorted({m for m in months if months.count(m) > 1})
    if dupes:
        raise UserError(f"cannot write one file per month: more than one snapshot in {', '.join(dupes)}")
    shutil.rmtree(dest, ignore_errors=True)  # a month dropped by a rollback must not survive
    dest.mkdir(parents=True)
    total = 0
    for (sid, as_of), month in zip(snaps, months):
        log.info("exporting %s %s", MONTHLY, month)
        f = dest / f"provider_{month}.parquet"
        con.execute(f"COPY ({_monthly_sql(sid, as_of.isoformat())}) TO '{f}' (FORMAT PARQUET, COMPRESSION ZSTD)")
        total += con.execute(f"SELECT count(*) FROM read_parquet('{f}')").fetchone()[0]
    return total


def _dd_type(kind: str) -> str:
    return {"date": "DATE", "int": "INTEGER"}.get(kind, "VARCHAR")


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
    if not include_monthly and (out / "parquet" / MONTHLY).exists():
        log.warning("%s exists but this export has no monthly files; treating it as stale and leaving it "
                    "out of the manifest", out / "parquet" / MONTHLY)
        stale.append(f"parquet/{MONTHLY}/")
    entries = []
    for f in sorted(p for p in out.rglob("*") if p.is_file()):
        rel = f.relative_to(out).as_posix()
        if rel in skip or rel.startswith(tuple(stale)):
            continue
        entries.append(_entry(out, rel))
    entries.sort(key=lambda e: e["path"])
    return entries


def data_dictionary_rows() -> list[tuple[str, str, str, str, str]]:
    """(table, column, type, cms_header, description) for every exported column."""
    rows = []

    def meta(table: str, extra: list[str] = ()) -> None:
        for m in META_EXPORT + list(extra):
            t = {"effective_date": "DATE", "end_date": "DATE", "is_current": "BOOLEAN",
                 "first_snapshot_id": "INTEGER", "last_snapshot_id": "INTEGER", "row_seq": "INTEGER", "slot": "INTEGER"}.get(m, "VARCHAR")
            rows.append((table, m, t, "", META_DESC[m]))

    meta("provider")
    for c in SCALAR_PROVIDER:
        desc = SUPPRESSED_DESC if c.name in SUPPRESSED_COLUMNS else SCRUB_DESC.get(c.name, "")
        rows.append(("provider", c.name, _dd_type(c.kind), c.cms_header, desc))
    meta("provider_taxonomy", ["slot"])
    for f in TAXONOMY_FIELDS:
        hdr = {"taxonomy_code": "Healthcare Provider Taxonomy Code_N", "license_number": "Provider License Number_N",
               "license_state": "Provider License Number State Code_N",
               "primary_taxonomy": "Healthcare Provider Primary Taxonomy Switch_N",
               "taxonomy_group": "Healthcare Provider Taxonomy Group_N"}[f]
        desc = "Slot N of 15 unpivoted in canonical order (primary taxonomy first); slots where all five fields are blank are dropped"
        rows.append(("provider_taxonomy", f, "VARCHAR", hdr, desc + ("; " + SCRUB_DESC[f] if f in SCRUB_DESC else "")))
    meta("provider_other_identifier", ["slot"])
    for f in OTHER_ID_FIELDS:
        hdr = {"other_id": "Other Provider Identifier_N", "other_id_type": "Other Provider Identifier Type Code_N",
               "other_id_state": "Other Provider Identifier State_N", "other_id_issuer": "Other Provider Identifier Issuer_N"}[f]
        desc = "Slot N of 50 unpivoted in canonical order; slots where all four fields are blank are dropped"
        rows.append(("provider_other_identifier", f, "VARCHAR", hdr, desc + ("; " + SCRUB_DESC[f] if f in SCRUB_DESC else "")))
    for s in (PRACTICE_LOCATION, OTHER_NAME, ENDPOINT):
        meta(s.name, ["row_seq"])
        for c in s.columns:
            rows.append((s.name, c.name, _dd_type(c.kind), c.cms_header, ""))
    return rows


def monthly_dictionary_rows() -> list[tuple[str, str, str, str, str]]:
    """Dictionary rows of the optional monthly files (same CMS columns as the raw file, wide)."""
    rows = [(MONTHLY, m, {"snapshot_id": "INTEGER", "as_of_date": "DATE", "effective_date": "DATE"}.get(m, "VARCHAR"),
             "", MONTHLY_DESC[m]) for m in MONTHLY_META]
    for c in PROVIDER.columns:
        field, _, n = c.name.rpartition("_")
        if n.isdigit() and field in TAXONOMY_FIELDS + OTHER_ID_FIELDS:
            desc = ("Slot N in canonical order, as in provider_taxonomy / provider_other_identifier"
                    + ("; " + SCRUB_DESC[field] if field in SCRUB_DESC else ""))
        else:
            desc = SUPPRESSED_DESC if c.name in SUPPRESSED_COLUMNS else SCRUB_DESC.get(c.name, "")
        rows.append((MONTHLY, c.name, _dd_type(c.kind), c.cms_header, desc))
    return rows


def write_data_dictionary(path: Path, rows: list[tuple[str, str, str, str, str]] | None = None) -> None:
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["table", "column", "type", "cms_header", "description"])
        w.writerows(rows if rows is not None else data_dictionary_rows())


def export_all(db: Path, out: Path, *, csv: bool = False, threads: int | None = None,
               memory_limit: str | None = None, releases: Path | None = None,
               new_edition: bool = False, changelog: Path | None = None, monthly_files: bool = False) -> str:
    """Write a release into ``out`` and return its release id.

    ``releases`` is the repository's ``releases/`` directory (edition number, earlier releases).
    With it, the export is blocked unless the warehouse still reproduces the latest release of the
    current edition (release.py), and the new release's metadata is recorded there. Without it the
    export is edition 1 and nothing is recorded. ``new_edition`` starts the next edition instead
    (a breaking correction; needs a dated change-log entry in ``changelog``). ``monthly_files``
    adds one wide provider file per snapshot (Parquet only; large, so opt-in).
    """
    csv_too = csv
    if not Path(db).exists():
        raise UserError(f"database {db} not found; nothing to export (pass --db, or run `load` first)", exit_code=2)
    out.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db))
    try:
        finalize(con)  # make open versions' last_snapshot_id current even after an interrupted load
        ppef.finalize(con)
        with_ppef = ppef.loaded(con)
        if threads:
            con.execute(f"SET threads={int(threads)}")
        if memory_limit:
            con.execute(f"SET memory_limit='{memory_limit}'")
        con.execute("SET preserve_insertion_order=false")
        if con.execute("SELECT count(*) FROM snapshot").fetchone()[0] == 0:
            raise UserError(f"database {db} holds no snapshots; run `load` first", exit_code=2)

        # Hard gate: never write an export that carries a tax id or SSN.
        problems = privacy_problems(con)
        if problems:
            raise PrivacyRefusal(problems)

        # Gate 2: every snapshot's fingerprint from the version tables equals the one stored at load.
        log.info("recomputing content fingerprints from the version tables")
        release.ensure_fingerprint_column(con)
        recomputed = {**release.recompute_fingerprints(con), **ppef.recompute_fingerprints(con)}
        problems = release.fingerprint_problems({**release.stored_fingerprints(con), **ppef.stored_fingerprints(con)}, recomputed)
        if problems:
            raise UserError("export blocked; the warehouse does not match its load-time fingerprints: "
                            + "; ".join(problems[:5]) + (f" (+{len(problems) - 5} more)" if len(problems) > 5 else ""))

        # Gate 3: continuity with the latest release of the current edition.
        dictionary_rows = data_dictionary_rows() + (ppef.data_dictionary_rows() if with_ppef else [])
        # The monthly table is always part of the comparison: it is a format choice like --csv, and
        # leaving it out of one export must not block the next.
        dictionary = {(t, c, ty) for t, c, ty, _, _ in dictionary_rows + monthly_dictionary_rows()}
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

        # Gate 4: every snapshot can be rebuilt from a fresh clone (releases/sources.csv, sources.py).
        manifest = sources.sources_path(releases)
        if manifest is not None and manifest.exists():
            ids = [r[0] for r in con.execute("SELECT snapshot_id FROM snapshot ORDER BY 1").fetchall()]
            lacking = sources.missing_sources(sources.read_sources(manifest), ids)
            if lacking:
                raise UserError(f"export blocked; {len(lacking)} snapshot(s) have no row in {manifest}, so a fresh "
                                f"clone could not rebuild them: {', '.join(map(str, lacking[:10]))}. `nppes-over-time "
                                f"download` records the months it fetches; for a zip obtained elsewhere run "
                                f"`nppes-over-time download --record <zip> [--url <where it is public>]`")
            log.info("sources: every snapshot has a recorded source zip (%s)", manifest)
        problems = ppef.sources_problems(con, releases)
        if problems:
            raise UserError(f"export blocked; {len(problems)} loaded PPEF file(s) are not recorded in releases/{ppef.SOURCES_FILE}: "
                            + "; ".join(problems[:5]) + (f" (+{len(problems) - 5} more)" if len(problems) > 5 else "")
                            + ". Run `nppes-over-time ppef-sources` and fill in the urls (docs/MAINTAINING.md)")

        # A directory without manifest.json is an incomplete export; the manifest is written last.
        # Removed only after the gates pass, so a refused export leaves a previous intact one alone.
        for name in NON_DATA_FILES:
            (out / name).unlink(missing_ok=True)

        aliases = {"provider": "p", "practice_location": "pl", "other_name": "onm", "endpoint": "ep"}
        fp_cols = ", ".join(f"{aliases[s.name]}.content_fingerprint AS {s.name}_fingerprint" for s in STREAMS)
        snap_sql = f"""
            SELECT s.snapshot_id, s.as_of_date, s.zip_file, s.zip_date, s.main_csv, s.main_csv_bytes, s.main_csv_columns,
                   array_to_string(s.streams_present, ',') AS streams_present,
                   p.n_rows AS provider_rows, p.n_new AS provider_new, p.n_changed AS provider_changed,
                   p.n_unchanged AS provider_unchanged, p.n_absent AS provider_absent, p.n_bad_dates AS provider_bad_dates,
                   pl.n_rows AS practice_location_rows, onm.n_rows AS other_name_rows, ep.n_rows AS endpoint_rows,
                   {fp_cols}
            FROM snapshot s
            LEFT JOIN snapshot_stream p ON p.snapshot_id = s.snapshot_id AND p.stream = 'provider'
            LEFT JOIN snapshot_stream pl ON pl.snapshot_id = s.snapshot_id AND pl.stream = 'practice_location'
            LEFT JOIN snapshot_stream onm ON onm.snapshot_id = s.snapshot_id AND onm.stream = 'other_name'
            LEFT JOIN snapshot_stream ep ON ep.snapshot_id = s.snapshot_id AND ep.stream = 'endpoint'
            ORDER BY s.snapshot_id"""
        con.execute(f"COPY ({snap_sql}) TO '{out / 'snapshots.csv'}' (FORMAT CSV, HEADER)")
        (out / ppef.SNAPSHOTS_FILE).unlink(missing_ok=True)
        if with_ppef:
            con.execute(f"COPY ({ppef.snapshots_sql()}) TO '{out / ppef.SNAPSHOTS_FILE}' (FORMAT CSV, HEADER)")
        write_data_dictionary(out / "data_dictionary.csv",
                              dictionary_rows + (monthly_dictionary_rows() if monthly_files else []))

        all_tables = tables()
        if with_ppef:
            all_tables.update(ppef.export_tables())
        else:
            for name in ppef.export_tables():       # a stale medicare table from an earlier export must not linger
                shutil.rmtree(out / "parquet" / name, ignore_errors=True)
                shutil.rmtree(out / "csv" / name, ignore_errors=True)
        counts: dict[str, int] = {}
        for name, (sql, partition) in all_tables.items():
            log.info("exporting %s", name)
            counts[name] = con.execute(f"SELECT count(*) FROM ({sql})").fetchone()[0]
            _copy(con, sql, out / "parquet" / name, "parquet", partition)
            if csv_too:
                _copy(con, sql, out / "csv" / name, "csv", partition)
        if monthly_files:
            counts[MONTHLY] = _write_monthly(con, out / "parquet" / MONTHLY)

        first, last, n = con.execute("SELECT min(as_of_date), max(as_of_date), count(*) FROM snapshot").fetchone()
        files = _file_entries(out, csv_too, monthly_files)
        sums = "".join(f"{e['sha256']}  {e['path']}\n" for e in files).encode()
        (out / "SHA256SUMS").write_bytes(sums)
        rid = release.release_id(edition, last, sums)

        ex = release.example_npi(con)
        ex_rows = release.example_history(con, ex) if ex else []
        ex_name = None
        if ex:
            row = con.execute("SELECT coalesce(org_name, last_name || ', ' || first_name, last_name) FROM provider_version "
                              "WHERE npi = ? AND entity_type IS NOT NULL ORDER BY effective_date DESC LIMIT 1", [ex]).fetchone()
            ex_name = row[0] if row else None
        latest_count = con.execute("SELECT n_npis FROM snapshot_stream WHERE stream = 'provider' ORDER BY snapshot_id DESC LIMIT 1").fetchone()[0]
        ppef_n, ppef_first, ppef_last = (con.execute("SELECT count(*), min(as_of_date)::VARCHAR, max(as_of_date)::VARCHAR FROM ppef_snapshot").fetchone()
                                         if with_ppef else (0, "", ""))
        generated_at = release.now_iso()
        ctx = release.ReadmeContext(
            release_id=rid, edition=edition, generated_at=generated_at, snapshots=n,
            first_as_of=str(first), last_as_of=str(last),
            distinct_npis=con.execute("SELECT count(DISTINCT npi) FROM provider_version").fetchone()[0],
            row_counts=counts, formats=["parquet"] + (["csv"] if csv_too else []),
            example_npi=ex, example_rows=ex_rows, example_name=ex_name, latest_provider_count=latest_count,
            changelog=release.changelog_entries(changelog),
            ppef_snapshots=ppef_n, ppef_first_as_of=ppef_first or "", ppef_last_as_of=ppef_last or "")
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
