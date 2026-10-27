"""Turn a finished export into a deposit for a data archive (Zenodo).

An export directory holds a few hundred files: every partitioned table is one Parquet file per
``effective_year``. Zenodo takes at most 100 files and 50 GB per record, and a reader wants one
download per table, so ``package`` writes::

    <dest>/
      README.md, CITATION.cff, manifest.json, SHA256SUMS,     copied unchanged from the export
      data_dictionary.csv, snapshots.csv, medicare_snapshots.csv, pos_snapshots.csv, privacy_audit.csv
      LICENSE-DATA.txt                                         CC0 1.0 dedication of the data files
      parquet_<table>.zip                                      one table, paths as in the export
      csv_<table>.zip                                          the gzipped CSV of a table, when exported
      zenodo.json                                              deposition metadata for the Zenodo API
      PACKAGE_SHA256SUMS                                       sha256 of every file above

The zips are stored, not deflated: Parquet and .csv.gz are compressed already. Unzipping every
zip in the directory of ``SHA256SUMS`` restores the export's own paths, so ``sha256sum -c SHA256SUMS``
verifies the data the reader unpacked against the release id.

Packaging refuses an export that is incomplete (no ``manifest.json``), whose files do not match
``SHA256SUMS``, or whose ``privacy_audit.csv`` is missing or not clean. It never uploads anything;
``docs/MAINTAINING.md`` has the upload steps.
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import shutil
import zipfile
from pathlib import Path

from .. import __version__, sources
from .privacy import AUDIT_FILE
from .errors import UserError
from .release import REPO_URL

log = logging.getLogger(__name__)

META_FILES = ("README.md", "CITATION.cff", "manifest.json", "SHA256SUMS", "data_dictionary.csv", AUDIT_FILE)  # + each snapshots_file
PACKAGE_SUMS = "PACKAGE_SHA256SUMS"
ZENODO_FILE = "zenodo.json"
LICENSE_FILE = "LICENSE-DATA.txt"
MAX_FILES = 100
MAX_BYTES = 50 * 10**9
_CHUNK = 16 * 1024 * 1024

CC0 = """The data files of this release are dedicated to the public domain under the Creative Commons
CC0 1.0 Universal Public Domain Dedication: https://creativecommons.org/publicdomain/zero/1.0/

To the extent possible under law, the author has waived all copyright and related or neighboring
rights to these data files. They are derived from the CMS National Plan and Provider Enumeration
System (NPPES) Data Dissemination files, the CMS Medicare Fee-For-Service Public Provider
Enrollment files and the CMS Provider of Services files, which are public. Please cite the release (CITATION.cff) and CMS.

