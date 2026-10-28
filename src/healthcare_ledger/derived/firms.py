"""Firms in the provider graph: who controls whom, as connected components that keep their identity over time.

The graph's edges are one hop: a practitioner reassigns to an organization, an organization's hospital enrollment
matches a CCN. Health systems and chains are not one hop. A physician group, a hospital and a nursing home belong to
one firm when an owner controls each of them, however many holding companies sit in between. CMS's monthly All Owners
files (the ``ownership`` source) name, for every hospital, nursing home, home health agency, hospice, FQHC and rural
health clinic enrollment, its direct and indirect owners by their own PECOS associate (PAC) id. With
``graph-build --ownership`` the graph adds, on each PPEF quarter from the first ownership release:

``control``
    owner -> owned edges of the quarter: an organization (PAC id) that is a 5% or greater direct or indirect owner
    (roles 34 and 35) of a facility enrollment's legal entity, with more than half of it or an unreported share. This
    is the strict-control rule of Luo et al. (2024), under which a joint venture controls neither parent, and the
    share rule of ASPE's ultimate-owner method (Welch, Xu, De Lew and Sommers 2023). Lenders (roles 43, 44),
    officers, directors and managing employees make no edge; nor do individual owners (a person who owns three
    nursing homes is a chain in ASPE's sense, not a firm here).
``firm_member``
    every organization (PAC id), facility (CCN) and controlling owner (PAC id) in force on the quarter, with the firm
    it belongs to: the connected component of the edges legal entity -> its one ultimate owner (``ultimate_owner_sql``;
    every control edge instead would let joint ventures join their partners' systems) and enrollment -> CCN: CMS's own
    CCN from the Enrollments files, and the graph's hospital and post-acute address links only for a facility type
    the quarter's Enrollments files do not cover. An address link joins two legal entities CMS keeps apart where they
    share an address: a long-term care hospital inside its host hospital, a CCN CMS no longer lists. A component is
    transitive: when A controls B and B controls hospital C, A, B and C share a firm.
``firm``
    each firm on the quarter with its counts of organizations, hospitals and post-acute facilities and a name (the
    controlling owner with the most facilities, else the organization with the most).
``chain``
    ASPE's assignment of hospitals and nursing homes to chains by ultimate owner: among owners with more than half
    or an unreported share, the one owning the most facilities defines a chain, its facilities are removed, and so
    on until the next owner would own fewer than three. A facility with no reported owner counts its own PAC id as
    its owner.

Firm ids are stable: a component keeps the id of the previous quarter's firm that holds most of its members (when
several components claim one firm, the one holding most of its members wins), so a firm survives members arriving
and leaving, and a merger shows as the absorbed firm's rows closing. A new component takes the id ``F`` + its
smallest member. The tables are interval tables like the rest of the graph (derived/graph.py).
"""

from __future__ import annotations

import duckdb

CONTROL_ROLES = ("34", "35")      # 5% or greater direct / indirect ownership interest
CHAIN_MIN = 3                     # ASPE stops at the first chain of two
CHAIN_TYPES = {"hospital": ("hospital",), "nursing_home": ("snf",)}


def _asof(d: str, alias: str = "") -> str:
    a = f"{alias}." if alias else ""
    return f"{a}effective_date <= DATE '{d}' AND ({a}end_date IS NULL OR {a}end_date > DATE '{d}')"


def quarter_sql(d: str) -> dict[str, str]:
    """The quarter's facility enrollments and control edges, from the ownership source's published tables."""
    roles = ", ".join(f"'{r}'" for r in CONTROL_ROLES)
    return {
        "fenr": f"""
            SELECT enrollment_id, facility_type, pac_id, ccn, npi, coalesce(dba_name, org_name) AS name, provider_type_code
            FROM medicare_facility_enrollment WHERE {_asof(d)} AND pac_id IS NOT NULL""",
        "fown": f"""
            SELECT o.enrollment_id, o.facility_type, o.pac_id, o.owner_pac_id, o.owner_type, o.owner_role_code,
                   o.ownership_pct, coalesce(o.owner_org_name, o.owner_dba_name) AS owner_name
            FROM medicare_facility_owner o WHERE {_asof(d, 'o')} AND o.owner_pac_id IS NOT NULL""",
        "control": f"""
            SELECT DISTINCT owner_pac_id, pac_id, any_value(owner_name) OVER (PARTITION BY owner_pac_id) AS owner_name
            FROM fown
            WHERE owner_type = 'O' AND owner_role_code IN ({roles}) AND (ownership_pct > 50 OR ownership_pct IS NULL)
              AND owner_pac_id <> pac_id""",
    }


