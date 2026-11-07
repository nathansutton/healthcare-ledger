# Methodology

The code in `src/healthcare_ledger/` is the reference. This file gives the rules. There is no section 8: the provider graph moved to a different repository, and the other sections keep their numbers.

## 1. Order the snapshots

The loader reads the as-of date from the name of the main CSV in each zip. The loader loads the snapshots in as-of order. If two zips have the same as-of date, the loader keeps one zip by these rules, in this order:

1. The zip with the earlier date in its name.
2. The `_V2` zip (a CMS reissue of the same month).
3. The zip with the earlier name.

The loader does not insert a snapshot that is older than the newest loaded snapshot. To insert a historical month, do these steps:

1. Run `healthcare-ledger rollback --to <YYYYMMDD>` with the month before it.
2. Run `load` again.

## 2. Read the files

DuckDB reads all columns as text. Then the loader makes these changes:

- It removes leading and trailing spaces.
- It changes empty strings and the tokens `<UNAVAIL>`, `<BLANK>` and `<EMPTY>` to NULL.
- In code columns only, it also changes the word `NULL` to NULL.
- If `other_org_name` is NULL, it sets `other_org_name_type` to NULL.

The loader does not change the data in any other way.

If a header does not agree with the schema, the load stops. The 200 "Other Provider Identifier" headers of the main file are the exception. The loader reads these columns and then drops them (section 2a).

## 2a. Remove tax ids and SSNs

CMS hides the EIN and SSN fields, but providers type tax ids and SSNs into free-text fields. The rules in `core/privacy.py` fail closed. The loader removes each value that could be a tax id or SSN, unless its shape is a known, legitimate format.

A *tax-id token* is a run of digits with no digit directly before or after it. A tax-id token is one of these:

- a formatted SSN (`NNN-NN-NNNN`, with a space, a dot or a dash between the groups)
- a formatted EIN (`NN-NNNNNNN`)
- nine bare digits.

The loader applies these rules:

- `ein` and `parent_org_tin` are always NULL.
- The warehouse does not contain the 200 "Other Provider Identifier" columns (identifier, type, state and issuer of 50 slots). These columns mix payer and Medicaid ids with tax ids, and nobody can tell them apart. Thus the loader drops these columns when it reads the file.
- A `license_number` is NULL when one of these conditions is true:
  - It contains a formatted SSN.
  - It is exactly a formatted EIN.
  - It contains another tax-id token in a *format* that is not a known license format of its state.
  - It is nine digits, and the same nine digits appear under a different state in the same row. The nine digits can have optional spaces, dots or dashes, whether or not they form a token.
- `credential`, `other_credential` and `ao_credential` are NULL when they contain nine digits, with any separators (`123.456789`, `1234-56789`).
- In every other free-text column, the loader replaces each tax-id token with `[REDACTED]`. These columns are listed below.

The *format* of a license is as follows:

- The format of a nine-digit license is its first three digits, followed by `999999`. For example, `036999999` is the format of an Illinois physician license.
- The format of any other license is its upper-case text, with `9` in place of each digit. Examples are `999999999RN` in Oregon and `MD999999999` in DC.

`src/healthcare_ledger/core/license_formats.csv` lists the 451 (state, format) pairs that at least 25 distinct NPIs used in the 2007 to 2026 files. The list is frozen for each edition.

The other free-text columns are names, addresses and titles. They also include the text columns of the practice-location, other-name and endpoint files. They also include the Medicare names, cities and descriptions. In a name column, the loader also replaces any run of nine digits, with any separators (`999-999-999` in an organization name). Address lines and endpoint text keep other groups of nine digits. Examples are a street number followed by a ZIP code, suite codes and dotted OIDs. Postal codes, telephone and fax numbers, codes, dates and NPI-valued columns are not free text.

The loader applies the rules before hashing, so removed content is never in a version. `validate` and `export` check the rules again on the warehouse. Then `export` scans the Parquet files that it wrote (`core/privacy.py`, `audit`). It writes the counts for each column to `privacy_audit.csv`. If any check fails, `export` stops and deletes the files that it wrote.

## 3. Fingerprint

