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


def _cells(con) -> dict[tuple[str, str], tuple[str, str]]:
    """(x, y) -> (kinds, cell) of every pair."""
    return {(x, y): (k, c) for x, y, k, c in con.execute("SELECT x, y, kinds, cell FROM oc_pair").fetchall()}


def _calib(con, p: dict[str, float] | float, replace: bool = False) -> None:
    """A level-0 calibration over the pairs' own cells: ``p`` by kinds, or one p for every cell."""
    case = (f"CASE kinds {' '.join(f"WHEN '{k}' THEN {v}" for k, v in p.items())} END" if isinstance(p, dict) else str(p))
    con.execute(f"CREATE {'OR REPLACE ' if replace else ''}TEMP TABLE oc_calib AS "
                f"SELECT DISTINCT 0 AS level, kinds, size, coh, name, 10 AS pairs, {case} AS p FROM oc_pair")


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
    assert cells[("V1", "V2")] == ("mail", "3-5/scattered/no shared name")
    # the family pair shares two kinds, priced together; its strongest key (a tie at three, so the official) is cohesive
    assert cells[("F1", "F2")] == cells[("F1", "F3")] == ("ao+mail", "3-5/cohesive/rare name")
    assert not any(x.startswith("V") and "ao" in k for (x, _), (k, _) in cells.items())    # each vendor client's own official


def test_legal_form_words_never_make_a_shared_name():
    con = _con({"A": "SMITH MEDICAL LLC", "B": "JONES MEDICAL LLC"}, [("A", "phone", "6170000001"), ("B", "phone", "6170000001")])
    oc.evidence(con)
    # MEDICAL is shared (and rare among two organizations); LLC is a stop word
    assert _cells(con)[("A", "B")][1].endswith("/rare name")
    con = _con({"A": "SMITH LLC", "B": "JONES LLC"}, [("A", "phone", "6170000001"), ("B", "phone", "6170000001")])
    oc.evidence(con)
    assert _cells(con)[("A", "B")][1].endswith("/no shared name")


def test_calibration_counts_known_pairs_by_cell():
    orgs = {p: f"ORG {p}" for p in "ABCD"}
    con = _con(orgs, [(p, "phone", "6170000001") for p in "ABCD"])
    con.execute("INSERT INTO oc_firm VALUES ('A', 'F1'), ('B', 'F1'), ('C', 'F2')")
    con.execute("INSERT INTO oc_sys_group VALUES ('D', 'S1')")
    oc.evidence(con)
    oc.units(con)
    oc.calibrate(con, min_pairs=1)
    # firm pairs only: A-B same, A-C and B-C different; D is a system member, never compared with a firm member; one
    # cell at each of the three levels
    rows = con.execute("SELECT level, kinds, size, coh, name, pairs, round(p, 3) FROM oc_calib ORDER BY level").fetchall()
    assert rows == [(0, "phone", "3-5", "scattered", "rare name", 3, 0.333), (1, "phone", "3-5", None, None, 3, 0.333),
                    (2, "phone", "3-5", None, None, 3, 0.333)]


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
    _calib(con, {"ao": 0.9, "mail": 0.1})
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
    _calib(con, {"ao": 0.9, "mail": 0.1}, replace=True)
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
    _calib(con, 0.45)
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


