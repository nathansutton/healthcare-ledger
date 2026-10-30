"""Owner communities: each organization in the provider graph with everything under the same owner, quarter by quarter.

A firm (derived/firms.py) is exact but reaches only what CMS's All Owners files cover: hospitals, nursing homes, home
health agencies, hospices and clinics, from 2022. Physician groups, dialysis chains, staffing companies and
everything before 2022 are outside it. An owner community adds them from three kinds of evidence:

hard links, never cut
    the All Owners firm of a facility's legal entity, and the AHRQ Compendium's health systems: a firm joins the one
    system that holds most of its hospitals, and a physician group in the Compendium's group practice file joins its
    system. AHRQ assigns each group to the system it mostly bills with, which for a staffing company (emergency,
    hospitalist, anesthesia) is the hospital it staffs rather than its owner, so a group whose NPPES family spans
    ``STAFFING_MIN_SYSTEMS`` or more systems keeps its family instead. Two systems are never joined by a hard link,
    and a firm is never split.
soft links, weighed
    keys that NPPES records for each organization NPI: the authorized official (name and phone), the mailing address,
    the practice phone, and a subpart's parent organization name (matched to an organization in the same state).
    Two organizations sharing a key get a probability of common ownership calibrated, once, on an anchor quarter:
    among pairs of organizations whose owner is known (the same All Owners firm or not; the same AHRQ system or not),
    the share with the same owner in each cell of key kind x key size x key cohesion x shared name. Cohesion
    separates a family from a vendor: a mailing address whose organizations each name their own official is a
    billing or credentialing vendor serving clients; one whose organizations share one official is a family (for an
    official's key, the same with mailing addresses). A shared name word that few organizations carry is the
    comparison field of record linkage (Fellegi and Sunter 1969). A pair's weight is 1 - prod(1 - p) over its keys.
clustering
    each hard unit is one node (its size the number of its organizations) and Leiden (Traag, Waltman and van Eck 2019)
    maximizes the Constant Potts Model at resolution 1/2: sum over communities of (weight inside - n_pairs / 2). This
    is correlation clustering: a community holds when its pairs are, on the whole, more likely under one owner than
    not, so a key shared by hundreds of unrelated clients cannot merge them.

Over time, the calibration is fixed (``owner_link_calibration.csv``) so the weights do not move from quarter to
quarter, Leiden starts each quarter from the previous quarter's communities so a community changes only when its
evidence does, and ids carry over as firm ids do (``firms.label_firms``): a community keeps the id of the previous
quarter's community holding most of its members. The AHRQ year in force on a quarter is the latest one on or before
it (the earliest before the first). Authorized officials' names and phones are join keys only; no output carries
them.
"""

from __future__ import annotations

import csv
import json
import logging
import time
from pathlib import Path

import duckdb

from .. import __version__
from ..core.connect import configure, open_catalog
from ..core.errors import UserError
from . import firms
from .graph import Table, advance, intervals_sql

log = logging.getLogger(__name__)

GAMMA = 0.5                   # CPM resolution: correlation clustering at even odds
MAX_KEY = 1000                # keys shared by more organizations are left out (pairs grow with the square)
MIN_CELL_PAIRS = 30           # a calibration cell needs this many known pairs, else its links carry no weight
STAFFING_MIN_SYSTEMS = 3      # a family whose groups AHRQ places in this many systems is a staffing company's
P_MAX = 0.99                  # no single key is certain
SEED = 42
# legal-form and filler words that never count as a shared name
STOP_WORDS = ("LLC", "INC", "PC", "PLLC", "PA", "LLP", "LTD", "LP", "CORP", "CORPORATION", "CO", "COMPANY", "MD", "DO",
              "THE", "OF", "AND", "A", "P", "C", "L", "S")
