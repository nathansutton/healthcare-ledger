"""Owner communities (derived/owner_communities.py): evidence cells, hard units, clustering and ids over time, on
handmade input tables. The end-to-end build on the fixture graph is in test_graph.py."""
import duckdb
import pytest

from healthcare_ledger.derived import owner_communities as oc


def _con(orgs: dict[str, str], keys: list[tuple[str, str, str]]) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("CREATE TEMP TABLE oc_org (pac_id VARCHAR, org_name VARCHAR, state VARCHAR)")
    con.executemany("INSERT INTO oc_org VALUES (?, ?, 'MA')", list(orgs.items()))
    con.execute("CREATE TEMP TABLE oc_key (pac_id VARCHAR, kind VARCHAR, key VARCHAR)")
    con.executemany("INSERT INTO oc_key VALUES (?, ?, ?)", keys)
    for t, cols in (("oc_firm", "pac_id VARCHAR, firm_id VARCHAR"), ("oc_firm_sys", "firm_id VARCHAR, sys VARCHAR"),
                    ("oc_fac_sys", "pac_id VARCHAR, sys VARCHAR"), ("oc_sys_group", "pac_id VARCHAR, sys VARCHAR")):
        con.execute(f"CREATE TEMP TABLE {t} ({cols})")
    return con


def _cells(con) -> dict[tuple[str, str, str], str]:
    return {(x, y, k): c for x, y, k, c in con.execute("SELECT x, y, kind, cell FROM oc_pair").fetchall()}


def test_a_vendor_address_is_scattered_and_a_family_address_cohesive():
    # V1-V4 share a billing vendor's mailing address but each names its own official; F1-F3 share an address and one
    # official, and a name word no other organization carries
    orgs = {"V1": "ALPHA MD PA", "V2": "BETA PEDIATRICS", "V3": "GAMMA EYE CARE", "V4": "DELTA FOOT CLINIC",
            "F1": "ZYLOR HEALTH OF OHIO LLC", "F2": "ZYLOR HEALTH OF TEXAS LLC", "F3": "ZYLOR CARDIOLOGY LLC"}
    keys = [(p, "mail", "PO BOX 1|02101") for p in ("V1", "V2", "V3", "V4")]
    keys += [(p, "ao", f"OFFICIAL {p}|6170000000") for p in ("V1", "V2", "V3", "V4")]
    keys += [(p, "mail", "1 MAIN ST|43004") for p in ("F1", "F2", "F3")] + [(p, "ao", "ONE OFFICIAL|6140000000") for p in ("F1", "F2", "F3")]
    con = _con(orgs, keys)
    oc.evidence(con)
    cells = _cells(con)
    assert cells[("V1", "V2", "mail")] == "3-5/scattered/no shared name"
    assert cells[("F1", "F2", "mail")] == "3-5/cohesive/rare name"
    assert cells[("F1", "F3", "ao")] == "3-5/cohesive/rare name"
    assert not any(k == "ao" and x.startswith("V") for x, _, k in cells)        # each vendor client's own official


def test_legal_form_words_never_make_a_shared_name():
    con = _con({"A": "SMITH MEDICAL LLC", "B": "JONES MEDICAL LLC"}, [("A", "phone", "6170000001"), ("B", "phone", "6170000001")])
    oc.evidence(con)
    # MEDICAL is shared (and rare among two organizations); LLC is a stop word
    assert _cells(con)[("A", "B", "phone")].endswith("/rare name")
    con = _con({"A": "SMITH LLC", "B": "JONES LLC"}, [("A", "phone", "6170000001"), ("B", "phone", "6170000001")])
    oc.evidence(con)
    assert _cells(con)[("A", "B", "phone")].endswith("/no shared name")


def test_calibration_counts_known_pairs_by_cell():
    orgs = {p: f"ORG {p}" for p in "ABCD"}
    con = _con(orgs, [(p, "phone", "6170000001") for p in "ABCD"])
    con.execute("INSERT INTO oc_firm VALUES ('A', 'F1'), ('B', 'F1'), ('C', 'F2')")
    con.execute("INSERT INTO oc_sys_group VALUES ('D', 'S1')")
    oc.evidence(con)
    oc.calibrate(con, min_pairs=1)
    # firm pairs only: A-B same, A-C and B-C different; D is a system group with no other known pair
    (kind, cell, pairs, p), = con.execute("SELECT * FROM oc_calib").fetchall()
    assert (kind, pairs, round(p, 3)) == ("phone", 3, 0.333)