`row_hash` is the md5 of all data columns in schema order, joined with `chr(31)`. A NULL hashes as an empty string. A new CMS column is `hash_optional`. Such a column goes into the hash only when it has a value, so it does not change existing hashes. When an NPI has many rows, `version_hash` is the md5 of the sorted row hashes.

Before hashing, the loader sorts the 15 taxonomy slots into a canonical order (`sources/nppes/slots.py`). The primary taxonomy is slot 1. When CMS changes only the slot order, the loader does not make a new version.

Each snapshot has one `content_fingerprint` for each stream. The fingerprint is the md5 of the sorted `npi:version_hash` lines for that as-of date.

## 4. Make versions

The loader compares each snapshot with the open versions. It gives each NPI one label: `new`, `changed`, `unchanged` or `absent`.

- For `changed` and `absent`, the loader closes the open version.
- For `new` and `changed`, the loader opens a new version.

If a zip does not have a stream, the loader skips that stream for that snapshot.

The loader stops if the main file would make more than 5% of known NPIs absent. The usual cause is a damaged file.

The loader loads each snapshot in one transaction. If the load stops, run it again. The load starts from the next snapshot.

## 4a. Publish tables

The version tables (`<stream>_version`) are the record of the files. There is one version table for each file that CMS publishes. Each version table has the bookkeeping columns that prove that a release reproduces them: `is_current`, `first_snapshot_id`, `last_snapshot_id`, `version_hash`, `row_seq`.

A release publishes different tables, with a shape that agrees with what the files describe (`core/published.py`). Each source declares these tables as SQL views over its version tables (`Source.tables`). The warehouse catalog, the export and `healthcare_ledger.connect` all use these views under the same names.

- There is one table for each kind of record, also when CMS splits the record across files. For example, the two Provider of Services files are one `facility` table (section 7). The tables for the NPPES supplemental files have names that tell what they hold about a provider (`provider_practice_location`, `provider_other_name`, `provider_endpoint`).
- The views decode the codes that a reader would otherwise look up:
  - `provider.entity_type` is `individual` or `organization` (CMS codes 1 and 2).
  - `medicare_enrollment.enrollee_type` uses the same two values for the `I` or `O` prefix of the enrollment id.
  - `facility.facility_type` names the provider type with one coding for both POS files.
  - `provider.primary_taxonomy_code` repeats slot 1 of `provider_taxonomy` (section 3).
- Each table has its key, `effective_date`, `end_date` and `close_reason`, and then its data columns. The tables do not contain the bookkeeping columns. `is_current` is the same as `end_date IS NULL`. The snapshot ids and hashes are for the checks of sections 5 and 9, not for a reader. The tables also do not contain the columns that rule 2a makes always NULL (`ein`, `parent_org_tin`).

The views change no version, hash or fingerprint. The continuity check of a release (section 9) uses the version tables. `export` writes each view as a directory of Parquet files, partitioned by `effective_year`. `export` takes the column types for `data_dictionary.csv` from the view. `export` refuses a view if its columns are different from the columns that its source declares.

## 5. Check

`validate` makes sure of these conditions:

- Versions do not overlap.
- Each NPI has a maximum of one open version.
- For each snapshot, the versions on its as-of date have the same NPI count as the file.

`verify` compares the warehouse with the zips, one field at a time.

## 6. Medicare enrollment files (PPEF)

CMS publishes a quarterly extract of PECOS, the Medicare fee-for-service enrollment system. This extract is the *Public Provider Enrollment Files* (PPEF). The code is `sources/ppef.py`. Each quarter is a set of CSVs:

- the enrollment file
- a sub-file for practice locations
- a sub-file for reassignment of benefits
- a sub-file for secondary specialties (from August 2016)
- a sub-file for additional NPIs (from October 2025).

The source directory has one subdirectory for each quarter, with the CMS label as its name (`2024-Q2`). The label is not a date. The as-of date of a quarter is the extract date in the enrollment file name. Sometimes CMS names the enrollment file without a date (`PPEF_Enrollment_Extract_2023Q1.csv`). Then the as-of date is the date in the name of the practice-location file of the quarter. The snapshot id is that date as YYYYMMDD.

When a quarter has two enrollment files, the loader uses the file with `_Clean` in its name. The loader loads the January 2026 quarter from the original extract, not from the `_Clean` re-publication of CMS. The reason is that the clean file also drops `MULTIPLE_NPI_FLAG`. The name rule of the loader removes the same 35 values.

