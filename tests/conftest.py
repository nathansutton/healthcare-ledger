"""Helpers that fabricate tiny NPPES zips so the SCD logic can be tested end to end."""

from __future__ import annotations

import csv
import importlib
import io
import json
import re
import zipfile
from pathlib import Path

import duckdb

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


def study_con(study: Path, export: Path | None = None, graph: Path | None = None):
    """An in-memory DuckDB that resolves a research notebook's data/ paths against its study directory, with the
    tables of an export and a graph as views when given, as the notebook opens them (``healthcare_ledger.connect``)."""
    import healthcare_ledger
    con = healthcare_ledger.connect(export, graph) if export or graph else duckdb.connect()
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


# ---------------------------------------------------------------- Doctors and Clinicians (tests/test_doctors_clinicians.py)
from healthcare_ledger.sources.doctors_clinicians.dac import HEADER_2014  # noqa: E402

# the 2015 header: the 2014 one plus the group's DBA name and two program flags
H2015 = [*HEADER_2014[:18], "Organization DBA name", *HEADER_2014[18:],
         "Received PQRS Maintenance of Certification Program Incentive", "Participated in Million Hearts"]
H2020 = ["NPI", "Ind_PAC_ID", "Ind_enrl_ID", "lst_nm", "frst_nm", "mid_nm", "suff", "gndr", "Cred", "Med_sch", "Grd_yr",
         "pri_spec", "sec_spec_1", "sec_spec_2", "sec_spec_3", "sec_spec_4", "sec_spec_all", "org_nm", "org_pac_id",
         "num_org_mem", "adr_ln_1", "adr_ln_2", "ln_2_sprs", "cty", "st", "zip", "phn_numbr",
         *(f"hosp_afl{x}_{i}" for i in range(1, 6) for x in ("", "_lbn")), "ind_assgn", "grp_assgn", "adrs_id"]
# canonical field -> header in each vintage, for building rows
FIELDS = {"npi": "NPI", "pac": "PAC ID", "enrl": "Professional Enrollment ID", "last": "Last Name", "first": "First Name",
          "cred": "Credential", "grad": "Graduation year", "spec": "Primary specialty", "org": "Organization legal name",
          "org_pac": "Group Practice PAC ID", "n": "Number of Group Practice members", "adr": "Line 1 Street Address",
          "city": "City", "st": "State", "zip": "Zip Code", "ccn1": "Claims based hospital affiliation CCN 1",
          "lbn1": "Claims based hospital affiliation LBN 1", "assign": "Professional accepts Medicare Assignment",
          "erx": "Participating in eRx"}
TO_2020 = {"npi": "NPI", "pac": "Ind_PAC_ID", "enrl": "Ind_enrl_ID", "last": "lst_nm", "first": "frst_nm", "cred": "Cred",
           "grad": "Grd_yr", "spec": "pri_spec", "org": "org_nm", "org_pac": "org_pac_id", "n": "num_org_mem",
           "adr": "adr_ln_1", "city": "cty", "st": "st", "zip": "zip", "ccn1": "hosp_afl_1", "lbn1": "hosp_afl_lbn_1",
           "assign": "ind_assgn", "phone": "phn_numbr"}


def _row(npi: str, pac: str, org_pac: str, org: str, **kw) -> dict:
    return {"npi": npi, "pac": pac, "enrl": "I200910050" + npi[-5:], "last": "SMITH", "first": "ANN", "cred": "MD",
            "grad": "1994", "spec": "INTERNAL MEDICINE", "org": org, "org_pac": org_pac, "n": "70", "adr": "1 MAIN ST",
            "city": "BOSTON", "st": "MA", "zip": "021142621", "ccn1": "220071", "lbn1": "GENERAL HOSPITAL",
            "assign": "Y", "erx": "N", **kw}


def _csv(header: list[str] | None, names: dict[str, str], rows: list[dict], blank: str = "") -> bytes:
    """``rows`` under ``header`` (canonical fields mapped by ``names``), or headerless in HEADER_2014 order with
    CMS's single-space blanks when ``header`` is None."""
    cols = list(header or HEADER_2014)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    if header is not None:
        w.writerow(cols)
    for r in rows:
        by_header = {names[k]: v for k, v in r.items() if k in names}
        w.writerow([by_header.get(h, blank) for h in cols])
    return buf.getvalue().encode("latin-1")