def components(con: duckdb.DuckDBPyConnection, nodes: str, edges: str, out: str) -> int:
    """Create ``out`` (node, comp): each node of the SELECT ``nodes`` (column node) labelled with the smallest node of
    its connected component in the undirected SELECT ``edges`` (columns a, b). Label propagation with pointer jumping;
    returns the number of rounds."""
    con.execute(f"CREATE OR REPLACE TEMP TABLE _e AS SELECT a, b FROM ({edges}) UNION SELECT b, a FROM ({edges})")
    con.execute(f"CREATE OR REPLACE TEMP TABLE {out} AS SELECT DISTINCT node, node AS comp FROM ({nodes})"
                f" UNION SELECT a, a FROM _e")
    rounds = 0
    while True:
        rounds += 1
        con.execute(f"""
            CREATE OR REPLACE TEMP TABLE _n AS
            SELECT l.node, least(l.comp, coalesce(min(m.comp), l.comp)) AS comp
            FROM {out} l LEFT JOIN _e ON _e.a = l.node LEFT JOIN {out} m ON m.node = _e.b
            GROUP BY l.node, l.comp""")
        # pointer jumping: take the label of one's label
        con.execute(f"""
            CREATE OR REPLACE TEMP TABLE _n AS
            SELECT n.node, least(n.comp, coalesce(j.comp, n.comp)) AS comp FROM _n n LEFT JOIN _n j ON j.node = n.comp""")
        changed = con.execute(f"SELECT count(*) FROM _n JOIN {out} USING (node) WHERE _n.comp <> {out}.comp").fetchone()[0]
        con.execute(f"CREATE OR REPLACE TEMP TABLE {out} AS SELECT * FROM _n")
        if not changed:
            break
    con.execute("DROP TABLE _e; DROP TABLE _n")
    return rounds