The key is the enrollment id (`ENRLMT_ID`), not the NPI, because one provider can have several enrollments. The key of the reassignment file is the reassigning enrollment id. Rules 2 to 5 above apply, with the enrollment id in place of the NPI:

- trimmed text, and NULL for blanks and placeholders
- the formatted tax id rule of 2a on the organization name
- NULL for a first, middle or last name that contains a digit (see below)
- md5 row hashes, and a version hash for each enrollment id
- `new`, `changed`, `unchanged` and `absent` against the previous quarter
- one transaction for each quarter
- a content fingerprint for each stream and quarter
- the 5% absent guard on the enrollment file.

A few dozen rows in each file have a nine-digit number, an NPI or a birth date in a name field. In May 2026, CMS re-published the January 2026 extract as `_Clean.csv` and removed exactly those values. The loader applies the same removal to every quarter.

The version tables are `medicare_<stream>_version` and `medicare_<stream>_state`. The bookkeeping is in `ppef_snapshot` and `ppef_snapshot_stream`. The release publishes each stream as `medicare_<stream>` (section 4a), and the enrollment file with `enrollee_type`.

The loader has special rules for three headers:

- `MULTIPLE_NPI_FLAG` (from October 2025) and `GNDR_SW` (sex, in the files that CMS served up to April 2019) are `hash_optional`. Thus the quarters without these columns keep their hashes. In the quarter in which such a column appears or disappears, every enrollment gets one new version.
- `ENRLMT_SPECIALTY_DESC` is in the April 2016 file only. It repeats `PROVIDER_TYPE_DESC` without its prefix. The loader does not load it.

The loader also makes these repairs:

- It left-pads with zeros a PAC id that is shorter than ten digits.
- In the August 2016 and January 2017 address files (the copies of NBER), the `STATE_CD` and `ZIP_CD` headers are swapped. Most values under the state header can be ZIP codes, and most values under the ZIP header can be state codes. In that case, the loader reads the two columns the other way.

In August 2025, CMS re-published the quarterly files for 2016 to 2022 as "historical" zips. These zips are degraded copies:

- CMS cut organization names to one character.
- CMS cut last names to 17 characters, first names to 11 and middle names to 7 (to the initial from December 2020).
- CMS cut provider type descriptions to 51 or 56 characters.
- One PAC id in ten lost its leading zeros.
- The sex column is not there.

The loader repairs only the PAC id.

NBER keeps the files that CMS served at the time, for August 2016 to April 2019. These files have the full fields. The loader uses them for the nine quarters that NBER has (October 2017 and May 2018 are missing there). The versions for the other 2016 to 2022 quarters have the truncation. When a full-width file follows a truncated file, or the reverse, most enrollments get a new version.

`medicare_snapshots.csv` names the file that the loader used for each quarter. `releases/ppef_sources.csv` tells where that file came from.

`validate` runs these checks on the Medicare tables:

- the section 5 invariants
- the privacy rules of 2a on their text columns
- shape checks on their code columns (NPI and PAC id ten digits, state two letters, ZIP five or nine digits).

`export` recomputes the PPEF fingerprints and checks them against `medicare_snapshots.csv` of the previous release. `export` refuses to publish a quarter if `releases/ppef_sources.csv` does not record its files with their sha256. `sources --source-name ppef` writes the rows, and the maintainer fills in the url. The PPEF tables do not change any NPPES fingerprint. Thus the addition of the PPEF tables is not a breaking correction.

## 7. Provider of Services files (POS)

CMS publishes a quarterly extract of its certification systems: the *Provider of Services* files. These files list every institutional provider and supplier that is certified for Medicare or Medicaid. Examples are hospitals, nursing homes, home health agencies, hospices, dialysis facilities, ambulatory surgical centers, rural health clinics, FQHCs and other types. The key is the CMS Certification Number (CCN, `PRVDR_NUM`). The clinical laboratory (CLIA) file is a separate POS file, and the loader does not load it.

The code is `sources/pos/pos.py`. The column schema is `sources/pos/pos_columns.csv`. The loader made this schema from the headers of every file that CMS has published, and the schema has the CMS descriptions.

