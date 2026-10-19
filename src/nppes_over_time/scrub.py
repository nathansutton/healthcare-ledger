"""Identifiers this project never publishes, whatever the CMS file contains.

CMS withholds the Employer Identification Number (EIN) and Social Security
Number from the dissemination file, but only from the fields designed for
them. Providers also type tax ids into free-form fields: the "Other Provider
Identifier" slots (issuer "TAX ID", "EIN", "FEIN", "TIN"), the license-number
slots, and occasionally a name field. The October 2026 file still carries
thousands of them. CMS masks some of these values itself (``=========``,
``$$$$$$$$$``) but misses most.

The rules remove only values that are certainly a tax id or SSN; a value that
merely *could* be one (a nine-digit payer id under an "Other" identifier, say)
is left alone, because payer ids and Medicaid ids look the same and are what
researchers need. Every rule is applied to the staged rows *before* they are
hashed, so suppressed content never reaches the warehouse, never creates a
version and never reaches an export. ``privacy_problems`` re-checks a built
warehouse and is run by ``validate`` and, as a hard gate, by ``export``.

Rules (column names are the canonical short names in ``schema.py``):

1. ``ein`` and ``parent_org_tin`` are always NULL. CMS prints ``<UNAVAIL>``
   there, in lower case before 2009; no real value has ever been published.
2. A value that is exactly a formatted EIN (``NN-NNNNNNN``) or SSN
   (``NNN-NN-NNNN``) is NULL in every identifier and name column listed in
   ``SHAPE_SCRUB_COLUMNS``.
3. ``other_id_N`` is NULL when its issuer text says it is a tax id or SSN
   (``TAXID_ISSUER_RE``) *and* the value has exactly nine digits once
   punctuation is removed. A value under such an issuer with any other digit
   count is kept: it may be a state tax id or a typo, but it is not an EIN.
4. A formatted EIN or SSN embedded in ``other_id_issuer_N`` text is replaced
   by ``[REDACTED]`` ("MEDICARE ID UNDER OLD TIN 58-1611969").
5. In the supplemental streams (``practice_location``, ``other_name``, ``endpoint``) a
   value in any text column that is exactly a formatted EIN or SSN is NULL.

The rules are summarized in docs/METHODOLOGY.md section 2a.
"""

from __future__ import annotations

import re

from .schema import PROVIDER, STREAMS, Stream

SUPPRESSED_COLUMNS: tuple[str, ...] = ("ein", "parent_org_tin")

# whole-value shapes (RE2 syntax)
TAXID_SHAPE_RE = r"^\d{2}-\d{7}$|^\d{3}-\d{2}-\d{4}$"
# issuer text that says the identifier is a tax id or SSN
TAXID_ISSUER_RE = (r"(?i)\b(TAX|EIN|FEIN|TIN|SSN|SS#|SOC(IAL)?\s*SEC(URITY)?"
                   r"|EMPLOYER\s+ID(ENTIFICATION)?|IRS|FED(ERAL)?\s*ID)\b")
# the same shapes embedded in longer text
EMBEDDED_TAXID_RE = r"\d{2}-\d{7}|\d{3}-\d{2}-\d{4}"
REDACTED = "[REDACTED]"

NAME_COLUMNS: tuple[str, ...] = (
    "org_name", "other_org_name", "parent_org_name",
    "last_name", "first_name", "middle_name", "credential",
    "other_last_name", "other_first_name", "other_middle_name", "other_credential",
    "ao_last_name", "ao_first_name", "ao_middle_name", "ao_title", "ao_credential",
)
OTHER_ID_COLUMNS = tuple(f"other_id_{i}" for i in range(1, 51))
OTHER_ID_ISSUER_COLUMNS = tuple(f"other_id_issuer_{i}" for i in range(1, 51))
LICENSE_COLUMNS = tuple(f"license_number_{i}" for i in range(1, 16))
SHAPE_SCRUB_COLUMNS: tuple[str, ...] = OTHER_ID_COLUMNS + OTHER_ID_ISSUER_COLUMNS + LICENSE_COLUMNS + NAME_COLUMNS

_OTHER_ID = re.compile(r"^other_id_(\d+)$")
_ISSUER = re.compile(r"^other_id_issuer_(\d+)$")


def _lit(pattern: str) -> str:
    return "'" + pattern.replace("'", "''") + "'"


def _redacted(col: str) -> str:
    return f"regexp_replace({col}, {_lit(EMBEDDED_TAXID_RE)}, '{REDACTED}', 'g')"


def _nine_digits(col: str) -> str:
    return f"length(regexp_replace({col}, '[^0-9]', '', 'g')) = 9"


