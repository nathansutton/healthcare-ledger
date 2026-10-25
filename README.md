# nppes-over-time

This project gives the history of every provider in the CMS NPPES registry, from 2007 to now.

This project does not host the CMS files and does not give you the data. It is a recipe that finds each public copy of the monthly file, records where that copy is, and puts all the copies in one format.

Each month, CMS publishes a full copy of the NPI registry. CMS keeps only the latest copy. This project reads an archive of the monthly files and makes an SCD Type 2 panel. The panel has one row for each NPI and version, with the first and last dates CMS showed that version. You can make any monthly file again exactly from the panel. It is the successor to the [NBER NPI/NPPES cumulative collection](https://www.nber.org/research/data/npinppes-cumulative-collection-preliminary).

The build uses Python, DuckDB and the CMS zip files only. The output is Parquet, and optionally gzipped CSV. A change in any published CMS column makes a new version. When an NPI leaves the file, its last version closes with `close_reason = 'absent'`. Tax ids and SSNs are removed before hashing, the 50 "Other Provider Identifier" slots are not published, and every export is scanned for tax ids and SSNs before it is released ([METHODOLOGY.md](docs/METHODOLOGY.md) section 2a).

## Quick start

You need Linux or macOS, Python 3.12 or newer, [uv](https://docs.astral.sh/uv/) and `unzip`.

```bash
uv sync
uv run nppes-over-time download     # get the recorded zips into data/raw, sha256-checked
uv run nppes-over-time load         # build data/nppes.duckdb (hours for the full archive)
uv run nppes-over-time validate     # SCD invariants, snapshot reconstruction, privacy rules
uv run nppes-over-time export --csv # write export/
uv run nppes-over-time ppef-load    # optional: add the Medicare PPEF quarters in data/ppef before export
uv run nppes-over-time pos-load     # optional: add the Provider of Services quarters in data/pos before export
uv run nppes-over-time graph-build  # optional: derive the provider graph from NPPES, PPEF and POS (METHODOLOGY section 8);
                                    # add --compendium data/ahrq after `compendium-download` for AHRQ's health systems
```

The `Makefile` has one target for each command. To extend the panel by one month and make a release, use `make monthly`. [MAINTAINING.md](docs/MAINTAINING.md) is the runbook.

## Output

| Table | Grain | Notes |
|---|---|---|
| `provider` | NPI × version | 54 scalar CMS columns |
| `provider_taxonomy` | NPI × version × slot | up to 15 slots, primary taxonomy first |
| `practice_location`, `other_name` | NPI × version × row | since July 2018 |
| `endpoint` | NPI × version × row | since March 2019 |
| `medicare_enrollment` | enrollment id × version | Medicare FFS enrollments (PECOS, the public PPEF file), quarterly from February 2016; optional |
| `medicare_practice_location`, `medicare_reassignment`, `medicare_secondary_specialty`, `medicare_additional_npi` | enrollment id × version × row | the PPEF sub-files |
| `medicare_snapshots` | quarterly file | counts and content fingerprints |
| `pos_facility` | CCN × version | Medicare-certified facilities in the Provider of Services (POS) Hospital & Non-Hospital file, annual from December 2011, quarterly from 2019; optional |
| `pos_facility_iqies` | CCN × version | facilities in the POS iQIES file, which CMS has been moving provider types into since the fourth quarter of 2023 |
| `pos_snapshots` | quarterly file | counts and content fingerprints |
| `snapshots` | monthly file | counts and content fingerprints |
| `data_dictionary` | column | CMS header for each column |
| `privacy_audit` | column | rows scanned for formatted SSNs and EINs and nine-digit numbers, and the verdict |

Each row has `effective_date` (inclusive), `end_date` (exclusive, NULL while current), `is_current`, `first_snapshot_id`, `last_snapshot_id`, `close_reason` and `version_hash`. The dates are the as-of dates of the CMS files.

The `medicare_*` tables are the same history for CMS's quarterly [Medicare Fee-For-Service Public Provider Enrollment](https://data.cms.gov/provider-characteristics/medicare-provider-supplier-enrollment/medicare-fee-for-service-public-provider-enrollment) files, 2016 to now. Their key is the PECOS enrollment id; `medicare_enrollment.npi` joins them to the NPPES tables. The dates are the extract dates of the quarterly files ([METHODOLOGY.md](docs/METHODOLOGY.md) section 6). They are in a release only when the maintainer loaded the quarters (`ppef-load`).

The `pos_*` tables are the same history for CMS's quarterly [Provider of Services](https://data.cms.gov/provider-characteristics/hospitals-and-other-facilities/provider-of-services-file-quality-improvement-and-evaluation-system) files: every hospital, nursing home, home health agency, hospice, dialysis facility, surgical center and clinic certified for Medicare, with its address, category, certification and termination dates, ownership, beds, staffing and services, keyed by the CMS Certification Number (`ccn`). CMS publishes two files per quarter since the end of 2023 and moves provider types from the old (QIES) file to the new (iQIES) one; a CCN that moves is closed with `close_reason = 'moved'` in `pos_facility` and continues in `pos_facility_iqies` ([METHODOLOGY.md](docs/METHODOLOGY.md) section 7). The dates are the last days of the quarters. Optional, like the Medicare tables (`pos-load`).

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

Each export has a `README.md` with a worked example and recipes for DuckDB, Python, R and Stata. It also has `CITATION.cff`, `data_dictionary.csv`, `snapshots.csv`, `privacy_audit.csv`, `SHA256SUMS` and `manifest.json`. The exporter writes `manifest.json` last. If it is missing, the export is not complete.

`export --monthly-files` (or `make export MONTHLY_FILES=1`) also writes the data as a monthly panel: `parquet/provider_monthly/provider_YYYY-MM.parquet`, one file per snapshot with every NPI in that month's CMS file, one row each, in the wide CMS layout without the 200 suppressed other-identifier columns. Each file is the as-of query over the version table written out. The files are large (0.3 to 0.8 GB a month, about 100 GB for the full history), so they are opt-in and Parquet only.

## Research

[`research/`](research/) has worked studies with committed outputs, one self-contained directory each: [`getting-started`](research/getting-started/) (reading the export, the as-of query, entry, exit, moves and version churn), [`np-full-practice-authority`](research/np-full-practice-authority/) (a staggered difference-in-differences study of state nurse practitioner scope-of-practice laws on the universe of NPs, month by month) and [`dobbs-obgyn-workforce`](research/dobbs-obgyn-workforce/) (a replication of a 2025 study of OB-GYN supply in states that banned abortion after *Dobbs*, extended from two years of follow-up to four). Each table a study uses is built in DuckDB SQL in the notebook, next to the reasoning for it. `uv sync --group research` installs what the notebooks need.

## Limits

The archive has 197 files, from 2007-11-12 to 2026-10-11. The files are monthly from April 2011, except May 2013. Before April 2011 there are 11 files, two to five per year, so a change in those years can be up to a year older than its `effective_date`. CMS made some edits to the full file, for example upper-case names in April 2009. These edits make a new version for almost all providers. Cosmetic edits such as `ST. LOUIS` to `ST LOUIS` also make versions.

## Status

The 2026-10-20 build (edition 2) has 9,852,261 NPIs and 31,548,829 provider versions over 197 snapshots.

## License and citation

The code license is Apache 2.0. The data files are [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/). To cite the data, use the release id and the `CITATION.cff` in your download. Also cite CMS NPPES.

To report a data problem, use the [issue form](https://github.com/nathansutton/nppes-over-time/issues/new?template=data-problem.yml).
