"""The reference CSVs under notebooks/data/ and the helper module must stay consistent."""
import csv
import importlib.util
import re
import sys
from pathlib import Path

import pytest

NB = Path(__file__).resolve().parent.parent / "notebooks"
DATA = NB / "data"


def _rows(name):
    with open(DATA / name, newline="") as f:
        return list(csv.DictReader(f))


@pytest.fixture(scope="module")
def nb():
    spec = importlib.util.spec_from_file_location("nppes_nb", NB / "nppes_nb.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["nppes_nb"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_fpa_dates_cover_every_state_once(nb):
    rows = _rows("np_fpa_dates.csv")
    states = [r["state"] for r in rows]
    assert sorted(states) == sorted(nb.STATES)
    for r in rows:
        assert r["coding"] in {"always", "statute", "never"}, r
        assert r["source"], f"{r['state']} has no source"
        if r["coding"] == "never":
            assert r["fpa_effective"] == "", r
        else:
            assert re.fullmatch(r"\d{4}-\d{2}", r["fpa_effective"]), r
        if r["coding"] == "always":
            assert r["fpa_effective"] == "1998-01", r
        assert r["partial"] in {"", "Y"}, r


def test_aanp_list_is_subset_of_states(nb):
    rows = _rows("aanp_full_practice_2026.csv")
    states = [r["state"] for r in rows]
    assert len(states) == len(set(states))
    assert set(states) <= set(nb.STATES)
    assert len(states) == 28  # 27 states and DC in May 2026


def test_population_is_a_balanced_state_year_grid(nb):
    rows = _rows("state_population.csv")
    years = sorted({int(r["year"]) for r in rows})
    assert years == list(range(2010, 2025))
    for st in nb.STATES:
        have = {int(r["year"]) for r in rows if r["state"] == st}
        assert have == set(years), st
    assert all(int(r["population"]) > 500_000 for r in rows)


def test_county_files_use_five_digit_fips():
    for name, cols in (("zcta_county.csv", ["zcta", "county_fips"]), ("state_border_county_pairs.csv", ["county_fips", "neighbor_fips"])):
        rows = _rows(name)
        assert rows
        for r in rows:
            for c in cols:
                assert re.fullmatch(r"\d{5}", r[c]), (name, r)
    zctas = [r["zcta"] for r in _rows("zcta_county.csv")]
    assert len(zctas) == len(set(zctas))
    pairs = _rows("state_border_county_pairs.csv")
    assert all(r["county_fips"][:2] != r["neighbor_fips"][:2] for r in pairs)


def test_helper_as_of_sql(nb):
    sql = nb.as_of("provider", "2019-01-13", "npi")
    assert "effective_date <= DATE '2019-01-13'" in sql
    assert "end_date > DATE '2019-01-13'" in sql


def test_helper_fpa_dates_parse(nb):
    df = nb.fpa_dates()
    assert df.fpa_effective.notna().sum() == (df.coding != "never").sum()
    assert df.aanp_full_practice.sum() == 28
    assert df.loc[df.state == "FL", "partial"].item()


def test_np_events_from_synthetic_versions(nb):
    """Entry, exit, move and sole-proprietor events from a hand-built version history."""
    import pandas as pd

    D = pd.Timestamp
    cols = ["npi", "effective_date", "end_date", "close_reason", "practice_state", "sole_proprietor",
            "deactivation_date", "primary_taxonomy", "is_np"]
    rows = [
        # A: RN in 2012, becomes NP in 2013 (entry), moves MD->PA in 2014, sole prop X->Y in 2015, deactivated via stub in 2016
        ("A", D("2012-01-08"), D("2013-01-13"), "changed", "MD", "X", None, "163W00000X", False),
        ("A", D("2013-01-13"), D("2014-01-12"), "changed", "MD", "X", None, "363L00000X", True),
        ("A", D("2014-01-12"), D("2015-01-11"), "changed", "PA", "X", None, "363L00000X", True),
        ("A", D("2015-01-11"), D("2016-01-10"), "changed", "PA", "Y", None, "363L00000X", True),
        ("A", D("2016-01-10"), None, None, None, None, D("2016-01-02"), None, False),
        # B: NP from the start, switches away to a physician assistant code in 2015 (exit), never deactivated
        ("B", D("2012-01-08"), D("2015-01-11"), "changed", "NY", "N", None, "363L00000X", True),
        ("B", D("2015-01-11"), None, None, "NY", "N", None, "363A00000X", False),
        # C: NP who left the file in 2014
        ("C", D("2012-01-08"), D("2014-01-12"), "absent", "TX", "N", None, "363L00000X", True),
    ]
    v = pd.DataFrame(rows, columns=cols)
    ev = nb.np_events(v)
    def one(npi, event, col):
        hits = ev[(ev.npi == npi) & (ev.event == event)]
        assert len(hits) == 1, (npi, event, hits)
        return hits[col].iloc[0]

    assert one("A", "entry", "date") == D("2013-01-13")
    assert one("A", "entry", "detail") == "switched_taxonomy"
    assert one("A", "move", "from_state") == "MD" and one("A", "move", "state") == "PA"
    assert one("A", "became_sole_proprietor", "date") == D("2015-01-11")
    assert one("A", "exit", "detail") == "deactivated" and one("A", "exit", "state") == "PA"  # state from the version before the stub
    assert one("B", "entry", "detail") == "new_npi"
    assert one("B", "exit", "detail") == "switched_taxonomy" and one("B", "exit", "state") == "NY"
    assert one("C", "exit", "detail") == "absent" and one("C", "exit", "date") == D("2014-01-12")
    assert (ev.event == "exit").sum() == 3 and (ev.event == "entry").sum() == 3
