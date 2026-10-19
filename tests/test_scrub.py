"""Tax ids and SSNs never reach the warehouse or the export, whatever the CMS file says."""

import duckdb
import pytest

from conftest import main_row, write_zip
from nppes_over_time.cli import main
from nppes_over_time.export import export_all
from nppes_over_time.schema import ENDPOINT, OTHER_NAME, PRACTICE_LOCATION
from nppes_over_time.scrub import privacy_problems
from test_scd import load_all


def _org(npi="1000000001", **over):
    base = dict(
        entity_type="2", org_name="ACME CLINIC", ein="12-3456789", parent_org_tin="98-7654321",
        other_id_1="12-3456789", other_id_type_1="01", other_id_issuer_1="BCBS",            # formatted EIN -> NULL
        other_id_2="123456789", other_id_type_2="01", other_id_issuer_2="AETNA",            # nine digits, payer issuer: kept (could be a payer id)
        other_id_3="123456789", other_id_type_3="05", other_id_issuer_3="",                 # Medicaid: kept
        other_id_4="123456789", other_id_type_4="01", other_id_issuer_4="TAX ID #",         # tax-id issuer + nine digits -> NULL
        other_id_5="12 345 6789", other_id_type_5="01", other_id_issuer_5="MEDICARE ID UNDER OLD TIN 58-1611969",  # issuer says TIN, nine digits -> NULL; issuer redacted
        other_id_6="ABC123", other_id_type_6="01", other_id_issuer_6="TAX ID",              # tax-id issuer but not nine digits: kept
        other_id_7="Q78", other_id_type_7="01", other_id_issuer_7="123456789",              # bare-number issuer: kept (not certain)
        other_id_8="123-45-6789", other_id_type_8="05", other_id_issuer_8="",               # SSN shape in Medicaid slot -> NULL
        other_id_9="W1234", other_id_type_9="01", other_id_issuer_9="FIRST HEALTH",         # payer named FIRST: not a keyword hit
        other_id_10="987654321", other_id_type_10="04", other_id_issuer_10="12-3456789TAX ID",  # keyword only visible after redaction -> NULL
        license_number_1="12-3456789", license_state_1="OH",                                 # EIN shape -> NULL, state kept
        license_number_2="123456789", license_state_2="IL",                                  # nine-digit licence kept
        parent_org_name="123456789",                                                         # nine digits as a name: kept (not certain)
        ao_title="OWNER TIN 12-3456789",                                                     # free text, not an identifier column: kept
    )
    base.update(over)
    return main_row(npi, **base)


def _slots(con, fields: tuple[str, ...], n: int) -> set[tuple]:
    """Populated slots of a repeating group as a set, since slots are stored in canonical order (slots.py)."""
    parts = [f"SELECT {', '.join(f'{f}_{i}' for f in fields)} FROM provider_version WHERE "
             + " OR ".join(f"{f}_{i} IS NOT NULL" for f in fields) for i in range(1, n + 1)]
    return set(con.execute(" UNION ALL ".join(parts)).fetchall())


def test_rules_apply_at_staging(source, tmp_path):
    write_zip(source, "20200101", "20200112", [_org()])
    con = load_all(source, tmp_path)
    assert con.execute("SELECT ein, parent_org_tin, parent_org_name, ao_title, org_name FROM provider_version").fetchone() == (
        None, None, "123456789", "OWNER TIN 12-3456789", "ACME CLINIC")
    assert _slots(con, ("other_id", "other_id_type", "other_id_issuer"), 50) == {
        (None, "01", "BCBS"),                                       # 1: formatted EIN -> NULL
        ("123456789", "01", "AETNA"),                               # 2: kept
        ("123456789", "05", None),                                  # 3: kept
        (None, "01", "TAX ID #"),                                   # 4: -> NULL
        (None, "01", "MEDICARE ID UNDER OLD TIN [REDACTED]"),       # 5: -> NULL, issuer redacted
        ("ABC123", "01", "TAX ID"),                                 # 6: kept
        ("Q78", "01", "123456789"),                                 # 7: kept
        (None, "05", None),                                         # 8: SSN -> NULL, type kept
        ("W1234", "01", "FIRST HEALTH"),                            # 9: kept
        (None, "04", "[REDACTED]TAX ID"),                           # 10: -> NULL
    }
    assert _slots(con, ("license_number", "license_state"), 15) == {(None, "OH"), ("123456789", "IL")}
    assert privacy_problems(con) == []


def test_suppressed_content_never_makes_a_version(source, tmp_path):
    write_zip(source, "20200101", "20200112", [_org()])
    # every suppressed field changes, nothing published changes -> still one version
    write_zip(source, "20200201", "20200209", [_org(ein="11-1111111", parent_org_tin="22-2222222", other_id_1="33-3333333",
                                                   other_id_4="444444444", other_id_5="555555555", license_number_1="55-5555555",
                                                   other_id_8="666-66-6666", other_id_issuer_5="MEDICARE ID UNDER OLD TIN 77-7777777")])
    # a published field changes -> second version
    write_zip(source, "20200301", "20200308", [_org(org_name="ACME CLINIC LLC")])
    con = load_all(source, tmp_path)
    assert con.execute("SELECT list(n_changed ORDER BY snapshot_id) FROM snapshot_stream WHERE stream='provider'").fetchone() == ([0, 0, 1],)
    assert con.execute("SELECT count(*) FROM provider_version").fetchone() == (2,)


