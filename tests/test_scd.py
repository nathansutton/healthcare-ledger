import dataclasses
from datetime import date
from pathlib import Path

import duckdb
import pytest

from conftest import MAIN_HEADERS, catalog, main_row, write_zip
from healthcare_ledger.sources.nppes import schema
from healthcare_ledger.core import columns, engine
from healthcare_ledger.sources.nppes.loader import Warehouse
from healthcare_ledger.core.columns import Column
from healthcare_ledger.sources.nppes.schema import OTHER_NAME, PRACTICE_LOCATION
from healthcare_ledger.sources.nppes.snapshots import discover

PL_H = ["NPI"] + [c.cms_header for c in PRACTICE_LOCATION.columns]


def pl_row(npi, addr, city="X", **kw):
    r = {h: "" for h in PL_H}
    r["NPI"] = npi
    r[PL_H[1]] = addr
    r[PL_H[3]] = city
    return r


def load_all(source: Path, tmp_path: Path) -> duckdb.DuckDBPyConnection:
    wh = Warehouse(tmp_path, work_dir=tmp_path / "work")
    for s in discover(source):
        wh.load_snapshot(s)
    wh.finalize()
    wh.close()
    return duckdb.connect(str(tmp_path / "nppes.duckdb"), read_only=True)


def test_full_lifecycle(source, tmp_path):
    # S1: A, B, C.  S2: A unchanged, B changed, C absent, D new.  S3: C returns, A unchanged.
    write_zip(source, "20200101", "20200112", [
        main_row("1000000001", entity_type="1", last_name="SMITH", practice_city="DENVER", enumeration_date="05/23/2005"),
        main_row("1000000002", entity_type="1", last_name="JONES", practice_city="BOISE"),
        main_row("1000000003", entity_type="2", org_name="ACME CLINIC"),
    ])
    write_zip(source, "20200201", "20200209", [
        main_row("1000000001", entity_type="1", last_name="SMITH", practice_city="DENVER", enumeration_date="05/23/2005"),
        main_row("1000000002", entity_type="1", last_name="JONES", practice_city="BOISE", mail_phone="3035551212"),
        main_row("1000000004", entity_type="1", last_name="NEW"),
    ])
    write_zip(source, "20200301", "20200308", [
        main_row("1000000001", entity_type="1", last_name="SMITH", practice_city="DENVER", enumeration_date="05/23/2005"),
        main_row("1000000002", entity_type="1", last_name="JONES", practice_city="BOISE", mail_phone="3035551212"),
        main_row("1000000003", deactivation_date="02/15/2020"),  # deactivation stub row
        main_row("1000000004", entity_type="1", last_name="NEW"),
    ])
    con = load_all(source, tmp_path)

    rows = con.execute("""SELECT npi, effective_date, end_date, is_current, first_snapshot_id, last_snapshot_id, close_reason
                          FROM provider_version ORDER BY npi, effective_date""").fetchall()
    assert rows == [
        ("1000000001", date(2020, 1, 12), None, True, 20200112, 20200308, None),
        ("1000000002", date(2020, 1, 12), date(2020, 2, 9), False, 20200112, 20200112, "changed"),
        ("1000000002", date(2020, 2, 9), None, True, 20200209, 20200308, None),
        ("1000000003", date(2020, 1, 12), date(2020, 2, 9), False, 20200112, 20200112, "absent"),
        ("1000000003", date(2020, 3, 8), None, True, 20200308, 20200308, None),
        ("1000000004", date(2020, 2, 9), None, True, 20200209, 20200308, None),
    ]
    # the deactivation stub is a real version with the date typed
    assert con.execute("""SELECT entity_type, deactivation_date FROM provider_version
                          WHERE npi='1000000003' AND is_current""").fetchone() == (None, date(2020, 2, 15))
    # the mailing-phone change (not in docwatch's old 11-column hash) produced a new version
    assert con.execute("SELECT mail_phone FROM provider_version WHERE npi='1000000002' AND is_current").fetchone() == ("3035551212",)
    # a change-level mutation such as a punctuation-only edit WOULD count; the other way round, trimming does not
    stats = con.execute("SELECT snapshot_id, n_new, n_changed, n_unchanged, n_absent FROM snapshot_stream WHERE stream='provider' ORDER BY 1").fetchall()
    assert stats == [(20200112, 3, 0, 0, 0), (20200209, 1, 1, 1, 1), (20200308, 1, 0, 3, 0)]
    # as-of reconstruction matches each snapshot's NPI count
    for sid, d, n in [(20200112, "2020-01-12", 3), (20200209, "2020-02-09", 3), (20200308, "2020-03-08", 4)]:
        got = con.execute(f"SELECT count(*) FROM provider_version WHERE effective_date <= DATE '{d}' AND (end_date IS NULL OR end_date > DATE '{d}')").fetchone()[0]
        assert got == n, sid


