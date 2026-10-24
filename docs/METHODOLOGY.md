# Methodology

The code in `src/nppes_over_time/` is the reference. This file gives the rules.

## 1. Order the snapshots

The loader reads the as-of date from the name of the main CSV in each zip. It loads the snapshots in as-of order. If two zips have the same as-of date, it keeps the earliest zip, and then a `_V2` zip.

The loader does not insert a snapshot that is older than the newest loaded snapshot. To insert a historical month, run `nppes-over-time rollback --to <YYYYMMDD>` with the month before it. Then run `load` again.

## 2. Read the files

DuckDB reads all columns as text. The loader removes leading and trailing spaces. Empty strings and the tokens `<UNAVAIL>`, `<BLANK>` and `<EMPTY>` become NULL. In code columns only, the word `NULL` also becomes NULL. If `other_org_name` is NULL, `other_org_name_type` is also NULL. The loader does not change the data in any other way.

If a header does not match the schema, the load stops.

## 2a. Remove tax ids and SSNs

CMS hides the EIN and SSN fields, but providers type tax ids into free-text fields. The rules in `scrub.py` remove a value only when it is certainly a tax id:

- `ein` and `parent_org_tin` are always NULL.
- A value in `NN-NNNNNNN` or `NNN-NN-NNNN` format is NULL. This rule applies to identifier, license, name and supplemental columns.
- An `other_id` with nine digits is NULL when its issuer text names a tax id (`TAX`, `EIN`, `TIN`, `SSN`, and similar words).
- A formatted tax id in issuer text becomes `[REDACTED]`.

The rules run before hashing. `validate` and `export` check them again. If a check fails, `export` stops.

## 3. Fingerprint

`row_hash` is the md5 of all data columns in schema order, joined with `chr(31)`. A NULL hashes as an empty string. A new CMS column is `hash_optional`. It goes into the hash only when it has a value, so it does not change existing hashes. When an NPI has many rows, `version_hash` is the md5 of the sorted row hashes.

Before hashing, the loader sorts the 15 taxonomy slots and the 50 other-identifier slots into a canonical order (`slots.py`). The primary taxonomy is slot 1. When CMS only changes the slot order, the loader does not make a new version.

Each snapshot has one `content_fingerprint` for each stream. It is the md5 of the sorted `npi:version_hash` lines for that as-of date.

## 4. Make versions

The loader compares each snapshot with the open versions and gives each NPI one label: `new`, `changed`, `unchanged` or `absent`. For `changed` and `absent`, it closes the open version. For `new` and `changed`, it opens a new version. If a zip does not have a stream, the loader skips that stream for that snapshot.

The loader stops if the main file would make more than 5% of known NPIs absent. A damaged file is the usual cause.

Each snapshot loads in one transaction. If the load stops, run it again. It starts from the next snapshot.

## 5. Check

`validate` makes sure that versions do not overlap and that each NPI has a maximum of one open version. It also makes sure that, for each snapshot, the versions on its as-of date have the same NPI count as the file. `verify` compares the warehouse with the zips, one field at a time.

## 6. Medicare enrollment files (PPEF)

CMS publishes a quarterly extract of PECOS, the Medicare fee-for-service enrollment system: the *Public Provider Enrollment Files* (PPEF). Each quarter is a set of CSVs: the enrollment file, and sub-files for practice locations, reassignment of benefits, secondary specialties (from August 2016) and additional NPIs (from October 2025). The code is `ppef.py`.

The source directory has one subdirectory per quarter, named as CMS labels it (`2024-Q2`). The label is not a date. The as-of date of a quarter is the extract date in the enrollment file name, or, when CMS named the file without one (`PPEF_Enrollment_Extract_2023Q1.csv`), the date in the quarter's practice-location file name. The snapshot id is that date as YYYYMMDD. When a quarter holds two enrollment files, the one named `_Clean` wins. The January 2026 quarter is loaded from the original extract rather than CMS's `_Clean` re-publication, because the clean file also drops `MULTIPLE_NPI_FLAG`; the loader's name rule removes the same 35 values.

