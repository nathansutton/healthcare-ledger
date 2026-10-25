# Methodology

The code in `src/nppes_over_time/` is the reference. This file gives the rules.

## 1. Order the snapshots

The loader reads the as-of date from the name of the main CSV in each zip. It loads the snapshots in as-of order. If two zips have the same as-of date, it keeps the earliest zip, and then a `_V2` zip.

The loader does not insert a snapshot that is older than the newest loaded snapshot. To insert a historical month, run `nppes-over-time rollback --to <YYYYMMDD>` with the month before it. Then run `load` again.

## 2. Read the files

DuckDB reads all columns as text. The loader removes leading and trailing spaces. Empty strings and the tokens `<UNAVAIL>`, `<BLANK>` and `<EMPTY>` become NULL. In code columns only, the word `NULL` also becomes NULL. If `other_org_name` is NULL, `other_org_name_type` is also NULL. The loader does not change the data in any other way.

If a header does not match the schema, the load stops.

## 2a. Remove tax ids and SSNs

CMS hides the EIN and SSN fields, but providers type tax ids and SSNs into free-text fields. The rules in `scrub.py` fail closed: a value that could be a tax id or SSN is removed unless its shape is a known, legitimate format. A *tax-id token* is a run of digits, bounded by non-digits, that is a formatted SSN (`NNN-NN-NNNN`, with any of space, dot or dash between the groups), a formatted EIN (`NN-NNNNNNN`) or nine bare digits.

- `ein`, `parent_org_tin` and all 200 "Other Provider Identifier" columns (identifier, type, state and issuer of 50 slots) are always NULL. The other identifiers mix payer and Medicaid ids with tax ids that cannot be told apart, so none is published.
- A `license_number` is NULL when it contains a formatted SSN, is exactly a formatted EIN, contains another tax-id token in a *format* that is not a known license format of its state, or is nine digits (with optional spaces, dots or dashes, whether or not they form a token) whose same nine digits appear under a different state in the same row. The format of a nine-digit license is its first three digits followed by `999999` (`036999999` is an Illinois physician license); the format of any other license is its upper-case text with every digit replaced by `9` (`999999999RN` in Oregon, `MD999999999` in DC). `src/nppes_over_time/license_formats.csv` lists the 451 (state, format) pairs that at least 25 distinct NPIs used in the 2007 to 2026 files; it is frozen per edition.
- `credential`, `other_credential` and `ao_credential` are NULL when they contain nine digits, however separated (`123.456789`, `1234-56789`).
- In every other free-text column (names, addresses, titles, the text columns of the practice-location, other-name and endpoint files, and the Medicare names, cities and descriptions) each tax-id token is replaced by `[REDACTED]`. In a name column so is any run of nine digits however separated (`999-999-999` in an organization name); address lines and endpoint text keep other groupings of nine digits (a street number followed by a ZIP code, suite codes, dotted OIDs). Postal codes, telephone and fax numbers, codes, dates and NPI-valued columns are not free text.

The rules run before hashing, so suppressed content never makes a version. `validate` and `export` check them again on the warehouse; `export` then scans the Parquet files it wrote (`audit.py`) and writes the per-column counts to `privacy_audit.csv`. If any check fails, `export` stops and deletes the files it wrote.

## 3. Fingerprint

`row_hash` is the md5 of all data columns in schema order, joined with `chr(31)`. A NULL hashes as an empty string. A new CMS column is `hash_optional`. It goes into the hash only when it has a value, so it does not change existing hashes. When an NPI has many rows, `version_hash` is the md5 of the sorted row hashes.

Before hashing, the loader sorts the 15 taxonomy slots into a canonical order (`slots.py`). The primary taxonomy is slot 1. When CMS only changes the slot order, the loader does not make a new version.

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

