"""A provider graph built from the warehouse: practitioners, the organizations they bill through, and hospitals.

The graph is derived, not loaded: every edge and node comes from tables the warehouse already holds (the
Medicare PPEF enrollment and reassignment streams, the NPPES provider stream and the POS hospital file), and
``graph-build`` can rebuild it from scratch at any time. docs/METHODOLOGY.md section 8 has the rules and
docs/designs/provider-graph.md the literature they follow.

Nodes

``practitioner``
    an individual Medicare enrollee, keyed by PECOS associate id (PAC id, one per person), with the NPI,
    the Medicare specialties of its enrollments, and the NPPES primary taxonomy and practice ZIP of that
    NPI on the quarter's date.
``organization``
    an organization Medicare enrollee, keyed by PAC id. PECOS gives an organization one PAC id per legal
    entity (one tax id), however many enrollments (states, NPIs, provider types) it holds, so this is the
    public stand-in for the tax id the literature groups practices by. ``hospital`` is true when the entity
    holds a hospital, critical access hospital or rural emergency hospital enrollment, or a Part B
    hospital department enrollment.
``hospital``
    a POS hospital (category 01, active), keyed by CCN, with its location, subtype, beds and control type.
``facility``
    an active POS nursing home (skilled nursing and nursing facilities) or home health agency, keyed by CCN,
    from the QIES file and, once CMS moved the type there, the iQIES file (home health from December 2023,
    nursing homes from September 2025).

Edges

``reassignment``
    practitioner -> organization: the practitioner reassigns Medicare benefits to the organization, through
    any pair of their enrollments. This is a billing relationship, not employment or ownership.
``hospital_link``
    organization enrollment -> hospital CCN: a hospital-type enrollment's NPI matched to a POS hospital in
    the same state by NPPES phone and street number (rule 1), street number within the ZIP (rule 2) or
    phone alone (rule 3). Only active POS hospitals are candidates; an enrollment whose best rule ties
    between two CCNs is not linked.
``facility_link``
    organization enrollment -> facility CCN: a skilled nursing facility or home health agency enrollment
    matched by the same three rules to an active POS facility of the same type.

With ``--compendium``, two annual tables from the AHRQ Compendium of U.S. Health Systems join them
(nppes_over_time.compendium):

``system``
    a Compendium health system in a Compendium year (2016, 2018, 2020-2023).
``system_link``
    organization (PAC id) or hospital, nursing home or home health agency (CCN) -> its system that year, or
    NULL when AHRQ lists it outside any system. Organizations come from AHRQ's group practice files, which
    also give the physicians MD-PPAS puts in each tax id by plurality of Medicare billing.

These are keyed by ``year``, not by quarter: the Compendium describes a calendar year.

The quarterly tables are interval tables on the PPEF quarter dates, with the version semantics of the rest of the
warehouse: a row holds on date ``d`` when ``effective_date <= d`` and ``end_date`` is NULL or later than
``d``. A row starts on the first quarter it is observed with exactly these values and ends on the first
quarter it is not, so a value that changes, or a relationship that lapses and returns, makes a new row.
"""

from __future__ import annotations

import csv
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import duckdb

from . import __version__

log = logging.getLogger(__name__)

# PPEF provider types that make an organization a hospital entity (the "hospital tax id" of the literature)
HOSPITAL_TYPES = ("00-09", "00-85", "00-24", "12-Z1")
# the subset that is certified as a facility and therefore has a CCN in the POS hospital file
FACILITY_TYPES = ("00-09", "00-85", "00-24")
# PPEF provider types linked to POS nursing homes and home health agencies
NURSING_HOME_TYPES = ("00-18",)
HOME_HEALTH_TYPES = ("00-06",)
# POS codes of those facilities: QIES category (02 SNF/NF, 03 distinct part, 04 SNF, 10 NF; 05 HHA) and the iQIES
# type and subtype that replaced them (20 with subtypes 16-19; 3)
POS_NURSING_HOME = ("02", "03", "04", "10")
IQIES_NURSING_HOME = ("20",)
POS_HOME_HEALTH = ("05",)
IQIES_HOME_HEALTH = ("3",)