KEY_SIZES = "CASE WHEN n = 2 THEN '2' WHEN n <= 5 THEN '3-5' WHEN n <= 20 THEN '6-20' WHEN n <= 100 THEN '21-100' ELSE '101+' END"
NAME_BINS = ("CASE WHEN mindf IS NULL THEN 'no shared name' WHEN mindf <= 50 THEN 'rare name' "
             "WHEN mindf <= 2000 THEN 'name' ELSE 'common word' END")

TABLES = (
    Table("owner_community_member", ("pac_id",), ("community_id", "basis"),
          "organization (PAC id) -> its owner community; basis is how it is tied in: all_owners (a facility firm), "
          "ahrq (a Compendium system), nppes (shared NPPES keys only) or alone"),
    Table("owner_community", ("community_id",),
          ("name", "n_organizations", "n_hospitals", "n_post_acute", "physicians"),
          "owner communities with their organizations, hospitals and post-acute facilities (by the graph's links) and "
          "physicians (sum over member groups of group_size.physicians, each physician split across their groups)"),
)

_NORM = "trim(regexp_replace(regexp_replace(upper(coalesce({c}, '')), '[^A-Z0-9 ]', ' ', 'g'), ' +', ' ', 'g'))"


def _asof(d: str, alias: str = "") -> str:
    a = f"{alias}." if alias else ""
    return f"{a}effective_date <= DATE '{d}' AND ({a}end_date IS NULL OR {a}end_date > DATE '{d}')"


# ------------------------------------------------------------------------------------------- quarter inputs

