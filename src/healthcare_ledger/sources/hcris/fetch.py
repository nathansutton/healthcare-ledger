"""Fetch the HCRIS cost-report zips that ``releases/hcris_sources.csv`` pins, sha256-checked.

One row per zip ``load`` reads::

    name, origin, url, bytes, sha256

(``origin`` is ``cms``: downloads.cms.gov/FILES/HCRIS/). CMS re-issues the open fiscal years as reports are settled,
so a re-fetch of a recent year may report a mismatch; the pinned bytes are the ones the load was built from. Files
land in ``<dest>/raw/<name>``.
"""

from __future__ import annotations

from pathlib import Path

from ...core import filesources

SOURCES_FILE = "hcris_sources.csv"
COLUMNS = ["name", "origin", "url", "bytes", "sha256"]


def read_sources(releases: Path) -> list[dict[str, str]]:
    return filesources.read_manifest(Path(releases) / SOURCES_FILE, "HCRIS")


def download(releases: Path, dest: Path, *, check_only: bool = False) -> list[str]:
    rows = [{"directory": "raw", "file": r["name"], "bytes": r["bytes"], "sha256": r["sha256"], "url": r["url"],
             "member": ""} for r in read_sources(releases)]
    return filesources.download_recorded(rows, Path(dest), check_only=check_only, manifest_name=SOURCES_FILE)
