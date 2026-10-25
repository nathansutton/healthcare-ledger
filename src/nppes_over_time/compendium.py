"""The AHRQ Compendium of U.S. Health Systems: download its files and turn them into the graph's system tables.

AHRQ publishes, for 2016, 2018 and 2020-2023, a list of health systems (each with at least one general acute
hospital and a group of physicians under common ownership or management) and linkage files that place
hospitals, nursing homes and home health agencies (by CCN) and physician group practices (by PECOS associate
id) in those systems. docs/METHODOLOGY.md section 8 has the rules.

``download`` fetches every file AHRQ has published under its compendium directory. The file list is the
Internet Archive's index of that directory plus ``KNOWN_FILES``; each file comes from ahrq.gov first and from
the archive's raw capture when ahrq.gov answers with an empty 202, which it does to scripts intermittently.

``system_tables`` reads the latest revision of each year's CSVs (``FILES``) into two annual tables:

``system``
    one row per Compendium system and year.
``system_link``
    one row per linked entity and year: an organization (PECOS associate id, from the group practice file),
    a hospital, nursing home or home health agency (CCN), with its system id, or NULL when AHRQ lists the
    entity as not in a system.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import logging
import time
import urllib.request
import zipfile
from pathlib import Path

import duckdb

log = logging.getLogger(__name__)

BASE = "https://www.ahrq.gov/sites/default/files/wysiwyg/chsp/compendium/"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"}

# the latest revision of each file type and year; zips are unpacked into raw/<zip stem>/
FILES = {
    "system": {
        2016: "2016-compendium-files-2020update/chsp-compendium-2019update.csv",
        2018: "chsp-compendium-2018-updated-2021.csv",
        2020: "chsp-compendium-2020-rev.csv",
        2021: "chsp-compendium-2021-rev.csv",
        2022: "chsp-compendium-2022-rev.csv",
        2023: "chsp-compendium-2023-rev.csv",
    },
    "hospital": {
        2016: "2016-compendium-files-2020update/compendium_system_hospital_linkage_file.csv",
        2018: "2018-compendium-files-2021update/chsp-hospital-linkage-2018.csv",
        2020: "chsp-hospital-linkage-2020-rev.csv",
        2021: "chsp-hospital-linkage-2021-rev.csv",
        2022: "chsp-hospital-linkage-2022-rev.csv",
        2023: "chsp-hospital-linkage-2023.csv",
    },
    "group_practice": {y: f"{y}-group-practice-linkage-public-file.csv" for y in (2016, 2018, 2020, 2021, 2022)},
    "nursing_home": {y: f"chsp-nh-linkage-{y}.csv" for y in (2020, 2021, 2022, 2023)},
    "home_health": {y: f"chsp-hhco-linkage-{y}.csv" for y in (2020, 2021, 2022, 2023)},
}
# files the Internet Archive has not captured, which download fetches by name
KNOWN_FILES = (
    "2016-compendium-files-2020update.zip", "2018-compendium-files-2021update.zip",
    "chsp-compendium-2023-rev.csv", "chsp-compendium-2023-rev.xlsx",
    "chsp-nh-linkage-2023.csv", "chsp-hhco-linkage-2023.csv", "chsp-hospital-linkage-2023.csv",
    "chsp-os-linkage-2023.xlsx",
)

SYSTEM_COLUMNS = ("health_sys_id", "health_sys_name", "health_sys_city", "health_sys_state", "total_mds",
                  "prim_care_mds", "grp_cnt", "hosp_cnt", "acutehosp_cnt", "sys_multistate", "sys_beds")
# file type -> (node type, AHRQ's row id, node id column, entity name column)
LINKS = {
    "hospital": ("hospital", "compendium_hospital_id", "ccn", "hospital_name"),
    "nursing_home": ("nursing_home", "compendium_nh_id", "ccn", "nursing_home_name"),
    "home_health": ("home_health", "compendium_hh_id", "ccn", "home_health_care_org_name"),
}


def _get(url: str, timeout: int = 300) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
            return r.status, r.read()
    except Exception as e:  # noqa: BLE001 - any failure falls through to the next source
        return getattr(e, "code", 0) or 0, b""


def _archive_index() -> dict[str, str]:
    """file name -> timestamp of its latest successful capture"""
    _, body = _get("https://web.archive.org/cdx/search/cdx?url=ahrq.gov/sites/default/files/wysiwyg/chsp/compendium/*"
                   "&output=json&fl=original,timestamp&filter=statuscode:200")
    latest: dict[str, str] = {}
    for original, ts in (json.loads(body)[1:] if body else []):
        name = original.split("?")[0].rsplit("/", 1)[-1]
        if name and ts > latest.get(name, ""):
            latest[name] = ts
    return latest


def download(dest: Path, *, pause: float = 1.0) -> list[dict]:
    """Fetch every compendium file into dest/raw (keeping files already there) and write dest/manifest.csv."""
    raw = dest / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    index = _archive_index()
    rows = []
    for name in sorted(set(index) | set(KNOWN_FILES)):
        path = raw / name
        if path.exists() and path.stat().st_size:
            source = "existing"
        else:
            status, body = _get(BASE + name)
            source = f"ahrq.gov ({status})"
            if status != 200 or not body:
                time.sleep(pause)
                status, body = _get(f"https://web.archive.org/web/{index.get(name, '2026')}id_/{BASE}{name}")
                source = f"web.archive.org {index.get(name, 'latest')} ({status})"
            if status != 200 or not body:
                log.warning("compendium %s: not found (%s)", name, source)
                rows.append({"name": name, "source": source, "bytes": 0, "sha256": ""})
                continue
            if body[:2] == b"\x1f\x8b" and not name.endswith((".gz", ".zip")):
                body = gzip.decompress(body)  # some archive captures keep the server's gzip transfer encoding
            path.write_bytes(body)
            time.sleep(pause)
        if name.endswith(".zip"):
            with zipfile.ZipFile(path) as z:
                z.extractall(raw / path.stem)
        data = path.read_bytes()
        rows.append({"name": name, "source": source, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        log.info("compendium %s: %s bytes, %s", name, f"{len(data):,}", source)
    with open(dest / "manifest.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["name", "source", "bytes", "sha256"])
        w.writeheader()
        w.writerows(rows)
    return rows


def _normalized(path: Path, out: Path) -> Path:
    """AHRQ's CSVs mix UTF-8 and Latin-1 and some end lines with a bare CR; rewrite one as UTF-8 with LF."""
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    rows = list(csv.reader(io.StringIO(text.replace("\r\n", "\n").replace("\r", "\n"))))
    header = [h.strip().lower() for h in rows[0]]
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(r for r in rows[1:] if any(c.strip() for c in r))
    return out


