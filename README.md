# healthcare-ledger

A dated history of the public U.S. registries of health care providers, and the links between them. It covers five sources:

- **NPPES**, the CMS registry of every NPI: 197 monthly files, 2007-11-12 to 2026-10-11.
- **Medicare PPEF**, the fee-for-service enrollments in PECOS and the groups each clinician bills through: 43 quarterly files, 2016-02-01 to 2026-09-29.
- **Provider of Services (POS)**, the Medicare-certified hospitals, nursing homes, home health agencies and other facilities: 38 files, 2011-12-31 to 2026-09-30 (annual at first, quarterly from 2019).
- **AHRQ Compendium of U.S. Health Systems**: the 2016, 2018 and 2020 to 2023 editions.
- **HHS Medicaid provider spending** (T-MSIS): billing volume by NPI and month, January 2018 to December 2024.

NPPES, PPEF and POS each become version tables: one row for each record and version, with the first and last dates the registry showed it. The AHRQ and Medicaid files are loaded as published. A crosswalk joins them with dates: NPI to Medicare enrollment, enrollment to PECOS associate id (one per person or legal entity) and to the facility's CMS Certification Number, and associate id or CCN to an AHRQ health system ([METHODOLOGY.md](docs/METHODOLOGY.md) section 8b).

What this adds to each source:

