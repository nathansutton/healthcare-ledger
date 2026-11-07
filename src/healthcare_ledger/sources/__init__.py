"""The data sources the warehouse is built from, one module or package per source, and their registry.

``SOURCES`` is every source in load and export order: the built-in versioned sources, then any source another
installed package registers under the ``healthcare_ledger.sources`` entry point, ordered by name; ``by_name``
looks one up for the CLI. A built-in source is a ``sources/<name>/`` package whose ``__init__`` defines ``SOURCE``
(a ``base.Source``) and one line here. A plugin source is the same ``base.Source`` declared in another package:

    [project.entry-points."healthcare_ledger.sources"]
    ahrq = "my_package.sources.ahrq:SOURCE"

and every generic command (download, load, reset, validate, status) then covers it. A plugin cannot change what a
release is: the export, its gates and the release record take only versioned sources, and a plugin's reference
source is left out of them as before.
"""

from __future__ import annotations

from importlib.metadata import entry_points

from ..core.errors import UserError
from .base import Source
from .doctors_clinicians import SOURCE as DAC
from .nppes import SOURCE as NPPES
from .ownership import SOURCE as OWNERSHIP
from .pos import SOURCE as POS
from .ppef import SOURCE as PPEF

ENTRY_POINT_GROUP = "healthcare_ledger.sources"
BUILTIN: tuple[Source, ...] = (NPPES, PPEF, POS, OWNERSHIP, DAC)


def _plugins(builtin: tuple[Source, ...]) -> tuple[Source, ...]:
    """The sources installed packages register, by entry point name; a clash with another source is refused."""
    taken = {s.name: "healthcare_ledger" for s in builtin}
    found = []
    for ep in sorted(entry_points(group=ENTRY_POINT_GROUP), key=lambda e: e.name):
        src = ep.load()
        if not isinstance(src, Source):
            raise TypeError(f"entry point {ep.name} = {ep.value} ({ENTRY_POINT_GROUP}) is not a healthcare_ledger Source")
        if src.name in taken:
            raise ValueError(f"source {src.name!r} from {ep.value} clashes with the one from {taken[src.name]}")
        taken[src.name] = ep.value
        found.append(src)
    return tuple(found)


SOURCES: tuple[Source, ...] = BUILTIN + _plugins(BUILTIN)


def by_name(name: str) -> Source:
    for s in SOURCES:
        if s.name == name:
            return s
    raise UserError(f"unknown source {name!r}; the sources are {', '.join(s.name for s in SOURCES)}", exit_code=2)