def test_hard_units_never_chain_systems_and_staffing_families_keep_their_family():
    orgs = {p: f"ORG {p}" for p in ("H", "F", "G1", "G2", "G3", "G4", "X", "Y")}
    # G1-G3 are one staffing family (one official at one address), each placed by AHRQ in the system it staffs; G4 is
    # a system's own group
    keys = [(p, k, v) for p in ("G1", "G2", "G3") for k, v in (("ao", "STAFF OFFICIAL|6150000000"), ("mail", "1 HQ WAY|37919"))]
    con = _con(orgs, keys)
    con.execute("INSERT INTO oc_firm VALUES ('F', 'FIRM1'), ('X', 'FIRM1'), ('Y', 'FIRM2')")
    con.execute("INSERT INTO oc_firm_sys VALUES ('FIRM1', 'S1')")
    con.execute("INSERT INTO oc_fac_sys VALUES ('H', 'S2'), ('X', 'S3')")       # X's firm's system wins over its own
    con.execute("INSERT INTO oc_sys_group VALUES ('G1', 'S1'), ('G2', 'S2'), ('G3', 'S3'), ('G4', 'S1')")
    oc.evidence(con)
    oc.units(con)
    unit = {p: (u, b) for p, u, b in con.execute("SELECT pac_id, unit, basis FROM oc_unit").fetchall()}
    assert {r[0] for r in con.execute("SELECT pac_id FROM oc_staffing").fetchall()} == {"G1", "G2", "G3"}
    assert unit["F"] == unit["X"] == ("SS1", "all_owners")
    assert unit["Y"] == ("FFIRM2", "all_owners")
    assert unit["H"] == ("SS2", "ahrq") and unit["G4"] == ("SS1", "ahrq")
    assert unit["G1"] == ("PG1", None) and unit["G2"] == ("PG2", None)


@pytest.fixture
def family():
    """Five organizations: A-D one family (shared official, p 0.9); E a vendor client linked to A by a scattered
    address (p 0.1); F and G one All Owners firm with no NPPES link."""
    orgs = {p: f"ORG {p}" for p in "ABCDEFG"}
    keys = [(p, "ao", "FAMILY OFFICIAL|6170000000") for p in "ABCD"] + [(p, "mail", "PO BOX 9|02101") for p in "AE"]
    con = _con(orgs, keys)
    con.execute("INSERT INTO oc_firm VALUES ('F', 'FIRM1'), ('G', 'FIRM1')")
    con.execute("CREATE TEMP TABLE oc_size AS SELECT pac_id, 1.0 AS physicians, 0 AS n_hospitals, 0 AS n_post_acute FROM oc_org")
    oc.evidence(con)
    con.execute("CREATE TEMP TABLE oc_calib AS SELECT DISTINCT kind, cell, 10 AS pairs, CASE kind WHEN 'ao' THEN 0.9 ELSE 0.1 END AS p FROM oc_pair")
    return con


def _members(con) -> dict[str, str]:
    return dict(con.execute("SELECT pac_id, community_id FROM owner_community_member").fetchall())


def test_communities_follow_the_weighed_evidence(family):
    con = family
    oc.units(con)
    oc.cluster(con)
    oc.label(con, "2020-01-01")
    m = _members(con)
    assert m["A"] == m["B"] == m["C"] == m["D"] == "OA"            # the family, named O + its smallest PAC id
    assert m["E"] == "OE"                                          # a weak vendor link does not pull E in
    assert m["F"] == m["G"] == "OF" and m["F"] != m["A"]           # the firm holds with no link at all
    basis = dict(con.execute("SELECT pac_id, basis FROM owner_community_member").fetchall())
    assert (basis["A"], basis["E"], basis["F"]) == ("nppes", "alone", "all_owners")
    assert con.execute("SELECT n_organizations, physicians FROM owner_community WHERE community_id = 'OA'").fetchone() == (4, 4.0)