def quarter_inputs(con: duckdb.DuckDBPyConnection, d: str, ahrq_year: int | None) -> None:
    """Create the quarter's input tables from the graph views and NPPES's ``provider``:
    ``oc_org`` (pac_id, org_name, state), ``oc_key`` (pac_id, kind, key), ``oc_firm`` (pac_id, firm_id: All Owners firms
    holding a facility), the Compendium's ``oc_sys_group`` (pac_id, sys: group practices), ``oc_firm_sys`` (firm_id,
    sys) and ``oc_fac_sys`` (pac_id, sys: facility legal entities by the graph's links), and ``oc_size`` (pac_id,
    physicians, n_hospitals, n_post_acute)."""
    con.execute(f"""CREATE OR REPLACE TEMP TABLE oc_org AS
        SELECT pac_id, org_name, practice_state AS state, npis FROM organization WHERE {_asof(d)}""")
    con.execute(f"""CREATE OR REPLACE TEMP TABLE oc_nppes AS
        SELECT o.pac_id, o.state, {_NORM.format(c='p.org_name')} AS name, {_NORM.format(c='p.parent_org_name')} AS parent,
               CASE WHEN p.ao_last_name IS NOT NULL THEN {_NORM.format(c='p.ao_first_name')} || ' '
                    || {_NORM.format(c='p.ao_last_name')} || '|' || regexp_replace(coalesce(p.ao_phone, ''), '[^0-9]', '', 'g')
               END AS ao,
               {_NORM.format(c='p.mail_address_1')} || '|' || left(p.mail_postal_code, 5) AS mail,
               regexp_replace(coalesce(p.practice_phone, ''), '[^0-9]', '', 'g') AS phone
        FROM provider p JOIN (SELECT pac_id, state, unnest(npis) AS npi FROM oc_org) o USING (npi) WHERE {_asof(d, 'p')}""")
    con.execute("""CREATE OR REPLACE TEMP TABLE oc_key AS
        SELECT DISTINCT pac_id, 'ao' AS kind, ao AS key FROM oc_nppes WHERE length(ao) > 12
        UNION SELECT pac_id, 'mail', mail FROM oc_nppes WHERE length(mail) > 8
        UNION SELECT pac_id, 'phone', phone FROM oc_nppes WHERE length(phone) = 10
        UNION SELECT n.pac_id, 'parent', n.state || '|' || n.parent FROM oc_nppes n WHERE n.parent <> ''
        UNION SELECT m.pac_id, 'parent', n.state || '|' || n.parent FROM oc_nppes n
              JOIN oc_nppes m ON m.name = n.parent AND m.state = n.state WHERE n.parent <> ''""")
    con.execute(f"""CREATE OR REPLACE TEMP TABLE oc_firm AS
        WITH m AS (SELECT * FROM firm_member WHERE {_asof(d)})
        SELECT node_id AS pac_id, firm_id FROM m
        WHERE node_type = 'pac' AND firm_id IN (SELECT firm_id FROM m WHERE node_type = 'ccn')""")
    con.execute(f"""CREATE OR REPLACE TEMP TABLE oc_firm_ccn AS
        SELECT node_id AS ccn, firm_id FROM firm_member WHERE {_asof(d)} AND node_type = 'ccn'""")
    y = ahrq_year if ahrq_year is not None else -1
    con.execute(f"""CREATE OR REPLACE TEMP TABLE oc_sys_group AS
        SELECT node_id AS pac_id, min(health_sys_id) AS sys FROM system_link
        WHERE year = {y} AND node_type = 'organization' AND health_sys_id IS NOT NULL GROUP BY 1""")
    # a firm's system, and a facility's legal entity's: the system holding most of its facilities (by CCN; hospitals,
    # nursing homes and home health agencies in the Compendium's facility files), ties broken by system id
    con.execute(f"""CREATE OR REPLACE TEMP TABLE oc_ccn_sys AS SELECT node_id AS ccn, min(health_sys_id) AS sys FROM system_link
        WHERE year = {y} AND node_type IN ('hospital', 'nursing_home', 'home_health') AND health_sys_id IS NOT NULL GROUP BY 1""")
    con.execute("""CREATE OR REPLACE TEMP TABLE oc_firm_sys AS
        SELECT firm_id, arg_min(sys, (-n, sys)) AS sys FROM (
            SELECT f.firm_id, s.sys, count(*) AS n FROM oc_ccn_sys s JOIN oc_firm_ccn f USING (ccn) GROUP BY 1, 2)
        GROUP BY 1""")
    con.execute(f"""CREATE OR REPLACE TEMP TABLE oc_fac_sys AS
        SELECT pac_id, arg_min(sys, (-n, sys)) AS sys FROM (
            SELECT l.pac_id, s.sys, count(*) AS n FROM oc_ccn_sys s JOIN (
                SELECT organization_pac_id AS pac_id, ccn FROM hospital_link WHERE {_asof(d)}
                UNION SELECT organization_pac_id, ccn FROM post_acute_link WHERE {_asof(d)}) l USING (ccn) GROUP BY 1, 2)
        GROUP BY 1""")
    con.execute(f"""CREATE OR REPLACE TEMP TABLE oc_size AS
        WITH g AS (SELECT organization_pac_id AS pac_id, physicians FROM group_size
                   WHERE as_of_date = (SELECT max(as_of_date) FROM group_size WHERE as_of_date <= DATE '{d}')),
             h AS (SELECT organization_pac_id AS pac_id, count(DISTINCT ccn) AS n FROM hospital_link WHERE {_asof(d)} GROUP BY 1),
             pa AS (SELECT organization_pac_id AS pac_id, count(DISTINCT ccn) AS n FROM post_acute_link WHERE {_asof(d)} GROUP BY 1)
        SELECT o.pac_id, coalesce(g.physicians, 0) AS physicians, coalesce(h.n, 0) AS n_hospitals, coalesce(pa.n, 0) AS n_post_acute
        FROM oc_org o LEFT JOIN g USING (pac_id) LEFT JOIN h USING (pac_id) LEFT JOIN pa USING (pac_id)""")


# ------------------------------------------------------------------------------------------- evidence

