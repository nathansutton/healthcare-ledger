"""Integrity checks on a loaded warehouse.

Invariants, per stream:

* (npi, effective_date, row_seq) is unique.
* Every NPI has at most one open version, and the open versions are exactly
  the rows of the state table.
* is_current <=> end_date IS NULL <=> close_reason IS NULL.
* end_date > effective_date.
* Consecutive versions of an NPI never overlap.
* Reconstructing each loaded snapshot by interval lookup yields exactly the
  number of NPIs that snapshot's file contained.

Plus the privacy invariants in ``scrub.py``: no tax id or SSN anywhere in
``provider_version``. ``export`` runs those too and refuses to write if any fail.
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb

from .schema import STREAMS
from .scrub import privacy_problems

log = logging.getLogger(__name__)


def validate(db: Path) -> list[str]:
    con = duckdb.connect(str(db), read_only=True)
    problems: list[str] = []

    def check(desc: str, sql: str | None, expect=0, got=None) -> None:
        if sql is not None:
            got = con.execute(sql).fetchone()[0]
        if got != expect:
            problems.append(f"{desc}: got {got}, expected {expect}")
        log.info("%-70s %s", desc, "ok" if got == expect else f"FAIL ({got})")

    for s in STREAMS:
        v = f"{s.name}_version"
        if con.execute(f"SELECT count(*) FROM {v}").fetchone()[0] == 0:
            continue
        check(f"{v}: duplicate (npi, effective_date, row_seq)",
              f"SELECT count(*) FROM (SELECT npi, effective_date, row_seq FROM {v} GROUP BY ALL HAVING count(*) > 1)")
        check(f"{v}: NPIs with more than one open version",
              f"SELECT count(*) FROM (SELECT npi FROM {v} WHERE is_current AND row_seq = 1 GROUP BY npi HAVING count(*) > 1)")
        check(f"{v}: open versions not matching state table",
              f"""SELECT (SELECT count(*) FROM (
                    SELECT npi, version_hash FROM {v} WHERE is_current AND row_seq = 1
                    EXCEPT SELECT npi, version_hash FROM {s.name}_state))
                  + (SELECT count(*) FROM (SELECT npi, version_hash FROM {s.name}_state
                     EXCEPT SELECT npi, version_hash FROM {v} WHERE is_current AND row_seq = 1))""")
        check(f"{v}: is_current / end_date / close_reason inconsistency",
              f"""SELECT count(*) FROM {v}
                  WHERE is_current <> (end_date IS NULL) OR is_current <> (close_reason IS NULL)""")
        check(f"{v}: end_date not after effective_date",
              f"SELECT count(*) FROM {v} WHERE end_date <= effective_date")
        check(f"{v}: overlapping consecutive versions",
              f"""SELECT count(*) FROM (
                    SELECT npi, end_date,
                           lead(effective_date) OVER (PARTITION BY npi ORDER BY effective_date) AS next_eff
                    FROM {v} WHERE row_seq = 1)
                  WHERE next_eff IS NOT NULL AND (end_date IS NULL OR next_eff < end_date)""")
        if s.name == "provider":
            check(f"{v}: versions with more than one row (duplicate NPI rows in a CMS file)",
                  f"SELECT count(*) FROM {v} WHERE row_seq > 1")
        last = con.execute(f"SELECT max(snapshot_id) FROM snapshot_stream WHERE stream = '{s.name}'").fetchone()[0]
        check(f"{v}: open versions whose last_snapshot_id is not the latest snapshot ({last})",
              f"SELECT count(*) FROM {v} WHERE is_current AND last_snapshot_id <> {last}")
        check(f"{v}: last_snapshot_id before first_snapshot_id",
              f"SELECT count(*) FROM {v} WHERE last_snapshot_id < first_snapshot_id")
        snaps = con.execute(
            f"""SELECT snapshot_id, n_npis, n_new, n_changed, n_absent, csv_mode
                FROM snapshot_stream WHERE stream = '{s.name}' ORDER BY 1""").fetchall()
        # One pass each for the opened/closed counts of every snapshot, not two scans per snapshot.
        opened = dict(con.execute(
            f"SELECT first_snapshot_id, count(*) FROM {v} WHERE row_seq = 1 GROUP BY 1").fetchall())
        closed = dict(con.execute(
            f"SELECT strftime(end_date, '%Y%m%d')::INTEGER, count(*) FROM {v} "
            f"WHERE row_seq = 1 AND end_date IS NOT NULL GROUP BY 1").fetchall())
        for sid, n_npis, n_new, n_changed, n_absent, csv_mode in snaps:
            d = f"{sid // 10000:04d}-{sid // 100 % 100:02d}-{sid % 100:02d}"
            if csv_mode != "strict":
                log.warning("%s snapshot %s was parsed with the %s CSV reader; inspect it", v, sid, csv_mode)
            check(f"{v}: reconstruct snapshot {sid} NPI count",
                  f"""SELECT count(*) FROM {v} WHERE row_seq = 1
                      AND effective_date <= DATE '{d}' AND (end_date IS NULL OR end_date > DATE '{d}')""",
                  expect=n_npis)
            check(f"{v}: versions opened at {sid} = new + changed", None, expect=n_new + n_changed,
                  got=opened.get(sid, 0))
            check(f"{v}: versions closed at {sid} = changed + absent", None, expect=n_changed + n_absent,
                  got=closed.get(sid, 0))
    privacy = privacy_problems(con)
    problems += privacy
    log.info("%-70s %s", "provider_version: tax ids and SSNs suppressed", "ok" if not privacy else f"FAIL ({len(privacy)} check(s))")
    con.close()
    return problems
