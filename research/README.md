# Research

Worked studies that run against an export directory. Each subdirectory is one self-contained project: its question, its reference data and its notebook. The notebook outputs are committed, so GitHub shows the figures without the data.

| Project | What it shows |
|---|---|
| [`getting-started/`](getting-started/) | How to read the export with DuckDB, the as-of query, one provider's history, entry, exit and moves, which version churn is CMS housekeeping, and what a monthly archive shows that an annual one hides. |
| [`np-full-practice-authority/`](np-full-practice-authority/) | Do state full practice authority laws change nurse practitioner supply, entry, cross-state moves or independence? Replicates and extends a published Maryland-Pennsylvania border comparison, then runs staggered difference-in-differences across every adoption since 2011. |
| [`medicaid-ghost-physicians/`](medicaid-ghost-physicians/) | A quarter of Medicaid-enrolled physicians saw no Medicaid patient in 2021 (Health Affairs, 2026). Extends the study's enrollment table from 2019-2021 to 2018-2024 with the public T-MSIS extracts and the monthly NPPES history, on the study's definitions, and shows what the public spending extract can and cannot say about its ghost share. Needs the three HHS files from opendata.hhs.gov. |

## Running them

```bash
uv sync --group research                  # adds jupyterlab, pandas, matplotlib, pyfixest, differences
export NPPES_EXPORT=/path/to/export       # default: the repository's export/ directory
uv run jupyter lab research/
```

To re-execute a notebook in place, from its directory:

```bash
uv run jupyter nbconvert --to notebook --execute --inplace np_full_practice_authority.ipynb
```

## How a project is laid out

```
<project>/
  README.md        the question, and how to run it
  data/            small public inputs, with SOURCES.md citing each one
  <project>.ipynb  the argument, the SQL that builds each table, the estimates and the figures
```

Each table is built in DuckDB SQL in the notebook cell where the argument needs it, with the reasoning in the markdown just above. Python is used only for the regressions and the plots. To start a new project, copy a directory and keep its first code cell, which opens the export.