Since the end of 2023, CMS publishes two files for each quarter, one for each certification system. The loader keeps them as two streams of the same shape, both with the CCN as key. Thus the history of each file is exact. The release publishes the two streams as one table, `facility` (see below):

- `pos_facility`: the *Hospital & Non-Hospital Facilities* file. In 2025, CMS renamed it *Quality Improvement and Evaluation System* (QIES). The file is annual at year end from December 2011, and quarterly from March 2019. It has 470 to 500 upper-case SAS column names. The layout changed in 2013, 2014 and 2017.
- `pos_facility_iqies`: the *Internet Quality Improvement and Evaluation System* (iQIES) file, from the fourth quarter of 2023. When CMS migrates a provider type, it moves that type from the QIES file into this file:
  - 2023-Q4: home health agencies, hospices and ambulatory surgical centers
  - 2025-Q3: nursing homes
  - 2026-Q1: dialysis facilities, ICF/IIDs, outpatient therapy, portable x-ray, CORFs and OPOs
  - 2026-Q3: rural health clinics, FQHCs, CMHCs and PRTFs. After this move, the QIES file has only hospitals.

  The iQIES file has 102 to 186 lower-case column names. These names are a subset of the QIES names, plus new names. The iQIES file has a different coding of the provider type (`prvdr_type_id` and `prvdr_sbtyp_id` in place of `prvdr_ctgry_cd` and `prvdr_ctgry_sbtyp_cd`).

The source directory has one subdirectory for each quarter, with the name `YYYY-QN`. Each subdirectory holds the file or the two files of the quarter, with the CMS names. These names are not systematic (the Q1 2025 hospital file is `PQWB.POSQ.CLIA.DATA.MAR25.csv`). The loader identifies the two files by their header. The as-of date of a quarter is its last day: the end of the period that data.cms.gov records for the dataset. The snapshot id is that date as YYYYMMDD.

The loader does not load one published quarter: the 2020-Q1 QIES file (`POS_OTHER_MAR20.csv`). CMS and NBER have the same bytes of this file. The file does not have 9,566 facilities that are in the quarters before and after it, and 6,689 of these are in Texas. Thus the guard of section 4 refuses the file as a damaged extract. The `pos_facility` panel goes from 2019-12-31 to 2020-06-30.

Rules 2 to 5 apply, with the CCN in place of the NPI, and with these additions:

- The tokens `Not Available`, `Not Applicable` and `N/A` are placeholders, and the loader changes them to NULL. The iQIES files print these tokens from 2024-Q4, where earlier files were blank.
- The loader normalizes dates and counts before hashing, and gives them types (`DATE`, `DOUBLE`). The reason is that CMS writes them differently from one quarter to the next:
  - dates in the QIES file: `YYYYMMDD`
  - dates in the iQIES file: `YYYY-MM-DD` or `MM/DD/YYYY`
  - counts in the QIES file: `01` until 2023-Q3, and `1` from 2023-Q4
  - counts in the iQIES file: `1.00`, `1`, `.50` or `0.5`.
- A count is a column that CMS types as `NUMBER` in its layout (the `*_CNT` columns and `TCHNCL_STF_NUM`). Codes keep their text and their leading zeros.
- If a value is not a date or a number, it stays in the hash as written. The loader stores it as NULL and counts it in `n_bad_dates` or `n_bad_numbers`. Each file has one to three such values (`99999999`).
- The token rule of 2a applies to the free-text columns. These columns are the facility and organization names, the street address, the service descriptions of the 2011 to 2013 files, and the Medicaid vendor number. In the vendor number, the loader replaces nine-digit state ids with `[REDACTED]`.
- A column that is not in every file of its stream is `hash_optional`. Thus, in a quarter in which CMS added or dropped columns, only the facilities with values that appeared or disappeared get a new version.
- The iQIES `processing_date` has one value for each file. It is not a facility attribute, and the loader does not load it.

A CCN can leave one file and appear in the other file in the same quarter. Then the loader closes its version in the stream that it left, with `close_reason = 'moved'`, not `absent`. The history of the CCN continues in the other stream.

