-- One row per cross-state move by an NP (destination in the 50 states or DC), with what we know about
-- the law at both ends and whether the mover set up as a sole proprietor within a year.

CREATE OR REPLACE TABLE np_moves AS
WITH moves AS (
    SELECT npi, date, state, from_state
    FROM np_events WHERE event = 'move' AND state IN (SELECT state FROM states)
),
sole_prop AS (
    SELECT npi, date
    FROM np_events WHERE event = 'became_sole_proprietor' AND state IN (SELECT state FROM states)
)
SELECT m.npi, m.date, m.from_state, m.state,
       date_diff('month', dest.fpa_effective, m.date) AS months_since_destination_fpa,
       CASE WHEN dest.coding = 'statute' AND date_diff('month', dest.fpa_effective, m.date) BETWEEN 0 AND 24
                THEN 'into FPA state, first 2 years'
            WHEN dest.coding = 'never' THEN 'into never-FPA state'
            ELSE 'other' END AS destination,
       -- FPA counts at the origin only if it was in force on the move date
       coalesce(orig.coding = 'always' OR (orig.coding = 'statute' AND orig.fpa_effective <= m.date), false) AS from_fpa_state,
       EXISTS (SELECT 1 FROM sole_prop s
               WHERE s.npi = m.npi AND s.date BETWEEN m.date AND m.date + INTERVAL 1 YEAR) AS sole_prop_within_year
FROM moves m
JOIN fpa_dates dest ON dest.state = m.state
LEFT JOIN fpa_dates orig ON orig.state = m.from_state
ORDER BY m.date, m.npi;
