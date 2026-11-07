# Getting started

[`getting_started.ipynb`](getting_started.ipynb) is a tour of the export. It shows:

- the snapshots
- the history of one provider
- the as-of query, which makes each monthly file again
- the growth of the registry by provider family
- when providers enter and exit
- moves between states, and how precisely the ledger dates them
- which versions are CMS housekeeping
- what an archive with one file each year does not show
- how to read the export from other tools

All the queries are DuckDB SQL over the Parquet files, in the notebook cells. The first cell makes one view for each table. To start your own analysis, copy that cell. The notebook runs in about ten seconds on the full export.

The notebook needs only the downloaded export. To open it, run:

```bash
uv sync --group notebook
LEDGER_EXPORT=/path/to/export uv run jupyter lab getting_started.ipynb
```
