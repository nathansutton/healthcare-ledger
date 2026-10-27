"""Discover NPPES monthly dissemination zips and turn them into ordered snapshots.

A snapshot is identified by its *as-of date*: the end date embedded in the
main member's filename (``npidata_pfile_20050523-20260208.csv`` is as of
2026-02-08). The zip filename date (``..._20260201.zip``) is only CMS's
publication slot and lags the as-of date by one to three weeks. Two zips
can even carry the same payload (the April 2010 zip repeats February 2010),
so snapshots are deduplicated on as-of date.
"""

from __future__ import annotations

import calendar
import logging
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from ...core.errors import UserError
from .schema import STREAMS

log = logging.getLogger(__name__)

# Naming conventions accepted for the monthly full-replacement zip:
#   NPPES_Data_Dissemination_20260201.zip            (archive convention, YYYYMMDD)
#   NPPES_Data_Dissemination_October_2026_V2.zip     (CMS download page convention)
#   NPPES_Data_Dissemination_Sept_2015.zip           (CMS used three-letter months, and "Sept", in 2015-2016)
# Weekly incremental and deactivation zips are deliberately not matched.
MONTH_INDEX = {**{calendar.month_name[i].lower(): i for i in range(1, 13)},
               **{calendar.month_abbr[i].lower(): i for i in range(1, 13)}, "sept": 9}
ZIP_RE = re.compile(
    r"NPPES_Data_Dissemination_(?:(?P<ymd>\d{8})|(?P<month>[A-Za-z]+)_(?P<year>\d{4}))(?:_V\d)?\.zip$",
    re.IGNORECASE)
MEMBER_DATES_RE = re.compile(r"_(\d{8})-(\d{8})\.csv$", re.IGNORECASE)


def _yyyymmdd(s: str) -> date:
    return datetime.strptime(s, "%Y%m%d").date()


def zip_date_from_name(name: str) -> date | None:
    m = ZIP_RE.search(name)
    if not m:
        return None
    if m.group("ymd"):
        return _yyyymmdd(m.group("ymd"))
    month = MONTH_INDEX.get(m.group("month").lower())
    if month is None:
        return None
    return date(int(m.group("year")), month, 1)


@dataclass
class Snapshot:
    zip_path: Path
    zip_date: date
    as_of_date: date
    members: dict[str, str] = field(default_factory=dict)      # stream -> member name
    member_bytes: dict[str, int] = field(default_factory=dict)  # stream -> uncompressed size
    duplicate_of: Path | None = None

    @property
    def snapshot_id(self) -> int:
        return int(self.as_of_date.strftime("%Y%m%d"))

    @property
    def label(self) -> str:
        return self.zip_path.name

    def has(self, stream: str) -> bool:
        """A stream is present only if its member exists and is non-empty.

        CMS shipped zero-byte endpoint files in Jan/Feb 2019. Treating those
        as "no endpoints for anyone" would wrongly close every NPI's history,
        so an empty member counts as not present.
        """
        return stream in self.members and self.member_bytes.get(stream, 0) > 0


def inspect_zip(zip_path: Path) -> Snapshot:
    zip_date = zip_date_from_name(zip_path.name)
    if zip_date is None:
        raise ValueError(f"not an NPPES dissemination zip: {zip_path.name}")
    members: dict[str, str] = {}
    sizes: dict[str, int] = {}
    with open(zip_path, "rb") as fh:
        if fh.read(2) != b"PK":
            raise ValueError(f"{zip_path.name} is not a zip file (an HTML error page saved as .zip? "
                             f"re-download it or move it out of the source directory)")
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            base = Path(info.filename).name
            if "__MACOSX" in info.filename or "header" in base.lower():
                continue
            for stream in STREAMS:
                if re.match(stream.member_pattern, base, re.IGNORECASE):
                    if stream.name in members:
                        raise ValueError(f"{zip_path.name}: two {stream.name} members")
                    members[stream.name] = info.filename
                    sizes[stream.name] = info.file_size
    if "provider" not in members:
        raise ValueError(f"{zip_path.name}: no main npidata member found")
    dm = MEMBER_DATES_RE.search(Path(members["provider"]).name)
    as_of = _yyyymmdd(dm.group(2))
    return Snapshot(zip_path=zip_path, zip_date=zip_date, as_of_date=as_of, members=members, member_bytes=sizes)