def evidence(con: duckdb.DuckDBPyConnection) -> None:
    """``oc_pair`` (x, y, kind, cell): every pair of organizations sharing a key of ``oc_key`` with at most MAX_KEY
    organizations, with the cell its link is calibrated in (key size / cohesion / shared name)."""
    con.execute(f"CREATE OR REPLACE TEMP TABLE oc_key_n AS SELECT kind, key, count(*) AS n, {KEY_SIZES} AS size FROM oc_key GROUP BY 1, 2")
    # cohesion: how many officials (for an address or phone) or addresses (for an official) the key's organizations
    # name; an organization naming none counts as its own
    con.execute("""CREATE OR REPLACE TEMP TABLE oc_key_coh AS
        SELECT k.kind, k.key,
               CASE WHEN count(DISTINCT coalesce(o.key, 'self:' || k.pac_id)) <= greatest(1, count(DISTINCT k.pac_id) // 3)
                    THEN 'cohesive'
                    WHEN count(DISTINCT coalesce(o.key, 'self:' || k.pac_id)) < count(DISTINCT k.pac_id) THEN 'mixed'
                    ELSE 'scattered' END AS coh
        FROM oc_key k LEFT JOIN oc_key o ON o.pac_id = k.pac_id AND o.kind = CASE k.kind WHEN 'ao' THEN 'mail' ELSE 'ao' END
        WHERE k.kind <> 'parent' GROUP BY 1, 2""")
    con.execute(f"""CREATE OR REPLACE TEMP TABLE oc_pair0 AS
        SELECT a.pac_id AS x, b.pac_id AS y, kind, min(n.size) AS size, min(coalesce(c.coh, '-')) AS coh
        FROM oc_key a JOIN oc_key b USING (kind, key) JOIN oc_key_n n USING (kind, key) LEFT JOIN oc_key_coh c USING (kind, key)
        WHERE a.pac_id < b.pac_id AND n.n <= {MAX_KEY} GROUP BY 1, 2, 3""")
    stop = ", ".join(f"'{w}'" for w in STOP_WORDS)
    con.execute(f"""CREATE OR REPLACE TEMP TABLE oc_tok AS
        SELECT * FROM (SELECT DISTINCT pac_id, unnest(string_split({_NORM.format(c='org_name')}, ' ')) AS tok FROM oc_org)
        WHERE tok NOT IN ({stop}) AND length(tok) >= 2""")
    con.execute("""CREATE OR REPLACE TEMP TABLE oc_names AS
        WITH df AS (SELECT tok, count(*) AS df FROM oc_tok GROUP BY 1), p AS (SELECT DISTINCT x, y FROM oc_pair0)
        SELECT p.x, p.y, min(df.df) AS mindf FROM p JOIN oc_tok t1 ON t1.pac_id = p.x
        JOIN oc_tok t2 ON t2.pac_id = p.y AND t2.tok = t1.tok JOIN df ON df.tok = t1.tok GROUP BY 1, 2""")
    con.execute(f"""CREATE OR REPLACE TEMP TABLE oc_pair AS
        SELECT p.x, p.y, p.kind, p.size || '/' || p.coh || '/' || {NAME_BINS} AS cell
        FROM oc_pair0 p LEFT JOIN oc_names USING (x, y)""")


def calibrate(con: duckdb.DuckDBPyConnection, half: int | None = None, min_pairs: int = MIN_CELL_PAIRS) -> None:
    """``oc_calib`` (kind, cell, pairs, p): among the quarter's pairs of organizations with a known owner (two members
    of All Owners facility firms, or two Compendium system groups outside them), the share under the same owner by
    cell. With ``half`` (0 or 1), only owners whose id hashes to it, so the other half can be held out."""
    keep = "" if half is None else f"AND hash(lab) % 2 = {half}"
    con.execute(f"""CREATE OR REPLACE TEMP TABLE oc_truth AS SELECT * FROM (
        SELECT 'firm' AS src, pac_id, firm_id AS lab FROM oc_firm
        UNION ALL SELECT 'system', pac_id, sys FROM oc_sys_group WHERE pac_id NOT IN (SELECT pac_id FROM oc_firm)) WHERE TRUE {keep}""")
    con.execute(f"""CREATE OR REPLACE TEMP TABLE oc_calib AS
        SELECT kind, cell, count(*) AS pairs, avg((t1.lab = t2.lab)::INT) AS p FROM oc_pair
        JOIN oc_truth t1 ON t1.pac_id = x JOIN oc_truth t2 ON t2.pac_id = y AND t2.src = t1.src
        GROUP BY 1, 2 HAVING count(*) >= {min_pairs}""")


