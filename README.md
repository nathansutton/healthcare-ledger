# healthcare-ledger

healthcare-ledger is a dated history of the public U.S. registries of health care providers. It changes each public file into SCD Type 2 tables. Each table has one row for each record and version, with the first and last dates that the registry showed that version.

The ledger has five sources:

| Source | What it lists | Files |
|---|---|---|
| **NPPES** | Every NPI in the CMS registry | 197 monthly files, 2007-11-12 to 2026-10-11 |
| **Medicare PPEF** | The fee-for-service enrollments in PECOS, and the groups through which each clinician bills | 43 quarterly files, 2016-02-01 to 2026-09-29 |
| **Provider of Services (POS)** | The Medicare-certified hospitals, nursing homes, home health agencies and other facilities | 38 files, 2011-12-31 to 2026-09-30 (annual at first, quarterly from 2019) |
| **Medicare facility Enrollments and All Owners** | The enrollments and owners of hospitals, nursing homes, home health agencies, hospices, FQHCs and RHCs | From 2022 |
| **Doctors and Clinicians** (formerly Physician Compare) | The claims-filtered list of clinicians, their groups and their hospital affiliations | 2014 to 2022 |

What the ledger adds:

- **CMS** publishes each registry as separate files, one date at a time. CMS keeps only the latest file and gives no history of a record.
- **NBER** keeps the monthly NPPES files in its [NPI/NPPES cumulative collection](https://www.nber.org/research/data/npinppes-cumulative-collection-preliminary). The ledger changes them into one panel. You can make each monthly file again, exactly, from the panel. The NPPES part, "NPPES over time", is the successor to that collection.

Each release of the tables is on Zenodo as Parquet and gzipped CSV. Download a release and query it; you do not have to build anything. This repository is also the recipe for a release. It records where each public copy of each CMS file is, with its sha256. It downloads the copies and builds the tables with Python and DuckDB. The repository does not keep the CMS files.

## Get the data

**Not published yet.** Edition 1 will be the first release. Its DOI will be here, and this section will then work as written.

1. Open the Zenodo record of the release (DOI: *to be added with edition 1*).
2. Download these files into one empty folder: `README.md`, `manifest.json`, `SHA256SUMS`, `data_dictionary.csv`, `snapshots.csv` and `parquet_provider.zip` (about 2.2 GB).
3. Unzip `parquet_provider.zip` in that folder.
4. When you need another table, download its `parquet_<table>.zip` into the same folder and unzip it. For example, `provider_taxonomy` is 0.4 GB. The full release is about 3.5 GB.
5. To check the files, run `sha256sum -c --ignore-missing SHA256SUMS`. On Windows, run `certutil -hashfile <file> SHA256`.
6. Install [DuckDB](https://duckdb.org/docs/installation/): the CLI, or `pip install duckdb`. You do not need this package to read the data.
7. In that folder, run this query:

```sql
-- every version of one home health agency since 2008: 14 rows
SELECT effective_date, end_date, practice_city, practice_state
FROM read_parquet('parquet/provider/*/*.parquet', hive_partitioning = true)
WHERE npi = '1205089596' ORDER BY effective_date;
```

```
effective_date  end_date    practice_city     practice_state
2008-11-10      2009-03-08  Niles             IL
2009-03-08      2010-01-11  NILES             IL
2010-01-11      2011-04-11  ADDISON           IL
...
2021-11-07      2025-02-09  SOUTH BARRINGTON  IL
2025-02-09      NULL        SOUTH BARRINGTON  IL
```

If you get `IO Error: No files found that match the pattern "parquet/provider/*/*.parquet"`, DuckDB is not in the folder with the unzipped files. Go to that folder and run the query again.

The `README.md` of the release continues from here. It shows the registry as of a date, each table, and recipes for Python, R and Stata.

To read the data from Python, run `pip install git+https://github.com/nathansutton/healthcare-ledger`. Then `healthcare_ledger.connect("<that folder>")` gives one view for each table (see *Output*). If you query a table that you did not download, the error tells you which zip to get.

To cite the data, use the release id. It is in `manifest.json` and in the title of the record. Do not cite the concept DOI, because it moves to each new release.

## Build it yourself

You need Linux or macOS, Python 3.12 or newer, [uv](https://docs.astral.sh/uv/) and `unzip`.

### Try one month

Before you download the full archive, try the pipeline on one month:

```bash
uv sync
uv run healthcare-ledger smoke        # or `make smoke`
```

`smoke` does these steps, all in `data/smoke/`:

1. It downloads the newest zip in `releases/sources.csv` into `data/smoke/raw` (about 1.2 GB) and checks its sha256.
2. It loads the zip into a warehouse.
3. It validates the warehouse and verifies it against the zip.
4. It exports the warehouse and reads the export back.

The run needs about 15 GB of free disk. If you run it again, it keeps the zip and builds the rest again. To use a different month, add `--snapshot YYYYMMDD`.

By default, DuckDB uses up to 80% of RAM. On a smaller computer, add `--memory 8GB --threads 4` (or `make smoke MEMORY=8GB THREADS=4`). The run then takes about ten minutes.

### Full build

```bash
uv sync
uv run healthcare-ledger download     # get the recorded zips into data/raw, sha256-checked
uv run healthcare-ledger load         # build data/warehouse/nppes.duckdb (hours for the full archive)
uv run healthcare-ledger validate     # SCD invariants, snapshot reconstruction, privacy rules
uv run healthcare-ledger export --csv # write export/
uv run healthcare-ledger load --source-name ppef  # optional: Medicare PPEF quarters in data/ppef -> data/warehouse/ppef.duckdb
uv run healthcare-ledger load --source-name pos   # optional: Provider of Services quarters in data/pos -> data/warehouse/pos.duckdb
uv run healthcare-ledger load --source-name ownership           # optional: All Owners releases in data/ownership
uv run healthcare-ledger load --source-name doctors_clinicians  # optional: Doctors and Clinicians archive files in data/dac
```

The `Makefile` has one target for each command. To add one month to the panel and make a release, use `make monthly`. [MAINTAINING.md](docs/MAINTAINING.md) is the runbook.

## Sources

### NPPES (NPPES over time)

Each month, CMS publishes a full copy of the NPI registry and keeps only the latest copy. The `nppes` source reads an archive of the monthly files and makes an SCD Type 2 panel:

- The panel has one row for each NPI and version, with the first and last dates that CMS showed that version.
- A change in any published CMS column makes a new version.
- When an NPI leaves the file, its last version closes with `close_reason = 'absent'`.

Privacy rules ([METHODOLOGY.md](docs/METHODOLOGY.md) section 2a):

- The loader removes tax ids and SSNs before it calculates the hashes.
- The release does not include the 50 "Other Provider Identifier" slots or the EIN columns, which are always blank.
- The exporter scans each export for tax ids and SSNs before the release.

### Medicare PPEF and POS

The `ppef` and `pos` sources make the same type of history from the quarterly CMS Medicare enrollment files and Provider of Services files. See *Output* below, and [METHODOLOGY.md](docs/METHODOLOGY.md) sections 6 and 7.

### Facility ownership and Doctors and Clinicians

The `ownership` source makes the same type of history from the CMS Enrollments and All Owners files. The `doctors_clinicians` source makes it from the archived Doctors and Clinicians files. See [METHODOLOGY.md](docs/METHODOLOGY.md) sections 7a and 7b.

### Add a source from a different package

A different installed package can add a source under the `healthcare_ledger.sources` entry point (see `src/healthcare_ledger/sources/__init__.py`). The commands `download`, `load`, `validate` and `status` then include that source. The release never includes such a source, because the export uses only the versioned sources above.

## Output

Each table shows one type of record that a registry describes. The tables do not copy the CMS file layout:

- One table holds one type of record, from all the CMS files that contain it.
- Codes are decoded where a researcher must otherwise look them up.

Each table is a view over the version tables of the load, one for each CMS file. The version tables stay in the warehouse as the record of each file ([METHODOLOGY.md](docs/METHODOLOGY.md) section 4a).

| Table | Grain | Notes |
|---|---|---|
| `provider` | NPI × version | the NPPES record of a clinician or organization: `entity_type` (`individual`, `organization`), `primary_taxonomy_code`, names, addresses, dates |
| `provider_taxonomy` | NPI × version × slot | up to 15 taxonomies with license and state, primary first |
| `provider_practice_location`, `provider_other_name` | NPI × version × row | secondary practice locations and organization other names, since July 2018 |
| `provider_endpoint` | NPI × version × row | electronic endpoints (Direct addresses, FHIR servers), since March 2019 |
| `medicare_enrollment` | enrollment id × version | Medicare fee-for-service enrollments (PECOS, the public PPEF file), quarterly from February 2016, with `enrollee_type` (`individual`, `organization`); optional |
| `medicare_practice_location`, `medicare_reassignment`, `medicare_secondary_specialty`, `medicare_additional_npi` | enrollment id × version × row | the practice locations of an enrollment, the groups to which it reassigns its benefits, its other specialties and its other NPIs |
| `facility` | CCN × version | every Medicare- or Medicaid-certified hospital, nursing home, home health agency, hospice, dialysis facility and clinic (Provider of Services), annual from December 2011, quarterly from 2019, with `facility_type` and `active`; optional |
| `medicare_facility_enrollment`, `medicare_facility_owner` | enrollment id × version (× row) | the Medicare enrollments of facilities and their owners (Enrollments and All Owners files), from 2022, with `facility_type`; optional |
| `doctors_clinicians` | NPI × version × row | clinicians with their groups, practice addresses and hospital affiliations (Doctors and Clinicians files), 2014 to 2022; optional |
| `snapshots`, `medicare_snapshots`, `pos_snapshots` | file | counts and content fingerprints of each monthly NPPES file and each quarter |
| `data_dictionary` | column | type, CMS header and notes for each column |
| `privacy_audit` | column | rows scanned for formatted SSNs and EINs and nine-digit numbers, and the verdict |

Each table starts with these columns:

- its key: `npi`, `enrollment_id` or `ccn`
- `effective_date`: the first date of the version (inclusive)
- `end_date`: the date the version stopped (exclusive; NULL while the version is current)
- `close_reason`: `changed` or `absent`

The dates are the dates of the CMS files: the as-of dates for NPPES, the extract dates for the PPEF, and the last day of the quarter for POS. If a table has more than one row for each key, it holds all the rows of the version.

The `medicare_*` tables make the same history from the quarterly CMS [Medicare Fee-For-Service Public Provider Enrollment](https://data.cms.gov/provider-characteristics/medicare-provider-supplier-enrollment/medicare-fee-for-service-public-provider-enrollment) files, from 2016. Their key is the PECOS enrollment id. Use `medicare_enrollment.npi` to join them to `provider` ([METHODOLOGY.md](docs/METHODOLOGY.md) section 6). A release includes them only if the maintainer loaded the quarters (`load --source-name ppef`).

`facility` makes the same history from the quarterly CMS [Provider of Services](https://data.cms.gov/provider-characteristics/hospitals-and-other-facilities/provider-of-services-file-quality-improvement-and-evaluation-system) files. It lists each certified facility with its address, type, certification and termination dates, ownership, beds, staff and services. Its key is the CMS Certification Number (`ccn`). Since the end of 2023, CMS moves provider types from its old certification system (QIES) to a new system (iQIES), and publishes one file for each system. `facility` does these things ([METHODOLOGY.md](docs/METHODOLOGY.md) section 7):

- It follows a CCN from one file to the other.
- It uses the same type names for the two files (`facility_types.csv`).
- It keeps the file of each version in `pos_file`.

`facility` is optional, as are the Medicare tables (`load --source-name pos`).

```python
import healthcare_ledger
con = healthcare_ledger.connect("export")    # a view per table, Parquet read lazily
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

Each table is a directory under `parquet/`, partitioned by `effective_year` (hive partitions). Thus `read_parquet('export/parquet/<table>/*/*.parquet', hive_partitioning = true)` reads each table without the package.

Each export has a `README.md` with a worked example and recipes for DuckDB, Python, R and Stata. It also has `CITATION.cff`, `data_dictionary.csv`, `snapshots.csv`, `privacy_audit.csv`, `SHA256SUMS` and `manifest.json`. The exporter writes `manifest.json` last. If `manifest.json` is not there, the export is not complete.

### Monthly files

`export --monthly-files` (or `make export MONTHLY_FILES=1`) also writes the data as a monthly panel: `parquet/provider_monthly/provider_YYYY-MM.parquet`.

- There is one file for each snapshot, with one row for each NPI in the CMS file of that month.
- The files use the wide CMS layout, without the 200 other-identifier columns.
- Each file is the as-of query over the version table.

The files are large: 0.3 to 0.8 GB for each month, and about 100 GB for the full history. Thus you must ask for them, and they are Parquet only.

## Examples

[`examples/getting-started`](examples/getting-started/) is a notebook tour of the export, with the outputs committed. It shows how to read the export with DuckDB, the as-of query, the history of one provider, entry, exit, moves and version churn. To install what it needs, run `uv sync --group notebook`.

## Limits

- The archive has 197 files, from 2007-11-12 to 2026-10-11.
- The files are monthly from April 2011, except May 2013.
- Before April 2011 there are 11 files, two to five for each year. Thus a change in those years can be up to a year older than its `effective_date`.
- CMS made some edits to the full file, for example upper-case names in April 2009. These edits make a new version for almost all providers.
- Cosmetic edits, for example `ST. LOUIS` to `ST LOUIS`, also make versions.

## Status

There is no release yet. The current build has:

- 9,852,261 NPIs and 30,017,747 provider versions, over 197 monthly snapshots (2007-11-12 to 2026-10-11)
- 43 Medicare PPEF quarters (2016-02-01 to 2026-09-29)
- 38 Provider of Services quarters (2011-12-31 to 2026-09-30)

## License and citation

The code license is Apache 2.0. The data files are [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/). To cite the data, use the release id and the `CITATION.cff` in your download. Also cite CMS NPPES.

To tell us about a data problem, use the [issue form](https://github.com/nathansutton/healthcare-ledger/issues/new?template=data-problem.yml).
