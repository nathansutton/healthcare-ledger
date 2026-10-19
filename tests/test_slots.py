"""Canonical slot order: a CMS reshuffle of the repeating groups is not a change, and the stored
order is a total order of the slot contents (empties dropped, sorted, primary taxonomy first)."""

from __future__ import annotations

import duckdb

from conftest import main_row, write_zip
from nppes_over_time import slots
from nppes_over_time.cli import main
from nppes_over_time.schema import PROVIDER, Stream
from nppes_over_time.verify import verify
from test_scd import load_all

TWO_TAX = dict(taxonomy_code_1="207Q00000X", license_number_1="L1", license_state_1="CO", primary_taxonomy_1="N",
               taxonomy_code_2="208D00000X", license_number_2="L2", license_state_2="TX", primary_taxonomy_2="Y", taxonomy_group_2="G")
TWO_IDS = dict(other_id_1="M1", other_id_type_1="05", other_id_state_1="CO",
               other_id_2="U2", other_id_type_2="02", other_id_issuer_2="UPIN")


def _swap(fields: dict[str, str], groups=(("taxonomy_code", "license_number", "license_state", "primary_taxonomy", "taxonomy_group"),
                                          ("other_id", "other_id_type", "other_id_state", "other_id_issuer"))) -> dict[str, str]:
    out = dict(fields)
    for g in groups:
        for f in g:
            a, b = out.pop(f"{f}_1", None), out.pop(f"{f}_2", None)
            if a is not None:
                out[f"{f}_2"] = a
            if b is not None:
                out[f"{f}_1"] = b
    return out


def test_reshuffled_slots_are_not_a_new_version(source, tmp_path):
    base = dict(entity_type="1", last_name="SMITH", **TWO_TAX, **TWO_IDS)
    write_zip(source, "20200101", "20200112", [main_row("1000000001", **base)])
    write_zip(source, "20200201", "20200209", [main_row("1000000001", **_swap(base))])
    con = load_all(source, tmp_path)
    assert con.execute("SELECT count(*) FROM provider_version").fetchone()[0] == 1
    assert con.execute("SELECT n_changed FROM snapshot_stream WHERE snapshot_id = 20200209 AND stream = 'provider'").fetchone()[0] == 0
    # stored order: primary taxonomy first; other ids by type code
    row = con.execute("""SELECT taxonomy_code_1, primary_taxonomy_1, taxonomy_group_1, taxonomy_code_2, primary_taxonomy_2,
                                other_id_1, other_id_type_1, other_id_issuer_1, other_id_2, other_id_type_2, other_id_state_2
                         FROM provider_version""").fetchone()
    assert row == ("208D00000X", "Y", "G", "207Q00000X", "N", "U2", "02", "UPIN", "M1", "05", "CO")


def test_empty_slots_are_packed_to_the_front(source, tmp_path):
    write_zip(source, "20200101", "20200112", [
        main_row("1000000001", entity_type="1", taxonomy_code_3="207Q00000X", primary_taxonomy_3="Y", other_id_7="X7", other_id_type_7="01"),
    ])
    con = load_all(source, tmp_path)
    row = con.execute("SELECT taxonomy_code_1, primary_taxonomy_1, taxonomy_code_3, other_id_1, other_id_type_1, other_id_7 FROM provider_version").fetchone()
    assert row == ("207Q00000X", "Y", None, "X7", "01", None)


def test_sort_is_total_and_stable_for_ties(source, tmp_path):
    # the same taxonomy listed twice sorts next to itself; two non-primary codes sort by code then state
    fields = dict(entity_type="1", taxonomy_code_1="208D00000X", license_state_1="TX", primary_taxonomy_1="N",
                  taxonomy_code_2="207Q00000X", license_state_2="CO", primary_taxonomy_2="N",
                  taxonomy_code_3="207Q00000X", license_state_3="AZ", primary_taxonomy_3="N",
                  taxonomy_code_4="207Q00000X", license_state_4="AZ", primary_taxonomy_4="N")
    write_zip(source, "20200101", "20200112", [main_row("1000000001", **fields)])
    con = load_all(source, tmp_path)
    assert con.execute("SELECT taxonomy_code_1, license_state_1, taxonomy_code_2, license_state_2, taxonomy_code_3, license_state_3, "
                       "taxonomy_code_4, license_state_4, taxonomy_code_5 FROM provider_version").fetchone() == (
        "207Q00000X", "AZ", "207Q00000X", "AZ", "207Q00000X", "CO", "208D00000X", "TX", None)