Three headers are handled specially. `MULTIPLE_NPI_FLAG` (from October 2025) and `GNDR_SW` (sex, in the files CMS served up to April 2019) are `hash_optional`, so the quarters without them keep their hashes; the quarter in which such a column appears or disappears versions every enrollment once. `ENRLMT_SPECIALTY_DESC`, in the April 2016 file only, repeats `PROVIDER_TYPE_DESC` without its prefix and is not loaded. A PAC id shorter than ten digits is left-padded with zeros. In the August 2016 and January 2017 address files (NBER's copies) the `STATE_CD` and `ZIP_CD` headers are swapped; when most values under the state header are ZIP codes and most under the ZIP header are state codes, the loader reads the two columns the other way round.

The quarterly files CMS re-published in August 2025 as "historical" zips for 2016 to 2022 are degraded copies: organization names are cut to one character, last names to 17, first names to 11, middle names to 7 (to the initial from December 2020), provider type descriptions to 51 or 56 characters, one PAC id in ten lost its leading zeros, and the sex column is gone. The loader repairs only the PAC id. The files CMS served at the time, which NBER keeps for August 2016 to April 2019, have the full fields and are used for the nine quarters NBER has (October 2017 and May 2018 are missing there); the versions for the other 2016 to 2022 quarters carry the truncation, and a quarter in which a full-width file follows a truncated one, or the reverse, versions most enrollments. `medicare_snapshots.csv` names the file each quarter was built from and `releases/ppef_sources.csv` where it came from.

`validate` runs the section 5 invariants on the Medicare tables, the privacy rules of 2a on their text columns, and shape checks on their code columns (NPI and PAC id ten digits, state two letters, ZIP five or nine digits). `export` recomputes the PPEF fingerprints, checks them against the previous release's `medicare_snapshots.csv`, and refuses to publish a quarter whose files are not recorded with their sha256 in `releases/ppef_sources.csv` (`ppef-sources` writes the rows; the maintainer fills in the url). The PPEF tables do not change any NPPES fingerprint, so adding them is not a breaking correction.

## 7. Provider of Services files (POS)

CMS publishes a quarterly extract of its certification systems, the *Provider of Services* files: every institutional provider and supplier certified for Medicare or Medicaid (hospitals, nursing homes, home health agencies, hospices, dialysis facilities, ambulatory surgical centers, rural health clinics, FQHCs and the rest), keyed by the CMS Certification Number (CCN, `PRVDR_NUM`). The clinical laboratory (CLIA) file is a separate POS file and is not loaded. The code is `pos.py`; the column schema is `pos_columns.csv`, built from the headers of every file CMS has published, with CMS's descriptions.

Since the end of 2023 CMS publishes two files per quarter, and the loader keeps them as two streams of the same shape, both keyed by CCN:

- `pos_facility`: the *Hospital & Non-Hospital Facilities* file, renamed *Quality Improvement and Evaluation System* (QIES) in 2025. Annual at year end from December 2011, quarterly from March 2019. Upper-case SAS column names, 470 to 500 of them; the layout changed in 2013, 2014 and 2017.
- `pos_facility_iqies`: the *Internet Quality Improvement and Evaluation System* (iQIES) file, from the fourth quarter of 2023. CMS moves provider types from the QIES file into it as it migrates them: home health agencies, hospices and ambulatory surgical centers in 2023-Q4; nursing homes in 2025-Q3; dialysis facilities, ICF/IIDs, outpatient therapy, portable x-ray, CORFs and OPOs in 2026-Q1; rural health clinics, FQHCs, CMHCs and PRTFs in 2026-Q3, leaving hospitals alone in the QIES file. Lower-case column names, 102 to 186 of them, a subset of the QIES names plus new ones, and a different coding of the provider type (`prvdr_type_id` and `prvdr_sbtyp_id` in place of `prvdr_ctgry_cd` and `prvdr_ctgry_sbtyp_cd`).

The source directory has one subdirectory per quarter, named `YYYY-QN`, with the quarter's file or two files inside under CMS's own names, which are not systematic (the Q1 2025 hospital file is `PQWB.POSQ.CLIA.DATA.MAR25.csv`). The loader tells the two files apart by their header. The as-of date of a quarter is its last day, the end of the period data.cms.gov records for the dataset; the snapshot id is that date as YYYYMMDD. One published quarter is not loaded: the 2020-Q1 QIES file (`POS_OTHER_MAR20.csv`, the same bytes at CMS and NBER) lacks 9,566 facilities that are in the quarters before and after it, 6,689 of them in Texas, so the guard of section 4 refuses it as a damaged extract and the `pos_facility` panel goes from 2019-12-31 to 2020-06-30.

Rules 2 to 5 apply with the CCN in place of the NPI, with these additions. The tokens `Not Available`, `Not Applicable` and `N/A`, which the iQIES files print from 2024-Q4 where earlier files were blank, are placeholders and become NULL. Dates and counts are normalized before hashing and typed (`DATE`, `DOUBLE`), because CMS renders them differently from one quarter to the next: dates as `YYYYMMDD` in the QIES file and `YYYY-MM-DD` or `MM/DD/YYYY` in the iQIES file, counts as `01` until 2023-Q3 and `1` from 2023-Q4 in the QIES file, and as `1.00`, `1`, `.50` or `0.5` in the iQIES file. A count is a column CMS types `NUMBER` in its layout (the `*_CNT` columns and `TCHNCL_STF_NUM`); codes keep their text and their leading zeros. A value that is not a date or a number stays in the hash as written, is stored as NULL and is counted in `n_bad_dates` or `n_bad_numbers` (one to three per file, `99999999`). The token rule of 2a applies to the free-text columns: facility and organization names, the street address, the service descriptions of the 2011 to 2013 files and the Medicaid vendor number, where nine-digit state ids become `[REDACTED]`. A column that is not in every file of its stream is `hash_optional`, so the quarters in which CMS added or dropped columns version only the facilities whose values appeared or disappeared. The iQIES `processing_date`, one value per file, is not a facility attribute and is not loaded.

A CCN that leaves one file and appears in the other in the same quarter is closed with `close_reason = 'moved'` in the stream it left, not `absent`; its history continues in the other table, so a facility's full history is the union of the two tables filtered on `ccn`. Not every CCN of a migrating type reaches the other file: of the 45,248 home health agencies, hospices and ASCs in the 2023-Q3 QIES file, 36,272 are in the first iQIES file and 9,402 are in neither 2023-Q4 file, almost all of them terminated before 2012. Those close as `absent`, as do the 9,980 clinical laboratories (category 18) that the file carried until 2012 and that belong to the separate CLIA file from 2013. The later migrations leave the same kind of remainder behind: 22,414 nursing homes in 2025-Q3, 14,381 facilities in 2026-Q1 and 8,874 in 2026-Q3 are in neither file, all but a few dozen of them terminated, half of them before 2001; the iQIES file carries a provider type's active facilities and its recently terminated ones only. The 5% absent guard of section 4 therefore counts only absent CCNs whose provider type still has rows in the file; a type whose rows all left the file has changed systems, which is not evidence of a damaged file.

The iQIES file is young and CMS still changes how it renders codes, so several quarters version most of its facilities, faithfully to the file: 2024-Q1 added `control_type` and filled `pgm_trmntn_cd` (`00`, active) where 2023-Q4 was blank; 2024-Q4 changed the switch columns from `true`/`false` to `Yes`/`No` and filled `cmplnc_stus_cd` and `acptbl_poc_sw`; 2025-Q3 changed `state_rgn_cd` for most facilities; 2026-Q1 dropped the leading zero of `ssa_state_cd`. The QIES file has three such events: the December 2012 file filled switch columns that had been blank and blanked others (`MDCD_MDCR_PRTCPTG_PRVDR_SW` blank to `N` for 126,000 facilities, `ACRDTN_TYPE_CD` `1` to `0` for 99,000); the June 2020 file alone writes its switches as `true`/`false` instead of `Y`/`N` and its codes without leading zeros (`prvdr_ctgry_sbtyp_cd` `1` for `01`, `rgn_cd` `4` for `04`), so every facility versions in 2020-Q2 and again in 2020-Q3 when the September file reverts; and the 2023-Q4 file dropped the leading zeros of its counts, which the number rule absorbs. In the iQIES file, 2025-Q2 and 2026-Q2 also rewrote `fy_end_mo_day_cd` (`12-31` to `31-Dec`) and 2026-Q2 restored the leading zero of `ssa_state_cd`. The tables are `pos_facility_version`, `pos_facility_iqies_version` and their `_state` tables, with bookkeeping in `pos_snapshot` and `pos_snapshot_stream` (file, size, sha256, column count and counts per stream and quarter, including `n_moved`). `validate` runs the section 5 invariants on them (versions closed at a snapshot are changed + absent + moved), the privacy rules of 2a on their text columns and shape checks on the CCN and state; the shape checks, which catch a column read under the wrong header, tolerate one malformed row in ten thousand (the 2024-Q1 file has one row with tabs in the name and a one-digit state). `export` recomputes their fingerprints, checks them against the previous release's `pos_snapshots.csv` and refuses to publish a quarter whose files are not recorded with their sha256 in `releases/pos_sources.csv` (`pos-sources` writes the rows; the maintainer fills in the url). The POS tables do not change any NPPES or Medicare fingerprint, so adding them is not a breaking correction.

## 8. Provider graph

`graph-build` derives a graph from the warehouse; it loads nothing and changes no table or fingerprint, and it can be rebuilt from scratch at any time (two minutes for 43 quarters). The code is `graph.py`; `research/provider-networks/` uses it and summarizes the literature its rules follow.

The clock is the PPEF quarter: on each quarter's as-of date the builder reads the PPEF enrollment and reassignment versions in force that day, the NPPES version of each enrolled NPI in force that day, and the POS hospital version in force that day (the last quarter end on or before it). It writes five Parquet tables and `quarters.csv`, with counts per quarter, and `manifest.json`.

| Table | Key | Row |
|---|---|---|
| `practitioner` | `pac_id` | an individual enrollee (PECOS associate id): its NPI, the sorted Medicare provider types and states of its enrollments, and the NPPES primary taxonomy, practice ZIP (five digits) and state of its NPI |
| `organization` | `pac_id` | an organization enrollee: its NPIs, provider types, states and number of enrollments; `hospital` when any enrollment is a hospital (`00-09`), critical access hospital (`00-85`), rural emergency hospital (`00-24`) or Part B hospital department (`12-Z1`); the NPPES name, ZIP and state of its lowest NPI |
| `hospital` | `ccn` | an active POS hospital (category `01`, termination code `00`): name, subtype, state, ZIP, county FIPS, certified beds, control type |
| `reassignment` | `practitioner_pac_id`, `organization_pac_id` | the practitioner reassigns benefits to the organization through at least one pair of their enrollments |
| `hospital_link` | `enrollment_id` | a hospital, critical access hospital or rural emergency hospital enrollment matched to a POS CCN, with its organization, NPI and `match_rule` |

Enrollments are collapsed to associate ids because PECOS issues one associate id per person and one per organization (legal entity), however many enrollments it holds. A PPEF reassignment row whose reassigning enrollment is not in that quarter's enrollment file, or whose receiving enrollment is not an organization's, makes no edge; `quarters.csv` counts them (`n_reassignment_dropped`, 0.1 to 0.2% of rows).

The hospital link matches the NPPES practice address and phone of a hospital-type enrollment's NPI to the active POS hospitals of the same state: 1, the same ten-digit phone and street number; 2, the same street number and five-digit ZIP; 3, the same phone. The best rule wins, and an enrollment whose best rule matches two or more CCNs is not linked. Against CMS's Hospital Enrollments file of 29 September 2026, which gives each hospital enrollment's CCN, the rules are right 100.0%, 99.1% and 97.1% of the time and link 89% of the enrollments whose CCN is a POS hospital (`research/provider-networks/`, section 2). Terminated hospitals are excluded because a hospital that converts to a critical access hospital gets a new CCN at the same address.

Every table has the warehouse's interval semantics (section 4): a row starts on the first quarter it is observed with exactly those values and ends (`end_date`, exclusive) on the first quarter it is not; a row in the last quarter has a NULL `end_date`. A value that changes, or an edge that lapses and returns, makes a new row. There is no `close_reason`. The builder's test checks that the intervals reproduce every quarter's count.

### 8a. Health systems from the AHRQ Compendium

With `--compendium <dir>`, `graph-build` adds two tables from the AHRQ Compendium of U.S. Health Systems (`compendium.py`). They are annual, keyed by `year`, because each Compendium edition describes a calendar year: 2016, 2018 and 2020 to 2023 (there is no 2017 or 2019 edition).

| Table | Key | Row |
|---|---|---|
| `system` | `year`, `health_sys_id` | a Compendium health system: name, city, state, physician and primary care physician counts, group, hospital and acute hospital counts, multistate flag, beds |
| `system_link` | `year`, `node_type`, `node_id`, `compendium_id` | an entity AHRQ lists that year: an `organization` (PECOS associate id) from the group practice file, or a `hospital`, `nursing_home` or `home_health` agency (CCN), with its `health_sys_id`, NULL when AHRQ places it in no system. Organizations also carry `md_ppas_physicians`, the physicians MD-PPAS assigns to that tax id by plurality of Medicare Part B lines |

`compendium-download` fetches every file AHRQ has published under its compendium directory: the Internet Archive's index of that directory names them, ahrq.gov serves them, and the archive's raw captures stand in when ahrq.gov answers a script with an empty 202 response. It writes `raw/` and a `manifest.csv` with each file's source and sha256. `graph-build` reads the latest revision of each year: the 2016 files as updated in 2020, the 2018 files as updated in 2021, the revised 2020 to 2022 files. The CSVs mix UTF-8 and Latin-1 and some end lines with a bare carriage return; each is decoded and rewritten as UTF-8 before it is read, and every column stays text except the counts.

A group practice file row is a tax id with two or more physicians in that year's MD-PPAS. AHRQ does not publish the tax id; it gives the associate ids, more than one for 0.1% of rows (`^`-separated in the file, one `system_link` row each, sharing a `compendium_id`). AHRQ publishes group practice files for 2016, 2018 and 2020 to 2022 only; hospital files for every edition; nursing home and home health files from 2020. Its outpatient-site files (2022, 2023) carry no associate id or NPI and are not read.

The graph inherits the PPEF's breaks: organization enrollments fall by 40,000 between September 2022 and March 2023, and the April 2026 file is the first with physician assistants' reassignments (155,000 more practitioners with an edge than in January 2026). A reassignment is a billing permission, not employment or ownership, and an associate id is a tax id, not a health system: section 8a's tables are AHRQ's assignment of tax ids and facilities to systems, not the graph's.

## 9. Releases and editions

The release id is `e<edition>-<YYYYMMDD of last as-of>-<first 8 hex of sha256(SHA256SUMS)>`.

An edition is a group of releases that use the same rules (sections 2 to 5). In one edition, each new release contains all earlier releases exactly. Before `export` writes files, it compares the fingerprints with the latest release in `releases/`. If they are different, it stops.

A change that alters a released fingerprint is a breaking correction. Add a dated entry to the change log, then run `export --new-edition`.

## 10. Change log

Dated entries, newest last. `export --new-edition` requires an entry dated after the previous release's export date, and copies the log into the export README.

- 2026-10-18 (edition 1): first edition. Versioning rules as in sections 2 to 5, suppression rules as in 2a, repeating groups in canonical slot order before hashing (section 3).
- 2026-10-19 (edition 2): 46 monthly files from 2008 to 2015 added from the NBER NPI/NPPES cumulative collection (data.nber.org/npi/). The panel is monthly from April 2011 instead of June 2015, so versions in 2008 to 2015 get earlier, finer-grained `effective_date`s and intermediate versions that annual files hid. No rule changes.
- 2026-10-22 (edition 2): the Medicare Fee-For-Service Public Provider Enrollment (PPEF) quarterly extracts added as the `medicare_*` tables, February 2016 to September 2026, keyed by enrollment id (section 6). No change to the NPPES rules or fingerprints.
- 2026-10-24 (edition 3): privacy rules rewritten to fail closed (section 2a). The 50 other-identifier slots are no longer published (the `provider_other_identifier` table is gone and the monthly files lose those 200 columns); license numbers with a tax-id token survive only in a known state format; credentials with nine digits are NULL; tax-id tokens in every other free-text column, NPPES and Medicare, become `[REDACTED]`. Exports carry `privacy_audit.csv`. The Medicare practice locations of August 2016 and January 2017 are read with their swapped state and ZIP headers corrected (section 6). Every NPPES version hash that involved an other identifier, a license, a credential or a token changes.
- 2026-10-25 (edition 3): the CMS Provider of Services files added as the `pos_facility` (QIES) and `pos_facility_iqies` (iQIES) tables, December 2011 to September 2026, keyed by CCN (section 7). No change to the NPPES or Medicare rules or fingerprints.
- 2026-10-25 (edition 3): `graph-build --compendium` adds the AHRQ Compendium's annual `system` and `system_link` tables to the derived graph (section 8a), and `compendium-download` fetches the Compendium files. The graph is not part of the export; no change to any table or fingerprint.
