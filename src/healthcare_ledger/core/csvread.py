"""Read a CMS CSV into a DuckDB temp table, trying progressively looser parsers.

CMS files are mostly clean, but over 20 years they include ragged rows, latin-1 files, data rows
wider than the header and UTF-8 files with a few latin-1 bytes. Every loader reads through
``read_csv_into`` so the fallbacks, and the ``csv_mode`` recorded per file, mean the same thing for
every source. Must be called outside a transaction: DuckDB aborts the transaction on the first
failed statement, which would defeat the fallback chain.
"""

from __future__ import annotations

import csv
import logging
from collections.abc import Callable
from pathlib import Path

import duckdb

log = logging.getLogger(__name__)

BASE = "header=true, all_varchar=true, delim=',', quote='\"', escape='\"'"
LENIENT = "strict_mode=false, null_padding=true, parallel=false, max_line_size=20000000"


def read_csv_into(con: duckdb.DuckDBPyConnection, table: str, path: Path, *, work_dir: Path | None = None,
                  scratch: list[Path] | None = None,
                  explicit_header_if: Callable[[duckdb.DuckDBPyConnection, str], bool] | None = None,
                  repair: bool = False) -> str:
    """Create temp table ``table`` from ``path``; returns the csv_mode that worked.

    ``explicit_header_if(con, table)`` is checked after a successful read; when true the file is re-read
    with the header row as explicit names (``read_with_explicit_header``). ``repair=True`` adds a last
    resort for UTF-8 files with stray latin-1 bytes: a repaired copy is written to ``work_dir`` and
    appended to ``scratch`` for the caller to delete.
    """
    base = f"'{path}', {BASE}"
    # UTF-8 readers come before latin-1 ones: a latin-1 fallback on a UTF-8 file would silently
    # mojibake every multibyte character and create spurious versions.
    attempts = [
        ("strict", f"read_csv({base})"),
        ("lenient", f"read_csv({base}, {LENIENT})"),
        ("strict-latin1", f"read_csv({base}, encoding='latin-1')"),
        ("lenient-latin1", f"read_csv({base}, {LENIENT}, encoding='latin-1')"),
    ]
    last: Exception | None = None
    for mode, reader in attempts:
        try:
            con.execute(f"CREATE OR REPLACE TEMP TABLE {table} AS SELECT * FROM {reader}")
        except duckdb.Error as e:  # noqa: PERF203
            last = e
            log.debug("%s reader failed on %s: %s", mode, path.name, str(e).splitlines()[0])
            continue
        if explicit_header_if is not None and explicit_header_if(con, table):
            # Data rows wider than the header row (CMS pl_pfile, Jul 2018: two stray trailing
            # fields). DuckDB then stops trusting the header; re-read with explicit names.
            mode = read_with_explicit_header(con, table, path)
        if mode != "strict":
            log.warning("%s parsed with %s reader", path.name, mode)
        return mode
    if not repair:
        raise RuntimeError(f"could not parse {path}") from last
    # The April 2025 PPEF file is UTF-8 with nine latin-1 bytes in it (a no-break space after a name, an
    # accented initial). DuckDB 1.5 fails on it in every mode, so repair it line by line into UTF-8.
    repaired = Path(work_dir) / f"{path.name}.utf8.csv"
    n = repair_utf8(path, repaired)
    if scratch is not None:
        scratch.append(repaired)
    try:
        con.execute(f"CREATE OR REPLACE TEMP TABLE {table} AS SELECT * FROM read_csv('{repaired}', {BASE})")
    except duckdb.Error as e:
        raise RuntimeError(f"could not parse {path}, even after repairing {n} non-UTF-8 line(s)") from (last or e)
    log.warning("%s parsed after repairing %d non-UTF-8 line(s) (decoded as latin-1)", path.name, n)
    return f"repaired-utf8+{n}"


def read_with_explicit_header(con: duckdb.DuckDBPyConnection, table: str, path: Path) -> str:
    """Re-read ``path`` naming the columns from its header row, plus ``_extra_N`` for fields beyond it."""
    with open(path, newline="", encoding="utf-8", errors="replace") as fh:
        header = next(csv.reader(fh))
    opts = "header=false, skip=1, all_varchar=true, null_padding=true, strict_mode=false, parallel=false, max_line_size=20000000"
    width = len(con.execute(f"DESCRIBE SELECT * FROM read_csv('{path}', {opts})").fetchall())
    extra = max(0, width - len(header))
    names = header + [f"_extra_{i}" for i in range(1, extra + 1)]
    names_sql = "[" + ", ".join("'" + n.replace("'", "''") + "'" for n in names) + "]"
    con.execute(f"CREATE OR REPLACE TEMP TABLE {table} AS SELECT * FROM read_csv('{path}', {opts}, names={names_sql})")
    return f"explicit-header+{extra}extra"


def repair_utf8(src: Path, dest: Path) -> int:
    """Copy ``src`` to ``dest`` as UTF-8, decoding each line that is not valid UTF-8 as latin-1. Returns that count."""
    bad = 0
    with open(src, "rb") as fin, open(dest, "wb") as fout:
        for line in fin:
            try:
                line.decode("utf-8")
            except UnicodeDecodeError:
                bad += 1
                line = line.decode("latin-1").encode("utf-8")
            fout.write(line)
    return bad
