"""The shared reference tables in reference/ (rebuilt by reference/build.py --check, which needs the upstream files)."""
import csv
import re
from pathlib import Path

REFERENCE = Path(__file__).resolve().parent.parent / "reference"


def rows(name):
    with open(REFERENCE / name, newline="") as f:
        return list(csv.DictReader(f))


def test_state_fips_is_the_50_states_and_dc():
    rs = rows("state_fips.csv")
    assert len(rs) == 51 and len({r["state"] for r in rs}) == 51 and "DC" in {r["state"] for r in rs}
    assert len({r["fips"] for r in rs}) == 51 and all(re.fullmatch(r"\d{2}", r["fips"]) for r in rs)
    assert all(r["name"] for r in rs)


def test_zcta_county_is_one_county_per_zcta():
    rs = rows("zcta_county.csv")
    assert len(rs) == 33_120 and len({r["zcta"] for r in rs}) == len(rs)
    assert all(re.fullmatch(r"\d{5}", r["zcta"]) and re.fullmatch(r"\d{5}", r["county_fips"]) for r in rs)
    # 2010 geography: Shannon County SD, not its 2015 name Oglala Lakota
    counties = {r["county_fips"] for r in rs}
    assert "46113" in counties and "46102" not in counties


def test_county_cbsa_has_the_382_msas_of_2016():
    rs = rows("county_cbsa_2015.csv")
    assert len({r["county_fips"] for r in rs}) == len(rs)
    assert {r["cbsa_type"] for r in rs} == {"metro", "micro"}
    assert len({r["cbsa"] for r in rs if r["cbsa_type"] == "metro" and not r["county_fips"].startswith("72")}) == 382
    # every CBSA county is reachable from some ZCTA except three small Virginia cities (Fairfax, Manassas Park,
    # Martinsville) that hold no ZCTA's largest population
    assert {r["county_fips"] for r in rs} - {r["county_fips"] for r in rows("zcta_county.csv")} == {"51600", "51685", "51690"}


def test_zip_hrr():
    rs = rows("zip_hrr_2019.csv")
    assert len(rs) == 40_866 and len({r["hrr"] for r in rs}) == 306
    assert all(re.fullmatch(r"\d{5}", r["zip5"]) for r in rs)


def test_border_pairs_cross_states_in_both_directions():
    rs = rows("state_border_county_pairs.csv")
    pairs = {(r["county_fips"], r["neighbor_fips"]) for r in rs}
    assert len(pairs) == len(rs) == 2_616
    assert all(a[:2] != b[:2] for a, b in pairs) and all((b, a) in pairs for a, b in pairs)
    # every border county has a ZCTA under the 2010 map
    assert {a for a, _ in pairs} - {r["county_fips"] for r in rows("zcta_county.csv")} == set()