The code that produced them is Apache-2.0 licensed: """ + REPO_URL + "\n"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def _check_export(out: Path) -> dict:
    if not (out / "manifest.json").is_file():
        raise UserError(f"{out} has no manifest.json, so the export is incomplete; run `healthcare-ledger export` first", exit_code=2)
    manifest = json.loads((out / "manifest.json").read_text())
    audit = out / AUDIT_FILE
    if not audit.is_file():
        raise UserError(f"{out} has no {AUDIT_FILE} and cannot be packaged. Export again",
                        exit_code=2)
    with open(audit, newline="") as f:
        bad = [f"{r['table']}.{r['column']}" for r in csv.DictReader(f) if r["status"] == "FAIL"]
    if bad:
        raise UserError(f"refusing to package; {AUDIT_FILE} reports possible tax ids or SSNs in {', '.join(bad)}")
    log.info("verifying %d data files against SHA256SUMS", len(manifest["files"]))
    for e in manifest["files"]:
        f = out / e["path"]
        if not f.is_file() or f.stat().st_size != e["bytes"] or _sha256(f) != e["sha256"]:
            raise UserError(f"refusing to package; {e['path']} does not match manifest.json (changed or missing since export)")
    return manifest


def _units(manifest: dict) -> dict[str, list[str]]:
    """Zip name -> export paths it holds: one zip per table and format."""
    units: dict[str, list[str]] = {}
    for e in manifest["files"]:
        path = e["path"]
        parts = path.split("/")
        if parts[0] not in ("parquet", "csv"):
            continue
        # parquet/endpoint.parquet, csv/endpoint.csv.gz, parquet/provider/effective_year=2020/data_0.parquet
        table = parts[1].split(".")[0]
        units.setdefault(f"{parts[0]}_{table}.zip", []).append(path)
    return units


def zenodo_metadata(manifest: dict) -> dict:
    """Deposition metadata in the shape of Zenodo's REST API (``metadata`` of a deposition)."""
    rid = manifest["release_id"]
    counts = manifest.get("row_counts", {})
    medicare = (f" It also holds {manifest['ppef_snapshots']} quarterly Medicare fee-for-service enrollment extracts (PPEF) "
                f"as version tables keyed by PECOS enrollment id." if manifest.get("ppef_snapshots") else "")
    medicare += (f" It also holds {manifest['pos_snapshots']} quarterly Provider of Services (POS) files of Medicare-certified "
                 f"facilities as version tables keyed by CMS Certification Number." if manifest.get("pos_snapshots") else "")
    description = (
        f"<p>Monthly history of every provider in the CMS National Plan and Provider Enumeration System (NPPES), "
        f"{manifest['first_as_of_date']} to {manifest['last_as_of_date']}: {manifest['snapshots']} monthly snapshots as a "
        f"Slowly Changing Dimension Type 2 panel of {manifest['distinct_npis']:,} NPIs and {counts.get('provider', 0):,} "
        f"provider versions, with taxonomy and license, practice-location, other-name and endpoint histories.{medicare} "
        f"Any monthly NPPES file in the range can be rebuilt exactly with an as-of query.</p>"
        f"<p>Tax ids and SSNs that providers typed into free-text fields are removed, the CMS 'Other Provider Identifier' "
        f"slots are not published, and privacy_audit.csv records an independent scan of the files.</p>"
        f"<p>Release id {rid}. Each partitioned table is one zip; unzip next to SHA256SUMS and run "
        f"<code>sha256sum -c SHA256SUMS</code>. README.md explains the tables and how to read them in DuckDB, Python, "
        f"R and Stata. Built by healthcare-ledger ({REPO_URL}).</p>")
    return {
        "title": f"NPPES over time: monthly history of the CMS NPI registry, release {rid}",
        "upload_type": "dataset",
        "description": description,
        "creators": [{"name": "Sutton, Nathan"}],
        "license": "cc-zero",
        "access_right": "open",
        "version": rid,
        "publication_date": manifest["generated_at"][:10],
        "keywords": ["NPPES", "NPI", "CMS", "healthcare providers", "health workforce", "panel data",
                     "slowly changing dimension", "Medicare", "PECOS"],
        "related_identifiers": [
            {"identifier": REPO_URL, "relation": "isCompiledBy", "resource_type": "software"},
            {"identifier": "https://www.nber.org/research/data/npinppes-cumulative-collection-preliminary",
             "relation": "isDerivedFrom", "resource_type": "dataset"},
        ],
        "notes": f"Generated by {manifest.get('generator', 'healthcare-ledger ' + __version__)}.",
    }


def package(out: Path, dest: Path, *, include_csv: bool = True) -> list[dict]:
    """Write the deposit for export ``out`` into ``dest``; returns its files (name, bytes, sha256).
    ``include_csv=False`` leaves the gzipped CSV copies out (they are in the export's SHA256SUMS, so a
    reader checks the Parquet files with ``sha256sum -c --ignore-missing SHA256SUMS``)."""
    out, dest = Path(out), Path(dest)
    manifest = _check_export(out)
    if dest.exists() and any(dest.iterdir()):
        raise UserError(f"{dest} is not empty; remove it or pass another --dest", exit_code=2)
    dest.mkdir(parents=True, exist_ok=True)
    for name in META_FILES + tuple(s.snapshots_file for s in sources.SOURCES if s.versioned):
        if (out / name).exists():
            shutil.copyfile(out / name, dest / name)
    (dest / LICENSE_FILE).write_text(CC0)
    for name, paths in _units(manifest).items():
        if name.startswith("csv_") and not include_csv:
            continue
        log.info("packaging %s (%d file(s))", name, len(paths))
        with zipfile.ZipFile(dest / name, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as z:
            for p in sorted(paths):
                z.write(out / p, arcname=p)
    (dest / ZENODO_FILE).write_text(json.dumps({"metadata": zenodo_metadata(manifest)}, indent=2) + "\n")
    files = [{"name": f.name, "bytes": f.stat().st_size, "sha256": _sha256(f)}
             for f in sorted(dest.iterdir()) if f.name != PACKAGE_SUMS]
    (dest / PACKAGE_SUMS).write_text("".join(f"{e['sha256']}  {e['name']}\n" for e in files))
    n, size = len(files) + 1, sum(e["bytes"] for e in files)
    if n > MAX_FILES or size > MAX_BYTES:
        raise UserError(f"{dest} has {n} files and {size / 1e9:.1f} GB; a Zenodo record takes at most {MAX_FILES} files "
                        f"and {MAX_BYTES / 1e9:.0f} GB. Package with --no-csv, or split the deposit")
    log.info("package %s: %d files, %.1f GB, release %s", dest, n, size / 1e9, manifest["release_id"])
    return files
