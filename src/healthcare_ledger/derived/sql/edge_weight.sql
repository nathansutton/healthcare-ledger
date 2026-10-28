-- edge_weight: each physician edge with the weights the sources support. w_fractional is 1/k. Where a Doctors and
-- Clinicians vintage covers the quarter and lists the physician, w_listed spreads the physician over the groups CMS
-- lists them under (Medicare claims under the group in the past 12 months) and gives the others 0; elsewhere it is
-- 1/k. listed is NULL when no vintage covers the quarter. w_medicaid is the edge's share of the physician's Medicaid
-- dollars on the quarters medicaid_volume covers, NULL for a physician with none
WITH l AS (SELECT q.as_of_date, r.practitioner_pac_id, r.organization_pac_id FROM quarters q
           JOIN listed_reassignment r ON r.effective_date <= q.as_of_date AND (r.end_date IS NULL OR r.end_date > q.as_of_date)),
covered AS (SELECT DISTINCT as_of_date FROM l),
x AS (SELECT e.*, c.as_of_date IS NOT NULL AS covered, l.organization_pac_id IS NOT NULL AS is_listed
      FROM physician_edge e
      LEFT JOIN covered c USING (as_of_date)
      LEFT JOIN l USING (as_of_date, practitioner_pac_id, organization_pac_id)),
k AS (SELECT *, count(*) FILTER (WHERE is_listed) OVER (PARTITION BY as_of_date, practitioner_pac_id) AS k_listed FROM x),
m AS (SELECT as_of_date, practitioner_pac_id, organization_pac_id, medicaid_paid,
             sum(medicaid_paid) OVER (PARTITION BY as_of_date, practitioner_pac_id) AS physician_paid
      FROM medicaid_volume)
SELECT k.as_of_date, k.practitioner_pac_id, k.organization_pac_id, k.k,
       1.0 / k.k AS w_fractional,
       CASE WHEN k.covered THEN k.is_listed END AS listed,
       k.k_listed,
       CASE WHEN k.covered AND k.k_listed > 0 THEN CASE WHEN k.is_listed THEN 1.0 / k.k_listed ELSE 0 END
            ELSE 1.0 / k.k END AS w_listed,
       CASE WHEN mp.physician_paid > 0 THEN coalesce(m.medicaid_paid, 0) / mp.physician_paid END AS w_medicaid
FROM k
LEFT JOIN m USING (as_of_date, practitioner_pac_id, organization_pac_id)
LEFT JOIN (SELECT DISTINCT as_of_date, practitioner_pac_id, physician_paid FROM m) mp USING (as_of_date, practitioner_pac_id)
