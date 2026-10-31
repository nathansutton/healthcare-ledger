"""Fetch the IRS files that ``releases/irs_sources.csv`` pins, sha256-checked.

One row per file ``load`` reads::

    name, origin, url, bytes, sha256

``origin`` is ``nccs`` for a Form 990 e-file table the Nonprofit Open Data Collective publishes (parquet, one per
table and tax year) and ``irs`` for an Exempt Organizations Business Master File extract region. Files land in
``<dest>/raw/<name>``. The BMF is a current snapshot the IRS replaces monthly, so a re-fetch after a new release
reports a mismatch; the pinned bytes are the ones the load was built from.
"""

from __future__ import annotations

from pathlib import Path

from ...core import filesources

SOURCES_FILE = "irs_sources.csv"
COLUMNS = ["name", "origin", "url", "bytes", "sha256"]


def read_sources(releases: Path) -> list[dict[str, str]]:
    return filesources.read_manifest(Path(releases) / SOURCES_FILE, "IRS")


def download(releases: Path, dest: Path, *, check_only: bool = False) -> list[str]:
    """Fill ``dest/raw`` with every pinned file it lacks. ``check_only`` fetches nothing and reports the files that are
    missing or differ. Returns the problems; empty means every file is present and matches."""
    rows = [{"directory": "raw", "file": r["name"], "bytes": r["bytes"], "sha256": r["sha256"], "url": r["url"],
             "member": ""} for r in read_sources(releases)]
    return filesources.download_recorded(rows, Path(dest), check_only=check_only, manifest_name=SOURCES_FILE)