@dataclass(frozen=True)
class Table:
    name: str
    key: tuple[str, ...]
    attrs: tuple[str, ...]
    description: str


TABLES = (
    Table("practitioner", ("pac_id",),
          ("npi", "provider_type_codes", "enrollment_states", "taxonomy_code", "practice_zip5", "practice_state"),
          "individual Medicare enrollees (PAC id) with Medicare specialties and the NPPES taxonomy and ZIP of their NPI"),
    Table("organization", ("pac_id",),
          ("npis", "provider_type_codes", "enrollment_states", "n_enrollments", "hospital", "org_name",
           "practice_zip5", "practice_state"),
          "organization Medicare enrollees (PAC id, one per legal entity); name and ZIP are NPPES's for the lowest NPI"),
    Table("hospital", ("ccn",),
          ("fac_name", "subtype", "state", "zip5", "county_fips", "beds", "control_type"),
          "active POS hospitals (category 01)"),
    Table("reassignment", ("practitioner_pac_id", "organization_pac_id"), (),
          "practitioner reassigns Medicare benefits to organization"),
    Table("hospital_link", ("enrollment_id",), ("organization_pac_id", "npi", "ccn", "match_rule"),
          "hospital-type enrollment matched to its POS CCN"),
    Table("facility", ("ccn",),
          ("facility_type", "fac_name", "subtype", "state", "zip5", "county_fips", "beds", "control_type"),
          "active POS nursing homes and home health agencies (QIES, then iQIES)"),
    Table("facility_link", ("enrollment_id",), ("organization_pac_id", "npi", "ccn", "facility_type", "match_rule"),
          "skilled nursing facility or home health enrollment matched to its POS CCN"),
)


