-- The small public inputs in data/ (cited in data/SOURCES.md), typed and joined.
-- Paths are relative to the study directory (the runner sets file_search_path to it).

-- the 50 states and DC, with their two-digit FIPS code
CREATE OR REPLACE TABLE states AS
SELECT state, fips FROM read_csv('data/state_fips.csv', all_varchar = true);

-- the as-of date of every monthly snapshot: the archive is monthly from April 2011 (May 2013 is missing)
CREATE OR REPLACE TABLE monthly_snapshots AS
SELECT as_of_date FROM snapshots WHERE as_of_date >= DATE '2011-04-01' ORDER BY as_of_date;

-- FPA adoption month per state under McMichael and Markowitz, plus whether AANP calls it full practice in 2026.
-- coding: 'always' (FPA before 1998), 'statute' (took effect in fpa_effective), 'never' (no FPA through 2026)
CREATE OR REPLACE TABLE fpa_dates AS
SELECT f.state,
       f.coding,
       strptime(f.fpa_effective, '%Y-%m')::DATE AS fpa_effective,  -- first day of the month
       coalesce(f.partial = 'Y', false) AS partial,                  -- Florida: primary care NPs only
       a.state IS NOT NULL AS aanp_full_practice,
       f.note,
       f.source
FROM read_csv('data/np_fpa_dates.csv', all_varchar = true) f
LEFT JOIN read_csv('data/aanp_full_practice_2026.csv', all_varchar = true) a USING (state)
ORDER BY f.state;

-- resident population by state and year; the last Census year is carried forward to the year of the last snapshot
CREATE OR REPLACE TABLE state_population AS
WITH census AS (
    SELECT state, year, population FROM read_csv('data/state_population.csv', columns = {
        'state': 'VARCHAR', 'year': 'INTEGER', 'population': 'BIGINT', 'source': 'VARCHAR'})
),
last_census AS (SELECT max(year) AS year FROM census),
last_snapshot AS (SELECT max(year(as_of_date)) AS year FROM snapshots)
SELECT state, year, population, false AS carried_forward FROM census
UNION ALL
SELECT c.state, unnest(generate_series(l.year + 1, s.year))::INTEGER AS year, c.population, true
FROM census c, last_census l, last_snapshot s
WHERE c.year = l.year
ORDER BY state, year;

-- 2020 ZCTA to the county holding most of its land area
CREATE OR REPLACE TABLE zcta_county AS
SELECT zcta, county_fips FROM read_csv('data/zcta_county.csv', all_varchar = true);

-- adjacent counties that lie in different states
CREATE OR REPLACE TABLE border_county_pairs AS
SELECT county_fips, neighbor_fips FROM read_csv('data/state_border_county_pairs.csv', all_varchar = true);
