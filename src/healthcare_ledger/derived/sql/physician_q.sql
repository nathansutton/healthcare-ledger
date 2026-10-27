-- physician_q: a physician (a practitioner whose NPPES primary taxonomy is an allopathic or osteopathic
-- physician code, 20*) on each quarter's date, with their Medicare specialties and practice ZIP and state
SELECT q.as_of_date, p.pac_id, p.provider_type_codes, p.practice_zip5, p.practice_state
FROM quarters q
JOIN practitioner p ON p.effective_date <= q.as_of_date AND (p.end_date IS NULL OR p.end_date > q.as_of_date)
WHERE p.taxonomy_code LIKE '20%'
