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
export. ``privacy_problems`` re-checks a built warehouse and is run by ``validate`` and, as a hard
gate, by ``export``; ``audit.py`` checks the exported files a second time, independently.

A *tax-id token* (``TOKEN_RE``) is a run of digits, bounded by non-digits, that is a formatted SSN
(``NNN-NN-NNNN`` or ``NNN NN NNNN``), a formatted EIN (``NN-NNNNNNN``) or nine bare digits.

Rules (column names are the canonical short names in ``schema.py``):

1. Suppressed columns are always NULL: ``ein`` and ``parent_org_tin`` (CMS prints ``<UNAVAIL>``
   there; no real value was ever published) and every "Other Provider Identifier" column
   (identifier, type, state and issuer, 50 slots). The other identifiers are payer and Medicaid
   ids mixed with tax ids that cannot be told apart, so none is published.
2. ``license_number_N`` is NULL when it contains a formatted SSN, is exactly a formatted EIN, or
   contains any other tax-id token and (a) its *format* is not a known license format of its state
   (``license_formats.csv``), or (b) it is a *bare nine* (nine digits, optionally with spaces, dots
   or dashes) and the same nine digits appear in another license slot of the row under a different
   state (a license number belongs to one state; an SSN goes wherever the provider goes). The format
   of a bare nine is its first three digits followed by ``999999``; the format of any other value is
   the value in upper case with every digit replaced by ``9``.
3. ``credential``, ``other_credential`` and ``ao_credential`` are NULL when they contain a
   tax-id token. A credential is letters ("MD", "LCSW"); a number in it is something else.
4. In every other free-text column of every stream (names, addresses, titles, endpoint text),
   each tax-id token is replaced by ``[REDACTED]``. Code columns, dates, postal codes, telephone
   and fax numbers and the NPI-valued columns are not free text and are left alone.

``license_formats.csv`` lists every (state, format) pair of a license number with a tax-id token
in it that at least 25 distinct NPIs used in the 2007 to 2026 files: Illinois's profession codes
(``036999999`` physician, ``041999999`` registered nurse), Oregon's ``999999999RN``, DC's
``MD999999999``, the national behavior technician number ``RBT-99-9999999`` under the states that
record it, and the like. A real license format is shared by many providers; a stray SSN is not. The
file is frozen per edition: a new format is a new edition (docs/METHODOLOGY.md section 2a).
"""

from __future__ import annotations

import csv
import re
from functools import cache
from pathlib import Path

from .schema import PROVIDER, STREAMS, Stream, is_code_column

OTHER_ID_FIELDS = ("other_id", "other_id_type", "other_id_state", "other_id_issuer")
OTHER_ID_COLUMNS: tuple[str, ...] = tuple(f"{f}_{i}" for i in range(1, 51) for f in OTHER_ID_FIELDS)
SUPPRESSED_COLUMNS: tuple[str, ...] = ("ein", "parent_org_tin") + OTHER_ID_COLUMNS

# a tax-id token, without its boundaries (RE2 syntax)
TOKEN_RE = r"[0-9]{3}-[0-9]{2}-[0-9]{4}|[0-9]{3} [0-9]{2} [0-9]{4}|[0-9]{2}-[0-9]{7}|[0-9]{9}"
# the token bounded by non-digits; groups 1 and 3 are the boundaries
BOUNDED_TOKEN_RE = rf"(^|[^0-9])({TOKEN_RE})([^0-9]|$)"
SSN_TOKEN_RE = r"(^|[^0-9])[0-9]{3}[- ][0-9]{2}[- ][0-9]{4}([^0-9]|$)"
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

_LICENSE = re.compile(r"^license_number_(\d+)$")


@cache
def license_formats() -> tuple[str, ...]:
    """Known license formats as ``'ST:FORMAT'`` strings (``license_formats.csv``)."""
    with open(FORMATS_FILE, newline="") as f:
        return tuple(sorted(f"{r['state']}:{r['format']}" for r in csv.DictReader(f)))


def _lit(pattern: str) -> str:
    return "'" + pattern.replace("'", "''") + "'"


def free_text_columns(stream: Stream) -> list[str]:
    """Columns of ``stream`` the token rule (4) applies to."""
    return [c.name for c in stream.columns
            if c.kind == "text" and not is_code_column(c.name) and not _NOT_FREE_TEXT_RE.search(c.name)
            and c.name not in SUPPRESSED_COLUMNS and c.name not in LICENSE_COLUMNS and c.name not in CREDENTIAL_COLUMNS]


def redact_expr(expr: str) -> str:
    """``expr`` with every tax-id token replaced by [REDACTED]. Two passes: a match consumes the
    boundary character after it, so a token right behind another one is only found the second time."""
    once = lambda e: f"regexp_replace({e}, {_lit(BOUNDED_TOKEN_RE)}, '\\1{REDACTED}\\3', 'g')"  # noqa: E731
    return once(once(expr))


def has_token(expr: str) -> str:
    return f"regexp_matches({expr}, {_lit(BOUNDED_TOKEN_RE)})"


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
            f"OR ({has_token(col)} AND (NOT {known} OR ({_bare_nine(col)} AND {cross}))))")


LIC9 = "_lic9"


def _lic9_list() -> str:
    """Per row: the digits and state of every bare-nine license slot (rule 2b)."""
    items = ", ".join(f"CASE WHEN {_bare_nine(f'license_number_{i}')} THEN {{'d': {_digits(f'license_number_{i}')}, "
                      f"'s': license_state_{i}}} END" for i in range(1, 16))
    return f"list_filter([{items}], x -> x IS NOT NULL)"


def _cross_state(i: int) -> str:
    return (f"(len(list_filter({LIC9}, x -> x.d = {_digits(f'license_number_{i}')} "
            f"AND x.s IS DISTINCT FROM license_state_{i})) > 0)")


def _license_bad(i: int) -> str:
    """True when license slot i must be NULL (rule 2); needs the row's ``_lic9`` list."""
    return license_bad(f"license_number_{i}", f"license_state_{i}", _cross_state(i))