def _zip(path: Path, payload: bytes, done: bool = True, member: str = "National_Downloadable_File.csv") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(member, payload)
    if done:
        path.with_name(path.name + ".done").write_text("")


A, B, C, D = "1000000001", "1000000002", "1000000003", "1000000004"


@pytest.fixture
def dac_source(tmp_path: Path) -> Path:
    src = tmp_path / "dac"
    # March 2014, no header row: A in one group (its row repeated), a row without an NPI; B in two; C with an SSN in the group name, a digit in the first name and
    # a tax id for a credential
    _zip(src / "2014" / "doc_archive_03_2014.zip", _csv(None, FIELDS, [
        _row(A, "0042100026", "0113900655", "WESTERN HEALTH CORPORATION"),
        _row(A, "0042100026", "0113900655", "WESTERN HEALTH CORPORATION"),     # an exact repeat, loaded once
        _row(" ", "0244383578", "2860544749", "PODIATRIC PARTNERS"),            # no NPI: dropped
        _row(B, "2163334699", "7113900655", "NORTHSHORE ASSOCIATES", n="936"),
        _row(B, "2163334699", "8729138003", "LINDEN PHYSICIANS PC", n="43", zip="21201"),
        _row(C, "7517003643", "4789850256", "ACME 123-45-6789 LLC", first="J0HN", cred="123456789")], blank=" "))
    # April 2015 with a header: the same content, but A's PAC ids lost their leading zeros, the member count is written
    # 070 and the graduation year 1994.0, and the new columns (DBA name, two flags) are blank
    _zip(src / "2015" / "doc_archive_04_2015.zip", _csv(H2015, FIELDS, [
        _row(A, "42100026", "113900655", "WESTERN HEALTH CORPORATION", n="070", grad="1994.0"),
        _row(B, "2163334699", "8729138003", "LINDEN PHYSICIANS PC", n="43", zip="21201"),
        _row(B, "2163334699", "7113900655", "NORTHSHORE ASSOCIATES", n="936"),
        _row(C, "7517003643", "4789850256", "ACME 123-45-6789 LLC", first="J0HN", cred="123456789")]))
    # August 2020, short headers: A moves to another group, B is gone, D is new
    _zip(src / "2020" / "doctors_and_clinicians_archive_08_2020.zip", _csv(H2020, TO_2020, [
        _row(A, "0042100026", "9935112143", "EL CENTRO PHYSICIANS", phone="7605551234"),
        _row(C, "7517003643", "4789850256", "ACME 123-45-6789 LLC", first="J0HN", cred="123456789"),
        _row(D, "5890732267", "4789850256", "ACME 123-45-6789 LLC", ccn1="05A045")]))
    # not snapshots: a zip still downloading, a yearly bundle, a stray file
    _zip(src / "2020" / "doctors_and_clinicians_archive_11_2020.zip", b"NPI\n", done=False)
    _zip(src / "2016" / "doctors_and_clinicians_archive_2016.zip", b"NPI\n")
    (src / "2016" / "notes.txt").write_text("x")
    return src


# ---------------------------------------------------------------- facility Enrollments and All Owners (tests/test_ownership.py)
ENR = ["ENROLLMENT ID", "ENROLLMENT STATE", "PROVIDER TYPE CODE", "PROVIDER TYPE TEXT", "NPI", "MULTIPLE NPI FLAG", "CCN",
       "ASSOCIATE ID", "ORGANIZATION NAME", "DOING BUSINESS AS NAME", "INCORPORATION DATE", "INCORPORATION STATE",
       "ORGANIZATION TYPE STRUCTURE", "ORGANIZATION OTHER TYPE TEXT", "PROPRIETARY_NONPROFIT", "NURSING HOME PROVIDER NAME",
       "AFFILIATION ENTITY NAME", "AFFILIATION ENTITY ID", "ADDRESS LINE 1", "ADDRESS LINE 2", "CITY", "STATE", "ZIP CODE"]
