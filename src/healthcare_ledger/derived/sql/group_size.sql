-- group_size: each organization's physicians on each quarter's date, as a headcount and as the sum of 1/k,
-- which counts each physician once over all their groups
SELECT as_of_date, organization_pac_id, count(*) AS headcount, sum(1.0 / k) AS physicians
FROM physician_edge GROUP BY ALL
