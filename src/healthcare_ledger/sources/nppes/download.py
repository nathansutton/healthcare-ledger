"""Rebuild a source directory from the source manifest, and fetch new months from CMS.

Two kinds of month:

* **Recorded months.** ``releases/sources.csv`` (manifest.py) names the exact zip every published
  snapshot was built from: its bytes, sha256 and URL (CMS, a Wayback Machine capture, or NBER's
  copy under https://data.nber.org/npi/), or ``origin = archive`` when no public copy is known.
  ``download`` fetches each recorded month that ``--dest`` lacks from that URL (or from
  ``--mirror``) and checks the sha256, so a fresh
  clone gets the same bytes the release was built from, never a near-miss such as the V2 twin
  of a V1 file.
* **New months**, after the last recorded one. CMS's download page links only the current
  month, but older monthly zips stay online for a while at https://download.cms.gov/nppes/ (in
  October 2026 CMS removed everything before January 2021) under names that changed over time:

    NPPES_Data_Dissemination_<Month>_<Year>.zip        full month name (most months)
    NPPES_Data_Dissemination_<Mon>_<Year>.zip          three-letter month, Aug 2015 to Apr 2016
    NPPES_Data_Dissemination_Sept_2015.zip             "Sept", once
    NPPES_Data_Dissemination_<Month>_<Year>_V2.zip     V2 layout, Jan 2025 onward (V2 only from Mar 2026)

  There is no directory listing, so candidates are generated per month (and, with ``--cdx``,
  taken from the Wayback Machine's CDX index) and verified with a HEAD request. A fetched new
  month is checked against the md5 in CMS's ETag and recorded in the manifest.

The CMS edge (Akamai) answers bursts with an "Access Denied" page (HTML, with HTTP 200 or 403),
so probes and downloads are sequential and paced, and any HTML answer stops the run: it means
"blocked", not "present" or "missing". Downloads resume where they stopped (HTTP Range), and a
finished file must match its expected size, open as a zip and match its hash before it is
renamed into place.

Usage:
    healthcare-ledger download --probe            # month table: local / recorded source / CMS
    healthcare-ledger download                    # fetch every recorded month you lack, then new CMS months
    healthcare-ledger download --check            # sha256 every local zip against releases/sources.csv
    healthcare-ledger download --mirror URL       # fetch 'archive' months from URL/<file>
    healthcare-ledger download --record ZIP [--url URL]   # add a zip you obtained elsewhere to the manifest

``fetch_missing`` is the same thing as a function, used by ``healthcare-ledger monthly``.
"""

from __future__ import annotations

import argparse
import calendar
import re
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import date
from pathlib import Path

from ...core.errors import UserError
from .manifest import Source, digests, origin_of, read_sources, sources_path, upsert_source
from .snapshots import inspect_zip, zip_date_from_name

