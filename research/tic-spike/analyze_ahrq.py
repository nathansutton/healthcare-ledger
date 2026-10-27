"""Does the public provider graph reproduce the AHRQ Compendium's claims-based physician-to-system measures?

    python analyze_ahrq.py WAREHOUSE_DIR GRAPH_DIR

WAREHOUSE_DIR holds ahrq.duckdb from `healthcare-ledger load --source-name ahrq`; this reads its
ahrq_group_practice_linkage table, AHRQ's Group
Practice Linkage Files for 2016, 2018 and 2020-2022. Each row is a tax id with two or more physicians in that year's MD-PPAS, its PECOS associate ids, its
Compendium health system (if any) and its MD-PPAS physician count, where MD-PPAS puts each physician in the
one tax id that billed the plurality of their Medicare Part B lines. That is the claims-based main-group rule
the consolidation papers use and the PPEF lacks, so for these years it is a benchmark the graph can be
scored against. The graph is read on the PPEF quarter nearest 1 July of each year.
"""

import sys

import duckdb

warehouse, graph = sys.argv[1], sys.argv[2]
YEARS = {2016: "2016-08-30", 2018: "2018-07-03", 2020: "2020-07-01", 2021: "2021-07-01", 2022: "2022-07-01"}

con = duckdb.connect()
con.execute(f"ATTACH '{warehouse}/ahrq.duckdb' AS ahrq (READ_ONLY)")
con.execute(f"""
CREATE TABLE gplf AS
SELECT year, tin_name, health_sys_id, health_sys_name,
       md_do_md_ppas::INT AS md, row_number() OVER () AS tin_row, string_split(pecos_pac_ids, '^') AS pac_ids
FROM ahrq.ahrq_group_practice_linkage;
CREATE TABLE gplf_pac AS SELECT year, tin_row, unnest(pac_ids) AS pac_id FROM gplf;
CREATE TABLE q AS SELECT * FROM (VALUES {", ".join(f"({y}, DATE '{d}')" for y, d in YEARS.items())}) q(year, d);
CREATE TABLE phys AS
SELECT q.year, p.pac_id FROM q JOIN '{graph}/practitioner.parquet' p
  ON p.effective_date <= q.d AND (p.end_date IS NULL OR p.end_date > q.d)
WHERE p.taxonomy_code LIKE '20%';
CREATE TABLE edge AS
SELECT e.*, count(*) OVER (PARTITION BY e.year, e.practitioner_pac_id) AS k,
       count(*) OVER (PARTITION BY e.year, e.organization_pac_id) AS org_physicians
FROM (SELECT q.year, r.practitioner_pac_id, r.organization_pac_id FROM q JOIN '{graph}/reassignment.parquet' r
        ON r.effective_date <= q.d AND (r.end_date IS NULL OR r.end_date > q.d)) e
SEMI JOIN phys p ON p.year = e.year AND p.pac_id = e.practitioner_pac_id;
-- each graph organization's AHRQ tax id that year, if its associate id is listed
CREATE TABLE org_tin AS
SELECT DISTINCT e.year, e.organization_pac_id, gp.tin_row
FROM (SELECT DISTINCT year, organization_pac_id FROM edge) e
JOIN gplf_pac gp ON gp.year = e.year AND gp.pac_id = e.organization_pac_id;
""")


def show(title: str, sql: str) -> None:
    print(f"\n== {title}")
    print(con.sql(sql).df().round(1).to_string(index=False))


show("1. linkage: AHRQ tax ids found among the graph's organizations with a physician edge", """
SELECT g.year, count(*) AS ahrq_tins, 100 * avg((o.tin_row IS NOT NULL)::INT) AS pct_tins_found,
       100 * sum(g.md) FILTER (WHERE o.tin_row IS NOT NULL) / sum(g.md) AS pct_md_ppas_physicians_found
FROM gplf g LEFT JOIN (SELECT DISTINCT year, tin_row FROM org_tin) o USING (year, tin_row)
GROUP BY 1 ORDER BY 1""")

