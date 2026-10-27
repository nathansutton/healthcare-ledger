"""The graph's annual health system tables, derived from the AHRQ Compendium source (``sources/ahrq``).

AHRQ publishes, for 2016, 2018 and 2020-2023, a list of health systems (each with at least one general acute
hospital and a group of physicians under common ownership or management) and linkage files that place
hospitals, nursing homes and home health agencies (by CCN) and physician group practices (by PECOS associate
id) in those systems. ``load --source-name ahrq`` puts each file type in an ``ahrq_*`` table, one row per
entity and year; docs/METHODOLOGY.md section 8a has the rules.

``system_tables`` turns those into two annual tables:

``system``
    one row per Compendium system and year.
``system_link``
    one row per linked entity and year: an organization (PECOS associate id, from the group practice file),
    a hospital, nursing home or home health agency (CCN), with its system id, or NULL when AHRQ lists the
    entity as not in a system. ``via`` names the file. For an edition with no group practice file (2023),
    the organizations of the latest earlier file are carried forward (``via`` 'carried forward from <year>'),
    keeping a system id only if that system is in the edition's list: an associate id keeps its label from
    one edition to the next 96% of the time, which AHRQ's outpatient-site files, matched by address and name,
    do not approach (docs/METHODOLOGY.md section 8a), so ``ahrq_outpatient_site_linkage`` is not used.
"""

from __future__ import annotations

import duckdb

SYSTEM_COLUMNS = ("health_sys_id", "health_sys_name", "health_sys_city", "health_sys_state", "total_mds",
                  "prim_care_mds", "grp_cnt", "hosp_cnt", "acutehosp_cnt", "sys_multistate", "sys_beds")
# facility table -> (node type, AHRQ's row id, node id column, entity name column, via)
LINKS = {
    "ahrq_hospital_linkage": ("hospital", "compendium_hospital_id", "ccn", "hospital_name", "hospital file"),
    "ahrq_nursing_home_linkage": ("nursing_home", "compendium_nh_id", "ccn", "nursing_home_name", "nursing home file"),
    "ahrq_home_health_linkage": ("home_health", "compendium_hh_id", "ccn", "home_health_care_org_name", "home health file"),
}
GROUP_PRACTICE = "ahrq_group_practice_linkage"


def _years(con: duckdb.DuckDBPyConnection, table: str) -> list[int]:
    return [r[0] for r in con.execute(f"SELECT DISTINCT year::INTEGER FROM {table} ORDER BY 1").fetchall()]


def system_tables(con: duckdb.DuckDBPyConnection) -> dict[str, list[int]]:
    """Create the system and system_link tables in con from the ahrq_* tables. Returns the years each table covers."""
    num = "TRY_CAST(nullif(regexp_replace({c}, '[^0-9.-]', '', 'g'), '') AS {t}) AS {c}"
    con.execute(f"""
        CREATE OR REPLACE TABLE system AS
        SELECT year::INTEGER AS year, health_sys_id, health_sys_name, health_sys_city, health_sys_state,
               {", ".join(num.format(c=c, t="DOUBLE" if c == "sys_beds" else "INTEGER") for c in SYSTEM_COLUMNS[4:])}
        FROM ahrq_system WHERE health_sys_id IS NOT NULL ORDER BY year, health_sys_id""")
    facility = " UNION ALL ".join(f"""
        SELECT year::INTEGER AS year, '{node}' AS node_type, {node_id} AS node_id, {row_id} AS compendium_id,
               {name} AS name, health_sys_id, NULL::INTEGER AS md_ppas_physicians, '{via}' AS via
        FROM {table} WHERE {node_id} IS NOT NULL""" for table, (node, row_id, node_id, name, via) in LINKS.items())
    con.execute(f"""
        CREATE OR REPLACE TABLE system_link AS
        WITH g AS (SELECT *, 'GP' || year || '-' || row_number() OVER (PARTITION BY year ORDER BY tin_name, pecos_pac_ids)
                             AS compendium_id
                   FROM {GROUP_PRACTICE})
        SELECT year::INTEGER AS year, 'organization' AS node_type, unnest(string_split(pecos_pac_ids, '^')) AS node_id,
               compendium_id, tin_name AS name, health_sys_id, TRY_CAST(md_do_md_ppas AS INTEGER) AS md_ppas_physicians,
               'group practice file' AS via
        FROM g WHERE pecos_pac_ids IS NOT NULL
        UNION ALL {facility}""")
    # editions without a group practice file inherit the latest earlier one's organizations
    gp_years = _years(con, GROUP_PRACTICE)
    for year in _years(con, "ahrq_system"):
        if year in gp_years or year < gp_years[0]:
            continue
        src = max(y for y in gp_years if y < year)
        con.execute(f"""
            INSERT INTO system_link
            SELECT {year}, node_type, node_id, compendium_id, name,
                   CASE WHEN health_sys_id IN (SELECT health_sys_id FROM system WHERE year = {year}) THEN health_sys_id END,
                   NULL, 'carried forward from {src}'
            FROM system_link WHERE year = {src} AND node_type = 'organization'""")
    con.execute("CREATE OR REPLACE TABLE system_link AS SELECT * FROM system_link ORDER BY year, node_type, node_id")
    return {t: _years(con, t) for t in ("ahrq_system", *LINKS, GROUP_PRACTICE)}
