# Maintaining

Run all commands from the checkout. The computer must have approximately 32 GB of RAM and 150 GB of free disk space. To limit DuckDB, set `THREADS=` and `MEMORY=`.

## First run

The warehouse is the directory `data/warehouse/`. It has one DuckDB file for each source: `nppes.duckdb`, and also `ppef.duckdb`, `pos.duckdb`, `ownership.duckdb` and `doctors_clinicians.duckdb` after you load those sources. Each `load` writes only the file of its source. All other commands attach the files that are present and read them as one catalog. NPPES is required. The other sources are optional.

You can get a warehouse in two ways:

- To use an existing warehouse, copy its files into `data/warehouse/`. Then run `make validate`.
- To build a new warehouse from the zips, run these commands:

```bash
make install
make download        # gets 191 zips from CMS, NBER and the Wayback Machine; MIRROR=<url> gets the 6 archive-only zips
make check-sources   # must show 197 of 197
make load            # 3 to 4 hours
make validate
make export          # must show "continuity with release ...: ok"
```

`releases/sources.csv` gives the sha256 and the URL of each zip. These are the origins of the zips:

- CMS serves the zips from January 2021 and later. In October 2026, CMS removed the older zips.
- Of the 127 older zips, 105 come from the [NBER NPI/NPPES cumulative collection](https://data.nber.org/npi/). They are in the per-year directories, `backfiles/` and `nppes/zip-orig/`.
- 16 older zips come from Wayback Machine captures of the CMS hosts.
- Six zips have no public source: October and November 2019, and April, August, September and November 2020. `make download` reports them as missing. `MIRROR=<url>` gets them from a private copy.

CAUTION: CMS can remove more zips. Before each release, make sure that the recorded URLs still answer.

If CMS sends "Access Denied", wait one hour. Then run `make download` again.

## Monthly run

CMS publishes a new file in the second week of each month. Run these commands:

```bash
git pull
make monthly
```

`make monthly` downloads, loads, validates, verifies and exports. If a check fails, the warehouse goes back to the last release, and `make monthly` exports nothing. In that case, do these steps:

1. Read `data/work/monthly.log`.
2. Repair the cause.
3. Run `make monthly` again.

After a successful run, do these steps:

1. Copy `export/` to the host. Put it in a directory that has the release id as its name.
2. Commit `releases/<id>/` and `releases/sources.csv`.
3. Merge them before the next monthly run.

## Medicare PPEF quarters

The `medicare_*` tables come from the CMS quarterly Medicare Fee-For-Service Public Provider Enrollment extracts. These tables are optional. A release has them only if the warehouse has them.

Keep the files in one quarter directory for each quarter. Give each quarter directory the name that CMS uses for the quarter. Put the `PPEF_*.csv` files of that quarter in it, for example `data/ppef/2026-Q1/PPEF_Enrollment_Extract_2026.04.01.csv`. The sub-files for practice location, reassignment, secondary specialty and additional NPI go next to it. The loader gets the date of the quarter from the extract date in the file names, not from the directory name.

To rebuild the quarter directories of an existing release from a new clone, run `uv run healthcare-ledger download --source-name ppef`. You can also run `make download SOURCE_NAME=ppef`. The command does these things:

- It downloads each file that `releases/ppef_sources.csv` records into `data/ppef/<quarter>/`.
- It extracts the members of the CMS historical zips.
- It checks the sha256 of each file.

With `--check`, the command only verifies the files that are present. In October 2026, all recorded URLs still served the recorded bytes.

Where the files are:

- **Current and recent quarters**: data.cms.gov. The catalog `https://data.cms.gov/data.json` lists the current enrollment extract. Each quarter has a dataset id, and past quarters also have one. For each dataset id, `https://data.cms.gov/data-api/v1/dataset-resources/<id>` answers with the download URLs. These are the URLs of the enrollment file, the four sub-files and the data dictionary. Wayback captures of `data.json` contain the past ids.
- **2016 to 2022**: the CMS historical zips. The same API lists them under "Historical Medicare FFS Public Provider Enrollment Data" (`PECOS_Public_Provider_Main_Historical_Files_CY2016-CY2018_2025.08.08.zip` and the two later zips). Unzip the files of each year into the quarter directories.
- **NBER** keeps copies at `https://data.nber.org/ppef/`: 2016 to April 2019 by month, and `2023_2024Q2.zip`.

Then run these commands:

```bash
make load SOURCE_NAME=ppef            # loads every quarter not yet in the warehouse, in extract-date order
make validate
make sources SOURCE_NAME=ppef         # writes releases/ppef_sources.csv (size and sha256 of every loaded file)
```

Then do these steps:

1. Fill in the `origin` and `url` columns of `releases/ppef_sources.csv` for the new rows:
   - For a CMS file, write `cms` and the data.cms.gov URL.
   - For a file from a historical zip, write the URL of the zip. Write the member path in `member`.
   - For an NBER copy, write `nber`.
2. Commit `releases/ppef_sources.csv`.
3. Run `make export`. `export` refuses a quarter whose files are not recorded.

A quarter loads one time only. PPEF quarters have no rollback. For example, CMS can republish an extract. To replace the files of a quarter, do these steps:

1. Run `uv run healthcare-ledger reset --source-name ppef --yes`. This command drops the Medicare tables in `data/warehouse/ppef.duckdb`. It does not change other files.
2. Run `make load SOURCE_NAME=ppef` again. This takes about five minutes for forty quarters.

Use the files that CMS served at the time, not the CMS "historical" zips of August 2025. The 2016 to 2022 files in those zips are truncated (METHODOLOGY section 6). NBER has the original files for August 2016 to April 2019 under `https://data.nber.org/ppef/<year>/<month>/`. Their names are `Base_Provider_Enrollment_File.csv`, `Address_Sub-_File.csv`, `Reassignment_Sub-_File.csv` and `Secondary_Specialty.csv`.

The month of an NBER directory is the month of the NBER download, not the extract date. To use the NBER original files, do these steps:

1. Find the quarter of each download. Compare the enrollment row count.
2. Rename the files to the `PPEF_*_Extract_<date>.csv` pattern of their quarter.
3. Put them in the quarter directory in place of the copies from the zip.

NBER's `2017/10` repeats the July 2017 file. Thus the October 2017 and May 2018 quarters have no original file, and they keep the copies from the zip. `releases/ppef_sources.csv` records the copy that each loaded quarter comes from.

## Provider of Services quarters

The `pos_*` tables come from the CMS quarterly Provider of Services files (METHODOLOGY section 7). These tables are optional. A release has them only if the warehouse has them.

Keep the files in one quarter directory for each quarter. Give each quarter directory the name `YYYY-QN`. Put the file or files of that quarter in it, with the names that CMS uses (`data/pos/2019-Q1/POS_OTHER_MAR19.csv`). From `2023-Q4`, the iQIES file is also in the quarter directory (`data/pos/2023-Q4/Provider_of_Services_File_HHA_ASC_Hospices_iQiES_2023_Q4.csv`). The loader identifies the two files by their header, not by their names. The loader gets the date of the quarter from the directory name.

To rebuild the quarter directories of an existing release from a new clone, run `uv run healthcare-ledger download --source-name pos`. You can also run `make download SOURCE_NAME=pos`. The command downloads each file that `releases/pos_sources.csv` records into `data/pos/<quarter>/`. It checks the sha256 of each file. With `--check`, the command only verifies the files that are present. In October 2026, data.cms.gov served all recorded files.

Where the files are:

- **data.cms.gov** lists all quarters of the two files, past quarters included. In `https://data.cms.gov/data.json`, the datasets have these titles:
  - The QIES file: `Provider of Services File - Quality Improvement and Evaluation System : <date>`. Before 2025, the title was `Hospital & Non-Hospital Facilities`.
  - The iQIES file: `Provider of Services File - Internet Quality Improvement and Evaluation System : <date>`.

  The date in the title is not a dependable quarter label (`2024-04-01` is the second quarter of 2024). The `temporal` field of the dataset gives the quarter. For each dataset id, `https://data.cms.gov/data-api/v1/dataset-resources/<id>` answers with the download URL of the CSV, the data dictionary and the methodology note.
- **NBER** keeps copies of the QIES file under `https://data.nber.org/pos/`:
  - The original files of 2010-Q4 to 2022-Q3 are in `2022/orig/` (`pos_other_Q<n><year>.csv`) and `web_update/orig/cms_csv/`.
  - The year-end files are in the per-year directories.
  - Processed Stata and SAS versions back to 1984 are in other layouts.

Then run these commands:

```bash
make load SOURCE_NAME=pos             # loads every quarter not yet in the warehouse, in quarter order
make validate
make sources SOURCE_NAME=pos          # writes releases/pos_sources.csv (size and sha256 of every loaded file)
```

Then do these steps:

1. Fill in the `origin` (`cms`) and `url` columns of `releases/pos_sources.csv` for the new rows.
2. Commit `releases/pos_sources.csv`.
3. Run `make export`. `export` refuses a quarter whose files are not recorded.

A quarter loads one time only. POS quarters have no rollback. To replace the files of a quarter, do these steps:

1. Run `uv run healthcare-ledger reset --source-name pos --yes`. This command drops the `pos_*` tables in `data/warehouse/pos.duckdb`. It does not change other files.
2. Run `make load SOURCE_NAME=pos` again. This takes about 15 minutes for fifteen years.

If CMS adds a new column, the load stops with `unmapped columns`. Then do these steps:

1. Add the column to `src/healthcare_ledger/sources/pos/pos_columns.csv`.
2. Set `hash_optional` to `yes`, because the column is not in all files.
3. Load again.

In some quarters, a whole provider type leaves one of the two files. This is normal (METHODOLOGY section 7). But some quarters mark more than 5% of the CCNs of a type as absent while the type is still in the file. The load refuses these quarters. It refuses a truncated NPPES file in the same way. The 2020-Q1 file is such a file: 9,566 facilities are missing, and they come back in the next quarter.

Keep the 2020-Q1 file out of the source directory. The loader ignores a directory whose name is not `YYYY-QN`, for example `2020-Q1.incomplete`.

## Facility ownership, Doctors and Clinicians

**Ownership** (`ownership`, versioned; METHODOLOGY section 7a). These are the CMS Enrollments and All Owners files for hospitals, SNFs, home health agencies, hospices, FQHCs and RHCs. Keep them in one directory for each CMS release date, `data/ownership/YYYY-MM-DD/`, with the CMS file names. `releases/ownership_sources.csv` records each loaded file with its data.cms.gov URL. Thus `make download SOURCE_NAME=ownership` can rebuild the directories.

New CMS releases are in `https://data.cms.gov/data.json`. Their dataset titles are `Hospital All Owners : <date>`, `Skilled Nursing Facility Enrollments : <date>` and similar titles. To add a CMS release, do these steps:

1. Put its files in a directory with the extract date of the file names as its name. If the names have no date, use the date of the dataset.
2. Run `make load SOURCE_NAME=ownership`.
3. Run `make sources SOURCE_NAME=ownership`.
4. Fill in the URLs.

If CMS republishes a CMS release (`..._update.csv`), keep only the update.

The load refuses a CMS release that drops more than 5% of the enrollments of a type. The July 2026 hospice file is such a CMS release. A check found that its drops were real (METHODOLOGY 7a). Thus `COVERAGE_BREAKS` in `sources/ownership/ownership.py` gives it its own limit. A new CMS release of this type needs the same check.

`reset --source-name ownership --yes` drops only the tables of `ownership.duckdb`. A full reload takes about two minutes.

**Doctors and Clinicians** (`doctors_clinicians`, versioned; section 7b). These are the archived quarterly files from 2014 to 2022, from the Internet Archive. The provider-data site of data.cms.gov refuses scripts.

CAUTION: Do not try to go around the refusal of the data.cms.gov provider-data site.

- `make download SOURCE_NAME=doctors_clinicians` downloads the zips that `releases/doctors_clinicians_sources.csv` records into `data/dac/raw/<year>/`. It checks the CSV in each zip.
- `make load SOURCE_NAME=doctors_clinicians` loads the zips in date order. This takes about one minute for each vintage. The loader reads the zips directly.

Some vintages drop more than the 5% limit, but a check found that the drops were real. `COVERAGE_BREAKS` in `sources/doctors_clinicians/dac.py` lists these vintages with their own limit:

- April 2018, April 2019, August 2020 and September 2022.
- Each of them comes after a gap of four months or more between archived files.
- Each of them has more clinicians in total than the file before it.

If a new archive file goes above the 5% limit, do the same check before you add it to `COVERAGE_BREAKS`.

## Out-of-order month

The loader does not append a month that is older than the newest loaded snapshot. To insert such a month is a breaking correction.

- To insert the month, run `healthcare-ledger rollback --to <month before>`. Then run `make load`.
- If you do not want the month, remove the zip from `data/raw/`.

## Privacy refusal

WARNING: If `validate` or `export` finds a tax id, do not publish. Do not share the values. Send the log to the maintainer.

`export` does two checks:

1. It checks the warehouse with the `check_problems` of each source.
2. It checks the Parquet files that it wrote (`core/privacy.py`, `audit`).

If the second check fails, `export` deletes the files that it wrote. `privacy_audit.csv` gives counts for each column, never values.

Sometimes a state starts to issue license numbers in a new pattern, that is, a new license format. These licenses are NULL until you add the format to `src/healthcare_ledger/core/license_formats.csv`. To add a format is a breaking correction. The file lists the (state, format) pairs that at least 25 NPIs use. The file is frozen for each edition (METHODOLOGY 2a).

## Publish on Zenodo

Each release id has one Zenodo record. Each record is a new version of the same concept record. Thus the concept DOI always resolves to the newest release.

```bash
uv run healthcare-ledger package --out export --dest deposit/<release id>     # add --no-csv to leave the CSV copies out
```

`package` does these things:

- It verifies each data file again against `manifest.json`.
- It refuses an export if its `privacy_audit.csv` is missing or has a failure.
- It writes one stored zip for each table and format. If you unzip all of them next to `SHA256SUMS`, you get the paths of the export again.
- It writes the metadata files without change.
- It writes `LICENSE-DATA.txt` (CC0), `zenodo.json` (the deposition metadata) and `PACKAGE_SHA256SUMS`.

A Zenodo record holds a maximum of 100 files and 50 GB. If the deposit is too large, `package` stops.

WARNING: You cannot undo a publication. You can only replace a published file with a new version.

To upload with the Zenodo web interface, do these steps:

1. On the existing record, choose "New version". For the first upload, choose "New upload".
2. Upload all files in `deposit/<release id>/`.
3. Paste the fields of `zenodo.json`.
4. Check the preview.
5. Publish.

To upload with the REST API, do these steps:

1. Create the new version.
2. `PUT` each file to the bucket URL of the deposition.
3. `PUT` `zenodo.json` as the metadata.
4. Publish.

After you publish, add the DOI to `releases/<id>/` as a `DOI` file. Also add the DOI to the README.

Then measure the time of the reader's path. Do this for the first release of edition 1. Do it again each time the *Get the data* section of the README changes. Do these steps:

1. Use a computer that does not have the repository. Use an empty folder.
2. Start a clock.
3. Open the README on GitHub.
4. Do the steps of *Get the data* exactly: DOI, download, unzip, the one-provider query.
5. Stop the clock when the 14 rows match.
6. Record the time and each problem in the PR or in the release notes.

Fix the section before the next release in these conditions:

- The time is more than 15 minutes.
- A step needed something that the section does not say.

## Breaking corrections

Only the maintainer does breaking corrections. Do these steps:

1. Repair the code.
2. Rebuild the warehouse. Then validate and verify it.
3. Add a dated entry under "Change log" in [METHODOLOGY.md](METHODOLOGY.md).
4. Run `healthcare-ledger export --csv --new-edition`.
5. Commit `releases/EDITION` with the new record.
