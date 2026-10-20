-- The state-month panel and the two estimation samples built from it.

-- NPs in force on each monthly snapshot, by practice state (US states and DC only).
-- A version is in force on date d when effective_date <= d < end_date (NULL end_date: still current).
CREATE OR REPLACE TABLE np_state_stock AS
SELECT s.as_of_date AS as_of,
       v.practice_state AS state,
       count(*) AS nps,
       count(*) FILTER (WHERE v.sole_proprietor = 'Y') AS sole_proprietors
FROM monthly_snapshots s
JOIN np_versions v
  ON v.effective_date <= s.as_of_date AND (v.end_date IS NULL OR v.end_date > s.as_of_date)
WHERE v.is_np AND v.practice_state IN (SELECT state FROM states)
GROUP BY ALL
ORDER BY state, as_of;

-- One row per state and monthly snapshot: stock, flows, population, treatment timing, outcomes.
-- t is months since April 2011 (the first monthly snapshot); g is the adoption month on the same
-- clock, NULL for a state that never adopted. Moves count at the destination (move) and at the
-- origin (move_out); events in months without a snapshot are not counted.
CREATE OR REPLACE TABLE np_state_month AS
WITH events AS (
    SELECT *, date_trunc('month', date)::DATE AS month
    FROM np_events WHERE state IN (SELECT state FROM states)
),
flows AS (
    SELECT state, month,
           count(*) FILTER (WHERE event = 'entry') AS entry,
           count(*) FILTER (WHERE event = 'exit') AS exit,
           count(*) FILTER (WHERE event = 'move') AS move,
           count(*) FILTER (WHERE event = 'became_sole_proprietor') AS became_sole_proprietor,
           count(*) FILTER (WHERE event = 'left_sole_proprietor') AS left_sole_proprietor
    FROM events GROUP BY ALL
),
move_out AS (
    SELECT from_state AS state, month, count(*) AS move_out
    FROM events WHERE event = 'move' GROUP BY ALL
),
panel AS (
    SELECT s.state, date_trunc('month', s.as_of)::DATE AS month, s.as_of, s.nps, s.sole_proprietors,
           coalesce(f.entry, 0) AS entry, coalesce(f.exit, 0) AS exit, coalesce(f.move, 0) AS move,
           coalesce(o.move_out, 0) AS move_out,
           coalesce(f.became_sole_proprietor, 0) AS became_sole_proprietor,
           coalesce(f.left_sole_proprietor, 0) AS left_sole_proprietor
    FROM np_state_stock s
    LEFT JOIN flows f ON f.state = s.state AND f.month = date_trunc('month', s.as_of)
    LEFT JOIN move_out o ON o.state = s.state AND o.month = date_trunc('month', s.as_of)
)
SELECT p.*,
       year(p.month) AS year,
       pop.population,
       d.fpa_effective, d.coding, d.partial, d.aanp_full_practice,
       p.nps / pop.population * 1e5 AS nps_per_100k,
       ln(p.nps / pop.population * 1e5) AS log_nps_per_100k,
       p.entry / p.nps * 1000 AS entry_rate,            -- per 1,000 NPs in the state that month
       p.move / p.nps * 1000 AS in_rate,
       p.move_out / p.nps * 1000 AS out_rate,
       p.sole_proprietors / p.nps AS sp_share,
       date_diff('month', DATE '2011-04-01', p.month) AS t,
       date_diff('month', DATE '2011-04-01', d.fpa_effective)::DOUBLE AS g
FROM panel p
JOIN fpa_dates d USING (state)
LEFT JOIN state_population pop ON pop.state = p.state AND pop.year = year(p.month)
ORDER BY p.state, p.month;

SELECT CASE WHEN count(*) > 0 THEN error('np_state_month: a snapshot year has no population row') END
FROM np_state_month WHERE population IS NULL;

-- The difference-in-differences sample, monthly: states that never adopted FPA, and the states that
-- adopted a full-scope law inside the monthly window. Dropped: FPA before 1998, adoption before
-- April 2011, and Florida (primary care only). sid numbers the states 0..n-1 alphabetically.
CREATE OR REPLACE TABLE did_state_month AS
SELECT dense_rank() OVER (ORDER BY state) - 1 AS sid, *
FROM np_state_month
WHERE coding = 'never'
   OR (coding = 'statute' AND fpa_effective >= DATE '2011-04-01' AND NOT partial)
ORDER BY state, month;

-- The same sample by calendar quarter, which keeps the Callaway-Sant'Anna group-time cells manageable.
-- Levels and shares are taken from the last month of the quarter, flows are summed, and the flow
-- rates are per 1,000 NPs (the quarter's mean stock) per quarter. t and g count quarters from 2011Q2.
CREATE OR REPLACE TABLE did_state_quarter AS
WITH q AS (
    SELECT state, sid, date_trunc('quarter', month)::DATE AS quarter,
           arg_max(log_nps_per_100k, month) AS log_nps_per_100k,
           arg_max(sp_share, month) AS sp_share,
           sum(entry) AS entry, sum(move) AS move, sum(move_out) AS move_out,
           avg(nps) AS nps,
           any_value(fpa_effective) AS fpa_effective,
           any_value(aanp_full_practice) AS aanp_full_practice
    FROM did_state_month GROUP BY ALL
)
SELECT *,
       entry / nps * 1000 AS entry_rate,
       move / nps * 1000 AS move_rate,
       move_out / nps * 1000 AS move_out_rate,
       date_diff('quarter', DATE '2011-04-01', quarter) AS t,
       date_diff('quarter', DATE '2011-04-01', date_trunc('quarter', fpa_effective))::DOUBLE AS g
FROM q
ORDER BY state, quarter;
