# Nurse practitioner full practice authority

Do state laws that let nurse practitioners (NPs) practice without physician oversight change NP supply, entry, cross-state moves or independence? The study uses every NP's practice state and sole-proprietor flag on every monthly NPPES file since April 2011. It replicates and extends the Bae et al. Maryland-Pennsylvania border comparison with a pre-period and mobility. Then it runs staggered difference-in-differences (Callaway-Sant'Anna, DID2S) across the 17 states that adopted FPA inside the monthly window, plus a border-county check for every adoption.

[`np_full_practice_authority.ipynb`](np_full_practice_authority.ipynb) has the question, the estimates and how to read them.

## Running it

```bash
uv sync --group research
export NPPES_EXPORT=/path/to/export                  # default: the repository's export/
uv run python pipeline.py                            # builds build/np_fpa.duckdb in a few seconds
uv run jupyter lab np_full_practice_authority.ipynb  # its first cell runs the pipeline too
```

`build/np_fpa.duckdb` is an ordinary DuckDB file, so the tables can also be queried from the DuckDB CLI, R or Stata.

## The pipeline

`pipeline.py` runs these files in order. Each one reads only the tables made before it.

| SQL | Makes | What |
|---|---|---|
| [`export_views.sql`](sql/export_views.sql) | `provider`, `provider_taxonomy`, `snapshots` | temporary views over the export's Parquet and `snapshots.csv` |
| [`reference_data.sql`](sql/reference_data.sql) | `states`, `monthly_snapshots`, `fpa_dates`, `state_population`, `zcta_county`, `border_county_pairs` | the files in `data/`, typed and joined |
| [`np_versions.sql`](sql/np_versions.sql) | `np_versions` | every version of every individual whose primary taxonomy was ever an NP code (`363L`) |
| [`np_events.sql`](sql/np_events.sql) | `np_events` | entry, move, sole-proprietor switches and exit, dated to the snapshot that first showed them |
| [`np_state_panel.sql`](sql/np_state_panel.sql) | `np_state_stock`, `np_state_month`, `did_state_month`, `did_state_quarter` | the state-month panel, and the monthly and quarterly estimation samples |
| [`np_moves.sql`](sql/np_moves.sql) | `np_moves` | one row per cross-state move, with the law at both ends |
| [`np_border_counties.sql`](sql/np_border_counties.sql) | `np_county_stock`, `fpa_cohorts`, `border_cohort_counties`, `border_index` | NPs in each adopting state's border counties against the untreated counties across the line |

To study another profession, change the `363L` prefix in `np_versions.sql` (for example `363A`, physician assistants) and replace `data/np_fpa_dates.csv` with that profession's laws.

## Data

[`data/SOURCES.md`](data/SOURCES.md) cites every input. `tests/test_research_np_fpa.py` checks the files, runs `np_events.sql` on a hand-built history, and runs the whole pipeline on a small fixture export.
