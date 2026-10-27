"""The SCD Type 2 engine on toy sources: classification, multi-row versions, hash_optional columns, the
absent guard, the 'moved' hook and rollback to the exact pre-load state."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import pytest

from conftest import main_row, write_zip
from healthcare_ledger.core import engine
from healthcare_ledger.core.engine import Staged, Stats, VersionedWarehouse
from healthcare_ledger.sources.nppes.loader import Warehouse as NppesWarehouse
from healthcare_ledger.core.columns import Column
from healthcare_ledger.sources.nppes.schema import OTHER_NAME
from healthcare_ledger.sources.nppes.snapshots import discover
from test_pos import pos_source  # noqa: F401  (fixture)
from test_ppef import ppef_source  # noqa: F401  (fixture)


@dataclass(frozen=True)
class ToyStream:
    name: str
    key_header: str
    columns: tuple[Column, ...]
    always_present: bool = False


MAIN = ToyStream("toy_main", "ID", (Column("name", "NAME"), Column("grp", "GRP"),
                                    Column("note", "NOTE", hash_optional=True)), always_present=True)
SUB = ToyStream("toy_sub", "ID", (Column("city", "CITY"),))
OTHER = ToyStream("toy_other", "ID", (Column("name", "NAME"), Column("grp", "GRP")))


@dataclass
class ToySnapshot:
    as_of_date: date
    members: dict[str, Path] = field(default_factory=dict)

    @property
    def snapshot_id(self) -> int:
        return int(self.as_of_date.strftime("%Y%m%d"))

    @property
    def label(self) -> str:
        return self.as_of_date.isoformat()

    def has(self, stream: str) -> bool:
        return stream in self.members


class ToyWarehouse(VersionedWarehouse):
    """PPEF-shaped: an always-present main stream, a multi-row sub stream, keys gone are 'absent'."""
    source_label = "TOY"
    database = "toy"
    key = "id"
    key_noun = "ids"
    streams = (MAIN, SUB)
    snapshot_table = "toy_snapshot"
    stream_table = "toy_snapshot_stream"
    single_row_streams = frozenset({MAIN.name})

    def init_bookkeeping(self, con):
        con.execute(f"""CREATE TABLE IF NOT EXISTS {self.snapshot_table} (
            snapshot_id INTEGER NOT NULL, as_of_date DATE NOT NULL, directory VARCHAR NOT NULL,
            streams_present VARCHAR[], loaded_at TIMESTAMP NOT NULL, seconds DOUBLE)""")
        con.execute(f"""CREATE TABLE IF NOT EXISTS {self.stream_table} (
            snapshot_id INTEGER NOT NULL, stream VARCHAR NOT NULL, file VARCHAR NOT NULL, sha256 VARCHAR,
            n_rows BIGINT, n_keys BIGINT, n_new BIGINT, n_changed BIGINT, n_unchanged BIGINT, n_absent BIGINT,
            n_moved BIGINT, csv_mode VARCHAR, content_fingerprint VARCHAR)""")


class MovingWarehouse(ToyWarehouse):
    """POS-shaped: two single-row streams of the same shape; a key that leaves one and is in the other this
    snapshot has 'moved'; the absent guard counts only keys whose group still has rows in the file."""
    streams = (MAIN, OTHER)
    snapshot_table = "mv_snapshot"
    stream_table = "mv_snapshot_stream"
    single_row_streams = frozenset({MAIN.name, OTHER.name})
    gone_kinds = ("absent", "moved")

    def keep_empty(self, stream):
        return False

    def absent_kind_sql(self, stream, staged):
        keys = " UNION ALL ".join(f"SELECT id FROM {st.key_table}" for st in staged)
        return f"CASE WHEN o.id IN ({keys}) THEN 'moved' ELSE 'absent' END"

    def counted_absent(self, st: Staged, delta: str, out: Stats) -> int:
        return self.con.execute(f"""SELECT count(*) FROM {delta} d
            JOIN {st.stream.name}_version v ON v.id = d.id AND v.is_current AND v.row_seq = 1
            WHERE d.kind = 'absent' AND v.grp IN (SELECT grp FROM {st.rows_table})""").fetchone()[0]


def _csv(path: Path, header: str, rows: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join([header, *rows]) + "\n")
    return path


def _snap(tmp: Path, day: date, **files: tuple[str, list[str]]) -> ToySnapshot:
    d = tmp / "src" / day.isoformat()
    return ToySnapshot(day, {f"toy_{n}": _csv(d / f"{n}.csv", h, rows) for n, (h, rows) in files.items()})


D1, D2, D3 = date(2020, 1, 1), date(2020, 2, 1), date(2020, 3, 1)


def _wh(tmp: Path, cls=ToyWarehouse, **kw) -> VersionedWarehouse:
    return cls(tmp / "warehouse", work_dir=tmp / "work", **kw)


def _versions(con, table: str) -> list[tuple]:
    return con.execute(f"SELECT id, effective_date, end_date, is_current, first_snapshot_id, close_reason, row_seq, * "
                       f"EXCLUDE (id, effective_date, end_date, is_current, first_snapshot_id, last_snapshot_id, "
                       f"close_reason, version_hash, row_seq) FROM {table} ORDER BY ALL").fetchall()


def test_new_changed_unchanged_absent_and_multi_row(tmp_path):
    wh = _wh(tmp_path)
    wh.load_snapshot(_snap(tmp_path, D1, main=("ID,NAME,GRP", ["1,ANN,a", "2,BOB,a", "3,CY,b"]),
                                         sub=("ID,CITY", ["1,DENVER", "1,BOISE", "2,MESA"])))
    out = wh.load_snapshot(_snap(tmp_path, D2, main=("ID,NAME,GRP", ["1,ANN,a", "2,ROB,a", "4,DEE,b"]),
                                               sub=("ID,CITY", ["1,BOISE", "1,DENVER", "2,TEMPE"])))
    wh.finalize()
    m, s = out
    assert (m.n_rows, m.n_keys, m.n_new, m.n_changed, m.n_unchanged, m.n_absent) == (3, 3, 1, 1, 1, 1)
    # the order of a key's rows is not a change: 1 keeps its two-row version
    assert (s.n_rows, s.n_keys, s.n_new, s.n_changed, s.n_unchanged, s.n_absent) == (3, 2, 0, 1, 1, 0)
    assert m.file == "main.csv" and len(m.sha256) == 64 and m.content_fingerprint
    c = wh.con
    assert _versions(c, "toy_main_version") == [
        ("1", D1, None, True, 20200101, None, 1, "ANN", "a", None),
        ("2", D1, D2, False, 20200101, "changed", 1, "BOB", "a", None),
        ("2", D2, None, True, 20200201, None, 1, "ROB", "a", None),
        ("3", D1, D2, False, 20200101, "absent", 1, "CY", "b", None),
        ("4", D2, None, True, 20200201, None, 1, "DEE", "b", None)]
    assert _versions(c, "toy_sub_version") == [
        ("1", D1, None, True, 20200101, None, 1, "DENVER"), ("1", D1, None, True, 20200101, None, 2, "BOISE"),
        ("2", D1, D2, False, 20200101, "changed", 1, "MESA"), ("2", D2, None, True, 20200201, None, 1, "TEMPE")]
    assert c.execute("SELECT count(DISTINCT version_hash) FROM toy_sub_version WHERE id = '1'").fetchone()[0] == 1
    # closed versions carry the last snapshot that showed them; finalize stamps the open ones
    assert c.execute("SELECT DISTINCT last_snapshot_id FROM toy_main_version WHERE is_current").fetchall() == [(20200201,)]
    assert c.execute("SELECT last_snapshot_id FROM toy_main_version WHERE close_reason = 'absent'").fetchall() == [(20200101,)]
    assert c.execute("SELECT snapshot_id, streams_present, directory FROM toy_snapshot ORDER BY 1").fetchall() == [
        (20200101, ["toy_main", "toy_sub"], "2020-01-01"), (20200201, ["toy_main", "toy_sub"], "2020-02-01")]
    # stored fingerprints agree with the ones recomputed from the version tables by interval lookup
    assert engine.stored_fingerprints(c, ToyWarehouse) == engine.recompute_fingerprints(c, ToyWarehouse)
    assert engine.loaded(c, ToyWarehouse) and not engine.loaded(c, MovingWarehouse)
    with pytest.raises(ValueError, match="already loaded"):
        wh.load_snapshot(_snap(tmp_path, D2, main=("ID,NAME,GRP", ["1,ANN,a"])))
    with pytest.raises(ValueError, match="load in order"):
        wh.load_snapshot(_snap(tmp_path, date(2020, 1, 15), main=("ID,NAME,GRP", ["1,ANN,a"])))


def test_missing_stream_closes_nothing_and_empty_main_file_is_loaded(tmp_path):
    wh = _wh(tmp_path, max_absent_share=1.0)
    wh.load_snapshot(_snap(tmp_path, D1, main=("ID,NAME,GRP", ["1,ANN,a"]), sub=("ID,CITY", ["1,DENVER"])))
    # no sub file, and an empty one, are both "not present"; an empty main file closes everything
    out = wh.load_snapshot(_snap(tmp_path, D2, main=("ID,NAME,GRP", ["1,ANN,a"])))
    assert [o.stream for o in out] == ["toy_main"]
    out = wh.load_snapshot(_snap(tmp_path, D3, main=("ID,NAME,GRP", []), sub=("ID,CITY", [])))
    assert [(o.stream, o.n_absent) for o in out] == [("toy_main", 1)]
    assert wh.con.execute("SELECT is_current FROM toy_sub_version").fetchall() == [(True,)]


def test_hash_optional_column_versions_only_where_it_is_set(tmp_path):
    wh = _wh(tmp_path)
    wh.load_snapshot(_snap(tmp_path, D1, main=("ID,NAME,GRP", ["1,ANN,a", "2,BOB,a"])))
    # the column appears, NULL for everyone: no change
    out = wh.load_snapshot(_snap(tmp_path, D2, main=("ID,NAME,GRP,NOTE", ["1,ANN,a,", "2,BOB,a,"])))
    assert (out[0].n_changed, out[0].n_unchanged) == (0, 2)
    out = wh.load_snapshot(_snap(tmp_path, D3, main=("ID,NAME,GRP,NOTE", ["1,ANN,a,hi", "2,BOB,a,"])))
    assert (out[0].n_changed, out[0].n_unchanged) == (1, 1)


def test_unmapped_header_stops_the_load(tmp_path):
    wh = _wh(tmp_path)
    with pytest.raises(ValueError, match=r"unmapped columns \['EXTRA'\]"):
        wh.load_snapshot(_snap(tmp_path, D1, main=("ID,NAME,GRP,EXTRA", ["1,ANN,a,x"])))
    assert wh.loaded_snapshot_ids() == []


def test_absent_guard_refuses_and_leaves_the_warehouse_alone(tmp_path):
    wh = _wh(tmp_path)
    rows = [f"{i},N{i},a" for i in range(1000)]
    wh.load_snapshot(_snap(tmp_path, D1, main=("ID,NAME,GRP", rows), sub=("ID,CITY", [f"{i},X" for i in range(1000)])))
    # a multi-row stream is not guarded: all of sub may go; 6% of main may not
    with pytest.raises(RuntimeError, match=r"60 of 1,000 ids would be marked absent \(6.0% > 5%\)"):
        wh.load_snapshot(_snap(tmp_path, D2, main=("ID,NAME,GRP", rows[60:]), sub=("ID,CITY", ["1,X"])))
    assert wh.loaded_snapshot_ids() == [20200101]
    assert wh.con.execute("SELECT count(*) FROM toy_main_version WHERE NOT is_current").fetchone()[0] == 0
    assert wh.con.execute("SELECT count(*) FROM toy_sub_state").fetchone()[0] == 1000
    wh.load_snapshot(_snap(tmp_path, D2, main=("ID,NAME,GRP", rows[40:]), sub=("ID,CITY", ["1,X"])))
    assert wh.con.execute("SELECT count(*) FROM toy_sub_state").fetchone()[0] == 1


def test_moved_between_streams_and_group_aware_guard(tmp_path):
    wh = _wh(tmp_path, MovingWarehouse)
    a = [f"{i},N{i},a" for i in range(1100)]
    b = [f"b{i},B{i},b" for i in range(200)]
    wh.load_snapshot(_snap(tmp_path, D1, main=("ID,NAME,GRP", a + b)))
    # every group-b key leaves main: 60 reach the other stream (moved), 140 reach neither (absent but not
    # counted, since no group-b row is left in main: counted they would be 11% and refused); one group-a
    # key is absent and counted
    other = b[:60]
    out = wh.load_snapshot(_snap(tmp_path, D2, main=("ID,NAME,GRP", a[1:]), other=("ID,NAME,GRP", other)))
    m, o = out
    assert (m.n_absent, m.n_moved, m.n_unchanged) == (141, 60, 1099)
    assert (o.n_new, o.n_absent, o.n_moved) == (60, 0, 0)
    c = wh.con
    assert c.execute("SELECT close_reason, count(*) FROM toy_main_version WHERE NOT is_current GROUP BY 1 ORDER BY 1").fetchall() == [
        ("absent", 141), ("moved", 60)]
    assert c.execute("SELECT count(*) FROM toy_main_state").fetchone()[0] == 1099
    assert c.execute("SELECT n_moved FROM mv_snapshot_stream WHERE stream = 'toy_main' AND snapshot_id = 20200201").fetchone() == (60,)
    # 60 group-a absences of 1,099 keys is over 5%: refused
    with pytest.raises(RuntimeError, match=r"60 of 1,099 ids would be marked absent"):
        wh.load_snapshot(_snap(tmp_path, D3, main=("ID,NAME,GRP", a[61:]), other=("ID,NAME,GRP", other)))
    assert wh.loaded_snapshot_ids() == [20200101, 20200201]
    # a key back in main from the other stream is new there and has moved out of the other one
    out = wh.load_snapshot(_snap(tmp_path, D3, main=("ID,NAME,GRP", a[1:] + b[:1]), other=("ID,NAME,GRP", other[1:])))
    assert (out[0].n_new, out[1].n_moved, out[1].n_absent) == (1, 1, 0)
    assert c.execute("SELECT count(*) FROM toy_other_state").fetchone()[0] == 59


def _dump(con, spec) -> dict[str, list[tuple]]:
    """Every row of every table of ``spec``; the snapshot table without its wall-clock columns."""
    tables = [spec.stream_table] + [f"{s.name}_{x}" for s in spec.streams for x in ("version", "state")]
    out = {t: con.execute(f"SELECT * FROM {t} ORDER BY ALL").fetchall() for t in tables}
    out[spec.snapshot_table] = con.execute(f"SELECT * EXCLUDE (loaded_at, seconds) FROM {spec.snapshot_table} ORDER BY ALL").fetchall()
    return out


@pytest.mark.parametrize("cls", [ToyWarehouse, MovingWarehouse])
def test_rollback_restores_the_exact_pre_load_state(tmp_path, cls):
    wh = _wh(tmp_path, cls, max_absent_share=1.0)
    # the second stream: toy_sub (multi-row, PPEF-shaped) or toy_other (where keys move to, POS-shaped)
    second = {ToyWarehouse: lambda rows: {"sub": ("ID,CITY", [f"{r},X" for r in rows])},
              MovingWarehouse: lambda rows: {"other": ("ID,NAME,GRP", [f"{r},X,c" for r in rows])}}[cls]
    wh.load_snapshot(_snap(tmp_path, D1, main=("ID,NAME,GRP", ["1,ANN,a", "2,BOB,a", "3,CY,b"]), **second(["9"])))
    wh.load_snapshot(_snap(tmp_path, D2, main=("ID,NAME,GRP", ["1,ANN,a", "2,ROB,a"])))
    wh.finalize()
    before = _dump(wh.con, cls)
    # the third snapshot changes, closes (absent, or moved for key 1 in the POS shape) and opens versions in both streams
    third = _snap(tmp_path, D3, main=("ID,NAME,GRP", ["2,ROBERT,a", "4,DEE,b"]), **second(["1"]))
    wh.load_snapshot(third)
    wh.finalize()
    after = _dump(wh.con, cls)
    assert after != before
    if cls is MovingWarehouse:
        assert ("moved",) in wh.con.execute("SELECT close_reason FROM toy_main_version").fetchall()
    removed = engine.rollback(wh.con, cls, 20200201)
    assert removed == {"toy_main": 2, f"toy_{next(iter(second([])))}": 1, "snapshots": 1}
    assert _dump(wh.con, cls) == before
    # and the rolled-back snapshot loads again to the same result
    wh.load_snapshot(third)
    wh.finalize()
    assert _dump(wh.con, cls).keys() == after.keys()
    assert {t: r for t, r in _dump(wh.con, cls).items() if t != cls.snapshot_table} == \
           {t: r for t, r in after.items() if t != cls.snapshot_table}


def test_reset_drops_only_the_spec_tables(tmp_path):
    wh = _wh(tmp_path)
    wh.load_snapshot(_snap(tmp_path, date(2020, 1, 15), main=("ID,NAME,GRP", ["1,ANN,a"])))
    wh.con.execute("CREATE TABLE unrelated (x INTEGER)")
    dropped = engine.reset(wh.con, ToyWarehouse)
    assert sorted(dropped) == sorted(["toy_snapshot", "toy_snapshot_stream", "toy_main_version", "toy_main_state",
                                      "toy_sub_version", "toy_sub_state"])
    assert {r[0] for r in wh.con.execute("SELECT table_name FROM information_schema.tables").fetchall()} == {"unrelated"}
    assert not engine.loaded(wh.con, ToyWarehouse)
    engine.finalize(wh.con, ToyWarehouse)     # a no-op on a warehouse without the source


def test_post_stage_hook_rewrites_before_hashing(tmp_path):
    class Upper(ToyWarehouse):
        def post_stage(self, stream, stg):
            if stream is MAIN:
                self.con.execute(f"UPDATE {stg} SET name = upper(name)")
    wh = _wh(tmp_path, Upper)
    wh.load_snapshot(_snap(tmp_path, D1, main=("ID,NAME,GRP", ["1,ann,a"])))
    out = wh.load_snapshot(_snap(tmp_path, D2, main=("ID,NAME,GRP", ["1,ANN,a"])))
    assert out[0].n_unchanged == 1
    assert wh.con.execute("SELECT name FROM toy_main_version").fetchall() == [("ANN",)]


@pytest.mark.parametrize("source", ["ppef", "pos"])
def test_rollback_of_the_real_sources(tmp_path, request, source):
    """PPEF (multi-row sub-files, the clean-extract and missing-file rules) and POS (CCNs that move between the
    QIES and iQIES files): roll back to the first quarter and get exactly the warehouse that quarter alone gave."""
    from healthcare_ledger.sources.pos.pos import PosWarehouse, discover_pos
    from healthcare_ledger.sources.ppef import PpefWarehouse, discover_ppef
    cls, discover = {"ppef": (PpefWarehouse, discover_ppef), "pos": (PosWarehouse, discover_pos)}[source]
    snaps = discover(request.getfixturevalue(f"{source}_source"))
    wh = cls(tmp_path / "warehouse", work_dir=tmp_path / "work")
    wh.load_snapshot(snaps[0])
    wh.finalize()
    before = _dump(wh.con, cls)
    for snap in snaps[1:]:
        wh.load_snapshot(snap)
    wh.finalize()
    reasons = {r[0] for s in cls.streams for r in wh.con.execute(f"SELECT close_reason FROM {s.name}_version").fetchall()}
    assert reasons >= ({"changed", "absent", "moved"} if source == "pos" else {"changed", "absent"})
    removed = engine.rollback(wh.con, cls, snaps[0].snapshot_id)
    assert removed["snapshots"] == len(snaps) - 1
    assert _dump(wh.con, cls) == before


def test_nppes_post_stage_scrubs_before_hashing(source, tmp_path):
    """NPPES shape: scrub and type-requires-name run in post_stage, before the row hash, so a tax id in a
    suppressed or dropped column, or a type code next to a placeholder name, creates no version."""
    write_zip(source, "20200101", "20200112", [main_row("1000000001", entity_type="2", org_name="ACME")])
    write_zip(source, "20200201", "20200209", [main_row("1000000001", entity_type="2", org_name="ACME", ein="12-3456789",
                                                        other_id_1="123456789", other_id_issuer_1="TAX ID",
                                                        other_org_name="<UNAVAIL>", other_org_name_type="6")])
    wh = NppesWarehouse(tmp_path / "warehouse", work_dir=tmp_path / "work")
    outs = [wh.load_snapshot(s) for s in discover(source)]
    assert (outs[1][0].n_changed, outs[1][0].n_unchanged) == (0, 1)
    assert wh.con.execute("SELECT count(*), max(ein), max(other_org_name_type) FROM provider_version").fetchone() == (1, None, None)
    assert not any(c.startswith("other_id") for c in wh._columns("provider_version"))


def test_rollback_of_nppes(source, tmp_path):
    """NPPES (zip members, a supplemental stream that appears later, an absent NPI): roll back to the first
    month and get exactly the warehouse that month alone gave."""
    hdr = ["NPI"] + [c.cms_header for c in OTHER_NAME.columns]
    on = lambda npi, name: {hdr[0]: npi, hdr[1]: name, hdr[2]: "3"}
    write_zip(source, "20200101", "20200112", [main_row("1000000001", entity_type="1", last_name="A"),
                                                main_row("1000000002", entity_type="1", last_name="B")],
              othername=[on("1000000001", "ACME")])
    write_zip(source, "20200201", "20200209", [main_row("1000000001", entity_type="1", last_name="C"),
                                                main_row("1000000002", entity_type="1", last_name="B"),
                                                main_row("1000000003", entity_type="1", last_name="D")],
              othername=[on("1000000001", "ACME"), on("1000000001", "BETA")])
    write_zip(source, "20200301", "20200308", [main_row("1000000001", entity_type="1", last_name="C")],
              othername=[on("1000000003", "GAMMA")])
    snaps = discover(source)
    wh = NppesWarehouse(tmp_path / "warehouse", work_dir=tmp_path / "work")
    wh.load_snapshot(snaps[0])
    wh.finalize()
    before = _dump(wh.con, NppesWarehouse)
    for snap in snaps[1:]:
        wh.load_snapshot(snap)
    wh.finalize()
    reasons = {r[0] for s in NppesWarehouse.streams for r in wh.con.execute(f"SELECT close_reason FROM {s.name}_version").fetchall()}
    assert reasons >= {"changed", "absent"}
    removed = engine.rollback(wh.con, NppesWarehouse, snaps[0].snapshot_id)
    assert removed == {"provider": 2, "practice_location": 0, "other_name": 3, "endpoint": 0, "snapshots": 2}
    assert _dump(wh.con, NppesWarehouse) == before


def test_each_source_loads_into_its_own_file(source, tmp_path, request):
    """One snapshot of each versioned source and the reference sources into one warehouse directory: a file each,
    each holding only its source's tables, and the catalog sees all of them."""
    import duckdb

    from conftest import catalog, write_ahrq, write_medicaid
    from healthcare_ledger import sources
    write_zip(source, "20200101", "20200112", [main_row("1000000001", entity_type="1", last_name="A")])
    dirs = {"nppes": source, "ppef": request.getfixturevalue("ppef_source"), "pos": request.getfixturevalue("pos_source"),
            "ahrq": write_ahrq(tmp_path / "ahrq"), "medicaid": write_medicaid(tmp_path / "medicaid")}
    warehouse = tmp_path / "warehouse"
    for src in sources.SOURCES:
        if src.versioned:
            wh = src.warehouse_cls(warehouse, work_dir=tmp_path / "work")
            wh.load_snapshot(src.discover(dirs[src.name])[0])
            wh.close()
        else:
            src.load(dirs[src.name], warehouse, tmp_path / "work")
        if src.name == "nppes":
            assert [p.name for p in warehouse.iterdir()] == ["nppes.duckdb"]
    assert sorted(p.name for p in warehouse.iterdir()) == ["ahrq.duckdb", "medicaid.duckdb", "nppes.duckdb", "pos.duckdb",
                                                                    "ppef.duckdb"]
    tables = {}
    for src in sources.SOURCES:
        con = duckdb.connect(str(warehouse / f"{src.database}.duckdb"), read_only=True)
        tables[src.name] = {r[0] for r in con.execute("SELECT table_name FROM duckdb_tables()").fetchall()}
        con.close()
        if src.versioned:
            cls = src.warehouse_cls
            assert tables[src.name] == {cls.snapshot_table, cls.stream_table} | {f"{s.name}_{x}" for s in cls.streams
                                                                                for x in ("version", "state")}
        else:
            assert tables[src.name] and all(t.startswith(f"{src.name}_") for t in tables[src.name])
    names = list(tables.values())
    assert not any(a & b for i, a in enumerate(names) for b in names[i + 1:])
    con = catalog(warehouse, read_only=True)
    assert all(src.loaded(con) for src in sources.SOURCES)
    con.close()
