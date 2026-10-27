-- main_group: each physician's group by plurality of Medicaid volume on the quarters medicaid_volume covers
-- (ties broken by organization PAC id); a physician with one reassignment keeps it; share is that group's
-- part of the physician's Medicaid paid (1 for a single reassignment)
WITH e AS (
    SELECT e.as_of_date, e.practitioner_pac_id, e.organization_pac_id, e.k, coalesce(v.medicaid_paid, 0) AS paid,
           sum(coalesce(v.medicaid_paid, 0)) OVER (PARTITION BY e.as_of_date, e.practitioner_pac_id) AS physician_paid
    FROM physician_edge e
    LEFT JOIN medicaid_volume v USING (as_of_date, practitioner_pac_id, organization_pac_id)
    WHERE e.as_of_date IN (SELECT DISTINCT as_of_date FROM medicaid_volume))
SELECT as_of_date, practitioner_pac_id, organization_pac_id, k,
       CASE WHEN k = 1 THEN 1.0 ELSE paid / physician_paid END AS share
FROM e
WHERE k = 1 OR paid > 0
QUALIFY row_number() OVER (PARTITION BY as_of_date, practitioner_pac_id ORDER BY paid DESC, organization_pac_id) = 1
