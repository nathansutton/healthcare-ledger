# nppes-over-time

This project gives the history of every provider in the CMS NPPES registry, from 2007 to now.

This project does not host the CMS files and does not give you the data. It is a recipe that finds each public copy of the monthly file, records where that copy is, and puts all the copies in one format.

Each month, CMS publishes a full copy of the NPI registry. CMS keeps only the latest copy. This project reads an archive of the monthly files and makes an SCD Type 2 panel. The panel has one row for each NPI and version, with the first and last dates CMS showed that version. You can make any monthly file again exactly from the panel. It is the successor to the [NBER NPI/NPPES cumulative collection](https://www.nber.org/research/data/npinppes-cumulative-collection-preliminary).

The build uses Python, DuckDB and the CMS zip files only. The output is Parquet, and optionally gzipped CSV. A change in any of the 330 CMS columns makes a new version. When an NPI leaves the file, its last version closes with `close_reason = 'absent'`. Tax ids are removed before hashing ([METHODOLOGY.md](docs/METHODOLOGY.md) section 2a).

## Quick start

You need Linux or macOS, Python 3.12 or newer, [uv](https://docs.astral.sh/uv/) and `unzip`.

```bash
uv sync
uv run nppes-over-time download     # get the recorded zips into data/raw, sha256-checked
uv run nppes-over-time load         # build data/nppes.duckdb (hours for the full archive)
uv run nppes-over-time validate     # SCD invariants, snapshot reconstruction, privacy rules
uv run nppes-over-time export --csv # write export/
```

The `Makefile` has one target for each command. To extend the panel by one month and make a release, use `make monthly`. [MAINTAINING.md](docs/MAINTAINING.md) is the runbook.

## Output

| Table | Grain | Notes |
|---|---|---|
| `provider` | NPI × version | 54 scalar CMS columns |
| `provider_taxonomy` | NPI × version × slot | up to 15 slots, primary taxonomy first |
| `provider_other_identifier` | NPI × version × slot | up to 50 slots; tax ids removed |
| `practice_location`, `other_name` | NPI × version × row | since July 2018 |
| `endpoint` | NPI × version × row | since March 2019 |
| `snapshots` | monthly file | counts and content fingerprints |
| `data_dictionary` | column | CMS header for each column |

Each row has `effective_date` (inclusive), `end_date` (exclusive, NULL while current), `is_current`, `first_snapshot_id`, `last_snapshot_id`, `close_reason` and `version_hash`. The dates are the as-of dates of the CMS files.

```sql
-- the registry as CMS published it on 2019-01-13 (5,844,727 providers in the 2026-10-18 build)
SELECT count(*) FROM read_parquet('export/parquet/provider/*/*.parquet', hive_partitioning = true)
WHERE effective_date <= DATE '2019-01-13' AND (end_date IS NULL OR end_date > DATE '2019-01-13');

-- every version of Banner Health Services, Inc. since 2007: 12 rows, 6 distinct practice addresses
SELECT effective_date, end_date, practice_address_1, practice_city, practice_state
FROM read_parquet('export/parquet/provider/*/*.parquet', hive_partitioning = true)
WHERE npi = '1144225327' ORDER BY effective_date;
```

```python
import duckdb
con = duckdb.connect()
con.sql("SELECT count(*) FROM read_parquet('export/parquet/provider/*/*.parquet', hive_partitioning=true)")
```

Each export has a `README.md` with a worked example and recipes for DuckDB, Python, R and Stata. It also has `CITATION.cff`, `data_dictionary.csv`, `snapshots.csv`, `SHA256SUMS` and `manifest.json`. The exporter writes `manifest.json` last. If it is missing, the export is not complete.

`export --monthly-files` (or `make export MONTHLY_FILES=1`) also writes the data as a monthly panel: `parquet/provider_monthly/provider_YYYY-MM.parquet`, one file per snapshot with every NPI in that month's CMS file, one row each, in the wide 330-column CMS layout. Each file is the as-of query over the version table written out. The files are large (0.3 to 0.8 GB a month, about 100 GB for the full history), so they are opt-in and Parquet only.

## Notebooks

[`notebooks/`](notebooks/) has two worked examples with committed outputs: `00_getting_started` (reading the export, the as-of query, entry, exit, moves and version churn) and `01_nurse_practitioners_full_practice_authority` (a staggered difference-in-differences study of state nurse practitioner scope-of-practice laws on the universe of NPs, month by month). `uv sync --group notebooks` installs what they need.

## Limits

The archive has 197 files, from 2007-11-12 to 2026-10-11. The files are monthly from April 2011, except May 2013. Before April 2011 there are 11 files, two to five per year, so a change in those years can be up to a year older than its `effective_date`. CMS made some edits to the full file, for example upper-case names in April 2009. These edits make a new version for almost all providers. Cosmetic edits such as `ST. LOUIS` to `ST LOUIS` also make versions.

## Status

The 2026-10-20 build (edition 2) has 9,852,261 NPIs and 31,548,829 provider versions over 197 snapshots.

## License and citation

The code license is Apache 2.0. The data files are [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/). To cite the data, use the release id and the `CITATION.cff` in your download. Also cite CMS NPPES.

To report a data problem, use the [issue form](https://github.com/nathansutton/nppes-over-time/issues/new?template=data-problem.yml).
