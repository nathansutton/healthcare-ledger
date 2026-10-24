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

## Out-of-order month

The loader does not append a month that is older than the newest loaded snapshot. This is a breaking correction. To insert the month, run `nppes-over-time rollback --to <month before>` and then `make load`. If you do not want the month, remove the zip from `data/raw/`.

## Privacy refusal

If `validate` or `export` finds a tax id, do not publish. Do not share the values. Send the log to the maintainer.

## Breaking corrections

Only the maintainer does breaking corrections. Follow these steps:

1. Repair the code.
2. Rebuild the warehouse, then validate and verify it.
3. Add a dated entry under "Change log" in [METHODOLOGY.md](METHODOLOGY.md).
4. Run `nppes-over-time export --csv --new-edition`.
5. Commit `releases/EDITION` with the new record.
