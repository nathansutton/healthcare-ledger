# Spike: tax ids from Transparency in Coverage

**Question.** The provider graph ([`../provider-networks`](../provider-networks)) keys organizations by PECOS associate id, the public stand-in for the tax id the consolidation literature groups by. Does the tax id itself, from insurers' Transparency in Coverage (TiC) files, change what the graph reproduces?

**Status: spike, not a study.** Only the current month (October 2026) is served by the three portals, so this is one cross-section on the graph's last quarter (2026-09-29). The fetched data stays local (`data/tic/`, gitignored).

## Data

Each TiC in-network file opens with `provider_references`: provider groups, each a tax id with the NPIs contracted under it (and, for UHC and Aetna, a business name). [`tic_heads.py`](tic_heads.py) reads only that head: HTTP ranges, inflated on the fly, stopped at the `in_network` key. That is 25–125 MB of a 1–21 GB file.

| Payer | Index | Distinct files | Heads fetched | Physicians covered |
|---|---|---|---|---|
| Cigna | `cigna.com/static/mrf/latest.json` | 120 | 15 | 72% |
| Aetna | HealthSparq `latest_metadata.json`, brands ALICFI, AFEHBPFI | 318 | 15 | 76% |
| UHC | `transparency-in-coverage.uhc.com/api/v1/uhc/blobs/` | 7,094 | 16 | 74% |
| Union | | | 46 heads, 5.7 GB | **85%** |

Coverage saturates after each payer's one or two national networks; the other 40 heads added under one point. Physicians are the 926,673 practitioners with a physician taxonomy and an active PPEF enrollment. Only EIN-type tax ids are used.

Quirks: UHC files are multi-member gzip, and the first redirect hop sometimes answers a one-byte probe without a range. Aetna's `health1.aetna.com` has a broken TLS chain, so the HealthSparq host is used directly. Cigna's partner networks host their own zips, which can't be range-read and are skipped.

## Results

[`analyze.py`](analyze.py), output on all TiC pairs and, in brackets, on pairs that two or more payers list.

1. **The associate id is the tax id.** For 94% [99.5%] of organizations found in TiC, their NPIs sit under exactly one EIN. And 95% [99.4%] of EINs map back to one associate id. The stand-in the graph and Muhlestein & Smith rely on holds.
2. **Multi-group reassignments are often not current.** Of single-group physicians' reassignment edges, 96% [97.5%] reappear as the physician on the organization's EIN roster. For physicians with four or more reassignments it is 64% [50%]. That fits the stale-links explanation for the graph's level gap. It doesn't prove it, because a Medicare-only arrangement (locums, telehealth) need not be on a commercial roster.
3. **Luo et al.'s basic tax-id measure moves up a little, not to 43%.** Measure: physicians whose EIN also lists a hospital NPI (PPEF hospital-type enrollments). Population: the 766k physicians in TiC with a reassignment.

   | | Any hospital tax id | Fractional |
   |---|---|---|
   | PPEF associate id (graph) | 30% | 19% |
   | TiC EIN, one payer at a time | 31–37% | 22–27% |
   | TiC EIN, pairs listed by 2+ payers | 29% | 21% |

   The union of all three payers gives 46% "any". That is an artifact: pooling rosters gives each physician 2.4 EINs instead of 1.7–1.9, and each extra EIN is another chance to touch a hospital. The real tax id closes little of the gap to Luo's 43%. That is Luo et al.'s own point: most integration runs through tax ids that are not the hospital's, and finding it takes ownership (Schedule R) or system data, not better tax ids.
4. **Group size moves about 4 points toward Muhlestein & Smith.** Physicians in groups of 100 or more: 63% by EIN roster vs 67% by associate id, same physicians and weighting. Nine or fewer: 16% vs 13%. That is a part of the ~10-point level gap, not all of it, and rosters are their own kind of count.

## Verdict

TiC is worth keeping as a monthly snapshot from here on, for three uses. It validates the associate-id stand-in, which can now be stated with a number. It is a currentness filter for reassignment edges, the job Physician Compare's 12-month claims filter used to do. And it is a name source for groups. It does not move the vertical-integration result. The next step there is still ownership edges (All Owners, Schedule R), as the provider-networks README says.

## A claims-based benchmark: AHRQ's Group Practice Linkage Files

The papers assign each physician to one group by billing. The PPEF can't do that, and TiC doesn't either. AHRQ's Compendium of U.S. Health Systems publishes the result of doing it with claims. Its **Group Practice Linkage File** (2016, 2018, 2020, 2021, 2022) lists every tax id with two or more physicians in that year's MD-PPAS, which puts each physician in the one tax id billing the plurality of their Part B lines. For each tax id it gives the **PECOS associate ids**, the Compendium health system (if any; built from Form 990 Schedule R/H, PECOS chain home offices, 10-Ks and HOPD billing, the network method Luo et al. also use) and the MD-PPAS physician count. The associate id joins straight onto the graph.

