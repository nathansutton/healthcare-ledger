"""Fetch the AHRQ Compendium files that ``releases/ahrq_sources.csv`` pins, sha256-checked.

One row per file ``load`` reads (a zip for the 2016 and 2018 editions, whose files AHRQ ships together)::

    name, origin, url, bytes, sha256

``origin`` is ``ahrq`` for a file ahrq.gov serves and ``wayback`` for one only the Internet Archive still has,
whose ``url`` is then that capture. ahrq.gov intermittently answers scripts with an empty 202, so a file from
``ahrq`` that does not come back falls back to the archive's latest capture of the same url. Some captures keep
the server's gzip transfer encoding, which is undone. Files land in ``<dest>/raw/<name>``.
"""

from __future__ import annotations

import gzip
import logging
import time
import urllib.request
from pathlib import Path

from ...core import filesources

log = logging.getLogger(__name__)

SOURCES_FILE = "ahrq_sources.csv"
COLUMNS = ["name", "origin", "url", "bytes", "sha256"]
BASE = "https://www.ahrq.gov/sites/default/files/wysiwyg/chsp/compendium/"
WAYBACK = "https://web.archive.org/web/2026id_/"      # the capture nearest 2026, served as captured
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"}


def _get(url: str, timeout: int = 300) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
            return r.status, r.read()
    except Exception as e:  # noqa: BLE001 - any failure falls through to the next source
        return getattr(e, "code", 0) or 0, b""


def read_sources(releases: Path) -> list[dict[str, str]]:
    return filesources.read_manifest(Path(releases) / SOURCES_FILE, "AHRQ Compendium")


def _fetch(row: dict[str, str], pause: float) -> tuple[bytes, str]:
    status, body = _get(row["url"])
    where = f"{row['url']} ({status})"
    if (status != 200 or not body) and row["origin"] == "ahrq":
        time.sleep(pause)
        status, body = _get(WAYBACK + row["url"])
        where += f", then the Internet Archive ({status})"
    if status != 200:
        body = b""
    if body[:2] == b"\x1f\x8b" and not row["name"].endswith((".gz", ".zip")):
        body = gzip.decompress(body)
    return body, where


def download(releases: Path, dest: Path, *, check_only: bool = False, pause: float = 1.0) -> list[str]:
    """Fill ``dest/raw`` with every pinned file it lacks. ``check_only`` fetches nothing and reports the files
    that are missing or differ. Returns the problems; empty means every file is present and matches."""
    raw = Path(dest) / "raw"
    problems: list[str] = []
    for r in read_sources(releases):
        target = raw / r["name"]
        if target.is_file() and target.stat().st_size == int(r["bytes"]) and filesources.sha256_of(target) == r["sha256"]:
            continue
        if check_only:
            problems.append(f"raw/{r['name']}: {'differs from the recorded sha256' if target.exists() else 'missing'}")
            continue
        log.info("downloading %s", r["url"])
        body, where = _fetch(r, pause)
        time.sleep(pause)
        if not body:
            problems.append(f"raw/{r['name']}: not served by {where}")
            continue
        raw.mkdir(parents=True, exist_ok=True)
        part = target.with_name(target.name + ".part")
        part.write_bytes(body)
        got = filesources.sha256_of(part)
        if got != r["sha256"]:
            part.rename(target.with_name(target.name + ".mismatch"))
            problems.append(f"raw/{r['name']}: {where} now serves sha256 {got[:12]}..., {SOURCES_FILE} records "
                            f"{r['sha256'][:12]}...; the upstream file changed (kept as .mismatch)")
            continue
        part.rename(target)
    return problems
