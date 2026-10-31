"""Load the Nursing Home Compare ownership files into ``<warehouse>/nhc.duckdb``, rebuilt from scratch.

``raw/ownership_<yyyy>_<mm>.csv`` is one month's ``Ownership_Download.csv``, one row per nursing home (CCN, name,
address) x role x owner. Three layouts (``LAYOUTS``): to mid-2016 the owner is ``ORG_NAME`` or
``FIRST_NAME``/``LAST_NAME`` and the role ``ROLE_DESC``; from then ``OWNER_TYPE`` (Individual/Organization),
``OWNER_NAME``, ``OWNER_PERCENTAGE`` ("10%", "NO PERCENTAGE PROVIDED") and ``ASSOCIATION_DATE`` ("since 09/01/1969");
from August 2020 the same fields under spelled-out names (``Federal Provider Number``, ``Owner Name``). All become ``nhc_ownership``: as_of_date (the
file's month), ccn, provider_name, address, city, state, zip5, role, owner_type, owner_name (``LAST, FIRST`` for a
person), ownership_pct (a number or NULL), association_date (a date or NULL) and source_file. Names are kept as
CMS wrote them, upper case.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from pathlib import Path

import duckdb

from ...core.connect import configure, source_file
from ...core.errors import UserError

log = logging.getLogger(__name__)

DATABASE = "nhc"
TABLES = ("nhc_ownership",)
FILE_RE = re.compile(r"^ownership_(\d{4})_(\d{2})\.csv$")


def month_files(source_dir: Path) -> dict[str, Path]:
    raw = Path(source_dir) / "raw"
    files = {}
    for f in sorted(raw.glob("ownership_*.csv")) if raw.is_dir() else []:
        m = FILE_RE.match(f.name)
        if m:
            files[f"{m.group(1)}-{m.group(2)}-01"] = f
    if not files:
        raise UserError(f"the Nursing Home Compare directory {source_dir} has no raw/ownership_<yyyy>_<mm>.csv; fetch them with "
                        f"`healthcare-ledger download --source-name nhc --dest {source_dir}`", exit_code=2)
    return files


def utf8_copy(path: Path, tmp: Path) -> Path:
    """A UTF-8 copy of ``path`` in ``tmp``: CMS wrote most months in Windows-1252 (a name with an accent or a curly
    quote breaks a UTF-8 read), and a byte that is neither becomes U+FFFD."""
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")
    out = tmp / path.name
    out.write_text(text, encoding="utf-8")
    return out


# the three layouts' column names, by ours
LAYOUTS = (
    {"ccn": "PROVNUM", "provider_name": "PROVNAME", "address": "ADDRESS", "city": "CITY", "state": "STATE", "zip": "ZIP",
     "role": "ROLE_DESC", "owner_type": "OWNER_TYPE", "owner_name": "OWNER_NAME", "pct": "OWNER_PERCENTAGE", "date": "ASSOCIATION_DATE"},
    {"ccn": "Federal Provider Number", "provider_name": "Provider Name", "address": "Provider Address", "city": "Provider City",
     "state": "Provider State", "zip": "Provider Zip Code", "role": "Role played by Owner or Manager in Facility", "owner_type": "Owner Type",
     "owner_name": "Owner Name", "pct": "Ownership Percentage", "date": "Association Date"},
    {"ccn": "PROVNUM", "provider_name": "PROVNAME", "address": "ADDRESS", "city": "CITY", "state": "STATE", "zip": "ZIP",
     "role": "ROLE_DESC", "org_name": "ORG_NAME", "first_name": "FIRST_NAME", "last_name": "LAST_NAME"},
)


def select_sql(path: Path, d: str, header: list[str]) -> str:
    src = f"read_csv('{path}', header = true, all_varchar = true, quote = '\"', escape = '\"')"
    layout = next((m for m in LAYOUTS if all(c in header for c in m.values())), None)
    if layout is None:
        raise UserError(f"{path.name}: none of the known layouts ({header[:12]})", exit_code=2)
    c = {k: f'"{v}"' for k, v in layout.items()}
    common = f"""DATE '{d}' AS as_of_date, {c['ccn']} AS ccn, upper({c['provider_name']}) AS provider_name, upper({c['address']}) AS address,
                 upper({c['city']}) AS city, upper({c['state']}) AS state, left({c['zip']}, 5) AS zip5, upper({c['role']}) AS role"""
    if "owner_name" in c:
        return f"""SELECT {common}, {c['owner_type']} AS owner_type, upper({c['owner_name']}) AS owner_name,
                TRY_CAST(regexp_extract({c['pct']}, '([0-9.]+)%', 1) AS DOUBLE) AS ownership_pct,
                TRY_STRPTIME(regexp_extract({c['date']}, '([0-9]{{2}}/[0-9]{{2}}/[0-9]{{4}})', 1), '%m/%d/%Y')::DATE AS association_date,
                '{path.name}' AS source_file FROM {src}"""
    return f"""SELECT {common}, CASE WHEN nullif({c['org_name']}, '') IS NOT NULL THEN 'Organization' ELSE 'Individual' END AS owner_type,
            upper(coalesce(nullif({c['org_name']}, ''), {c['last_name']} || ', ' || {c['first_name']})) AS owner_name,
            NULL::DOUBLE AS ownership_pct, NULL::DATE AS association_date, '{path.name}' AS source_file FROM {src}"""


def build(con: duckdb.DuckDBPyConnection, files: dict[str, Path], tmp: Path) -> dict[str, int]:
    created = False
    for d, path in sorted(files.items()):
        copy = utf8_copy(path, tmp)
        header = copy.open(encoding="utf-8-sig").readline().rstrip("\r\n").replace('"', "").split(",")
        log.info("nhc: %s (%s)", d, path.name)
        con.execute(f"{'INSERT INTO' if created else 'CREATE TABLE'} nhc_ownership {'' if created else 'AS'} {select_sql(copy, d, header)}")
        copy.unlink()
        created = True
    return {"nhc_ownership": con.execute("SELECT count(*) FROM nhc_ownership").fetchone()[0]}


def load(source_dir: Path, warehouse: Path, work: Path) -> dict[str, int]:
    files = month_files(source_dir)
    warehouse = Path(warehouse)
    warehouse.mkdir(parents=True, exist_ok=True)
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    target = source_file(warehouse, DATABASE)
    part = target.with_name(target.name + ".part")
    part.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(dir=work, prefix="nhc-") as tmp:
        con = duckdb.connect(str(part))
        try:
            configure(con, temp_dir=Path(tmp))
            counts = build(con, files, Path(tmp))
        except BaseException:
            con.close()
            part.unlink(missing_ok=True)
            raise
        con.close()
    os.replace(part, target)
    return counts