# ------------------------------------------------------------------------------------------- hard units

def units(con: duckdb.DuckDBPyConnection) -> None:
    """``oc_staffing`` (pac_id) and ``oc_unit`` (pac_id, unit, basis): each organization's hard unit, the first of the
    system of its firm, its firm, the system of its facilities, its own Compendium system as a group (unless it is a
    staffing company's), or itself."""
    # a family key (a parent name, a cohesive key, or an official signing for organizations at a few addresses) whose
    # Compendium groups sit in STAFFING_MIN_SYSTEMS systems or more
    con.execute(f"""CREATE OR REPLACE TEMP TABLE oc_staffing AS
        WITH fam AS (SELECT k.kind, k.key FROM oc_key k LEFT JOIN oc_key_coh c USING (kind, key) JOIN oc_sys_group s USING (pac_id)
                     WHERE k.kind = 'parent' OR c.coh = 'cohesive' OR (k.kind = 'ao' AND c.coh = 'mixed') GROUP BY 1, 2 HAVING count(DISTINCT s.sys) >= {STAFFING_MIN_SYSTEMS})
        SELECT DISTINCT k.pac_id FROM oc_key k JOIN fam USING (kind, key) WHERE k.pac_id IN (SELECT pac_id FROM oc_sys_group)""")
    con.execute("""CREATE OR REPLACE TEMP TABLE oc_unit AS
        SELECT o.pac_id,
               CASE WHEN fs.sys IS NOT NULL THEN 'S' || fs.sys
                    WHEN f.firm_id IS NOT NULL THEN 'F' || f.firm_id
                    WHEN x.sys IS NOT NULL THEN 'S' || x.sys
                    WHEN s.sys IS NOT NULL AND o.pac_id NOT IN (SELECT pac_id FROM oc_staffing) THEN 'S' || s.sys
                    ELSE 'P' || o.pac_id END AS unit,
               CASE WHEN f.firm_id IS NOT NULL THEN 'all_owners'
                    WHEN x.sys IS NOT NULL THEN 'ahrq'
                    WHEN s.sys IS NOT NULL AND o.pac_id NOT IN (SELECT pac_id FROM oc_staffing) THEN 'ahrq' END AS basis
        FROM (SELECT pac_id FROM oc_org UNION SELECT pac_id FROM oc_firm) o
        LEFT JOIN oc_firm f USING (pac_id) LEFT JOIN oc_firm_sys fs USING (firm_id)
        LEFT JOIN oc_fac_sys x USING (pac_id) LEFT JOIN oc_sys_group s USING (pac_id)""")


# ------------------------------------------------------------------------------------------- clustering

def _leiden():
    try:
        import igraph
        import leidenalg
    except ImportError:
        raise UserError("owner communities need igraph and leidenalg; install them with "
                        "`uv sync --extra communities` (or `pip install healthcare-ledger[communities]`)", exit_code=2) from None
    return igraph, leidenalg


