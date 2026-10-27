"""CMS Provider of Services files, quarterly, keyed by CCN (``pos.py``, columns in ``pos_columns.csv``)."""

from __future__ import annotations

from ..base import Source, quarter_status, recorded_download, recorded_files_gate
from . import pos

SOURCE = Source(
    name="pos",
    database=pos.PosWarehouse.database,
    title="POS",
    kind="versioned",
    key=pos.KEY,
    streams=pos.POS_STREAMS,
    required=False,
    snapshots_file=pos.SNAPSHOTS_FILE,
    sources_file=pos.SOURCES_FILE,
    default_source_dir="data/pos",
    snapshot_noun="quarters",
    warehouse_cls=pos.PosWarehouse,
    discover=pos.discover_checked,
    loaded=pos.loaded,
    finalize=pos.finalize,
    reset=pos.reset,
    check_problems=pos.check_problems,
    stored_fingerprints=pos.stored_fingerprints,
    recompute_fingerprints=pos.recompute_fingerprints,
    tables=pos.tables,
    snapshots_sql=pos.snapshots_sql,
    validate_streams=lambda: [(s.name, pos.KEY, "pos_snapshot_stream", False) for s in pos.POS_STREAMS],
    status=quarter_status("pos_snapshot"),
    check_sources=recorded_files_gate("pos", "POS", pos.SOURCES_FILE, pos.sources_problems),
    write_sources=pos.write_sources,
    download=recorded_download("POS", pos.download_sources),
)
