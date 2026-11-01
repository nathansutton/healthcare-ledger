"""Fetch the CHOW files that ``releases/chow_sources.csv`` pins, sha256-checked: ``name, origin, url, bytes, sha256``,
one row per CSV (``origin`` ``cms``: data.cms.gov's dataset-resources download url of the quarter the load was built
from; CMS replaces the file each quarter, so the pinned url is the dated one). Files land in ``<dest>/raw/<name>``."""

from __future__ import annotations

from pathlib import Path

from ...core import filesources

SOURCES_FILE = "chow_sources.csv"
COLUMNS = ["name", "origin", "url", "bytes", "sha256"]


def read_sources(releases: Path) -> list[dict[str, str]]:
    return filesources.read_manifest(Path(releases) / SOURCES_FILE, "CHOW")


def download(releases: Path, dest: Path, *, check_only: bool = False) -> list[str]:
    rows = [{"directory": "raw", "file": r["name"], "bytes": r["bytes"], "sha256": r["sha256"], "url": r["url"],
             "member": ""} for r in read_sources(releases)]
    return filesources.download_recorded(rows, Path(dest), check_only=check_only, manifest_name=SOURCES_FILE)