The key is the enrollment id (`ENRLMT_ID`), not the NPI: one provider can hold several enrollments. The reassignment file is keyed by the reassigning enrollment id. Rules 2 to 5 above apply with the enrollment id in place of the NPI: trimmed text, blanks and placeholders to NULL, the formatted tax id rule of 2a on the organization name, a first, middle or last name that contains a digit set to NULL (a few dozen rows per file hold a nine-digit number, an NPI or a birth date there; CMS's May 2026 re-publication of the January 2026 extract as `_Clean.csv` removed exactly those values, and the loader applies the same removal to every quarter), md5 row hashes, a version hash per enrollment id, `new`, `changed`, `unchanged` and `absent` against the previous quarter, one transaction per quarter, a content fingerprint per stream and quarter, and the 5% absent guard on the enrollment file. The tables are `medicare_<stream>_version` and `medicare_<stream>_state`, with bookkeeping in `ppef_snapshot` and `ppef_snapshot_stream`.

Three headers are handled specially. `MULTIPLE_NPI_FLAG` (from October 2025) and `GNDR_SW` (sex, in the files CMS served up to April 2019) are `hash_optional`, so the quarters without them keep their hashes; the quarter in which such a column appears or disappears versions every enrollment once. `ENRLMT_SPECIALTY_DESC`, in the April 2016 file only, repeats `PROVIDER_TYPE_DESC` without its prefix and is not loaded. A PAC id shorter than ten digits is left-padded with zeros.

The quarterly files CMS re-published in August 2025 as "historical" zips for 2016 to 2022 are degraded copies: organization names are cut to one character, last names to 17, first names to 11, middle names to 7 (to the initial from December 2020), provider type descriptions to 51 or 56 characters, one PAC id in ten lost its leading zeros, and the sex column is gone. The loader repairs only the PAC id. The files CMS served at the time, which NBER keeps for August 2016 to April 2019, have the full fields and are used for the nine quarters NBER has (October 2017 and May 2018 are missing there); the versions for the other 2016 to 2022 quarters carry the truncation, and a quarter in which a full-width file follows a truncated one, or the reverse, versions most enrollments. `medicare_snapshots.csv` names the file each quarter was built from and `releases/ppef_sources.csv` where it came from.

`validate` runs the section 5 invariants on the Medicare tables. `export` recomputes the PPEF fingerprints, checks them against the previous release's `medicare_snapshots.csv`, and refuses to publish a quarter whose files are not recorded with their sha256 in `releases/ppef_sources.csv` (`ppef-sources` writes the rows; the maintainer fills in the url). The PPEF tables do not change any NPPES fingerprint, so adding them is not a breaking correction.

## 9. Releases and editions

The release id is `e<edition>-<YYYYMMDD of last as-of>-<first 8 hex of sha256(SHA256SUMS)>`.

An edition is a group of releases that use the same rules (sections 2 to 5). In one edition, each new release contains all earlier releases exactly. Before `export` writes files, it compares the fingerprints with the latest release in `releases/`. If they are different, it stops.

A change that alters a released fingerprint is a breaking correction. Add a dated entry to the change log, then run `export --new-edition`.

## 10. Change log

Dated entries, newest last. `export --new-edition` requires an entry dated after the previous release's export date, and copies the log into the export README.

- 2026-10-18 (edition 1): first edition. Versioning rules as in sections 2 to 5, suppression rules as in 2a, repeating groups in canonical slot order before hashing (section 3).
- 2026-10-19 (edition 2): 46 monthly files from 2008 to 2015 added from the NBER NPI/NPPES cumulative collection (data.nber.org/npi/). The panel is monthly from April 2011 instead of June 2015, so versions in 2008 to 2015 get earlier, finer-grained `effective_date`s and intermediate versions that annual files hid. No rule changes.
- 2026-10-22 (edition 2): the Medicare Fee-For-Service Public Provider Enrollment (PPEF) quarterly extracts added as the `medicare_*` tables, February 2016 to September 2026, keyed by enrollment id (section 6). No change to the NPPES rules or fingerprints.