def test_old_vintage_headers_and_rename(source, tmp_path):
    # 2007-style file: 187 columns, "Provider Gender Code"; later file renames to Sex and adds columns.
    old_headers = [h.replace("Provider Sex Code", "Provider Gender Code") for h in MAIN_HEADERS[:187]]
    r = main_row("1000000009", entity_type="1", last_name="OLD", sex="F")
    r["Provider Gender Code"] = r.pop("Provider Sex Code")
    write_zip(source, "20071101", "20071112", [r], headers=old_headers, subdir="SR 1234/")
    write_zip(source, "20080501", "20080512", [main_row("1000000009", entity_type="1", last_name="OLD", sex="F")])
    write_zip(source, "20081101", "20081110", [main_row("1000000009", entity_type="1", last_name="OLD", sex="F", sole_proprietor="N")])
    con = load_all(source, tmp_path)
    rows = con.execute("SELECT effective_date, end_date, sex, sole_proprietor FROM provider_version ORDER BY effective_date").fetchall()
    # adding blank columns does not create a version; a populated new column does
    assert rows == [(date(2007, 11, 12), date(2008, 11, 10), "F", None), (date(2008, 11, 10), None, "F", "N")]
    assert con.execute("SELECT main_csv_columns FROM snapshot ORDER BY 1").fetchall() == [(187,), (330,), (330,)]


def test_whitespace_and_empty_are_equal(source, tmp_path):
    write_zip(source, "20200101", "20200112", [main_row("1000000001", entity_type="1", last_name=" SMITH ", sex="")])
    write_zip(source, "20200201", "20200209", [main_row("1000000001", entity_type="1", last_name="SMITH", sex=" ")])
    con = load_all(source, tmp_path)
    assert con.execute("SELECT count(*) FROM provider_version").fetchone() == (1,)
    assert con.execute("SELECT last_name, sex FROM provider_version").fetchone() == ("SMITH", None)


def test_duplicate_zip_is_skipped(source, tmp_path):
    write_zip(source, "20100201", "20100208", [main_row("1000000001", entity_type="1")])
    write_zip(source, "20100401", "20100208", [main_row("1000000001", entity_type="1")])
    snaps = discover(source)
    assert [s.snapshot_id for s in snaps] == [20100208]


