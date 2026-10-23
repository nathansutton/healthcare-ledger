# Reference data

Small public inputs, committed so the study runs from a clone, an export directory and the three HHS files. The notebook reads them with `read_csv`.

| File | What | Source |
|---|---|---|
| `specialty_taxonomy.csv` | The NUCC taxonomy codes that define the five physician specialties studied by Zhu et al. (2026): primary care, cardiology, dermatology, ophthalmology and psychiatry. `pattern` is a SQL `LIKE` pattern against the primary taxonomy. | NUCC Health Care Provider Taxonomy Code Set, version 25.0 (July 2025). The published study's code list is in its online appendix (Health Affairs, exhibit A2), which is behind the journal's paywall; this list is the notebook's own reading of the specialty names in the paper. |

## The HHS files (not committed)

The notebook also needs the three public extracts that HHS published on [opendata.hhs.gov](https://opendata.hhs.gov/) from the Transformed Medicaid Statistical Information System (T-MSIS), unzipped into one directory named by `HHS_MEDICAID`:

| File | Rows | What |
|---|---|---|
| `medicaid-provider-enrollment-segments.csv` | 66.0 million | One row per provider enrollment segment per state: NPI, state, start and end dates, status, plan (Medicaid or CHIP) and provider type. Segments overlapping January 2018 to December 2024. Published 24 July 2026. |
| `medicaid-provider-spending.csv` | 238.0 million | Paid outpatient and professional claims by billing NPI, servicing NPI, HCPCS code and service month, January 2018 to December 2024. Cells with fewer than 12 patients or 12 claim lines are not in the file. Published 13 February 2026. |
| `medicaid-provider-spending-ndc.csv` | 21.5 million | The same for pharmacy claims by billing NPI, prescribing NPI and NDC. Not used by this notebook. |

The row counts and the suppression floor are what the notebook measured on the files downloaded on 22 October 2026. HHS states that the data are only as accurate as what each state submitted.
