# Maintaining

Run all commands from the checkout. You need approximately 32 GB RAM and 150 GB of free disk space. To limit DuckDB, set `THREADS=` and `MEMORY=`.

## First run

Copy a warehouse file to `data/nppes.duckdb`, then run `make validate`. Or build a new warehouse from the zips:

```bash
make install
make download        # gets 191 zips from CMS, NBER and the Wayback Machine; MIRROR=<url> gets the 6 archive-only zips
make check-sources   # must show 197 of 197
make load            # 3 to 4 hours
make validate
make export          # must show "continuity with release ...: ok"
```

`releases/sources.csv` gives the sha256 and URL for each zip. CMS serves the zips from January 2021; in October 2026 it removed the older ones. Of the 127 older zips, 105 come from the [NBER NPI/NPPES cumulative collection](https://data.nber.org/npi/) (the per-year directories, `backfiles/` and `nppes/zip-orig/`) and 16 from Wayback Machine captures of CMS's hosts. Six zips (October and November 2019, April, August, September and November 2020) have no public source; `make download` reports them as missing, and `MIRROR=<url>` fetches them from a private copy. Before each release, check that the recorded URLs still answer, because CMS may remove more.

If CMS sends "Access Denied", wait one hour and run `make download` again.

## Monthly run

CMS publishes a new file in the second week of each month.

```bash
git pull
make monthly
```

This downloads, loads, validates, verifies and exports. If a check fails, the warehouse goes back to the last release and nothing is exported. Read `data/work/monthly.log`, repair the cause, and run it again.

After a successful run:

1. Copy `export/` to the host, into a directory with the release id as its name.
2. Commit `releases/<id>/` and `releases/sources.csv`. Merge them before the next monthly run.

## Medicare PPEF quarters

The `medicare_*` tables come from CMS's quarterly Medicare Fee-For-Service Public Provider Enrollment extracts. They are optional: a release has them only if the warehouse does. Keep the files in one directory per quarter, named as CMS labels the quarter, with that quarter's `PPEF_*.csv` files inside (`data/ppef/2026-Q1/PPEF_Enrollment_Extract_2026.04.01.csv`, and the practice location, reassignment, secondary specialty and additional NPI sub-files beside it). The loader dates the quarter by the extract date in the file names, not by the directory name.

To rebuild the directories of an existing release from a fresh clone, run `uv run nppes-over-time ppef-download` (or `make ppef-download`). It fetches every file `releases/ppef_sources.csv` records into `data/ppef/<quarter>/`, extracts the members of CMS's historical zips, and checks each sha256; `--check` only verifies what is there. In October 2026 every recorded URL still served the recorded bytes.

Where the files are:

- **Current and recent quarters**: data.cms.gov. The catalog `https://data.cms.gov/data.json` lists the current enrollment extract; every quarter's dataset id (also past ones) answers at `https://data.cms.gov/data-api/v1/dataset-resources/<id>` with the download URL of the enrollment file, the four sub-files and the data dictionary. Past ids are in Wayback captures of `data.json`.
- **2016 to 2022**: CMS's historical zips, listed by the same API under "Historical Medicare FFS Public Provider Enrollment Data" (`PECOS_Public_Provider_Main_Historical_Files_CY2016-CY2018_2025.08.08.zip` and the two later ones). Unzip each year's files into the quarter directories.
- **NBER** keeps copies at `https://data.nber.org/ppef/` (2016 to April 2019 by month, and `2023_2024Q2.zip`).

Then:

```bash
make ppef-load                        # loads every quarter not yet in the warehouse, in extract-date order
make validate
make ppef-sources                     # writes releases/ppef_sources.csv (size and sha256 of every loaded file)
```

Fill in the `origin` and `url` columns of `releases/ppef_sources.csv` for the new rows (`cms` and the data.cms.gov URL, or the historical zip URL with the member path in `member`; `nber` for NBER copies), commit it, and run `make export`. `export` refuses a quarter whose files are not recorded.

A quarter loads once. There is no rollback for PPEF quarters: to replace a quarter's files (for example when CMS republishes an extract), run `uv run nppes-over-time ppef-reset --yes`, which drops the Medicare tables and nothing else, then `make ppef-load` again (about five minutes for forty quarters).

Prefer the files CMS served at the time over its August 2025 "historical" zips, whose 2016 to 2022 files are truncated (METHODOLOGY section 6). NBER has the originals for August 2016 to April 2019 under `https://data.nber.org/ppef/<year>/<month>/` as `Base_Provider_Enrollment_File.csv`, `Address_Sub-_File.csv`, `Reassignment_Sub-_File.csv` and `Secondary_Specialty.csv`; rename them to the `PPEF_*_Extract_<date>.csv` pattern of their quarter and put them in the quarter directory in place of the zip's copies. The directory month is NBER's download month, not the extract date: match a download to its quarter by the enrollment row count. NBER's `2017/10` repeats the July 2017 file, so the October 2017 and May 2018 quarters have no original and stay on the zip's copies. `releases/ppef_sources.csv` records which copy each loaded quarter came from.

## Provider of Services quarters

The `pos_*` tables come from CMS's quarterly Provider of Services files (METHODOLOGY section 7). They are optional: a release has them only if the warehouse does. Keep the files in one directory per quarter, named `YYYY-QN`, with that quarter's file or files inside under the names CMS serves them (`data/pos/2019-Q1/POS_OTHER_MAR19.csv`; from `2023-Q4` also the iQIES file beside it, `data/pos/2023-Q4/Provider_of_Services_File_HHA_ASC_Hospices_iQiES_2023_Q4.csv`). The loader tells the two files apart by their header, not by their names, and dates the quarter by the directory name.

To rebuild the directories of an existing release from a fresh clone, run `uv run nppes-over-time pos-download` (or `make pos-download`). It fetches every file `releases/pos_sources.csv` records into `data/pos/<quarter>/` and checks each sha256; `--check` only verifies what is there. In October 2026 data.cms.gov served every recorded file.

Where the files are:

- **data.cms.gov** lists every quarter of both files, past ones included: the datasets titled `Provider of Services File - Quality Improvement and Evaluation System : <date>` (the QIES file; titled `Hospital & Non-Hospital Facilities` before 2025) and `Provider of Services File - Internet Quality Improvement and Evaluation System : <date>` (the iQIES file) in `https://data.cms.gov/data.json`. The date in the title drifts (`2024-04-01` is the second quarter of 2024); the dataset's `temporal` field gives the quarter. Each dataset's id answers at `https://data.cms.gov/data-api/v1/dataset-resources/<id>` with the CSV's download URL, the data dictionary and the methodology note.
- **NBER** keeps copies of the QIES file under `https://data.nber.org/pos/`: the originals of 2010-Q4 to 2022-Q3 in `2022/orig/` (`pos_other_Q<n><year>.csv`) and `web_update/orig/cms_csv/`, the year-end files in the per-year directories, and processed Stata and SAS versions back to 1984 in other layouts.

Then:

```bash
make pos-load                         # loads every quarter not yet in the warehouse, in quarter order
make validate
make pos-sources                      # writes releases/pos_sources.csv (size and sha256 of every loaded file)
```

Fill in the `origin` (`cms`) and `url` columns of `releases/pos_sources.csv` for the new rows, commit it, and run `make export`. `export` refuses a quarter whose files are not recorded.

A quarter loads once. There is no rollback for POS quarters: to replace a quarter's files, run `uv run nppes-over-time pos-reset --yes`, which drops the `pos_*` tables and nothing else, then `make pos-load` again (about a quarter of an hour for fifteen years). A new CMS column stops the load with `unmapped columns`; add it to `src/nppes_over_time/pos_columns.csv` with `hash_optional` set to `yes` (it is not in every file) and load again. A quarter in which a whole provider type leaves one file is normal (METHODOLOGY section 7); a quarter that would mark more than 5% of a type's CCNs absent while the type is still in the file is refused, like a truncated NPPES file. The 2020-Q1 file is such a file (9,566 facilities missing, back the next quarter); keep it out of the source directory (a directory name that is not `YYYY-QN`, such as `2020-Q1.incomplete`, is ignored).

## Provider graph

```bash
make graph DB=data/nppes.e3.duckdb GRAPH=graph    # or: uv run nppes-over-time graph-build --db ... --out graph
```

The graph is derived from the NPPES, PPEF and POS tables (METHODOLOGY section 8) and is rebuilt from scratch each time, in about two minutes; run it after loading new PPEF or POS quarters. It needs all three sources in the warehouse and stops with a message naming the missing table otherwise; without the iQIES POS table (`pos_facility_iqies`) it still builds, but home health agencies drop out of `facility` after December 2023 and nursing homes after September 2025, when CMS moved them there. The research notebook in `research/provider-networks/` reads it from `$NPPES_GRAPH`. It is not part of the export.

The notebook's section 6 also needs the AHRQ Compendium of U.S. Health Systems tables (METHODOLOGY section 8a):

```bash
make compendium-download                                         # every Compendium file into data/ahrq/raw (about 260 MB)
make graph DB=data/nppes.e3.duckdb GRAPH=graph COMPENDIUM=data/ahrq
```

AHRQ publishes a new edition about once a year, usually two years after the year it describes. Run `compendium-download` again (it keeps the files it has), then add the new year's file names to `FILES` in `src/nppes_over_time/compendium.py`, which takes the latest revision of each year. ahrq.gov answers scripts with an empty 202 at times; the download then takes the Internet Archive's copy, and a file the archive has never captured has to be added to `KNOWN_FILES` by name.

## Out-of-order month

The loader does not append a month that is older than the newest loaded snapshot. This is a breaking correction. To insert the month, run `nppes-over-time rollback --to <month before>` and then `make load`. If you do not want the month, remove the zip from `data/raw/`.

## Privacy refusal

If `validate` or `export` finds a tax id, do not publish. Do not share the values. Send the log to the maintainer. `export` checks twice: the warehouse (`scrub.py`, `ppef.check_problems`), then the Parquet files it wrote (`audit.py`). A failure in the second check deletes the files it wrote. `privacy_audit.csv` gives per-column counts, never values.

A new license format (a state starts issuing numbers in a new pattern) makes those licenses NULL until it is added to `src/nppes_over_time/license_formats.csv`, which is a breaking correction: the file lists the (state, format) pairs used by at least 25 NPIs and is frozen per edition (METHODOLOGY 2a).

## Publish on Zenodo

A release is published as one Zenodo record per release id, as a new version of the same concept record so the concept DOI always resolves to the newest release.

```bash
uv run nppes-over-time package --out export --dest deposit/<release id>     # add --no-csv to leave the CSV copies out
```

`package` re-verifies every data file against `manifest.json`, refuses an export whose `privacy_audit.csv` is missing or has a failure, and writes one stored zip per table and format (unzipping them all next to `SHA256SUMS` restores the export's paths), the metadata files as they are, `LICENSE-DATA.txt` (CC0), `zenodo.json` (the deposition metadata) and `PACKAGE_SHA256SUMS`. A Zenodo record takes at most 100 files and 50 GB; `package` stops if the deposit would not fit.

Upload: in the Zenodo web interface choose "New version" on the existing record (or "New upload" the first time), upload every file in `deposit/<release id>/`, paste the fields of `zenodo.json`, check the preview and publish. With the REST API: create the new version, `PUT` each file to the deposition's bucket URL, `PUT` `zenodo.json` as the metadata, then publish. Publishing cannot be undone: a published file can only be superseded by a new version. Then add the DOI to `releases/<id>/` (a `DOI` file) and to the README.

## Breaking corrections

Only the maintainer does breaking corrections. Follow these steps:

1. Repair the code.
2. Rebuild the warehouse, then validate and verify it.
3. Add a dated entry under "Change log" in [METHODOLOGY.md](METHODOLOGY.md).
4. Run `nppes-over-time export --csv --new-edition`.
5. Commit `releases/EDITION` with the new record.