OWN = ["ENROLLMENT ID", "ASSOCIATE ID", "ORGANIZATION NAME", "ASSOCIATE ID - OWNER", "TYPE - OWNER", "ROLE CODE - OWNER",
       "ROLE TEXT - OWNER", "ASSOCIATION DATE - OWNER", "FIRST NAME - OWNER", "MIDDLE NAME - OWNER", "LAST NAME - OWNER",
       "TITLE - OWNER", "ORGANIZATION NAME - OWNER", "DOING BUSINESS AS NAME - OWNER", "ADDRESS LINE 1 - OWNER",
       "ADDRESS LINE 2 - OWNER", "CITY - OWNER", "STATE - OWNER", "ZIP CODE - OWNER", "PERCENTAGE OWNERSHIP",
       "CREATED FOR ACQUISITION - OWNER", "CORPORATION - OWNER", "LLC - OWNER", "MEDICAL PROVIDER SUPPLIER - OWNER",
       "MANAGEMENT SERVICES COMPANY - OWNER", "MEDICAL STAFFING COMPANY - OWNER", "HOLDING COMPANY - OWNER",
       "INVESTMENT FIRM - OWNER", "FINANCIAL INSTITUTION - OWNER", "CONSULTING FIRM - OWNER", "FOR PROFIT - OWNER",
       "NON PROFIT - OWNER", "OTHER TYPE - OWNER", "OTHER TYPE TEXT - OWNER"]


def _write_owner_csv(path: Path, header: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, header, quoting=csv.QUOTE_ALL)
        w.writeheader()
        w.writerows(rows)


def _snf(eid: str, pac: str, ccn: str, name: str, city: str = "BOSTON") -> dict:
    return {"ENROLLMENT ID": eid, "ENROLLMENT STATE": "MA", "PROVIDER TYPE CODE": "00-18", "NPI": "1" + eid[-9:], "CCN": ccn,
            "ASSOCIATE ID": pac, "ORGANIZATION NAME": name, "INCORPORATION DATE": "01/02/2001", "CITY": city, "STATE": "MA",
            "ZIP CODE": "02138"}


def _owner(eid: str, pac: str, owner: str, kind: str = "O", pct: str = "100", **kw) -> dict:
    return {"ENROLLMENT ID": eid, "ASSOCIATE ID": pac, "ASSOCIATE ID - OWNER": owner, "TYPE - OWNER": kind,
            "ROLE CODE - OWNER": "34", "ROLE TEXT - OWNER": "5% OR GREATER DIRECT OWNERSHIP INTEREST",
            "ASSOCIATION DATE - OWNER": "03/04/2015", "PERCENTAGE OWNERSHIP": pct, **kw}


@pytest.fixture
def ownership_source(tmp_path: Path) -> Path:
    src = tmp_path / "ownership"
    e1, e2 = "O20000101000001", "O20000101000002"
    # release 1: two nursing homes, one owned by a chain (organization) and one by a person with an SSN in her name field
    _write_owner_csv(src / "2023-01-01" / "SNF_Enrollments.csv", ENR, [_snf(e1, "1111111111", "225001", "CHAIN NURSING ONE LLC"),
                                                             _snf(e2, "2222222222", "225002", "MAIN ST NURSING")])
    _write_owner_csv(src / "2023-01-01" / "SNF_All_Owners.csv", OWN, [
        _owner(e1, "1111111111", "9999999999", **{"ORGANIZATION NAME - OWNER": "CHAIN HOLDINGS INC 12-3456789",
                                                  "HOLDING COMPANY - OWNER": "Y"}),
        _owner(e2, "2222222222", "3333333333", "I", "60", **{"FIRST NAME - OWNER": "ANN", "LAST NAME - OWNER": "LEE"}),
        _owner(e2, "2222222222", "4444444444", "I", "40.00", **{"FIRST NAME - OWNER": "B0B", "LAST NAME - OWNER": "LEE"})])
    # release 2: only the enrollment file (an owners-less month); the second home changes city
    _write_owner_csv(src / "2023-02-01" / "SNF_Enrollments_2023.02.01.csv", ENR, [
        _snf(e1, "1111111111", "225001", "CHAIN NURSING ONE LLC"), _snf(e2, "2222222222", "225002", "MAIN ST NURSING", "CAMBRIDGE")])
    # release 3: the chain buys the second home (its owners change, and 40 and 40.00 are the same share), and the first
    # enrollment leaves the file
    _write_owner_csv(src / "2023-03-01" / "SNF_Enrollments_2023.03.01.csv", ENR, [
        _snf(e2, "2222222222", "225002", "MAIN ST NURSING", "CAMBRIDGE")])
    _write_owner_csv(src / "2023-03-01" / "SNF_All_Owners_2023.03.01.csv", OWN, [
        _owner(e2, "2222222222", "9999999999", **{"ORGANIZATION NAME - OWNER": "CHAIN HOLDINGS INC"})])
    return src


