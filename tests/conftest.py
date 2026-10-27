"""Helpers that fabricate tiny NPPES zips so the SCD logic can be tested end to end."""

from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from pathlib import Path

import pytest

from healthcare_ledger.sources.nppes.schema import ENDPOINT, OTHER_ID_HEADERS, OTHER_NAME, PRACTICE_LOCATION, PROVIDER

_SQL = re.compile(r'con\.(execute|sql)\("""(.*?)"""\)', re.DOTALL)


def notebook_sql(notebook: Path, *, queries: bool = False) -> list[str]:
    """The triple-quoted SQL of every con.execute call in a research notebook's code cells, in order.

    With queries=True, also the con.sql calls, which a notebook uses for the result tables it displays. The group
    primitives (derived/sql/, written by graph-build) are not among them; ``sql_creating`` returns their SQL.
    """
    cells = json.loads(notebook.read_text())["cells"]
    return [m.group(2) for c in cells if c["cell_type"] == "code" for m in _SQL.finditer("".join(c["source"]))
            if queries or m.group(1) == "execute"]


def sql_creating(notebook: Path, table: str) -> str:
    """The SQL that creates the table: a group primitive's derived/sql file, which graph-build runs and a notebook
    reads as Parquet, or else the one con.execute block of the research notebook that creates it."""
    from healthcare_ledger.derived import primitives
    if table in {p.name for p in primitives.PRIMITIVES}:
        return f"CREATE TABLE {table} AS\n{primitives.sql(table)}"
    [block] = [b for b in notebook_sql(notebook) if f"CREATE TABLE {table} AS" in b]
    return block


def study_con(study: Path):
    """An in-memory DuckDB that resolves a research notebook's data/ paths against its study directory."""
    import duckdb
    con = duckdb.connect()
    con.execute("SET file_search_path = ?", [str(study)])
    return con


def catalog(warehouse: Path, *, read_only: bool = False):
    """A connection to a test warehouse directory on which every loaded source's tables resolve by name."""
    from healthcare_ledger.core.connect import open_catalog
    return open_catalog(warehouse, read_only=read_only, require=())


def study_rows(study: Path, name: str) -> list[dict[str, str]]:
    """The rows of one of a study's data/ CSVs."""
    with open(study / "data" / name, newline="") as f:
        return list(csv.DictReader(f))


# the 330 headers of the CMS main file, in its order: the other identifiers sit before "Is Sole Proprietor"
_split = next(i for i, c in enumerate(PROVIDER.columns) if c.name == "sole_proprietor")
MAIN_HEADERS = (["NPI"] + [c.cms_header for c in PROVIDER.columns[:_split]] + list(OTHER_ID_HEADERS)
                + [c.cms_header for c in PROVIDER.columns[_split:]])
# names for the other-identifier headers, which the loader drops: other_id_N, other_id_type_N, ...
OTHER_ID_NAMES = {f"{f}_{i}": OTHER_ID_HEADERS[4 * (i - 1) + j]
                  for i in range(1, 51) for j, f in enumerate(("other_id", "other_id_type", "other_id_state", "other_id_issuer"))}


def main_row(npi: str, **fields: str) -> dict[str, str]:
    """A main-file row keyed by canonical short name (or an ``OTHER_ID_NAMES`` name); everything else blank."""
    by_name = {c.name: c.cms_header for c in PROVIDER.columns} | OTHER_ID_NAMES
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


AHRQ_GP_HEADER = ("tin_name,pecos_pac_ids,health_sys_id,health_sys_name,tin_name_md_ppas,state_md_ppas,md_do_md_ppas,"
                  "np_pa_md_ppas,service_lines_md_ppas,ma_only_tin")
AHRQ_FACILITY = {"nursing_home_linkage": ("compendium_nh_id", "nursing_home_name"),
                 "home_health_linkage": ("compendium_hh_id", "home_health_care_org_name")}


