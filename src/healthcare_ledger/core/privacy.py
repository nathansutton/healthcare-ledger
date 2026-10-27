"""Identifiers this project never publishes, whatever the CMS file contains.

CMS withholds the Employer Identification Number (EIN) and Social Security Number from the
dissemination file, but only from the fields designed for them. Providers also type tax ids and
SSNs into free-form fields: the "Other Provider Identifier" slots (issuer "TAX ID", "SSN", or a
payer that bills by TIN), the license-number slots, the credential, the parent organization's
name, and occasionally a name or address line. CMS masks a few of these itself and misses most;
the archived monthly files it served until October 2026 carried several hundred SSNs.

The rules fail closed: a value that *could* be a tax id or SSN is suppressed unless its shape is
a known, legitimate format. Every rule is applied to the staged rows *before* they are hashed,
so suppressed content never reaches the warehouse, never creates a version and never reaches an
export. Each source's ``check_problems`` re-checks a built warehouse and is run by ``validate`` and, as a hard
gate, by ``export``; ``audit`` checks the exported files a second time, independently.

A *tax-id token* (``TOKEN_RE``) is a run of digits, bounded by non-digits, that is a formatted SSN
(``NNN-NN-NNNN``, with any of space, dot or dash between the groups), a formatted EIN
(``NN-NNNNNNN``) or nine bare digits.

Rules (column names are the canonical short names in ``sources/nppes/schema.py``; the NPPES
provider rules are applied by ``sources/nppes/scrub.py``):

1. Suppressed columns are always NULL: ``ein`` and ``parent_org_tin`` (CMS prints ``<UNAVAIL>``
   there; no real value was ever published). The "Other Provider Identifier" columns
   (identifier, type, state and issuer, 50 slots) are not read at all (``sources.nppes.schema.OTHER_ID_HEADERS``):
   they are payer and Medicaid ids mixed with tax ids that cannot be told apart.
2. ``license_number_N`` is NULL when it contains a formatted SSN, is exactly a formatted EIN, or
   (a) contains any other tax-id token and its *format* is not a known license format of its state
   (``license_formats.csv``), or (b) it is a *bare nine* (nine digits, optionally with spaces, dots
   or dashes, whether or not they form a token) and the same nine digits appear in another license
   slot of the row under a different state (a license number belongs to one state; an SSN goes
   wherever the provider goes). The format
   of a bare nine is its first three digits followed by ``999999``; the format of any other value is
   the value in upper case with every digit replaced by ``9``.
3. ``credential``, ``other_credential`` and ``ao_credential`` are NULL when they contain nine
   digits, however separated (``NINE_RUN_RE``). A credential is letters ("MD", "LCSW"); a number
   in it is something else.
4. In every other free-text column of every stream (names, addresses, titles, endpoint text),
   each tax-id token is replaced by ``[REDACTED]``; in a name column (``*name``) so is any nine
   digits however separated (``NINE_RUN_RE``), since a name has no use for them. Address lines
   and endpoint text keep other groupings of nine digits (street number plus ZIP code, suite
   codes, dotted OIDs). Code columns, dates, postal codes, telephone
   and fax numbers and the NPI-valued columns are not free text and are left alone.

``license_formats.csv`` lists every (state, format) pair of a license number with a tax-id token
in it that at least 25 distinct NPIs used in the 2007 to 2026 files: Illinois's profession codes
(``036999999`` physician, ``041999999`` registered nurse), Oregon's ``999999999RN``, DC's
``MD999999999``, the national behavior technician number ``RBT-99-9999999`` under the states that
record it, and the like. A real license format is shared by many providers; a stray SSN is not. The
file is frozen per edition: a new format is a new edition (docs/METHODOLOGY.md section 2a).

The audit
---------
``audit`` is an independent privacy scan of the files an export wrote. The rules above remove tax
ids and SSNs before anything is hashed and ``check_problems`` re-checks the warehouse. The audit
checks a third time, on the published Parquet files themselves, so a
bug in the export SQL (a column re-joined from somewhere it should not come from, a suppressed
column exported by name) cannot leak what the warehouse does not hold.

Every text column of every table under ``parquet/`` is scanned for tax-id tokens (``TOKEN_RE``:
a formatted SSN, a formatted EIN or nine bare digits, bounded by non-digits). Columns that hold
numbers by design are *exempt* and only counted: postal codes (nine-digit ZIP+4), telephone and fax
numbers (the POS files' ``phne_num`` and ``fax_phne_num`` too), code columns (a taxonomy code such as ``390200000X`` holds nine digits) and the id
columns. Credentials and name columns may hold no nine digits, however separated (rules 3 and 4), and
``license_number`` is held to rule 2: every hit must
be a known license format of its state, and no bare nine may appear under two states in one version.
Any other hit fails the audit. The result is written as ``privacy_audit.csv``, one row per
(table, column) with counts only, never a value.
"""

from __future__ import annotations

import csv
import logging
import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import duckdb

from .columns import is_code_column

log = logging.getLogger(__name__)

SUPPRESSED_COLUMNS: tuple[str, ...] = ("ein", "parent_org_tin")