def require_files(compendium: Path) -> None:
    from .errors import UserError

    missing = [n for files in FILES.values() for n in files.values() if not (compendium / "raw" / n).is_file()]
    if missing:
        raise UserError(f"the compendium directory {compendium} lacks {len(missing)} file(s), e.g. raw/{missing[0]}; "
                        f"fetch them with `nppes-over-time compendium-download --dest {compendium}`", exit_code=2)


def system_tables(con: duckdb.DuckDBPyConnection, compendium: Path, work: Path) -> dict[str, list[int]]:
    """Create the system and system_link tables in con from compendium/raw. Returns the years each file type covers."""
    require_files(compendium)
    raw = compendium / "raw"
    work.mkdir(parents=True, exist_ok=True)
    for kind, files in FILES.items():
        paths = [str(_normalized(raw / name, work / f"compendium_{kind}_{year}.csv")) for year, name in files.items()]
        con.execute(f"""
            CREATE OR REPLACE TEMP TABLE ahrq_{kind} AS
            SELECT regexp_extract(filename, '_(\\d{{4}})\\.csv$', 1)::INTEGER AS year, * EXCLUDE (filename)
            FROM read_csv({paths}, all_varchar = true, header = true, union_by_name = true, filename = true)""")
    num = "TRY_CAST(nullif(regexp_replace({c}, '[^0-9.-]', '', 'g'), '') AS {t}) AS {c}"
    con.execute(f"""
        CREATE OR REPLACE TABLE system AS
        SELECT year, health_sys_id, health_sys_name, health_sys_city, health_sys_state,
               {", ".join(num.format(c=c, t="DOUBLE" if c == "sys_beds" else "INTEGER") for c in SYSTEM_COLUMNS[4:])}
        FROM ahrq_system WHERE health_sys_id IS NOT NULL ORDER BY year, health_sys_id""")
    facility = " UNION ALL ".join(f"""
        SELECT year, '{node}' AS node_type, {node_id} AS node_id, {row_id} AS compendium_id, {name} AS name,
               health_sys_id, NULL::INTEGER AS md_ppas_physicians
        FROM ahrq_{kind} WHERE {node_id} IS NOT NULL""" for kind, (node, row_id, node_id, name) in LINKS.items())
    con.execute(f"""
        CREATE OR REPLACE TABLE system_link AS
        WITH g AS (SELECT *, 'GP' || year || '-' || row_number() OVER (PARTITION BY year ORDER BY tin_name, pecos_pac_ids)
                             AS compendium_id
                   FROM ahrq_group_practice)
        SELECT year, 'organization' AS node_type, unnest(string_split(pecos_pac_ids, '^')) AS node_id, compendium_id,
               tin_name AS name, health_sys_id, TRY_CAST(md_do_md_ppas AS INTEGER) AS md_ppas_physicians
        FROM g WHERE pecos_pac_ids IS NOT NULL
        UNION ALL {facility}""")
    con.execute("CREATE OR REPLACE TABLE system_link AS SELECT * FROM system_link ORDER BY year, node_type, node_id")
    return {kind: sorted(files) for kind, files in FILES.items()}