def write_ahrq(directory: Path) -> Path:
    """Every file `load --source-name ahrq` reads, one tiny file each, in ``directory/raw`` with AHRQ's quirks:
    the 2016 and 2018 files inside their zips, the 2022 group practice file in Latin-1 with bare CR line endings,
    multiple associate ids joined by ^, practices AHRQ lists outside any system, a 2023 system column the
    other years lack, and the outpatient sites as .xlsx."""
    import openpyxl

    from healthcare_ledger.sources.ahrq.load import FILES
    raw = directory / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    zips: dict[str, dict[str, bytes]] = {}
    for table, files in FILES.items():
        for year, name in files.items():
            if table == "system":
                text = ("health_sys_id,health_sys_name,health_sys_city,health_sys_state,total_mds,prim_care_mds,grp_cnt,"
                        "hosp_cnt,acutehosp_cnt,sys_multistate,sys_beds" + (",hhco_cnt" if year == 2023 else "") + "\n"
                        "HSI1,General Health,Boston,MA,\"1,200\",300,4,1,1,0,400" + (",2" if year == 2023 else "") + "\n")
            elif table == "group_practice_linkage":
                # H (the hospital's group) is in the system; G and the two-id practice Y^Z are not
                text = (f"{AHRQ_GP_HEADER}\nGENERAL HOSPITAL,H,HSI1,General Health,GENERAL HOSPITAL,MA,30,2,900,0\n"
                        f"GROUP PRACTICE,G,,,GROUP PRACTICE,MA,2,0,40,0\n"
                        f"CL\xc9MENT PARTNERS,Y^Z,,,CL\xc9MENT PARTNERS,MA,3,0,50,0\n,,,,,,,,,\n")
            elif table == "hospital_linkage":
                text = ("compendium_hospital_id,ccn,hospital_name,hospital_street,hospital_city,hospital_state,hospital_zip,"
                        "acutehosp_flag,health_sys_id,health_sys_name,health_sys_city,health_sys_state\n"
                        "CHSP1,220001,General Hospital,100 Hospital Rd,Boston,MA,02114,1,HSI1,General Health,Boston,MA\n"
                        "CHSP2,221001,Twin A,7 River Rd,Springfield,MA,01001,1,,,,\n")
            elif table in AHRQ_FACILITY:
                row_id, name_col = AHRQ_FACILITY[table]
                text = (f"{row_id},onekey_id,ccn,{name_col},health_sys_id,health_sys_name\n"
                        f"{table[:2].upper()}1,1,225000,A FACILITY,,\n")
            else:
                wb = openpyxl.Workbook()
                ws = wb.active
                ws.append(["Compendium_OS_ID", "os_name", "os_mds", "health_sys_id"])
                ws.append(["OS1", "Clinic", 5, "HSI1"])
                ws.append(["OS2", "Lab", None, None])
                ws.append([None, "a note under the table", None, None])
                buf = io.BytesIO()
                wb.save(buf)
                (raw / name).write_bytes(buf.getvalue())
                continue
            data = text.replace("\n", "\r").encode("latin-1") if (year, table) == (2022, "group_practice_linkage") \
                else text.encode()
            if "/" in name:
                stem, member = name.split("/", 1)
                zips.setdefault(stem, {})[member] = data
            else:
                (raw / name).write_bytes(data)
    for stem, members in zips.items():
        with zipfile.ZipFile(raw / f"{stem}.zip", "w") as z:
            for member, data in members.items():
                z.writestr(member, data)
    return directory


MEDICAID_HEADER = "BILLING_PROVIDER_NPI_NUM,SERVICING_PROVIDER_NPI_NUM,HCPCS_CODE,CLAIM_FROM_MONTH,TOTAL_PATIENTS,TOTAL_CLAIM_LINES,TOTAL_PAID"


def medicaid_rows() -> list[str]:
    """20 rows in HHS's layout: one billing NPI left empty (as HHS does for some), HCPCS codes with leading
    zeros, months from 2018-01 to 2024-07."""
    rows = []
    for i in range(20):
        billing = "" if i == 0 else f"{1000000000 + i % 4}"
        rows.append(f"{billing},{2000000000 + i},{i:05d},{2018 + i % 7}-{1 + i % 12:02d},{12 + i},{20 + 2 * i},{100.25 * i}")
    return rows


def write_medicaid(directory: Path) -> Path:
    """The zip `load --source-name medicaid` reads, holding ``medicaid_rows``, in ``directory/raw``."""
    from healthcare_ledger.sources.medicaid.load import MEMBER, ZIP
    raw = directory / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(raw / ZIP, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(MEMBER, "\n".join([MEDICAID_HEADER, *medicaid_rows()]) + "\n")
    return directory