# ---------------------------------------------------------------- Medicare utilization
MUP_HEADER = ("Rndrng_NPI,Rndrng_Prvdr_Last_Org_Name,Rndrng_Prvdr_First_Name,Rndrng_Prvdr_MI,Rndrng_Prvdr_Crdntls,"
              "Rndrng_Prvdr_Ent_Cd,Rndrng_Prvdr_St1,Rndrng_Prvdr_St2,Rndrng_Prvdr_City,Rndrng_Prvdr_State_Abrvtn,"
              "Rndrng_Prvdr_State_FIPS,Rndrng_Prvdr_Zip5,Rndrng_Prvdr_RUCA,Rndrng_Prvdr_RUCA_Desc,Rndrng_Prvdr_Cntry,"
              "Rndrng_Prvdr_Type,Rndrng_Prvdr_Mdcr_Prtcptg_Ind,HCPCS_Cd,HCPCS_Desc,HCPCS_Drug_Ind,Place_Of_Srvc,Tot_Benes,"
              "Tot_Srvcs,Tot_Bene_Day_Srvcs,Avg_Sbmtd_Chrg,Avg_Mdcr_Alowd_Amt,Avg_Mdcr_Pymt_Amt,Avg_Mdcr_Stdzd_Amt")


def write_mup(directory: Path, rows: dict[int, list[str]] | None = None) -> Path:
    """One Medicare Physician & Other Practitioners by Provider and Service CSV per year in ``directory/raw``: by default
    one physician's office visits in 2019, billed partly in a facility and partly in an office."""
    rows = rows or {2019: [
        '1000000001,SMITH,ANN,,MD,I,1 MAIN ST,,BOSTON,MA,25,02138,1,"Metropolitan",US,Internal Medicine,Y,99213,'
        'Office visit,N,F,20,30,30,150,80,64,66',
        '1000000001,SMITH,ANN,,MD,I,1 MAIN ST,,BOSTON,MA,25,02138,1,"Metropolitan",US,Internal Medicine,Y,99213,'
        'Office visit,N,O,15,10,10,150,100,80,82']}
    raw = directory / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    for year, lines in rows.items():
        (raw / f"MUP_PHY_R25_P04_V20_D{year % 100:02d}_Prov_Svc.csv").write_text("\n".join([MUP_HEADER, *lines]) + "\n")
    return directory


# ------------------------------------------------------------------------------------------- ownership sources

def _parquet(path: Path, rows: list[dict]) -> None:
    """Write ``rows`` (every value text) as a parquet file, every column VARCHAR, as NCCS does."""
    con = duckdb.connect()
    cols = list(dict.fromkeys(k for r in rows for k in r))
    con.execute(f"CREATE TABLE t ({', '.join(f'{c} VARCHAR' for c in cols)})")
    con.executemany(f"INSERT INTO t VALUES ({', '.join('?' for _ in cols)})", [[r.get(c) for c in cols] for r in rows])
    con.execute(f"COPY t TO '{path}' (FORMAT parquet)")


