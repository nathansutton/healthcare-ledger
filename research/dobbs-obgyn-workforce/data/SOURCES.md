# Reference data

Small public inputs, committed so the study runs from a clone plus an export directory. The notebook reads them with `read_csv`.

| File | What | Source |
|---|---|---|
| `states.csv` | The 50 states and DC: two-letter code, name, FIPS code, the paper's group (`restrictive`, `control`, or blank for states outside the study), the female population aged 15 to 49, and the value printed in the paper's eTable 2 | Groups: Zhu et al. (2025), eMethods (Guttmacher Institute legislation tracker, July 2024). Population: American Community Survey 2017-2021 5-year estimates, table B01001, sum of `B01001_E030` to `B01001_E039` (females 15-17 to 45-49), from the Census Bureau's table-based summary file `acsdt5y2021-b01001.dat` (www2.census.gov/programs-surveys/acs/summary_file/2021/table-based-SF/data/5YRData/). eTable 2 values transcribed from the supplement. |
| `taxonomy.csv` | NUCC taxonomy codes for each sample: `obgyn` (the primary sample), `family_medicine` (family physicians, counted when a secondary taxonomy is an `obgyn` code), `np_obstetric` and `primary_care` (the falsification test) | Zhu et al. (2025), eTable 1, for every set except `np_obstetric`. eTable 1 lists no nurse practitioner codes; `np_obstetric` is the three NUCC Health Care Provider Taxonomy codes for nurse practitioners that name obstetrics, perinatal care or women's health, our reading of "related to obstetrics". |

Zhu JM, Huntington A, McConnell KJ, Whelan AM, Aaron R, Rodriguez MI. Post-*Dobbs* decision changes in obstetrics and gynecology clinical workforce in states with abortion restrictions. *JAMA Intern Med*. 2025;185(5):598-600. doi:10.1001/jamainternmed.2024.8149. The supplement (eMethods, eTables 1 and 2) is free on the article page.

eTable 2 matches the ACS for 22 of the 26 study states; Mississippi and Missouri, and Nevada and New Hampshire, carry each other's values. The notebook shows that the paper's means and standard deviations are reproduced with the ACS values and not with the printed ones.
