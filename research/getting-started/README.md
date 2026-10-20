# Getting started

[`getting_started.ipynb`](getting_started.ipynb) is a tour of the export: the snapshots, one provider's history, the as-of query that rebuilds any monthly file, registry growth by provider family, entry and exit timing, cross-state moves and how precisely they are dated, which versions are CMS housekeeping, and what a once-a-year archive would miss.

Everything is DuckDB SQL over the Parquet files, written in the notebook cells. The first cell creates one view per table; copy it to start your own analysis. It runs in about ten seconds on the full export.

```bash
uv sync --group research
NPPES_EXPORT=/path/to/export uv run jupyter lab getting_started.ipynb
```
