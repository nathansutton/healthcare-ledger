# Provider networks

Can the public CMS files in this warehouse be joined into one graph of who practices where and for whom, and does that graph reproduce what health economists have measured about consolidation?

`healthcare-ledger graph-build` builds the graph quarter by quarter, February 2016 to September 2026 (43 PPEF quarters; [METHODOLOGY.md](../../docs/METHODOLOGY.md) section 8). Its nodes are practitioners and organizations, keyed by PECOS associate id, and active POS hospitals, nursing homes and home health agencies, keyed by CCN. With `--compendium` it adds AHRQ's health systems by Compendium year, with edges from organizations, hospitals, nursing homes and home health agencies to their system (section 8a). It has three kinds of quarterly edges. A **reassignment** edge runs from a practitioner to each organization that bills Medicare for them. A **hospital link** runs from a hospital's Medicare enrollment to its CCN, matched by address and phone and checked against CMS's own crosswalk: right 97 to 100 percent of the time, and found for 89 percent of linkable enrollments. A **post-acute link** does the same for nursing homes (right 99.9 percent of the time, found for 97 percent) and home health agencies (98.7 and 68 percent). In September 2026 the graph holds 2.2 million practitioners, 291,000 organizations, 7,031 hospitals and 3.7 million reassignment edges.

[`provider_networks.ipynb`](provider_networks.ipynb) replicates three published measures and extends each over the decade. Section 4b adds billing volume from HHS's public Medicaid claims extract, the one public source that links a billing organization to a servicing clinician with dollars. Section 6 scores the graph against AHRQ's claims-based assignment of physicians to tax ids and systems, and measures system membership. Section 7 measures market concentration with systems as firms:

