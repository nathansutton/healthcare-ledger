"""A provider graph built from the warehouse: practitioners, the organizations they bill through, and hospitals.

The graph is derived, not loaded: every edge and node comes from tables the warehouse already holds (the
published tables ``medicare_enrollment``, ``medicare_reassignment``, ``provider`` and ``facility``), and
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
``post_acute_facility``
    an active POS nursing home (skilled nursing and nursing facilities) or home health agency, keyed by CCN
    (``facility_type`` 'nursing_home' or 'home_health' in ``facility``).

Edges

``reassignment``
    practitioner -> organization: the practitioner reassigns Medicare benefits to the organization, through
    any pair of their enrollments. This is a billing relationship, not employment or ownership.
``hospital_link``
    organization enrollment -> hospital CCN: a hospital-type enrollment's NPI matched to a POS hospital in
    the same state by NPPES phone and street number (rule 1), street number within the ZIP (rule 2) or
    phone alone (rule 3). Only active POS hospitals are candidates; an enrollment whose best rule ties
    between two CCNs is not linked.
``post_acute_link``
    organization enrollment -> post-acute facility CCN: a skilled nursing facility or home health agency
    enrollment matched by the same three rules to an active POS facility of the same type.

With ``--compendium``, two annual tables derived from the AHRQ Compendium of U.S. Health Systems source
(``load --source-name ahrq``; derived/compendium.py) join them:

``system``
    a Compendium health system in a Compendium year (2016, 2018, 2020-2023).
``system_link``
    organization (PAC id) or hospital, nursing home or home health agency (CCN) -> its system that year, or
    NULL when AHRQ lists it outside any system. Organizations come from AHRQ's group practice files, which
    also give the physicians MD-PPAS puts in each tax id by plurality of Medicare billing.

These are keyed by ``year``, not by quarter: the Compendium describes a calendar year.

``physician_quarter``, ``physician_edge``, ``group_size``
    the group primitives studies share (derived/primitives.py, SQL in derived/sql/): physicians by quarter, their
    reassignment edges with ``k``, and each organization's size as a headcount and as the sum of ``1/k``. With
    ``--medicaid``, ``medicaid_volume`` and ``main_group`` too, from the Medicaid source (``load --source-name
    medicaid``): each physician's main group by plurality of Medicaid volume.

``crosswalk``
    the links between identifiers those tables hold, as one long table of edges npi -> enrollment -> pac and
    enrollment -> ccn by quarter, and, with ``--compendium``, pac or ccn -> system by Compendium year
    (derived/crosswalk.py).

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

from .. import __version__
from ..core.connect import configure, open_catalog, source_file
from . import crosswalk, firms, primitives

log = logging.getLogger(__name__)

# PPEF provider types that make an organization a hospital entity (the "hospital tax id" of the literature)
HOSPITAL_TYPES = ("00-09", "00-85", "00-24", "12-Z1")
# the subset that is certified as a facility and therefore has a CCN in the POS hospital file
FACILITY_TYPES = ("00-09", "00-85", "00-24")
# PPEF provider types linked to POS nursing homes and home health agencies
NURSING_HOME_TYPES = ("00-18",)
HOME_HEALTH_TYPES = ("00-06",)


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
    Table("post_acute_facility", ("ccn",),
          ("facility_type", "fac_name", "subtype", "state", "zip5", "county_fips", "beds", "control_type"),
          "active POS nursing homes and home health agencies (QIES, then iQIES)"),
    Table("post_acute_link", ("enrollment_id",), ("organization_pac_id", "npi", "ccn", "facility_type", "match_rule"),
          "skilled nursing facility or home health enrollment matched to its POS CCN"),
)
# with --ownership, from the first quarter on or after the first ownership release (derived/firms.py)
FIRM_TABLES = (
    Table("control", ("owner_pac_id", "pac_id"), ("owner_name",),
          "organization (PAC id) controlling a facility enrollment's legal entity: a 5%+ direct or indirect owner with "
          "more than half of it or an unreported share (CMS All Owners)"),
    Table("firm_member", ("node_type", "node_id"), ("firm_id",),
          "organization or owner (pac) or facility (ccn) -> its firm, the connected component of control and "
          "enrollment -> CCN edges, with ids carried over between quarters"),
    Table("firm", ("firm_id",), ("name", "n_organizations", "n_hospitals", "n_post_acute", "n_facilities"),
          "firms (connected components of control) with counts of organizations, hospitals and post-acute facilities"),
    Table("chain", ("chain_type", "ccn"), ("chain_owner_pac_id", "chain_name", "chain_size"),
          "hospital and nursing home chains by ASPE's ultimate-owner method (Welch et al. 2023), three or more facilities"),
)
# built from the quarter's tables above, advanced like them, and written with the annual system edges appended
CROSSWALK = Table("crosswalk", crosswalk.KEY, crosswalk.ATTRS, crosswalk.DESCRIPTION)


def _in_list(values: tuple[str, ...]) -> str:
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


def _quarter_sql(d: str) -> dict[str, str]:
    """The SELECT that gives each table's rows on date d (an ISO date literal)."""
    def asof(alias: str = "") -> str:
        a = f"{alias}." if alias else ""
        return f"{a}effective_date <= DATE '{d}' AND ({a}end_date IS NULL OR {a}end_date > DATE '{d}')"
    hospital_types = ", ".join(f"'{t}'" for t in HOSPITAL_TYPES)
    facility_types = ", ".join(f"'{t}'" for t in FACILITY_TYPES)
    return {
        "enr": f"""
            SELECT enrollment_id, enrollee_type, npi, pac_id, provider_type_code, state
            FROM medicare_enrollment WHERE {asof()}""",
        "nppes": f"""
            SELECT npi, org_name, primary_taxonomy_code AS taxonomy_code,
                   left(practice_postal_code, 5) AS zip5, practice_state,
                   nullif(regexp_replace(practice_phone, '[^0-9]', '', 'g'), '') AS phone,
                   nullif(regexp_extract(practice_address_1, '^\\s*(\\d+)', 1), '') AS street_number
            FROM provider
            WHERE {asof()} AND npi IN (SELECT npi FROM enr)""",
        "practitioner": """
            WITH i AS (SELECT * FROM enr WHERE enrollee_type = 'individual' AND pac_id IS NOT NULL),
            first_npi AS (SELECT pac_id, min(npi) AS npi FROM i GROUP BY pac_id)
            SELECT i.pac_id, any_value(f.npi) AS npi,
                   list_sort(list(DISTINCT i.provider_type_code)) AS provider_type_codes,
                   list_sort(list(DISTINCT i.state)) AS enrollment_states,
                   any_value(n.taxonomy_code) AS taxonomy_code, any_value(n.zip5) AS practice_zip5,
                   any_value(n.practice_state) AS practice_state
            FROM i JOIN first_npi f USING (pac_id) LEFT JOIN nppes n ON n.npi = f.npi
            GROUP BY i.pac_id""",
        "organization": f"""
            WITH o AS (SELECT * FROM enr WHERE enrollee_type = 'organization' AND pac_id IS NOT NULL),
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
            FROM facility
            WHERE {asof()} AND facility_type = 'hospital' AND active""",
        "reassignment": f"""
            SELECT DISTINCT i.pac_id AS practitioner_pac_id, o.pac_id AS organization_pac_id
            FROM medicare_reassignment r
            JOIN enr i ON i.enrollment_id = r.enrollment_id
            JOIN enr o ON o.enrollment_id = r.receiving_enrollment_id
            WHERE {asof('r')}
              AND o.enrollee_type = 'organization' AND i.pac_id IS NOT NULL AND o.pac_id IS NOT NULL""",
        "hospital_link": _link_sql(facility_types, "hospital") + """
            SELECT enrollment_id, pac_id AS organization_pac_id, npi, ccn, match_rule
            FROM best WHERE n_best = 1""",
        "post_acute_facility": f"""
            SELECT ccn, facility_type, fac_name,
                   CASE pos_file WHEN 'qies' THEN prvdr_ctgry_cd || '-' || coalesce(prvdr_ctgry_sbtyp_cd, '')
                        ELSE 'iqies ' || prvdr_type_id || '-' || coalesce(prvdr_sbtyp_id, '') END AS subtype,
                   state_cd AS state, left(zip_cd, 5) AS zip5, fips_state_cd || fips_cnty_cd AS county_fips,
                   crtfd_bed_cnt AS beds, gnrl_cntl_type_cd AS control_type,
                   nullif(regexp_replace(phne_num, '[^0-9]', '', 'g'), '') AS phone,
                   nullif(regexp_extract(st_adr, '^\\s*(\\d+)', 1), '') AS street_number
            FROM facility
            WHERE {asof()} AND facility_type IN ('nursing_home', 'home_health') AND active""",
        "post_acute_link": _link_sql(
            _in_list(NURSING_HOME_TYPES + HOME_HEALTH_TYPES), "post_acute_facility",
            f"AND f.facility_type = CASE WHEN h.provider_type_code IN ({_in_list(HOME_HEALTH_TYPES)}) "
            f"THEN 'home_health' ELSE 'nursing_home' END") + """
            SELECT enrollment_id, pac_id AS organization_pac_id, npi, ccn, facility_type, match_rule
            FROM best WHERE n_best = 1""",
    }


