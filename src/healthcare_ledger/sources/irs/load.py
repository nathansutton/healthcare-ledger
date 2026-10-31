"""Load the Form 990 e-file tables and the Business Master File into ``<warehouse>/irs.duckdb``, rebuilt from scratch.

``raw/`` holds, per tax year, the Nonprofit Open Data Collective's parquet tables of the IRS e-file XML (its efile
v3_1 release: one row per return in the header table, one per listed entity in the schedule tables, column names
from its concordance), and the four regional CSVs of the IRS's EO BMF. They become:

``irs_return``
    one row per Form 990 return (``object_id``): the filer's EIN, name, address, group exemption number, website,
    tax year and period, and return type (990 or 990-EZ; 990-PF and 990-T are not in the e-file tables used).
``irs_related``
    one row per entity a return's Schedule R lists: the filer's EIN, the entity's ``kind`` (disregarded, exempt,
    partnership or corporation, Schedule R Parts I to IV), its EIN when given (taxable entities often omit it), name,
    address, legal domicile, primary activity, exempt code, the filer's ownership share and whether the filer
    controls it under section 512(b)(13), and ``controlled``: whether the filer's relation is one of control: every
    disregarded entity, every related exempt organization (Schedule R's related organizations are parents,
    subsidiaries, brother-sister organizations and supporting organizations: one family under the Compendium's
    definition), and a taxable entity the filer owns more than half of or controls.
``irs_hospital_facility``
    one row per hospital facility a return's Schedule H Part V lists: name, address, state license number, the
    subordinate EIN when the facility is a separate filer, and its type flags.
``irs_family``
    per tax year, every filer and every entity on a family edge with its family: the connected component of the
    family edges (``families``: a controlled relation from a charity-type filer to a charity-type or taxable entity,
    reciprocated when the entity files a Schedule R itself), labelled by its smallest EIN. A family is what the
    Compendium builds a system from: every nonprofit a system's returns relate.
``irs_bmf``
    the EO BMF: every exempt organization with its name, address, group exemption number, subsection, affiliation
    code, NTEE code and filing requirement, as of the extract's date.

EINs are kept as nine digits; a related entity's EIN that is not nine digits, all zeros or the filer's own is NULL.
Ownership shares are read as a percentage (a share written as a fraction is multiplied by 100).
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

DATABASE = "irs"
PREFIX = "irs_"
TABLES = ("irs_return", "irs_related", "irs_hospital_facility", "irs_family", "irs_bmf")
YEARS = tuple(range(2013, 2026))
# the efile table each of ours reads, per tax year
NCCS = {
    "header": "F9-P00-T00-HEADER",
    "disregarded": "SR-P01-T01-ID-DISREGARDED-ENTITIES",
    "exempt": "SR-P02-T01-ID-RLTD-TAX-EXEMPED-ORGS",
    "partnership": "SR-P03-T01-ID-RLTD-ORGS-TAXABLE-PARTNERSHIP",
    "corporation": "SR-P04-T01-ID-RLTD-ORGS-TAXABLE-CORPORATION",
    "hospital": "SH-P05-T01-HOSPITAL-FACILITY",
}
BMF_FILES = ("eo1.csv", "eo2.csv", "eo3.csv", "eo4.csv")
BMF_COLUMNS = ("EIN", "NAME", "ICO", "STREET", "CITY", "STATE", "ZIP", "GROUP", "SUBSECTION", "AFFILIATION", "CLASSIFICATION",
               "RULING", "DEDUCTIBILITY", "FOUNDATION", "ACTIVITY", "ORGANIZATION", "STATUS", "TAX_PERIOD", "ASSET_CD",
               "INCOME_CD", "FILING_REQ_CD", "PF_FILING_REQ_CD", "ACCT_PD", "ASSET_AMT", "INCOME_AMT", "REVENUE_AMT",
               "NTEE_CD", "SORT_NAME")

EIN = "CASE WHEN regexp_matches({c}, '^[0-9]{{9}}$') AND {c} <> '000000000' THEN {c} END"
FLAG = "coalesce(lower({c}) IN ('x', '1', 'true'), FALSE)"       # a checkbox: the XML's X, or NCCS's 1/true
# Schedule R column prefixes by part
_SR = {"disregarded": "SR_01_DISREG_ENTITY", "exempt": "SR_02_RLTD_ORG", "partnership": "SR_03_RLTD_ORG_PTR",
       "corporation": "SR_04_RLTD_ORG_CORP"}


def _pct(c: str) -> str:
    return (f"CASE WHEN TRY_CAST({c} AS DOUBLE) IS NULL THEN NULL WHEN TRY_CAST({c} AS DOUBLE) <= 1 "
            f"THEN TRY_CAST({c} AS DOUBLE) * 100 ELSE TRY_CAST({c} AS DOUBLE) END")


def files(source_dir: Path) -> dict[tuple[str, int], Path]:
    """(table, year) -> parquet file in ``raw/``, for every year present; the header must be there for each year."""
    raw = Path(source_dir) / "raw"
    found = {}
    for key, table in NCCS.items():
        for y in YEARS:
            f = raw / f"{table}-{y}.parquet"
            if f.is_file():
                found[(key, y)] = f
    years = sorted({y for k, y in found if k == "header"})
    if not years:
        raise UserError(f"the IRS directory {source_dir} has no raw/{NCCS['header']}-<year>.parquet; fetch the files with "
                        f"`healthcare-ledger download --source-name irs --dest {source_dir}`", exit_code=2)
    missing = [k for k in NCCS for y in years if (k, y) not in found and k != "header"]
    if missing:
        raise UserError(f"{raw}: years {years} lack {sorted(set(missing))} tables for some year; fetch the pinned files", exit_code=2)
    for f in BMF_FILES:
        if not (raw / f).is_file():
            raise UserError(f"{raw} lacks the BMF extract {f}; fetch the pinned files", exit_code=2)
    return found


def _sr_select(kind: str, path: Path, year: int) -> str:
    p = _SR[kind]
    pct = _pct(f"{p}_PCT_OWN") if kind in ("partnership", "corporation") else "NULL::DOUBLE"
    ctrl512 = FLAG.format(c=f"{p}_CTRL_512_X") if kind in ("exempt", "corporation") else "FALSE"
    controlled = {"disregarded": "TRUE", "exempt": "TRUE",
                  "partnership": f"coalesce({pct} > 50, FALSE)",
                  "corporation": f"coalesce({pct} > 50, FALSE) OR {ctrl512}"}[kind]
    exempt = f"{p}_EXEMPT_CODE" if kind == "exempt" else "NULL::VARCHAR"
    corp_type = f"{p}_TYPE" if kind == "corporation" else "NULL::VARCHAR"
    return f"""SELECT {year} AS tax_year, OBJECTID AS object_id, {EIN.format(c='ORG_EIN')} AS ein, '{kind}' AS kind,
            CASE WHEN {EIN.format(c=f'{p}_EIN')} <> ORG_EIN THEN {EIN.format(c=f'{p}_EIN')} END AS related_ein,
            {p}_NAME_L1 AS related_name, {p}_ADDR_L1 AS address, {p}_ADDR_CITY AS city, {p}_ADDR_STATE AS state,
            left({p}_ADDR_ZIP, 5) AS zip5, {p}_DMCL_STATE AS domicile_state, {p}_PRIMARY_ACT AS primary_activity,
            {exempt} AS exempt_code, {corp_type} AS entity_type, {pct} AS pct_own, {ctrl512} AS control_512b13,
            {controlled} AS controlled, {p}_CTRL_NAME_L1 AS controlling_name
        FROM read_parquet('{path}')"""


def build(con: duckdb.DuckDBPyConnection, source_dir: Path, found: dict[tuple[str, int], Path]) -> dict[str, int]:
    years = sorted({y for k, y in found if k == "header"})
    raw = Path(source_dir) / "raw"
    log.info("irs: returns for tax years %s", years)
    con.execute("CREATE TABLE irs_return AS " + " UNION ALL ".join(f"""
        SELECT {y} AS tax_year, OBJECTID AS object_id, {EIN.format(c='ORG_EIN')} AS ein, RETURN_TYPE AS return_type,
               TRY_CAST(TAX_PERIOD_BEGIN_DATE AS DATE) AS period_begin, TRY_CAST(TAX_PERIOD_END_DATE AS DATE) AS period_end,
               F9_00_ORG_NAME_L1 AS name, F9_00_ORG_NAME_DBA_L1 AS dba_name, F9_00_ORG_ADDR_L1 AS address,
               F9_00_ORG_ADDR_CITY AS city, F9_00_ORG_ADDR_STATE AS state, left(F9_00_ORG_ADDR_ZIP, 5) AS zip5,
               F9_00_GROUP_EXEMPT_NUM AS group_exemption, F9_00_ORG_WEBSITE AS website, F9_00_ORG_PHONE AS phone,
               F9_00_YEAR_FORMATION AS formation_year, F9_00_LEGAL_DMCL_STATE AS domicile_state,
               F9_00_ORG_EXEMPT_TYPE AS exempt_type, {FLAG.format(c='F9_00_RETURN_GROUP_X')} AS group_return,
               {FLAG.format(c='F9_00_RETURN_FINAL_X')} AS final_return
        FROM read_parquet('{found[("header", y)]}')""" for y in years))
    log.info("irs: Schedule R")
    con.execute("CREATE TABLE irs_related AS " + " UNION ALL ".join(
        _sr_select(k, found[(k, y)], y) for y in years for k in _SR))
    log.info("irs: Schedule H facilities")
    # the subordinate-EIN columns are missing from some years' tables
    def opt(path: Path, col: str) -> str:
        have = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{path}')").fetchall()}
        return col if col in have else "NULL::VARCHAR"
    con.execute("CREATE TABLE irs_hospital_facility AS " + " UNION ALL ".join(f"""
        SELECT {y} AS tax_year, OBJECTID AS object_id, {EIN.format(c='ORG_EIN')} AS ein,
               TRY_CAST(SH_05_HOSPITAL_FACILITY_NUM AS INTEGER) AS facility_num, SH_05_HOSPITAL_NAME_L1 AS name,
               SH_05_HOSPITAL_ADDR_L1 AS address, SH_05_HOSPITAL_ADDR_CITY AS city, SH_05_HOSPITAL_ADDR_STATE AS state,
               left(SH_05_HOSPITAL_ADDR_ZIP, 5) AS zip5, SH_05_HOSPITAL_STATE_LIC_NUM AS state_license,
               {EIN.format(c=opt(found[("hospital", y)], 'SH_05_HOSPITAL_SUBORD_EIN'))} AS subordinate_ein, SH_05_HOSPITAL_WEBSITE AS website,
               {FLAG.format(c='SH_05_HOSPITAL_LIC_X')} AS licensed, {FLAG.format(c='SH_05_HOSPITAL_MEDICAL_X')} AS general_medical,
               {FLAG.format(c='SH_05_HOSPITAL_CHILDREN_X')} AS childrens, {FLAG.format(c='SH_05_HOSPITAL_TEACHING_X')} AS teaching,
               {FLAG.format(c='SH_05_HOSPITAL_CRITICAL_ACCESS_X')} AS critical_access, {FLAG.format(c='SH_05_HOSPITAL_ER_24HOURS_X')} AS er_24h
        FROM read_parquet('{found[("hospital", y)]}')""" for y in years))
    log.info("irs: families")
    families(con)
    log.info("irs: BMF")
    con.execute("CREATE TABLE irs_bmf AS " + " UNION ALL ".join(f"""
        SELECT {EIN.format(c='"EIN"')} AS ein, "NAME" AS name, "ICO" AS in_care_of, "STREET" AS address, "CITY" AS city, "STATE" AS state,
               left("ZIP", 5) AS zip5, "GROUP" AS group_exemption, "SUBSECTION" AS subsection, "AFFILIATION" AS affiliation,
               "CLASSIFICATION" AS classification, "RULING" AS ruling, "FOUNDATION" AS foundation, "ORGANIZATION" AS organization,
               "STATUS" AS status, "TAX_PERIOD" AS tax_period, "FILING_REQ_CD" AS filing_requirement, "NTEE_CD" AS ntee,
               TRY_CAST("ASSET_AMT" AS BIGINT) AS assets, TRY_CAST("INCOME_AMT" AS BIGINT) AS income, TRY_CAST("REVENUE_AMT" AS BIGINT) AS revenue,
               "SORT_NAME" AS sort_name, '{f}' AS source_file
        FROM read_csv('{raw / f}', header = true, all_varchar = true)""" for f in BMF_FILES))
    return {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TABLES}


FAMILY_FILER_TYPES = ("501c3", "501c2", "501c4", "501c25")     # charities, title-holding companies, social-welfare HMOs
FAMILY_RELATED_SUBSECTIONS = ("3", "2", "4", "25", "")       # the same for a related exempt entity, or no code given
SUBSECTION = "regexp_extract(upper(coalesce({c}, '')), 'C\\)?\\s*\\(?\\s*([0-9]+)', 1)"


def families(con: duckdb.DuckDBPyConnection) -> None:
    """``irs_family`` (tax_year, ein, family_id, n_members) by label propagation over the family edges of each tax
    year; a filer with no edge is a family of one.

    A family edge is a controlled relation (``irs_related.controlled``) from a filer that is a charity, title-holding
    company or 501(c)(4) (``FAMILY_FILER_TYPES``) to an entity that is one of those too, or taxable, or of no stated
    subsection; and, when the related entity files a Schedule R of its own that year, one it lists back. Related
    organizations are related in both directions (a parent and its subsidiary each list the other), so an
    unreciprocated listing is a different relation: a multiple-employer retirement or benefit fund lists every
    participating employer as related, an association its members, a fraternal order its lodges, and through such
    a hub every hospital system in the country would be one family. On the 2022 returns 95% of listings whose target
    files a Schedule R are reciprocated; the retirement funds' are under 5%."""
    types = ", ".join(f"'{t}'" for t in FAMILY_FILER_TYPES)
    subs = ", ".join(f"'{t}'" for t in FAMILY_RELATED_SUBSECTIONS)
    con.execute(f"""CREATE TEMP TABLE _e0 AS
        SELECT DISTINCT r.tax_year, r.ein AS a, r.related_ein AS b FROM irs_related r
        WHERE r.controlled AND r.ein IS NOT NULL AND r.related_ein IS NOT NULL
          AND (r.kind <> 'exempt' OR {SUBSECTION.format(c='r.exempt_code')} IN ({subs}))
          AND EXISTS (SELECT 1 FROM irs_return f WHERE f.tax_year = r.tax_year AND f.ein = r.ein AND f.exempt_type IN ({types}))""")
    con.execute("""CREATE TEMP TABLE _sr AS SELECT DISTINCT tax_year, ein FROM irs_related WHERE related_ein IS NOT NULL""")
    con.execute("""CREATE TEMP TABLE _e1 AS
        SELECT e.* FROM _e0 e
        WHERE NOT EXISTS (SELECT 1 FROM _sr s WHERE s.tax_year = e.tax_year AND s.ein = e.b)
           OR EXISTS (SELECT 1 FROM _e0 r WHERE r.tax_year = e.tax_year AND r.a = e.b AND r.b = e.a)""")
    con.execute("CREATE TEMP TABLE _e AS SELECT tax_year, a, b FROM _e1 UNION SELECT tax_year, b, a FROM _e1")
    con.execute("""CREATE TEMP TABLE _l AS
        SELECT DISTINCT tax_year, ein AS node, ein AS comp FROM irs_return WHERE ein IS NOT NULL
        UNION SELECT tax_year, a, a FROM _e""")
    while True:
        con.execute("""CREATE OR REPLACE TEMP TABLE _n AS
            SELECT l.tax_year, l.node, least(l.comp, coalesce(min(m.comp), l.comp)) AS comp
            FROM _l l LEFT JOIN _e ON _e.tax_year = l.tax_year AND _e.a = l.node
            LEFT JOIN _l m ON m.tax_year = l.tax_year AND m.node = _e.b GROUP BY l.tax_year, l.node, l.comp""")
        con.execute("""CREATE OR REPLACE TEMP TABLE _n AS
            SELECT n.tax_year, n.node, least(n.comp, coalesce(j.comp, n.comp)) AS comp
            FROM _n n LEFT JOIN _n j ON j.tax_year = n.tax_year AND j.node = n.comp""")
        changed = con.execute("SELECT count(*) FROM _n JOIN _l USING (tax_year, node) WHERE _n.comp <> _l.comp").fetchone()[0]
        con.execute("CREATE OR REPLACE TEMP TABLE _l AS SELECT * FROM _n")
        if not changed:
            break
    con.execute("""CREATE TABLE irs_family AS
        SELECT tax_year, node AS ein, comp AS family_id, count(*) OVER (PARTITION BY tax_year, comp) AS n_members FROM _l""")
    con.execute("DROP TABLE _e0; DROP TABLE _sr; DROP TABLE _e1; DROP TABLE _e; DROP TABLE _l; DROP TABLE _n")


def load(source_dir: Path, warehouse: Path, work: Path) -> dict[str, int]:
    """Rebuild ``<warehouse>/irs.duckdb`` from ``source_dir/raw``; the new file replaces the old one only once every
    table is built."""
    found = files(source_dir)
    warehouse = Path(warehouse)
    warehouse.mkdir(parents=True, exist_ok=True)
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    target = source_file(warehouse, DATABASE)
    part = target.with_name(target.name + ".part")
    part.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(dir=work, prefix="irs-") as tmp:
        con = duckdb.connect(str(part))
        try:
            configure(con, temp_dir=Path(tmp))
            counts = build(con, source_dir, found)
        except BaseException:
            con.close()
            part.unlink(missing_ok=True)
            raise
        con.close()
    os.replace(part, target)
    return counts
