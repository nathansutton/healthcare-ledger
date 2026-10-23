# OB-GYN supply after *Dobbs*

Zhu, Huntington, McConnell, Whelan, Aaron and Rodriguez (*JAMA Internal Medicine* 185(5):598-600, 2025) counted OB-GYN practitioners in NPPES per 100,000 women aged 15 to 49 and compared the 12 states that banned or near-banned abortion after *Dobbs* with 14 states whose restrictions did not change. Their difference-in-differences estimate, on the December files of 2018 to 2023 and the July 2024 file, was -3.0 per 100,000 (-4.2 percent, P = .04), with a two-year follow-up that they named as a limit.

This study rebuilds their panel from the monthly NPPES history and reproduces their Table to within a few tenths per 100,000. It runs the sensitivity analyses their supplement describes, then the same regression through July 2026. The estimate grows to -3.8 (-5.3 percent, P = .04). But the restrictive states were falling behind at the same pace before *Dobbs* as after it: the paper's own parallel-trends test puts the pre-period divergence at -0.8 per 100,000 a year (not significant), which alone would produce most of the estimate, and the monthly gap has no break at the decision.

The paper's eTable 2 prints the populations of Mississippi and Missouri, and of Nevada and New Hampshire, swapped. Its regression used the correct values.

[`dobbs_obgyn_workforce.ipynb`](dobbs_obgyn_workforce.ipynb) has the replication, the extension and how to read them.

## Running it

```bash
uv sync --group research
export NPPES_EXPORT=/path/to/export                  # default: the repository's export/
uv run jupyter lab dobbs_obgyn_workforce.ipynb
```

Each table (`states`, `taxonomy`, `study_versions`, `panel_dates`, `state_year`, `monthly`) is built in DuckDB SQL in the cell that first needs it, with the reasoning just above. The notebook runs end to end in about a minute on the full export.

## Data

[`data/SOURCES.md`](data/SOURCES.md) cites every input. `tests/test_research_dobbs_obgyn.py` checks the reference files against the paper's supplement and runs every `con.execute` and triple-quoted `con.sql` block of the notebook on a small fixture export.
