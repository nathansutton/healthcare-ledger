"""Source manifests for the quarterly file sets (Medicare PPEF, Provider of Services).

The NPPES months are pinned by ``releases/sources.csv`` (sources/nppes/manifest.py), one zip per snapshot. The
quarterly collections are sets of CSVs in one directory per quarter, so they get their own
manifests of the same shape, one row per loaded file::

    snapshot_id, directory, file, bytes, sha256, origin, url, member

``directory`` is the quarter directory, ``file`` the CSV inside it, ``url`` where it is public
and ``member`` the path inside the zip at ``url`` when the file is extracted from one. The loader
records size and sha256 of every file it loads; ``write_manifest`` turns that into rows, keeping
the origin, url and member a maintainer already filled in; ``manifest_problems`` is the export
gate (every loaded file recorded with the same sha256); ``download_recorded`` rebuilds the
quarter directories from a fresh clone, sha256-checked.
"""

from __future__ import annotations

import csv
import hashlib
import logging
import time
import zipfile
from pathlib import Path

from .errors import UserError

log = logging.getLogger(__name__)

COLUMNS = ["snapshot_id", "directory", "file", "bytes", "sha256", "origin", "url", "member"]


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(16 << 20):
            h.update(chunk)
    return h.hexdigest()


def read_manifest(path: Path, what: str) -> list[dict[str, str]]:
    if not path.exists():
        raise UserError(f"{path} does not exist; nothing records where the {what} files come from", exit_code=2)
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def manifest_problems(path: Path, loaded: list[tuple[int, str, str]], fix: str) -> list[str]:
    """Loaded files ``(snapshot_id, file, sha256)`` that ``path`` does not record with the same sha256."""
    if not path.exists():
        return [f"{path} does not exist; {fix}"]
    with open(path, newline="") as f:
        recorded = {(int(r["snapshot_id"]), r["file"]): r["sha256"] for r in csv.DictReader(f)}
    problems = []
    for sid, file, sha in loaded:
        have = recorded.get((sid, file))
        if have is None:
            problems.append(f"{sid} {file}: no row in {path.name}")
        elif have != sha:
            problems.append(f"{sid} {file}: {path.name} records sha256 {have[:12]}..., the loaded file is {sha[:12]}...")
    return problems


def write_manifest(path: Path, loaded: list[tuple[int, str, str, int, str]]) -> int:
    """Write or update ``path`` from ``(snapshot_id, directory, file, bytes, sha256)`` rows, keeping the
    origin, url and member already recorded for a file. Returns the number of rows whose url is empty."""
    old: dict[tuple[int, str], dict] = {}
    if path.exists():
        with open(path, newline="") as f:
            old = {(int(r["snapshot_id"]), r["file"]): r for r in csv.DictReader(f)}
    rows = []
    for sid, directory, file, nbytes, sha in loaded:
        prev = old.get((sid, file), {})
        rows.append({"snapshot_id": sid, "directory": directory, "file": file, "bytes": nbytes, "sha256": sha,
                     "origin": prev.get("origin", ""), "url": prev.get("url", ""), "member": prev.get("member", "")})
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    return sum(1 for r in rows if not r["url"])


def fetch_url(url: str, target: Path, tries: int = 4) -> None:
    """Stream ``url`` into ``target`` (via ``target.part``), retrying with a growing pause."""
    import urllib.error
    import urllib.request

    from ..sources.nppes.download import UA

    tmp = target.with_name(target.name + ".part")
    for attempt in range(1, tries + 1):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=300) as r:
                if "html" in (r.headers.get("Content-Type") or "").lower():
                    raise ValueError("the server answered with a web page, not the file")
                with open(tmp, "wb") as f:
                    while chunk := r.read(16 << 20):
                        f.write(chunk)
            tmp.rename(target)
            return
        except (urllib.error.URLError, OSError, ValueError) as e:
            tmp.unlink(missing_ok=True)
            if attempt == tries:
                raise UserError(f"could not download {url}: {e}; try again later (data.cms.gov throttles bursts)") from None
            log.warning("attempt %d/%d for %s failed: %s; retrying", attempt, tries, url, e)
            time.sleep(60 * attempt)


def download_recorded(rows: list[dict[str, str]], dest: Path, *, check_only: bool = False, pace: float = 1.0,
                      manifest_name: str) -> list[str]:
    """Rebuild the quarter directories of a manifest under ``dest``, sha256-checked.

    Each row is a file the release was built from: ``dest/<directory>/<file>``. A file already there
    with the recorded sha256 is kept. Otherwise it is fetched from ``url``, or, when ``member`` is set,
    extracted from the zip at ``url`` (cached once in ``dest/zips/``). ``check_only`` downloads nothing
    and reports the files that are missing or differ. Returns the problems; empty means every file
    is present and matches.
    """
    dest = Path(dest)
    problems: list[str] = []
    zips: dict[str, Path] = {}
    for r in rows:
        target = dest / r["directory"] / r["file"]
        if target.is_file() and target.stat().st_size == int(r["bytes"]) and sha256_of(target) == r["sha256"]:
            continue
        if check_only:
            problems.append(f"{r['directory']}/{r['file']}: {'differs from the recorded sha256' if target.exists() else 'missing'}")
            continue
        if not r["url"]:
            problems.append(f"{r['directory']}/{r['file']}: no url recorded in {manifest_name}")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if r["member"]:
            zpath = zips.get(r["url"])
            if zpath is None:
                zpath = dest / "zips" / r["url"].rsplit("/", 1)[-1]
                if not zpath.exists():
                    zpath.parent.mkdir(parents=True, exist_ok=True)
                    log.info("downloading %s", r["url"])
                    fetch_url(r["url"], zpath)
                    time.sleep(pace)
                zips[r["url"]] = zpath
            log.info("extracting %s from %s", r["member"], zpath.name)
            tmp = target.with_name(target.name + ".part")
            with zipfile.ZipFile(zpath) as z, z.open(r["member"]) as src, open(tmp, "wb") as out:
                while chunk := src.read(16 << 20):
                    out.write(chunk)
            tmp.rename(target)
        else:
            log.info("downloading %s", r["url"])
            fetch_url(r["url"], target)
            time.sleep(pace)
        got = sha256_of(target)
        if got != r["sha256"]:
            target.rename(target.with_name(target.name + ".mismatch"))
            problems.append(f"{r['directory']}/{r['file']}: {r['url']} now serves sha256 {got[:12]}..., the release was built "
                            f"from {r['sha256'][:12]}...; the upstream file changed (kept as .mismatch)")
    return problems