def test_scrub_runs_before_the_sort(source, tmp_path):
    # slot 1 is only a formatted EIN: scrubbed to nothing and dropped. Slot 2 keeps its issuer text
    # under a NULL id. Slot 3 is a real Medicaid id and sorts first by type code.
    write_zip(source, "20200101", "20200112", [
        main_row("1000000001", entity_type="1", other_id_1="12-3456789",
                 other_id_2="123456789", other_id_type_2="01", other_id_issuer_2="TAX ID",
                 other_id_3="M3", other_id_type_3="05", other_id_state_3="CO"),
    ])
    con = load_all(source, tmp_path)
    assert con.execute("SELECT other_id_1, other_id_type_1, other_id_issuer_1, other_id_2, other_id_type_2, other_id_3 FROM provider_version").fetchone() == (
        None, "01", "TAX ID", "M3", "05", None)


def test_verify_accepts_reshuffled_source_months(source, tmp_path):
    base = dict(entity_type="1", **TWO_TAX, **TWO_IDS)
    write_zip(source, "20200101", "20200112", [main_row("1000000001", **base)])
    write_zip(source, "20200201", "20200209", [main_row("1000000001", **_swap(base))])
    db, work = tmp_path / "w.duckdb", tmp_path / "work"
    assert main(["load", "--source", str(source), "--db", str(db), "--work", str(work)]) == 0
    assert verify(db, source, None, work) == []
    assert verify(db, source, [20200112, 20200209], work) == []


def test_export_slot_is_the_canonical_position(source, tmp_path):
    base = dict(entity_type="1", **TWO_TAX, **TWO_IDS)
    write_zip(source, "20200101", "20200112", [main_row("1000000001", **_swap(base))])
    db, out = tmp_path / "w.duckdb", tmp_path / "export"
    assert main(["load", "--source", str(source), "--db", str(db), "--work", str(tmp_path / "work")]) == 0
    assert main(["export", "--db", str(db), "--out", str(out), "--releases", str(tmp_path / "releases")]) == 0
    con = duckdb.connect()
    tax = con.execute(f"SELECT slot, taxonomy_code, primary_taxonomy FROM read_parquet('{out}/parquet/provider_taxonomy/*/*.parquet') ORDER BY slot").fetchall()
    assert tax == [(1, "208D00000X", "Y"), (2, "207Q00000X", "N")]
    ids = con.execute(f"SELECT slot, other_id, other_id_type FROM read_parquet('{out}/parquet/provider_other_identifier/*/*.parquet') ORDER BY slot").fetchall()
    assert ids == [(1, "U2", "02"), (2, "M1", "05")]
    dd = (out / "data_dictionary.csv").read_text()
    assert "canonical" in dd.lower()


def test_canonical_slot_hash_is_pinned(source, tmp_path):
    # Golden hash for a row with slot content: changing the sort rule re-versions every NPI that has slots.
    write_zip(source, "20200101", "20200112", [main_row("1000000001", entity_type="1", last_name="SMITH", **_swap({**TWO_TAX, **TWO_IDS}))])
    con = load_all(source, tmp_path)
    assert con.execute("SELECT version_hash FROM provider_version").fetchone() == ("11c882c99d5837b426956f848144b6f8",)


def test_canonical_select_covers_every_slot_column_once():
    sql = slots.canonical_select(PROVIDER, "t")
    for g in slots.GROUPS:
        for col in g.columns:
            assert sql.count(f" AS {col},") + sql.count(f" AS {col})") == 1, col
    assert slots.canonical_select(Stream("x", "", ()), "t") is None