def test_supplemental_streams_set_semantics(source, tmp_path):
    base = [main_row("1000000001", entity_type="1"), main_row("1000000002", entity_type="1")]
    # 2018: no supplemental files at all -> stream skipped
    write_zip(source, "20180101", "20180107", base)
    # 2019-01: pl present, endpoint zero-byte -> endpoint skipped
    write_zip(source, "20190101", "20190113", base, pl=[pl_row("1000000001", "1 MAIN ST"), pl_row("1000000001", "2 OAK AVE")], empty_endpoint=True)
    # 2019-02: same two locations in different order -> unchanged; NPI 2 gains one
    write_zip(source, "20190201", "20190210", base, pl=[pl_row("1000000001", "2 OAK AVE"), pl_row("1000000001", "1 MAIN ST"), pl_row("1000000002", "9 ELM")])
    # 2019-03: NPI 1 drops to one location -> changed; NPI 2 absent from pl -> closed
    write_zip(source, "20190301", "20190310", base, pl=[pl_row("1000000001", "1 MAIN ST")])
    con = load_all(source, tmp_path)
    rows = con.execute("""SELECT npi, effective_date, end_date, close_reason, address_1
                          FROM practice_location_version ORDER BY npi, effective_date, address_1""").fetchall()
    assert rows == [
        ("1000000001", date(2019, 1, 13), date(2019, 3, 10), "changed", "1 MAIN ST"),
        ("1000000001", date(2019, 1, 13), date(2019, 3, 10), "changed", "2 OAK AVE"),
        ("1000000001", date(2019, 3, 10), None, None, "1 MAIN ST"),
        ("1000000002", date(2019, 2, 10), date(2019, 3, 10), "absent", "9 ELM"),
    ]
    # row_seq is a dense 1..n within each version (order itself carries no meaning)
    assert con.execute("""SELECT list_sort(list(row_seq)) FROM practice_location_version
                          WHERE npi='1000000001' AND effective_date=DATE '2019-01-13'""").fetchone() == ([1, 2],)
    assert con.execute("SELECT count(*) FROM endpoint_version").fetchone() == (0,)
    present = con.execute("SELECT snapshot_id, streams_present FROM snapshot ORDER BY 1").fetchall()
    assert present[0] == (20180107, ["provider"]) and present[1] == (20190113, ["provider", "practice_location"])
    # provider stream untouched by supplemental churn
    assert con.execute("SELECT count(*) FROM provider_version").fetchone() == (2,)


def test_out_of_order_load_rejected(source, tmp_path):
    write_zip(source, "20200101", "20200112", [main_row("1000000001", entity_type="1")])
    write_zip(source, "20200201", "20200209", [main_row("1000000001", entity_type="1")])
    s1, s2 = discover(source)
    wh = Warehouse(tmp_path, work_dir=tmp_path / "work")
    wh.load_snapshot(s2)
    with pytest.raises(ValueError):
        wh.load_snapshot(s1)
    with pytest.raises(ValueError):
        wh.load_snapshot(s2)


def test_header_only_supplemental_member_is_not_absence(source, tmp_path):
    base = [main_row("1000000001", entity_type="1")]
    write_zip(source, "20190101", "20190113", base, pl=[pl_row("1000000001", "1 MAIN ST")])
    write_zip(source, "20190201", "20190210", base, pl=[])  # header line only
    write_zip(source, "20190301", "20190310", base, pl=[pl_row("1000000001", "1 MAIN ST")])
    con = load_all(source, tmp_path)
    assert con.execute("SELECT count(*), bool_and(is_current) FROM practice_location_version").fetchone() == (1, True)
    assert con.execute("SELECT streams_present FROM snapshot WHERE snapshot_id=20190210").fetchone() == (["provider"],)


def test_mass_absence_tripwire(source, tmp_path):
    write_zip(source, "20200101", "20200112", [main_row(f"1000{i:06d}", entity_type="1") for i in range(2000)])
    write_zip(source, "20200201", "20200209", [main_row(f"1000{i:06d}", entity_type="1") for i in range(1800)])  # 10% vanish
    s1, s2 = discover(source)
    wh = Warehouse(tmp_path, work_dir=tmp_path / "work")
    wh.load_snapshot(s1)
    with pytest.raises(RuntimeError, match="absent"):
        wh.load_snapshot(s2)
    assert wh.loaded_snapshot_ids() == [20200112]  # rolled back
    wh.close()
    wh = Warehouse(tmp_path, work_dir=tmp_path / "work", max_absent_share=1.0)
    wh.load_snapshot(s2)
    assert wh.loaded_snapshot_ids() == [20200112, 20200209]