def cluster(con: duckdb.DuckDBPyConnection, gamma: float = GAMMA) -> None:
    """``comp`` (node, comp): each organization of ``oc_unit`` with the smallest PAC id of its community, by Leiden CPM
    over the hard units weighted by ``oc_pair`` x ``oc_calib``. Starts from ``prev_firm`` (node, firm_id: the previous
    quarter's communities) when it holds any of the units' organizations."""
    ig, la = _leiden()
    con.execute(f"""CREATE OR REPLACE TEMP TABLE oc_w AS
        SELECT x, y, 1 - product(1 - least(c.p, {P_MAX})) AS w FROM oc_pair JOIN oc_calib c USING (kind, cell) GROUP BY 1, 2""")
    units_ = con.execute("SELECT unit, count(*) FROM oc_unit GROUP BY 1 ORDER BY 1").fetchall()
    index = {u: i for i, (u, _) in enumerate(units_)}
    edges = con.execute("""SELECT least(u1.unit, u2.unit), greatest(u1.unit, u2.unit), sum(w) FROM oc_w
                           JOIN oc_unit u1 ON u1.pac_id = x JOIN oc_unit u2 ON u2.pac_id = y WHERE u1.unit <> u2.unit
                           GROUP BY 1, 2 ORDER BY 1, 2""").fetchall()
    g = ig.Graph(n=len(units_), edges=[(index[a], index[b]) for a, b, _ in edges], edge_attrs={"weight": [w for *_, w in edges]})
    con.execute("CREATE TEMP TABLE IF NOT EXISTS prev_firm (node VARCHAR, firm_id VARCHAR)")
    # each unit starts in the previous community of most of its organizations, ties broken by id
    start = dict(con.execute("""SELECT unit, arg_min(firm_id, (-n, firm_id)) FROM (
            SELECT u.unit, p.firm_id, count(*) AS n FROM oc_unit u JOIN prev_firm p ON p.node = u.pac_id GROUP BY 1, 2)
        GROUP BY 1""").fetchall())
    labels = {c: i for i, c in enumerate(sorted(set(start.values())))}
    # new units each start alone, numbered on from the carried labels (leidenalg wants labels below the node count)
    fresh = iter(range(len(labels), len(units_)))
    initial = [labels[start[u]] if u in start else next(fresh) for u, _ in units_]
    part = la.CPMVertexPartition(g, initial_membership=initial, weights="weight", node_sizes=[n for _, n in units_],
                                 resolution_parameter=gamma)
    opt = la.Optimiser()
    opt.set_rng_seed(SEED)
    opt.optimise_partition(part, n_iterations=-1)
    con.execute("CREATE OR REPLACE TEMP TABLE oc_unit_comm (unit VARCHAR, community INTEGER)")
    con.executemany("INSERT INTO oc_unit_comm VALUES (?, ?)", [(u, part.membership[i]) for i, (u, _) in enumerate(units_)])
    con.execute("""CREATE OR REPLACE TEMP TABLE comp AS
        SELECT u.pac_id AS node, min(u.pac_id) OVER (PARTITION BY c.community) AS comp FROM oc_unit u JOIN oc_unit_comm c USING (unit)""")


def label(con: duckdb.DuckDBPyConnection, d: str) -> None:
    """The quarter's ``owner_community_member`` and ``owner_community`` temp tables from ``comp``, with ids carried
    over from the previous quarter (``firms.label_firms``, prefix O)."""
    firms.label_firms(con, d, prefix="O")
    con.execute("""CREATE OR REPLACE TEMP TABLE owner_community_member AS
        WITH m AS (SELECT l.node AS pac_id, l.firm_id AS community_id, u.basis, count(*) OVER (PARTITION BY l.firm_id) AS n
                   FROM firm_label l JOIN oc_unit u ON u.pac_id = l.node)
        SELECT pac_id, community_id, coalesce(basis, CASE WHEN n > 1 THEN 'nppes' ELSE 'alone' END) AS basis FROM m""")
    con.execute("""CREATE OR REPLACE TEMP TABLE owner_community AS
        SELECT m.community_id,
               arg_max(o.org_name, (s.n_hospitals + s.n_post_acute) * 1e6 + s.physicians) AS name,
               count(*) AS n_organizations, sum(s.n_hospitals)::BIGINT AS n_hospitals,
               sum(s.n_post_acute)::BIGINT AS n_post_acute, round(sum(s.physicians), 1) AS physicians
        FROM owner_community_member m LEFT JOIN oc_org o USING (pac_id) LEFT JOIN oc_size s USING (pac_id)
        GROUP BY 1""")