def _hash_expr(cols: tuple[str, ...]) -> str:
    if not cols:
        return "''"
    return "md5(CAST(struct_pack(" + ", ".join(f"{c} := {c}" for c in cols) + ") AS VARCHAR))"


class GraphBuilder:
    """Builds the interval tables one PPEF quarter at a time, in an in-memory DuckDB with the warehouse's source
    files attached read-only (``core.connect.open_catalog``)."""

    def __init__(self, warehouse: Path, *, threads: int | None = None, memory_limit: str | None = None,
                 work_dir: Path | None = None, compendium: bool = False, medicaid: bool = False, ownership: bool = False):
        # tables the build creates stay in memory; with the Compendium, Medicaid or ownership, its source file must be
        # there too
        require = ["nppes", *(["ahrq"] if compendium else []), *(["medicaid"] if medicaid else []),
                   *(["ownership"] if ownership else [])]
        self.con = open_catalog(warehouse, read_only=True, require=require if len(require) > 1 else None)
        self.work_dir = work_dir
        self.compendium = compendium
        self.medicaid = medicaid
        self.ownership = ownership
        self.ownership_from: str | None = None     # the first ownership release, set by build()
        self.threads, self.memory_limit = threads, memory_limit
        if work_dir:
            work_dir.mkdir(parents=True, exist_ok=True)
        # spills into work_dir itself and keeps insertion order, unlike the loaders (plan 007 revisits this)
        configure(self.con, threads=threads, memory_limit=memory_limit, temp_dir=work_dir, preserve_insertion_order=True)
        self.stats: list[dict] = []

    def quarters(self) -> list[tuple[int, str]]:
        return [(s, d.isoformat()) for s, d in
                self.con.execute("SELECT snapshot_id, as_of_date FROM ppef_snapshot ORDER BY as_of_date").fetchall()]

    def _require(self) -> None:
        have = {r[0] for r in self.con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
        need = {"ppef_snapshot", "medicare_enrollment", "medicare_reassignment", "medicare_additional_npi", "provider",
                "facility"}
        missing = sorted(need - have)
        if missing:
            from ..core.errors import UserError
            raise UserError(f"the warehouse lacks {', '.join(missing)}; the graph needs the NPPES, PPEF and POS tables "
                            f"(load them with `load --source-name nppes`, `ppef` and `pos`)", exit_code=2)

    def _require_compendium(self) -> None:
        from .. import sources
        from ..core.errors import UserError
        ahrq = sources.by_name("ahrq")
        problems = ahrq.check_problems(self.con) if ahrq.loaded(self.con) else ["no Compendium tables"]
        if problems:
            raise UserError(f"--compendium needs the AHRQ source in the warehouse ({problems[0]}); load it with "
                            f"`healthcare-ledger load --source-name ahrq` (fetch the files with `download --source-name ahrq`)",
                            exit_code=2)

    def _require_medicaid(self) -> None:
        from .. import sources
        from ..core.errors import UserError
        medicaid = sources.by_name("medicaid")
        problems = medicaid.check_problems(self.con) if medicaid.loaded(self.con) else ["no medicaid_spending table"]
        if problems:
            raise UserError(f"--medicaid needs the Medicaid source in the warehouse ({problems[0]}); load it with "
                            f"`healthcare-ledger load --source-name medicaid` (fetch the zip with `download --source-name "
                            f"medicaid`)", exit_code=2)

    def _require_ownership(self) -> None:
        from .. import sources
        from ..core.errors import UserError
        src = sources.by_name("ownership")
        if not src.loaded(self.con):
            raise UserError("--ownership needs the ownership source in the warehouse; load it with "
                            "`healthcare-ledger load --source-name ownership` (fetch the files with `download --source-name "
                            "ownership`)", exit_code=2)
        self.ownership_from = self.con.execute("SELECT min(as_of_date)::VARCHAR FROM ownership_snapshot").fetchone()[0]

    def build(self, limit: int | None = None) -> list[dict]:
        self._require()
        if self.compendium:
            self._require_compendium()
        if self.medicaid:
            self._require_medicaid()
        if self.ownership:
            self._require_ownership()
        for t in (*TABLES, *FIRM_TABLES, CROSSWALK):
            self.con.execute(f"DROP TABLE IF EXISTS {t.name}_open; DROP TABLE IF EXISTS {t.name}_closed")
        for snapshot_id, d in self.quarters()[:limit]:
            started = time.time()
            self._quarter(snapshot_id, d)
            log.info("graph quarter %s: %s (%.0fs)", d,
                     ", ".join(f"{k} {v:,}" for k, v in self.stats[-1].items() if k.startswith("n_")),
                     time.time() - started)
        return self.stats

    def _quarter(self, snapshot_id: int, d: str) -> None:
        sql = _quarter_sql(d)
        con = self.con
        con.execute(f"CREATE OR REPLACE TEMP TABLE enr AS {sql['enr']}")
        con.execute(f"CREATE OR REPLACE TEMP TABLE nppes AS {sql['nppes']}")
        stat: dict = {"snapshot_id": snapshot_id, "as_of_date": d}
        for t in TABLES:
            con.execute(f"CREATE OR REPLACE TEMP TABLE {t.name} AS {sql[t.name]}")
            self._advance(t, d)
            stat[f"n_{t.name}"] = con.execute(f"SELECT count(*) FROM {t.name}").fetchone()[0]
        with_firms = self.ownership and d >= self.ownership_from
        if with_firms:
            stat.update(firms.build_quarter(con, d))
            for t in FIRM_TABLES:
                self._advance(t, d)
                stat[f"n_{t.name}"] = con.execute(f"SELECT count(*) FROM {t.name}").fetchone()[0]
        con.execute(f"CREATE OR REPLACE TEMP TABLE {CROSSWALK.name} AS {crosswalk.quarter_sql(d, firms=with_firms)}")
        self._advance(CROSSWALK, d)
        stat.update(zip(
            ("n_reassignment_rows", "n_reassignment_dropped", "n_hospital_enrollments"),
            con.execute(f"""
                WITH r AS (SELECT enrollment_id, receiving_enrollment_id FROM medicare_reassignment
                           WHERE effective_date <= DATE '{d}' AND (end_date IS NULL OR end_date > DATE '{d}'))
                SELECT (SELECT count(*) FROM r),
                       (SELECT count(*) FROM r WHERE r.enrollment_id NOT IN (SELECT enrollment_id FROM enr)
                           OR r.receiving_enrollment_id NOT IN (SELECT enrollment_id FROM enr WHERE enrollee_type = 'organization')),
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

    def _intervals(self, t: Table) -> str:
        return f"""SELECT {", ".join(t.key + t.attrs)}, effective_date, end_date FROM (
                       SELECT * FROM {t.name}_closed
                       UNION ALL BY NAME SELECT * EXCLUDE (_h), NULL::DATE AS end_date FROM {t.name}_open)"""

    def write(self, out: Path, warehouse: Path) -> dict:
        out.mkdir(parents=True, exist_ok=True)
        tables = {}
        built = {r[0] for r in self.con.execute("SELECT table_name FROM duckdb_tables()").fetchall()}
        for t in (*TABLES, *(t for t in FIRM_TABLES if f"{t.name}_open" in built)):
            path = out / f"{t.name}.parquet"
            self.con.execute(f"""
                COPY ({self._intervals(t)} ORDER BY {", ".join(t.key)}, effective_date)
                TO '{path}' (FORMAT parquet, COMPRESSION zstd)""")
            n = self.con.execute(f"SELECT count(*) FROM '{path}'").fetchone()[0]
            tables[t.name] = {"rows": n, "key": list(t.key), "description": t.description}
        compendium_years = None
        if self.compendium:
            from .compendium import system_tables
            compendium_years = system_tables(self.con)
            for name, key, description in (
                    ("system", ["year", "health_sys_id"], "AHRQ Compendium health systems, by Compendium year"),
                    ("system_link", ["year", "node_type", "node_id", "compendium_id"],
                     "organization (PAC id) or facility (CCN) -> Compendium system, by Compendium year")):
                path = out / f"{name}.parquet"
                self.con.execute(f"COPY {name} TO '{path}' (FORMAT parquet, COMPRESSION zstd)")
                n = self.con.execute(f"SELECT count(*) FROM '{path}'").fetchone()[0]
                tables[name] = {"rows": n, "key": key, "description": description, "annual": True}
        t = CROSSWALK
        edges = self._intervals(t) + (f" UNION ALL BY NAME {crosswalk.system_edges_sql()}" if self.compendium else "")
        path = out / f"{t.name}.parquet"
        self.con.execute(f"""
            COPY (SELECT {", ".join(crosswalk.COLUMNS)} FROM ({edges}) ORDER BY {", ".join(t.key)}, effective_date)
            TO '{path}' (FORMAT parquet, COMPRESSION zstd)""")
        n = self.con.execute(f"SELECT count(*) FROM '{path}'").fetchone()[0]
        tables[t.name] = {"rows": n, "key": [*t.key, "effective_date"], "description": t.description}
        with open(out / "quarters.csv", "w", newline="") as f:
            # the firm counts start with the first ownership quarter; earlier quarters leave them empty
            fields = list(dict.fromkeys(k for st in self.stats for k in st))
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(self.stats)
        # the group primitives read the tables just written, as a study opens them
        from .. import sources
        tables.update(primitives.build(
            out, medicaid=source_file(warehouse, sources.by_name("medicaid").database) if self.medicaid else None,
            threads=self.threads, memory_limit=self.memory_limit, work_dir=self.work_dir))
        manifest = {
            "generator": f"healthcare-ledger {__version__} graph-build",
            "warehouse": warehouse.name,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "first_as_of_date": self.stats[0]["as_of_date"],
            "last_as_of_date": self.stats[-1]["as_of_date"],
            "quarters": len(self.stats),
            "hospital_types": list(HOSPITAL_TYPES),
            "facility_types": list(FACILITY_TYPES),
            "tables": tables,
        }
        if self.ownership:
            manifest["ownership"] = {"source": "CMS Enrollments and All Owners files", "first_release": self.ownership_from,
                                     "control_roles": list(firms.CONTROL_ROLES), "chain_min": firms.CHAIN_MIN}
        if compendium_years:
            manifest["compendium"] = {"source": "AHRQ Compendium of U.S. Health Systems", "years": compendium_years}
        (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        return manifest

    def close(self) -> None:
        self.con.close()


def build_graph(warehouse: Path, out: Path, *, limit: int | None = None, compendium: bool = False, medicaid: bool = False,
                ownership: bool = False, **kw) -> dict:
    g = GraphBuilder(warehouse, compendium=compendium, medicaid=medicaid, ownership=ownership, **kw)
    try:
        g.build(limit)
        return g.write(out, warehouse)
    finally:
        g.close()