def test_duplicate_npi_rows_flagged_by_validate(source, tmp_path):
    from healthcare_ledger.core.validate import validate
    write_zip(source, "20200101", "20200112", [main_row("1000000001", entity_type="1", last_name="A"),
                                                main_row("1000000001", entity_type="1", last_name="B")])
    load_all(source, tmp_path).close()
    problems = validate(tmp_path)
    assert any("duplicate NPI rows" in p for p in problems) and len(problems) == 1


def test_v2_reissue_wins_tie(source, tmp_path):
    write_zip(source, "20260301", "20260308", [main_row("1000000001", entity_type="1")])
    (source / "NPPES_Data_Dissemination_20260301.zip").rename(source / "NPPES_Data_Dissemination_March_2026.zip")
    write_zip(source, "20260301", "20260308", [main_row("1000000001", entity_type="1")])
    (source / "NPPES_Data_Dissemination_20260301.zip").rename(source / "NPPES_Data_Dissemination_March_2026_V2.zip")
    snaps = discover(source)
    assert len(snaps) == 1 and snaps[0].zip_path.name.endswith("_V2.zip")


def test_zip_date_from_name_conventions():
    from datetime import date as d
    from healthcare_ledger.sources.nppes.snapshots import zip_date_from_name as z
    assert z("NPPES_Data_Dissemination_20260201.zip") == d(2026, 2, 1)
    assert z("NPPES_Data_Dissemination_October_2026_V2.zip") == d(2026, 10, 1)
    assert z("NPPES_Data_Dissemination_Sept_2015.zip") == d(2015, 9, 1)
    assert z("NPPES_Data_Dissemination_Apr_2016.zip") == d(2016, 4, 1)
    assert z("NPPES_Data_Dissemination_feb_2008.zip") == d(2008, 2, 1)
    assert z("NPPES_Data_Dissemination_100526_101126_Weekly_V2.zip") is None
    assert z("NPPES_Deactivated_NPI_Report_101226_V2.zip") is None
    assert z("NPPES_Data_Dissemination_Smarch_2015.zip") is None


def test_extract_member_with_unzip_matches_zipfile(source, tmp_path):
    import shutil
    from healthcare_ledger.sources.nppes.snapshots import extract_member, extract_member_with_unzip
    if shutil.which("unzip") is None:
        pytest.skip("unzip not installed")
    write_zip(source, "20200101", "20200112", [main_row("1000000001", entity_type="1", last_name="A")])
    (snap,) = discover(source)
    a = extract_member(snap, "provider", tmp_path / "w1")
    b = extract_member_with_unzip(snap.zip_path, snap.members["provider"], tmp_path / "w2" / "x.csv") if (tmp_path / "w2").mkdir() is None else None
    assert a.read_bytes() == b.read_bytes()


def test_wider_data_rows_than_header_are_tolerated(source, tmp_path):
    # CMS pl_pfile, Jul-Nov 2018: 10 header fields, 12 fields per data row (timestamp + empty).
    hdr = ",".join(f'"{h}"' for h in PL_H)
    raw = (hdr + "\n" + '"1000000001","1 MAIN ST","","DENVER","CO","80202","US","3035551212","","","06/19/2018 13:06",""\n'
           + '"1000000001","2 OAK AVE","","DENVER","CO","80202","US","","","","06/19/2018 13:06",""\n').encode()
    base = [main_row("1000000001", entity_type="1")]
    write_zip(source, "20180701", "20180708", base, raw_members={"pl_pfile_20050523-{as_of}.csv": raw})
    write_zip(source, "20180801", "20180812", base, pl=[pl_row("1000000001", "1 MAIN ST", "DENVER"), pl_row("1000000001", "2 OAK AVE", "DENVER")])
    con = load_all(source, tmp_path)
    july = con.execute("""SELECT address_1, postal_code, phone FROM practice_location_version
                          WHERE effective_date = DATE '2018-07-08' ORDER BY address_1""").fetchall()
    assert july == [("1 MAIN ST", "80202", "3035551212"), ("2 OAK AVE", "80202", None)]
    # the stray trailing fields never reach the table
    assert "_extra_1" not in [r[0] for r in con.execute("DESCRIBE practice_location_version").fetchall()]
    assert con.execute("SELECT csv_mode FROM snapshot_stream WHERE stream='practice_location' ORDER BY snapshot_id").fetchall() == [("explicit-header+2extra",), ("strict",)]


