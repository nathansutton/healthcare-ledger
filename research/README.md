# Research

Worked studies that run against an export directory. Each subdirectory is one self-contained project: its question, its reference data and its notebook. The notebook outputs are committed, so GitHub shows the figures without the data.

| Project | What it shows |
|---|---|
| [`getting-started/`](getting-started/) | How to read the export with DuckDB, the as-of query, one provider's history, entry, exit and moves, which version churn is CMS housekeeping, and what a monthly archive shows that an annual one hides. |
| [`np-full-practice-authority/`](np-full-practice-authority/) | Do state full practice authority laws change nurse practitioner supply, entry, cross-state moves or independence? Replicates and extends a published Maryland-Pennsylvania border comparison, then runs staggered difference-in-differences across every adoption since 2011. |
| [`dobbs-obgyn-workforce/`](dobbs-obgyn-workforce/) | Did OB-GYN supply fall in the states that banned abortion after *Dobbs*? Replicates a 2025 JAMA Internal Medicine difference-in-differences study from public data, then extends it from two years of follow-up to four. The estimate grows, but the gap between the two groups of states was widening at the same pace before the decision, and it still is with the comparison states, file dates and denominators a published critique proposes. |
| [`provider-networks/`](provider-networks/) | A graph of practitioners, the organizations they bill Medicare through and the hospitals behind them, 2016 to 2026. Replicates the group-size trend of two *Health Affairs* papers and extends it a decade (physicians in groups of 100 or more: 46 to 67 percent), shows that a Medicare-only HHI trend changes sign with how multi-group physicians are counted and is flat once Medicaid billing picks each physician's main group, and tracks a lower bound on hospital integration. |

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