- **NBER** keeps the monthly NPPES files in its [NPI/NPPES cumulative collection](https://www.nber.org/research/data/npinppes-cumulative-collection-preliminary). This project turns them into one panel, and any monthly file can be made again exactly from it. The NPPES part, "NPPES over time", is the successor to that collection.
- **CMS** publishes each registry as separate files, one date at a time. It gives no history of a record and no dated link from one registry to another.
- **AHRQ** gives a list of health systems once a year, with the PECOS associate ids and CCNs in each. Here each edition is tied to the quarterly Medicare enrollments and the NPIs behind them.

This project does not host the files and does not give you the data. It is a recipe: it records where each public copy of each file is (with its sha256), downloads the copies, and builds the tables. The build uses Python and DuckDB only. The output is Parquet, and optionally gzipped CSV.

## Quick start

You need Linux or macOS, Python 3.12 or newer, [uv](https://docs.astral.sh/uv/) and `unzip`.

To try the whole pipeline on one month before you download the archive:

```bash
uv sync
uv run healthcare-ledger smoke        # or `make smoke`
```

`smoke` downloads the newest zip in `releases/sources.csv` (about 1.2 GB, sha256-checked) into `data/smoke/raw`. It then loads it, validates and verifies the warehouse against the zip, exports it and reads the export back. Everything stays under `data/smoke/`, and the run needs about 15 GB of free disk. A re-run keeps the zip and rebuilds the rest. `--snapshot YYYYMMDD` picks another month. DuckDB uses up to 80% of RAM by default; on a smaller machine pass `--memory 8GB --threads 4` (`make smoke MEMORY=8GB THREADS=4`), which finishes in about ten minutes.

The full build:

```bash
uv sync
uv run healthcare-ledger download     # get the recorded zips into data/raw, sha256-checked
uv run healthcare-ledger load         # build data/warehouse/nppes.duckdb (hours for the full archive)
uv run healthcare-ledger validate     # SCD invariants, snapshot reconstruction, privacy rules
uv run healthcare-ledger export --csv # write export/
uv run healthcare-ledger load --source-name ppef  # optional: Medicare PPEF quarters in data/ppef -> data/warehouse/ppef.duckdb
uv run healthcare-ledger load --source-name pos   # optional: Provider of Services quarters in data/pos -> data/warehouse/pos.duckdb
uv run healthcare-ledger load --source-name ahrq  # optional: AHRQ Compendium editions in data/ahrq -> data/warehouse/ahrq.duckdb
uv run healthcare-ledger load --source-name medicaid  # optional: HHS Medicaid spending in data/medicaid -> data/warehouse/medicaid.duckdb
uv run healthcare-ledger graph-build  # optional: derive the provider graph from NPPES, PPEF and POS (METHODOLOGY section 8);
                                    # add --compendium, with ahrq loaded, for AHRQ's health systems,
                                    # and --medicaid, with medicaid loaded, for each physician's main group
```

The `Makefile` has one target for each command. To extend the panel by one month and make a release, use `make monthly`. [MAINTAINING.md](docs/MAINTAINING.md) is the runbook.

## Sources

### NPPES (NPPES over time)

Each month, CMS publishes a full copy of the NPI registry, and keeps only the latest copy. The `nppes` source reads an archive of the monthly files and makes an SCD Type 2 panel. The panel has one row for each NPI and version, with the first and last dates CMS showed that version. A change in any published CMS column makes a new version. When an NPI leaves the file, its last version closes with `close_reason = 'absent'`. Tax ids and SSNs are removed before hashing, the 50 "Other Provider Identifier" slots and the always-blank EIN columns are not published, and every export is scanned for tax ids and SSNs before it is released ([METHODOLOGY.md](docs/METHODOLOGY.md) section 2a).

### Medicare PPEF and POS

The `ppef` and `pos` sources are the same history for CMS's quarterly Medicare enrollment and Provider of Services files (see *Output* below, and [METHODOLOGY.md](docs/METHODOLOGY.md) sections 6 and 7).

### AHRQ Compendium and Medicaid spending

The `ahrq` and `medicaid` sources are reference sources: dated files loaded whole, with no versions. `releases/ahrq_sources.csv` and `releases/medicaid_sources.csv` pin each file by url, size and sha256. The export does not include them; `graph-build --compendium` and `--medicaid` read them ([METHODOLOGY.md](docs/METHODOLOGY.md) sections 8a and 8c).

## Output

The tables are what the registries describe, not how CMS splits its files: one table per kind of record, whatever the number of files behind it, with codes decoded where a researcher would otherwise look them up. Each is a view over the load's version tables, one per CMS file, which stay in the warehouse as the record of what each file said ([METHODOLOGY.md](docs/METHODOLOGY.md) section 4a).

| Table | Grain | Notes |
|---|---|---|
| `provider` | NPI × version | the NPPES record of a clinician or organization: `entity_type` (`individual`, `organization`), `primary_taxonomy_code`, names, addresses, dates |
| `provider_taxonomy` | NPI × version × slot | up to 15 taxonomies with license and state, primary first |
| `provider_practice_location`, `provider_other_name` | NPI × version × row | secondary practice locations and organization other names, since July 2018 |
| `provider_endpoint` | NPI × version × row | electronic endpoints (Direct addresses, FHIR servers), since March 2019 |
| `medicare_enrollment` | enrollment id × version | Medicare fee-for-service enrollments (PECOS, the public PPEF file), quarterly from February 2016, with `enrollee_type` (`individual`, `organization`); optional |
| `medicare_practice_location`, `medicare_reassignment`, `medicare_secondary_specialty`, `medicare_additional_npi` | enrollment id × version × row | an enrollment's practice locations, the groups it reassigns its benefits to, its other specialties and NPIs |
| `facility` | CCN × version | every Medicare- or Medicaid-certified hospital, nursing home, home health agency, hospice, dialysis facility and clinic (Provider of Services), annual from December 2011, quarterly from 2019, with `facility_type` and `active`; optional |
| `snapshots`, `medicare_snapshots`, `pos_snapshots` | file | counts and content fingerprints of each monthly NPPES file and each quarter |
| `data_dictionary` | column | type, CMS header and notes for each column |
| `privacy_audit` | column | rows scanned for formatted SSNs and EINs and nine-digit numbers, and the verdict |

Every table starts with its key (`npi`, `enrollment_id` or `ccn`), `effective_date` (inclusive), `end_date` (exclusive, NULL while current) and `close_reason` (`changed` or `absent`). The dates are the dates of the CMS files: as-of dates for NPPES, extract dates for the PPEF, the last days of quarters for POS. A table with several rows per key holds every row of the version.

The `medicare_*` tables are the same history for CMS's quarterly [Medicare Fee-For-Service Public Provider Enrollment](https://data.cms.gov/provider-characteristics/medicare-provider-supplier-enrollment/medicare-fee-for-service-public-provider-enrollment) files, 2016 to now. Their key is the PECOS enrollment id; `medicare_enrollment.npi` joins them to `provider` ([METHODOLOGY.md](docs/METHODOLOGY.md) section 6). They are in a release only when the maintainer loaded the quarters (`load --source-name ppef`).

`facility` is the same history for CMS's quarterly [Provider of Services](https://data.cms.gov/provider-characteristics/hospitals-and-other-facilities/provider-of-services-file-quality-improvement-and-evaluation-system) files: every certified facility with its address, type, certification and termination dates, ownership, beds, staffing and services, keyed by the CMS Certification Number (`ccn`). Since the end of 2023 CMS has been moving provider types from its old certification system (QIES) to a new one (iQIES), and publishes a file for each; `facility` follows a CCN from one file to the other, names its type the same way in both (`facility_types.csv`), and keeps the file a version came from in `pos_file` ([METHODOLOGY.md](docs/METHODOLOGY.md) section 7). Optional, like the Medicare tables (`load --source-name pos`).

```python
import healthcare_ledger
con = healthcare_ledger.connect("export")    # a view per table, Parquet read lazily; graph="graph" adds the graph's
con.sql("SELECT entity_type, count(*) FROM provider WHERE end_date IS NULL GROUP BY 1")
# with POS loaded: SELECT facility_type, count(*) FROM facility WHERE end_date IS NULL AND active GROUP BY 1
```

```sql
-- the registry as CMS published it on 2019-01-13 (5,844,727 providers in the 2026-10-18 build)
SELECT count(*) FROM read_parquet('export/parquet/provider/*/*.parquet', hive_partitioning = true)
WHERE effective_date <= DATE '2019-01-13' AND (end_date IS NULL OR end_date > DATE '2019-01-13');

-- every version of Banner Health Services, Inc. since 2007: 12 rows, 6 distinct practice addresses
SELECT effective_date, end_date, practice_address_1, practice_city, practice_state
FROM read_parquet('export/parquet/provider/*/*.parquet', hive_partitioning = true)
WHERE npi = '1144225327' ORDER BY effective_date;
```

Every table is a directory under `parquet/`, hive-partitioned by `effective_year`, so `read_parquet('export/parquet/<table>/*/*.parquet', hive_partitioning = true)` reads any of them without the package.

Each export has a `README.md` with a worked example and recipes for DuckDB, Python, R and Stata. It also has `CITATION.cff`, `data_dictionary.csv`, `snapshots.csv`, `privacy_audit.csv`, `SHA256SUMS` and `manifest.json`. The exporter writes `manifest.json` last. If it is missing, the export is not complete.

`export --monthly-files` (or `make export MONTHLY_FILES=1`) also writes the data as a monthly panel: `parquet/provider_monthly/provider_YYYY-MM.parquet`, one file per snapshot with every NPI in that month's CMS file, one row each, in the wide CMS layout without the 200 other-identifier columns. Each file is the as-of query over the version table written out. The files are large (0.3 to 0.8 GB a month, about 100 GB for the full history), so they are opt-in and Parquet only.

## Graph and crosswalk

`graph-build` derives a provider graph from the warehouse, quarter by quarter on the PPEF dates: practitioners, organizations, hospitals and facilities, the reassignments between them, the links from enrollments to POS CCNs, and, with `--compendium`, AHRQ's health systems ([METHODOLOGY.md](docs/METHODOLOGY.md) section 8). It also writes `crosswalk.parquet`, one long table of dated edges (`from_type`, `from_id`, `to_type`, `to_id`, `effective_date`, `end_date`, `source`, `method`) from NPI to enrollment, enrollment to associate id and CCN, and associate id or CCN to system (section 8b), and the group tables that studies of physician groups share (section 8c). The graph is not part of the export; it is rebuilt from the warehouse in a few minutes.

## Research

[`research/`](research/) has worked studies with committed outputs, one self-contained directory each: [`getting-started`](research/getting-started/) (reading the export, the as-of query, entry, exit, moves and version churn), [`np-full-practice-authority`](research/np-full-practice-authority/) (a staggered difference-in-differences study of state nurse practitioner scope-of-practice laws on the universe of NPs, month by month), [`dobbs-obgyn-workforce`](research/dobbs-obgyn-workforce/) (a replication of a 2025 study of OB-GYN supply in states that banned abortion after *Dobbs*, extended from two years of follow-up to four) and [`provider-networks`](research/provider-networks/) (published measures of physician consolidation and vertical integration, rebuilt on the graph). Each table a study uses is built in DuckDB SQL in the notebook, next to the reasoning for it. `uv sync --group research` installs what the notebooks need.

## Limits

The archive has 197 files, from 2007-11-12 to 2026-10-11. The files are monthly from April 2011, except May 2013. Before April 2011 there are 11 files, two to five per year, so a change in those years can be up to a year older than its `effective_date`. CMS made some edits to the full file, for example upper-case names in April 2009. These edits make a new version for almost all providers. Cosmetic edits such as `ST. LOUIS` to `ST LOUIS` also make versions.

## Status

Nothing has been released yet. The current build has 9,852,261 NPIs and 30,017,747 provider versions over 197 monthly snapshots (2007-11-12 to 2026-10-11), 43 Medicare PPEF quarters (2016-02-01 to 2026-09-29) and 38 Provider of Services quarters (2011-12-31 to 2026-09-30).

## License and citation

The code license is Apache 2.0. The data files are [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/). To cite the data, use the release id and the `CITATION.cff` in your download. Also cite CMS NPPES.

To report a data problem, use the [issue form](https://github.com/nathansutton/healthcare-ledger/issues/new?template=data-problem.yml).
