"""Column declarations and the canonical text of a CMS value, shared by every source.

``Column`` is one data column of a stream: its canonical short name, the CMS header it comes from
and the aliases seen in the wild. Headers are matched on ``header_key`` (lowercase alphanumerics),
so spacing and punctuation differences between vintages do not matter. ``null_placeholders`` is
the trim-and-NULL rule the engine applies to every staged text value.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


# CMS placeholders that mean "no value". The bracketed tokens are system-generated (CMS prints
# <UNAVAIL> where it withholds a field, such as every organization's EIN; the Oct 2026 file put
# <UNAVAIL> into 1.9M other-organization-name fields that had been blank). They become NULL in
# every column. The 2007 and 2008 files spell the token in lower case, so both cases are listed:
# the match is case-sensitive and the planner cannot afford an upper() per column.
_PLACEHOLDER_WORDS = ("UNAVAIL", "BLANK", "EMPTY")
PLACEHOLDERS: tuple[str, ...] = tuple(f"<{w}>" for w in _PLACEHOLDER_WORDS) + tuple(f"<{w.lower()}>" for w in _PLACEHOLDER_WORDS)

# The literal word NULL is treated as a placeholder only in code-valued columns (the Oct 2026
# file wrote it into 3.2M license-state slots). In free-text columns it stays: 127 providers
# are surnamed Null.
NULL_WORD = "NULL"
CODE_COLUMN_RE = re.compile(
    r"^(entity_type|sex|sole_proprietor|org_subpart|deactivation_reason|other_org_name_type|other_last_name_type"
    r"|.*_state(_\d+)?|.*_country|.*_type(_\d+)?|taxonomy_code_\d+|taxonomy_group_\d+|primary_taxonomy_\d+"
    r"|use_code|content_type|endpoint_type)$")


def is_code_column(name: str) -> bool:
    return bool(CODE_COLUMN_RE.match(name))


def placeholder_tokens(name: str) -> list[str]:
    """The values that mean "no value" in column `name`: '', the CMS placeholders and, in code columns, NULL."""
    return ["", *PLACEHOLDERS] + ([NULL_WORD] if is_code_column(name) else [])


def null_placeholders(expr: str, tokens: list[str]) -> str:
    """SQL for trim(expr), or NULL when the trimmed value is one of `tokens`.

    Written with list_contains on purpose. Over 329 NPPES columns, CASE WHEN trim(x) IN (...)
    made DuckDB's planner explode past 80 GB, and the nested nullif() chain this replaced spent
    2.45 s planning each snapshot (66 kB of SQL, the same on a 3-row file); list_contains is one
    scalar call per column and plans in 0.04 s with identical output (DuckDB 1.5.6, Oct 2026).
    """
    toks = ", ".join("'" + t.replace("'", "''") + "'" for t in tokens)
    return f"CASE WHEN list_contains([{toks}], trim({expr})) THEN NULL ELSE trim({expr}) END"

def header_key(header: str) -> str:
    """Normalize a CMS header into a comparison key: lowercase alphanumerics."""
    return re.sub(r"[^a-z0-9]", "", header.lower())


@dataclass(frozen=True)
class Column:
    name: str                      # canonical short name
    cms_header: str                # header in the most recent CMS file
    aliases: tuple[str, ...] = ()  # other headers CMS used for the same field
    kind: str = "text"             # text | date | int
    # True for columns added after the 2026-10 baseline: they enter the row hash only when non-NULL,
    # so adding one to an existing warehouse does not re-version every NPI.
    hash_optional: bool = False

    def keys(self) -> set[str]:
        return {header_key(self.cms_header), *(header_key(a) for a in self.aliases)}