def _q(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def _link_sql(enrollment_types: str, candidates: str, same_type: str = "") -> str:
    """Match enrollments of the given provider types to candidate facilities by NPPES phone and street number.

    Rule 1: same ten-digit phone and street number; rule 2: same street number and ZIP; rule 3: same phone; all
    within the state. The best rule wins and an enrollment whose best rule hits two or more CCNs is not linked.
    """
    return f"""
            WITH h AS (
                SELECT e.enrollment_id, e.pac_id, e.npi, e.provider_type_code, n.zip5, n.practice_state AS state,
                       n.phone, n.street_number
                FROM enr e JOIN nppes n ON n.npi = e.npi
                WHERE e.provider_type_code IN ({enrollment_types})),
            cand AS (
                SELECT h.enrollment_id, h.pac_id, h.npi, f.*,
                       CASE WHEN length(h.phone) = 10 AND h.phone = f.phone AND h.street_number = f.street_number THEN 1
                            WHEN h.street_number = f.street_number AND h.zip5 = f.zip5 THEN 2
                            ELSE 3 END AS match_rule
                FROM h JOIN {candidates} f ON f.state = h.state {same_type}
                 AND ((length(h.phone) = 10 AND h.phone = f.phone)
                      OR (h.street_number = f.street_number AND h.zip5 = f.zip5))),
            best AS (
                SELECT *, count(*) OVER (PARTITION BY enrollment_id) AS n_best
                FROM cand QUALIFY match_rule = min(match_rule) OVER (PARTITION BY enrollment_id))"""


def _quarter_sql(d: str, iqies: bool = True) -> dict[str, str]:
    """The SELECT that gives each table's rows on date d (an ISO date literal)."""
    def asof(alias: str = "") -> str:
        a = f"{alias}." if alias else ""
        return f"{a}effective_date <= DATE '{d}' AND ({a}end_date IS NULL OR {a}end_date > DATE '{d}')"
    hospital_types = ", ".join(f"'{t}'" for t in HOSPITAL_TYPES)
    facility_types = ", ".join(f"'{t}'" for t in FACILITY_TYPES)
    return {
        "enr": f"""
            SELECT enrollment_id, npi, pac_id, provider_type_code, state
            FROM wh.medicare_enrollment_version WHERE {asof()}""",
        "nppes": f"""
            SELECT npi, org_name, taxonomy_code_1 AS taxonomy_code,
                   left(practice_postal_code, 5) AS zip5, practice_state,
                   nullif(regexp_replace(practice_phone, '[^0-9]', '', 'g'), '') AS phone,
                   nullif(regexp_extract(practice_address_1, '^\\s*(\\d+)', 1), '') AS street_number
            FROM wh.provider_version
            WHERE {asof()} AND npi IN (SELECT npi FROM enr)""",
        "practitioner": """
            WITH i AS (SELECT * FROM enr WHERE enrollment_id LIKE 'I%' AND pac_id IS NOT NULL),
            first_npi AS (SELECT pac_id, min(npi) AS npi FROM i GROUP BY pac_id)
            SELECT i.pac_id, any_value(f.npi) AS npi,
                   list_sort(list(DISTINCT i.provider_type_code)) AS provider_type_codes,
                   list_sort(list(DISTINCT i.state)) AS enrollment_states,
                   any_value(n.taxonomy_code) AS taxonomy_code, any_value(n.zip5) AS practice_zip5,
                   any_value(n.practice_state) AS practice_state
            FROM i JOIN first_npi f USING (pac_id) LEFT JOIN nppes n ON n.npi = f.npi
            GROUP BY i.pac_id""",
        "organization": f"""
            WITH o AS (SELECT * FROM enr WHERE enrollment_id LIKE 'O%' AND pac_id IS NOT NULL),
            first_npi AS (SELECT pac_id, min(npi) AS npi FROM o GROUP BY pac_id)
            SELECT o.pac_id, list_sort(list(DISTINCT o.npi)) AS npis,
                   list_sort(list(DISTINCT o.provider_type_code)) AS provider_type_codes,
                   list_sort(list(DISTINCT o.state)) AS enrollment_states,
                   count(*) AS n_enrollments,
                   bool_or(o.provider_type_code IN ({hospital_types})) AS hospital,
                   any_value(n.org_name) AS org_name, any_value(n.zip5) AS practice_zip5,
                   any_value(n.practice_state) AS practice_state
            FROM o JOIN first_npi f USING (pac_id) LEFT JOIN nppes n ON n.npi = f.npi
            GROUP BY o.pac_id""",
        "hospital": f"""
            SELECT ccn, fac_name, prvdr_ctgry_sbtyp_cd AS subtype, state_cd AS state, left(zip_cd, 5) AS zip5,
                   fips_state_cd || fips_cnty_cd AS county_fips, crtfd_bed_cnt AS beds,
                   gnrl_cntl_type_cd AS control_type,
                   nullif(regexp_replace(phne_num, '[^0-9]', '', 'g'), '') AS phone,
                   nullif(regexp_extract(st_adr, '^\\s*(\\d+)', 1), '') AS street_number
            FROM wh.pos_facility_version
            WHERE {asof()} AND prvdr_ctgry_cd = '01' AND pgm_trmntn_cd = '00'""",
        "reassignment": f"""
            SELECT DISTINCT i.pac_id AS practitioner_pac_id, o.pac_id AS organization_pac_id
            FROM wh.medicare_reassignment_version r
            JOIN enr i ON i.enrollment_id = r.enrollment_id
            JOIN enr o ON o.enrollment_id = r.receiving_enrollment_id
            WHERE {asof('r')}
              AND o.enrollment_id LIKE 'O%' AND i.pac_id IS NOT NULL AND o.pac_id IS NOT NULL""",
        "hospital_link": _link_sql(facility_types, "hospital") + """
            SELECT enrollment_id, pac_id AS organization_pac_id, npi, ccn, match_rule
            FROM best WHERE n_best = 1""",
        "facility": f"""
            SELECT ccn, CASE WHEN prvdr_ctgry_cd = '05' THEN 'home_health' ELSE 'nursing_home' END AS facility_type,
                   fac_name, prvdr_ctgry_cd || '-' || coalesce(prvdr_ctgry_sbtyp_cd, '') AS subtype,
                   state_cd AS state, left(zip_cd, 5) AS zip5, fips_state_cd || fips_cnty_cd AS county_fips,
                   crtfd_bed_cnt AS beds, gnrl_cntl_type_cd AS control_type,
                   nullif(regexp_replace(phne_num, '[^0-9]', '', 'g'), '') AS phone,
                   nullif(regexp_extract(st_adr, '^\\s*(\\d+)', 1), '') AS street_number
            FROM wh.pos_facility_version
            WHERE {asof()} AND prvdr_ctgry_cd IN ({_q(POS_NURSING_HOME + POS_HOME_HEALTH)}) AND pgm_trmntn_cd = '00'
            """ + (f"""
            UNION ALL BY NAME
            SELECT ccn, CASE WHEN prvdr_type_id IN ({_q(IQIES_HOME_HEALTH)}) THEN 'home_health' ELSE 'nursing_home' END
                       AS facility_type,
                   fac_name, 'iqies ' || prvdr_type_id || '-' || coalesce(prvdr_sbtyp_id, '') AS subtype,
                   state_cd AS state, left(zip_cd, 5) AS zip5, fips_state_cd || fips_cnty_cd AS county_fips,
                   crtfd_bed_cnt AS beds, gnrl_cntl_type_cd AS control_type,
                   nullif(regexp_replace(phne_num, '[^0-9]', '', 'g'), '') AS phone,
                   nullif(regexp_extract(st_adr, '^\\s*(\\d+)', 1), '') AS street_number
            FROM wh.pos_facility_iqies_version
            -- the first iQIES file (2023-Q4) leaves the termination code blank for active agencies
            WHERE {asof()} AND prvdr_type_id IN ({_q(IQIES_NURSING_HOME + IQIES_HOME_HEALTH)})
              AND coalesce(pgm_trmntn_cd, '00') = '00'
            """ if iqies else ""),
        "facility_link": _link_sql(
            _q(NURSING_HOME_TYPES + HOME_HEALTH_TYPES), "facility",
            f"AND f.facility_type = CASE WHEN h.provider_type_code IN ({_q(HOME_HEALTH_TYPES)}) "
            f"THEN 'home_health' ELSE 'nursing_home' END") + """
            SELECT enrollment_id, pac_id AS organization_pac_id, npi, ccn, facility_type, match_rule
            FROM best WHERE n_best = 1""",
    }


def _hash_expr(cols: tuple[str, ...]) -> str:
    if not cols:
        return "''"
    return "md5(CAST(struct_pack(" + ", ".join(f"{c} := {c}" for c in cols) + ") AS VARCHAR))"


class GraphBuilder:
    """Builds the interval tables one PPEF quarter at a time, in an in-memory DuckDB attached to the warehouse."""

    def __init__(self, db: Path, *, threads: int | None = None, memory_limit: str | None = None,
                 work_dir: Path | None = None, compendium: Path | None = None):
        self.con = duckdb.connect()
        self.work_dir = work_dir
        self.compendium = compendium
        if threads:
            self.con.execute(f"SET threads = {int(threads)}")
        if memory_limit:
            self.con.execute(f"SET memory_limit = '{memory_limit}'")
        if work_dir:
            work_dir.mkdir(parents=True, exist_ok=True)
            self.con.execute(f"SET temp_directory = '{work_dir}'")
        self.con.execute(f"ATTACH '{db}' AS wh (READ_ONLY)")
        self.stats: list[dict] = []

    def quarters(self) -> list[tuple[int, str]]:
        return [(s, d.isoformat()) for s, d in
                self.con.execute("SELECT snapshot_id, as_of_date FROM wh.ppef_snapshot ORDER BY as_of_date").fetchall()]

    def _require(self) -> None:
        have = {r[0] for r in self.con.execute(
            "SELECT table_name FROM duckdb_tables() WHERE database_name = 'wh'").fetchall()}
        need = {"ppef_snapshot", "medicare_enrollment_version", "medicare_reassignment_version",
                "provider_version", "pos_facility_version"}
        missing = sorted(need - have)
        # CMS moved nursing homes and home health agencies to the iQIES POS file; a warehouse without it has them
        # only until they moved
        self.iqies = "pos_facility_iqies_version" in have
        if missing:
            from .errors import UserError
            raise UserError(f"the warehouse lacks {', '.join(missing)}; the graph needs the NPPES, PPEF and POS tables "
                            f"(load them with `load`, `ppef-load` and `pos-load`)", exit_code=2)

    def build(self, limit: int | None = None) -> list[dict]:
        self._require()
        if self.compendium:
            from .compendium import require_files
            require_files(self.compendium)
        for t in TABLES:
            self.con.execute(f"DROP TABLE IF EXISTS {t.name}_open; DROP TABLE IF EXISTS {t.name}_closed")
        for snapshot_id, d in self.quarters()[:limit]:
            started = time.time()
            self._quarter(snapshot_id, d)
            log.info("graph quarter %s: %s (%.0fs)", d,
                     ", ".join(f"{k} {v:,}" for k, v in self.stats[-1].items() if k.startswith("n_")),
                     time.time() - started)
        return self.stats

    def _quarter(self, snapshot_id: int, d: str) -> None:
        sql = _quarter_sql(d, iqies=self.iqies)
        con = self.con
        con.execute(f"CREATE OR REPLACE TEMP TABLE enr AS {sql['enr']}")
        con.execute(f"CREATE OR REPLACE TEMP TABLE nppes AS {sql['nppes']}")
        stat: dict = {"snapshot_id": snapshot_id, "as_of_date": d}
        for t in TABLES:
            con.execute(f"CREATE OR REPLACE TEMP TABLE {t.name} AS {sql[t.name]}")
            self._advance(t, d)
            stat[f"n_{t.name}"] = con.execute(f"SELECT count(*) FROM {t.name}").fetchone()[0]
        stat.update(zip(
            ("n_reassignment_rows", "n_reassignment_dropped", "n_hospital_enrollments"),
            con.execute(f"""
                WITH r AS (SELECT enrollment_id, receiving_enrollment_id FROM wh.medicare_reassignment_version
                           WHERE effective_date <= DATE '{d}' AND (end_date IS NULL OR end_date > DATE '{d}'))
                SELECT (SELECT count(*) FROM r),
                       (SELECT count(*) FROM r WHERE r.enrollment_id NOT IN (SELECT enrollment_id FROM enr)
                           OR r.receiving_enrollment_id NOT IN (SELECT enrollment_id FROM enr WHERE enrollment_id LIKE 'O%')),
                       (SELECT count(*) FROM enr WHERE provider_type_code IN ({", ".join(f"'{x}'" for x in FACILITY_TYPES)}))
            """).fetchone()))
        self.stats.append(stat)

    def _advance(self, t: Table, d: str) -> None:
        """Close the open rows that this quarter does not repeat and open the rows that are new."""
        con = self.con
        cols = ", ".join(t.key + t.attrs)
        con.execute(f"CREATE OR REPLACE TEMP TABLE cur AS SELECT {cols}, {_hash_expr(t.attrs)} AS _h FROM {t.name}")
        if not con.execute(f"SELECT count(*) FROM duckdb_tables() WHERE table_name = '{t.name}_open'").fetchone()[0]:
            con.execute(f"CREATE TABLE {t.name}_open AS SELECT *, DATE '{d}' AS effective_date FROM cur")
            con.execute(f"CREATE TABLE {t.name}_closed AS SELECT * EXCLUDE (_h), NULL::DATE AS end_date "
                        f"FROM {t.name}_open LIMIT 0")
            return
        same = " AND ".join([f"o.{k} = c.{k}" for k in t.key] + ["o._h = c._h"])
        con.execute(f"""
            INSERT INTO {t.name}_closed
            SELECT o.* EXCLUDE (_h), DATE '{d}' AS end_date FROM {t.name}_open o
            WHERE NOT EXISTS (SELECT 1 FROM cur c WHERE {same})""")
        con.execute(f"""
            CREATE OR REPLACE TABLE {t.name}_open AS
            SELECT o.* FROM {t.name}_open o WHERE EXISTS (SELECT 1 FROM cur c WHERE {same})
            UNION ALL BY NAME
            SELECT c.*, DATE '{d}' AS effective_date FROM cur c
            WHERE NOT EXISTS (SELECT 1 FROM {t.name}_open o WHERE {same})""")

    def write(self, out: Path, db: Path) -> dict:
        out.mkdir(parents=True, exist_ok=True)
        tables = {}
        for t in TABLES:
            cols = ", ".join(t.key + t.attrs)
            path = out / f"{t.name}.parquet"
            self.con.execute(f"""
                COPY (SELECT {cols}, effective_date, end_date FROM (
                        SELECT * FROM {t.name}_closed
                        UNION ALL BY NAME SELECT * EXCLUDE (_h), NULL::DATE AS end_date FROM {t.name}_open)
                      ORDER BY {", ".join(t.key)}, effective_date)
                TO '{path}' (FORMAT parquet, COMPRESSION zstd)""")
            n = self.con.execute(f"SELECT count(*) FROM '{path}'").fetchone()[0]
            tables[t.name] = {"rows": n, "key": list(t.key), "description": t.description}
        compendium_years = None
        if self.compendium:
            import tempfile

            from .compendium import system_tables
            with tempfile.TemporaryDirectory(dir=self.work_dir) as tmp:
                compendium_years = system_tables(self.con, self.compendium, Path(tmp))
            for name, key, description in (
                    ("system", ["year", "health_sys_id"], "AHRQ Compendium health systems, by Compendium year"),
                    ("system_link", ["year", "node_type", "node_id", "compendium_id"],
                     "organization (PAC id) or facility (CCN) -> Compendium system, by Compendium year")):
                path = out / f"{name}.parquet"
                self.con.execute(f"COPY {name} TO '{path}' (FORMAT parquet, COMPRESSION zstd)")
                n = self.con.execute(f"SELECT count(*) FROM '{path}'").fetchone()[0]
                tables[name] = {"rows": n, "key": key, "description": description, "annual": True}
        with open(out / "quarters.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(self.stats[0]))
            w.writeheader()
            w.writerows(self.stats)
        manifest = {
            "generator": f"nppes-over-time {__version__} graph-build",
            "warehouse": db.name,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "first_as_of_date": self.stats[0]["as_of_date"],
            "last_as_of_date": self.stats[-1]["as_of_date"],
            "quarters": len(self.stats),
            "hospital_types": list(HOSPITAL_TYPES),
            "facility_types": list(FACILITY_TYPES),
            "tables": tables,
        }
        if compendium_years:
            manifest["compendium"] = {"source": "AHRQ Compendium of U.S. Health Systems", "years": compendium_years}
        (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        return manifest

    def close(self) -> None:
        self.con.close()


def build_graph(db: Path, out: Path, *, limit: int | None = None, compendium: Path | None = None, **kw) -> dict:
    g = GraphBuilder(db, compendium=compendium, **kw)
    try:
        g.build(limit)
        return g.write(out, db)
    finally:
        g.close()