Not every CCN of a migrating type gets to the other file. The 2023-Q3 QIES file has 45,248 home health agencies, hospices and ASCs. Of these, 36,272 are in the first iQIES file, and 9,402 are in neither 2023-Q4 file. Almost all of these 9,402 were terminated before 2012. The loader closes them as `absent`.

The loader also closes as `absent` the 9,980 clinical laboratories (category 18). The file had these laboratories until 2012. From 2013, they belong to the separate CLIA file.

The later migrations leave the same kind of remainder:

- 2025-Q3: 22,414 nursing homes
- 2026-Q1: 14,381 facilities
- 2026-Q3: 8,874 facilities.

These facilities are in neither file. All but a few dozen of them are terminated, and half of them were terminated before 2001. The iQIES file has only the active facilities of a provider type and its recently terminated facilities.

Thus the 5% absent guard of section 4 counts only the absent CCNs whose provider type still has rows in the file. If all rows of a type left the file, the type changed systems. This is not evidence of a damaged file.

The iQIES file is new, and CMS still changes how it writes codes. Thus, in several quarters, most of its facilities get a new version, and these versions are true to the file:

- 2024-Q1 added `control_type`, and filled `pgm_trmntn_cd` (`00`, active) where 2023-Q4 was blank.
- 2024-Q4 changed the switch columns from `true`/`false` to `Yes`/`No`, and filled `cmplnc_stus_cd` and `acptbl_poc_sw`.
- 2025-Q3 changed `state_rgn_cd` for most facilities.
- 2026-Q1 dropped the leading zero of `ssa_state_cd`.

The iQIES file also has these changes:

- 2025-Q2 and 2026-Q2 rewrote `fy_end_mo_day_cd` (`12-31` to `31-Dec`).
- 2026-Q2 restored the leading zero of `ssa_state_cd`.

The QIES file has three such events:

- The December 2012 file filled switch columns that were blank, and blanked others. For example, `MDCD_MDCR_PRTCPTG_PRVDR_SW` went from blank to `N` for 126,000 facilities, and `ACRDTN_TYPE_CD` from `1` to `0` for 99,000.
- Only the June 2020 file writes its switches as `true`/`false` instead of `Y`/`N`. It also writes its codes without leading zeros (`prvdr_ctgry_sbtyp_cd` `1` for `01`, `rgn_cd` `4` for `04`). Thus every facility gets a new version in 2020-Q2, and again in 2020-Q3, when the September file reverts.
- The 2023-Q4 file dropped the leading zeros of its counts. The number rule absorbs this change.

The tables are `pos_facility_version`, `pos_facility_iqies_version` and their `_state` tables. The bookkeeping is in `pos_snapshot` and `pos_snapshot_stream`. These tables hold the file, size, sha256, column count and counts for each stream and quarter, with `n_moved`.

`validate` runs these checks on the POS tables:

- the section 5 invariants (the versions closed at a snapshot are changed + absent + moved)
- the privacy rules of 2a on their text columns
- shape checks on the CCN and the state.

The shape checks find a column that the loader read under the wrong header. The shape checks accept one malformed row in ten thousand. For example, the 2024-Q1 file has one row with tabs in the name and a one-digit state.

`export` recomputes the POS fingerprints and checks them against `pos_snapshots.csv` of the previous release. `export` refuses to publish a quarter if `releases/pos_sources.csv` does not record its files with their sha256. `sources --source-name pos` writes the rows, and the maintainer fills in the url. The POS tables do not change any NPPES or Medicare fingerprint. Thus the addition of the POS tables is not a breaking correction.

`facility` publishes the two streams as one table of facilities, with the CCN as key (section 4a). It has the QIES versions first, then the iQIES versions. If a version ends because its CCN moved, `facility` closes it as `changed`, because the facility continues.

Sometimes a CCN is in both files in the same quarter. An example is the 58 organ procurement organizations of 2025-Q4. Both files listed them before CMS dropped them from the QIES file. In this case the iQIES row has priority. `facility` cuts the QIES version where the iQIES version starts. `facility` leaves out a QIES version that starts on or after that date.

`validate` checks these conditions on `facility`:

- No two versions of a CCN overlap.
- Each QIES version that `facility` leaves out is inside an iQIES version. A CCN that went back to the QIES file would fail this check.
- Each version has a `facility_type`.