def discover(source_dir: Path) -> list[Snapshot]:
    """Return unique snapshots in as-of order. Duplicates are logged and dropped."""
    snaps = [inspect_zip(p) for p in sorted(source_dir.iterdir())
             if ZIP_RE.search(p.name) and not p.name.endswith(".part")]
    # On a tie (same as-of date, same month) prefer the V2 reissue, then the earlier-named zip.
    snaps.sort(key=lambda s: (s.as_of_date, s.zip_date, "_v2" not in s.zip_path.name.lower(), s.zip_path.name))
    unique: list[Snapshot] = []
    for s in snaps:
        if unique and unique[-1].as_of_date == s.as_of_date:
            s.duplicate_of = unique[-1].zip_path
            log.warning("%s duplicates %s (both as of %s); skipping",
                        s.zip_path.name, unique[-1].zip_path.name, s.as_of_date)
            continue
        unique.append(s)
    return unique


def extract_member(snapshot: Snapshot, stream: str, work_dir: Path) -> Path:
    """Stream one member out of the zip onto local disk (DuckDB needs a seekable file).

    Python's zipfile handles stored and deflate members. CMS's April 2018 zip
    uses Deflate64 ("enhanced deflate", method 9), which zipfile rejects, so
    that case falls back to the Info-ZIP ``unzip`` command, which supports it.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    member = snapshot.members[stream]
    out = work_dir / f"{snapshot.snapshot_id}_{Path(member).name}"
    with zipfile.ZipFile(snapshot.zip_path) as zf:
        info = zf.getinfo(member)
        if info.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            with zf.open(member) as src, open(out, "wb") as dst:
                while chunk := src.read(64 << 20):
                    dst.write(chunk)
            return out
    log.info("%s: member %s uses compression method %d; extracting with unzip",
             snapshot.zip_path.name, member, info.compress_type)
    return extract_member_with_unzip(snapshot.zip_path, member, out)


def extract_member_with_unzip(zip_path: Path, member: str, out: Path) -> Path:
    """Extract one member with the ``unzip`` CLI (needed for Deflate64 members)."""
    import shutil
    import subprocess

    if shutil.which("unzip") is None:
        raise RuntimeError(f"{zip_path.name} needs the `unzip` command for member {member} (Deflate64)")
    with open(out, "wb") as dst:
        proc = subprocess.run(["unzip", "-p", str(zip_path), member], stdout=dst, stderr=subprocess.PIPE, check=False)
    if proc.returncode != 0:
        out.unlink(missing_ok=True)
        raise RuntimeError(f"unzip failed on {zip_path.name}:{member}: {proc.stderr.decode(errors='replace')[:200]}")
    return out


def require_source(path: str | Path) -> Path:
    """``path`` as a directory that holds at least one CMS zip; a UserError naming the fix otherwise."""
    p = Path(path)
    if not p.is_dir():
        raise UserError(f"source directory {p} does not exist; it should hold NPPES_Data_Dissemination_*.zip files "
                        f"(pass --source, or `healthcare-ledger download --dest {p}` to fetch the source zips)", exit_code=2)
    if not any(ZIP_RE.search(f.name) and not f.name.endswith(".part") for f in p.iterdir()):
        raise UserError(f"no NPPES_Data_Dissemination_*.zip files in {p}; nothing to do. Run "
                        f"`healthcare-ledger download --dest {p}` to fetch every recorded source zip "
                        f"(docs/MAINTAINING.md), or point --source at your archive", exit_code=2)
    return p