def test_a_firm_joins_a_system_only_with_most_of_its_listed_facilities():
    con = duckdb.connect()
    # the Compendium lists every facility, most nursing homes outside any system
    con.execute("""CREATE TEMP TABLE oc_ccn_listed AS SELECT * FROM (VALUES
        ('H1', 'S1', TRUE), ('H2', 'S1', TRUE), ('H3', 'S2', TRUE), ('N1', 'S1', FALSE), ('N2', NULL, FALSE),
        ('N3', NULL, FALSE), ('N4', 'S2', FALSE), ('N5', 'S2', FALSE)) v(ccn, sys, hospital)""")
    con.execute("""CREATE TEMP TABLE src AS SELECT * FROM (VALUES
        ('SYS', 'H1'), ('SYS', 'H2'), ('SYS', 'H3'), ('SYS', 'N2'), ('SYS', 'N3'),     -- 2 of 3 hospitals: S1
        ('TIE', 'H1'), ('TIE', 'H3'),                                                   -- 1 of 2: no majority
        ('CHAIN', 'N1'), ('CHAIN', 'N2'), ('CHAIN', 'N3'),                              -- 1 of 3 homes: none
        ('PAC', 'N4'), ('PAC', 'N5'), ('PAC', 'N2')) v(firm_id, ccn)""")             # 2 of 3 homes: S2
    assert dict(con.execute(oc.majority_sys("src", "firm_id")).fetchall()) == {"SYS": "S1", "PAC": "S2"}


def test_a_community_is_named_for_its_system_then_its_firm_then_its_largest_organization(family):
    con = family
    con.execute("UPDATE oc_size SET n_post_acute = 1 WHERE pac_id IN ('B', 'C')")      # B and C tie; B has the smaller id
    con.execute("CREATE TEMP TABLE oc_firm_name AS SELECT 'FIRM1' AS firm_id, 'FIRM ONE HOLDINGS' AS name")
    oc.units(con); oc.cluster(con); oc.label(con, "2020-01-01")
    names = dict(con.execute("SELECT community_id, name FROM owner_community").fetchall())
    assert (names["OA"], names["OF"], names["OE"]) == ("ORG B", "FIRM ONE HOLDINGS", "ORG E")
    con.execute("INSERT INTO oc_firm_sys VALUES ('FIRM1', 'S9')")
    con.execute("INSERT INTO oc_sys_name VALUES ('S9', 'Nine Health')")
    oc.units(con); oc.cluster(con); oc.label(con, "2020-04-01")
    assert dict(con.execute("SELECT community_id, name FROM owner_community").fetchall())["OF"] == "Nine Health"


def test_missing_leiden_is_a_user_error(monkeypatch):
    import builtins
    from healthcare_ledger.core.errors import UserError
    real = builtins.__import__
    monkeypatch.setattr(builtins, "__import__", lambda name, *a, **k: (_ for _ in ()).throw(ImportError(name))
                        if name in ("igraph", "leidenalg") else real(name, *a, **k))
    with pytest.raises(UserError, match="communities"):
        oc._leiden()


def test_the_strongest_key_is_the_one_shared_by_the_fewest_organizations():
    # A and B share a phone of their own (two organizations, one official: cohesive) and a mailing address of a
    # vendor's with 150 clients. The pair is priced by the phone: size 2, cohesive. Comparing the size labels as text
    # would pick '101+' (which sorts before '2') and the vendor's scattered cohesion.
    orgs = {f"C{i:03d}": f"CLIENT {i}" for i in range(150)}
    orgs.update({"A": "ALPHA CARE", "B": "BETA CARE"})
    keys = [(p, "mail", "PO BOX 1|02101") for p in orgs] + [(p, "ao", f"OFFICIAL {p}|6170000000") for p in orgs if p[0] == "C"]
    keys += [("A", "phone", "6175550100"), ("B", "phone", "6175550100"), ("A", "ao", "ONE OFFICIAL|6175550100"), ("B", "ao", "ONE OFFICIAL|6175550100")]
    con = _con(orgs, keys)
    oc.evidence(con)
    kinds, kind1, size, coh = con.execute("SELECT kinds, kind1, size, coh FROM oc_pair WHERE x = 'A' AND y = 'B'").fetchone()
    assert (kinds, kind1, size, coh) == ("ao+mail+phone", "ao", "2", "cohesive")
    # the clients' only shared key is the vendor's address (A and B share one official among 151, so it is mixed)
    assert con.execute("SELECT kinds, size, coh FROM oc_pair WHERE x = 'C000' AND y = 'C001'").fetchone() == ("mail", "101+", "mixed")


