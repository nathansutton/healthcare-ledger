"""The graph's crosswalk: one long, interval-dated table of the links between NPI, Medicare enrollment, PAC id, CCN
and health system that the other graph tables hold in their own shapes.

Each row is a directed edge from a finer identifier to a coarser one, with the interval it holds and where it
comes from. ``graph-build`` writes it; nothing in it is inferred beyond the graph's own tables:

``npi -> enrollment``
    the enrollment's NPI in the PPEF enrollment file (``source`` 'ppef_enrollment') and its further NPIs in the
    PPEF additional NPI file ('ppef_additional_npi', from October 2025).
``enrollment -> pac``
    the enrollment's PECOS associate id in the PPEF enrollment file.
``enrollment -> ccn``
    the graph's ``hospital_link`` and ``facility_link`` rows; ``method`` is 'match_rule_<n>', the rule that matched.
``pac -> system``, ``ccn -> system``
    with ``--compendium``, the ``system_link`` rows that name a system: an organization's associate id from the
    group practice file, or a hospital, nursing home or home health CCN from its file (``source`` the ``ahrq_*``
    table). These are annual: a Compendium year's edge holds from 1 January of that year to 1 January of the next
    Compendium year (NULL for the latest). ``method`` is 'carried_forward' for organizations of an edition with no
    group practice file, 'registry' otherwise.

The quarterly edges have the graph's interval semantics (docs/METHODOLOGY.md section 8b): a row holds on date
``d`` when ``effective_date <= d`` and ``end_date`` is NULL or later than ``d``; an edge that lapses and returns
makes a new row, and so does a change of ``method``.
"""

from __future__ import annotations

from .compendium import GROUP_PRACTICE, LINKS

KEY = ("from_type", "from_id", "to_type", "to_id", "source")
ATTRS = ("method",)
# the written column order
COLUMNS = ("from_type", "from_id", "to_type", "to_id", "effective_date", "end_date", "source", "method")
DESCRIPTION = ("interval-dated edges npi -> enrollment -> pac and enrollment -> ccn by PPEF quarter, and pac or ccn "
               "-> AHRQ system by Compendium year")


def quarter_sql(d: str) -> str:
    """The SELECT giving the quarterly edges on date d, from the graph's temp tables enr, hospital_link and
    facility_link of that quarter and the PPEF additional NPI versions in force that day."""
    return f"""
        SELECT DISTINCT 'npi' AS from_type, npi AS from_id, 'enrollment' AS to_type, enrollment_id AS to_id,
               'ppef_enrollment' AS source, 'registry' AS method
        FROM enr WHERE npi IS NOT NULL
        UNION ALL
        SELECT DISTINCT 'npi', npi, 'enrollment', enrollment_id, 'ppef_additional_npi', 'registry'
        FROM medicare_additional_npi_version
        WHERE effective_date <= DATE '{d}' AND (end_date IS NULL OR end_date > DATE '{d}') AND npi IS NOT NULL
        UNION ALL
        SELECT DISTINCT 'enrollment', enrollment_id, 'pac', pac_id, 'ppef_enrollment', 'registry'
        FROM enr WHERE pac_id IS NOT NULL
        UNION ALL
        SELECT 'enrollment', enrollment_id, 'ccn', ccn, 'hospital_link', 'match_rule_' || match_rule FROM hospital_link
        UNION ALL
        SELECT 'enrollment', enrollment_id, 'ccn', ccn, 'facility_link', 'match_rule_' || match_rule FROM facility_link"""


def system_edges_sql() -> str:
    """The SELECT giving the annual pac -> system and ccn -> system edges from the graph's system and system_link
    tables (derived/compendium.py)."""
    sources = {"organization": GROUP_PRACTICE} | {node: table for table, (node, *_) in LINKS.items()}
    source = "CASE node_type " + " ".join(f"WHEN '{n}' THEN '{t}'" for n, t in sources.items()) + " END"
    return f"""
        SELECT DISTINCT CASE WHEN node_type = 'organization' THEN 'pac' ELSE 'ccn' END AS from_type,
               node_id AS from_id, 'system' AS to_type, health_sys_id AS to_id, {source} AS source,
               CASE WHEN via LIKE 'carried forward%' THEN 'carried_forward' ELSE 'registry' END AS method,
               make_date(l.year, 1, 1) AS effective_date, make_date(e.next_year, 1, 1) AS end_date
        FROM system_link l
        JOIN (SELECT year, lead(year) OVER (ORDER BY year) AS next_year FROM (SELECT DISTINCT year FROM system)) e
          USING (year)
        WHERE health_sys_id IS NOT NULL"""