def label_firms(con: duckdb.DuckDBPyConnection, d: str) -> None:
    """Turn ``comp`` (node, comp) into ``firm_label`` (node, firm_id), carrying firm ids over from ``prev_firm``
    (node, firm_id: the previous quarter's labels) and recording every id ever used in ``firm_ids``."""
    con.execute("CREATE TEMP TABLE IF NOT EXISTS prev_firm (node VARCHAR, firm_id VARCHAR)")
    con.execute("CREATE TEMP TABLE IF NOT EXISTS firm_ids (firm_id VARCHAR)")
    con.execute("""
        CREATE OR REPLACE TEMP TABLE _votes AS
        SELECT c.comp, p.firm_id, count(*) AS n FROM comp c JOIN prev_firm p USING (node) GROUP BY ALL""")
    # each earlier firm goes to the component holding most of its members; each component keeps the earlier firm
    # among those that came to it with the most members
    con.execute("""
        CREATE OR REPLACE TEMP TABLE _keep AS
        WITH home AS (SELECT * FROM _votes QUALIFY row_number() OVER (PARTITION BY firm_id ORDER BY n DESC, comp) = 1)
        SELECT comp, firm_id FROM home QUALIFY row_number() OVER (PARTITION BY comp ORDER BY n DESC, firm_id) = 1""")
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE firm_label AS
        WITH c AS (SELECT DISTINCT comp FROM comp),
        named AS (
            SELECT c.comp, coalesce(k.firm_id,
                   CASE WHEN 'F' || c.comp IN (SELECT firm_id FROM firm_ids) THEN 'F' || c.comp || '@{d}'
                        ELSE 'F' || c.comp END) AS firm_id
            FROM c LEFT JOIN _keep k USING (comp))
        SELECT comp.node, named.firm_id FROM comp JOIN named USING (comp)""")
    con.execute("INSERT INTO firm_ids SELECT DISTINCT firm_id FROM firm_label WHERE firm_id NOT IN (SELECT firm_id FROM firm_ids)")
    con.execute("CREATE OR REPLACE TEMP TABLE prev_firm AS SELECT * FROM firm_label")
    con.execute("DROP TABLE _votes; DROP TABLE _keep")


def chains(con: duckdb.DuckDBPyConnection, facility_types: tuple[str, ...], kind: str) -> str:
    """ASPE's greedy ultimate-owner assignment over the quarter's ``fenr`` and ``fown`` for enrollments of
    ``facility_types``; returns a SELECT of (ccn, chain_type, chain_owner_pac_id, chain_name, chain_size)."""
    types = ", ".join(f"'{t}'" for t in facility_types)
    roles = ", ".join(f"'{r}'" for r in CONTROL_ROLES)
    # facilities by CCN: one row per CCN (an enrollment per CCN; units sharing a parent's PAC id have their own CCN)
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE _fac AS
        SELECT ccn, any_value(pac_id) AS pac_id, list(DISTINCT enrollment_id) AS enrollments
        FROM fenr WHERE facility_type IN ({types}) AND ccn IS NOT NULL GROUP BY ccn""")
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE _own AS
        WITH o AS (
            SELECT f.ccn, w.owner_pac_id, any_value(w.owner_name) AS owner_name
            FROM _fac f JOIN fown w ON list_contains(f.enrollments, w.enrollment_id)
            WHERE w.owner_type = 'O' AND w.owner_role_code IN ({roles}) AND (w.ownership_pct > 50 OR w.ownership_pct IS NULL)
            GROUP BY ALL)
        SELECT * FROM o
        UNION ALL   -- a facility with no organizational owner of record is owned by its own legal entity
        SELECT f.ccn, f.pac_id, (SELECT any_value(name) FROM fenr WHERE fenr.pac_id = f.pac_id)
        FROM _fac f WHERE f.ccn NOT IN (SELECT ccn FROM o)""")
    con.execute("CREATE OR REPLACE TEMP TABLE _chain (ccn VARCHAR, chain_owner_pac_id VARCHAR, chain_name VARCHAR, chain_size INTEGER)")
    while True:
        top = con.execute("""
            SELECT owner_pac_id, any_value(owner_name), count(DISTINCT ccn) AS n FROM _own
            GROUP BY owner_pac_id ORDER BY n DESC, owner_pac_id LIMIT 1""").fetchone()
        if top is None or top[2] < CHAIN_MIN:
            break
        owner, name, n = top
        con.execute("INSERT INTO _chain SELECT DISTINCT ccn, ?, ?, ? FROM _own WHERE owner_pac_id = ?", [owner, name, n, owner])
        con.execute("DELETE FROM _own WHERE ccn IN (SELECT ccn FROM _chain)")
    return f"SELECT ccn, '{kind}' AS chain_type, chain_owner_pac_id, chain_name, chain_size FROM _chain"


def ultimate_owner_sql() -> str:
    """Each controlled legal entity (PAC id) of the quarter's ``control`` edges with its one ultimate owner: of the
    organizations controlling it, the one whose controlled entities hold the most facilities (CCNs), ties broken by
    PAC id. This is ASPE's rule (the owner of the largest set of facilities is the ultimate owner, since an ultimate
    owner owns everything its subsidiaries own) applied entity by entity rather than chain by chain, over every
    facility type together."""
    return """
        WITH reach AS (
            SELECT c.owner_pac_id, count(DISTINCT f.ccn) AS n
            FROM control c JOIN fenr f ON f.pac_id = c.pac_id GROUP BY 1)
        SELECT c.pac_id, c.owner_pac_id FROM control c JOIN reach r USING (owner_pac_id)
        QUALIFY row_number() OVER (PARTITION BY c.pac_id ORDER BY r.n DESC, c.owner_pac_id) = 1"""


def build_quarter(con: duckdb.DuckDBPyConnection, d: str) -> dict[str, int]:
    """Create the quarter's temp tables ``control``, ``firm_member``, ``firm`` and ``chain`` from the published
    ownership tables and the graph's own temp tables of the quarter (``organization``, ``hospital_link``,
    ``post_acute_link``, ``enr``). Returns counts for quarters.csv."""
    for name, sql in quarter_sql(d).items():
        con.execute(f"CREATE OR REPLACE TEMP TABLE {name} AS {sql}")
    # nodes: every organization of the quarter, every facility enrollment's legal entity and CCN, every controlling owner
    nodes = """
        SELECT 'pac:' || pac_id AS node FROM organization
        UNION SELECT 'pac:' || pac_id FROM fenr
        UNION SELECT 'ccn:' || ccn FROM fenr WHERE ccn IS NOT NULL
        UNION SELECT 'pac:' || owner_pac_id FROM control"""
    edges = """
        SELECT 'pac:' || owner_pac_id AS a, 'pac:' || pac_id AS b FROM control
        UNION SELECT 'pac:' || pac_id, 'ccn:' || ccn FROM fenr WHERE ccn IS NOT NULL
        UNION SELECT 'pac:' || organization_pac_id, 'ccn:' || ccn FROM hospital_link
        WHERE NOT EXISTS (SELECT 1 FROM fenr WHERE facility_type = 'hospital')
        UNION SELECT 'pac:' || organization_pac_id, 'ccn:' || ccn FROM post_acute_link l
        WHERE NOT EXISTS (SELECT 1 FROM fenr WHERE facility_type = CASE l.facility_type WHEN 'nursing_home' THEN 'snf'
                                                                                         ELSE 'hha' END)"""
    # each owned legal entity hangs from one ultimate owner (ultimate_owner_sql), not from every owner it reports:
    # with every control edge, joint ventures and unreported shares join unrelated systems into one component
    con.execute(f"CREATE OR REPLACE TEMP TABLE ultimate AS {ultimate_owner_sql()}")
    edges = edges.replace("SELECT 'pac:' || owner_pac_id AS a, 'pac:' || pac_id AS b FROM control",
                          "SELECT 'pac:' || owner_pac_id AS a, 'pac:' || pac_id AS b FROM ultimate")
    rounds = components(con, nodes, edges, "comp")
    label_firms(con, d)
    con.execute("""
        CREATE OR REPLACE TEMP TABLE firm_member AS
        SELECT split_part(node, ':', 1) AS node_type, split_part(node, ':', 2) AS node_id, firm_id FROM firm_label""")
    con.execute("""
        CREATE OR REPLACE TEMP TABLE firm AS
        WITH m AS (SELECT * FROM firm_member),
        hosp AS (SELECT ccn FROM hospital UNION SELECT ccn FROM fenr WHERE facility_type = 'hospital'),
        pa AS (SELECT ccn FROM post_acute_facility UNION SELECT ccn FROM fenr WHERE facility_type IN ('snf', 'hha', 'hospice')),
        owner_reach AS (   -- each controlling owner's facilities, for the firm's name
            SELECT c.owner_pac_id, any_value(c.owner_name) AS owner_name, count(DISTINCT f.ccn) AS n
            FROM control c JOIN fenr f ON f.pac_id = c.pac_id GROUP BY 1),
        org_reach AS (SELECT pac_id, any_value(name) AS name, count(DISTINCT ccn) AS n FROM fenr GROUP BY 1),
        named AS (
            SELECT m.firm_id,
                   arg_max(coalesce(o.owner_name, r.name, g.org_name),
                           coalesce(o.n, 0) * 1e6 + coalesce(r.n, 0) * 1e3 + CASE WHEN g.pac_id IS NOT NULL THEN 1 ELSE 0 END) AS name
            FROM m
            LEFT JOIN owner_reach o ON m.node_type = 'pac' AND o.owner_pac_id = m.node_id
            LEFT JOIN org_reach r ON m.node_type = 'pac' AND r.pac_id = m.node_id
            LEFT JOIN organization g ON m.node_type = 'pac' AND g.pac_id = m.node_id
            GROUP BY 1)
        SELECT m.firm_id, any_value(n.name) AS name,
               count(*) FILTER (WHERE m.node_type = 'pac' AND m.node_id IN (SELECT pac_id FROM organization)) AS n_organizations,
               count(*) FILTER (WHERE m.node_type = 'ccn' AND m.node_id IN (SELECT ccn FROM hosp)) AS n_hospitals,
               count(*) FILTER (WHERE m.node_type = 'ccn' AND m.node_id IN (SELECT ccn FROM pa)) AS n_post_acute,
               count(*) FILTER (WHERE m.node_type = 'ccn') AS n_facilities
        FROM m JOIN named n USING (firm_id) GROUP BY m.firm_id""")
    # chains() reuses its scratch tables, so each kind is materialized before the next runs
    con.execute("CREATE OR REPLACE TEMP TABLE chain (ccn VARCHAR, chain_type VARCHAR, chain_owner_pac_id VARCHAR, "
                "chain_name VARCHAR, chain_size INTEGER)")
    for kind, types in CHAIN_TYPES.items():
        con.execute(f"INSERT INTO chain {chains(con, types, kind)}")
    return {"n_control": con.execute("SELECT count(*) FROM control").fetchone()[0], "firm_rounds": rounds}