def test_pairs_back_off_to_coarser_cells_and_holding_companies_are_not_members():
    con = _con({p: f"ORG {p}" for p in "ABCD"}, [(p, "phone", "6170000001") for p in "ABCD"] + [("A", "mail", "1 WAY|02101"), ("B", "mail", "1 WAY|02101")])
    # HOLD is a firm's owner with no enrollment of its own: in oc_firm, not in oc_org
    con.execute("INSERT INTO oc_firm VALUES ('A', 'F1'), ('B', 'F1'), ('C', 'F2'), ('HOLD', 'F1')")
    oc.evidence(con)
    # a calibration with only the strongest-kind level for a phone of 3-5: C-D is priced there; A-B (mail+phone, its
    # strongest key the address of two) has no cell at any level and carries no weight until a level-1 cell appears
    con.execute("CREATE TEMP TABLE oc_calib AS SELECT * FROM (VALUES (2, 'phone', '3-5', NULL, NULL, 30, 0.7::DOUBLE)) t(level, kinds, size, coh, name, pairs, p)")
    w = {(x, y): (round(w, 2), lv) for x, y, w, lv in con.execute(oc.weights_sql()).fetchall()}
    assert w[("C", "D")] == (0.7, 2) and ("A", "B") not in w
    con.execute("INSERT INTO oc_calib VALUES (1, 'mail+phone', '2', NULL, NULL, 30, 0.95::DOUBLE)")
    w = {(x, y): (round(w, 2), lv) for x, y, w, lv in con.execute(oc.weights_sql()).fetchall()}
    assert w[("A", "B")] == (0.95, 1) and w[("C", "D")] == (0.7, 2)
    oc.units(con)
    assert {r[0] for r in con.execute("SELECT pac_id FROM oc_unit").fetchall()} == set("ABCD")


def test_support_margin_and_events_are_recorded_each_quarter(family):
    con = family
    oc.units(con); oc.cluster(con); oc.label(con, "2020-01-01")
    sup = {p: (s, m) for p, s, m in con.execute("SELECT pac_id, support, margin FROM owner_community_support").fetchall()}
    # A has three family links at 0.9 among four members: support 2.7, margin 2.7 - 0.5 x 3; F and G hold by their
    # firm alone, with no NPPES link: support 0, margin -0.5; E is alone and has no row
    assert sup["A"] == (2.7, 1.2) and sup["F"] == (0.0, -0.5) and "E" not in sup
    events = con.execute("SELECT event, sum(n_organizations) FROM owner_community_event GROUP BY 1").fetchall()
    assert events == [("entry", 7)]
    # next quarter: the firm buys B (a split off the family), E leaves the graph, and a newcomer N arrives alone
    con.execute("INSERT INTO oc_firm VALUES ('B', 'FIRM1')")
    con.execute("DELETE FROM oc_org WHERE pac_id = 'E'; DELETE FROM oc_key WHERE pac_id = 'E'")
    con.execute("INSERT INTO oc_org VALUES ('N', 'ORG N', 'MA'); INSERT INTO oc_size VALUES ('N', 1.0, 0, 0)")
    oc.evidence(con)
    _calib(con, {"ao": 0.9, "mail": 0.1}, replace=True)
    oc.units(con); oc.cluster(con); oc.label(con, "2020-04-01")
    rows = sorted(con.execute("SELECT event, from_community_id, to_community_id, n_organizations FROM owner_community_event").fetchall())
    assert rows == [("entry", None, "ON", 1), ("exit", "OE", None, 1), ("move", "OA", "OF", 1)]
    # and when a whole community's id ends, its members' flow is an absorption
    con.execute("INSERT INTO oc_firm VALUES ('A', 'FIRM1'), ('C', 'FIRM1'), ('D', 'FIRM1')")
    oc.units(con); oc.cluster(con); oc.label(con, "2020-07-01")
    assert con.execute("SELECT event, from_community_id, to_community_id, n_organizations FROM owner_community_event").fetchall() \
        == [("absorbed", "OF", "OA", 3)]      # a tie of three and three: the smaller id, OA, holds
