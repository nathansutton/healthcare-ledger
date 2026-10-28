"""Fetch the Medicare Physician & Other Practitioners by Provider and Service files that
``releases/medicare_utilization_sources.csv`` pins, sha256-checked.

One row per file ``load`` reads::

    name, origin, url, bytes, sha256

(the convention of ``releases/medicaid_sources.csv``). Each file is 2 to 3 GB, so it is streamed to disk. Files land in
``<dest>/raw/<name>``.
"""

from __future__ import annotations

from pathlib import Path

from ...core import filesources

SOURCES_FILE = "medicare_utilization_sources.csv"
COLUMNS = ["name", "origin", "url", "bytes", "sha256"]


def read_sources(releases: Path) -> list[dict[str, str]]:
    return filesources.read_manifest(Path(releases) / SOURCES_FILE, "Medicare utilization")


def download(releases: Path, dest: Path, *, check_only: bool = False) -> list[str]:
    """Fill ``dest/raw`` with every pinned file it lacks. ``check_only`` fetches nothing and reports the files that are
    missing or differ. Returns the problems; empty means every file is present and matches."""
    rows = [{"directory": "raw", "file": r["name"], "bytes": r["bytes"], "sha256": r["sha256"], "url": r["url"],
             "member": ""} for r in read_sources(releases)]
    return filesources.download_recorded(rows, Path(dest), check_only=check_only, manifest_name=SOURCES_FILE)
