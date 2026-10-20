-- NP versions turned into dated events: entry, move (a change of practice state), switch into or out
-- of sole-proprietor status, and exit. One row per event.
--
-- date is the snapshot that first showed the change. Entry and exit are symmetric: an NPI enters when it
-- first appears with an NP primary taxonomy or switches into one, and exits when it switches away from
-- one, leaves the file (close_reason = 'absent') or is deactivated. CMS's sole-proprietor code is Y, N or
-- X (not answered); N or X to Y is a switch in, and Y to N or X a switch out.
--
-- is_np is NULL on a deactivation stub; SQL's three-valued logic leaves those rows out of every
-- "is_np" and "NOT is_np" test, so a stub is never an entry and only counts through the deactivation rule.

CREATE OR REPLACE TABLE np_events AS
WITH v AS (
    SELECT *,
           row_number() OVER w = 1 AS is_first,
           lag(is_np) OVER w AS prev_np,
           lag(practice_state) OVER w AS prev_state,
           lag(sole_proprietor) OVER w AS prev_sp,
           lag(deactivation_date) OVER w AS prev_deactivation_date
    FROM np_versions
    WINDOW w AS (PARTITION BY npi ORDER BY effective_date)
)
-- entry: the first version as an NP, either a new NPI or the version whose taxonomy switched to NP
SELECT npi, effective_date AS date, 'entry' AS event, practice_state AS state, NULL::VARCHAR AS from_state,
       CASE WHEN is_first THEN 'new_npi' ELSE 'switched_taxonomy' END AS detail
FROM v WHERE is_np AND (is_first OR prev_np = false)

UNION ALL
SELECT npi, effective_date, 'move', practice_state, prev_state, NULL
FROM v WHERE is_np AND prev_state IS NOT NULL AND practice_state IS NOT NULL AND practice_state <> prev_state

UNION ALL
SELECT npi, effective_date, 'became_sole_proprietor', practice_state, NULL, prev_sp
FROM v WHERE is_np AND prev_sp IN ('N', 'X') AND sole_proprietor = 'Y'

UNION ALL
SELECT npi, effective_date, 'left_sole_proprietor', practice_state, NULL, sole_proprietor
FROM v WHERE is_np AND prev_sp = 'Y' AND sole_proprietor IN ('N', 'X')

-- exit, three ways. The state is the one the NP practiced in before leaving.
UNION ALL
SELECT npi, effective_date, 'exit', prev_state, NULL, 'switched_taxonomy'
FROM v WHERE prev_np AND NOT is_np AND deactivation_date IS NULL

UNION ALL
SELECT npi, end_date, 'exit', practice_state, NULL, 'absent'
FROM v WHERE is_np AND close_reason = 'absent'

-- a deactivation usually arrives as a stub version with no state or taxonomy, so look at the version before it
UNION ALL
SELECT npi, effective_date, 'exit', coalesce(practice_state, prev_state), NULL, 'deactivated'
FROM v WHERE deactivation_date IS NOT NULL AND prev_deactivation_date IS NULL AND (is_np OR prev_np)

ORDER BY date, npi;
