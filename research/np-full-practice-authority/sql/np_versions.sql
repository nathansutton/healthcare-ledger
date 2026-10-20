-- Every version of every individual whose primary taxonomy was ever a nurse practitioner code.
--
-- One row per (npi, effective_date). is_np says whether the primary taxonomy of *that* version is an
-- NP code, so a nurse who was an RN first has is_np = false until the switch. "Individual" is decided
-- per NPI (any version with entity_type = '1'), because a deactivated NPI's final stub version has no
-- entity type, and that stub is the version that carries deactivation_date. On the stub is_np is NULL.
--
-- 363L is Nurse Practitioner (all specialties) in the NUCC taxonomy. Swap the prefix to study another
-- profession, for example 363A (physician assistants) or 1835 (pharmacists).

CREATE OR REPLACE TABLE np_versions AS
WITH individuals AS (
    SELECT DISTINCT npi FROM provider WHERE entity_type = '1'
),
ever_np AS (
    SELECT DISTINCT t.npi
    FROM provider_taxonomy t
    JOIN individuals USING (npi)
    WHERE t.slot = 1 AND t.taxonomy_code LIKE '363L%'
)
SELECT p.npi, p.effective_date, p.end_date, p.close_reason,
       p.practice_state, p.practice_postal_code, p.sole_proprietor, p.sex,
       p.enumeration_date, p.deactivation_date, p.last_update_date,
       t.taxonomy_code AS primary_taxonomy,
       t.taxonomy_code LIKE '363L%' AS is_np
FROM provider p
JOIN ever_np USING (npi)
LEFT JOIN provider_taxonomy t                -- slot 1 is the primary taxonomy
  ON t.npi = p.npi AND t.effective_date = p.effective_date AND t.slot = 1
ORDER BY p.npi, p.effective_date;
