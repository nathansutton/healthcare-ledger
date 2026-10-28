"""CMS's Doctors and Clinicians (Physician Compare) National Downloadable File, quarterly 2014 to 2022, keyed by NPI
(``dac.py``, columns in ``dac_columns.csv``)."""

from __future__ import annotations

from ..base import Source, quarter_status, recorded_download, recorded_files_gate
from . import dac

SOURCE = Source(
    name="doctors_clinicians",
    database=dac.DacWarehouse.database,
    title="Doctors and Clinicians",
    kind="versioned",
    key=dac.KEY,
    streams=dac.DAC_STREAMS,
    required=False,
    snapshots_file=dac.SNAPSHOTS_FILE,
    sources_file=dac.SOURCES_FILE,
    default_source_dir="data/dac/raw",
    snapshot_noun="snapshots",
    warehouse_cls=dac.DacWarehouse,
    discover=dac.discover_checked,
    loaded=dac.loaded,
    finalize=dac.finalize,
    reset=dac.reset,
    check_problems=dac.check_problems,
    stored_fingerprints=dac.stored_fingerprints,
    recompute_fingerprints=dac.recompute_fingerprints,
    tables=dac.tables,
    snapshots_sql=dac.snapshots_sql,
    validate_streams=lambda: [(s.name, dac.KEY, "dac_snapshot_stream", False) for s in dac.DAC_STREAMS],
    status=quarter_status("dac_snapshot"),
    check_sources=recorded_files_gate("doctors_clinicians", "Doctors and Clinicians", dac.SOURCES_FILE,
                                      dac.sources_problems),
    write_sources=dac.write_sources,
    download=recorded_download("Doctors and Clinicians", dac.download_sources),
)
