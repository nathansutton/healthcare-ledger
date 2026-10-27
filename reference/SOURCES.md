# Shared reference geography

These are small public tables that more than one study needs, kept here once so the studies use the same map. The notebooks read them as `../../reference/<file>`. `build.py` rebuilds each table from its pinned upstream file:

```bash
uv run --group research python reference/build.py --check   # rebuild in memory, compare with these files byte for byte
uv run --group research python reference/build.py           # rewrite them
```

Upstream files are downloaded into `data/reference/` (outside git). A file whose sha256 differs from its pin is refused.

## Tables

| File | What | Rule | Upstream (sha256) |
|---|---|---|---|
| `zcta_county.csv` | `zcta`, `county_fips`. 33,120 ZCTAs, 2010 geography (ZCTAs and counties as of the 2010 census) | each ZCTA goes to the county that holds most of its 2010 population (`POPPT`). Ties are broken by total area (`AREAPT`); the only ties are ZCTAs with no population. | Census Bureau, 2010 ZCTA to County Relationship File, https://www2.census.gov/geo/docs/maps-data/data/rel/zcta_county_rel_10.txt (`ea4798cfffbed5eaba990842ca0b1984e97d16ed7d3e0b4ef3e2d9216d5b8980`) |
| `county_cbsa_2015.csv` | `county_fips`, `cbsa`, `cbsa_title`, `cbsa_type` (`metro` or `micro`). 1,899 counties in a CBSA. That is 382 metropolitan areas outside Puerto Rico, the MSAs of 2016. Counties outside every CBSA are not listed. | as delineated. A county in a metropolitan division keeps its CBSA. | Census Bureau, core-based statistical area delineation list 1 of July 2015, https://www2.census.gov/programs-surveys/metro-micro/geographies/reference-files/2015/delineation-files/list1.xls (`3f04531db0b1963c8681ad905bedd2efe2aa01084b9d90cb601f68a0b6d43323`) |
| `zip_hrr_2019.csv` | `zip5`, `hrr` (number), `hrr_city`, `hrr_state`. 40,866 ZIP codes, 306 HRRs. | Dartmouth's own assignment. The HSA columns are dropped. | Dartmouth Atlas of Health Care, ZIP Code Crosswalks 2019, `ZipHsaHrr19.csv` in https://data.dartmouthatlas.org/downloads/geography/ZipHsaHrr19.csv.zip (`b389ea8f395a4de047b67d930a86f3219f6b717a228fa5ca087dddd31f0f635b`) |
| `state_fips.csv` | `state` (postal code), `name`, `fips`. The 50 states and DC. | the territories are dropped | Census Bureau, state codes, https://www2.census.gov/geo/docs/reference/state.txt (`bea4e03f71a1fa0045ae732aabad11fa541e5932b071c2369bb0d325e8cba5a0`) |
| `state_border_county_pairs.csv` | `county_fips`, `neighbor_fips`. 2,616 ordered pairs of adjacent counties that lie in different states, in both directions. | 2010 counties | Census Bureau, county adjacency file (2010), https://www2.census.gov/geo/docs/reference/county_adjacency.txt (`b6df774db9ad5b94c47036379e207dd4622e46ef22c01ca2b7603453a4901014`) |

A ZIP code is matched to a ZCTA by its five digits. Studies that need a ZIP's CBSA join `zcta_county.csv` to `county_cbsa_2015.csv`. The 2015 delineation is built on 2010 counties, so the two tables use the same county codes. So does the adjacency file.

Who reads them:
* **np-full-practice-authority:** `state_fips`, `zcta_county`, `state_border_county_pairs`.
* **provider-networks:** `zcta_county`, `county_cbsa_2015`, `zip_hrr_2019`.
* **dobbs-obgyn-workforce:** `state_fips`, for state names.

## Why one ZCTA to county map, and which one

Until plan 012 (October 2026), np-full-practice-authority used a different map. It placed each **2020** ZCTA in the county holding most of its **land area**. The source was Census Bureau, 2020 ZCTA to County Relationship File, https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/tab20_zcta520_county20_natl.txt (sha256 `3ed41278d637dc249e0323306f68be8a6c234e3090f4de88ef328dee71aeaaaf`, field `AREALAND_PART`; that file has no population). It had 33,791 ZCTAs. In October 2026 the operator chose the population rule on 2010 geography for both studies. That rule matches the intent of the CBSA delineation, which is built from 2010 counties and commuting. It also matches FPA's own 2010 adjacency file.

Each map was rebuilt from its upstream file and matched its committed file exactly. 32,923 ZCTAs are in both, and 976 of them go to different counties. Below, P10 means the 2010 population rule, L20 the 2020 land rule, and L10 the land rule applied to the 2010 file. Each row is placed in the first cause that applies, in the order listed.

| Cause | ZCTAs | Test |
|---|---:|---|
| County recoded between 2010 and 2020 | 27 | Wade Hampton AK `02270` to Kusilvak `02158` (12), Shannon SD `46113` to Oglala Lakota `46102` (7), Valdez-Cordova AK `02261` to Chugach `02063` (4) and Copper River `02066` (4). These are the same places, and all are outside any CBSA. |
| Zero-population ZCTA | 1 | P10 falls back to area |
| Population tie, land tie | 0, 0 | |
| Rule only (population or land) | 754 | L10 = L20. In these ZCTAs the county P10 picks holds a median 66% of the population; the county L20 picks holds a median 31%. |
| Vintage: 2010 and 2020 boundaries | 156 | L10 = P10, but the 2020 land-largest county differs |
| Vintage: the 2020 county was not part of the ZCTA in 2010 | 22 | L10 = P10, and the L20 county is not in the ZCTA's 2010 rows |
| Rule and vintage both | 16 | L10 differs from both |
| **Total** | **976** | 13 of them go to counties in different states |

Coverage also differs. 868 ZCTAs exist only in the 2020 file (ZIPs created after 2010), and 197 only in the 2010 file. Practice ZIPs in the 868 have no county under the 2010 map.

The land rule had two side effects on FPA's border design:
* Its 2020 county codes never matched the 2010 adjacency file at Shannon County SD (`46113`).
* It gave 12 Virginia independent cities no ZCTA, Danville (`51590`, a VA-NC border county) among them.

The 2010 map has neither problem.

## State codes

np-full-practice-authority's `state_fips.csv` and dobbs-obgyn-workforce's `states.csv` used to carry their own copies of the codes. The two copies agreed with each other and agree with the Census file: 51 codes, and the Dobbs names match too. The Dobbs file now keeps only its study columns and takes names from here.
