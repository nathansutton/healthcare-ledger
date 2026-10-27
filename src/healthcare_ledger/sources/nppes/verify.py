"""Field-level check that the warehouse reproduces the raw monthly files.

``validate`` proves counts and invariants without needing the raw data. ``verify`` is
the stronger, opt-in check: for each chosen snapshot it re-stages every stream from the
CMS zip with the loader's own code (``Warehouse._stage`` and ``typed_columns``),
then compares the result, every column of every row plus ``version_hash`` and
``row_seq``, with what interval lookup returns from the warehouse. A bug in hashing,
diffing or the insert, or a hand edit after load, shows up as a difference.

The warehouse's NPPES file is only ever attached READ_ONLY. Staging happens in a scratch
warehouse directory inside the work directory (``verify_scratch/``), which is removed afterwards.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from ...core.connect import open_catalog, source_file
from .loader import Warehouse, typed_columns
from .schema import STREAMS
from .snapshots import discover, extract_member

log = logging.getLogger(__name__)


def _loaded(warehouse: Path) -> tuple[dict[int, str], dict[int, set[str]]]:
    con = open_catalog(warehouse, read_only=True)
    try:
        zips = {r[0]: r[1] for r in con.execute("SELECT snapshot_id, zip_file FROM snapshot").fetchall()}
        streams: dict[int, set[str]] = {}
        for sid, name in con.execute("SELECT snapshot_id, stream FROM snapshot_stream").fetchall():
            streams.setdefault(sid, set()).add(name)
    finally:
        con.close()
    return zips, streams


def verify(warehouse: Path, source: Path, snapshot_ids: list[int] | None, work_dir: Path,
           threads: int | None = None, memory_limit: str | None = None) -> list[str]:
    problems: list[str] = []
    zips, loaded_streams = _loaded(Path(warehouse))
    if snapshot_ids is None:
        snapshot_ids = [max(zips)] if zips else []
    by_id = {s.snapshot_id: s for s in discover(Path(source))}

    todo = []
    for sid in snapshot_ids:
        if sid not in zips:
            problems.append(f"snapshot {sid} is not in the warehouse")
        elif sid not in by_id:
            problems.append(f"snapshot {sid}: no zip for it in {source}")
        elif by_id[sid].zip_path.name != zips[sid]:
            problems.append(f"snapshot {sid}: warehouse was loaded from {zips[sid]}, source has {by_id[sid].zip_path.name}")
        else:
            todo.append(by_id[sid])
    if not todo:
        return problems

    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    scratch_dir = work_dir / "verify_scratch"
    shutil.rmtree(scratch_dir, ignore_errors=True)
    scratch = Warehouse(scratch_dir, threads=threads, memory_limit=memory_limit, work_dir=work_dir)
    try:
        c = scratch.con
        c.execute(f"ATTACH '{source_file(warehouse, Warehouse.database)}' AS loaded (READ_ONLY)")
        for snap in todo:
            sid = snap.snapshot_id
            as_of = snap.as_of_date.isoformat()
            for s in STREAMS:
                if s.name not in loaded_streams.get(sid, set()):
                    continue
                path = extract_member(snap, s.name, work_dir)
                try:
                    st = scratch._stage(s, path, sid)
                finally:
                    path.unlink(missing_ok=True)
                if st is None:
                    problems.append(f"{sid} {s.name}: warehouse has the stream but the file has no rows")
                    continue
                try:
                    data = ", ".join(col.name for col in s.columns)
                    expected = (f"SELECT h.npi, n.version_hash, q.row_seq, {', '.join(typed_columns(s)[0])} "
                                f"FROM {st.rows_table} h JOIN {st.seq_table} q ON q.rid = h.rowid "
                                f"JOIN {st.key_table} n ON n.npi = h.npi")
                    actual = (f"SELECT npi, version_hash, row_seq, {data} FROM loaded.{s.name}_version "
                              f"WHERE effective_date <= DATE '{as_of}' AND (end_date IS NULL OR end_date > DATE '{as_of}')")
                    c.execute(f"CREATE OR REPLACE TEMP TABLE _missing AS {expected} EXCEPT ALL {actual}")
                    c.execute(f"CREATE OR REPLACE TEMP TABLE _extra AS {actual} EXCEPT ALL {expected}")
                    n_missing = c.execute("SELECT count(*) FROM _missing").fetchone()[0]
                    n_extra = c.execute("SELECT count(*) FROM _extra").fetchone()[0]
                    label = f"{sid} {s.name}: {st.n_rows:,} rows match source"
                    if not n_missing and not n_extra:
                        log.info("%-70s %s", label, "ok")
                        continue
                    both = "SELECT npi FROM _missing UNION ALL SELECT npi FROM _extra"
                    n_npis = c.execute(f"SELECT count(DISTINCT npi) FROM ({both})").fetchone()[0]
                    sample = [r[0] for r in c.execute(f"SELECT DISTINCT npi FROM ({both}) ORDER BY npi LIMIT 3").fetchall()]
                    log.info("%-70s %s", label, f"FAIL {n_missing:,} missing, {n_extra:,} extra")
                    problems.append(f"{sid} {s.name}: {n_missing:,} source rows missing from the warehouse, "
                                    f"{n_extra:,} warehouse rows not in the source ({n_npis:,} NPIs; e.g. {', '.join(sample)})")
                finally:
                    for t in (st.rows_table, st.seq_table, st.key_table, "_missing", "_extra"):
                        c.execute(f"DROP TABLE IF EXISTS {t}")
    finally:
        scratch.close()
        shutil.rmtree(scratch_dir, ignore_errors=True)
    return problems
