"""Helpers shared by the notebooks in this directory.

Everything reads the Parquet export of a release (``export/`` by default, or the
directory in ``$NPPES_EXPORT``). Nothing here touches the DuckDB warehouse, so the
notebooks run on a downloaded release.
"""
from __future__ import annotations

import os
from pathlib import Path

import duckdb
import pandas as pd

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"

TABLES = {
    "provider": "parquet/provider/*/*.parquet",
    "provider_taxonomy": "parquet/provider_taxonomy/*/*.parquet",
    "provider_other_identifier": "parquet/provider_other_identifier/*/*.parquet",
    "practice_location": "parquet/practice_location.parquet",
    "other_name": "parquet/other_name.parquet",
    "endpoint": "parquet/endpoint.parquet",
}

STATES = [
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID", "IL", "IN",
    "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH",
    "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT",
    "VT", "VA", "WA", "WV", "WI", "WY",
]

NP_TAXONOMY_PREFIX = "363L"  # Nurse Practitioner (all specialties) in the NUCC taxonomy


def export_dir() -> Path:
    """The export directory: ``$NPPES_EXPORT`` or ``../export`` relative to this file."""
    return Path(os.environ.get("NPPES_EXPORT", HERE.parent / "export")).resolve()


def connect(threads: int | None = None) -> duckdb.DuckDBPyConnection:
    """Open an in-memory DuckDB with one view per export table.

    After this, ``provider``, ``provider_taxonomy`` and the other table names can be
    used directly in SQL. The views read Parquet lazily, so a query that filters on
    ``npi`` or ``effective_year`` touches only the row groups it needs.
    """
    root = export_dir()
    if not (root / "manifest.json").exists():
        raise FileNotFoundError(
            f"{root} has no manifest.json. Point NPPES_EXPORT at a complete export directory."
        )
    con = duckdb.connect()
    if threads:
        con.execute(f"SET threads = {int(threads)}")
    for name, glob in TABLES.items():
        path = root / glob
        if "*" in glob or path.exists():
            con.execute(
                f"CREATE VIEW {name} AS SELECT * FROM read_parquet('{root / glob}', hive_partitioning = true)"
            )
    return con


def snapshots() -> pd.DataFrame:
    """``snapshots.csv`` of the export, one row per monthly file, with ``as_of_date`` parsed."""
    df = pd.read_csv(export_dir() / "snapshots.csv", parse_dates=["as_of_date", "zip_date"])
    return df


def as_of(table: str, date: str, columns: str = "*") -> str:
    """SQL for ``table`` as CMS published it on ``date`` (a snapshot as-of date).

    Interval endpoints are snapshot dates, so between two snapshots this returns the
    earlier one. Use inside a larger query: ``f"SELECT count(*) FROM ({as_of('provider', d)})"``.
    """
    return (
        f"SELECT {columns} FROM {table} "
        f"WHERE effective_date <= DATE '{date}' AND (end_date IS NULL OR end_date > DATE '{date}')"
    )


def monthly_dates(start: str = "2011-04-01", end: str | None = None) -> list[pd.Timestamp]:
    """The as-of date of every monthly snapshot from ``start`` (default: when the panel becomes monthly)."""
    s = snapshots()
    s = s[s.as_of_date >= pd.Timestamp(start)]
    if end:
        s = s[s.as_of_date <= pd.Timestamp(end)]
    return list(s.as_of_date)


def fpa_dates() -> pd.DataFrame:
    """State FPA adoption months (see data/SOURCES.md), with ``fpa_effective`` as a month-start Timestamp."""
    df = pd.read_csv(DATA / "np_fpa_dates.csv", dtype=str)
    df["fpa_effective"] = pd.to_datetime(df.fpa_effective, format="%Y-%m", errors="coerce")
    df["partial"] = df.partial.fillna("") == "Y"
    aanp = pd.read_csv(DATA / "aanp_full_practice_2026.csv", dtype=str)
    df["aanp_full_practice"] = df.state.isin(aanp.state)
    return df


