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

## 9. Releases and editions

The release id is `e<edition>-<YYYYMMDD of last as-of>-<first 8 hex of sha256(SHA256SUMS)>`.

An edition is a group of releases that use the same rules (sections 2 to 5). In one edition, each new release contains all earlier releases exactly. Before `export` writes files, it compares the fingerprints with the latest release in `releases/`. If they are different, it stops.

A change that alters a released fingerprint is a breaking correction. Add a dated entry to the change log, then run `export --new-edition`.

## 10. Change log

Dated entries, newest last. `export --new-edition` requires an entry dated after the previous release's export date, and copies the log into the export README.

- 2026-10-18 (edition 1): first edition. Versioning rules as in sections 2 to 5, suppression rules as in 2a, repeating groups in canonical slot order before hashing (section 3).
- 2026-10-19 (edition 2): 46 monthly files from 2008 to 2015 added from the NBER NPI/NPPES cumulative collection (data.nber.org/npi/). The panel is monthly from April 2011 instead of June 2015, so versions in 2008 to 2015 get earlier, finer-grained `effective_date`s and intermediate versions that annual files hid. No rule changes.
