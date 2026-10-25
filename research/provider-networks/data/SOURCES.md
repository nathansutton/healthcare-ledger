# Reference data

Small public inputs, committed so that the study runs from a clone plus a graph directory. The notebook reads them with `read_csv`.

| File | What | Source |
|---|---|---|
| `zip_hrr_2019.csv` | ZIP code to Dartmouth hospital referral region (HRR): `zip5`, `hrr` (number), `hrr_city`, `hrr_state`. 40,866 ZIP codes, 306 HRRs. | Dartmouth Atlas of Health Care, ZIP Code Crosswalks, 2019: `ZipHsaHrr19.csv` in https://data.dartmouthatlas.org/downloads/geography/ZipHsaHrr19.csv.zip (sha256 of the zip `b389ea8f…f0f635b`). Columns `zipcode19`, `hrrnum`, `hrrcity`, `hrrstate` renamed; the HSA columns dropped. |
| `hospital_enrollments_2026-09-29.csv` | CMS's crosswalk from each hospital, critical access hospital and rural emergency hospital Medicare enrollment to its CCN, of the same date as the graph's last PPEF quarter: `enrollment_id`, `provider_type_code`, `npi`, `ccn`, `cah_or_hospital_ccn`, `pac_id`. 9,138 enrollments. Used only to measure the precision and recall of the graph's hospital link. | CMS, *Hospital Enrollments*, data.cms.gov, file `Hospital_Enrollments_2026.09.29.csv` (https://data.cms.gov/sites/default/files/2026-10/961708ac-2136-43cb-9587-ac64af23b6ab/Hospital_Enrollments_2026.09.29.csv, sha256 `5f02975f…e9e281a5`). Columns `ENROLLMENT ID`, `PROVIDER TYPE CODE`, `NPI`, `CCN`, `CAH OR HOSPITAL CCN` and `ASSOCIATE ID` kept and renamed; names, addresses and the other 33 columns dropped. |

A third of the enrollments in the CMS file carry a CCN that is not a POS hospital: swing-bed (`xxZxxx`), psychiatric-unit (`xxSxxx`), rehabilitation-unit (`xxTxxx`) and similar CCNs that share the hospital's address and NPI. `cah_or_hospital_ccn` names the parent hospital for some of them, and the notebook checks the link only on the enrollments whose CCN, or parent CCN, is an active POS hospital.
