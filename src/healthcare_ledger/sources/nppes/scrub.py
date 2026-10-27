"""The privacy rules of ``core.privacy`` applied to the NPPES streams.

``scrub_select`` builds the staging pass the loader runs before hashing (rules 1 to 4 on the
provider stream, rule 4 on the supplemental streams), and ``privacy_problems`` re-checks the
NPPES version tables of a built warehouse; ``validate`` and ``export`` run it.
"""

from __future__ import annotations

import re

from ...core.privacy import (CREDENTIAL_COLUMNS, LICENSE_COLUMNS, SUPPRESSED_COLUMNS, _bare_nine, _digits,
                             credential_bad, free_text_columns, has_token, license_bad, redact_expr)
from .schema import PROVIDER, STREAMS, Stream

_LICENSE = re.compile(r"^license_number_(\d+)$")

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
        return f"CASE WHEN {credential_bad(name)} THEN NULL ELSE {name} END"
    if name in free_text_columns(PROVIDER):
        return redact_expr(name, name)
    return None


def scrub_select(stream: Stream, source_table: str) -> str | None:
    """``SELECT`` over ``source_table`` applying every rule that touches this stream, or None when
    nothing in the stream is subject to a rule."""
    if stream.name == PROVIDER.name:
        repl = [f"{e} AS {c.name}" for c in stream.columns if (e := scrub_expr(c.name)) is not None]
        return (f"SELECT * EXCLUDE ({LIC9}) REPLACE ({', '.join(repl)}) "
                f"FROM (SELECT *, {_lic9_list()} AS {LIC9} FROM {source_table})")
    repl = [f"{redact_expr(n, n)} AS {n}" for n in free_text_columns(stream)]
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
    for c in CREDENTIAL_COLUMNS:
        checks.append((f"{c} holds nine digits", credential_bad(c)))
    for c in free_text_columns(PROVIDER):
        checks.append((f"{c} holds a tax-id token", has_token(c, c)))
    return checks


def stream_privacy_checks(stream: Stream) -> list[tuple[str, str]]:
    """Checks for a supplemental stream (rule 4), same shape as ``privacy_checks``."""
    return [(f"{stream.name}.{n} holds a tax-id token", has_token(n, n)) for n in free_text_columns(stream)]


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