# an SSN grouping with any separators: 123-45-6789, 123 45 6789, 123.45.6789, 123-45 6789
SSN_RE = r"[0-9]{3}[ .-][0-9]{2}[ .-][0-9]{4}"
# a tax-id token, without its boundaries (RE2 syntax)
TOKEN_RE = rf"{SSN_RE}|[0-9]{{2}}-[0-9]{{7}}|[0-9]{{9}}"
# the token bounded by non-digits; groups 1 and 3 are the boundaries
BOUNDED_TOKEN_RE = rf"(^|[^0-9])({TOKEN_RE})([^0-9]|$)"
SSN_TOKEN_RE = rf"(^|[^0-9]){SSN_RE}([^0-9]|$)"
# nine digits with any single separators between them (123.456789, 1234-56789), bounded like a
# token (groups 1 and 3): the credential rule, and the token rule of name columns
NINE_RUN_RE = r"(^|[^0-9])([0-9](?:[ .-]?[0-9]){8})([^0-9]|$)"
EIN_EXACT_RE = r"^[0-9]{2}-[0-9]{7}$"
BARE_NINE_RE = r"^[0-9][0-9 .-]*[0-9]$"   # plus: exactly nine digits
REDACTED = "[REDACTED]"

LICENSE_COLUMNS = tuple(f"license_number_{i}" for i in range(1, 16))
CREDENTIAL_COLUMNS: tuple[str, ...] = ("credential", "other_credential", "ao_credential")
# text columns that hold numbers by design; the token rule does not apply to them
_NOT_FREE_TEXT_RE = re.compile(r"(^|_)(postal_code|zip|phone|phone_ext|fax|code|flag)$"
                               r"|^(npi|replacement_npi|pac_id|receiving_enrollment_id|state|country|sex)$")

FORMATS_FILE = Path(__file__).with_name("license_formats.csv")
LICENSE_FORMAT_MIN_NPIS = 25


@cache
def license_formats() -> tuple[str, ...]:
    """Known license formats as ``'ST:FORMAT'`` strings (``license_formats.csv``)."""
    with open(FORMATS_FILE, newline="") as f:
        return tuple(sorted(f"{r['state']}:{r['format']}" for r in csv.DictReader(f)))


def _lit(pattern: str) -> str:
    return "'" + pattern.replace("'", "''") + "'"


def free_text_columns(stream) -> list[str]:
    """Columns of ``stream`` (of any source) the token rule (4) applies to."""
    return [c.name for c in stream.columns
            if c.kind == "text" and not is_code_column(c.name) and not _NOT_FREE_TEXT_RE.search(c.name)
            and c.name not in SUPPRESSED_COLUMNS and c.name not in LICENSE_COLUMNS and c.name not in CREDENTIAL_COLUMNS]


def is_name_column(col: str) -> bool:
    return col.endswith("name")


def token_re(col: str | None = None) -> str:
    """The token pattern of rule 4 for column ``col``: in a name, nine digits however separated."""
    return NINE_RUN_RE if col is not None and is_name_column(col) else BOUNDED_TOKEN_RE


def redact_expr(expr: str, col: str | None = None) -> str:
    """``expr`` (the value of column ``col``) with every token replaced by [REDACTED]. Two passes: a
    match consumes the boundary character after it, so a token right behind another one is only
    found the second time."""
    once = lambda e: f"regexp_replace({e}, {_lit(token_re(col))}, '\\1{REDACTED}\\3', 'g')"  # noqa: E731
    return once(once(expr))


def has_token(expr: str, col: str | None = None) -> str:
    return f"regexp_matches({expr}, {_lit(token_re(col))})"


def credential_bad(col: str) -> str:
    """True when credential ``col`` must be NULL (rule 3): nine digits, however separated."""
    return f"regexp_matches({col}, {_lit(NINE_RUN_RE)})"


def _digits(col: str) -> str:
    return f"regexp_replace({col}, '[^0-9]', '', 'g')"


def _bare_nine(col: str) -> str:
    return f"(regexp_matches({col}, {_lit(BARE_NINE_RE)}) AND length({_digits(col)}) = 9)"


def license_format(col: str) -> str:
    """The format of a license value (rule 2): ``036999999`` for a bare nine, else digits masked to 9."""
    return (f"CASE WHEN {_bare_nine(col)} THEN substr({_digits(col)}, 1, 3) || '999999' "
            f"ELSE regexp_replace(upper({col}), '[0-9]', '9', 'g') END")


def license_bad(col: str, state: str, cross: str) -> str:
    """True when license value ``col`` (of state ``state``) must be NULL (rule 2); ``cross`` is the
    SQL for 2b, true when the same bare nine is in the row under another state."""
    lits = ", ".join(f"'{p}'" for p in license_formats())
    known = f"coalesce({state} || ':' || {license_format(col)} IN ({lits}), false)"
    return (f"(regexp_matches({col}, {_lit(SSN_TOKEN_RE)}) OR regexp_matches({col}, {_lit(EIN_EXACT_RE)}) "
            f"OR ({has_token(col)} AND NOT {known}) OR ({_bare_nine(col)} AND {cross}))")


# ---------------------------------------------------------------------- audit
AUDIT_FILE = "privacy_audit.csv"
EXEMPT_RE = re.compile(r"(^|_)(postal_code|zip|zip_cd|phone|phone_ext|fax|phne_num)$|^(npi|enrollment_id|pac_id|receiving_enrollment_id|ccn)$"
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


def _license_violations(con: duckdb.DuckDBPyConnection, src: str) -> int:
    """Rows of a long taxonomy table that break rule 2: a value license_bad rejects, or one bare
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
