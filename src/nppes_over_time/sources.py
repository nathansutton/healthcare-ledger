"""The source manifest: which zip every published snapshot was built from, and where to get it.

``releases/sources.csv`` is committed with the release records. One row per loaded snapshot:

    snapshot_id   as-of date of the snapshot, YYYYMMDD
    file          the name to save the zip under (the upstream name when there is one)
    bytes, sha256 the exact zip the release was built from
    origin        cms      download.cms.gov still serves these exact bytes
                  wayback  only the Internet Archive's Wayback Machine holds them
                  nber     NBER's NPI/NPPES cumulative collection holds them, under
                           https://data.nber.org/npi/<year>/, npi/backfiles/ or nppes/zip-orig/
                           (CMS no longer serves them)
                  archive  no public source is known; the zip has to come from someone who has it
    url           where ``download`` fetches it (empty for ``archive``)

``nppes-over-time download`` reads it to rebuild a source directory byte for byte, appends a
row for every new month it fetches, and ``download --check`` hashes a source directory against
it. ``export`` refuses to publish a snapshot that has no row, so every release stays
reproducible from a fresh clone.
"""

from __future__ import annotations

import csv
import hashlib
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from .errors import UserError
from .snapshots import zip_date_from_name

SOURCES_FILE = "sources.csv"
ORIGINS = ("cms", "wayback", "nber", "archive")


@dataclass(frozen=True)
class Source:
    snapshot_id: int
    file: str
    bytes: int
    sha256: str
    origin: str
    url: str = ""

    @property
    def month(self) -> tuple[int, int]:
        d = zip_date_from_name(self.file)
        return (d.year, d.month)


COLUMNS = [f.name for f in fields(Source)]


def sources_path(releases: Path | None) -> Path | None:
    return None if releases is None else Path(releases) / SOURCES_FILE


def read_sources(path: Path | None) -> list[Source]:
    """Rows of ``path`` in snapshot order; [] if there is no manifest."""
    if path is None or not path.exists():
        return []
    out = []
    with open(path, newline="") as f:
        r = csv.DictReader(f)
        if r.fieldnames != COLUMNS:
            raise UserError(f"{path} must have the columns {','.join(COLUMNS)}, found {','.join(r.fieldnames or [])}",
                            exit_code=2)
        for i, row in enumerate(r, start=2):
            try:
                s = Source(int(row["snapshot_id"]), row["file"], int(row["bytes"]), row["sha256"].lower(),
                           row["origin"], row["url"])
            except ValueError as e:
                raise UserError(f"{path} line {i}: {e}", exit_code=2) from None
            if s.origin not in ORIGINS or zip_date_from_name(s.file) is None or len(s.sha256) != 64:
                raise UserError(f"{path} line {i}: bad row {row}", exit_code=2)
            if (s.origin == "archive") != (s.url == ""):
                raise UserError(f"{path} line {i}: origin 'archive' means no url, and every other origin needs one",
                                exit_code=2)
            out.append(s)
    out.sort(key=lambda s: s.snapshot_id)
    return out


def write_sources(path: Path, rows: list[Source]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, lineterminator="\n")
        w.writeheader()
        for s in sorted(rows, key=lambda s: s.snapshot_id):
            w.writerow(asdict(s))
    tmp.replace(path)


def upsert_source(path: Path, new: Source) -> None:
    """Add ``new`` to the manifest, replacing a row for the same snapshot."""
    rows = [s for s in read_sources(path) if s.snapshot_id != new.snapshot_id]
    write_sources(path, rows + [new])


def origin_of(url: str) -> str:
    if not url:
        return "archive"
    if "web.archive.org/" in url:
        return "wayback"
    if "data.nber.org/" in url:
        return "nber"
    return "cms"


def digests(path: Path) -> tuple[str, str]:
    """(sha256, md5) of a file, in one pass."""
    sha, md5 = hashlib.sha256(), hashlib.md5()
    with open(path, "rb") as f:
        while chunk := f.read(16 << 20):
            sha.update(chunk)
            md5.update(chunk)
    return sha.hexdigest(), md5.hexdigest()


def missing_sources(rows: list[Source], snapshot_ids: list[int]) -> list[int]:
    have = {s.snapshot_id for s in rows}
    return [sid for sid in snapshot_ids if sid not in have]
