-- Border counties for every adoption: NPs in the adopting state's border counties against the adjacent
-- counties across the line in states still untreated, indexed to the adoption month.

-- NPs in force on each monthly snapshot, by county of the practice ZIP
CREATE OR REPLACE TABLE np_county_stock AS
SELECT s.as_of_date AS as_of, z.county_fips AS county, count(*) AS nps
FROM monthly_snapshots s
JOIN np_versions v
  ON v.effective_date <= s.as_of_date AND (v.end_date IS NULL OR v.end_date > s.as_of_date)
JOIN zcta_county z ON z.zcta = left(v.practice_postal_code, 5)
WHERE v.is_np
GROUP BY ALL
ORDER BY county, as_of;

-- the cohorts: full-scope adoptions inside the monthly window
CREATE OR REPLACE TABLE fpa_cohorts AS
SELECT state, fpa_effective FROM fpa_dates
WHERE coding = 'statute' AND fpa_effective >= DATE '2011-04-01' AND NOT partial
ORDER BY fpa_effective, state;

-- each cohort's border counties ('own') and the counties across the line ('across') in states that
-- stay untreated for the five years after the cohort adopts
CREATE OR REPLACE TABLE border_cohort_counties AS
SELECT DISTINCT c.state AS cohort, side.side, side.county
FROM fpa_cohorts c
JOIN states s ON s.state = c.state
JOIN border_county_pairs p ON left(p.county_fips, 2) = s.fips
JOIN states ns ON ns.fips = left(p.neighbor_fips, 2)
JOIN fpa_dates n ON n.state = ns.state
CROSS JOIN LATERAL (VALUES ('own', p.county_fips), ('across', p.neighbor_fips)) side(side, county)
WHERE n.coding = 'never' OR n.fpa_effective > c.fpa_effective + INTERVAL 60 MONTH
ORDER BY cohort, side, county;

-- NP counts on each side for every snapshot from 60 months before to 60 months after adoption.
-- k is months since adoption. Each cohort is indexed to the mean own/across ratio over the four
-- months ending at adoption (k = -3..0). A cohort is dropped when it has no untreated neighbour
-- counties, fewer than two base months, or a base month with no NPs on either side.
CREATE OR REPLACE TABLE border_index AS
WITH series AS (
    SELECT c.state AS cohort, m.as_of_date AS as_of,
           date_diff('month', c.fpa_effective, m.as_of_date) AS k,
           coalesce(sum(n.nps) FILTER (WHERE b.side = 'own'), 0) AS own,
           coalesce(sum(n.nps) FILTER (WHERE b.side = 'across'), 0) AS across
    FROM fpa_cohorts c
    JOIN monthly_snapshots m ON date_diff('month', c.fpa_effective, m.as_of_date) BETWEEN -60 AND 60
    LEFT JOIN border_cohort_counties b ON b.cohort = c.state
    LEFT JOIN np_county_stock n ON n.county = b.county AND n.as_of = m.as_of_date
    GROUP BY ALL
),
base AS (
    SELECT cohort, avg(own / across) AS base_ratio
    FROM series WHERE k BETWEEN -3 AND 0
    GROUP BY cohort
    HAVING count(*) >= 2 AND min(own) > 0 AND min(across) > 0
)
SELECT s.cohort, s.k, s.as_of, s.own, s.across,
       (s.own / s.across) / b.base_ratio * 100 AS ratio_index
FROM series s JOIN base b USING (cohort)
ORDER BY cohort, k;
