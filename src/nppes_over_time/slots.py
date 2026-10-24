"""Canonical order of the main file's taxonomy/license slots.

CMS stores two repeating groups in numbered slots: 15 taxonomy slots (taxonomy code, license
number, license state, primary switch, taxonomy group) and 50 other-identifier slots (identifier,
type, state, issuer). It reshuffles entries between slots from month to month without changing
them: in some years 96-99% of the version changes those columns cause are the same entries in a
different order. Hashing the slots as CMS wrote them would turn every
reshuffle into a new version. The other-identifier slots are suppressed entirely (scrub.py rule 1),
so only the taxonomy group needs an order.

So each group is put into a canonical order before it is hashed or stored, after the scrub rules
have run (a suppressed value is suppressed before the sort sees it):

1. a slot whose every field is NULL is dropped; the remaining entries are packed into slots 1..n
   in order, and the rest are NULL;
2. the entries are sorted by their fields, joined with the unit separator chr(31) and NULL read
   as '', in the order ``SORT_FIELDS`` lists for the group;
3. in the taxonomy group a slot whose primary switch is ``Y`` sorts before every other slot, so
   ``taxonomy_code_1`` is the primary taxonomy whenever CMS flagged one.

The order is total, so the same set of entries always lands in the same slots, and two months
that differ only by a reshuffle hash identically. The published ``slot`` column is this canonical
position, not the position in the CMS file. The rule is part of the version semantics frozen per
edition (docs/METHODOLOGY.md section 3).
"""

from __future__ import annotations

from dataclasses import dataclass

from .schema import PROVIDER, Stream

SEP = "chr(31)"


@dataclass(frozen=True)
class SlotGroup:
    name: str
    n: int
    fields: tuple[str, ...]       # column name prefixes; the column for slot i is f"{field}_{i}"
    sort_fields: tuple[str, ...]  # the order the fields enter the sort key
    primary_field: str | None = None  # a field whose value 'Y' sorts the slot first

    def column(self, field: str, i: int) -> str:
        return f"{field}_{i}"

    @property
    def columns(self) -> list[str]:
        return [self.column(f, i) for i in range(1, self.n + 1) for f in self.fields]


TAXONOMY = SlotGroup(
    name="taxonomy", n=15,
    fields=("taxonomy_code", "license_number", "license_state", "primary_taxonomy", "taxonomy_group"),
    sort_fields=("taxonomy_code", "license_state", "license_number", "taxonomy_group", "primary_taxonomy"),
    primary_field="primary_taxonomy",
)
GROUPS: tuple[SlotGroup, ...] = (TAXONOMY,)


def _struct(g: SlotGroup, i: int) -> str:
    """A struct literal for slot i: the sort key ``k`` first, then every field."""
    parts = []
    if g.primary_field:
        parts.append(f"CASE WHEN {g.column(g.primary_field, i)} = 'Y' THEN '0' ELSE '1' END")
    parts += [f"coalesce({g.column(f, i)}, '')" for f in g.sort_fields]
    key = f"concat_ws({SEP}, {', '.join(parts)})"
    non_empty = " OR ".join(f"{g.column(f, i)} IS NOT NULL" for f in g.fields)
    fields = ", ".join(f"'{f}': {g.column(f, i)}" for f in g.fields)
    return f"CASE WHEN {non_empty} THEN {{'k': {key}, {fields}}} END"


def canonical_select(stream: Stream, source_table: str) -> str | None:
    """``SELECT`` over ``source_table`` with every slot group in canonical order, or None when the
    stream has no slot groups. Every other column passes through unchanged."""
    if stream.name != PROVIDER.name:
        return None
    lists = []
    replace = []
    for g in GROUPS:
        lst = f"_{g.name}_slots"
        structs = ", ".join(_struct(g, i) for i in range(1, g.n + 1))
        lists.append(f"list_sort(list_filter([{structs}], x -> x IS NOT NULL)) AS {lst}")
        for i in range(1, g.n + 1):
            replace += [f"{lst}[{i}].{f} AS {g.column(f, i)}" for f in g.fields]
    exclude = ", ".join(f"_{g.name}_slots" for g in GROUPS)
    return (f"SELECT * EXCLUDE ({exclude}) REPLACE ({', '.join(replace)}) "
            f"FROM (SELECT *, {', '.join(lists)} FROM {source_table})")


# every group names provider columns; fail at import time if the schema drifts
_known = {c.name for c in PROVIDER.columns}
assert all(set(g.columns) <= _known for g in GROUPS)
