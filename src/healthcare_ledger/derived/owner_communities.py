"""Owner communities: each organization in the provider graph with everything under the same owner, quarter by quarter.

A firm (derived/firms.py) is exact but reaches only what CMS's All Owners files cover: hospitals, nursing homes, home
health agencies, hospices and clinics, from 2022. Physician groups, dialysis chains, staffing companies and
everything before 2022 are outside it. An owner community adds them from three kinds of evidence:

hard links, never cut
    the All Owners firm of a facility's legal entity, and the AHRQ Compendium's health systems: a firm (or a
    facility's legal entity outside any firm) joins the system that holds the majority of its facilities the
    Compendium lists, hospitals if it has any, and a physician group in the Compendium's group practice file joins its
    system. AHRQ assigns each group to the system it mostly bills with, which for a staffing company (emergency,
    hospitalist, anesthesia) is the hospital it staffs rather than its owner, so a group whose NPPES family spans
    ``STAFFING_MIN_SYSTEMS`` or more systems keeps its family instead. Two systems are never joined by a hard link,
    and a firm is never split. Only the graph's organizations (Medicare-enrolled legal entities) are members; a
    holding company that is an owner in All Owners but enrolls nothing is not.
soft links, weighed
    keys that NPPES records for each organization NPI: the authorized official (name and phone), the mailing address,
    the practice phone, and a subpart's parent organization name (matched to an organization in the same state);
    and, from the warehouse's other sources when they are loaded, the chain home office a facility's cost report
    names (``hcris``, hospitals from 2010, nursing homes from 2011, home health agencies from 2020), the owner of 5%
    or more Nursing Home Compare listed for a nursing home (``nhc``, 2014 to 2022) and, with ``--tic``, the
    organizations a Form 990's Schedule R relates (``irs``, tax years 2013 on): a filer's EIN and its related
    entities' EINs are carried to the graph's organizations by the EIN-to-NPI rosters of insurers' Transparency in
    Coverage files, and a listing is ``mutual`` when the related entity's own Schedule R lists the filer back,
    ``listed`` otherwise. Related organizations are not merged transitively (two systems sharing a joint venture
    each list it, and it lists both back): each listing is one pair's evidence, and Leiden decides.
    Two organizations sharing keys get one probability of common ownership, calibrated once on an anchor quarter:
    among pairs of organizations whose owner is known (two members of AHRQ systems, the same one or not; two members
    of All Owners firms outside any system, the same one or not), the share with the same owner in each cell of (the set of key kinds shared) x (the size of the strongest key
    shared, the one with the fewest organizations) x (that key's cohesion) x (shared name). Pricing the set of kinds
    together, rather than each kind on its own and multiplying as if independent, matters because in a family the
    official, the mailing address and the phone are usually one person filling in one form: three correlated
    votes, not three independent ones (the naive-Bayes step of Fellegi and Sunter 1969 overstates such pairs).
    Cohesion separates a family from a vendor: a mailing address whose organizations each name their own official is
    a billing or credentialing vendor serving clients; one whose organizations share one official is a family (for
    an official's key, the same with mailing addresses). A shared name word that few organizations carry is the
    comparison field of record linkage. A cell with fewer than ``MIN_CELL_PAIRS`` known pairs backs off to the cell
    without cohesion and name, then to the strongest kind alone with the size; a pair whose cells are all too thin
    carries no weight.
clustering
    each hard unit is one node (its size the number of its organizations) and Leiden (Traag, Waltman and van Eck 2019)
    maximizes the Constant Potts Model at resolution ``GAMMA`` = 1/2 with the probabilities as weights: sum over
    communities of (weight inside - n_pairs / 2). With p as the weight this is correlation clustering that maximizes
    the expected number of correctly placed pairs: a community holds when its pairs are, on the whole, more likely
    under one owner than not, so a key shared by hundreds of unrelated clients cannot merge them. (Log-odds weights
    would give the most likely partition instead; p is used because each p is an empirical share with a floor of
    MIN_CELL_PAIRS pairs behind it, and the two agree wherever p is far from 1/2.)

Over time, the calibration is fixed (``owner_link_calibration.csv``) so the weights do not move from quarter to
quarter, Leiden starts each quarter from the previous quarter's communities, a pair of organizations together last
quarter holds at a lower bar (p > GAMMA - HISTORY, evolutionary clustering) so a link wavering about the bar does not
move an organization back and forth, a firm membership or facility link that CMS's files drop for at most MAX_HOLE
quarters and restore is run through the hole, and ids carry over as firm ids do (``firms.label_firms``): a community
keeps the id of the previous quarter's community holding most of its members. The AHRQ year in force on a quarter is
the latest one on or before it (the earliest before the first). Each quarter also records, for every member of a
community of two or more, how much NPPES evidence holds it there (``owner_community_support``), and every flow of
organizations between communities from the quarter before (``owner_community_event``: a merger, a split, a move, an
entry or an exit), so the id rule hides nothing. Authorized officials' names and phones are join keys only; no
output carries them.
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
from .firms import asof
from .graph import Table, advance, intervals_sql

log = logging.getLogger(__name__)

GAMMA = 0.5                   # CPM resolution: correlation clustering at even odds
MAX_KEY = 1000                # keys shared by more organizations are left out (pairs grow with the square)
MIN_CELL_PAIRS = 30           # a calibration cell needs this many known pairs, else the pair backs off to a coarser cell
STAFFING_MIN_SYSTEMS = 3      # a family whose groups AHRQ places in this many systems is a staffing company's
HISTORY = 0.1                 # a pair together last quarter holds while p > GAMMA - HISTORY (evolutionary clustering)
MAX_HOLE = 2                  # a firm membership or facility link gone for at most this many quarters is a hole
SEED = 42
LEIDEN_ITERATIONS = 2         # Leiden passes per quarter; the warm start carries the optimisation from quarter to quarter
# interval tables whose holes are closed: (the table's key, the column that must come back unchanged)
HOLES = {"firm_member": ("node_type, node_id", "firm_id"), "hospital_link": ("enrollment_id", "ccn"),
         "post_acute_link": ("enrollment_id", "ccn")}
# legal-form and filler words that never count as a shared name (single letters never reach the test: a name word
# is two characters or more)
STOP_WORDS = ("LLC", "INC", "PC", "PLLC", "PA", "LLP", "LTD", "LP", "CORP", "CORPORATION", "CO", "COMPANY", "MD", "DO",
              "THE", "OF", "AND")
KEY_SIZES = "CASE WHEN n = 2 THEN '2' WHEN n <= 5 THEN '3-5' WHEN n <= 20 THEN '6-20' WHEN n <= 100 THEN '21-100' ELSE '101+' END"
NAME_BINS = ("CASE WHEN mindf IS NULL THEN 'no shared name' WHEN mindf <= 50 THEN 'rare name' "
             "WHEN mindf <= 2000 THEN 'name' ELSE 'common word' END")
# calibration levels, finest first: the columns of oc_pair a cell is keyed on
LEVELS = (("kinds", "size", "coh", "name"), ("kinds", "size"), ("kind1", "size"))
HCRIS_STALE_MONTHS = 18       # a cost report whose fiscal year ended this long before the quarter still names the chain
NHC_STALE_MONTHS = 6          # a Nursing Home Compare month this far before the quarter still names the owners
IRS_FILER_TYPES = ("501c3", "501c2", "501c4", "501c25")
IRS_RELATED_SUBSECTIONS = ("3", "2", "4", "25", "")
IRS_SUBSECTION = "regexp_extract(upper(coalesce({c}, '')), 'C\\)?\\s*\\(?\\s*([0-9]+)', 1)"
# evidence a source adds: the warehouse table it needs
SOURCE_TABLES = {"hcris": "hcris_home_office", "nhc": "nhc_ownership", "irs": "irs_related"}

TABLES = (
    Table("owner_community_member", ("pac_id",), ("community_id", "basis"),
          "organization (PAC id) -> its owner community; basis is how it is tied in: all_owners (a facility firm), "
          "ahrq (a Compendium system), nppes (shared NPPES keys only) or alone"),
    Table("owner_community", ("community_id",),
          ("name", "n_organizations", "n_hospitals", "n_post_acute", "physicians"),
          "owner communities with their organizations, hospitals and post-acute facilities (by the graph's links) and "
          "physicians (sum over member groups of group_size.physicians, each physician split across their groups)"),
)
# per-quarter tables (as_of_date in the key), not intervals
QUARTER_TABLES = (
    Table("owner_community_support", ("as_of_date", "pac_id"), ("community_id", "support", "margin"),
          "each member of a community of two or more, by quarter, with the NPPES evidence holding it there: support "
          "is the sum of its link probabilities to the other members, margin is support - gamma x (members - 1), the "
          "organization's own gain from staying (a hard-linked member stays whatever its margin; a soft member with a "
          "margin below zero is held by its history or by the community's gain as a whole)"),
    Table("owner_community_event", ("as_of_date", "from_community_id", "to_community_id"), ("n_organizations", "event"),
          "flows of organizations between communities from the previous quarter: absorbed (the whole predecessor id "
          "ended and its members went here), split (a block left a surviving predecessor for a new id), move (between "
          "two surviving communities), entry (an organization new to the graph) or exit (one that left it)"),
)

_NORM = "trim(regexp_replace(regexp_replace(upper(coalesce({c}, '')), '[^A-Z0-9 ]', ' ', 'g'), ' +', ' ', 'g'))"


# ------------------------------------------------------------------------------------------- quarter inputs

def filled(src: str, part: str, same: str) -> str:
    """A SELECT over the interval rows of ``src`` with their holes closed, and ``_hole`` marking the rows run on: a row
    that ends and comes back (the next row of the key ``part`` with the same ``same``) within MAX_HOLE quarters of
    ``oc_quarter`` (d) runs on to its return. CMS's All Owners and enrollment files drop an owner or an enrollment
    for a quarter or two and restore it; the hole is known only once the row is back, so the last quarters keep
    theirs. ``part`` is the table's key, so each partition's rows are disjoint in time and the order is fixed."""
    return f"""WITH s AS (SELECT *, lead(effective_date) OVER w AS _next, lead({same}) OVER w AS _next_same
                          FROM {src} WINDOW w AS (PARTITION BY {part} ORDER BY effective_date)),
        h AS (SELECT *, _next_same = {same} AND _next > end_date
                        AND (SELECT count(*) FROM oc_quarter q WHERE q.d >= s.end_date AND q.d < s._next) <= {MAX_HOLE} AS _hole
              FROM s)
        SELECT * EXCLUDE (end_date, _next, _next_same, _hole), CASE WHEN _hole THEN _next ELSE end_date END AS end_date,
               coalesce(_hole, FALSE) AS _hole FROM h"""


def provider_keys(con: duckdb.DuckDBPyConnection) -> None:
    """``oc_provider``: every version of every organization NPI the graph's organizations ever held, from NPPES's
    ``provider``, with its keys normalized: name, parent name, official (name|phone, only with a ten-digit phone: a
    name alone links namesakes nationwide), mailing address (line|ZIP5) and practice phone. Built once; each quarter
    reads the versions in force on its date."""
    con.execute(f"""CREATE OR REPLACE TEMP TABLE oc_provider AS
        SELECT p.npi, p.effective_date, p.end_date, {_NORM.format(c='p.org_name')} AS name,
               {_NORM.format(c='p.parent_org_name')} AS parent,
               CASE WHEN p.ao_last_name IS NOT NULL AND regexp_matches(coalesce(p.ao_phone, ''), '^[0-9]{{10}}$')
                    THEN {_NORM.format(c='p.ao_first_name')} || ' ' || {_NORM.format(c='p.ao_last_name')} || '|' || p.ao_phone
               END AS ao,
               {_NORM.format(c='p.mail_address_1')} || '|' || left(p.mail_postal_code, 5) AS mail,
               regexp_replace(coalesce(p.practice_phone, ''), '[^0-9]', '', 'g') AS phone
        FROM provider p WHERE p.npi IN (SELECT unnest(npis) FROM organization)""")


def quarter_inputs(con: duckdb.DuckDBPyConnection, d: str, ahrq_year: int | None, evidence_from: tuple[str, ...] = ()) -> None:
    """Create the quarter's input tables from the graph views and ``oc_provider`` (``provider_keys``):
    ``oc_org`` (pac_id, org_name, state), ``oc_key`` (pac_id, kind, key), ``oc_firm`` (pac_id, firm_id: All Owners firms
    holding a facility, over the graph's organizations), the Compendium's ``oc_sys_group`` (pac_id, sys: group
    practices), ``oc_firm_sys`` (firm_id, sys) and ``oc_fac_sys`` (pac_id, sys: facility legal entities by the graph's
    links), and ``oc_size`` (pac_id, physicians, n_hospitals, n_post_acute). With ``evidence_from`` (names of
    ``SOURCE_TABLES``), ``oc_key`` also holds the ``chain`` and ``nhc`` keys and ``oc_pair_extra`` (x, y, kind, rank,
    size, coh) the ``irs`` pairs (``extra_evidence``)."""
    con.execute(f"""CREATE OR REPLACE TEMP TABLE oc_org AS
        SELECT pac_id, org_name, practice_state AS state, npis FROM organization WHERE {asof(d)}""")
    con.execute(f"""CREATE OR REPLACE TEMP TABLE oc_nppes AS
        SELECT o.pac_id, o.state, p.name, p.parent, p.ao, p.mail, p.phone
        FROM oc_provider p JOIN (SELECT pac_id, state, unnest(npis) AS npi FROM oc_org) o USING (npi) WHERE {asof(d, 'p')}""")
    con.execute("""CREATE OR REPLACE TEMP TABLE oc_key AS
        SELECT DISTINCT pac_id, 'ao' AS kind, ao AS key FROM oc_nppes WHERE ao IS NOT NULL AND length(ao) > 12
        UNION SELECT pac_id, 'mail', mail FROM oc_nppes WHERE length(mail) > 8
        UNION SELECT pac_id, 'phone', phone FROM oc_nppes WHERE length(phone) = 10
        UNION SELECT n.pac_id, 'parent', n.state || '|' || n.parent FROM oc_nppes n WHERE n.parent <> ''
        UNION SELECT m.pac_id, 'parent', n.state || '|' || n.parent FROM oc_nppes n
              JOIN oc_nppes m ON m.name = n.parent AND m.state = n.state WHERE n.parent <> ''""")
    con.execute(f"""CREATE OR REPLACE TEMP TABLE oc_firm AS
        WITH m AS (SELECT * FROM firm_member WHERE {asof(d)})
        SELECT node_id AS pac_id, firm_id FROM m
        WHERE node_type = 'pac' AND node_id IN (SELECT pac_id FROM oc_org) AND firm_id IN (SELECT firm_id FROM m WHERE node_type = 'ccn')""")
    con.execute(f"""CREATE OR REPLACE TEMP TABLE oc_firm_ccn AS
        SELECT node_id AS ccn, firm_id FROM firm_member WHERE {asof(d)} AND node_type = 'ccn'""")
    y = ahrq_year if ahrq_year is not None else -1
    con.execute(f"""CREATE OR REPLACE TEMP TABLE oc_sys_group AS
        SELECT node_id AS pac_id, min(health_sys_id) AS sys FROM system_link
        WHERE year = {y} AND node_type = 'organization' AND health_sys_id IS NOT NULL GROUP BY 1""")
    # every facility the Compendium's facility files list, with its system or none (most nursing homes and home
    # health agencies are listed outside any system)
    con.execute(f"""CREATE OR REPLACE TEMP TABLE oc_ccn_listed AS
        SELECT node_id AS ccn, min(health_sys_id) AS sys, bool_or(node_type = 'hospital') AS hospital FROM system_link
        WHERE year = {y} AND node_type IN ('hospital', 'nursing_home', 'home_health') GROUP BY 1""")
    con.execute("CREATE OR REPLACE TEMP TABLE oc_firm_sys AS " + majority_sys("oc_firm_ccn", "firm_id"))
    con.execute(f"""CREATE OR REPLACE TEMP TABLE oc_fac_ccn AS
        SELECT organization_pac_id AS pac_id, ccn FROM hospital_link WHERE {asof(d)}
        UNION SELECT organization_pac_id, ccn FROM post_acute_link WHERE {asof(d)}""")
    con.execute("CREATE OR REPLACE TEMP TABLE oc_fac_sys AS " + majority_sys("oc_fac_ccn", "pac_id"))
    extra_evidence(con, d, evidence_from)
    con.execute(f"CREATE OR REPLACE TEMP TABLE oc_sys_name AS SELECT health_sys_id AS sys, any_value(health_sys_name) AS name "
                f"FROM system WHERE year = {y} GROUP BY 1")
    con.execute(f"CREATE OR REPLACE TEMP TABLE oc_firm_name AS SELECT firm_id, name FROM firm WHERE {asof(d)}")
    con.execute(f"""CREATE OR REPLACE TEMP TABLE oc_size AS
        WITH g AS (SELECT organization_pac_id AS pac_id, physicians FROM group_size
                   WHERE as_of_date = (SELECT max(as_of_date) FROM group_size WHERE as_of_date <= DATE '{d}')),
             h AS (SELECT organization_pac_id AS pac_id, count(DISTINCT ccn) AS n FROM hospital_link WHERE {asof(d)} GROUP BY 1),
             pa AS (SELECT organization_pac_id AS pac_id, count(DISTINCT ccn) AS n FROM post_acute_link WHERE {asof(d)} GROUP BY 1)
        SELECT o.pac_id, coalesce(g.physicians, 0) AS physicians, coalesce(h.n, 0) AS n_hospitals, coalesce(pa.n, 0) AS n_post_acute
        FROM oc_org o LEFT JOIN g USING (pac_id) LEFT JOIN h USING (pac_id) LEFT JOIN pa USING (pac_id)""")


def majority_sys(src: str, id_col: str) -> str:
    """A SELECT of (``id_col``, sys) for the ids of ``src`` (``id_col``, ccn) whose facilities a Compendium system
    holds the majority of, among their facilities ``oc_ccn_listed`` lists: their hospitals when they have any, else
    their nursing homes and home health agencies. A plurality of the few facilities a system holds would put a chain
    of nursing homes, two of whose 33 the Compendium places in a hospital system, in that system."""
    return f"""WITH f AS (SELECT s.{id_col}, l.sys, l.hospital FROM {src} s JOIN oc_ccn_listed l USING (ccn)),
        k AS (SELECT f.* FROM f JOIN (SELECT {id_col}, bool_or(hospital) AS h FROM f GROUP BY 1) b USING ({id_col}) WHERE f.hospital = b.h),
        n AS (SELECT {id_col}, sys, count(*) AS n, sum(count(*)) OVER (PARTITION BY {id_col}) AS total FROM k GROUP BY 1, 2)
        SELECT {id_col}, sys FROM n WHERE sys IS NOT NULL AND 2 * n > total"""


def extra_evidence(con: duckdb.DuckDBPyConnection, d: str, evidence_from: tuple[str, ...]) -> None:
    """Add the quarter's keys from the other sources to ``oc_key`` and create ``oc_pair_extra``:

    ``chain``
        the home office id of the cost report in force for each facility's legal entity (``oc_fac_ccn``): the report
        whose fiscal period covers the quarter, else the latest one ended within HCRIS_STALE_MONTHS before it.
    ``nhc``
        each owner of 5% or more (``role`` beginning with 5%) in the latest Nursing Home Compare month on or before
        the quarter, within NHC_STALE_MONTHS, as ``type|name``, for the nursing home's legal entity.
    ``irs`` (``oc_pair_extra``)
        every Schedule R listing of the latest tax year before the quarter's year (a return for tax year T is filed
        during T+1) by a charity-type filer (IRS_FILER_TYPES) of a controlled charity-type or taxable entity
        (``irs_related.controlled``, IRS_RELATED_SUBSECTIONS), both EINs carried to organizations by ``oc_ein_pac``
        (``tic_roster``), one pair per organization pair: rank 1 and size ``mutual`` when the related entity's
        Schedule R lists the filer back, rank 2 and ``listed`` otherwise.
    """
    con.execute("CREATE OR REPLACE TEMP TABLE oc_pair_extra (x VARCHAR, y VARCHAR, kind VARCHAR, rank INTEGER, size VARCHAR, coh VARCHAR)")
    if "hcris" in evidence_from:
        con.execute(f"""INSERT INTO oc_key
            SELECT DISTINCT f.pac_id, 'chain', h.home_office_id FROM (
                SELECT ccn, home_office_id, row_number() OVER (PARTITION BY ccn ORDER BY (fy_end >= DATE '{d}') DESC, fy_end DESC, rpt_rec_num) AS r
                FROM hcris_home_office
                WHERE chain AND home_office_id IS NOT NULL AND fy_begin <= DATE '{d}'
                  AND fy_end >= DATE '{d}' - INTERVAL {HCRIS_STALE_MONTHS} MONTH) h
            JOIN oc_fac_ccn f USING (ccn) WHERE h.r = 1""")
    if "nhc" in evidence_from:
        con.execute(f"""INSERT INTO oc_key
            SELECT DISTINCT f.pac_id, 'nhc', coalesce(n.owner_type, '?') || '|' || n.owner_name FROM nhc_ownership n JOIN oc_fac_ccn f USING (ccn)
            WHERE n.as_of_date = (SELECT max(as_of_date) FROM nhc_ownership WHERE as_of_date <= DATE '{d}')
              AND n.as_of_date >= DATE '{d}' - INTERVAL {NHC_STALE_MONTHS} MONTH AND n.role LIKE '5%%' AND n.owner_name IS NOT NULL""")
    if "irs" in evidence_from:
        year = int(d[:4]) - 1
        types = ", ".join(f"'{t}'" for t in IRS_FILER_TYPES)
        subs = ", ".join(f"'{t}'" for t in IRS_RELATED_SUBSECTIONS)
        con.execute(f"""CREATE OR REPLACE TEMP TABLE oc_irs_edge AS
            WITH t AS (SELECT max(tax_year) AS y FROM irs_return WHERE tax_year <= {year}),
            e AS (SELECT DISTINCT r.ein AS a, r.related_ein AS b FROM irs_related r, t
                  WHERE r.tax_year = t.y AND r.controlled AND r.ein IS NOT NULL AND r.related_ein IS NOT NULL
                    AND (r.kind <> 'exempt' OR {IRS_SUBSECTION.format(c='r.exempt_code')} IN ({subs}))
                    AND EXISTS (SELECT 1 FROM irs_return f WHERE f.tax_year = t.y AND f.ein = r.ein AND f.exempt_type IN ({types})))
            SELECT e.a, e.b, EXISTS (SELECT 1 FROM e r WHERE r.a = e.b AND r.b = e.a) AS mutual FROM e""")
        con.execute("""INSERT INTO oc_pair_extra
            SELECT least(pa.pac_id, pb.pac_id), greatest(pa.pac_id, pb.pac_id), 'irs',
                   CASE WHEN bool_or(e.mutual) THEN 1 ELSE 2 END, CASE WHEN bool_or(e.mutual) THEN 'mutual' ELSE 'listed' END, '-'
            FROM oc_irs_edge e JOIN oc_ein_pac pa ON pa.ein = e.a JOIN oc_ein_pac pb ON pb.ein = e.b
            WHERE pa.pac_id <> pb.pac_id AND pa.pac_id IN (SELECT pac_id FROM oc_org) AND pb.pac_id IN (SELECT pac_id FROM oc_org)
            GROUP BY 1, 2""")


def tic_roster(con: duckdb.DuckDBPyConnection, heads: Path) -> int:
    """``oc_ein_pac`` (ein, pac_id): every EIN an insurer's Transparency in Coverage file lists an organization NPI
    under (``research/tic-spike/tic_heads.py`` writes the heads), by the graph's organizations' NPIs over all
    quarters. Returns the number of EINs."""
    files = sorted(Path(heads).glob("*/*.parquet"))
    if not files:
        raise UserError(f"{heads} holds no Transparency in Coverage heads (<payer>/<file>.parquet)", exit_code=2)
    con.execute(f"""CREATE OR REPLACE TEMP TABLE oc_ein_pac AS
        SELECT DISTINCT t.tin AS ein, o.pac_id
        FROM read_parquet({[str(f) for f in files]}) t
        JOIN (SELECT pac_id, unnest(npis) AS npi FROM organization) o ON o.npi = t.npi::VARCHAR
        WHERE t.tin_type = 'ein' AND regexp_matches(t.tin, '^[0-9]{{9}}$') AND t.tin NOT IN ('000000000', '999999999')""")
    return con.execute("SELECT count(DISTINCT ein) FROM oc_ein_pac").fetchone()[0]


# ------------------------------------------------------------------------------------------- evidence

def evidence(con: duckdb.DuckDBPyConnection) -> None:
    """``oc_pair`` (x, y, kinds, kind1, size, coh, name, cell): every pair of organizations sharing a key of ``oc_key``
    with at most MAX_KEY organizations, or in ``oc_pair_extra`` (when it exists), once, with the set of kinds it
    shares (``kinds``, '+'-joined in kind order), the strongest kind (``kind1``: the key shared by the fewest
    organizations, an extra pair's rank counting as that few; ties to the kind then the key), that key's size bin
    and cohesion, the shared-name bin and ``cell`` = size/coh/name."""
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
    # every shared key of a pair, then the strongest key of each kind and of the pair: the size bins are labels, so
    # the strongest key is found by its count n, never by comparing labels
    con.execute(f"""CREATE OR REPLACE TEMP TABLE oc_pair0 AS
        SELECT x, y, kind, n, size, coh FROM (
            SELECT a.pac_id AS x, b.pac_id AS y, kind, n.n, n.size, coalesce(c.coh, '-') AS coh,
                   row_number() OVER (PARTITION BY a.pac_id, b.pac_id, kind ORDER BY n.n, key) AS r
            FROM oc_key a JOIN oc_key b USING (kind, key) JOIN oc_key_n n USING (kind, key) LEFT JOIN oc_key_coh c USING (kind, key)
            WHERE a.pac_id < b.pac_id AND n.n <= {MAX_KEY}) WHERE r = 1""")
    con.execute("CREATE TEMP TABLE IF NOT EXISTS oc_pair_extra (x VARCHAR, y VARCHAR, kind VARCHAR, rank INTEGER, size VARCHAR, coh VARCHAR)")
    con.execute("INSERT INTO oc_pair0 SELECT x, y, kind, rank, size, coh FROM oc_pair_extra WHERE x < y")
    stop = ", ".join(f"'{w}'" for w in STOP_WORDS)
    con.execute(f"""CREATE OR REPLACE TEMP TABLE oc_tok AS
        SELECT * FROM (SELECT DISTINCT pac_id, unnest(string_split({_NORM.format(c='org_name')}, ' ')) AS tok FROM oc_org)
        WHERE tok NOT IN ({stop}) AND length(tok) >= 2""")
    con.execute("""CREATE OR REPLACE TEMP TABLE oc_names AS
        WITH df AS (SELECT tok, count(*) AS df FROM oc_tok GROUP BY 1), p AS (SELECT DISTINCT x, y FROM oc_pair0)
        SELECT p.x, p.y, min(df.df) AS mindf FROM p JOIN oc_tok t1 ON t1.pac_id = p.x
        JOIN oc_tok t2 ON t2.pac_id = p.y AND t2.tok = t1.tok JOIN df ON df.tok = t1.tok GROUP BY 1, 2""")
    con.execute(f"""CREATE OR REPLACE TEMP TABLE oc_pair AS
        WITH s AS (SELECT x, y, string_agg(kind, '+' ORDER BY kind) AS kinds, arg_min(kind, (n, kind)) AS kind1,
                          arg_min(size, (n, kind)) AS size, arg_min(coh, (n, kind)) AS coh
                   FROM oc_pair0 GROUP BY 1, 2)
        SELECT s.x, s.y, s.kinds, s.kind1, s.size, s.coh, {NAME_BINS} AS name,
               s.size || '/' || s.coh || '/' || {NAME_BINS} AS cell
        FROM s LEFT JOIN oc_names USING (x, y)""")


def calibrate(con: duckdb.DuckDBPyConnection, half: int | None = None, min_pairs: int = MIN_CELL_PAIRS) -> None:
    """``oc_calib`` (level, kinds, size, coh, name, pairs, p): among the quarter's pairs of organizations whose owner
    is known, the share under the same owner by cell at each of ``LEVELS`` (0: kinds x size x cohesion x name; 1:
    kinds x size; 2: the strongest kind x size), keeping cells of ``min_pairs`` pairs or more. The known owner is the
    hard unit (``oc_unit``: the Compendium system, else the All Owners firm), and a pair is known when both sides
    have one of the same source (two system members, or two firm members): a system member and a firm member are
    not compared, because a firm outside every system is as often a system's entity the Compendium does not list
    (a home health agency, a nursing home) as an independent one. With ``half`` (0 or 1), only owners whose id hashes
    to it, so the other half can be held out."""
    keep = "" if half is None else f"AND hash(lab) % 2 = {half}"
    con.execute(f"""CREATE OR REPLACE TEMP TABLE oc_truth AS
        SELECT CASE WHEN unit LIKE 'S%' THEN 'system' ELSE 'firm' END AS src, pac_id, unit AS lab FROM oc_unit
        WHERE unit NOT LIKE 'P%' {keep}""")
    con.execute("""CREATE OR REPLACE TEMP TABLE oc_known AS
        SELECT p.*, (t1.lab = t2.lab)::INT AS same FROM oc_pair p
        JOIN oc_truth t1 ON t1.pac_id = x JOIN oc_truth t2 ON t2.pac_id = y AND t2.src = t1.src""")
    parts = []
    for level, cols in enumerate(LEVELS):
        sel = ", ".join(f"{c} AS {'kinds' if c == 'kind1' else c}" if c in ("kinds", "kind1", "size") else c for c in cols)
        nulls = ", ".join(f"NULL::VARCHAR AS {c}" for c in ("coh", "name") if c not in cols)
        parts.append(f"SELECT {level} AS level, {sel}{', ' + nulls if nulls else ''}, count(*) AS pairs, avg(same) AS p "
                     f"FROM oc_known GROUP BY ALL HAVING count(*) >= {min_pairs}")
    con.execute("CREATE OR REPLACE TEMP TABLE oc_calib AS SELECT level, kinds, size, coh, name, pairs, p FROM ("
                + " UNION ALL BY NAME ".join(parts) + ")")


def weights_sql() -> str:
    """A SELECT of (x, y, w, level) over ``oc_pair`` priced by ``oc_calib``: each pair's probability from the finest
    level with a cell for it."""
    return """SELECT p.x, p.y, coalesce(c0.p, c1.p, c2.p) AS w, coalesce(c0.level, c1.level, c2.level) AS level
        FROM oc_pair p
        LEFT JOIN oc_calib c0 ON c0.level = 0 AND c0.kinds = p.kinds AND c0.size = p.size AND c0.coh = p.coh AND c0.name = p.name
        LEFT JOIN oc_calib c1 ON c1.level = 1 AND c1.kinds = p.kinds AND c1.size = p.size
        LEFT JOIN oc_calib c2 ON c2.level = 2 AND c2.kinds = p.kind1 AND c2.size = p.size
        WHERE coalesce(c0.p, c1.p, c2.p) IS NOT NULL"""


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
        FROM oc_org o
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


def cluster(con: duckdb.DuckDBPyConnection, gamma: float = GAMMA, history: float = HISTORY) -> None:
    """``comp`` (node, comp): each organization of ``oc_unit`` with the smallest PAC id of its community, by Leiden CPM
    over the hard units weighted by ``oc_w`` (``weights_sql``). Starts from ``prev_firm`` (node, firm_id: the previous
    quarter's communities) when it holds any of the units' organizations, and adds ``history`` to every pair of
    organizations it puts in one community: the history cost of evolutionary clustering (Chakrabarti, Kumar and
    Tomkins 2006), so a pair stays together while p > gamma - history but joins only at p > gamma, and a link
    wavering about gamma does not move an organization back and forth. Leiden runs LEIDEN_ITERATIONS passes: on a
    quarter of 300,000 organizations two passes place 99.8% of them as a run to convergence does, in a tenth of the
    time, and the next quarter starts from the result."""
    ig, la = _leiden()
    con.execute(f"CREATE OR REPLACE TEMP TABLE oc_w AS {weights_sql()}")
    units_ = con.execute("SELECT unit, count(*) FROM oc_unit GROUP BY 1 ORDER BY 1").fetchall()
    index = {u: i for i, (u, _) in enumerate(units_)}
    con.execute("CREATE TEMP TABLE IF NOT EXISTS prev_firm (node VARCHAR, firm_id VARCHAR)")
    edges = con.execute(f"""
        WITH prev AS (SELECT u.unit, p.firm_id, count(*) AS n FROM oc_unit u JOIN prev_firm p ON p.node = u.pac_id GROUP BY 1, 2)
        SELECT a, b, sum(w) FROM (
            SELECT least(u1.unit, u2.unit) AS a, greatest(u1.unit, u2.unit) AS b, w FROM oc_w
            JOIN oc_unit u1 ON u1.pac_id = x JOIN oc_unit u2 ON u2.pac_id = y WHERE u1.unit <> u2.unit
            UNION ALL
            SELECT p.unit, q.unit, {history} * p.n * q.n FROM prev p JOIN prev q ON q.firm_id = p.firm_id AND p.unit < q.unit
            WHERE {history} > 0)
        GROUP BY 1, 2 ORDER BY 1, 2""").fetchall()
    g = ig.Graph(n=len(units_), edges=[(index[a], index[b]) for a, b, _ in edges], edge_attrs={"weight": [w for *_, w in edges]})
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
    opt.optimise_partition(part, n_iterations=LEIDEN_ITERATIONS)
    con.execute("CREATE OR REPLACE TEMP TABLE oc_unit_comm AS SELECT unnest(?::VARCHAR[]) AS unit, unnest(?::INTEGER[]) AS community",
                [[u for u, _ in units_], list(part.membership)])
    con.execute("""CREATE OR REPLACE TEMP TABLE comp AS
        SELECT u.pac_id AS node, min(u.pac_id) OVER (PARTITION BY c.community) AS comp FROM oc_unit u JOIN oc_unit_comm c USING (unit)""")


def label(con: duckdb.DuckDBPyConnection, d: str, gamma: float = GAMMA) -> None:
    """The quarter's ``owner_community_member``, ``owner_community``, ``owner_community_support`` and
    ``owner_community_event`` temp tables from ``comp``, with ids carried over from the previous quarter
    (``firms.label_firms``, prefix O). A community is named for its owner: the Compendium system (``oc_sys_name``)
    holding most of its organizations, else the All Owners firm (``oc_firm_name``) holding most, else its organization
    with the most facilities, then physicians; ties go to the smallest id. Events come from ``oc_prev_member``, the
    previous quarter's members (empty on the first)."""
    con.execute("CREATE TEMP TABLE IF NOT EXISTS oc_prev_member (pac_id VARCHAR, community_id VARCHAR)")
    firms.label_firms(con, d, prefix="O")
    con.execute("CREATE TEMP TABLE IF NOT EXISTS oc_sys_name (sys VARCHAR, name VARCHAR)")
    con.execute("CREATE TEMP TABLE IF NOT EXISTS oc_firm_name (firm_id VARCHAR, name VARCHAR)")
    con.execute("CREATE TEMP TABLE IF NOT EXISTS oc_w (x VARCHAR, y VARCHAR, w DOUBLE, level INTEGER)")
    con.execute("""CREATE OR REPLACE TEMP TABLE owner_community_member AS
        WITH m AS (SELECT l.node AS pac_id, l.firm_id AS community_id, u.basis, count(*) OVER (PARTITION BY l.firm_id) AS n
                   FROM firm_label l JOIN oc_unit u ON u.pac_id = l.node)
        SELECT pac_id, community_id, coalesce(basis, CASE WHEN n > 1 THEN 'nppes' ELSE 'alone' END) AS basis FROM m""")
    con.execute("""CREATE OR REPLACE TEMP TABLE owner_community AS
        WITH mu AS (SELECT m.community_id, u.unit, f.firm_id FROM owner_community_member m JOIN oc_unit u USING (pac_id)
                    LEFT JOIN oc_firm f USING (pac_id)),
        sn AS (SELECT c.community_id, arg_min(n.name, (-c.k, c.sys)) AS name FROM (
                   SELECT community_id, substr(unit, 2) AS sys, count(*) AS k FROM mu WHERE unit LIKE 'S%' GROUP BY 1, 2) c
               JOIN oc_sys_name n USING (sys) GROUP BY 1),
        fn AS (SELECT c.community_id, arg_min(n.name, (-c.k, c.firm_id)) AS name FROM (
                   SELECT community_id, firm_id, count(*) AS k FROM mu WHERE firm_id IS NOT NULL GROUP BY 1, 2) c
               JOIN oc_firm_name n USING (firm_id) GROUP BY 1),
        c AS (SELECT m.community_id,
                     arg_min(o.org_name, (-(s.n_hospitals + s.n_post_acute), -s.physicians, m.pac_id)) AS org_name,
                     count(*) AS n_organizations, sum(s.n_hospitals)::BIGINT AS n_hospitals,
                     sum(s.n_post_acute)::BIGINT AS n_post_acute, round(sum(s.physicians), 1) AS physicians
              FROM owner_community_member m LEFT JOIN oc_org o USING (pac_id) LEFT JOIN oc_size s USING (pac_id) GROUP BY 1)
        SELECT c.community_id, coalesce(sn.name, fn.name, c.org_name) AS name,
               c.n_organizations, c.n_hospitals, c.n_post_acute, c.physicians
        FROM c LEFT JOIN sn USING (community_id) LEFT JOIN fn USING (community_id)""")
    con.execute(f"""CREATE OR REPLACE TEMP TABLE owner_community_support AS
        WITH m AS (SELECT pac_id, community_id, count(*) OVER (PARTITION BY community_id) AS n FROM owner_community_member),
        w AS (SELECT x AS pac_id, y AS other, w FROM oc_w UNION ALL SELECT y, x, w FROM oc_w),
        s AS (SELECT m.pac_id, sum(w.w) AS support FROM m JOIN w USING (pac_id) JOIN m o ON o.pac_id = w.other AND o.community_id = m.community_id
              GROUP BY 1)
        SELECT DATE '{d}' AS as_of_date, m.pac_id, m.community_id, round(coalesce(s.support, 0), 3)::DOUBLE AS support,
               round(coalesce(s.support, 0) - {gamma} * (m.n - 1), 3)::DOUBLE AS margin
        FROM m LEFT JOIN s USING (pac_id) WHERE m.n > 1""")
    con.execute(f"""CREATE OR REPLACE TEMP TABLE owner_community_event AS
        WITH flow AS (SELECT p.community_id AS from_community_id, c.community_id AS to_community_id, count(*) AS n
                      FROM oc_prev_member p FULL JOIN owner_community_member c USING (pac_id)
                      WHERE p.community_id IS DISTINCT FROM c.community_id GROUP BY 1, 2),
        survives AS (SELECT DISTINCT community_id FROM owner_community_member),
        existed AS (SELECT DISTINCT community_id FROM oc_prev_member)
        SELECT DATE '{d}' AS as_of_date, from_community_id, to_community_id, n AS n_organizations,
               CASE WHEN from_community_id IS NULL THEN 'entry'
                    WHEN to_community_id IS NULL THEN 'exit'
                    WHEN from_community_id NOT IN (SELECT community_id FROM survives) THEN 'absorbed'
                    WHEN to_community_id NOT IN (SELECT community_id FROM existed) THEN 'split'
                    ELSE 'move' END AS event
        FROM flow""")
    con.execute("CREATE OR REPLACE TEMP TABLE oc_prev_member AS SELECT pac_id, community_id FROM owner_community_member")


# ------------------------------------------------------------------------------------------- the build

class OwnerCommunityBuilder:
    """Reads a built graph directory (with firms and Compendium systems) and the warehouse's NPPES ``provider``; adds
    the owner community tables to the graph directory."""

    NEED = ("organization", "firm_member", "firm", "system", "system_link", "group_size", "hospital_link", "post_acute_link")

    def __init__(self, graph: Path, warehouse: Path, *, tic: Path | None = None, threads: int | None = None,
                 memory_limit: str | None = None, work_dir: Path | None = None):
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
        with open(self.graph / "quarters.csv") as f:
            self.dates = [r["as_of_date"] for r in csv.DictReader(f)]
        self.con.execute("CREATE TEMP TABLE oc_quarter AS SELECT unnest(?::DATE[]) AS d", [self.dates])
        self.holes: dict[str, int] = {}
        for t in self.NEED:
            src = f"'{self.graph / t}.parquet'"
            if t in HOLES:
                self.con.execute(f"CREATE TEMP TABLE {t} AS {filled(src, *HOLES[t])}")
                self.holes[t] = self.con.execute(f"SELECT count(*) FROM {t} WHERE _hole").fetchone()[0]
                self.con.execute(f"ALTER TABLE {t} DROP COLUMN _hole")
            else:
                self.con.execute(f"CREATE TEMP VIEW {t} AS SELECT * FROM {src}")
        started = time.time()
        provider_keys(self.con)
        log.info("owner communities: %d NPPES organization versions keyed (%.0fs)",
                 self.con.execute("SELECT count(*) FROM oc_provider").fetchone()[0], time.time() - started)
        # evidence from the other sources the warehouse holds; the IRS needs the EIN roster
        have = {r[0] for r in self.con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
        self.evidence_from = tuple(s for s, t in SOURCE_TABLES.items() if t in have and (s != "irs" or tic))
        self.tic = Path(tic) if tic else None
        if "irs" in self.evidence_from:
            log.info("owner communities: %d EINs on the Transparency in Coverage roster", tic_roster(self.con, self.tic))
        elif tic and "irs_related" not in have:
            log.warning("owner communities: --tic given but the irs source is not loaded; no IRS evidence")
        log.info("owner communities: evidence from NPPES%s", "".join(f", {s}" for s in self.evidence_from))
        self.ahrq_years = [r[0] for r in self.con.execute(
            "SELECT DISTINCT year FROM system_link WHERE node_type = 'organization' ORDER BY 1").fetchall()]
        self.firms_from = self.con.execute("SELECT min(effective_date)::VARCHAR FROM firm_member").fetchone()[0]
        self.stats: list[dict] = []
        self.inputs_for: str | None = None      # the quarter whose inputs and evidence are in place

    def ahrq_year(self, d: str) -> int | None:
        if not self.ahrq_years:
            return None
        prior = [y for y in self.ahrq_years if y <= int(d[:4])]
        return prior[-1] if prior else self.ahrq_years[0]

    def inputs(self, d: str) -> None:
        """The quarter's inputs and evidence, built once per quarter."""
        if self.inputs_for != d:
            quarter_inputs(self.con, d, self.ahrq_year(d), self.evidence_from)
            evidence(self.con)
            self.inputs_for = d

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
        self.inputs(anchor)
        units(self.con)
        calibrate(self.con, min_pairs=min_pairs)
        self.con.execute("CREATE OR REPLACE TEMP TABLE oc_calib_fixed AS SELECT * FROM oc_calib")
        log.info("owner communities: calibrated %d cells on %s (%.0fs)",
                 self.con.execute("SELECT count(*) FROM oc_calib").fetchone()[0], anchor, time.time() - started)
        self.anchor_date = anchor
        return anchor

    def build(self, limit: int | None = None) -> list[dict]:
        con = self.con
        for t in (*TABLES, *QUARTER_TABLES):
            con.execute(f"DROP TABLE IF EXISTS {t.name}_open; DROP TABLE IF EXISTS {t.name}_closed; DROP TABLE IF EXISTS {t.name}_all")
        con.execute("DROP TABLE IF EXISTS oc_prev_member")
        prev: dict[str, str] = {}
        for d in self.dates[:limit]:
            started = time.time()
            year = self.ahrq_year(d)
            self.inputs(d)
            con.execute("CREATE OR REPLACE TEMP TABLE oc_calib AS SELECT * FROM oc_calib_fixed")
            units(con)
            cluster(con)
            label(con, d)
            for t in TABLES:
                advance(con, t, d)
            for t in QUARTER_TABLES:
                con.execute(f"CREATE TABLE IF NOT EXISTS {t.name}_all AS SELECT * FROM {t.name} LIMIT 0")
                con.execute(f"INSERT INTO {t.name}_all SELECT * FROM {t.name}")
            cur = dict(con.execute("SELECT pac_id, community_id FROM owner_community_member").fetchall())
            both = [p for p in cur if p in prev]
            row = con.execute("""SELECT count(*) FILTER (n_organizations > 1), coalesce(sum(n_organizations) FILTER (n_organizations > 1), 0),
                                        coalesce(sum(physicians) FILTER (n_organizations > 1), 0), coalesce(sum(physicians), 0)
                                 FROM owner_community""").fetchone()
            basis = dict(con.execute("SELECT basis, count(*) FROM owner_community_member GROUP BY 1").fetchall())
            keys = dict(con.execute("SELECT kind, count(DISTINCT pac_id) FROM oc_key GROUP BY 1").fetchall())
            keys["irs"] = con.execute("SELECT count(DISTINCT x) + count(DISTINCT y) FROM oc_pair_extra").fetchone()[0]
            events = dict(con.execute("SELECT event, sum(n_organizations) FROM owner_community_event GROUP BY 1").fetchall())
            self.stats.append({
                "as_of_date": d, "ahrq_year": year, "all_owners": bool(self.firms_from and d >= self.firms_from),
                "n_communities_multi": row[0], "n_organizations_multi": row[1],
                "physicians_multi": round(row[2], 1), "physicians": round(row[3], 1),
                **{f"n_{b}": basis.get(b, 0) for b in ("all_owners", "ahrq", "nppes", "alone")},
                "n_staffing": con.execute("SELECT count(*) FROM oc_staffing").fetchone()[0],
                # the share of organizations present in both quarters whose community id changed
                "churn": round(sum(cur[p] != prev[p] for p in both) / len(both), 4) if both else None,
                **{f"n_{e}": int(events.get(e, 0)) for e in ("absorbed", "split", "move", "entry", "exit")},
                **{f"keyed_{k}": keys.get(k, 0) for k in ("ao", "mail", "phone", "parent", "chain", "nhc", "irs")},
            })
            prev = cur
            log.info("owner communities %s: %s (%.0fs)", d, self.stats[-1], time.time() - started)
        return self.stats

    def write(self) -> dict:
        if not self.stats:
            raise UserError("owner communities: no quarter was built, nothing to write", exit_code=2)
        out = self.graph
        tables = {}
        for t in TABLES:
            path = out / f"{t.name}.parquet"
            self.con.execute(f"COPY ({intervals_sql(t)} ORDER BY {', '.join(t.key)}, effective_date) "
                             f"TO '{path}' (FORMAT parquet, COMPRESSION zstd)")
            n = self.con.execute(f"SELECT count(*) FROM '{path}'").fetchone()[0]
            tables[t.name] = {"rows": n, "key": list(t.key), "description": t.description}
        for t in QUARTER_TABLES:
            path = out / f"{t.name}.parquet"
            self.con.execute(f"COPY (SELECT * FROM {t.name}_all ORDER BY {', '.join(t.key)}) TO '{path}' (FORMAT parquet, COMPRESSION zstd)")
            n = self.con.execute(f"SELECT count(*) FROM '{path}'").fetchone()[0]
            tables[t.name] = {"rows": n, "key": list(t.key), "description": t.description, "quarterly": True}
        self.con.execute(f"COPY (SELECT * FROM oc_calib_fixed ORDER BY level, kinds, size, coh, name) "
                         f"TO '{out / 'owner_link_calibration.csv'}' (HEADER)")
        with open(out / "owner_communities.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(self.stats[0]))
            w.writeheader()
            w.writerows(self.stats)
        m = self.manifest
        m["tables"].update(tables)
        m["owner_communities"] = {
            "generator": f"healthcare-ledger {__version__} owner-communities", "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "anchor": self.anchor_date, "quarters": len(self.stats), "gamma": GAMMA, "history": HISTORY, "max_key": MAX_KEY,
            "min_cell_pairs": MIN_CELL_PAIRS, "levels": [list(c) for c in LEVELS],
            "max_hole": MAX_HOLE, "holes_filled": self.holes,
            "evidence_from": ["nppes", *self.evidence_from], "tic": str(self.tic) if self.tic else None,
            "staffing_min_systems": STAFFING_MIN_SYSTEMS, "ahrq_years": self.ahrq_years, "all_owners_from": self.firms_from,
        }
        (out / "manifest.json").write_text(json.dumps(m, indent=2) + "\n")
        return m

    def close(self) -> None:
        self.con.close()


def build_owner_communities(graph: Path, warehouse: Path, *, anchor: str | None = None, limit: int | None = None,
                            min_pairs: int = MIN_CELL_PAIRS, tic: Path | None = None, **kw) -> dict:
    b = OwnerCommunityBuilder(graph, warehouse, tic=tic, **kw)
    try:
        b.calibrate(anchor, min_pairs=min_pairs)
        b.build(limit)
        return b.write()
    finally:
        b.close()
