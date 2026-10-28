"""The data sources the warehouse is built from, one module or package per source, and their registry.

``SOURCES`` is every source in load and export order (the versioned ones, then the reference ones, which
the export leaves out; ``base.Source.kind``); ``by_name`` looks one up for the CLI. A new
source is a ``sources/<name>/`` package whose ``__init__`` defines ``SOURCE`` (a ``base.Source``)
and one line here.
"""

from __future__ import annotations

from ..core.errors import UserError
from .ahrq import SOURCE as AHRQ
from .base import Source
from .doctors_clinicians import SOURCE as DAC
from .medicaid import SOURCE as MEDICAID
from .medicare_utilization import SOURCE as MEDICARE_UTILIZATION
from .nppes import SOURCE as NPPES
from .ownership import SOURCE as OWNERSHIP
from .pos import SOURCE as POS
from .ppef import SOURCE as PPEF

SOURCES: tuple[Source, ...] = (NPPES, PPEF, POS, OWNERSHIP, DAC, AHRQ, MEDICAID, MEDICARE_UTILIZATION)


def by_name(name: str) -> Source:
    for s in SOURCES:
        if s.name == name:
            return s
    raise UserError(f"unknown source {name!r}; the sources are {', '.join(s.name for s in SOURCES)}", exit_code=2)