def test_ids_carry_over_and_a_hard_link_moves_an_organization(family):
    con = family
    oc.units(con)
    oc.cluster(con)
    oc.label(con, "2020-01-01")
    first = _members(con)
    # next quarter: A's family gains 0H, whose smaller PAC id would name a fresh community O0H
    con.execute("INSERT INTO oc_org VALUES ('0H', 'ORG 0H', 'MA')")
    con.execute("INSERT INTO oc_key VALUES ('0H', 'ao', 'FAMILY OFFICIAL|6170000000')")
    con.execute("INSERT INTO oc_size VALUES ('0H', 1.0, 0, 0)")
    con.execute("INSERT INTO oc_org VALUES ('Z9', 'ORG Z9', 'MA')")      # new and alone, sorting after every unit
    con.execute("INSERT INTO oc_size VALUES ('Z9', 1.0, 0, 0)")
    oc.evidence(con)
    con.execute("CREATE OR REPLACE TEMP TABLE oc_calib AS SELECT DISTINCT kind, cell, 10 AS pairs, CASE kind WHEN 'ao' THEN 0.9 ELSE 0.1 END AS p FROM oc_pair")
    oc.units(con)
    oc.cluster(con)
    oc.label(con, "2020-04-01")
    m = _members(con)
    assert m["0H"] == m["A"] == first["A"] == "OA"                  # the family keeps its id though 0H < A
    assert m["Z9"] == "OZ9"
    con.execute("INSERT INTO oc_firm VALUES ('B', 'FIRM1')")
    oc.units(con)
    oc.cluster(con)
    oc.label(con, "2020-07-01")
    m = _members(con)
    # the firm buys B: B leaves the family for the firm's community, whose id holds (F and G came from it); the
    # family's links to B alone (4 x 0.9 against 3 x 4 pairs at 1/2) do not merge the firm with the family
    assert m["B"] == m["F"] == m["G"] == "OF" and m["A"] == m["0H"] == "OA"


def test_a_pair_together_last_quarter_holds_at_a_lower_bar():
    # X and Y share one key at p 0.45: below GAMMA, above GAMMA - HISTORY
    con = _con({"X": "ORG X", "Y": "ORG Y"}, [("X", "phone", "6170000001"), ("Y", "phone", "6170000001")])
    oc.evidence(con)
    con.execute("CREATE TEMP TABLE oc_calib AS SELECT DISTINCT kind, cell, 10 AS pairs, 0.45 AS p FROM oc_pair")
    oc.units(con)
    oc.cluster(con)
    assert dict(con.execute("SELECT * FROM comp").fetchall()) == {"X": "X", "Y": "Y"}       # apart, it does not join
    con.execute("CREATE OR REPLACE TEMP TABLE prev_firm AS SELECT * FROM (VALUES ('X', 'OX'), ('Y', 'OX')) t(node, firm_id)")
    oc.cluster(con)
    assert dict(con.execute("SELECT * FROM comp").fetchall()) == {"X": "X", "Y": "X"}       # together, it holds
    oc.cluster(con, history=0)
    assert dict(con.execute("SELECT * FROM comp").fetchall()) == {"X": "X", "Y": "Y"}


def test_holes_of_up_to_max_hole_quarters_are_closed():
    con = duckdb.connect()
    q = ["2020-01-01", "2020-04-01", "2020-07-01", "2020-10-01", "2021-01-01", "2021-04-01", "2021-07-01", "2021-10-01"]
    con.execute("CREATE TEMP TABLE oc_quarter AS SELECT unnest(?::DATE[]) AS d", [q])
    con.execute("""CREATE TEMP TABLE t AS SELECT * FROM (VALUES
        ('A', 'F1', DATE '2020-01-01', DATE '2020-04-01'), ('A', 'F1', DATE '2020-07-01', NULL),          -- one quarter out
        ('B', 'F1', DATE '2020-01-01', DATE '2020-04-01'), ('B', 'F1', DATE '2021-04-01', NULL),          -- four out
        ('C', 'F1', DATE '2020-01-01', DATE '2020-04-01'), ('C', 'F2', DATE '2020-07-01', NULL),          -- came back elsewhere
        ('D', 'F1', DATE '2020-01-01', DATE '2020-04-01'), ('D', 'F1', DATE '2020-10-01', DATE '2021-01-01'))  -- two out, then gone
        v(node_id, firm_id, effective_date, end_date)""")
    rows = con.execute(f"SELECT node_id, effective_date::VARCHAR, end_date::VARCHAR, _hole FROM ({oc.filled('t', 'node_id', 'firm_id')}) "
                       "ORDER BY 1, 2").fetchall()
    assert rows == [("A", "2020-01-01", "2020-07-01", True), ("A", "2020-07-01", None, False),
                    ("B", "2020-01-01", "2020-04-01", False), ("B", "2021-04-01", None, False),
                    ("C", "2020-01-01", "2020-04-01", False), ("C", "2020-07-01", None, False),
                    ("D", "2020-01-01", "2020-10-01", True), ("D", "2020-10-01", "2021-01-01", False)]


def test_missing_leiden_is_a_user_error(monkeypatch):
    import builtins
    from healthcare_ledger.core.errors import UserError
    real = builtins.__import__
    monkeypatch.setattr(builtins, "__import__", lambda name, *a, **k: (_ for _ in ()).throw(ImportError(name))
                        if name in ("igraph", "leidenalg") else real(name, *a, **k))
    with pytest.raises(UserError, match="communities"):
        oc._leiden()