**Every Compendium file, 2016-2023.** The Compendium is now a source of the warehouse: `healthcare-ledger download --source-name ahrq` fetches the files `releases/ahrq_sources.csv` pins (from ahrq.gov, which intermittently answers scripts with an empty 202, falling back to the Internet Archive's captures), and `healthcare-ledger load --source-name ahrq` stacks the latest revision of each year into `data/warehouse/ahrq.duckdb` (docs/METHODOLOGY.md section 8a). AHRQ's CSVs mix UTF-8, Latin-1 and bare-CR line endings, and every column is kept as text for the leading zeros. This spike first fetched every file AHRQ has published under `ahrq.gov/sites/default/files/wysiwyg/chsp/compendium/`, from the Internet Archive's index of that directory (102 file names) plus the names it never captured: 114 files, 261 MB. Four names do not exist anywhere: xlsx copies of the 2023 nursing-home and home-health files and their 2023 tech docs. The tables, each with an `ahrq_` prefix in the warehouse:

| Table | Years | Rows per year | Key |
|---|---|---|---|
| `system` | 2016, 2018, 2020-2023 | 626-640 systems | `health_sys_id` |
| `hospital_linkage` | 2016, 2018, 2020-2023 | 6,700-6,800 | CCN |
| `group_practice_linkage` | 2016, 2018, 2020-2022 | 35,000-41,000 tax ids | PECOS PAC ids |
| `nursing_home_linkage` | 2020-2023 | ~15,000 | CCN |
| `home_health_linkage` | 2020-2023 | 9,600-13,000 | CCN |
| `outpatient_site_linkage` | 2022, 2023 | ~280,000 sites | OneKey id, address (no NPI or PAC id) |

There is no 2017 or 2019 Compendium, and no group-practice file for 2023. Earlier editions (the 2016 files as first released in 2019, the 2018 files before their 2021 update) are not loaded. The graph is read on the PPEF quarter nearest 1 July.

1. **Linkage.** 87% of AHRQ tax ids (90% of their physicians) are found among the graph's organizations in 2016 and 2018, and 98% (99.8%) from 2020. AHRQ's documentation independently confirms that associate id and tax id are one-to-one for 99.9% of groups.
2. **System share reproduces.** Among physicians in the AHRQ-listed tax ids, the share in a Compendium system:

   | | 2016 | 2018 | 2020 | 2021 | 2022 |
   |---|---|---|---|---|---|
   | AHRQ, MD-PPAS plurality | 52.0 | 56.9 | 59.8 | 61.4 | 61.9 |
   | Graph, fractional | 53.4 | 57.2 | 59.8 | 61.1 | 61.4 |
   | Graph, single group | 54.6 | 59.1 | 61.3 | 62.7 | 63.2 |
   | Graph, any reassignment | 59.8 | 63.9 | 67.3 | 68.9 | 69.7 |

   The fractional weighting is within 1.4 points of the claims-based measure every year. Over the whole graph population (solo and unlisted tax ids counted as not in a system) it is 42% in 2016 and 53% in 2022. That puts AHRQ's system definition on the same footing as Luo et al.'s 43% for 2019, though the definitions differ: system-owned versus billing 75% or more through a hospital-related tax id.
3. **The group-size level gap is a counting artifact, and it closes.** On the same tax ids, the graph put 10 to 11 more points of physicians in groups of 100+ than MD-PPAS did, every year. The cause: the graph sized each group by headcount, so a physician reassigning to four groups was counted in all four. Its headcounts run 1.1 to 1.4 times MD-PPAS's, rising over time. Sizing groups by fractional headcount (each physician counted once across their groups, as MD-PPAS's assignment does) gives 46.6 / 49.6 / 52.7 / 54.3 / 55.5 against MD-PPAS's 44.7 / 48.4 / 51.7 / 53.4 / 54.8.
4. **Against Muhlestein & Smith, with the corrected sizing.** February 2016, whole graph population: 38.5% in groups of 100+ and 30.2% in groups of nine or fewer, against their 35.1% and 35.3% for December 2015 (headcount sizing gave 45.6% and 23.8%). The remaining 3 to 5 points are what stale reassignments and the differing physician populations can still explain. The corrected series runs from 38.5% (2016) to 55.7% (September 2026) in groups of 100+, and from 30.2% to 17.4% in groups of nine or fewer.

This corrects [`../provider-networks`](../provider-networks) section 3. Its "the gap is not an artifact of how multi-group physicians are split" is right about the weighting and wrong about the sizing. The HHI section uses shares, not sizes, and is unaffected.

## Running it

```bash
uv run --group research python -I research/tic-spike/tic_heads.py list data/tic
uv run --group research python -I research/tic-spike/tic_heads.py fetch data/tic cigna 15   # and aetna, uhc
# enrollment_20260929.parquet: medicare_enrollment_version as of 2026-09-29 from data/nppes.e3.duckdb
uv run --group research python -I research/tic-spike/analyze.py data/tic graph
uv run healthcare-ledger download --source-name ahrq && uv run healthcare-ledger load --source-name ahrq
uv run --group research python -I research/tic-spike/analyze_ahrq.py data/warehouse graph
```
