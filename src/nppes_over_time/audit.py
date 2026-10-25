"""Independent privacy scan of the files an export wrote.

``scrub.py`` removes tax ids and SSNs before anything is hashed and ``privacy_problems`` re-checks
the warehouse. This module checks a third time, on the published Parquet files themselves, so a
bug in the export SQL (a column re-joined from somewhere it should not come from, a suppressed
column exported by name) cannot leak what the warehouse does not hold.

Every text column of every table under ``parquet/`` is scanned for tax-id tokens (``scrub.TOKEN_RE``:
a formatted SSN, a formatted EIN or nine bare digits, bounded by non-digits). Columns that hold
numbers by design are *exempt* and only counted: postal codes (nine-digit ZIP+4), telephone and fax
numbers, code columns (a taxonomy code such as ``390200000X`` holds nine digits) and the hash and id
columns. Credentials and name columns may hold no nine digits, however separated (rules 3 and 4), and
``license_number`` is held to rule 2 of scrub.py: every hit must
be a known license format of its state, and no bare nine may appear under two states in one version.
Any other hit fails the audit. The result is written as ``privacy_audit.csv``, one row per
(table, column) with counts only, never a value.
"""

from __future__ import annotations

import csv
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import duckdb

from .scrub import BARE_NINE_RE, CREDENTIAL_COLUMNS, NINE_RUN_RE, license_bad, token_re

log = logging.getLogger(__name__)

AUDIT_FILE = "privacy_audit.csv"
EXEMPT_RE = re.compile(r"(^|_)(postal_code|zip|phone|phone_ext|fax)$|^(version_hash|npi|enrollment_id|pac_id|receiving_enrollment_id)$"
                       # code columns: a taxonomy code such as 390200000X holds nine digits by design
                       r"|(^|_)(code|state|country|type|group|flag|sex)$|^primary_taxonomy$")
SSN_FMT = r"(^|[^0-9])[0-9]{3}[ .-][0-9]{2}[ .-][0-9]{4}([^0-9]|$)"
EIN_FMT = r"(^|[^0-9])[0-9]{2}-[0-9]{7}([^0-9]|$)"
NINE = r"(^|[^0-9])[0-9]{9}([^0-9]|$)"


@dataclass
class AuditRow:
    table: str
    column: str
    rows: int
    ssn_formatted: int
    ein_formatted: int
    nine_digit: int
    violations: int
    status: str     # ok | exempt | FAIL


def _sources(out: Path) -> dict[str, str]:
    """table name -> read_parquet() expression for every table in ``out/parquet`` (monthly files excluded)."""
    found = {}
    root = out / "parquet"
    for p in sorted(root.iterdir()) if root.exists() else []:
        if p.name == "provider_monthly":
            continue
        if p.is_dir():
            if not any(p.rglob("*.parquet")):      # an empty partitioned table writes no file
                continue
            found[p.name] =f"read_parquet('{p}/**/*.parquet', hive_partitioning = true)"
        elif p.suffix == ".parquet":
            found[p.stem] = f"read_parquet('{p}')"
    return found


def _lit(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _license_violations(con: duckdb.DuckDBPyConnection, src: str) -> int:
    """Rows of a long taxonomy table that break rule 2: a value scrub.license_bad rejects, or one bare
    nine under two states in the same version."""
    n_bad = con.execute(f"SELECT count(*) FROM {src} WHERE {license_bad('license_number', 'license_state', 'false')}").fetchone()[0]
    digits = "regexp_replace(license_number, '[^0-9]', '', 'g')"
    bare = f"(regexp_matches(license_number, {_lit(BARE_NINE_RE)}) AND length({digits}) = 9)"
    n_cross = con.execute(f"""SELECT count(*) FROM (
            SELECT npi, effective_date, {digits} AS d FROM {src} WHERE {bare}
            GROUP BY ALL HAVING count(DISTINCT coalesce(license_state, '')) > 1)""").fetchone()[0]
    return n_bad + n_cross


def audit(out: Path) -> list[AuditRow]:
    """Scan every exported Parquet table under ``out``; returns one row per scanned text column."""
    con = duckdb.connect()
    rows: list[AuditRow] = []
    try:
        for table, src in _sources(Path(out)).items():
            cols = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {src}").fetchall() if r[1] == "VARCHAR"]
            if not cols:
                continue
            parts = ["count(*)"]
            for c in cols:
                parts += [f"count(*) FILTER (WHERE regexp_matches({c}, {_lit(p)}))" for p in (SSN_FMT, EIN_FMT, NINE)]
                # credentials (rule 3) and names (rule 4) hold no nine digits, however separated
                hit = NINE_RUN_RE if c in CREDENTIAL_COLUMNS else token_re(c)
                parts.append(f"count(*) FILTER (WHERE regexp_matches({c}, {_lit(hit)}))")
            log.info("auditing %s (%d text columns)", table, len(cols))
            res = con.execute(f"SELECT {', '.join(parts)} FROM {src}").fetchone()
            n = res[0]
            for i, c in enumerate(cols):
                ssn, ein, nine, any_token = res[1 + 4 * i: 5 + 4 * i]
                if EXEMPT_RE.search(c):
                    rows.append(AuditRow(table, c, n, ssn, ein, nine, 0, "exempt"))
                    continue
                violations = _license_violations(con, src) if c == "license_number" else any_token
                rows.append(AuditRow(table, c, n, ssn, ein, nine, violations, "FAIL" if violations else "ok"))
    finally:
        con.close()
    return rows


def write_audit(path: Path, rows: list[AuditRow]) -> None:
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["table", "column", "rows", "ssn_formatted", "ein_formatted", "nine_digit", "violations", "status"])
        w.writerows([r.table, r.column, r.rows, r.ssn_formatted, r.ein_formatted, r.nine_digit, r.violations, r.status]
                    for r in rows)


def failures(rows: list[AuditRow]) -> list[str]:
    return [f"privacy audit: {r.table}.{r.column}: {r.violations} row(s) hold a possible SSN or tax id"
            for r in rows if r.status == "FAIL"]
