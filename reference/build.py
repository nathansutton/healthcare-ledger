"""Rebuild the shared reference tables in reference/ from their pinned upstream files.

    uv run --group research python reference/build.py            # download what is missing, write the CSVs
    uv run --group research python reference/build.py --check    # rebuild in memory, compare byte for byte

Each upstream file is pinned by URL and sha256 (UPSTREAM below, and reference/SOURCES.md). Downloads go to
--cache (default data/reference/, outside git) and are refused when the hash differs. Census and Dartmouth
servers throttle scripts, so files are fetched one at a time, with a pause between them.
"""
import argparse
import csv
import hashlib
import io
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

UPSTREAM = {
    "zcta_county_rel_10.txt": (
        "https://www2.census.gov/geo/docs/maps-data/data/rel/zcta_county_rel_10.txt",
        "ea4798cfffbed5eaba990842ca0b1984e97d16ed7d3e0b4ef3e2d9216d5b8980"),
    "list1.xls": (
        "https://www2.census.gov/programs-surveys/metro-micro/geographies/reference-files/2015/delineation-files/list1.xls",
        "3f04531db0b1963c8681ad905bedd2efe2aa01084b9d90cb601f68a0b6d43323"),
    "ZipHsaHrr19.csv.zip": (
        "https://data.dartmouthatlas.org/downloads/geography/ZipHsaHrr19.csv.zip",
        "b389ea8f395a4de047b67d930a86f3219f6b717a228fa5ca087dddd31f0f635b"),
    "state.txt": (
        "https://www2.census.gov/geo/docs/reference/state.txt",
        "bea4e03f71a1fa0045ae732aabad11fa541e5932b071c2369bb0d325e8cba5a0"),
    "county_adjacency.txt": (
        "https://www2.census.gov/geo/docs/reference/county_adjacency.txt",
        "b6df774db9ad5b94c47036379e207dd4622e46ef22c01ca2b7603453a4901014"),
}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(cache: Path, name: str) -> Path:
    """The cached upstream file, downloaded first if missing; its sha256 must match the pin."""
    url, want = UPSTREAM[name]
    path = cache / name
    if not path.exists():
        cache.mkdir(parents=True, exist_ok=True)
        part = path.with_suffix(path.suffix + ".part")
        print(f"downloading {url}", file=sys.stderr)
        req = urllib.request.Request(url, headers={"User-Agent": "healthcare-ledger reference/build.py"})
        with urllib.request.urlopen(req, timeout=300) as r, open(part, "wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
        part.replace(path)
        time.sleep(5)
    got = sha256(path)
    if got != want:
        raise SystemExit(f"{path}: sha256 {got}, expected {want} (pinned in reference/build.py)")
    return path


def to_csv(header: list[str], rows) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue().encode("utf-8")


def zcta_county(cache: Path) -> bytes:
    """2010 ZCTA to the county holding most of its 2010 population; ties (only ZCTAs with no population) by total area."""
    best: dict[str, tuple[int, int, str]] = {}
    with open(fetch(cache, "zcta_county_rel_10.txt"), newline="", encoding="ascii") as f:
        for r in csv.DictReader(f):
            key = (int(r["POPPT"]), int(r["AREAPT"]), r["GEOID"])
            if r["ZCTA5"] not in best or key[:2] > best[r["ZCTA5"]][:2]:
                best[r["ZCTA5"]] = key
    return to_csv(["zcta", "county_fips"], ((z, best[z][2]) for z in sorted(best)))


def county_cbsa(cache: Path) -> bytes:
    """Each county in a core-based statistical area of the July 2015 delineation (list 1)."""
    import xlrd
    sheet = xlrd.open_workbook(fetch(cache, "list1.xls")).sheet_by_index(0)
    header = sheet.row_values(2)
    assert header[:4] == ["CBSA Code", "Metropolitan Division Code", "CSA Code", "CBSA Title"], header
    col = {h: i for i, h in enumerate(header)}
    kind = {"Metropolitan Statistical Area": "metro", "Micropolitan Statistical Area": "micro"}
    out = {}
    for i in range(3, sheet.nrows):
        v = sheet.row_values(i)
        if not v[col["FIPS County Code"]]:
            continue  # the blank row and notes under the table
        county = v[col["FIPS State Code"]] + v[col["FIPS County Code"]]
        row = (county, v[col["CBSA Code"]], v[col["CBSA Title"]], kind[v[col["Metropolitan/Micropolitan Statistical Area"]]])
        assert out.setdefault(county, row) == row, county  # a county in two metropolitan divisions repeats its CBSA
    return to_csv(["county_fips", "cbsa", "cbsa_title", "cbsa_type"], (out[c] for c in sorted(out)))


def zip_hrr(cache: Path) -> bytes:
    """Dartmouth's 2019 ZIP code to hospital referral region; the HSA columns are dropped."""
    with zipfile.ZipFile(fetch(cache, "ZipHsaHrr19.csv.zip")) as z:
        text = z.read("ZipHsaHrr19.csv").decode("ascii")
    rows = [(r["zipcode19"], r["hrrnum"], r["hrrcity"], r["hrrstate"]) for r in csv.DictReader(io.StringIO(text))]
    return to_csv(["zip5", "hrr", "hrr_city", "hrr_state"], rows)


def state_fips(cache: Path) -> bytes:
    """The 50 states and DC: postal code, name, two-digit FIPS code (territories dropped)."""
    with open(fetch(cache, "state.txt"), newline="", encoding="utf-8") as f:
        rows = [(r["STUSAB"], r["STATE_NAME"], r["STATE"]) for r in csv.DictReader(f, delimiter="|") if int(r["STATE"]) <= 56]
    assert len(rows) == 51, len(rows)
    return to_csv(["state", "name", "fips"], sorted(rows))


def state_border_county_pairs(cache: Path) -> bytes:
    """Pairs of adjacent counties in different states, both directions, from the 2010 county adjacency file."""
    pairs = set()
    county = None
    with open(fetch(cache, "county_adjacency.txt"), newline="", encoding="latin-1") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if parts[1]:
                county = parts[1]
            neighbor = parts[3]
            if county[:2] != neighbor[:2]:
                pairs.add((county, neighbor))
    return to_csv(["county_fips", "neighbor_fips"], sorted(pairs))


TABLES = {
    "zcta_county.csv": zcta_county,
    "county_cbsa_2015.csv": county_cbsa,
    "zip_hrr_2019.csv": zip_hrr,
    "state_fips.csv": state_fips,
    "state_border_county_pairs.csv": state_border_county_pairs,
}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--cache", type=Path, default=ROOT / "data" / "reference", help="where upstream files are kept")
    p.add_argument("--check", action="store_true", help="compare the rebuilt tables with the committed files")
    args = p.parse_args(argv)
    bad = 0
    for name, build in TABLES.items():
        data = build(args.cache)
        target = HERE / name
        if args.check:
            same = target.exists() and target.read_bytes() == data
            bad += not same
            print(f"{'ok' if same else 'DIFFERS'}  {name}  ({data.count(b'\n') - 1} rows)")
        else:
            target.write_bytes(data)
            print(f"wrote {name} ({data.count(b'\n') - 1} rows)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
