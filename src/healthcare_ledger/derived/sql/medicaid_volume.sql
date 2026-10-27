-- medicaid_volume: the Medicaid dollars an organization's NPIs billed for a physician's NPI in calendar year Y
-- (HHS's Medicaid spending extract, self-billing excluded), on the first quarter on or after 31 December Y
WITH y AS (SELECT DISTINCT year(month) AS y FROM medicaid_spending),
q AS (SELECT y.y, min(q.as_of_date) AS as_of_date FROM y JOIN quarters q ON q.as_of_date >= make_date(y.y, 12, 31) GROUP BY 1),
pr AS (SELECT q.y, q.as_of_date, p.pac_id, p.npi FROM q
       JOIN practitioner p ON p.effective_date <= q.as_of_date AND (p.end_date IS NULL OR p.end_date > q.as_of_date)),
og AS (SELECT q.as_of_date, o.pac_id, unnest(o.npis) AS npi FROM q
       JOIN organization o ON o.effective_date <= q.as_of_date AND (o.end_date IS NULL OR o.end_date > q.as_of_date)),
s AS (SELECT year(month) AS y, billing_npi, servicing_npi, sum(paid) AS paid
      FROM medicaid_spending WHERE billing_npi <> servicing_npi GROUP BY ALL)
SELECT pr.as_of_date, pr.pac_id AS practitioner_pac_id, og.pac_id AS organization_pac_id, sum(s.paid) AS medicaid_paid
FROM s
JOIN pr ON pr.y = s.y AND pr.npi = s.servicing_npi
JOIN og ON og.as_of_date = pr.as_of_date AND og.npi = s.billing_npi
GROUP BY ALL
