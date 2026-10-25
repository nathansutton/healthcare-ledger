# Provider networks

Can the public CMS files in this warehouse be joined into one graph of who practices where and for whom, and does that graph reproduce what health economists have measured about consolidation?

`nppes-over-time graph-build` builds the graph quarter by quarter, February 2016 to September 2026 (43 PPEF quarters; [METHODOLOGY.md](../../docs/METHODOLOGY.md) section 8). Its nodes are practitioners and organizations, keyed by PECOS associate id, and active POS hospitals, keyed by CCN. It has two kinds of edges. A **reassignment** edge runs from a practitioner to each organization that bills Medicare for them. A **hospital link** runs from a hospital's Medicare enrollment to its CCN, matched by address and phone and checked against CMS's own crosswalk: right 97 to 100 percent of the time, and found for 89 percent of linkable enrollments. In September 2026 the graph holds 2.2 million practitioners, 291,000 organizations, 7,031 hospitals and 3.7 million reassignment edges.

[`provider_networks.ipynb`](provider_networks.ipynb) replicates three published measures and extends each over the decade:

| Measure | Paper | Result |
|---|---|---|
| Share of physicians by group size | Welch et al. 2013; Muhlestein and Smith 2016 | **Replicates in direction.** Physicians in groups of 100 or more rise from 46 to 67 percent, 2016 to 2026; those in groups of nine or fewer fall from 24 to 13 percent. Both hold whether multi-group physicians are split fractionally or left out. Levels are about 10 points more consolidated than the papers report, for known reasons the public data cannot remove. The papers' finding that primary care consolidated faster than specialties reverses after 2016. |
| Share of local markets highly concentrated (HHI > 2,500), HRR by specialty | Capps, Dranove and Ody 2017; Fulton 2017 | **Not identified.** The sign of the trend depends on how a physician with several organizations is counted, and the public files have no billing volume to choose their main one. |
| Physicians billing through an organization that bills as a hospital | Luo et al. 2024 ("basic TIN" variant) | **A floor.** The share rises from 17 to 18 percent fractional, or 24 to 29 percent with any hospital reassignment. This is below Luo et al.'s 43 percent in 2019, as the basic measure must be. |

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
  * So the graph keeps organizations at the associate-id level, labels the hospital flag as a lower bound, and leaves system roll-up for an ownership layer (next steps).
* **Without claims, a physician's main group cannot be chosen.**
  * Every paper above picks a main tax id by plurality of charges or a 50 percent rule.
  * Physician Compare also hid reassignments with no claim in the past 12 months.
  * The PPEF has neither filter, so the notebook shows each measure two ways: fractional (1/k per group) and single-group physicians only. It reports a measure only when the two agree.

Patient-sharing and referral networks (Barnett et al., *HSR* 2011; Landon et al., *JAMA* 2012; Agha, Ericson, Geissler and Rebitzer, *Management Science* 2022) need claims and are out of reach. Their warning carries over: Zand et al. (*PLOS ONE* 2017) found that network measures depend heavily on how edges are constructed. The HHI result here is an instance of that.

## File breaks to know

* **Late 2022 to early 2023: organization enrollments fall by 40,000.** Edges and practitioners keep growing through it, so this reads as a PECOS clean-up. It adds a step to the small-group decline.
* **April 2026: CMS starts publishing physician assistants' reassignments.** Practitioners with an edge rise by 155,000 and edges by 350,000. The notebook's measures count physicians only.

## Next steps

Each of these is a published measure the graph is ready for, and none is done here.

1. **Ownership edges.** Load CMS's monthly All Owners files (hospitals, nursing homes, home health agencies, hospices, from late 2022). They give direct and indirect owners by associate id, with private-equity and REIT flags. This would follow ASPE's ultimate-owner method (Welch, Xu, De Lew and Sommers, 2023).
2. **Systems.** Find connected components over ownership edges, the step Luo et al. took with Schedule R. Validate against the AHRQ Compendium hospital linkage file.
3. **Other facilities.** Extend the hospital link to nursing homes, home health agencies, hospices and dialysis facilities.
4. **Staleness.** Use archived Doctors and Clinicians files (claims-filtered group membership) to measure how much of the level gap comes from stale reassignments.

## Running it

```bash
uv sync --group research
uv run nppes-over-time graph-build --db data/nppes.e3.duckdb --out graph   # about two minutes
export NPPES_GRAPH=$PWD/graph                                             # default: the repository's graph/
uv run jupyter lab research/provider-networks/provider_networks.ipynb
```

The notebook runs in about fifteen seconds. Each table (`link_check`, `physician_q`, `physician_edge`, `group_size`, `size_shares`, `physician_specialty`, `market_hhi`) is built in DuckDB SQL in the cell that first needs it, with the reasoning just above it.

## Data

[`data/SOURCES.md`](data/SOURCES.md) cites both reference files. `tests/test_graph.py` checks the builder on a hand-made warehouse. It also runs every SQL block of the notebook on the graph built from that warehouse.
