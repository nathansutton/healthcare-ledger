"""Load the CHOW files into ``<warehouse>/chow.duckdb``, rebuilt from scratch.

``raw/`` holds ``hospital_chow.csv`` and ``snf_chow.csv`` (one row per transaction, buyer and seller side by side,
columns ``<field> - BUYER`` and ``<field> - SELLER``) and ``hospital_chow_owner.csv`` and ``snf_chow_owner.csv`` (the
owners of each party's enrollment, the All Owners layout). They become ``chow_event`` (facility_type, chow type,
effective date, then the buyer's and the seller's enrollment id, state, provider type, NPI, CCN, PAC id, name and
DBA name as ``buyer_*`` and ``seller_*``) and ``chow_owner`` (facility_type, enrollment_id, pac_id, org_name, then the
owner's PAC id, type, role, association date, names, address, share and type flags as ``owner_*``). Every column is
text but the dates and the ownership share.
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

DATABASE = "chow"
TABLES = ("chow_event", "chow_owner")
FILES = {"hospital": ("hospital_chow.csv", "hospital_chow_owner.csv"), "snf": ("snf_chow.csv", "snf_chow_owner.csv")}
PARTY = {"enrollment_id": "ENROLLMENT ID", "state": "ENROLLMENT STATE", "provider_type_code": "PROVIDER TYPE CODE",
         "provider_type": "PROVIDER TYPE TEXT", "npi": "NPI", "ccn": "CCN", "pac_id": "ASSOCIATE ID",
         "org_name": "ORGANIZATION NAME", "dba_name": "DOING BUSINESS AS NAME"}


def _ident(col: str) -> str:
    """``PERCENTAGE OWNERSHIP`` -> ``percentage_ownership``; ``LAST NAME - OWNER`` -> ``owner_last_name``."""
    c = col.strip()
    owner = c.upper().endswith(" - OWNER")
    if owner:
        c = c[: -len(" - OWNER")]
    c = re.sub(r"[^a-z0-9]+", "_", c.lower()).strip("_")
    return f"owner_{c}" if owner else c


def _csv(path: Path) -> str:
    """``read_csv`` of ``path`` with its encoding: CMS writes some of these files in Latin-1."""
    try:
        path.read_bytes().decode("utf-8")
        enc = "utf-8"
    except UnicodeDecodeError:
        enc = "latin-1"
    return f"read_csv('{path}', header = true, all_varchar = true, encoding = '{enc}')"


def build(con: duckdb.DuckDBPyConnection, source_dir: Path) -> dict[str, int]:
    raw = Path(source_dir) / "raw"
    for t, (ev, ow) in FILES.items():
        for f in (ev, ow):
            if not (raw / f).is_file():
                raise UserError(f"{raw} lacks {f}; fetch the files with `healthcare-ledger download --source-name chow "
                                f"--dest {source_dir}`", exit_code=2)
    parts = []
    for t, (ev, _) in FILES.items():
        header = con.execute(f"SELECT * FROM {_csv(raw / ev)} LIMIT 0").description
        have = {d[0] for d in header}
        for side in ("BUYER", "SELLER"):
            missing = [f"{c} - {side}" for c in PARTY.values() if f"{c} - {side}" not in have]
            if missing or "EFFECTIVE DATE" not in have:
                raise UserError(f"{ev}: lacks {missing or ['EFFECTIVE DATE']}; CMS changed the layout", exit_code=2)
        cols = ", ".join(f'"{src} - {side}" AS {side.lower()}_{dst}' for side in ("BUYER", "SELLER") for dst, src in PARTY.items())
        parts.append(f"""SELECT '{t}' AS facility_type, "CHOW TYPE CODE" AS chow_type_code, "CHOW TYPE TEXT" AS chow_type,
                TRY_STRPTIME("EFFECTIVE DATE", '%m/%d/%Y')::DATE AS effective_date, {cols}
            FROM {_csv(raw / ev)}""")
    con.execute("CREATE TABLE chow_event AS " + " UNION ALL ".join(parts))
    parts = []
    for t, (_, ow) in FILES.items():
        header = [d[0] for d in con.execute(f"SELECT * FROM {_csv(raw / ow)} LIMIT 0").description]
        cols = []
        for c in header:
            name = _ident(c)
            expr = f'"{c}"'
            if name == "percentage_ownership":
                expr, name = f'TRY_CAST("{c}" AS DOUBLE)', "ownership_pct"
            elif name == "owner_association_date":
                expr = f'TRY_STRPTIME("{c}", \'%m/%d/%Y\')::DATE'
            elif name == "associate_id":
                name = "pac_id"
            elif name == "owner_associate_id":
                name = "owner_pac_id"
            cols.append(f"{expr} AS {name}")
        parts.append(f"SELECT '{t}' AS facility_type, {', '.join(cols)} FROM {_csv(raw / ow)}")
    con.execute("CREATE TABLE chow_owner AS " + " UNION ALL BY NAME ".join(parts))
    return {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TABLES}


def load(source_dir: Path, warehouse: Path, work: Path) -> dict[str, int]:
    warehouse = Path(warehouse)
    warehouse.mkdir(parents=True, exist_ok=True)
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    target = source_file(warehouse, DATABASE)
    part = target.with_name(target.name + ".part")
    part.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(dir=work, prefix="chow-") as tmp:
        con = duckdb.connect(str(part))
        try:
            configure(con, temp_dir=Path(tmp))
            counts = build(con, source_dir)
        except BaseException:
            con.close()
            part.unlink(missing_ok=True)
            raise
        con.close()
    os.replace(part, target)
    return counts