show("2. group size per tax id: graph physicians reassigning to it vs MD-PPAS physicians assigned to it", """
WITH s AS (
    SELECT o.year, g.md, sum(1.0 / e.k) AS fractional, count(*) AS any_edge
    FROM org_tin o JOIN gplf g USING (year, tin_row)
    JOIN edge e ON e.year = o.year AND e.organization_pac_id = o.organization_pac_id
    GROUP BY o.year, o.tin_row, g.md)
SELECT year, count(*) AS tins, sum(md) AS md_ppas, sum(fractional)::INT AS graph_fractional, sum(any_edge) AS graph_any,
       median(any_edge / md) AS median_ratio_any, median(fractional / md) AS median_ratio_fractional,
       corr(ln(1 + fractional), ln(1 + md)) AS log_corr
FROM s GROUP BY 1 ORDER BY 1""")

show("3. share of physicians in a Compendium health system, among physicians in AHRQ-listed (2+ physician) tax ids", """
WITH e AS (
    SELECT e.year, e.practitioner_pac_id, e.k, g.health_sys_id IS NOT NULL AS sys
    FROM edge e JOIN org_tin o USING (year, organization_pac_id) JOIN gplf g USING (year, tin_row)),
-- a physician is counted once in the graph, over their AHRQ-listed edges only
p AS (SELECT year, practitioner_pac_id, avg(sys::INT) AS frac_sys, bool_or(sys) AS any_sys, max(k) AS k
      FROM e GROUP BY 1, 2)
SELECT year,
       (SELECT 100 * sum(md) FILTER (WHERE health_sys_id IS NOT NULL) / sum(md) FROM gplf g WHERE g.year = p.year) AS md_ppas,
       100 * avg(frac_sys) AS graph_fractional, 100 * avg(any_sys::INT) FILTER (WHERE k = 1) AS graph_single_group,
       100 * avg(any_sys::INT) AS graph_any
FROM p GROUP BY 1 ORDER BY 1""")

show("4. share of physicians in tax ids of 100+ physicians (sizes by each source's own count), AHRQ-listed tax ids", """
WITH g_size AS (
    SELECT o.year, o.tin_row, count(DISTINCT e.practitioner_pac_id) AS n, sum(1.0 / e.k) AS n_frac
    FROM org_tin o JOIN edge e ON e.year = o.year AND e.organization_pac_id = o.organization_pac_id GROUP BY 1, 2),
w AS (SELECT e.year, 1.0 / e.k AS w, s.n, s.n_frac, e.k
      FROM edge e JOIN org_tin o USING (year, organization_pac_id) JOIN g_size s USING (year, tin_row))
SELECT year,
       (SELECT 100 * sum(md) FILTER (WHERE md >= 100) / sum(md) FROM gplf g WHERE g.year = w.year) AS md_ppas,
       100 * sum(w) FILTER (WHERE n >= 100) / sum(w) AS graph_fractional,
       100 * avg((n >= 100)::INT) FILTER (WHERE k = 1) AS graph_single_group,
       -- size a tax id the way MD-PPAS does, counting each physician once across their groups
       100 * sum(w) FILTER (WHERE n_frac >= 100) / sum(w) AS graph_fractional_fractional_size
FROM w GROUP BY 1 ORDER BY 1""")

show("5. the graph's whole physician population (solo and unlisted tax ids included), for the papers' denominators", """
WITH p AS (
    SELECT e.year, e.practitioner_pac_id, e.k,
           sum((g.health_sys_id IS NOT NULL)::INT * 1.0 / e.k) AS frac_sys
    FROM edge e LEFT JOIN org_tin o USING (year, organization_pac_id) LEFT JOIN gplf g USING (year, tin_row)
    GROUP BY 1, 2, 3)
SELECT year, count(*) AS physicians_with_edge, 100 * avg(frac_sys) AS pct_system_fractional,
       100 * avg((frac_sys > 0)::INT) FILTER (WHERE k = 1) AS pct_system_single_group
FROM p GROUP BY 1 ORDER BY 1""")
