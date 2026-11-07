"""Helpers that fabricate tiny NPPES zips so the SCD logic can be tested end to end."""

from __future__ import annotations

import csv
import io
import zipfile
from pathlib import Path


import pytest

from healthcare_ledger.sources.nppes.schema import ENDPOINT, OTHER_ID_HEADERS, OTHER_NAME, PRACTICE_LOCATION, PROVIDER


def catalog(warehouse: Path, *, read_only: bool = False):
    """A connection to a test warehouse directory on which every loaded source's tables resolve by name."""
    from healthcare_ledger.core.connect import open_catalog
    return open_catalog(warehouse, read_only=read_only, require=())


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


MEDICAID_HEADER = "BILLING_PROVIDER_NPI_NUM,SERVICING_PROVIDER_NPI_NUM,HCPCS_CODE,CLAIM_FROM_MONTH,TOTAL_PATIENTS,TOTAL_CLAIM_LINES,TOTAL_PAID"


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


# ------------------------------------------------------------------------------------------- ownership sources


