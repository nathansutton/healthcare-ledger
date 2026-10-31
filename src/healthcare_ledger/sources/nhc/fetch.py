"""Fetch the Nursing Home Compare ownership files that ``releases/nhc_sources.csv`` pins, sha256-checked.

One row per file ``load`` reads::

    name, origin, url, bytes, sha256

``name`` is ``ownership_<yyyy>_<mm>.csv``; ``origin`` is ``wayback`` and ``url`` the Internet Archive capture of
CMS's monthly archive zip, with the member to extract after ``#`` (``...zip#Ownership_Download.csv``). The archive
asks for a pause between requests. Files land in ``<dest>/raw/<name>``.
"""

from __future__ import annotations

import io
import logging
import time
import urllib.request
import zipfile
from pathlib import Path

from ...core import filesources

log = logging.getLogger(__name__)

SOURCES_FILE = "nhc_sources.csv"
COLUMNS = ["name", "origin", "url", "bytes", "sha256"]
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"}


def read_sources(releases: Path) -> list[dict[str, str]]:
    return filesources.read_manifest(Path(releases) / SOURCES_FILE, "Nursing Home Compare")


def download(releases: Path, dest: Path, *, check_only: bool = False, pause: float = 5.0) -> list[str]:
    """Fill ``dest/raw`` with every pinned file it lacks, extracting the member from each capture's zip. ``check_only``
    fetches nothing and reports the files that are missing or differ. Returns the problems."""
    raw = Path(dest) / "raw"
    problems: list[str] = []
    for r in read_sources(releases):
        target = raw / r["name"]
        if target.is_file() and target.stat().st_size == int(r["bytes"]) and filesources.sha256_of(target) == r["sha256"]:
            continue
        if check_only:
            problems.append(f"raw/{r['name']}: {'differs from the recorded sha256' if target.exists() else 'missing'}")
            continue
        url, _, member = r["url"].partition("#")
        log.info("downloading %s", url)
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=900) as resp:
                body = resp.read()
            data = zipfile.ZipFile(io.BytesIO(body)).read(member) if member else body
        except Exception as e:  # noqa: BLE001 - reported, the next file is tried
            problems.append(f"raw/{r['name']}: {url}: {e}")
            time.sleep(pause)
            continue
        raw.mkdir(parents=True, exist_ok=True)
        part = target.with_name(target.name + ".part")
        part.write_bytes(data)
        got = filesources.sha256_of(part)
        if got != r["sha256"]:
            part.rename(target.with_name(target.name + ".mismatch"))
            problems.append(f"raw/{r['name']}: the capture now serves sha256 {got[:12]}..., {SOURCES_FILE} records {r['sha256'][:12]}... (kept as .mismatch)")
        else:
            part.rename(target)
        time.sleep(pause)
    return problems