def test_failed_reader_attempt_does_not_poison_snapshot(source, tmp_path, monkeypatch):
    # DuckDB aborts a transaction on the first failed statement. A reader attempt that fails
    # (as the strict sniffer does on the May 2019 endpoint file) must therefore happen outside
    # the snapshot transaction, or the fallback and the whole snapshot are lost.
    original = engine.read_csv_into

    def flaky(con, table, path, **kw):
        try:
            con.execute("SELECT * FROM read_csv('/nonexistent/definitely-missing.csv')")
        except Exception:
            pass
        return original(con, table, path, **kw)

    monkeypatch.setattr(engine, "read_csv_into", flaky)
    write_zip(source, "20190501", "20190512", [main_row("1000000001", entity_type="1")])
    write_zip(source, "20190601", "20190609", [main_row("1000000001", entity_type="1", last_name="X")])
    con = load_all(source, tmp_path)
    assert con.execute("SELECT count(*) FROM snapshot").fetchone() == (2,)
    assert con.execute("SELECT count(*) FROM provider_version").fetchone() == (2,)


def test_init_schema_adds_new_canonical_columns(source, tmp_path):
    import duckdb
    from healthcare_ledger.sources.nppes.schema import OTHER_NAME
    con = duckdb.connect(str(tmp_path / "nppes.duckdb"))
    meta = ", ".join(f"{n} {t}" for n, t in __import__("healthcare_ledger.sources.nppes.loader", fromlist=["META_COLUMNS"]).META_COLUMNS)
    con.execute(f"CREATE TABLE other_name_version ({meta}, other_org_name VARCHAR, other_org_name_type VARCHAR)")
    con.close()
    wh = Warehouse(tmp_path, work_dir=tmp_path / "work")
    cols = [r[0] for r in wh.con.execute("DESCRIBE other_name_version").fetchall()]
    assert cols[-len(OTHER_NAME.columns):] == [c.name for c in OTHER_NAME.columns]
    wh.close()


def test_placeholders_become_null(source, tmp_path):
    from healthcare_ledger.core.columns import is_code_column
    assert is_code_column("license_state_3") and is_code_column("practice_state") and is_code_column("other_id_type_7")
    assert not is_code_column("last_name") and not is_code_column("ein") and not is_code_column("license_number_1")
    write_zip(source, "20260901", "20260913", [
        main_row("1000000001", entity_type="2", org_name="ACME", ein="<UNAVAIL>", license_state_1="", other_org_name=""),
        main_row("1000000002", entity_type="1", last_name="NULL", first_name="LAURA", license_state_1="CO"),
    ])
    # Oct 2026 style: NULL written into license state, <UNAVAIL> into other org name -> no new versions
    write_zip(source, "20261001", "20261011", [
        main_row("1000000001", entity_type="2", org_name="ACME", ein="<UNAVAIL>", license_state_1="NULL", other_org_name="<UNAVAIL>"),
        main_row("1000000002", entity_type="1", last_name="NULL", first_name="LAURA", license_state_1="CO"),
    ])
    con = load_all(source, tmp_path)
    assert con.execute("SELECT count(*) FROM provider_version").fetchone() == (2,)
    assert con.execute("SELECT ein, license_state_1, other_org_name FROM provider_version WHERE npi='1000000001'").fetchone() == (None, None, None)
    # the surname Null survives: NULL is only a placeholder in code-valued columns
    assert con.execute("SELECT last_name FROM provider_version WHERE npi='1000000002'").fetchone() == ("NULL",)