BASE = "https://download.cms.gov/nppes/"
CDX = "http://web.archive.org/cdx/search/cdx?url=download.cms.gov/nppes/*&output=txt&fl=original&collapse=urlkey"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) healthcare-ledger/0.1 (+https://github.com/nathansutton/healthcare-ledger)"}
FIRST_SERVED = date(2021, 1, 1)  # nothing earlier is online at CMS (Jun 2015 to Dec 2020 were removed in Oct 2026)
ABSENT = {404, 410}  # how the CMS origin answers a name it does not hold; 403 is the edge's block
ETAG_MD5_RE = re.compile(r'^"?([0-9a-f]{32})(?::\d+)?"?$')  # Akamai NetStorage: "<md5 of the bytes>:<mtime>"


class Blocked(UserError):
    """The CMS edge refused us; retrying now only extends the block."""


def month_iter(start: date, end: date):
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def candidate_names(y: int, m: int) -> list[str]:
    """Most-preferred first: V2, full name, three-letter, 'Sept'."""
    full, ab = calendar.month_name[m], calendar.month_abbr[m]
    names = [f"NPPES_Data_Dissemination_{full}_{y}_V2.zip", f"NPPES_Data_Dissemination_{full}_{y}.zip",
             f"NPPES_Data_Dissemination_{ab}_{y}.zip"]
    if m == 9:
        names.append(f"NPPES_Data_Dissemination_Sept_{y}.zip")
    return names


def parse_month(name: str) -> tuple[int, int] | None:
    d = zip_date_from_name(name)
    return (d.year, d.month) if d else None


def _blocked(url: str) -> Blocked:
    return Blocked(f"CMS answered {url} with an HTML page (the edge's 'Access Denied' block); "
                   f"wait an hour or so and run again", exit_code=1)


def head_info(url: str, tries: int = 3) -> tuple[int, str | None] | None:
    """(size, md5 from the ETag) of ``url`` at CMS (size 0 if CMS does not say), or None if CMS does not hold it.

    Raises ``UserError`` when the answer means "blocked" or "unreachable" rather than
    "absent", so a rate-limited probe never reads as a list of present (or missing) months.
    """
    req = urllib.request.Request(url, method="HEAD", headers=UA)
    for attempt in range(1, tries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                if "html" in (r.headers.get("Content-Type") or "").lower():
                    raise _blocked(url)
                m = ETAG_MD5_RE.match((r.headers.get("ETag") or "").strip())
                return int(r.headers.get("Content-Length") or 0), (m.group(1) if m else None)
        except urllib.error.HTTPError as e:
            if e.code in ABSENT:
                return None
            if e.code == 403:
                raise _blocked(url) from None
            err: Exception = e
        except (urllib.error.URLError, OSError) as e:  # DNS, refused, reset, timeout
            err = e
        if attempt < tries:
            time.sleep(10 * attempt)
    raise UserError(f"could not reach CMS for {url} after {tries} tries ({err}); check the network and run again")


def head(url: str, tries: int = 3) -> int | None:
    info = head_info(url, tries)
    return None if info is None else info[0]


def cdx_names() -> set[str]:
    """Filenames the Wayback Machine has ever seen under download.cms.gov/nppes/."""
    try:
        with urllib.request.urlopen(urllib.request.Request(CDX, headers=UA), timeout=120) as r:
            body = r.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        print(f"warning: Wayback CDX unavailable ({e}); using generated names only", file=sys.stderr)
        return set()
    if "<html" in body[:200].lower():
        print("warning: Wayback CDX returned an HTML page (service offline?); using generated names only", file=sys.stderr)
        return set()
    return {line.rsplit("/", 1)[-1].strip() for line in body.splitlines() if line.strip()}


def local_months(dest: Path) -> dict[tuple[int, int], str]:
    out: dict[tuple[int, int], str] = {}
    if not dest.exists():
        return out
    for p in sorted(dest.iterdir()):
        if p.name.endswith(".part") or p.suffix.lower() != ".zip":
            continue
        d = zip_date_from_name(p.name)
        if d:
            out.setdefault((d.year, d.month), p.name)
    return out


def probe(dest: Path, since: date, until: date, use_cdx: bool, pace: float = 0.5, skip: set | None = None):
    """One row per month: (year, month, local name, CMS size, CMS name, CMS md5).

    Months held locally, and months in ``skip``, are not probed.
    """
    have = local_months(dest)
    skip = skip or set()
    months = list(month_iter(since, until))
    extra: dict[tuple[int, int], list[str]] = {}
    if use_cdx:
        for n in cdx_names():
            ym = parse_month(n)
            if ym:
                extra.setdefault(ym, []).append(n)

    def remote(ym):
        seen = []
        for name in candidate_names(*ym) + [n for n in extra.get(ym, []) if n not in candidate_names(*ym)]:
            if name in seen:
                continue
            seen.append(name)
            info = head_info(BASE + name)
            time.sleep(pace)
            if info is not None:
                return info[0], name, info[1]
        return 0, None, None

    remotes = [(0, None, None) if ym in have or ym in skip else remote(ym) for ym in months]
    return [(y, m, have.get((y, m)), size, name, md5) for (y, m), (size, name, md5) in zip(months, remotes)]


def _verify(path: Path, expected: int, sha256: str | None, md5: str | None) -> str:
    """Check a finished download; return its sha256. Raises ValueError (retry) or UserError (give up)."""
    got = path.stat().st_size
    if expected and got != expected:
        raise ValueError(f"size {got} != expected {expected}")
    try:
        zipfile.ZipFile(path).close()  # reads the central directory: catches a truncated body
    except zipfile.BadZipFile as e:
        raise ValueError(f"not a complete zip ({e})") from None
    sha, m = digests(path)
    if md5 and m != md5:
        raise ValueError(f"md5 {m} != the server's ETag {md5}")
    if sha256 and sha != sha256:
        raise UserError(f"{path.name}: sha256 {sha} is not {sha256}, the zip the release was built from; the "
                        f"upstream file changed. Do not load it; report it (docs/MAINTAINING.md)", exit_code=1)
    return sha


def fetch(dest: Path, name: str, expected: int, tries: int = 6, *, url: str | None = None,
          sha256: str | None = None, md5: str | None = None) -> str | None:
    """Download ``url`` (default CMS's ``name``) to ``dest/name``; return its sha256, or None after ``tries`` failures.

    A partial ``.part`` file is kept between attempts and resumed with a Range request.
    """
    url = url or BASE + name
    target, tmp = dest / name, dest / (name + ".part")
    for attempt in range(1, tries + 1):
        try:
            have = tmp.stat().st_size if tmp.exists() else 0
            if expected and have > expected:
                tmp.unlink()
                have = 0
            if not (expected and have == expected):
                headers = dict(UA, **({"Range": f"bytes={have}-"} if have else {}))
                try:
                    r = urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=300)
                except urllib.error.HTTPError as e:
                    if e.code == 416 and have:  # nothing past what we hold: verify it as is
                        r = None
                    elif e.code == 403:
                        raise _blocked(url) from None
                    else:
                        raise
                if r is not None:
                    with r:
                        if "html" in (r.headers.get("Content-Type") or "").lower():
                            raise _blocked(url)
                        resumed = have and getattr(r, "status", 200) == 206
                        with open(tmp, "ab" if resumed else "wb") as f:
                            if not resumed:
                                first = r.read(4)
                                if first[:2] != b"PK":
                                    raise ValueError("response is not a zip")
                                f.write(first)
                            while chunk := r.read(16 << 20):
                                f.write(chunk)
            if expected and tmp.stat().st_size < expected:
                raise ValueError(f"connection ended at {tmp.stat().st_size} of {expected} bytes")
            try:
                sha = _verify(tmp, expected, sha256, md5)
            except ValueError:
                tmp.unlink(missing_ok=True)  # complete but wrong: start over
                raise
            except UserError:
                tmp.unlink(missing_ok=True)
                raise
            tmp.rename(target)
            return sha
        except UserError:
            raise
        except Exception as e:  # noqa: BLE001
            if attempt == tries:
                print(f"  attempt {attempt}/{tries} failed: {e}", file=sys.stderr)
                break
            wait = 120 * attempt
            print(f"  attempt {attempt}/{tries} failed: {e}; waiting {wait}s", file=sys.stderr)
            time.sleep(wait)
    return None


def month_arg(text: str) -> date:
    """Parse a YYYY-MM command-line month."""
    try:
        if len(text) != 7 or text[4] != "-":
            raise ValueError
        return date(int(text[:4]), int(text[5:7]), 1)
    except ValueError:
        raise UserError(f"{text!r} is not a month; use YYYY-MM", exit_code=2) from None


def _record(manifest: Path | None, dest: Path, name: str, sha: str, url: str) -> None:
    if manifest is None:
        return
    snap = inspect_zip(dest / name)
    upsert_source(manifest, Source(snap.snapshot_id, name, (dest / name).stat().st_size, sha, origin_of(url), url))
    print(f"  recorded {name} as snapshot {snap.snapshot_id} in {manifest}", file=sys.stderr)


def fetch_missing(dest: Path, since: date | None = None, until: date | None = None, *, releases: Path | None = None,
                  cdx: bool = False, probe_only: bool = False, pause: float = 45.0, mirror: str | None = None,
                  pace: float = 0.5) -> list[str]:
    """Print the month table, then download every month ``dest`` lacks: recorded months from their
    manifest URL, then the months CMS serves after the last recorded one.

    Returns the names of the files fetched. Raises ``UserError`` if any download failed after
    retries, or if a recorded month has no public source and ``--mirror`` was not given, so
    nothing is silently left out.
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    manifest = sources_path(releases)
    recorded = read_sources(manifest)
    by_month = {s.month: s for s in recorded}
    first = min(by_month) if by_month else None
    last = max(by_month) if by_month else None
    since = since or (date(*first, 1) if first else FIRST_SERVED)
    until = until or date.today().replace(day=1)
    # Recorded history is authoritative: only months after the last recorded one are asked of CMS.
    skip = {ym for ym in month_iter(since, until) if last and ym <= last}
    rows = probe(dest, since, until, cdx, pace=pace, skip=skip)
    have = local_months(dest)

    todo: list[tuple[str, int, str, str | None, str | None]] = []  # name, size, url, sha256, md5
    unavailable: list[Source] = []
    print(f"{'month':8} {'local':48} {'source':8} {'file':52} {'MB':>6}")
    for y, m, local, size, name, md5 in rows:
        s = by_month.get((y, m))
        if s:
            src_url = s.url or (f"{mirror.rstrip('/')}/{s.file}" if mirror else "")
            line = f"{y}-{m:02d}  {local or '-':48} {s.origin:8} {s.file:52} {s.bytes / 1e6:6.0f}"
            if local is None:
                if src_url:
                    todo.append((s.file, s.bytes, src_url, s.sha256, None))
                else:
                    unavailable.append(s)
                    line += "  no public source"
            elif (dest / local).stat().st_size != s.bytes:
                line += f"  SIZE DIFFERS from the recorded zip; run `download --check`"
            print(line)
        elif name:
            print(f"{y}-{m:02d}  {local or '-':48} {'cms':8} {name:52} {size / 1e6:6.0f}")
            if local is None:
                todo.append((name, size, BASE + name, None, md5))
        elif local:
            print(f"{y}-{m:02d}  {local:48} {'-':8} {'(not in sources.csv)':52}")
        elif last and (y, m) <= last:
            print(f"{y}-{m:02d}  {'-':48} {'-':8} {'(no file was published, or none survives)':52}")
    n_rec = sum(1 for (y, m, *_) in rows if (y, m) in by_month)
    n_local = sum(1 for (y, m, local, *_) in rows if (y, m) in by_month and local)
    print(f"\n{n_rec} recorded snapshots in range: {n_local} local, {n_rec - n_local - len(unavailable)} to download, "
          f"{len(unavailable)} with no public source; "
          f"{sum(1 for r in rows if r[4] and (r[0], r[1]) not in by_month)} new months at CMS; "
          f"{len(todo)} downloads to do", file=sys.stderr)
    if probe_only:
        return []
    fetched, failed = [], []
    for i, (name, size, url, sha256, md5) in enumerate(todo):
        print(f"downloading {name} ({size / 1e6:.0f} MB) from {url}", file=sys.stderr)
        sha = fetch(dest, name, size, url=url, sha256=sha256, md5=md5)
        if sha:
            fetched.append(name)
            if sha256 is None:
                _record(manifest, dest, name, sha, url)
        else:
            failed.append(name)
        if i < len(todo) - 1 and url.startswith(BASE):
            time.sleep(pause)  # be polite; bursts get the whole host blocked for a while
    if failed:
        raise UserError(f"download failed after retries for {', '.join(failed)}; CMS's edge rate-limits bursts, "
                        f"so wait a while and run `healthcare-ledger download` again (finished parts resume)")
    if unavailable:
        raise UserError(f"{len(unavailable)} recorded snapshot(s) have no public source and are not in {dest}: "
                        + ", ".join(s.file for s in unavailable)
                        + ". Copy them into the directory or pass --mirror URL; see docs/MAINTAINING.md")
    return fetched


def check(dest: Path, releases: Path | None) -> int:
    """sha256 every recorded month held in ``dest`` against the manifest. Returns the number of problems."""
    manifest = sources_path(releases)
    recorded = read_sources(manifest)
    if not recorded:
        raise UserError(f"no source manifest at {manifest}; nothing to check against", exit_code=2)
    have = local_months(dest)
    bad = missing = 0
    for s in recorded:
        local = have.get(s.month)
        if local is None:
            print(f"MISSING   {s.snapshot_id}  {s.file}")
            missing += 1
            continue
        sha, _ = digests(dest / local)
        if sha == s.sha256:
            print(f"ok        {s.snapshot_id}  {local}")
        else:
            print(f"MISMATCH  {s.snapshot_id}  {local}: sha256 {sha}, recorded {s.sha256} ({s.file})")
            bad += 1
    print(f"\n{len(recorded) - bad - missing} of {len(recorded)} recorded zips match, {bad} differ, {missing} missing",
          file=sys.stderr)
    return bad + missing


def record(path: Path, url: str, releases: Path | None) -> None:
    manifest = sources_path(releases)
    if manifest is None:
        raise UserError("--record needs --releases", exit_code=2)
    sha, _ = digests(path)
    snap = inspect_zip(path)
    upsert_source(manifest, Source(snap.snapshot_id, path.name, path.stat().st_size, sha, origin_of(url), url))
    print(f"recorded {path.name} as snapshot {snap.snapshot_id} ({origin_of(url)}) in {manifest}")


def add_arguments(ap: argparse.ArgumentParser) -> None:
    """The NPPES options of ``download``, next to the ``--dest``, ``--releases`` and ``--check`` every source takes
    (``Source.download_options``)."""
    ap.add_argument("--since", help="NPPES: first month to consider, YYYY-MM (default: the first recorded month)")
    ap.add_argument("--until", help="NPPES: last month to consider, YYYY-MM (default: this month)")
    ap.add_argument("--cdx", action="store_true", help="NPPES: also enumerate new-month names from the Wayback Machine CDX index")
    ap.add_argument("--probe", action="store_true", help="NPPES: report availability only, download nothing")
    ap.add_argument("--mirror", help="NPPES: base URL holding the 'archive' zips under their recorded names")
    ap.add_argument("--record", metavar="ZIP", help="NPPES: add a zip you obtained yourself to sources.csv")
    ap.add_argument("--url", default="", help="NPPES: with --record, where the zip can be downloaded (omit if nowhere public)")


def run(a: argparse.Namespace) -> int:
    releases = Path(a.releases) if a.releases else None
    if a.record:
        record(Path(a.record), a.url, releases)
        return 0
    if a.check:
        return 1 if check(Path(a.dest), releases) else 0
    since = month_arg(a.since) if a.since else None
    until = month_arg(a.until or date.today().strftime("%Y-%m"))
    fetched = fetch_missing(Path(a.dest), since, until, releases=releases, cdx=a.cdx,
                            probe_only=a.probe, mirror=a.mirror)
    if fetched:
        print("done; run `healthcare-ledger load` (or `monthly`). If a fetched month predates the newest loaded "
              "snapshot, roll back to the month before it and load again.", file=sys.stderr)
    return 0