`facility_type` names the provider type in the same way for both files (`sources/pos/facility_types.csv`, with the CMS labels from the QIES layout). This file matches each QIES category (`prvdr_ctgry_cd`) to the iQIES type (`prvdr_type_id`) to which CMS moved it. The match uses the CCNs that moved. The iQIES file codes the four nursing home categories (02, 03, 04, 10) as one type (20). Thus they are one `nursing_home`.

`active` means a termination code of `00`. In the iQIES file, a blank code also counts as active, because the first iQIES file leaves it blank for active facilities. `pos_file` keeps the file that a version comes from, because the two files code some columns differently.

## 7a. Facility enrollments and owners (CMS Enrollments and All Owners files)

From September 2022, CMS publishes two files on data.cms.gov for each of six kinds of Part A facility:

- An *Enrollments* file, with one row for each Medicare enrollment. It has the NPI, CCN, PECOS associate id of the legal entity, names, incorporation, organization structure, proprietary or nonprofit, and address. Each type also has a few more columns. These are the hospital subgroups and rural emergency hospital conversion, the affiliated entity of the nursing home, and the home health practice location type.
- An *All Owners* file, with one row for each enrollment and owner or managing party. It has the associate id of the owner, person or organization, role, association date, name and address, and share. It also has flags for the kind of organization that the owner is.

The types are:

- hospitals (monthly from November 2022; critical access hospitals from July 2023)
- skilled nursing facilities (monthly from September 2022)
- home health agencies and hospices (quarterly from March 2023)
- FQHCs and rural health clinics (quarterly from October 2023).

The code is `sources/ownership/ownership.py`. The column schema is `sources/ownership/ownership_columns.csv`, and it comes from the headers of every file.

Each file is one stream, with the enrollment id as key: `ownership_<type>_enrollment` and `ownership_<type>_owner`. The six types never share an enrollment id. The owners of an enrollment are its rows (`row_seq`), and one version holds all of them together.

The source directory has one subdirectory for each CMS release date, `YYYY-MM-DD`. Each subdirectory holds the files of that CMS release, with the CMS names. The as-of date is the extract date in the file names. The first SNF and hospital files have no extract date in their names. For these files, the as-of date is the release date that data.cms.gov gives.

Most CMS releases have only the hospital and SNF files. A stream that is not in a CMS release closes nothing (section 4). `releases/ownership_sources.csv` pins all 296 files of the 53 CMS releases, September 2022 to September 2026, with their data.cms.gov urls. Sometimes CMS re-published a release (`Hospital_All_Owners_2026.07.31_update.csv`). Then the loader loads the update.

Rules 2 to 5 apply, with the enrollment id in place of the NPI, and with these additions:

- The loader normalizes dates to ISO before hashing (CMS writes `MM/DD/YYYY`).
- A share is a number. Some CMS releases write a share of 0 where others leave it blank. The loader stores a share of 0 as NULL.
- Some CMS releases lose the leading zeros of associate ids and ZIP codes, because they write them from a number. An example is the December 2022 hospital owners file, for 104,000 rows. The loader adds the zeros again:
  - It left-pads an id of one to nine digits to ten digits.
  - It left-pads a ZIP of three or four digits to five, and a ZIP of seven or eight digits to nine.
- The token rule of 2a applies to the free-text columns.
- An owner's first, middle or last name with a digit in it is NULL.
- A column that is not in every file of its stream is `hash_optional`.

The files are mostly Latin-1, and a few are UTF-8 with stray Latin-1 bytes. The reader of section 2 reads both.

The versions record the changes that CMS makes, and some of these changes are only bookkeeping:

- CMS renumbers the affiliated entity ids of the nursing homes in most months. From November 2022 to September 2023, this alone gives 8,000 to 9,700 SNF enrollments a new version each month. Thus you cannot follow an affiliated entity by its id from one CMS release to the next.
- The owner files give most enrollments a new version in January 2025, when the private equity, REIT, chain home office and owned-by-another flags appear. For SNFs, this happens in November 2024, and the SNF files also add trust and parent-company flags.
- The owner files also give most enrollments a new version in July 2026, when those flags change from blank to `N` for organizational owners.
- The October 2025 FQHC and RHC enrollment files add the telephone.
- The hospital enrollment file grows by 2,400 in July 2023, when critical access hospitals join it.
- The July 2026 hospice enrollment file drops 395 of 6,066 hospices. Of these, 280 are in California, and most of the others are in Texas, Nevada and Arizona. In 2023, CMS put these four states under enhanced oversight of hospices. This drop is above the 5% guard of section 4. The loader lets it through at 7%, for that CMS release only (`COVERAGE_BREAKS` in `sources/ownership/ownership.py`).

