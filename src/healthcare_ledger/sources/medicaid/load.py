"""Load the HHS Medicaid spending extract into ``<warehouse>/medicaid.duckdb``, rebuilt from scratch.

``raw/medicaid-provider-spending.csv.zip`` holds one CSV (12 GB) with one row per billing NPI, servicing NPI,
HCPCS code and month. It is extracted into the work directory, read with explicit types into the table
``medicaid_spending`` with every column kept and renamed (``COLUMNS``), and the extracted CSV is deleted.
NPIs and codes are text; the month is the first of the month; an empty field is NULL.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import zipfile
from pathlib import Path

import duckdb

from ...core.connect import configure, source_file
from ...core.errors import UserError

log = logging.getLogger(__name__)

DATABASE = "medicaid"
TABLE = "medicaid_spending"
ZIP = "medicaid-provider-spending.csv.zip"
MEMBER = "medicaid-provider-spending.csv"

# column in the table -> (HHS header, type in the CSV)
COLUMNS: dict[str, tuple[str, str]] = {
    "billing_npi": ("BILLING_PROVIDER_NPI_NUM", "VARCHAR"),
    "servicing_npi": ("SERVICING_PROVIDER_NPI_NUM", "VARCHAR"),
    "hcpcs": ("HCPCS_CODE", "VARCHAR"),
    "month": ("CLAIM_FROM_MONTH", "VARCHAR"),          # YYYY-MM, stored as the DATE of the first of the month
    "patients": ("TOTAL_PATIENTS", "BIGINT"),
    "claim_lines": ("TOTAL_CLAIM_LINES", "BIGINT"),
    "paid": ("TOTAL_PAID", "DOUBLE"),
}


def require_files(source_dir: Path) -> Path:
    path = Path(source_dir) / "raw" / ZIP
    if not path.is_file():
        raise UserError(f"the Medicaid directory {source_dir} lacks raw/{ZIP}; fetch it with "
                        f"`healthcare-ledger download --source-name medicaid --dest {source_dir}`", exit_code=2)
    return path


def select_sql(csv_path: Path) -> str:
    """The SELECT that reads the extracted CSV into the table's columns."""
    types = ", ".join(f"'{h}': '{t}'" for h, t in COLUMNS.values())
    cols = ", ".join(f"strptime({h}, '%Y-%m')::DATE AS {c}" if c == "month" else f"{h} AS {c}"
                     for c, (h, _) in COLUMNS.items())
    return f"SELECT {cols} FROM read_csv('{csv_path}', header = true, types = {{{types}}})"


def build(con: duckdb.DuckDBPyConnection, zip_path: Path, work: Path) -> dict[str, int]:
    """Create ``medicaid_spending`` in ``con`` from the zip; returns its row count."""
    with zipfile.ZipFile(zip_path) as z:
        if MEMBER not in z.namelist():
            raise UserError(f"{zip_path} does not hold {MEMBER} (it has {', '.join(z.namelist())})", exit_code=2)
        csv_path = work / MEMBER
        log.info("medicaid: extracting %s (%s bytes)", MEMBER, f"{z.getinfo(MEMBER).file_size:,}")
        with z.open(MEMBER) as src, open(csv_path, "wb") as out:
            shutil.copyfileobj(src, out, 16 << 20)
    header = csv_path.open(encoding="utf-8").readline().rstrip("\r\n").split(",")
    want = [h for h, _ in COLUMNS.values()]
    if header != want:
        raise UserError(f"{MEMBER}: header {header} differs from {want}; HHS changed the file's layout", exit_code=2)
    con.execute(f"CREATE TABLE {TABLE} AS {select_sql(csv_path)}")
    csv_path.unlink()
    return {TABLE: con.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0]}


def load(source_dir: Path, warehouse: Path, work: Path) -> dict[str, int]:
    """Rebuild ``<warehouse>/medicaid.duckdb`` from ``source_dir/raw``. The new file replaces the old one only
    once the table is built, so a failed load leaves the previous one alone."""
    zip_path = require_files(source_dir)
    warehouse = Path(warehouse)
    warehouse.mkdir(parents=True, exist_ok=True)
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    target = source_file(warehouse, DATABASE)
    part = target.with_name(target.name + ".part")
    part.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(dir=work, prefix="medicaid-") as tmp:
        con = duckdb.connect(str(part))
        try:
            configure(con, temp_dir=Path(tmp))
            counts = build(con, zip_path, Path(tmp))
        except BaseException:
            con.close()
            part.unlink(missing_ok=True)
            raise
        con.close()
    os.replace(part, target)
    return counts
