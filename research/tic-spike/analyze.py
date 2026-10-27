"""Spike: do Transparency in Coverage tax ids change what the provider graph reproduces?

    python analyze.py DATA_DIR GRAPH_DIR

DATA_DIR holds tic_heads.py's heads/ and enrollment_20260929.parquet (PPEF enrollments on the graph's last
quarter, exported from the warehouse); GRAPH_DIR is graph-build's output. Everything is on 2026-09-29, the
quarter nearest the October 2026 TiC files. Each measure is run on every (tax id, NPI) pair TiC lists and on
pairs that two or more insurers list ("confirmed"), because payer rosters are known to carry NPIs that never
bill under the tax id.
"""

import sys

import duckdb

data, graph = sys.argv[1], sys.argv[2]
D = "DATE '2026-09-29'"
ASOF = f"effective_date <= {D} AND (end_date IS NULL OR end_date > {D})"
HOSPITAL_TYPES = "('00-09', '00-85', '00-24', '12-Z1')"

con = duckdb.connect()
con.execute(f"""
CREATE TABLE tic AS
SELECT tin, npi::VARCHAR AS npi, count(DISTINCT payer) AS payers, list(DISTINCT payer ORDER BY payer) AS payer_list,
       any_value(business_name) AS business_name
FROM read_parquet('{data}/heads/*/*.parquet')
WHERE tin_type = 'ein' AND tin ~ '^[0-9]{{9}}$' AND tin NOT IN ('000000000', '999999999')
GROUP BY ALL;
CREATE TABLE enr AS SELECT * FROM '{data}/enrollment_20260929.parquet';
CREATE TABLE phys AS SELECT pac_id, npi, provider_type_codes FROM '{graph}/practitioner.parquet'
    WHERE {ASOF} AND taxonomy_code LIKE '20%';
CREATE TABLE org AS SELECT pac_id, hospital, org_name FROM '{graph}/organization.parquet' WHERE {ASOF};
CREATE TABLE org_npi AS SELECT DISTINCT pac_id, npi FROM enr WHERE enrollment_id LIKE 'O%' AND pac_id IS NOT NULL;
CREATE TABLE hosp_npi AS SELECT DISTINCT npi FROM enr WHERE provider_type_code IN {HOSPITAL_TYPES};
CREATE TABLE edge AS
SELECT r.practitioner_pac_id, r.organization_pac_id,
       count(*) OVER (PARTITION BY r.practitioner_pac_id) AS k
FROM '{graph}/reassignment.parquet' r SEMI JOIN phys p ON p.pac_id = r.practitioner_pac_id
WHERE r.{ASOF};
""")


def show(title: str, sql: str) -> None:
    print(f"\n== {title}")
    print(con.sql(sql).df().round(1).to_string(index=False))