# ------------------------------------------------------------------------------------------- the build

class OwnerCommunityBuilder:
    """Reads a built graph directory (with firms and Compendium systems) and the warehouse's NPPES ``provider``; adds
    the owner community tables to the graph directory."""

    NEED = ("organization", "firm_member", "system_link", "group_size", "hospital_link", "post_acute_link")

    def __init__(self, graph: Path, warehouse: Path, *, threads: int | None = None, memory_limit: str | None = None,
                 work_dir: Path | None = None):
        self.graph, self.warehouse = Path(graph), Path(warehouse)
        manifest = self.graph / "manifest.json"
        if not manifest.is_file():
            raise UserError(f"{self.graph} holds no graph (no manifest.json); build it with `healthcare-ledger graph-build "
                            "--ownership --compendium`", exit_code=2)
        self.manifest = json.loads(manifest.read_text())
        missing = [t for t in self.NEED if not (self.graph / f"{t}.parquet").is_file()]
        if missing:
            raise UserError(f"{self.graph} lacks {', '.join(missing)}; owner communities need a graph built with "
                            "`graph-build --ownership --compendium`", exit_code=2)
        self.con = open_catalog(self.warehouse, read_only=True)
        if work_dir:
            work_dir.mkdir(parents=True, exist_ok=True)
        configure(self.con, threads=threads, memory_limit=memory_limit, temp_dir=work_dir, preserve_insertion_order=True)
        for t in self.NEED:
            self.con.execute(f"CREATE TEMP VIEW {t} AS SELECT * FROM '{self.graph / t}.parquet'")
        self.ahrq_years = [r[0] for r in self.con.execute(
            "SELECT DISTINCT year FROM system_link WHERE node_type = 'organization' ORDER BY 1").fetchall()]
        self.firms_from = self.con.execute("SELECT min(effective_date)::VARCHAR FROM firm_member").fetchone()[0]
        with open(self.graph / "quarters.csv") as f:
            self.dates = [r["as_of_date"] for r in csv.DictReader(f)]
        self.stats: list[dict] = []

    def ahrq_year(self, d: str) -> int | None:
        if not self.ahrq_years:
            return None
        prior = [y for y in self.ahrq_years if y <= int(d[:4])]
        return prior[-1] if prior else self.ahrq_years[0]

    def anchor(self) -> str:
        """The default calibration quarter: the last quarter with All Owners firms in a year with a Compendium group
        file of its own (not carried forward), else the last quarter with firms."""
        own = {r[0] for r in self.con.execute(
            "SELECT DISTINCT year FROM system_link WHERE node_type = 'organization' AND via = 'group practice file'").fetchall()}
        with_firms = [d for d in self.dates if self.firms_from and d >= self.firms_from]
        if not with_firms:
            raise UserError(f"{self.graph} has no quarter with All Owners firms to calibrate on; build it with "
                            "`graph-build --ownership --compendium`", exit_code=2)
        both = [d for d in with_firms if int(d[:4]) in own]
        return (both or with_firms)[-1]

    def calibrate(self, anchor: str | None = None, min_pairs: int = MIN_CELL_PAIRS) -> str:
        anchor = anchor or self.anchor()
        if anchor not in self.dates:
            raise UserError(f"--anchor {anchor} is not a graph quarter (quarters.csv)", exit_code=2)
        started = time.time()
        quarter_inputs(self.con, anchor, self.ahrq_year(anchor))
        evidence(self.con)
        calibrate(self.con, min_pairs=min_pairs)
        self.con.execute("CREATE OR REPLACE TEMP TABLE oc_calib_fixed AS SELECT * FROM oc_calib")
        log.info("owner communities: calibrated %d cells on %s (%.0fs)",
                 self.con.execute("SELECT count(*) FROM oc_calib").fetchone()[0], anchor, time.time() - started)
        self.anchor_date = anchor
        return anchor

    def build(self, limit: int | None = None) -> list[dict]:
        con = self.con
        for t in TABLES:
            con.execute(f"DROP TABLE IF EXISTS {t.name}_open; DROP TABLE IF EXISTS {t.name}_closed")
        prev: dict[str, str] = {}
        for d in self.dates[:limit]:
            started = time.time()
            year = self.ahrq_year(d)
            quarter_inputs(con, d, year)
            evidence(con)
            con.execute("CREATE OR REPLACE TEMP TABLE oc_calib AS SELECT * FROM oc_calib_fixed")
            units(con)
            cluster(con)
            label(con, d)
            for t in TABLES:
                advance(con, t, d)
            cur = dict(con.execute("SELECT pac_id, community_id FROM owner_community_member").fetchall())
            both = [p for p in cur if p in prev]
            row = con.execute("""SELECT count(*) FILTER (n_organizations > 1), coalesce(sum(n_organizations) FILTER (n_organizations > 1), 0),
                                        coalesce(sum(physicians) FILTER (n_organizations > 1), 0), coalesce(sum(physicians), 0)
                                 FROM owner_community""").fetchone()
            basis = dict(con.execute("SELECT basis, count(*) FROM owner_community_member GROUP BY 1").fetchall())
            self.stats.append({
                "as_of_date": d, "ahrq_year": year, "all_owners": bool(self.firms_from and d >= self.firms_from),
                "n_communities_multi": row[0], "n_organizations_multi": row[1],
                "physicians_multi": round(row[2], 1), "physicians": round(row[3], 1),
                **{f"n_{b}": basis.get(b, 0) for b in ("all_owners", "ahrq", "nppes", "alone")},
                "n_staffing": con.execute("SELECT count(*) FROM oc_staffing").fetchone()[0],
                # the share of organizations present in both quarters whose community id changed
                "churn": round(sum(cur[p] != prev[p] for p in both) / len(both), 4) if both else None,
            })
            prev = cur
            log.info("owner communities %s: %s (%.0fs)", d, self.stats[-1], time.time() - started)
        return self.stats

    def write(self) -> dict:
        out = self.graph
        tables = {}
        for t in TABLES:
            path = out / f"{t.name}.parquet"
            self.con.execute(f"COPY ({intervals_sql(t)} ORDER BY {', '.join(t.key)}, effective_date) "
                             f"TO '{path}' (FORMAT parquet, COMPRESSION zstd)")
            n = self.con.execute(f"SELECT count(*) FROM '{path}'").fetchone()[0]
            tables[t.name] = {"rows": n, "key": list(t.key), "description": t.description}
        self.con.execute(f"COPY (SELECT * FROM oc_calib_fixed ORDER BY kind, cell) TO '{out / 'owner_link_calibration.csv'}' (HEADER)")
        with open(out / "owner_communities.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(self.stats[0]))
            w.writeheader()
            w.writerows(self.stats)
        m = self.manifest
        m["tables"].update(tables)
        m["owner_communities"] = {
            "generator": f"healthcare-ledger {__version__} owner-communities", "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "anchor": self.anchor_date, "quarters": len(self.stats), "gamma": GAMMA, "max_key": MAX_KEY,
            "staffing_min_systems": STAFFING_MIN_SYSTEMS, "ahrq_years": self.ahrq_years, "all_owners_from": self.firms_from,
        }
        (out / "manifest.json").write_text(json.dumps(m, indent=2) + "\n")
        return m

    def close(self) -> None:
        self.con.close()


def build_owner_communities(graph: Path, warehouse: Path, *, anchor: str | None = None, limit: int | None = None,
                            min_pairs: int = MIN_CELL_PAIRS, **kw) -> dict:
    b = OwnerCommunityBuilder(graph, warehouse, **kw)
    try:
        b.calibrate(anchor, min_pairs=min_pairs)
        b.build(limit)
        return b.write()
    finally:
        b.close()
