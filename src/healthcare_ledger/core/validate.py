"""Integrity checks on a loaded warehouse.

Invariants, per stream (NPPES streams keyed by ``npi``; Medicare PPEF streams keyed by ``enrollment_id``):

* (key, effective_date, row_seq) is unique.
* Every key has at most one open version, and the open versions are exactly
  the rows of the state table.
* is_current <=> end_date IS NULL <=> close_reason IS NULL.
* end_date > effective_date.
* Consecutive versions of a key never overlap.
* Reconstructing each loaded snapshot by interval lookup yields exactly the
  number of keys that snapshot's file contained.

The streams and their keys come from each loaded versioned source (``Source.validate_streams``); each published table
(``core.published``) must have the columns its source declares; plus each
loaded source's ``check_problems``: the privacy invariants (no tax id or SSN in any version table;
``sources/nppes/scrub.py`` for NPPES), for PPEF and POS the shape of their code columns, and for a
reference source (AHRQ) the shape of its tables. ``export`` runs the versioned sources' checks too and
refuses to write if any fail.

A POS stream has a third close reason, ``moved`` (the CCN went to the other POS file in the same
quarter), so for it the versions closed at a snapshot are changed + absent + moved.
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb

from .. import sources
from . import published
from .connect import open_catalog

log = logging.getLogger(__name__)


def validate(warehouse: Path) -> list[str]:
    con = open_catalog(warehouse, read_only=True)
    problems: list[str] = []

    def check(desc: str, sql: str | None, expect=0, got=None) -> None:
        if sql is not None:
            got = con.execute(sql).fetchone()[0]
        if got != expect:
            problems.append(f"{desc}: got {got}, expected {expect}")
        log.info("%-70s %s", desc, "ok" if got == expect else f"FAIL ({got})")

    def check_stream(name: str, key: str, bookkeeping: str, one_row_per_key: bool) -> None:
        v = f"{name}_version"
        if con.execute(f"SELECT count(*) FROM {v}").fetchone()[0] == 0:
            return
        check(f"{v}: duplicate ({key}, effective_date, row_seq)",
              f"SELECT count(*) FROM (SELECT {key}, effective_date, row_seq FROM {v} GROUP BY ALL HAVING count(*) > 1)")
        check(f"{v}: keys with more than one open version",
              f"SELECT count(*) FROM (SELECT {key} FROM {v} WHERE is_current AND row_seq = 1 GROUP BY {key} HAVING count(*) > 1)")
        check(f"{v}: open versions not matching state table",
              f"""SELECT (SELECT count(*) FROM (
                    SELECT {key}, version_hash FROM {v} WHERE is_current AND row_seq = 1
                    EXCEPT SELECT {key}, version_hash FROM {name}_state))
                  + (SELECT count(*) FROM (SELECT {key}, version_hash FROM {name}_state
                     EXCEPT SELECT {key}, version_hash FROM {v} WHERE is_current AND row_seq = 1))""")
        check(f"{v}: is_current / end_date / close_reason inconsistency",
              f"""SELECT count(*) FROM {v}
                  WHERE is_current <> (end_date IS NULL) OR is_current <> (close_reason IS NULL)""")
        check(f"{v}: end_date not after effective_date",
              f"SELECT count(*) FROM {v} WHERE end_date <= effective_date")
        check(f"{v}: overlapping consecutive versions",
              f"""SELECT count(*) FROM (
                    SELECT {key}, end_date,
                           lead(effective_date) OVER (PARTITION BY {key} ORDER BY effective_date) AS next_eff
                    FROM {v} WHERE row_seq = 1)
                  WHERE next_eff IS NOT NULL AND (end_date IS NULL OR next_eff < end_date)""")
        if one_row_per_key:
            check(f"{v}: versions with more than one row (duplicate {'NPI' if key == 'npi' else key} rows in a CMS file)",
                  f"SELECT count(*) FROM {v} WHERE row_seq > 1")
        last = con.execute(f"SELECT max(snapshot_id) FROM {bookkeeping} WHERE stream = '{name}'").fetchone()[0]
        check(f"{v}: open versions whose last_snapshot_id is not the latest snapshot ({last})",
              f"SELECT count(*) FROM {v} WHERE is_current AND last_snapshot_id <> {last}")
        check(f"{v}: last_snapshot_id before first_snapshot_id",
              f"SELECT count(*) FROM {v} WHERE last_snapshot_id < first_snapshot_id")
        n_keys_col = "n_npis" if bookkeeping == "snapshot_stream" else "n_keys"
        moved_col = "n_moved" if "n_moved" in {r[0] for r in con.execute(f"DESCRIBE {bookkeeping}").fetchall()} else "0"
        snaps = con.execute(
            f"""SELECT snapshot_id, {n_keys_col}, n_new, n_changed, n_absent + {moved_col}, csv_mode
                FROM {bookkeeping} WHERE stream = '{name}' ORDER BY 1""").fetchall()
        # One pass each for the opened/closed counts of every snapshot, not two scans per snapshot.
        opened = dict(con.execute(
            f"SELECT first_snapshot_id, count(*) FROM {v} WHERE row_seq = 1 GROUP BY 1").fetchall())
        closed = dict(con.execute(
            f"SELECT strftime(end_date, '%Y%m%d')::INTEGER, count(*) FROM {v} "
            f"WHERE row_seq = 1 AND end_date IS NOT NULL GROUP BY 1").fetchall())
        for sid, n_keys, n_new, n_changed, n_absent, csv_mode in snaps:
            d = f"{sid // 10000:04d}-{sid // 100 % 100:02d}-{sid % 100:02d}"
            if csv_mode != "strict":
                log.warning("%s snapshot %s was parsed with the %s CSV reader; inspect it", v, sid, csv_mode)
            check(f"{v}: reconstruct snapshot {sid} key count",
                  f"""SELECT count(*) FROM {v} WHERE row_seq = 1
                      AND effective_date <= DATE '{d}' AND (end_date IS NULL OR end_date > DATE '{d}')""",
                  expect=n_keys)
            check(f"{v}: versions opened at {sid} = new + changed", None, expect=n_new + n_changed,
                  got=opened.get(sid, 0))
            check(f"{v}: versions closed at {sid} = changed + absent{' + moved' if moved_col != '0' else ''}", None,
                  expect=n_changed + n_absent, got=closed.get(sid, 0))

    present = [src for src in sources.SOURCES if src.present(con)]
    for src in (s for s in present if s.versioned):    # a reference source has no version tables
        for stream, key, bookkeeping, one_row_per_key in src.validate_streams():
            check_stream(stream, key, bookkeeping, one_row_per_key=one_row_per_key)
    for src in (s for s in present if s.versioned):
        for t in src.tables():
            try:
                published.dictionary_rows(con, t)
            except (ValueError, duckdb.Error) as e:
                problems.append(f"published table {t.name}: {e}")
    for src in present:
        found = src.check_problems(con)
        problems += found
        log.info("%-70s %s", f"{src.title} {'version tables: privacy and column shapes' if src.versioned else 'tables: shapes'}", "ok" if not found else f"FAIL ({len(found)} check(s))")
    con.close()
    return problems