def write_irs(directory: Path) -> Path:
    irs = importlib.import_module("healthcare_ledger.sources.irs.load")
    """Tax year 2022: hospital system S (501c3) files and lists its hospital H and foundation F as related exempt
    organizations, a taxable corporation C at 100% and a joint venture J at 40%; H and F file and list S back; the
    retirement fund R (501c3) lists S and the unrelated university U as related, and neither lists R; D is a
    disregarded entity of U with no EIN. U files a Schedule R (lists D), so R's listing of U is unreciprocated."""
    raw = Path(directory) / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    y = 2022
    header = [{"OBJECTID": o, "ORG_EIN": e, "RETURN_TYPE": "990", "TAX_PERIOD_BEGIN_DATE": "2022-01-01", "TAX_PERIOD_END_DATE": "2022-12-31",
               "F9_00_ORG_NAME_L1": n, "F9_00_ORG_NAME_DBA_L1": None, "F9_00_ORG_ADDR_L1": "1 WAY", "F9_00_ORG_ADDR_CITY": "BOSTON",
               "F9_00_ORG_ADDR_STATE": "MA", "F9_00_ORG_ADDR_ZIP": "021011234", "F9_00_GROUP_EXEMPT_NUM": None, "F9_00_ORG_WEBSITE": None,
               "F9_00_ORG_PHONE": None, "F9_00_YEAR_FORMATION": "1950", "F9_00_LEGAL_DMCL_STATE": "MA", "F9_00_ORG_EXEMPT_TYPE": t,
               "F9_00_RETURN_GROUP_X": None, "F9_00_RETURN_FINAL_X": None}
              for o, e, n, t in [("o1", "111111111", "SYSTEM S", "501c3"), ("o2", "222222222", "HOSPITAL H", "501c3"),
                                 ("o3", "333333333", "FOUNDATION F", "501c3"), ("o4", "444444444", "RETIREMENT FUND R", "501c3"),
                                 ("o5", "555555555", "UNIVERSITY U", "501c3")]]
    _parquet(raw / f"{irs.NCCS['header']}-{y}.parquet", header)
    def rel(p, o, e, rein, name, **kw):
        return {"OBJECTID": o, "ORG_EIN": e, f"{p}_EIN": rein, f"{p}_NAME_L1": name, f"{p}_ADDR_L1": None, f"{p}_ADDR_CITY": "BOSTON",
                f"{p}_ADDR_STATE": "MA", f"{p}_ADDR_ZIP": "02101", f"{p}_DMCL_STATE": "MA", f"{p}_PRIMARY_ACT": "HEALTH",
                f"{p}_CTRL_NAME_L1": None, **{f"{p}_{k}": v for k, v in kw.items()}}
    p = "SR_02_RLTD_ORG"
    _parquet(raw / f"{irs.NCCS['exempt']}-{y}.parquet", [
        rel(p, "o1", "111111111", "222222222", "HOSPITAL H", EXEMPT_CODE="501(C)(3)", CTRL_512_X="0"),
        rel(p, "o1", "111111111", "333333333", "FOUNDATION F", EXEMPT_CODE="501c3", CTRL_512_X="false"),
        rel(p, "o2", "222222222", "111111111", "SYSTEM S", EXEMPT_CODE="501(C)(3)", CTRL_512_X="0"),
        rel(p, "o3", "333333333", "111111111", "SYSTEM S", EXEMPT_CODE="501(C)(3)", CTRL_512_X="0"),
        rel(p, "o4", "444444444", "111111111", "SYSTEM S", EXEMPT_CODE="501(C)(3)", CTRL_512_X="0"),
        rel(p, "o4", "444444444", "555555555", "UNIVERSITY U", EXEMPT_CODE="501(C)(3)", CTRL_512_X="0"),
        rel(p, "o5", "555555555", "999999999", "ALUMNI VEBA", EXEMPT_CODE="501(C)(9)", CTRL_512_X="0")])
    p = "SR_01_DISREG_ENTITY"
    _parquet(raw / f"{irs.NCCS['disregarded']}-{y}.parquet", [rel(p, "o5", "555555555", None, "U PROPERTIES LLC")])
    p = "SR_03_RLTD_ORG_PTR"
    _parquet(raw / f"{irs.NCCS['partnership']}-{y}.parquet", [rel(p, "o1", "111111111", "666666666", "JOINT VENTURE J", PCT_OWN="0.40000")])
    p = "SR_04_RLTD_ORG_CORP"
    _parquet(raw / f"{irs.NCCS['corporation']}-{y}.parquet", [rel(p, "o1", "111111111", "777777777", "CORPORATION C", PCT_OWN="1.00000", CTRL_512_X="1", TYPE="C CORP")])
    _parquet(raw / f"{irs.NCCS['hospital']}-{y}.parquet", [{
        "OBJECTID": "o2", "ORG_EIN": "222222222", "SH_05_HOSPITAL_FACILITY_NUM": "1", "SH_05_HOSPITAL_NAME_L1": "HOSPITAL H",
        "SH_05_HOSPITAL_ADDR_L1": "2 WAY", "SH_05_HOSPITAL_ADDR_CITY": "BOSTON", "SH_05_HOSPITAL_ADDR_STATE": "MA", "SH_05_HOSPITAL_ADDR_ZIP": "02101",
        "SH_05_HOSPITAL_STATE_LIC_NUM": "L1", "SH_05_HOSPITAL_WEBSITE": None, "SH_05_HOSPITAL_LIC_X": "X", "SH_05_HOSPITAL_MEDICAL_X": "1",
        "SH_05_HOSPITAL_CHILDREN_X": None, "SH_05_HOSPITAL_TEACHING_X": "0", "SH_05_HOSPITAL_CRITICAL_ACCESS_X": None, "SH_05_HOSPITAL_ER_24HOURS_X": "true"}])
    for f in irs.BMF_FILES:
        with open(raw / f, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(irs.BMF_COLUMNS)
            if f == "eo1.csv":
                w.writerow(["111111111", "SYSTEM S", "", "1 WAY", "BOSTON", "MA", "02101", "0000", "03", "3", "1000", "195001", "1", "15", "", "1", "01", "202212", "9", "9", "01", "0", "12", "1", "1", "1", "E21", ""])
    return Path(directory)


def _hcris_zip(path: Path, prefix: str, year: int, rpt: list[list], alpha: list[list]) -> None:
    with zipfile.ZipFile(path, "w") as z:
        for kind, rows in (("rpt", rpt), ("alpha", alpha)):
            buf = io.StringIO()
            csv.writer(buf, lineterminator="\r\n").writerows(rows)
            z.writestr(f"{prefix}_{year}_{kind}.csv", buf.getvalue())


def write_hcris(directory: Path) -> Path:
    """Three hospital cost reports (one a chain member of HO1234, one not, one naming a chain without a number) and
    one nursing home report, fiscal 2022."""
    raw = Path(directory) / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    rpt = lambda n, ccn, b, e: [n, "2", ccn, "", "1", b, e, "06/01/2023", "N", "N", "A", "11001", "4", "05/21/2023", "F", "", "", "01/03/2023"]
    _hcris_zip(raw / "HOSP10FY2022.zip", "HOSP10", 2022,
               [rpt("1", "010001", "01/01/2022", "12/31/2022"), rpt("2", "010002", "07/01/2021", "06/30/2022"), rpt("3", "010003", "01/01/2022", "12/31/2022")],
               [["1", "S200001", "14000", "00100", "Y"], ["1", "S200001", "14000", "00200", "HO1234"], ["1", "S200001", "14100", "00100", "BIG CHAIN INC"],
                ["1", "S200001", "14200", "00100", "1 HQ WAY"], ["1", "S200001", "14300", "00100", "NASHVILLE"], ["1", "S200001", "14300", "00200", "tn"],
                ["1", "S200001", "14300", "00300", "372031234"],
                ["2", "S200001", "14000", "00100", "N"],
                ["3", "S200001", "14000", "00100", "Y"], ["3", "S200001", "14100", "00100", "NAME: SMALL CHAIN, LLC"],
                ["1", "A000000", "00100", "00100", "ignored"]])
    _hcris_zip(raw / "SNF10FY2022.zip", "SNF10", 2022, [rpt("9", "015001", "01/01/2022", "12/31/2022")],
               [["9", "S200001", "04300", "00100", "Y"], ["9", "S200001", "04400", "00100", "44H070"], ["9", "S200001", "04500", "00100", "NH CHAIN"]])
    return Path(directory)


def write_chow(directory: Path) -> Path:
    """One hospital and one SNF change of ownership, with two owners of the buyer, in Latin-1."""
    raw = Path(directory) / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    party = ["ENROLLMENT ID", "ENROLLMENT STATE", "PROVIDER TYPE CODE", "PROVIDER TYPE TEXT", "NPI", "MULTIPLE NPI FLAG", "CCN", "ASSOCIATE ID",
             "ORGANIZATION NAME", "DOING BUSINESS AS NAME"]
    head = [f"{c} - BUYER" for c in party] + ["CHOW TYPE CODE", "CHOW TYPE TEXT", "EFFECTIVE DATE"] + [f"{c} - SELLER" for c in party]
    buyer = ["O1", "TN", "00-09", "HOSPITAL", "1", "N", "440001", "B1", "BUYER INC", ""]
    seller = ["O0", "TN", "00-09", "HOSPITAL", "2", "N", "440001", "S1", "SELLER INC", ""]
    for t in ("hospital", "snf"):
        with open(raw / f"{t}_chow.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(head)
            w.writerow(buyer + ["CH", "CHANGE OF OWNERSHIP", "03/15/2019"] + seller)
        owner_head = ["ENROLLMENT ID", "ASSOCIATE ID", "ORGANIZATION NAME", "ASSOCIATE ID - OWNER", "TYPE - OWNER", "ROLE CODE - OWNER",
                      "ROLE TEXT - OWNER", "ASSOCIATION DATE - OWNER", "LAST NAME - OWNER", "ORGANIZATION NAME - OWNER", "PERCENTAGE OWNERSHIP",
                      "FOR PROFIT - OWNER"]
        (raw / f"{t}_chow_owner.csv").write_bytes(
            (",".join(owner_head) + "\r\nO1,B1,BUYER INC,P9,O,34,5% OR GREATER DIRECT OWNERSHIP INTEREST,01/01/2017,,PARENT CO,60.5,Y\r\n"
             "O1,B1,BUYER INC,P8,I,43,OPERATIONAL/MANAGERIAL CONTROL,01/01/2017,MU\xd1OZ,,,\r\n").encode("latin-1"))
    return Path(directory)


def write_nhc(directory: Path) -> Path:
    """Three months of Nursing Home Compare owners, one in each layout."""
    raw = Path(directory) / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    (raw / "ownership_2014_03.csv").write_text(
        '"PROVNUM","PROVNAME","ADDRESS","CITY","STATE","ZIP","ORG_NAME","FIRST_NAME","LAST_NAME","ROLE_DESC","filedate"\n'
        '"015009","BURNS NURSING HOME, INC.","701 MONROE","RUSSELLVILLE","AL","35653","","LARRY","DEARMAN","5% OR MORE OWNERSHIP INTEREST","2014-03-01"\n'
        '"015009","BURNS NURSING HOME, INC.","701 MONROE","RUSSELLVILLE","AL","35653","Burns Holdings LLC","","","5% OR MORE OWNERSHIP INTEREST","2014-03-01"\n')
    (raw / "ownership_2017_09.csv").write_bytes(
        ('"PROVNUM","PROVNAME","ADDRESS","CITY","STATE","ZIP","ROLE_DESC","OWNER_TYPE","OWNER_NAME","OWNER_PERCENTAGE","ASSOCIATION_DATE","filedate"\n'
         '"015009","BURNS NURSING HOME, INC.","701 MONROE","RUSSELLVILLE","AL","35653","5% OR GREATER DIRECT OWNERSHIP INTEREST","Individual","DEARMAN, LARRY","10%","since 09/01/1969","2017-09-01"\n'
         '"015009","BURNS NURSING HOME, INC.","701 MONROE","RUSSELLVILLE","AL","35653","OPERATIONAL/MANAGERIAL CONTROL","Organization","MU\xd1OZ CARE LLC","NO PERCENTAGE PROVIDED","since 01/02/2015","2017-09-01"\n').encode("cp1252"))
    (raw / "ownership_2021_12.csv").write_text(
        '"Federal Provider Number","Provider Name","Provider Address","Provider City","Provider State","Provider Zip Code","Role played by Owner or Manager in Facility","Owner Type","Owner Name","Ownership Percentage","Association Date","Location","Processing Date"\n'
        '"015009","BURNS NURSING HOME, INC.","701 MONROE","RUSSELLVILLE","AL","35653","5% OR GREATER INDIRECT OWNERSHIP INTEREST","Organization","BURNS HOLDINGS LLC","100%","since 03/01/2020","701 MONROE","2021-12-01"\n')
    return Path(directory)
