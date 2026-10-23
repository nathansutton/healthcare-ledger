"""Helpers that fabricate tiny NPPES zips so the SCD logic can be tested end to end."""

from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from pathlib import Path

import pytest

from nppes_over_time.schema import ENDPOINT, OTHER_NAME, PRACTICE_LOCATION, PROVIDER

_SQL_BLOCK = re.compile(r'con\.execute\("""(.*?)"""\)', re.DOTALL)
_SQL_QUERY = re.compile(r'con\.(?:execute|sql)\("""(.*?)"""\)', re.DOTALL)


def notebook_sql(notebook: Path, *, queries: bool = False) -> list[str]:
    """The triple-quoted SQL of every con.execute call in a research notebook's code cells, in order.

    With queries=True, also the con.sql calls, which a notebook uses for the result tables it displays.
    """
    pattern = _SQL_QUERY if queries else _SQL_BLOCK
    cells = json.loads(notebook.read_text())["cells"]
    return [m.group(1) for c in cells if c["cell_type"] == "code" for m in pattern.finditer("".join(c["source"]))]


def sql_creating(notebook: Path, table: str) -> str:
    """The one con.execute block of a research notebook that creates the table."""
    [block] = [b for b in notebook_sql(notebook) if f"CREATE TABLE {table} AS" in b]
    return block


MAIN_HEADERS = ["NPI"] + [c.cms_header for c in PROVIDER.columns]


def main_row(npi: str, **fields: str) -> dict[str, str]:
    """A main-file row keyed by canonical short name; everything else blank."""
    by_name = {c.name: c.cms_header for c in PROVIDER.columns}
    row = {h: "" for h in MAIN_HEADERS}
    row["NPI"] = npi
    for k, v in fields.items():
        row[by_name[k]] = v
    return row


def _csv_bytes(headers: list[str], rows: list[dict[str, str]]) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=headers, quoting=csv.QUOTE_ALL, lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return buf.getvalue().encode()


def write_zip(directory: Path, zip_date: str, as_of: str, main_rows: list[dict[str, str]], *,
              headers: list[str] | None = None, pl: list[dict] | None = None,
              othername: list[dict] | None = None, endpoint: list[dict] | None = None,
              empty_endpoint: bool = False, subdir: str = "", raw_members: dict[str, bytes] | None = None) -> Path:
    headers = headers or MAIN_HEADERS
    path = directory / f"NPPES_Data_Dissemination_{zip_date}.zip"
    with zipfile.ZipFile(path, "w") as zf:
        name = "npidata_pfile" if as_of >= "20180501" else "npidata"
        zf.writestr(f"{subdir}{name}_20050523-{as_of}.csv", _csv_bytes(headers, [{h: r.get(h, "") for h in headers} for r in main_rows]))
        zf.writestr(f"{subdir}{name}_20050523-{as_of}_FileHeader.csv", ",".join(headers))
        if pl is not None:
            zf.writestr(f"pl_pfile_20050523-{as_of}.csv", _csv_bytes(["NPI"] + [c.cms_header for c in PRACTICE_LOCATION.columns], pl))
        if othername is not None:
            zf.writestr(f"othername_pfile_20050523-{as_of}.csv", _csv_bytes(["NPI"] + [c.cms_header for c in OTHER_NAME.columns], othername))
        if endpoint is not None:
            zf.writestr(f"endpoint_pfile_20050523-{as_of}.csv", _csv_bytes(["NPI"] + [c.cms_header for c in ENDPOINT.columns], endpoint))
        if empty_endpoint:
            zf.writestr(f"endpoint_pfile_20050523-{as_of}.csv", b"")
        for name, payload in (raw_members or {}).items():
            zf.writestr(name.format(as_of=as_of), payload)
    return path


@pytest.fixture(autouse=True)
def _cwd_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run every test from an empty directory: `export` records releases under ./releases/ and reads
    ./docs/METHODOLOGY.md by default, and tests must never touch the repository's own copies."""
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def source(tmp_path: Path) -> Path:
    d = tmp_path / "zips"
    d.mkdir()
    return d
