# Research

Worked studies that run against an export directory. Each subdirectory is one self-contained project: its question, its reference data, its pipeline and its notebook. The notebook outputs are committed, so GitHub shows the figures without the data.

| Project | What it shows |
|---|---|
| [`getting-started/`](getting-started/) | How to read the export with DuckDB, the as-of query, one provider's history, entry, exit and moves, which version churn is CMS housekeeping, and what a monthly archive shows that an annual one hides. |
| [`np-full-practice-authority/`](np-full-practice-authority/) | Do state full practice authority laws change nurse practitioner supply, entry, cross-state moves or independence? Replicates and extends a published Maryland-Pennsylvania border comparison, then runs staggered difference-in-differences across every adoption since 2011. |

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
  README.md        the question, the design, and what each file does
  data/            small public inputs, with SOURCES.md citing each one
  sql/             the data pipeline, one DuckDB SQL file per stage
  pipeline.py      runs sql/ in order into build/<project>.duckdb (git-ignored)
  <project>.ipynb  estimation and figures, reading the tables the SQL built
```

The data work lives in SQL, so every derived table can be read, checked and rerun without Python. The notebook does what SQL is bad at: regressions and plots. To start a new project, copy `np-full-practice-authority/`, keep `sql/export_views.sql`, and replace the rest.
