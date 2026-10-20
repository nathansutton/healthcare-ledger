# Nurse practitioner full practice authority

Do state laws that let nurse practitioners (NPs) practice without physician oversight change NP supply, entry, cross-state moves or independence? The study uses every NP's practice state and sole-proprietor flag on every monthly NPPES file since April 2011. It replicates and extends the Bae et al. Maryland-Pennsylvania border comparison with a pre-period and mobility. Then it runs staggered difference-in-differences (Callaway-Sant'Anna, DID2S) across the 17 states that adopted FPA inside the monthly window, plus a border-county check for every adoption.

[`np_full_practice_authority.ipynb`](np_full_practice_authority.ipynb) has the question, the estimates and how to read them.

## Running it

```bash
uv sync --group research
export NPPES_EXPORT=/path/to/export                  # default: the repository's export/
uv run jupyter lab np_full_practice_authority.ipynb
```

Each table (`np_versions`, `np_events`, `np_state_month`, the estimation samples, `np_moves`, the border-county index) is built in DuckDB SQL in the cell that first needs it, with the reasoning just above. The notebook runs end to end in about 20 seconds on the full export.

To study another profession, change the `363L` prefix in the `np_versions` cell (for example `363A`, physician assistants) and replace `data/np_fpa_dates.csv` with that profession's laws.

## Data

[`data/SOURCES.md`](data/SOURCES.md) cites every input. `tests/test_research_np_fpa.py` checks the files, runs the notebook's event SQL on a hand-built history, and runs all of the notebook's SQL on a small fixture export.
