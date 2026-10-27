"""Load the AHRQ Compendium files into ``<warehouse>/ahrq.duckdb``, one table per file type, rebuilt from scratch.

Each table stacks its file type's years with a ``year`` column and the ``source_file`` it came from, taking
the latest revision AHRQ published for each year (``FILES``; the earlier revisions are not loaded). Columns
are matched by name across years, in the order they first appear; a year without a column has NULL there.
Every column but ``year`` is text, because ids such as CCNs and PAC ids carry leading zeros, and an empty
field is NULL.

AHRQ's CSVs are a mix of UTF-8 and Latin-1, some with bare CR line endings, so each one is decoded and
re-split in Python (``normalized``) and written as UTF-8 for DuckDB to read. The outpatient-site files are
only published as ``.xlsx`` and are read with openpyxl. A name with a directory, ``<zip stem>/<member>``, is a
member of ``raw/<zip stem>.zip``, read from the zip that ``releases/ahrq_sources.csv`` pins.
"""

from __future__ import annotations

import csv
import io
import logging
import os
import tempfile
import zipfile
from pathlib import Path

import duckdb

from ...core.connect import source_file
from ...core.errors import UserError

log = logging.getLogger(__name__)

DATABASE = "ahrq"
PREFIX = "ahrq_"

# table (without the ahrq_ prefix) -> year -> file in raw/, the latest revision of each
FILES: dict[str, dict[int, str]] = {
    "system": {
        2016: "2016-compendium-files-2020update/chsp-compendium-2019update.csv",
        2018: "chsp-compendium-2018-updated-2021.csv",
        2020: "chsp-compendium-2020-rev.csv",
        2021: "chsp-compendium-2021-rev.csv",
        2022: "chsp-compendium-2022-rev.csv",
        2023: "chsp-compendium-2023-rev.csv",
    },
    "hospital_linkage": {
        2016: "2016-compendium-files-2020update/compendium_system_hospital_linkage_file.csv",
        2018: "2018-compendium-files-2021update/chsp-hospital-linkage-2018.csv",
        2020: "chsp-hospital-linkage-2020-rev.csv",
        2021: "chsp-hospital-linkage-2021-rev.csv",
        2022: "chsp-hospital-linkage-2022-rev.csv",
        2023: "chsp-hospital-linkage-2023.csv",
    },
    "group_practice_linkage": {y: f"{y}-group-practice-linkage-public-file.csv" for y in (2016, 2018, 2020, 2021, 2022)},
    "nursing_home_linkage": {y: f"chsp-nh-linkage-{y}.csv" for y in (2020, 2021, 2022, 2023)},
    "home_health_linkage": {y: f"chsp-hhco-linkage-{y}.csv" for y in (2020, 2021, 2022, 2023)},
    "outpatient_site_linkage": {y: f"chsp-os-linkage-{y}.xlsx" for y in (2022, 2023)},
}
TABLES = tuple(PREFIX + t for t in FILES)


def _bytes(raw: Path, name: str) -> bytes:
    if "/" in name:
        stem, member = name.split("/", 1)
        with zipfile.ZipFile(raw / f"{stem}.zip") as z:
            return z.read(member)
    return (raw / name).read_bytes()


def _container(raw: Path, name: str) -> Path:
    """The file in raw/ that holds ``name``: itself, or the zip it is a member of."""
    return raw / (f"{name.split('/', 1)[0]}.zip" if "/" in name else name)


def normalized(data: bytes, xlsx: bool = False) -> tuple[list[str], list[list[str]]]:
    """The header (trimmed, lower case) and the rows of one file: rows with no value dropped, each padded
    or cut to the header's width."""
    if xlsx:
        import openpyxl

        ws = openpyxl.load_workbook(io.BytesIO(data), read_only=True).worksheets[0]
        it = ws.iter_rows(values_only=True)
        header = [str(h).strip().lower() for h in next(it)]
        body = [["" if v is None else str(v) for v in r] for r in it if r and r[0] is not None]
    else:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("latin-1")
        header, *body = list(csv.reader(io.StringIO(text.replace("\r\n", "\n").replace("\r", "\n"))))
        header = [h.strip().lower() for h in header]
        body = [r for r in body if any(r)]
    n = len(header)
    return header, [r[:n] + [""] * (n - len(r)) for r in body]


def require_files(source_dir: Path) -> None:
    raw = Path(source_dir) / "raw"
    missing = sorted({_container(raw, n).name for files in FILES.values() for n in files.values()
                      if not _container(raw, n).is_file()})
    if missing:
        raise UserError(f"the AHRQ directory {source_dir} lacks {len(missing)} file(s), e.g. raw/{missing[0]}; fetch them "
                        f"with `healthcare-ledger download --source-name ahrq --dest {source_dir}`", exit_code=2)


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def build(con: duckdb.DuckDBPyConnection, source_dir: Path, work: Path) -> dict[str, int]:
    """Create the ahrq_* tables in ``con`` from ``source_dir/raw``; returns their row counts."""
    raw = Path(source_dir) / "raw"
    counts = {}
    for table, years in FILES.items():
        parts = []
        for year, name in years.items():
            header, body = normalized(_bytes(raw, name), xlsx=name.endswith(".xlsx"))
            path = work / f"{table}_{year}.csv"
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(header)
                w.writerows(body)
            parts.append((year, name, header, path))
            log.info("ahrq %s %s: %s rows, %d columns", table, year, f"{len(body):,}", len(header))
        columns = list(dict.fromkeys(c for _, _, header, _ in parts for c in header))
        selects = []
        for year, name, header, path in parts:
            types = "{" + ", ".join(f"'{c.replace(chr(39), chr(39) * 2)}': 'VARCHAR'" for c in header) + "}"
            cols = ", ".join(_q(c) if c in header else f"NULL::VARCHAR AS {_q(c)}" for c in columns)
            selects.append(f"SELECT {year}::BIGINT AS year, '{name}' AS source_file, {cols} "
                           f"FROM read_csv('{path}', header = true, delim = ',', quote = '\"', escape = '\"', columns = {types})")
            missing = [c for c in columns if c not in header]
            if missing:
                log.info("ahrq %s %s lacks: %s", table, year, ", ".join(missing))
        con.execute(f"CREATE TABLE {PREFIX}{table} AS " + " UNION ALL ".join(selects))
        counts[PREFIX + table] = con.execute(f"SELECT count(*) FROM {PREFIX}{table}").fetchone()[0]
    return counts


def load(source_dir: Path, warehouse: Path, work: Path) -> dict[str, int]:
    """Rebuild ``<warehouse>/ahrq.duckdb`` from ``source_dir/raw``. The new file replaces the old one only once
    every table is built, so a failed load leaves the previous one alone."""
    require_files(source_dir)
    warehouse = Path(warehouse)
    warehouse.mkdir(parents=True, exist_ok=True)
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    target = source_file(warehouse, DATABASE)
    part = target.with_name(target.name + ".part")
    part.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(dir=work, prefix="ahrq-") as tmp:
        con = duckdb.connect(str(part))
        try:
            counts = build(con, source_dir, Path(tmp))
        except BaseException:
            con.close()
            part.unlink(missing_ok=True)
            raise
        con.close()
    os.replace(part, target)
    return counts
