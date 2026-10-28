-- medicare_volume: a physician's Medicare Part B billing in calendar year Y (CMS's Medicare Physician & Other
-- Practitioners by Provider and Service file, by the practitioner's NPI), on the first quarter on or after 31 December
-- Y: allowed dollars and services, and the office and outpatient evaluation and management visits (99201-99205,
-- 99211-99215) billed in a facility setting (place of service F: a hospital outpatient department or other facility)
-- and in an office (O); facility_em_share is the facility part of those visits
WITH y AS (SELECT DISTINCT year AS y FROM medicare_service),
q AS (SELECT y.y, min(q.as_of_date) AS as_of_date FROM y JOIN quarters q ON q.as_of_date >= make_date(y.y, 12, 31) GROUP BY 1),
pr AS (SELECT q.y, q.as_of_date, p.pac_id, p.npi FROM q
       JOIN practitioner p ON p.effective_date <= q.as_of_date AND (p.end_date IS NULL OR p.end_date > q.as_of_date)
       SEMI JOIN physician_q f ON f.as_of_date = q.as_of_date AND f.pac_id = p.pac_id),
s AS (SELECT year, npi, sum(allowed) AS allowed, sum(services) AS services,
             sum(services) FILTER (WHERE em AND place_of_service = 'F') AS em_facility,
             sum(services) FILTER (WHERE em AND place_of_service = 'O') AS em_office
      FROM (SELECT *, hcpcs IN ('99201', '99202', '99203', '99204', '99205', '99211', '99212', '99213', '99214', '99215') AS em
            FROM medicare_service)
      GROUP BY ALL)
SELECT pr.as_of_date, pr.pac_id AS practitioner_pac_id, s.year, s.allowed, s.services,
       coalesce(s.em_facility, 0) AS em_facility, coalesce(s.em_office, 0) AS em_office,
       coalesce(s.em_facility, 0) / nullif(coalesce(s.em_facility, 0) + coalesce(s.em_office, 0), 0) AS facility_em_share
FROM s JOIN pr ON pr.y = s.year AND pr.npi = s.npi
