# Ghost physicians in Medicaid

Zhu, Johnston, Hart, Polsky and McConnell (*Health Affairs*, February 2026) found that 27.7 percent of physicians enrolled in Medicaid saw no Medicaid patient in 2021, on restricted T-MSIS files for 2019 to 2021, and closed with the limit of their denominator: an NPPES record "does not confirm current clinical activity", and physicians who "retired, relocated, or shifted to nonclinical roles" stay in it. This study takes the two public T-MSIS extracts HHS released in 2026 (provider enrollment segments and provider spending, 2018 to 2024) and the monthly NPPES panel, and extends the study's enrollment table (its Exhibit 1) from three years to seven on the study's own definitions. The public spending extract drops cells with fewer than 12 patients, so it cannot observe the study's zero; the notebook shows what it does bound: the share of enrolled physicians with no visible visit is an upper bound on the ghost share, and the top tier of the study's participation measure can be approximated.

[`medicaid_ghost_physicians.ipynb`](medicaid_ghost_physicians.ipynb) has the question, the tables and how to read them.

## Running it

You need an export directory and the three HHS files, unzipped into one directory:

```bash
# from https://opendata.hhs.gov/ : Medicaid Provider Enrollment Segments, Medicaid Provider Spending (by HCPCS and by NDC)
ls $HHS_MEDICAID
# medicaid-provider-enrollment-segments.csv  medicaid-provider-spending.csv  medicaid-provider-spending-ndc.csv

uv sync --group research
export NPPES_EXPORT=/path/to/export                  # default: the repository's export/
export HHS_MEDICAID=/path/to/hhs                     # the directory above
uv run jupyter lab medicaid_ghost_physicians.ipynb
```

The first cell reads the two CSVs it needs (23 GB) into Parquet under `$HHS_MEDICAID/parquet/` once, in about a minute. After that the notebook runs end to end in under a minute on the full export.

Each table (`spells`, `state_screen`, `physician_year`, `enrollment_by_year`, `visible_em`, `participation`) is built in DuckDB SQL in the cell that first needs it, with the reasoning just above.

## Data

[`data/SOURCES.md`](data/SOURCES.md) cites the specialty code list and describes the HHS files. `tests/test_research_medicaid_ghosts.py` checks the code list, runs the spell-merging SQL on hand-built segments, runs every `con.execute` and triple-quoted `con.sql` block of the notebook on a small fixture export with small fixture HHS files, and runs the first cell's CSV-to-Parquet conversion on two small CSVs.