def state_population(through: int | None = None) -> pd.DataFrame:
    """State resident population by year from the Census file, with the last year carried forward.

    ``through`` is the last year to cover; by default the year of the latest snapshot in the export.
    """
    pop = pd.read_csv(DATA / "state_population.csv")
    if through is None:
        through = snapshots().as_of_date.max().year
    last = pop[pop.year == pop.year.max()]
    extra = [last.assign(year=y, source=f"carried forward from {pop.year.max()}") for y in range(pop.year.max() + 1, through + 1)]
    return pd.concat([pop, *extra], ignore_index=True)[["state", "year", "population"]]


def np_versions(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Every version of every individual whose primary taxonomy was ever a nurse practitioner code.

    One row per (npi, effective_date) with the fields the FPA notebook needs. ``is_np`` says
    whether the primary taxonomy of *that* version is an NP code, so a nurse who was an RN
    first appears with ``is_np = False`` until the switch. "Individual" is decided per NPI
    (any version with ``entity_type = '1'``), because a deactivated NPI's final stub version
    has no entity type and that stub is the version that carries ``deactivation_date``.
    """
    sql = f"""
    WITH ever_np AS (
        SELECT DISTINCT t.npi FROM provider_taxonomy t
        JOIN (SELECT DISTINCT npi FROM provider WHERE entity_type = '1') i USING (npi)
        WHERE t.slot = 1 AND t.taxonomy_code LIKE '{NP_TAXONOMY_PREFIX}%'
    )
    SELECT p.npi, p.effective_date, p.end_date, p.close_reason,
           p.practice_state, p.practice_postal_code, p.sole_proprietor, p.sex,
           p.enumeration_date, p.deactivation_date, p.last_update_date,
           t.taxonomy_code AS primary_taxonomy,
           t.taxonomy_code LIKE '{NP_TAXONOMY_PREFIX}%' AS is_np
    FROM provider p
    JOIN ever_np USING (npi)
    LEFT JOIN provider_taxonomy t
      ON t.npi = p.npi AND t.effective_date = p.effective_date AND t.slot = 1
    ORDER BY p.npi, p.effective_date
    """
    df = con.execute(sql).df()
    for c in ("effective_date", "end_date", "enumeration_date", "deactivation_date", "last_update_date"):
        df[c] = pd.to_datetime(df[c])
    return df


def np_events(versions: pd.DataFrame) -> pd.DataFrame:
    """Turn NP versions into dated events: entry, state move (in and out), sole-proprietor switch, exit.

    Returns one row per event with ``npi, date, event, state, from_state, detail``.
    ``date`` is the snapshot that first showed the change. Entry and exit are symmetric:
    an NPI enters when it first appears with an NP primary taxonomy or switches into one,
    and exits when it switches away from one, leaves the file (``absent``) or is deactivated.
    CMS's sole-proprietor code is ``Y``, ``N`` or ``X`` (not answered); a switch is counted
    when the previous version is ``N`` or ``X`` and the new one is ``Y``, or the reverse.
    """
    v = versions.sort_values(["npi", "effective_date"]).copy()
    g = v.groupby("npi", sort=False)
    v["prev_state"] = g.practice_state.shift()
    v["prev_sp"] = g.sole_proprietor.shift()
    v["prev_np"] = g.is_np.shift()
    first = g.cumcount() == 0
    out = []
    # entry: first version as an NP (either first appearance, or the version where taxonomy switched)
    became = v[v.is_np & (first | (v.prev_np == False))]  # noqa: E712
    out.append(pd.DataFrame({"npi": became.npi, "date": became.effective_date,
                             "event": "entry", "state": became.practice_state, "from_state": None,
                             "detail": ["new_npi" if f else "switched_taxonomy" for f in first[became.index]]}))
    moves = v[v.is_np & v.prev_state.notna() & v.practice_state.notna() & (v.prev_state != v.practice_state)]
    out.append(pd.DataFrame({"npi": moves.npi, "date": moves.effective_date, "event": "move",
                             "state": moves.practice_state, "from_state": moves.prev_state, "detail": None}))
    sp_on = v[v.is_np & v.prev_sp.isin(["N", "X"]) & (v.sole_proprietor == "Y")]
    out.append(pd.DataFrame({"npi": sp_on.npi, "date": sp_on.effective_date, "event": "became_sole_proprietor",
                             "state": sp_on.practice_state, "from_state": None, "detail": sp_on.prev_sp}))
    sp_off = v[v.is_np & (v.prev_sp == "Y") & v.sole_proprietor.isin(["N", "X"])]
    out.append(pd.DataFrame({"npi": sp_off.npi, "date": sp_off.effective_date, "event": "left_sole_proprietor",
                             "state": sp_off.practice_state, "from_state": None, "detail": sp_off.sole_proprietor}))
    # exit, three ways. The state is the one the NP practiced in before leaving.
    switched_away = v[(v.prev_np == True) & ~v.is_np & v.deactivation_date.isna()]  # noqa: E712
    out.append(pd.DataFrame({"npi": switched_away.npi, "date": switched_away.effective_date, "event": "exit",
                             "state": switched_away.prev_state, "from_state": None, "detail": "switched_taxonomy"}))
    absent = v[v.is_np & (v.close_reason == "absent")]
    out.append(pd.DataFrame({"npi": absent.npi, "date": absent.end_date, "event": "exit",
                             "state": absent.practice_state, "from_state": None, "detail": "absent"}))
    # a deactivation usually arrives as a stub version with no state or taxonomy, so look at the version before it
    deact = v[v.deactivation_date.notna() & g.deactivation_date.shift().isna() & (v.is_np | (v.prev_np == True))]  # noqa: E712
    out.append(pd.DataFrame({"npi": deact.npi, "date": deact.effective_date, "event": "exit",
                             "state": deact.practice_state.fillna(deact.prev_state), "from_state": None, "detail": "deactivated"}))
    ev = pd.concat(out, ignore_index=True)
    ev["date"] = pd.to_datetime(ev.date)
    return ev.sort_values(["date", "npi"]).reset_index(drop=True)


def live_on(versions: pd.DataFrame, date) -> pd.DataFrame:
    """The versions valid on ``date``: ``effective_date <= date < end_date`` (NULL end = still current)."""
    d = pd.Timestamp(date).to_datetime64()
    return versions[(versions.effective_date.values <= d) & (versions.end_date.fillna(pd.Timestamp("2999-12-31")).values > d)]


def np_stock_by_month(versions: pd.DataFrame, dates: list[pd.Timestamp], key: str = "practice_state") -> pd.DataFrame:
    """NP versions in force on each snapshot date, counted by ``key``, with the sole-proprietor count.

    Long frame with ``as_of, <key>, nps, sole_proprietors``. Rows whose key is NULL are dropped.
    """
    v = versions[versions.is_np & versions[key].notna()]
    rows = []
    for d in dates:
        agg = live_on(v, d).groupby(key).agg(nps=("npi", "size"),
                                            sole_proprietors=("sole_proprietor", lambda s: (s == "Y").sum()))
        agg["as_of"] = pd.Timestamp(d)
        rows.append(agg.reset_index())
    return pd.concat(rows, ignore_index=True)


def np_stock_by_state_month(versions: pd.DataFrame, dates: list[pd.Timestamp]) -> pd.DataFrame:
    """NPs by practice state (US states and DC only) on each snapshot date: ``as_of, state, nps, sole_proprietors``."""
    v = versions[versions.practice_state.isin(STATES)]
    return np_stock_by_month(v, dates, "practice_state").rename(columns={"practice_state": "state"})