`medicare_facility_enrollment` and `medicare_facility_owner` publish the six streams of each kind as one table. `facility_type` names the file.

## 7b. Doctors and Clinicians (Physician Compare) files

The *Doctors and Clinicians* national downloadable file of CMS had the name *Physician Compare* until 2020. It lists every clinician who is enrolled in Medicare and has claims or an enrollment in the past twelve months. It has one row for each clinician, group and practice address. The columns include names, specialties, medical school and graduation year, and the legal name, associate id and number of members of the group. They also include up to five hospital affiliations by CCN, which CMS derives from inpatient claims.

The file lists a clinician under a group only if the clinician billed Medicare claims under that group in the past twelve months. The PPEF reassignment file does not have this filter.

data.cms.gov keeps only the current file, behind a server that refuses scripts. The Internet Archive holds the quarterly archive files that CMS published from March 2014 to December 2022 (monthly in 2021). There are 46 of these files. `releases/doctors_clinicians_sources.csv` pins them with their Archive urls and the path of the CSV inside each zip. The code is `sources/doctors_clinicians/dac.py`, and the column schema is `dac_columns.csv`.

There is one stream, `dac_clinician`, with the NPI as key, and with one row for each clinician, group and address. The as-of date is the first day of the month in the zip name. The vintages are different:

- The March and June 2014 and December 2016 files have no header row. The loader supplies the 2014 and 2016 layouts by field count.
- The October 2016 and April 2017 headers do not include a column that the rows have.
- The column names change in July 2016, 2018 and 2020.
- The name of the CSV inside the zip changes five times. From 2016, the CSV shares the zip with quality-reporting files.

The loader maps every vintage onto one set of 47 columns. The columns that are not in every vintage are `hash_optional`. Some files repeat rows exactly (201,508 in May 2016), and the loader loads each distinct row once. A missing member count written `.` is NULL. The loader left-pads a group or clinician associate id of eight or nine digits to ten. The privacy rules of 2a apply, with the credential rule of NPPES (a credential with nine digits in it is NULL).

The content of this file changes more than the content of a registry. The number of group members is a count for the group. Thus, when one clinician joins a group, every member gets a new version. Also, CMS recomputes the hospital affiliations at each refresh. In every quarter, 60 to 70% of clinicians get a new version.

The April 2018 file lists 95,199 fewer clinicians (9.0%), across every state and specialty. Of these, 17,258 return in July. The loader lets that one vintage through the absent guard at 10% (`COVERAGE_BREAKS`).

Three later files go above the 5% guard after a gap of four months or more in the archive:

- April 2019: 5.4% absent, after December 2018
- August 2020: 8.5%, eight months after December 2019 (no archive file of early 2020 survives)
- September 2022: 5.9%, five months after April.

Each of these files lists more clinicians in total than the file before. Each file loses clinicians in every state and specialty in proportion. In the next file, a tenth to a fifth of them come back. This is turnover in a twelve-month claims window, not damaged files. The loader lets each file through at its own limit.

The published table is `doctors_clinicians`.

## 9. Releases and editions

The release id is `e<edition>-<YYYYMMDD of last as-of>-<first 8 hex of sha256(SHA256SUMS)>`.

An edition is a group of releases that use the same rules (sections 2 to 5). In one edition, each new release contains all earlier releases exactly. Before `export` writes files, it compares the fingerprints with the latest release in `releases/`. If the fingerprints are different, `export` stops.

A change that alters a released fingerprint is a breaking correction. For a breaking correction, do these steps:

1. Add a dated entry to the change log.
2. Run `export --new-edition`.

## 10. Change log

The entries have dates, with the newest entry last. The first published release starts edition 1. The project has not published a release yet. After that release, each breaking correction needs an entry here, dated after the export date of the previous release. `export --new-edition` checks this date. The export README contains the log.