for label, filt in (("all pairs", "true"), ("confirmed by 2+ payers", "payers >= 2")):
    con.execute(f"CREATE OR REPLACE VIEW t AS SELECT * FROM tic WHERE {filt}")
    print(f"\n######## {label}")

    show("0. coverage: physicians (taxonomy 20*) with an active PPEF enrollment, found in TiC", """
    SELECT count(*) AS physicians,
           100 * avg((p.npi IN (SELECT npi FROM t))::INT) AS pct_in_tic,
           100 * avg((p.pac_id IN (SELECT practitioner_pac_id FROM edge))::INT) AS pct_with_reassignment
    FROM phys p""")

    # 1. is an associate id one tax id? Map each organization's own NPIs to the EINs they are listed under.
    con.execute("""
    CREATE OR REPLACE TABLE org_ein AS
    SELECT o.pac_id, t.tin, count(*) AS npis FROM org_npi o JOIN t USING (npi) GROUP BY ALL""")
    show("1a. organizations (associate ids) found in TiC, by number of distinct EINs their NPIs are listed under", """
    WITH n AS (SELECT pac_id, count(*) AS eins FROM org_ein GROUP BY 1)
    SELECT (SELECT count(*) FROM org) AS organizations, count(*) AS found,
           100 * avg((eins = 1)::INT) AS pct_one_ein, 100 * avg((eins = 2)::INT) AS pct_two,
           100 * avg((eins >= 3)::INT) AS pct_three_plus FROM n""")
    show("1b. EINs that list an organization NPI, by number of distinct associate ids behind them", """
    WITH n AS (SELECT tin, count(*) AS pacs FROM org_ein GROUP BY 1)
    SELECT count(*) AS eins, 100 * avg((pacs = 1)::INT) AS pct_one_pac, 100 * avg((pacs = 2)::INT) AS pct_two,
           100 * avg((pacs >= 3)::INT) AS pct_three_plus FROM n""")

    # 2. do reassignment edges show up as the physician being listed under the organization's EIN?
    con.execute("""
    CREATE OR REPLACE TABLE edge_check AS
    SELECT e.*, EXISTS (SELECT 1 FROM org_ein oe JOIN t ON t.tin = oe.tin
                        WHERE oe.pac_id = e.organization_pac_id AND t.npi = p.npi) AS confirmed
    FROM edge e JOIN phys p ON p.pac_id = e.practitioner_pac_id
    WHERE p.npi IN (SELECT npi FROM t) AND e.organization_pac_id IN (SELECT pac_id FROM org_ein)""")
    show("2. reassignment edges whose physician TiC lists under the organization's EIN (both ends in TiC), by k", """
    SELECT CASE WHEN k >= 4 THEN '4+' ELSE k::VARCHAR END AS reassignments, count(*) AS edges,
           100 * avg(confirmed::INT) AS pct_confirmed
    FROM edge_check GROUP BY ROLLUP (1) ORDER BY 1 NULLS LAST""")

    # 3. Luo et al.'s basic measure on real tax ids: a physician's EIN also lists a hospital NPI.
    con.execute("""
    CREATE OR REPLACE TABLE phys_ein AS
    SELECT p.pac_id, t.tin, t.tin IN (SELECT tin FROM t SEMI JOIN hosp_npi USING (npi)) AS hospital_ein,
           count(*) OVER (PARTITION BY p.pac_id) AS m
    FROM phys p JOIN t USING (npi)""")
    show("3. physicians with a hospital tax id: TiC EINs vs the graph's associate ids (same physicians: in TiC and with a reassignment)", """
    WITH pop AS (SELECT DISTINCT pac_id FROM phys_ein SEMI JOIN edge ON edge.practitioner_pac_id = phys_ein.pac_id),
    tic_m AS (SELECT pac_id, bool_or(hospital_ein) AS any_h, sum(hospital_ein::INT / m) AS frac_h, max(m) AS m
              FROM phys_ein SEMI JOIN pop USING (pac_id) GROUP BY 1),
    g_m AS (SELECT e.practitioner_pac_id AS pac_id, bool_or(o.hospital) AS any_h, sum(o.hospital::INT / e.k) AS frac_h,
                   max(e.k) AS k
            FROM edge e JOIN org o ON o.pac_id = e.organization_pac_id
            WHERE e.practitioner_pac_id IN (SELECT pac_id FROM pop) GROUP BY 1)
    SELECT 'TiC EIN' AS firm, count(*) AS physicians, 100 * avg(any_h::INT) AS pct_any, 100 * avg(frac_h) AS pct_fractional,
           100 * avg(any_h::INT) FILTER (WHERE m = 1) AS pct_single_group, avg(m) AS mean_groups FROM tic_m
    UNION ALL
    SELECT 'PPEF associate id', count(*), 100 * avg(any_h::INT), 100 * avg(frac_h),
           100 * avg(any_h::INT) FILTER (WHERE k = 1), avg(k) FROM g_m""")

    # 4. Muhlestein and Smith's group size, by EIN roster vs by associate id, same physicians
    show("4. share of physicians (fractional) by group size: TiC EIN vs PPEF associate id, same physicians", """
    WITH pop AS (SELECT DISTINCT pac_id FROM phys_ein SEMI JOIN edge ON edge.practitioner_pac_id = phys_ein.pac_id),
    tsize AS (SELECT tin, count(*) AS n FROM phys_ein GROUP BY 1),
    gsize AS (SELECT organization_pac_id, count(*) AS n FROM edge GROUP BY 1),
    tw AS (SELECT 1.0 / pe.m AS w, s.n FROM phys_ein pe JOIN tsize s USING (tin) SEMI JOIN pop USING (pac_id)),
    gw AS (SELECT 1.0 / e.k AS w, s.n FROM edge e JOIN gsize s USING (organization_pac_id)
           WHERE e.practitioner_pac_id IN (SELECT pac_id FROM pop))
    SELECT 'TiC EIN' AS firm, 100 * sum(w) FILTER (WHERE n <= 9) / sum(w) AS "9 or fewer",
           100 * sum(w) FILTER (WHERE n >= 100) / sum(w) AS "100 or more",
           100 * sum(w) FILTER (WHERE n >= 500) / sum(w) AS "500 or more" FROM tw
    UNION ALL
    SELECT 'PPEF associate id', 100 * sum(w) FILTER (WHERE n <= 9) / sum(w), 100 * sum(w) FILTER (WHERE n >= 100) / sum(w),
           100 * sum(w) FILTER (WHERE n >= 500) / sum(w) FROM gw""")