@pytest.mark.parametrize("column", ["license_state_1", "last_name"])
def test_null_placeholders_expression(column):
    expr = columns.null_placeholders("x", columns.placeholder_tokens(column))
    values = ["", "<UNAVAIL>", "  <blank>  ", None, "X", " X ", "NULL"]
    got = [duckdb.execute(f"SELECT {expr} FROM (SELECT ?::VARCHAR AS x)", [v]).fetchone()[0] for v in values]
    # the word NULL is a placeholder only in code columns
    assert got == [None, None, None, None, "X", "X", None if columns.is_code_column(column) else "NULL"]


def test_type_code_without_name_is_null(source, tmp_path):
    on_h = ["NPI"] + [c.cms_header for c in OTHER_NAME.columns]
    on = lambda npi, name, typ: {"NPI": npi, on_h[1]: name, on_h[2]: typ, on_h[3]: ""}
    write_zip(source, "20260901", "20260913", [
        main_row("1000000001", entity_type="2", org_name="ACME"),
        main_row("1000000002", entity_type="2", org_name="BETA", other_org_name="BETA DBA", other_org_name_type="3"),
    ], othername=[on("1000000001", "ACME DBA", "3")])
    # Oct 2026 style: code 6 next to a blank or <UNAVAIL> name -> no new version
    write_zip(source, "20261001", "20261011", [
        main_row("1000000001", entity_type="2", org_name="ACME", other_org_name="<UNAVAIL>", other_org_name_type="6"),
        main_row("1000000002", entity_type="2", org_name="BETA", other_org_name="BETA DBA", other_org_name_type="3"),
    ], othername=[on("1000000001", "ACME DBA", "3"), on("1000000001", "", "5")])
    con = load_all(source, tmp_path)
    assert con.execute("SELECT count(*) FROM provider_version").fetchone() == (2,)
    assert con.execute("SELECT other_org_name_type FROM provider_version WHERE npi='1000000002'").fetchone() == ("3",)
    assert con.execute("SELECT count(*) FROM provider_version WHERE other_org_name_type IS NOT NULL AND other_org_name IS NULL").fetchone() == (0,)
    assert con.execute("SELECT count(*) FROM other_name_version WHERE other_org_name_type IS NOT NULL AND other_org_name IS NULL").fetchone() == (0,)


def test_rollback_reopens_and_allows_insert(source, tmp_path):
    from healthcare_ledger.sources.nppes.loader import rollback
    write_zip(source, "20200101", "20200112", [main_row("1000000001", entity_type="1", last_name="A"), main_row("1000000002", entity_type="1")])
    write_zip(source, "20200301", "20200308", [main_row("1000000001", entity_type="1", last_name="B")])  # 2 absent
    write_zip(source, "20200401", "20200412", [main_row("1000000001", entity_type="1", last_name="B"), main_row("1000000003", entity_type="1")])
    con = load_all(source, tmp_path)
    con.close()
    import duckdb
    con = duckdb.connect(str(tmp_path / "nppes.duckdb"))
    removed = rollback(con, 20200112)
    assert removed == {"provider": 2, "practice_location": 0, "other_name": 0, "endpoint": 0, "snapshots": 2}
    assert con.execute("SELECT count(*), bool_and(is_current), bool_and(end_date IS NULL) FROM provider_version").fetchone() == (2, True, True)
    assert con.execute("SELECT count(*) FROM provider_state").fetchone() == (2,)
    con.close()
    # a newly found February 2020 file can now be inserted, then the rest reloads in order
    write_zip(source, "20200201", "20200209", [main_row("1000000001", entity_type="1", last_name="A"), main_row("1000000002", entity_type="1", sex="F")])
    wh = Warehouse(tmp_path, work_dir=tmp_path / "work")
    done = set(wh.loaded_snapshot_ids())
    for s in discover(source):
        if s.snapshot_id not in done:
            wh.load_snapshot(s)
    wh.finalize()
    assert wh.loaded_snapshot_ids() == [20200112, 20200209, 20200308, 20200412]
    rows = wh.con.execute("SELECT npi, effective_date, end_date, close_reason FROM provider_version ORDER BY npi, effective_date").fetchall()
    assert rows == [
        ("1000000001", date(2020, 1, 12), date(2020, 3, 8), "changed"), ("1000000001", date(2020, 3, 8), None, None),
        ("1000000002", date(2020, 1, 12), date(2020, 2, 9), "changed"), ("1000000002", date(2020, 2, 9), date(2020, 3, 8), "absent"),
        ("1000000003", date(2020, 4, 12), None, None)]
    from healthcare_ledger.core.validate import validate
    wh.close()
    assert validate(tmp_path) == []


