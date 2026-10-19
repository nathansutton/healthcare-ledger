"""Canonical column schema for the NPPES data dissemination files.

CMS has published the monthly "NPPES Data Dissemination" file since 2007. The
main file (``npidata_*.csv``) grew from 187 columns (Nov 2007) to 330 columns
(Dec 2019 onward) and renamed one column ("Provider Gender Code" became
"Provider Sex Code" in March 2025). Growth was strictly additive, so every
vintage maps onto the final 330-column layout with NULLs for columns that did
not exist yet.

Three supplemental files were added to the zip in July 2018:
``pl_pfile`` (secondary practice locations), ``othername_pfile``
(organization other names) and ``endpoint_pfile`` (FHIR/Direct endpoints).

This module defines, for each "stream", the canonical short column names
(all <= 32 characters so they survive a trip through Stata), the CMS header
each one comes from, and header aliases seen in the wild. Matching is done on
a normalized key (lowercase alphanumerics only) so spacing and punctuation
differences between vintages do not matter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


# CMS placeholders that mean "no value". The bracketed tokens are system-generated (CMS prints
# <UNAVAIL> where it withholds a field, such as every organization's EIN; the Oct 2026 file put
# <UNAVAIL> into 1.9M other-organization-name fields that had been blank). They become NULL in
# every column. The 2007 and 2008 files spell the token in lower case, so both cases are listed:
# a nullif() chain is case-sensitive and the planner cannot afford an upper() per column.
_PLACEHOLDER_WORDS = ("UNAVAIL", "BLANK", "EMPTY")
PLACEHOLDERS: tuple[str, ...] = tuple(f"<{w}>" for w in _PLACEHOLDER_WORDS) + tuple(f"<{w.lower()}>" for w in _PLACEHOLDER_WORDS)

# The literal word NULL is treated as a placeholder only in code-valued columns (the Oct 2026
# file wrote it into 3.2M license-state slots). In free-text columns it stays: 127 providers
# are surnamed Null.
NULL_WORD = "NULL"
CODE_COLUMN_RE = re.compile(
    r"^(entity_type|sex|sole_proprietor|org_subpart|deactivation_reason|other_org_name_type|other_last_name_type"
    r"|.*_state(_\d+)?|.*_country|.*_type(_\d+)?|taxonomy_code_\d+|taxonomy_group_\d+|primary_taxonomy_\d+"
    r"|other_id_type_\d+|use_code|content_type|endpoint_type)$")


def is_code_column(name: str) -> bool:
    return bool(CODE_COLUMN_RE.match(name))

# A name-type code with no name describes nothing, so it is NULL wherever the name it qualifies is
# NULL. CMS needs the rule: in Feb 2025 it dropped other organization names from the main file and
# printed an undocumented type code 6 in their place, and the Oct 2026 file added code 6 to 1.47M
# organizations whose other name had always been blank.
TYPE_REQUIRES_NAME: dict[str, str] = {"other_org_name_type": "other_org_name"}


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


@dataclass(frozen=True)
class Stream:
    """One logical table derived from one member file of the CMS zip."""

    name: str
    member_pattern: str            # regex matched against zip member basenames
    columns: tuple[Column, ...]    # excludes the NPI key, which every stream has
    description: str = ""
    always_present: bool = False   # False => stream may be missing from a snapshot

    @property
    def data_columns(self) -> list[str]:
        return [c.name for c in self.columns]


# --------------------------------------------------------------------------
# Main file: 330 columns in the exact CMS order.
# --------------------------------------------------------------------------

def _main_columns() -> tuple[Column, ...]:
    cols: list[Column] = [
        Column("entity_type", "Entity Type Code"),
        Column("replacement_npi", "Replacement NPI"),
        Column("ein", "Employer Identification Number (EIN)"),
        Column("org_name", "Provider Organization Name (Legal Business Name)"),
        Column("last_name", "Provider Last Name (Legal Name)"),
        Column("first_name", "Provider First Name"),
        Column("middle_name", "Provider Middle Name"),
        Column("name_prefix", "Provider Name Prefix Text"),
        Column("name_suffix", "Provider Name Suffix Text"),
        Column("credential", "Provider Credential Text"),
        Column("other_org_name", "Provider Other Organization Name"),
        Column("other_org_name_type", "Provider Other Organization Name Type Code"),
        Column("other_last_name", "Provider Other Last Name"),
        Column("other_first_name", "Provider Other First Name"),
        Column("other_middle_name", "Provider Other Middle Name"),
        Column("other_name_prefix", "Provider Other Name Prefix Text"),
        Column("other_name_suffix", "Provider Other Name Suffix Text"),
        Column("other_credential", "Provider Other Credential Text"),
        Column("other_last_name_type", "Provider Other Last Name Type Code"),
        Column("mail_address_1", "Provider First Line Business Mailing Address"),
        Column("mail_address_2", "Provider Second Line Business Mailing Address"),
        Column("mail_city", "Provider Business Mailing Address City Name"),
        Column("mail_state", "Provider Business Mailing Address State Name"),
        Column("mail_postal_code", "Provider Business Mailing Address Postal Code"),
        Column("mail_country", "Provider Business Mailing Address Country Code (If outside U.S.)"),
        Column("mail_phone", "Provider Business Mailing Address Telephone Number"),
        Column("mail_fax", "Provider Business Mailing Address Fax Number"),
        Column("practice_address_1", "Provider First Line Business Practice Location Address"),
        Column("practice_address_2", "Provider Second Line Business Practice Location Address"),
        Column("practice_city", "Provider Business Practice Location Address City Name"),
        Column("practice_state", "Provider Business Practice Location Address State Name"),
        Column("practice_postal_code", "Provider Business Practice Location Address Postal Code"),
        Column("practice_country", "Provider Business Practice Location Address Country Code (If outside U.S.)"),
        Column("practice_phone", "Provider Business Practice Location Address Telephone Number"),
        Column("practice_fax", "Provider Business Practice Location Address Fax Number"),
        Column("enumeration_date", "Provider Enumeration Date", kind="date"),
        Column("last_update_date", "Last Update Date", kind="date"),
        Column("deactivation_reason", "NPI Deactivation Reason Code"),
        Column("deactivation_date", "NPI Deactivation Date", kind="date"),
        Column("reactivation_date", "NPI Reactivation Date", kind="date"),
        Column("sex", "Provider Sex Code", aliases=("Provider Gender Code",)),
        Column("ao_last_name", "Authorized Official Last Name"),
        Column("ao_first_name", "Authorized Official First Name"),
        Column("ao_middle_name", "Authorized Official Middle Name"),
        Column("ao_title", "Authorized Official Title or Position"),
        Column("ao_phone", "Authorized Official Telephone Number"),
    ]
    for i in range(1, 16):
        cols += [
            Column(f"taxonomy_code_{i}", f"Healthcare Provider Taxonomy Code_{i}"),
            Column(f"license_number_{i}", f"Provider License Number_{i}"),
            Column(f"license_state_{i}", f"Provider License Number State Code_{i}"),
            Column(f"primary_taxonomy_{i}", f"Healthcare Provider Primary Taxonomy Switch_{i}"),
        ]
    for i in range(1, 51):
        cols += [
            Column(f"other_id_{i}", f"Other Provider Identifier_{i}"),
            Column(f"other_id_type_{i}", f"Other Provider Identifier Type Code_{i}"),
            Column(f"other_id_state_{i}", f"Other Provider Identifier State_{i}"),
            Column(f"other_id_issuer_{i}", f"Other Provider Identifier Issuer_{i}"),
        ]
    cols += [
        Column("sole_proprietor", "Is Sole Proprietor"),
        Column("org_subpart", "Is Organization Subpart"),
        Column("parent_org_name", "Parent Organization LBN"),
        Column("parent_org_tin", "Parent Organization TIN"),
        Column("ao_name_prefix", "Authorized Official Name Prefix Text"),
        Column("ao_name_suffix", "Authorized Official Name Suffix Text"),
        Column("ao_credential", "Authorized Official Credential Text"),
    ]
    cols += [Column(f"taxonomy_group_{i}", f"Healthcare Provider Taxonomy Group_{i}") for i in range(1, 16)]
    cols.append(Column("certification_date", "Certification Date", kind="date"))
    assert len(cols) == 329, len(cols)  # + NPI = 330
    return tuple(cols)


PROVIDER = Stream(
    name="provider",
    member_pattern=r"^npidata(_pfile)?_\d{8}-\d{8}\.csv$",
    columns=_main_columns(),
    description="Main NPPES file: one row per NPI with all 330 CMS columns.",
    always_present=True,
)

PRACTICE_LOCATION = Stream(
    name="practice_location",
    member_pattern=r"^pl_pfile_\d{8}-\d{8}\.csv$",
    columns=(
        Column("address_1", "Provider Secondary Practice Location Address- Address Line 1"),
        Column("address_2", "Provider Secondary Practice Location Address-  Address Line 2"),
        Column("city", "Provider Secondary Practice Location Address - City Name"),
        Column("state", "Provider Secondary Practice Location Address - State Name"),
        Column("postal_code", "Provider Secondary Practice Location Address - Postal Code"),
        Column("country", "Provider Secondary Practice Location Address - Country Code (If outside U.S.)"),
        Column("phone", "Provider Secondary Practice Location Address - Telephone Number"),
        Column("phone_ext", "Provider Secondary Practice Location Address - Telephone Extension"),
        Column("fax", "Provider Practice Location Address - Fax Number",
               aliases=("Provider Secondary Practice Location Address - Fax Number",)),
    ),
    description="Secondary practice locations (pl_pfile), zero or more per NPI. Present from the Jul 2018 file.",
)

OTHER_NAME = Stream(
    name="other_name",
    member_pattern=r"^othername_pfile_\d{8}-\d{8}\.csv$",
    columns=(
        Column("other_org_name", "Provider Other Organization Name"),
        Column("other_org_name_type", "Provider Other Organization Name Type Code"),
        Column("created_date", "Created Date", kind="date"),  # added by CMS in the April 2026 V2 file
    ),
    description="Additional organization names (othername_pfile), zero or more per NPI. Present from the Jul 2018 file; created_date from Apr 2026.",
)

ENDPOINT = Stream(
    name="endpoint",
    member_pattern=r"^endpoint_pfile_\d{8}-\d{8}\.csv$",
    columns=(
        Column("endpoint_type", "Endpoint Type"),
        Column("endpoint_type_desc", "Endpoint Type Description"),
        Column("endpoint", "Endpoint"),
        Column("affiliation", "Affiliation"),
        Column("endpoint_desc", "Endpoint Description"),
        Column("affiliation_name", "Affiliation Legal Business Name"),
        Column("use_code", "Use Code", aliases=("Use",)),
        Column("use_desc", "Use Description"),
        Column("other_use_desc", "Other Use Description", aliases=("Use Other Description",)),
        Column("content_type", "Content Type"),
        Column("content_desc", "Content Description"),
        Column("other_content_desc", "Other Content Description", aliases=("Content Other Description",)),
        Column("affil_address_1", "Affiliation Address Line One"),
        Column("affil_address_2", "Affiliation Address Line Two"),
        Column("affil_city", "Affiliation Address City"),
        Column("affil_state", "Affiliation Address State"),
        Column("affil_country", "Affiliation Address Country"),
        Column("affil_postal_code", "Affiliation Address Postal Code"),
    ),
    description="Health information exchange endpoints (endpoint_pfile), zero or more per NPI. Shipped from Jul 2018, non-empty from Mar 2019.",
)

# When CMS adds a column, append it to the stream with `hash_optional=True`. Never set it on an
# existing column; that changes every stored hash.
STREAMS: tuple[Stream, ...] = (PROVIDER, PRACTICE_LOCATION, OTHER_NAME, ENDPOINT)
STREAM_BY_NAME = {s.name: s for s in STREAMS}

NPI_HEADER_KEY = header_key("NPI")


def map_headers(stream: Stream, headers: list[str]) -> tuple[dict[str, str | None], list[str]]:
    """Map a file's actual headers onto the stream's canonical columns.

    Returns ``(mapping, unmatched)`` where ``mapping`` is canonical name ->
    actual header (or None when the vintage lacks the column) and
    ``unmatched`` lists actual headers that matched nothing (should be empty).
    """
    by_key: dict[str, str] = {}
    for h in headers:
        k = header_key(h)
        if k in by_key and by_key[k] != h:
            raise ValueError(f"headers {by_key[k]!r} and {h!r} collide on key {k!r}")
        by_key[k] = h
    if NPI_HEADER_KEY not in by_key:
        raise ValueError("file has no NPI column")

    mapping: dict[str, str | None] = {"npi": by_key[NPI_HEADER_KEY]}
    used = {by_key[NPI_HEADER_KEY]}
    for col in stream.columns:
        hit = next((by_key[k] for k in col.keys() if k in by_key), None)
        mapping[col.name] = hit
        if hit is not None:
            if hit in used:
                raise ValueError(f"header {hit!r} matched more than one canonical column")
            used.add(hit)
    unmatched = [h for h in headers if h not in used]
    return mapping, unmatched


def column_kinds(stream: Stream) -> dict[str, str]:
    return {c.name: c.kind for c in stream.columns}
