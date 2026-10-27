-- physician_edge: a physician's reassignments in force on each quarter's date, with k, the number of
-- organizations the physician reassigns to that quarter
SELECT e.*, count(*) OVER (PARTITION BY e.as_of_date, e.practitioner_pac_id) AS k
FROM (SELECT q.as_of_date, r.practitioner_pac_id, r.organization_pac_id
      FROM quarters q
      JOIN reassignment r ON r.effective_date <= q.as_of_date AND (r.end_date IS NULL OR r.end_date > q.as_of_date)) e
SEMI JOIN physician_q p ON p.as_of_date = e.as_of_date AND p.pac_id = e.practitioner_pac_id