def scrub_expr(name: str) -> str | None:
    """SQL expression that scrubs column ``name``, referencing sibling columns by their
    staged (unscrubbed) names, or None if the column is untouched."""
    if name in SUPPRESSED_COLUMNS:
        return "NULL::VARCHAR"
    m = _OTHER_ID.match(name)
    if m:
        # The keyword test runs on the redacted issuer: "12-3456789TAX ID" only shows the word
        # boundary before TAX once the embedded id is replaced.
        issuer = _redacted(f"other_id_issuer_{m.group(1)}")
        return (f"CASE WHEN regexp_matches({name}, {_lit(TAXID_SHAPE_RE)}) THEN NULL "
                f"WHEN regexp_matches({issuer}, {_lit(TAXID_ISSUER_RE)}) AND {_nine_digits(name)} THEN NULL "
                f"ELSE {name} END")
    if _ISSUER.match(name):
        return f"CASE WHEN regexp_matches({name}, {_lit(TAXID_SHAPE_RE)}) THEN NULL ELSE {_redacted(name)} END"
    if name in SHAPE_SCRUB_COLUMNS:
        return f"CASE WHEN regexp_matches({name}, {_lit(TAXID_SHAPE_RE)}) THEN NULL ELSE {name} END"
    return None


def supplemental_text_columns(stream: Stream) -> list[str]:
    """Text columns of a supplemental (non-provider) stream; rule 5 applies to each."""
    return [c.name for c in stream.columns if c.kind == "text"]


def scrub_select(stream: Stream, source_table: str) -> str | None:
    """``SELECT * REPLACE (...) FROM source_table`` applying every rule that touches
    this stream, or None when nothing in the stream is subject to a rule."""
    if stream.name == "provider":
        repl = [f"{e} AS {c.name}" for c in stream.columns if (e := scrub_expr(c.name)) is not None]
    else:
        repl = [f"CASE WHEN regexp_matches({n}, {_lit(TAXID_SHAPE_RE)}) THEN NULL ELSE {n} END AS {n}"
                for n in supplemental_text_columns(stream)]
    if not repl:
        return None
    return f"SELECT * REPLACE ({', '.join(repl)}) FROM {source_table}"


def privacy_checks() -> list[tuple[str, str]]:
    """(description, boolean SQL predicate over provider_version) pairs that must all count 0."""
    checks: list[tuple[str, str]] = []
    for c in SUPPRESSED_COLUMNS:
        checks.append((f"{c} is populated", f"{c} IS NOT NULL"))
    for c in SHAPE_SCRUB_COLUMNS:
        checks.append((f"{c} holds a formatted EIN or SSN", f"regexp_matches({c}, {_lit(TAXID_SHAPE_RE)})"))
    for i in range(1, 51):
        checks.append((f"other_id_{i} is a nine-digit value under a tax-id issuer",
                       f"regexp_matches({_redacted(f'other_id_issuer_{i}')}, {_lit(TAXID_ISSUER_RE)}) AND {_nine_digits(f'other_id_{i}')}"))
        checks.append((f"other_id_issuer_{i} embeds a formatted EIN or SSN",
                       f"regexp_matches(other_id_issuer_{i}, {_lit(EMBEDDED_TAXID_RE)})"))
    return checks


def privacy_problems(con, batch: int = 60) -> list[str]:
    """Run every privacy check against the version tables; return the failures."""
    problems: list[str] = []
    if con.execute("SELECT count(*) FROM provider_version").fetchone()[0] != 0:
        checks = privacy_checks()
        for start in range(0, len(checks), batch):
            chunk = checks[start:start + batch]
            sel = ", ".join(f"count(*) FILTER (WHERE {pred})" for _, pred in chunk)
            row = con.execute(f"SELECT {sel} FROM provider_version").fetchone()
            problems += [f"privacy: {desc}: {n} row(s)" for (desc, _), n in zip(chunk, row) if n]
    for stream in STREAMS:
        if stream.name == "provider":
            continue
        table = f"{stream.name}_version"
        if con.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0:
            continue
        cols = supplemental_text_columns(stream)
        sel = ", ".join(f"count(*) FILTER (WHERE regexp_matches({n}, {_lit(TAXID_SHAPE_RE)}))" for n in cols)
        row = con.execute(f"SELECT {sel} FROM {table}").fetchone()
        problems += [f"privacy: {stream.name}.{n} holds a formatted EIN or SSN: {k} row(s)"
                     for n, k in zip(cols, row) if k]
    return problems


# every rule names a provider column; fail at import time if the schema drifts
_known = {c.name for c in PROVIDER.columns}
assert set(SHAPE_SCRUB_COLUMNS) <= _known and set(SUPPRESSED_COLUMNS) <= _known