def scrub_expr(name: str) -> str | None:
    """SQL expression that scrubs provider column ``name``, or None if the column is untouched.
    License expressions reference the ``_lic9`` list column that ``scrub_select`` adds."""
    if name in SUPPRESSED_COLUMNS:
        return "NULL::VARCHAR"
    m = _LICENSE.match(name)
    if m:
        return f"CASE WHEN {_license_bad(int(m.group(1)))} THEN NULL ELSE {name} END"
    if name in CREDENTIAL_COLUMNS:
        return f"CASE WHEN {has_token(name)} THEN NULL ELSE {name} END"
    if name in free_text_columns(PROVIDER):
        return redact_expr(name)
    return None


def scrub_select(stream: Stream, source_table: str) -> str | None:
    """``SELECT`` over ``source_table`` applying every rule that touches this stream, or None when
    nothing in the stream is subject to a rule."""
    if stream.name == PROVIDER.name:
        repl = [f"{e} AS {c.name}" for c in stream.columns if (e := scrub_expr(c.name)) is not None]
        return (f"SELECT * EXCLUDE ({LIC9}) REPLACE ({', '.join(repl)}) "
                f"FROM (SELECT *, {_lic9_list()} AS {LIC9} FROM {source_table})")
    repl = [f"{redact_expr(n)} AS {n}" for n in free_text_columns(stream)]
    if not repl:
        return None
    return f"SELECT * REPLACE ({', '.join(repl)}) FROM {source_table}"


def privacy_checks() -> list[tuple[str, str]]:
    """(description, boolean SQL predicate over a provider_version row with ``_lic9``) pairs that must all count 0."""
    checks: list[tuple[str, str]] = []
    for c in SUPPRESSED_COLUMNS:
        checks.append((f"{c} is populated", f"{c} IS NOT NULL"))
    for i in range(1, 16):
        checks.append((f"license_number_{i} is a possible SSN or tax id", _license_bad(i)))
    for c in CREDENTIAL_COLUMNS + tuple(free_text_columns(PROVIDER)):
        checks.append((f"{c} holds a tax-id token", has_token(c)))
    return checks


def stream_privacy_checks(stream: Stream) -> list[tuple[str, str]]:
    """Checks for a supplemental stream (rule 4), same shape as ``privacy_checks``."""
    return [(f"{stream.name}.{n} holds a tax-id token", has_token(n)) for n in free_text_columns(stream)]


def _run(con, table: str, checks: list[tuple[str, str]], batch: int = 60, extra: str = "") -> list[str]:
    problems: list[str] = []
    for start in range(0, len(checks), batch):
        chunk = checks[start:start + batch]
        sel = ", ".join(f"count(*) FILTER (WHERE {pred})" for _, pred in chunk)
        row = con.execute(f"SELECT {sel} FROM (SELECT *{extra} FROM {table})").fetchone()
        problems += [f"privacy: {desc}: {n} row(s)" for (desc, _), n in zip(chunk, row) if n]
    return problems


def privacy_problems(con) -> list[str]:
    """Run every privacy check against the NPPES version tables; return the failures."""
    problems: list[str] = []
    if con.execute("SELECT count(*) FROM provider_version").fetchone()[0] != 0:
        problems += _run(con, "provider_version", privacy_checks(), extra=f", {_lic9_list()} AS {LIC9}")
    for stream in STREAMS:
        if stream.name == PROVIDER.name:
            continue
        table = f"{stream.name}_version"
        if con.execute(f"SELECT count(*) FROM {table}").fetchone()[0] != 0:
            problems += _run(con, table, stream_privacy_checks(stream))
    return problems


# every rule names a provider column; fail at import time if the schema drifts
_known = {c.name for c in PROVIDER.columns}
assert set(SUPPRESSED_COLUMNS) <= _known and set(LICENSE_COLUMNS) <= _known and set(CREDENTIAL_COLUMNS) <= _known
