# Notebooks

Worked examples that run against an export directory. Outputs are committed, so GitHub renders the figures without the data.

| Notebook | What it shows |
|---|---|
| [`00_getting_started.ipynb`](00_getting_started.ipynb) | How to read the export with DuckDB, the as-of query, one provider's history, entry, exit and moves, which version churn is CMS housekeeping, and what a monthly archive shows that an annual one hides. |
| [`01_nurse_practitioners_full_practice_authority.ipynb`](01_nurse_practitioners_full_practice_authority.ipynb) | A research question on the universe of nurse practitioners: do state full practice authority laws change NP supply, entry, cross-state moves or independence? Replicates and extends a published Maryland-Pennsylvania border comparison, then runs staggered difference-in-differences across every adoption since 2011. |

## Running them

```bash
uv sync --group notebooks                 # adds jupyterlab, pandas, matplotlib, pyfixest, differences
export NPPES_EXPORT=/path/to/export       # default: ../export (the repo's own export directory)
cd notebooks && uv run jupyter lab
```

Each notebook executes end to end in about a minute on the full export with 16 threads. To re-execute in place:

```bash
cd notebooks && uv run jupyter nbconvert --to notebook --execute --inplace 01_nurse_practitioners_full_practice_authority.ipynb
```

`nppes_nb.py` holds the helpers the notebooks share (`connect`, `as_of`, `live_on`, `np_versions`, `np_events`, `np_stock_by_month`, `np_stock_by_state_month`). `data/` holds the small reference files (law dates, population, ZIP to county, border counties); `data/SOURCES.md` cites each one. `tests/test_notebooks_data.py` checks them.
