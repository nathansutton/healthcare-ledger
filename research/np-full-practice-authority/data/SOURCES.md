# Reference data

Small public inputs, committed so the study runs from a clone plus an export directory. The notebook reads them with `read_csv`. Shared geography (the state FIPS codes, the ZCTA to county map and the cross-state county pairs) is in [`reference/`](../../../reference/SOURCES.md), with its sources and the rule for mapping a ZIP code to a county.

| File | What | Source |
|---|---|---|
| `np_fpa_dates.csv` | Month each state's nurse practitioner full practice authority (FPA) law took effect, 1998 to 2026 | McMichael and Markowitz, *Toward a Uniform Classification of Nurse Practitioner Scope of Practice Laws*, NBER WP 28192 (rev. 2023), Table 2, for 1998 to 2020; state statutes and board notices for 2021 on (Massachusetts, New York, Kansas). `coding` is `always` (FPA before 1998), `statute` (month in `fpa_effective`), or `never` (no FPA through 2026 under that coding). `partial = Y` marks Florida, whose 2020 law covers primary care NPs only. |
| `aanp_full_practice_2026.csv` | States the American Association of Nurse Practitioners classified as full practice in its May 2026 map | AANP State Practice Environment, updated 05/2026 |
| `state_population.csv` | Resident population by state and year, 2010 to 2024 | Census Bureau population estimates, Vintage 2020 (2010 to 2019) and Vintage 2024 (2020 to 2024). The notebook carries the last year forward to the year of the last snapshot. |

The two classifications disagree on Illinois, Virginia, West Virginia and Florida (FPA under McMichael and Markowitz, not under AANP) and on when Utah and Maryland count. The study runs both.