def test_version_hash_is_pinned(source, tmp_path):
    # Golden hashes: any change here re-versions every NPI in an existing warehouse.
    write_zip(source, "20200101", "20200112", [
        main_row("1000000001", entity_type="1", last_name="SMITH", practice_city="DENVER", enumeration_date="05/23/2005"),
    ], othername=[{"NPI": "1000000001", "Provider Other Organization Name": "ACME", "Provider Other Organization Name Type Code": "3"}])
    con = load_all(source, tmp_path)
    got = (con.execute("SELECT version_hash FROM provider_version WHERE npi='1000000001'").fetchall(),
           con.execute("SELECT version_hash FROM other_name_version WHERE npi='1000000001'").fetchall())
    assert got == ([("5d482ed9708a167e37c8d0777e1103ff",)], [("23cd5b9ed0aabc1ae2126f5f3a1da945",)])


def _extra_column_n_changed(source, tmp_path, monkeypatch, *, hash_optional: bool) -> list[int]:
    from healthcare_ledger.core.validate import validate
    main = [main_row("1000000001", entity_type="1", last_name="SMITH")]
    base_csv = b'"NPI","Provider Other Organization Name","Provider Other Organization Name Type Code","Created Date"\n"1000000001","ACME","3",""\n'
    extra_csv = (b'"NPI","Provider Other Organization Name","Provider Other Organization Name Type Code","Created Date","Test Extra"\n'
                 b'"1000000001","ACME","3","","X"\n')
    member = "othername_pfile_20050523-{as_of}.csv"
    write_zip(source, "20200101", "20200112", main, raw_members={member: base_csv})
    wh = Warehouse(tmp_path, work_dir=tmp_path / "work")
    for s in discover(source):
        wh.load_snapshot(s)
    wh.close()

    other2 = dataclasses.replace(schema.OTHER_NAME, columns=schema.OTHER_NAME.columns + (
        Column("test_extra", "Test Extra", hash_optional=hash_optional),))
    monkeypatch.setattr(Warehouse, "streams", tuple(other2 if st.name == "other_name" else st for st in schema.STREAMS))

    write_zip(source, "20200201", "20200209", main, raw_members={member: base_csv})
    write_zip(source, "20200301", "20200308", main, raw_members={member: extra_csv})
    wh = Warehouse(tmp_path, work_dir=tmp_path / "work")
    for s in discover(source):
        if s.snapshot_id not in wh.loaded_snapshot_ids():
            wh.load_snapshot(s)
    wh.finalize()
    wh.close()

    assert validate(tmp_path) == []
    con = catalog(tmp_path, read_only=True)
    return [r[0] for r in con.execute("SELECT n_changed FROM snapshot_stream WHERE stream='other_name' ORDER BY snapshot_id").fetchall()]


def test_added_optional_column_is_hash_neutral_until_populated(source, tmp_path, monkeypatch):
    assert _extra_column_n_changed(source, tmp_path, monkeypatch, hash_optional=True) == [0, 0, 1]


def test_added_baseline_column_reversions_everyone(source, tmp_path, monkeypatch):
    # Control: without hash_optional, merely adding the empty column changes every hash.
    assert _extra_column_n_changed(source, tmp_path, monkeypatch, hash_optional=False) == [0, 1, 1]
