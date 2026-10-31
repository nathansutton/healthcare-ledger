"""Load the HCRIS cost-report files into ``<warehouse>/hcris.duckdb``, rebuilt from scratch.

``raw/`` holds one zip per form and fiscal year (``HOSP10FY2022.zip``: hospitals, form 2552-10, fiscal 2022; ``SNF10``
nursing homes, 2540-10; ``HHA20`` home health, 1728-20; ``HOSPC14-ALL-YEARS.zip`` hospices, 1984-14, every year in one
zip), each with a report file (``*_rpt.csv``, no header: record number, control type, CCN, NPI, status, fiscal period
begin and end, process date, ...) and an alphanumeric file (``*_alpha.csv``: record number, worksheet, line, column,
value; lines and columns five digits, zero-padded). The tables:

``hcris_report``
    one row per cost report: form, fiscal year (the file's), record number, CCN, NPI, control type code, status code,
    fiscal period begin and end, process date.
``hcris_home_office``
    one row per cost report with the chain question answered (worksheet S-2 part I, S-1 for hospices; ``LINES``):
    whether the provider is a chain member, the home office number as filed and normalized (``home_office_id``:
    upper case, no punctuation, the letter O read as zero among digits, since the number is typed by hand), the
    home office's name, address and Medicare contractor. A home office number is PECOS's id of a chain home
    office, shared across facility types; a filer with a name but no number gets ``NAME:`` + the normalized name.

A fiscal year's file holds the reports whose fiscal period ends in it, each with its own dates; a facility's chain
membership on a date is the report whose period covers it.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import zipfile
from pathlib import Path

import duckdb

from ...core.connect import configure, source_file
from ...core.errors import UserError

log = logging.getLogger(__name__)

DATABASE = "hcris"
TABLES = ("hcris_report", "hcris_home_office")
# form -> (zip name prefix, worksheet, {field: (line, column)})
FORMS: dict[str, tuple[str, str, dict[str, tuple[str, str]]]] = {
    "hospital": ("HOSP10FY", "S200001", {
        "chain": ("14000", "00100"), "number": ("14000", "00200"), "name": ("14100", "00100"),
        "contractor_name": ("14100", "00200"), "contractor_number": ("14100", "00300"), "address": ("14200", "00100"),
        "city": ("14300", "00100"), "state": ("14300", "00200"), "zip": ("14300", "00300")}),
    "snf": ("SNF10FY", "S200001", {
        "chain": ("04300", "00100"), "number": ("04400", "00100"), "name": ("04500", "00100"),
        "contractor_name": ("04500", "00200"), "contractor_number": ("04500", "00300"), "address": ("04600", "00100"),
        "city": ("04700", "00100"), "state": ("04700", "00200"), "zip": ("04700", "00300")}),
    "hha": ("HHA20FY", "S200001", {
        "chain": ("01600", "00100"), "name": ("01700", "00100"), "number": ("01700", "00200"),
        "contractor_number": ("01700", "00300"), "address": ("01700", "00400"), "city": ("01700", "00500"),
        "state": ("01700", "00600"), "zip": ("01700", "00700")}),
    "hospice": ("HOSPC14", "S100000", {
        "chain": ("01300", "00100"), "number": ("01300", "00200"), "name": ("01400", "00100"), "address": ("01500", "00100"),
        "city": ("01600", "00100"), "state": ("01600", "00200"), "zip": ("01600", "00300"),
        "contractor_name": ("01700", "00100"), "contractor_number": ("01800", "00100")}),
}
FIELDS = ("chain", "number", "name", "contractor_name", "contractor_number", "address", "city", "state", "zip")
MEMBER_RE = re.compile(r"^(HOSP10|SNF10|HHA20|HOSPC14)_(\d{4})_(rpt|alpha)\.csv$", re.IGNORECASE)
RPT_NAMES = ("rpt_rec_num", "control_type", "ccn", "npi", "status", "fy_begin", "fy_end", "process_date", "initial", "last",
             "transmittal", "contractor", "vendor", "contractor_created", "utilization", "npr_date", "special", "received")
ALPHA_NAMES = ("rpt_rec_num", "worksheet", "line", "col", "value")


def zips(source_dir: Path) -> list[tuple[str, Path]]:
    raw = Path(source_dir) / "raw"
    out = []
    for form, (prefix, _, _) in FORMS.items():
        for f in sorted(raw.glob("*.zip")) if raw.is_dir() else []:
            if f.name.upper().startswith(prefix):
                out.append((form, f))
    if not out:
        raise UserError(f"the HCRIS directory {source_dir} has no raw/<form>FY<year>.zip; fetch them with "
                        f"`healthcare-ledger download --source-name hcris --dest {source_dir}`", exit_code=2)
    return out


def normalize_id_sql(c: str) -> str:
    """The home office number ``c`` normalized: letters upper case, nothing but letters and digits, and the letter O
    read as zero after the letter prefix (H, HB, HF, HO or two digits and H) when the rest is then all digits."""
    v = f"upper(regexp_replace(coalesce({c}, ''), '[^A-Za-z0-9]', '', 'g'))"
    prefix = f"regexp_extract({v}, '^(H[BF]?|[0-9]{{2}}H)', 1)"
    rest = f"replace(substr({v}, length({prefix}) + 1), 'O', '0')"
    return f"CASE WHEN {v} = '' THEN NULL WHEN {prefix} <> '' AND regexp_matches({rest}, '^[0-9]+$') THEN {prefix} || {rest} ELSE {v} END"


def build(con: duckdb.DuckDBPyConnection, source_dir: Path, tmp: Path) -> dict[str, int]:
    con.execute("""CREATE TABLE hcris_report (form VARCHAR, fiscal_year INTEGER, rpt_rec_num VARCHAR, ccn VARCHAR, npi VARCHAR,
                   control_type VARCHAR, status VARCHAR, fy_begin DATE, fy_end DATE, process_date DATE, source_file VARCHAR)""")
    con.execute("""CREATE TABLE hcris_home_office (form VARCHAR, fiscal_year INTEGER, rpt_rec_num VARCHAR, ccn VARCHAR,
                   fy_begin DATE, fy_end DATE, chain BOOLEAN, home_office_number VARCHAR, home_office_id VARCHAR,
                   home_office_name VARCHAR, contractor_name VARCHAR, contractor_number VARCHAR, address VARCHAR,
                   city VARCHAR, state VARCHAR, zip5 VARCHAR)""")
    for form, path in zips(source_dir):
        _, ws, lines = FORMS[form]
        with zipfile.ZipFile(path) as z:
            members: dict[int, dict[str, str]] = {}
            for n in z.namelist():
                m = MEMBER_RE.match(Path(n).name)
                if m:
                    members.setdefault(int(m.group(2)), {})[m.group(3).lower()] = n
            if not members:
                raise UserError(f"{path.name}: no <form>_<year>_rpt.csv and _alpha.csv members", exit_code=2)
            for year, files in sorted(members.items()):
                if "rpt" not in files or "alpha" not in files:
                    raise UserError(f"{path.name}: fiscal {year} lacks its rpt or alpha member", exit_code=2)
                rpt, alpha = tmp / "rpt.csv", tmp / "alpha.csv"
                rpt.write_bytes(z.read(files["rpt"]))
                alpha.write_bytes(z.read(files["alpha"]))
                log.info("hcris: %s fiscal %s (%s)", form, year, path.name)
                con.execute(f"""INSERT INTO hcris_report
                    SELECT '{form}', {year}, rpt_rec_num, ccn, nullif(npi, ''), control_type, status,
                           TRY_STRPTIME(fy_begin, '%m/%d/%Y')::DATE, TRY_STRPTIME(fy_end, '%m/%d/%Y')::DATE,
                           TRY_STRPTIME(process_date, '%m/%d/%Y')::DATE, '{path.name}'
                    FROM read_csv('{rpt}', header = false, all_varchar = true, names = {list(RPT_NAMES)})""")
                cells = " ".join(f"WHEN line = '{ln}' AND col = '{col}' THEN '{f}'" for f, (ln, col) in lines.items())
                con.execute(f"""CREATE OR REPLACE TEMP TABLE _cells AS
                    SELECT rpt_rec_num, CASE {cells} END AS field, value
                    FROM read_csv('{alpha}', header = false, all_varchar = true, names = {list(ALPHA_NAMES)})
                    WHERE worksheet = '{ws}' AND CASE {cells} END IS NOT NULL""")
                con.execute(f"""CREATE OR REPLACE TEMP TABLE _wide AS
                    PIVOT (SELECT rpt_rec_num, field, any_value(value) AS value FROM _cells GROUP BY 1, 2)
                    ON field IN ({", ".join(repr(f) for f in FIELDS)}) USING any_value(value)""")
                con.execute(f"""INSERT INTO hcris_home_office
                    SELECT r.form, r.fiscal_year, r.rpt_rec_num, r.ccn, r.fy_begin, r.fy_end,
                           upper(trim(w.chain)) = 'Y' AS chain, nullif(trim(w.number), '') AS home_office_number,
                           coalesce({normalize_id_sql('w.number')},
                                    CASE WHEN trim(coalesce(w.name, '')) <> '' THEN 'NAME:' || upper(regexp_replace(w.name, '[^A-Za-z0-9]', '', 'g')) END) AS home_office_id,
                           nullif(trim(w.name), ''), nullif(trim(w.contractor_name), ''), nullif(trim(w.contractor_number), ''),
                           nullif(trim(w.address), ''), nullif(trim(w.city), ''), nullif(upper(trim(w.state)), ''), left(nullif(trim(w.zip), ''), 5)
                    FROM _wide w JOIN hcris_report r ON r.rpt_rec_num = w.rpt_rec_num AND r.form = '{form}' AND r.fiscal_year = {year}
                    WHERE w.chain IS NOT NULL OR w.number IS NOT NULL OR w.name IS NOT NULL""")
    con.execute("DROP TABLE IF EXISTS _cells; DROP TABLE IF EXISTS _wide")
    return {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TABLES}


def load(source_dir: Path, warehouse: Path, work: Path) -> dict[str, int]:
    warehouse = Path(warehouse)
    warehouse.mkdir(parents=True, exist_ok=True)
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    target = source_file(warehouse, DATABASE)
    part = target.with_name(target.name + ".part")
    part.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(dir=work, prefix="hcris-") as tmp:
        con = duckdb.connect(str(part))
        try:
            configure(con, temp_dir=Path(tmp))
            counts = build(con, source_dir, Path(tmp))
        except BaseException:
            con.close()
            part.unlink(missing_ok=True)
            raise
        con.close()
    os.replace(part, target)
    return counts