| Measure | Paper | Result |
|---|---|---|
| Share of physicians by group size | Welch et al. 2013; Muhlestein and Smith 2016 | **Replicates in direction, and the level within 3 to 5 points.** Physicians in groups of 100 or more rise from 38.5 to 55.7 percent, 2016 to 2026; those in groups of nine or fewer fall from 30.2 to 17.4 percent. Against Muhlestein and Smith's December 2015 (35.1 and 35.3 percent) the graph is 3.4 points higher and 5.1 lower. Groups are sized counting each physician once; a headcount, which counts a multi-group physician in every group, put the levels 10 points off. Against AHRQ's MD-PPAS counts on the same tax ids the fractional size is within 2 points every year, the headcount 10 to 11 points high. The papers' finding that primary care consolidated faster than specialties reverses after 2016. |
| Share of local markets highly concentrated (HHI > 2,500), HRR by specialty | Capps, Dranove and Ody 2017; Fulton 2017 | **Not identified from Medicare files alone; flat with Medicaid volume.** On the Medicare files, the sign of the trend depends on how a physician with several organizations is counted. The papers' rule (main firm = plurality of billing) can be applied with HHS's public Medicaid claims, which rank the groups of four in ten multi-group physicians. Under that rule the share of concentrated markets moves by less than two points in every specialty, end of 2018 to February 2025. The two Medicare-only weightings bracket it. |
| Physicians billing through an organization that bills as a hospital | Luo et al. 2024 ("basic TIN" variant) | **A floor.** The share rises from 17 to 18 percent fractional, or 24 to 29 percent with any hospital reassignment. This is below Luo et al.'s 43 percent in 2019, as the basic measure must be. |
| Share of MSAs highly concentrated (HHI > 2,500), systems as firms | Fulton 2017 | **Specialists reproduce, primary care does not.** In 2016, 67.5 percent of the 382 MSAs are highly concentrated for specialists (each specialty its own market), against Fulton's 65 percent; primary care is 21 percent against 39. After 2016 neither rises (specialists 63 percent in 2023). On section 4's HRR markets, system firms keep concentration flat where tax-id firms show it falling. The claims-based HHIs (Capps, Dranove and Ody; Baker et al.; the FTC's 2026 Atlas) need patient locations and dollars and are not reproduced. |
| Median MSA share of physicians in systems | Machta et al. 2020; Furukawa et al. 2020 | **Reproduces by specialty.** 2018: cardiology 57 (Machta 55), hematology-oncology 54 (57), dermatology 6 (6). National share 43 and 46 percent in 2016 and 2018, against Furukawa's 40 and 51. |
| Physicians billing through an organization in a health system | Luo et al. 2024 (full measure); AHRQ Compendium | **Reproduces AHRQ, brackets Luo.** On AHRQ's own tax ids, the graph's fractional share in a system is within 1.4 points of MD-PPAS every year (52 to 62 percent, 2016 to 2022). Over all physicians it rises from 43 to 53 percent, a few points above Luo et al.'s 43.5 percent for 2019, whose 75-percent-of-billing rule is stricter. Oncology and cardiology are the most integrated and gain 12 points. |

## Firms over time: `firms_over_time.ipynb`

The graph above is one hop: a practitioner reassigns to an organization, an organization's hospital enrollment matches a CCN. [`firms_over_time.ipynb`](firms_over_time.ipynb) adds three public sources that make it transitive and weighted, and replicates the studies that used them. CMS's monthly **All Owners** and **Enrollments** files (2022 on) name every facility's owners by associate id; `graph-build --ownership` turns them into **firms**, connected components of legal entity → ultimate owner and enrollment → CCN edges whose ids carry over from quarter to quarter, and into ASPE's **chains**. The archived **Physician Compare** files (2014-2022) are the claims filter the PPEF lacks: a clinician is listed under a group only with Medicare claims under it in the past year. CMS's **Medicare utilization** files give each physician's office visits by setting.

| Measure | Paper | Result |
|---|---|---|
| Hospital chains by ultimate owner, December 2022 | ASPE (Welch, Xu, De Lew and Sommers 2023) | **Replicates.** 256 chains of three or more holding 56.5% of hospitals, against 243 and 56.1%; the eleven largest in ASPE's order, each within six hospitals. 59.6% by September 2026. |
| Nursing home chains, September 2022 | ASPE (Welch, Oliveira, Blanco and Sommers 2023) | **Exact.** The ten largest chains to the facility and every size band within two SNFs. ASPE's ownership method agrees with CMS's own chains on 84 to 89% of its pairs and finds half of CMS's. |
| Health systems | AHRQ Compendium, 2022-2023 | **Firms are as precise as chains and find more.** 69 to 70% of the hospital pairs a firm makes are in one AHRQ system; firms find 45 to 54% of AHRQ's pairs (chains 43 to 50%). Acquisitions show as dated merges (UnitedHealth and Amedisys, 2025). |
| Hospital-related tax ids with the network and name steps; service location | Luo et al. 2024; Neprash et al. 2015 | **Still a floor.** The two steps add 3.8 points by 2026 (22.1%), against Luo et al.'s 43.5% in 2019: the All Owners files cover facilities, not the practices a system owns. 12% of physicians bill their office visits in hospital settings; the two measures agree on a third of each other's integrated physicians, as Luo et al. found (38%). |
| Physicians by group size on Physician Compare | Muhlestein and Smith 2016 | **Within a point.** 34.4% in groups of 100 or more and 36.0% in groups of nine or fewer in the November 2015 file, against 35.1 and 35.3%. Claims-filtered weights cut the per-tax-id error against MD-PPAS by 10 to 25%. |

## Why the graph is built this way

A survey of the literature before building set three rules.

* **The firm is the tax id, and the public stand-in is the PECOS associate id.**
  * Welch, Cuellar, Stearns and Bindman ("Proportion of Physicians in Large Group Practices Continued to Grow in 2009-11", *Health Affairs* 32(9), 2013) defined a group as a Medicare tax id, from PECOS and Part B claims.
  * Muhlestein and Smith ("Physician Consolidation: Rapid Movement From Small To Large Group Practices, 2013-15", *Health Affairs* 35(9), 2016) used Physician Compare's group ids, which are these associate ids.
  * The public PPEF reassignment file has no tax id: only the reassigning and receiving enrollment ids. The enrollment file maps each to an NPI and an associate id.
* **Reassignment is billing, not employment, and a tax id is not a system.**
  * The AHRQ Compendium of U.S. Health Systems declined to use PECOS for system membership. Its reasons: PECOS links are incomplete, it covers only Medicare, and one system spreads its physicians over many tax ids.
  * Capps, Dranove and Ody ("The Effect of Hospital Acquisitions of Physician Practices on Prices and Spending", *J Health Econ* 59, 2018) rolled tax ids up to systems with AHA and SK&A data.
  * Luo, Black and others ("A more complete measure of vertical integration between physicians and hospitals", *Health Serv Res*, 2024) found a basic hospital-tax-id measure catches only a minority of hospital-related tax ids. They used IRS Form 990 Schedule R ownership networks to find the rest.
  * So the graph keeps organizations at the associate-id level, labels the hospital flag as a lower bound, and takes system membership from the AHRQ Compendium, which built it with Schedule R and the other ownership sources (section 6). Its own ownership layer is a next step.
* **Without claims, a physician's main group cannot be chosen.**
  * Every paper above picks a main tax id by plurality of charges or a 50 percent rule.
  * Physician Compare also hid reassignments with no claim in the past 12 months.
  * The PPEF has neither filter, so the notebook shows each measure two ways: fractional (1/k per group) and single-group physicians only. It reports a measure only when the two agree.
  * A group is sized the same way, as the sum of 1/k over its physicians. MD-PPAS's plurality rule counts each physician in one tax id, so its group sizes add up to the physicians; a headcount does not. AHRQ's group practice files, which give MD-PPAS's count for every tax id with its associate ids, confirm the choice (section 6).

Patient-sharing and referral networks (Barnett et al., *HSR* 2011; Landon et al., *JAMA* 2012; Agha, Ericson, Geissler and Rebitzer, *Management Science* 2022) need claims and are out of reach. Their warning carries over: Zand et al. (*PLOS ONE* 2017) found that network measures depend heavily on how edges are constructed. The HHI result here is an instance of that.

## File breaks to know

* **Late 2022 to early 2023: organization enrollments fall by 40,000.** Edges and practitioners keep growing through it, so this reads as a PECOS clean-up. It adds a step to the small-group decline.
* **April 2026: CMS starts publishing physician assistants' reassignments.** Practitioners with an edge rise by 155,000 and edges by 350,000. The notebook's measures count physicians only.

## Next steps

Each of these is a published measure the graph is ready for, and none is done here.

Done in `firms_over_time.ipynb`: ownership edges and ASPE's ultimate-owner method (its sections 1 and 2), systems after 2023 as ownership components scored against AHRQ (section 3), and the Doctors and Clinicians claims filter as a weight, which closes most of the gap to Muhlestein and Smith (section 5). Its own next steps are in its last cell.

1. **Physician practices in firms.** The All Owners files cover Part A facilities, so a system's physician groups join its firm only through a shared associate id. IRS Form 990 Schedule R, the source of Luo et al.'s network step, would add the practices a system controls under other tax ids.
2. **Other facilities.** Hospices, FQHCs and rural health clinics are in the ownership files and the firms; dialysis facilities are not.
3. **Tax ids.** Insurers' Transparency in Coverage price files list provider groups by tax id and NPI. Only the current month is published, so they serve as a check of the associate-id stand-in and of which reassignments are current, going forward.
4. **Fulton's primary care.** Find what in SK&A's universe or ownership roll-up puts 39 percent of MSAs above 2,500 for primary care, against the graph's 21.

## Running it

```bash
uv sync --group research
uv run healthcare-ledger download --source-name ahrq   # optional, for section 6: the pinned files into data/ahrq
uv run healthcare-ledger load --source-name ahrq       # optional: data/warehouse/ahrq.duckdb
uv run healthcare-ledger download --source-name medicaid   # optional, for section 4b: the pinned HHS zip into data/medicaid (3.8 GB)
uv run healthcare-ledger load --source-name medicaid       # optional: data/warehouse/medicaid.duckdb (about a minute)
uv run healthcare-ledger download --source-name ownership             # for firms_over_time.ipynb: the facility owner files (about 4 GB)
uv run healthcare-ledger load --source-name ownership
uv run healthcare-ledger download --source-name doctors_clinicians    # the archived Physician Compare files (about 7 GB)
uv run healthcare-ledger load --source-name doctors_clinicians
uv run healthcare-ledger download --source-name medicare_utilization  # the Medicare by-provider-and-service files (about 30 GB)
uv run healthcare-ledger load --source-name medicare_utilization
uv run healthcare-ledger export                                       # firms_over_time.ipynb reads the ownership and Physician Compare tables from the export
uv run healthcare-ledger graph-build --out graph --compendium --medicaid --ownership --doctors-clinicians --medicare-utilization   # about ten minutes; drop an option without its source
export LEDGER_GRAPH=$PWD/graph                                             # default: the repository's graph/
uv run jupyter lab research/provider-networks/   # provider_networks.ipynb, then firms_over_time.ipynb
```

Without `--medicaid`, section 4b runs on empty tables and shows nothing; without `--compendium`, so does section 6. The group primitives (`physician_q`, `physician_edge`, `group_size`, and with `--medicaid` `medicaid_volume` and `main_group`) come from `graph-build`, which runs the SQL files in `src/healthcare_ledger/derived/sql/` (METHODOLOGY section 8c); the notebook shows each file above the cell that first reads it. Each other table (`link_check`, `facility_check`, `size_shares`, `physician_specialty`, `market_hhi`, `compendium_q`, `ahrq_edge`, `system_org`, `firm_share`, `msa_hhi`, `hrr_hhi`) is built in DuckDB SQL in the cell that first needs it, with the reasoning just above it.

## Data

[`data/SOURCES.md`](data/SOURCES.md) cites the five reference files. `tests/test_graph.py` checks the builder on a hand-made warehouse. It also runs every SQL block of the notebook on the graph built from that warehouse.
