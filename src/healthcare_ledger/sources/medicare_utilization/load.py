"""Load the Medicare Physician & Other Practitioners by Provider and Service files into
``<warehouse>/medicare_utilization.duckdb``, rebuilt from scratch.

``raw/`` holds one CSV per year (``MUP_PHY_R25_P04_V20_D15_Prov_Svc.csv`` is 2015: the year is the two digits after
``_D``), one row per rendering NPI, HCPCS code and place of service. They are read with explicit types into the table
``medicare_utilization_service`` with the year added. The rendering provider's name, credentials and street address are
not loaded (NPPES has them, versioned); the five-digit ZIP, state, RUCA code, provider type and entity code are. The
amounts are CMS's averages per service; ``allowed``, ``paid`` and ``standardized`` are those averages times
``services``, so they add up across codes and places.
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

DATABASE = "medicare_utilization"
TABLE = "medicare_utilization_service"
FILE_RE = re.compile(r"_D(\d{2})_Prov_Svc\.csv$", re.IGNORECASE)

# column in the table -> SQL over the CSV's columns (read as text)
COLUMNS: dict[str, str] = {
    "npi": "Rndrng_NPI",
    "entity_code": "Rndrng_Prvdr_Ent_Cd",
    "provider_type": "Rndrng_Prvdr_Type",
    "state": "Rndrng_Prvdr_State_Abrvtn",
    "zip5": "Rndrng_Prvdr_Zip5",
    "ruca": "Rndrng_Prvdr_RUCA",
    "medicare_participating": "Rndrng_Prvdr_Mdcr_Prtcptg_Ind",
    "hcpcs": "HCPCS_Cd",
    "hcpcs_drug": "HCPCS_Drug_Ind",
    "place_of_service": "Place_Of_Srvc",
    "beneficiaries": "TRY_CAST(Tot_Benes AS BIGINT)",
    "services": "TRY_CAST(Tot_Srvcs AS DOUBLE)",
    "beneficiary_day_services": "TRY_CAST(Tot_Bene_Day_Srvcs AS DOUBLE)",
    "submitted": "TRY_CAST(Avg_Sbmtd_Chrg AS DOUBLE) * TRY_CAST(Tot_Srvcs AS DOUBLE)",
    "allowed": "TRY_CAST(Avg_Mdcr_Alowd_Amt AS DOUBLE) * TRY_CAST(Tot_Srvcs AS DOUBLE)",
    "paid": "TRY_CAST(Avg_Mdcr_Pymt_Amt AS DOUBLE) * TRY_CAST(Tot_Srvcs AS DOUBLE)",
    "standardized": "TRY_CAST(Avg_Mdcr_Stdzd_Amt AS DOUBLE) * TRY_CAST(Tot_Srvcs AS DOUBLE)",
}
HEADERS = ("Rndrng_NPI", "Rndrng_Prvdr_Ent_Cd", "Rndrng_Prvdr_Type", "Rndrng_Prvdr_State_Abrvtn", "Rndrng_Prvdr_Zip5",
           "Rndrng_Prvdr_RUCA", "Rndrng_Prvdr_Mdcr_Prtcptg_Ind", "HCPCS_Cd", "HCPCS_Drug_Ind", "Place_Of_Srvc", "Tot_Benes",
           "Tot_Srvcs", "Tot_Bene_Day_Srvcs", "Avg_Sbmtd_Chrg", "Avg_Mdcr_Alowd_Amt", "Avg_Mdcr_Pymt_Amt", "Avg_Mdcr_Stdzd_Amt")


def year_files(source_dir: Path) -> dict[int, Path]:
    raw = Path(source_dir) / "raw"
    files = {}
    for f in sorted(raw.glob("*.csv")) if raw.is_dir() else []:
        m = FILE_RE.search(f.name)
        if m:
            year = 2000 + int(m.group(1))
            if year in files:
                raise UserError(f"two files for {year} in {raw}: {files[year].name} and {f.name}", exit_code=2)
            files[year] = f
    if not files:
        raise UserError(f"the Medicare utilization directory {source_dir} has no raw/*_D<yy>_Prov_Svc.csv; fetch them with "
                        f"`healthcare-ledger download --source-name medicare_utilization --dest {source_dir}`", exit_code=2)
    return files


def select_sql(path: Path, year: int) -> str:
    return (f"SELECT {year} AS year, " + ", ".join(f"{expr} AS {c}" for c, expr in COLUMNS.items())
            + f" FROM read_csv('{path}', header = true, all_varchar = true, quote = '\"', escape = '\"')")


def build(con: duckdb.DuckDBPyConnection, files: dict[int, Path]) -> dict[str, int]:
    """Create ``medicare_utilization_service`` in ``con`` from the year files; returns its row count."""
    created = False
    for year, path in sorted(files.items()):
        header = path.open(encoding="utf-8-sig").readline().rstrip("\r\n").split(",")
        missing = [h for h in HEADERS if h not in header]
        if missing:
            raise UserError(f"{path.name}: lacks {missing}; CMS changed the file's layout", exit_code=2)
        log.info("medicare utilization: %s (%s)", year, path.name)
        con.execute(f"{'INSERT INTO' if created else 'CREATE TABLE'} {TABLE} {'' if created else 'AS'} {select_sql(path, year)}")
        created = True
    return {TABLE: con.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0]}


def load(source_dir: Path, warehouse: Path, work: Path) -> dict[str, int]:
    """Rebuild ``<warehouse>/medicare_utilization.duckdb`` from ``source_dir/raw``; the new file replaces the old one only
    once the table is built."""
    files = year_files(source_dir)
    warehouse = Path(warehouse)
    warehouse.mkdir(parents=True, exist_ok=True)
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    target = source_file(warehouse, DATABASE)
    part = target.with_name(target.name + ".part")
    part.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(dir=work, prefix="mup-") as tmp:
        con = duckdb.connect(str(part))
        try:
            configure(con, temp_dir=Path(tmp))
            counts = build(con, files)
        except BaseException:
            con.close()
            part.unlink(missing_ok=True)
            raise
        con.close()
    os.replace(part, target)
    return counts
