"""CMS's Medicare facility Enrollments and All Owners files, monthly from 2022, keyed by enrollment id (``ownership.py``,
columns in ``ownership_columns.csv``)."""

from __future__ import annotations

from ..base import Source, quarter_status, recorded_download, recorded_files_gate
from . import ownership

SOURCE = Source(
    name="ownership",
    database=ownership.OwnershipWarehouse.database,
    title="Ownership",
    kind="versioned",
    key=ownership.KEY,
    streams=ownership.OWNERSHIP_STREAMS,
    required=False,
    snapshots_file=ownership.SNAPSHOTS_FILE,
    sources_file=ownership.SOURCES_FILE,
    default_source_dir="data/ownership",
    snapshot_noun="releases",
    warehouse_cls=ownership.OwnershipWarehouse,
    discover=ownership.discover_checked,
    loaded=ownership.loaded,
    finalize=ownership.finalize,
    reset=ownership.reset,
    check_problems=ownership.check_problems,
    stored_fingerprints=ownership.stored_fingerprints,
    recompute_fingerprints=ownership.recompute_fingerprints,
    tables=ownership.tables,
    snapshots_sql=ownership.snapshots_sql,
    validate_streams=lambda: [(s.name, ownership.KEY, "ownership_snapshot_stream", False)
                              for s in ownership.OWNERSHIP_STREAMS],
    status=quarter_status("ownership_snapshot"),
    check_sources=recorded_files_gate("ownership", "ownership", ownership.SOURCES_FILE, ownership.sources_problems),
    write_sources=ownership.write_sources,
    download=recorded_download("ownership", ownership.download_sources),
)