def test_lowercase_placeholder_is_null_and_not_a_change(source, tmp_path):
    write_zip(source, "20071101", "20071112", [main_row("1000000001", entity_type="2", org_name="ACME", other_org_name="<unavail>", mail_city="<Blank>")])
    write_zip(source, "20080501", "20080512", [main_row("1000000001", entity_type="2", org_name="ACME", other_org_name="<UNAVAIL>", mail_city="<Blank>")])
    con = load_all(source, tmp_path)
    assert con.execute("SELECT count(*) FROM provider_version").fetchone() == (1,)
    assert con.execute("SELECT other_org_name, mail_city FROM provider_version").fetchone() == (None, "<Blank>")  # mixed case is not a CMS token


def test_validate_and_export_refuse_a_planted_tax_id(source, tmp_path):
    write_zip(source, "20200101", "20200112", [main_row("1000000001", entity_type="1", last_name="A")])
    db = tmp_path / "w.duckdb"
    assert main(["load", "--source", str(source), "--db", str(db), "--work", str(tmp_path / "work")]) == 0
    assert main(["validate", "--db", str(db)]) == 0
    con = duckdb.connect(str(db))
    con.execute("UPDATE provider_version SET ein = '12-3456789', other_id_7 = '123456789', other_id_issuer_7 = 'TAX ID', other_id_issuer_12 = 'OLD TIN 12-3456789'")
    problems = privacy_problems(con)
    con.close()
    assert sorted(p.split(":")[1].strip() for p in problems) == sorted([
        "ein is populated", "other_id_7 is a nine-digit value under a tax-id issuer", "other_id_issuer_12 embeds a formatted EIN or SSN"])
    assert main(["validate", "--db", str(db)]) == 1
    with pytest.raises(RuntimeError, match="privacy"):
        export_all(db, tmp_path / "export")


def test_export_nulls_suppressed_columns_and_documents_rules(source, tmp_path):
    write_zip(source, "20200101", "20200112", [_org()])
    db = tmp_path / "w.duckdb"
    assert main(["load", "--source", str(source), "--db", str(db), "--work", str(tmp_path / "work")]) == 0
    out = tmp_path / "export"
    export_all(db, out)
    con = duckdb.connect()
    assert con.execute(f"SELECT ein, parent_org_tin, parent_org_name FROM read_parquet('{out}/parquet/provider/*/*.parquet')").fetchone() == (None, None, "123456789")
    # slots are in canonical order (slots.py), so compare the exported entries as a set
    ids = set(con.execute(f"SELECT other_id, other_id_issuer FROM read_parquet('{out}/parquet/provider_other_identifier/*/*.parquet')").fetchall())
    assert ids == {(None, "BCBS"), ("123456789", "AETNA"), ("123456789", None), (None, "TAX ID #"),
                   (None, "MEDICARE ID UNDER OLD TIN [REDACTED]"), ("ABC123", "TAX ID"), ("Q78", "123456789"), (None, None),
                   ("W1234", "FIRST HEALTH"), (None, "[REDACTED]TAX ID")}
    assert con.execute(f"SELECT count(*), max(slot) FROM read_parquet('{out}/parquet/provider_other_identifier/*/*.parquet')").fetchone() == (10, 10)
    dd = (out / "data_dictionary.csv").read_text()
    assert "provider,ein,VARCHAR,Employer Identification Number (EIN),\"Suppressed" in dd
    assert "provider_other_identifier,other_id,VARCHAR,Other Provider Identifier_N," in dd and "nine digits" in dd


def _row(stream, **values):
    """A supplemental-stream row keyed by CMS header, from canonical column names."""
    hdr = {c.name: c.cms_header for c in stream.columns}
    return {"NPI": "1000000001", **{hdr[k]: v for k, v in values.items()}}


def test_supplemental_streams_are_scrubbed(source, tmp_path):
    write_zip(source, "20200101", "20200112", [main_row("1000000001", entity_type="2", org_name="ACME")],
              endpoint=[_row(ENDPOINT, endpoint="12-3456789", endpoint_desc="https://fhir.example.org/r4", endpoint_type="CONNECT")],
              othername=[_row(OTHER_NAME, other_org_name="123-45-6789")])
    con = load_all(source, tmp_path)
    assert con.execute("SELECT endpoint, endpoint_desc, endpoint_type FROM endpoint_version").fetchall() == [
        (None, "https://fhir.example.org/r4", "CONNECT")]
    assert con.execute("SELECT other_org_name FROM other_name_version").fetchall() == [(None,)]
    assert privacy_problems(con) == []


def test_privacy_gate_covers_supplemental_tables(source, tmp_path):
    write_zip(source, "20200101", "20200112", [main_row("1000000001", entity_type="2", org_name="ACME")],
              endpoint=[_row(ENDPOINT, endpoint="https://fhir.example.org/r4", affiliation_name="ACME")])
    db = tmp_path / "w.duckdb"
    assert main(["load", "--source", str(source), "--db", str(db), "--work", str(tmp_path / "work")]) == 0
    assert main(["validate", "--db", str(db)]) == 0
    con = duckdb.connect(str(db))
    con.execute("UPDATE endpoint_version SET affiliation_name = '98-7654321'")
    problems = privacy_problems(con)
    con.close()
    assert len(problems) == 1 and "endpoint.affiliation_name" in problems[0]
    assert main(["validate", "--db", str(db)]) == 1
    with pytest.raises(RuntimeError, match="privacy"):
        export_all(db, tmp_path / "export")


def test_phone_numbers_are_not_tax_ids(source, tmp_path):
    write_zip(source, "20200101", "20200112", [main_row("1000000001", entity_type="2", org_name="ACME")],
              pl=[_row(PRACTICE_LOCATION, phone="3035551212", fax="303-555-1212")])
    con = load_all(source, tmp_path)
    assert con.execute("SELECT phone, fax FROM practice_location_version").fetchall() == [("3035551212", "303-555-1212")]
    assert privacy_problems(con) == []
