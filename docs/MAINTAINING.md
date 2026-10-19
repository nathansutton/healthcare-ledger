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
