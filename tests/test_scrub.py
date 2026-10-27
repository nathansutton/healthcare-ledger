"""Tax ids and SSNs never reach the warehouse or the export, whatever the CMS file says."""

import duckdb
import pytest

from conftest import catalog, main_row, write_zip
from healthcare_ledger.cli import main
from healthcare_ledger.core.export import export_all
from healthcare_ledger.sources.nppes.schema import ENDPOINT, OTHER_NAME, PRACTICE_LOCATION
from healthcare_ledger.core.privacy import license_formats
from healthcare_ledger.sources.nppes.scrub import privacy_problems
from test_scd import load_all


def _org(npi="1000000001", **over):
    base = dict(
        entity_type="2", org_name="ACME CLINIC", ein="12-3456789", parent_org_tin="98-7654321",
        other_id_1="M12345", other_id_type_1="05", other_id_state_1="CO", other_id_issuer_1="BCBS",  # other ids are not read
        other_id_2="123456789", other_id_type_2="01", other_id_issuer_2="TAX ID",
        license_number_1="12-3456789", license_state_1="OH",       # formatted EIN -> NULL, state kept
        license_number_2="036123456", license_state_2="IL",        # Illinois physician format -> kept
        license_number_3="041.123456", license_state_3="IL",       # same, with a dot -> kept
        license_number_4="123456789", license_state_4="NY",        # nine digits, no New York format starts 123 -> NULL
        license_number_5="A-123-45-6789", license_state_5="TN",    # formatted SSN inside -> NULL
        license_number_6="RBT-12-3456789", license_state_6="FL",   # behavior technician number, a known format: kept
        license_number_7="010123456", license_state_7="CA",        # known in CA and in DC, but one number
        license_number_8="010123456", license_state_8="DC",        # under two states is an SSN -> both NULL
        license_number_9="A12345", license_state_9="CO",           # ordinary license -> kept
        license_number_10="XQ123456789", license_state_10="CO",    # nine digits in an unknown format -> NULL
        license_number_11="123.456780", license_state_11="IL",     # not a token, but the same nine digits
        license_number_12="1234-56780", license_state_12="IN",     # under two states -> both NULL
        license_number_13="222.333444", license_state_13="WI",     # not a token, one state -> kept
        license_number_14="123.45.6789", license_state_14="IL",    # SSN grouping with dots -> NULL
        ao_credential="CPA 123.456789",                            # nine digits however separated -> NULL
        other_org_name="ACME 123.45-6789",                         # SSN grouping, mixed separators -> redacted
        ao_last_name="SMITH 123-456-789",                          # nine digits in a name, however grouped -> redacted
        practice_address_2="PO BOX 1234 60611",                    # box number + ZIP in an address: kept
        credential="MD 123456789",                                 # number in a credential -> NULL
        other_credential="M.D.",                                   # kept
        parent_org_name="123456789",                               # the parent's TIN typed as its name -> redacted
        ao_title="OWNER TIN 12-3456789",                           # redacted in place
        mail_address_1="123456789 987654321 MAIN ST",              # two tokens in a row: both redacted
        mail_address_2="SUITE 100",
        practice_address_1="PO BOX 60611-1234",                    # ZIP+4 is not a token
        mail_postal_code="606111234", practice_phone="3125551212", # numbers by design: untouched
    )
    base.update(over)
    return main_row(npi, **base)


def _slots(con, fields: tuple[str, ...], n: int) -> set[tuple]:
    """Populated slots of a repeating group as a set, since slots are stored in canonical order (slots.py)."""
    parts = [f"SELECT {', '.join(f'{f}_{i}' for f in fields)} FROM provider_version WHERE "
             + " OR ".join(f"{f}_{i} IS NOT NULL" for f in fields) for i in range(1, n + 1)]
    return set(con.execute(" UNION ALL ".join(parts)).fetchall())


def test_allowlist_has_the_formats_the_tests_rely_on():
    known = set(license_formats())
    assert {"IL:036999999", "IL:041999999", "CA:010999999", "DC:010999999", "FL:RBT-99-9999999"} <= known
    assert "NY:123999999" not in known and "CO:XQ999999999" not in known


def test_rules_apply_at_staging(source, tmp_path):
    write_zip(source, "20200101", "20200112", [_org()])
    con = load_all(source, tmp_path)
    assert con.execute("SELECT ein, parent_org_tin, parent_org_name, ao_title, org_name, credential, other_credential, "
                       "mail_address_1, mail_address_2, practice_address_1, mail_postal_code, practice_phone FROM provider_version").fetchone() == (
        None, None, "[REDACTED]", "OWNER TIN [REDACTED]", "ACME CLINIC", None, "M.D.",
        "[REDACTED] [REDACTED] MAIN ST", "SUITE 100", "PO BOX 60611-1234", "606111234", "3125551212")
    assert not any(r[0].startswith("other_id") for r in con.execute("DESCRIBE provider_version").fetchall())
    assert _slots(con, ("license_number", "license_state"), 15) == {
        (None, "OH"), ("036123456", "IL"), ("041.123456", "IL"), (None, "NY"), (None, "TN"),
        ("RBT-12-3456789", "FL"), (None, "CA"), (None, "DC"), ("A12345", "CO"), (None, "CO"),
        (None, "IL"), (None, "IN"), ("222.333444", "WI"), (None, "IL")}
    assert con.execute("SELECT ao_credential, other_org_name, ao_last_name, practice_address_2 FROM provider_version").fetchone() == (
        None, "ACME [REDACTED]", "SMITH [REDACTED]", "PO BOX 1234 60611")
    assert privacy_problems(con) == []


def test_suppressed_content_never_makes_a_version(source, tmp_path):
    write_zip(source, "20200101", "20200112", [_org()])
    # every suppressed field changes, nothing published changes -> still one version
    write_zip(source, "20200201", "20200209", [_org(ein="11-1111111", parent_org_tin="22-2222222", other_id_1="X999",
                                                   other_id_issuer_1="AETNA", other_id_3="M1", other_id_type_3="05",
                                                   license_number_1="55-5555555", license_number_4="987654321",
                                                   credential="MD 987654321", parent_org_name="987654321")])
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
    db = tmp_path / "warehouse"
    assert main(["load", "--source", str(source), "--warehouse", str(db), "--work", str(tmp_path / "work")]) == 0
    assert main(["validate", "--warehouse", str(db)]) == 0
    con = catalog(db)
    con.execute("UPDATE provider_version SET ein = '12-3456789', license_number_1 = '123456789', "
                "license_state_1 = 'NY', credential = 'MD123456789', ao_title = 'TIN 12-3456789'")
    problems = privacy_problems(con)
    con.close()
    assert sorted(p.split(":")[1].strip() for p in problems) == sorted([
        "ein is populated", "license_number_1 is a possible SSN or tax id",
        "credential holds nine digits", "ao_title holds a tax-id token"])
    assert main(["validate", "--warehouse", str(db)]) == 1
    with pytest.raises(RuntimeError, match="privacy"):
        export_all(db, tmp_path / "export")


def test_export_drops_other_identifiers_and_documents_rules(source, tmp_path):
    write_zip(source, "20200101", "20200112", [_org()])
    db = tmp_path / "warehouse"
    assert main(["load", "--source", str(source), "--warehouse", str(db), "--work", str(tmp_path / "work")]) == 0
    out = tmp_path / "export"
    export_all(db, out)
    con = duckdb.connect()
    assert con.execute(f"SELECT parent_org_name FROM read_parquet('{out}/parquet/provider/*/*.parquet')").fetchone() == ("[REDACTED]",)
    assert not (out / "parquet" / "provider_other_identifier").exists()
    assert "provider_other_identifier" not in (out / "SHA256SUMS").read_text()
    dd = (out / "data_dictionary.csv").read_text()
    assert "ein" not in {r.split(",")[1] for r in dd.splitlines()} and "parent_org_tin" not in dd     # always NULL: not published
    assert "other_id" not in dd
    assert "provider_taxonomy,license_number,VARCHAR,Provider License Number_N," in dd and "known license format" in dd
    audit = (out / "privacy_audit.csv").read_text()
    assert "FAIL" not in audit and "provider_taxonomy,license_number," in audit
    assert "privacy_audit.csv" in (out / "SHA256SUMS").read_text()


def _row(stream, **values):
    """A supplemental-stream row keyed by CMS header, from canonical column names."""
    hdr = {c.name: c.cms_header for c in stream.columns}
    return {"NPI": "1000000001", **{hdr[k]: v for k, v in values.items()}}


def test_supplemental_streams_are_scrubbed(source, tmp_path):
    write_zip(source, "20200101", "20200112", [main_row("1000000001", entity_type="2", org_name="ACME")],
              endpoint=[_row(ENDPOINT, endpoint="12-3456789", endpoint_desc="https://fhir.example.org/r4", endpoint_type="CONNECT")],
              othername=[_row(OTHER_NAME, other_org_name="ACME 123-45-6789")])
    con = load_all(source, tmp_path)
    assert con.execute("SELECT endpoint, endpoint_desc, endpoint_type FROM endpoint_version").fetchall() == [
        ("[REDACTED]", "https://fhir.example.org/r4", "CONNECT")]
    assert con.execute("SELECT other_org_name FROM other_name_version").fetchall() == [("ACME [REDACTED]",)]
    assert privacy_problems(con) == []


def test_privacy_gate_covers_supplemental_tables(source, tmp_path):
    write_zip(source, "20200101", "20200112", [main_row("1000000001", entity_type="2", org_name="ACME")],
              endpoint=[_row(ENDPOINT, endpoint="https://fhir.example.org/r4", affiliation_name="ACME")])
    db = tmp_path / "warehouse"
    assert main(["load", "--source", str(source), "--warehouse", str(db), "--work", str(tmp_path / "work")]) == 0
    assert main(["validate", "--warehouse", str(db)]) == 0
    con = catalog(db)
    con.execute("UPDATE endpoint_version SET affiliation_name = '98-7654321'")
    problems = privacy_problems(con)
    con.close()
    assert len(problems) == 1 and "endpoint.affiliation_name" in problems[0]
    assert main(["validate", "--warehouse", str(db)]) == 1
    with pytest.raises(RuntimeError, match="privacy"):
        export_all(db, tmp_path / "export")


def test_phone_numbers_are_not_tax_ids(source, tmp_path):
    write_zip(source, "20200101", "20200112", [main_row("1000000001", entity_type="2", org_name="ACME")],
              pl=[_row(PRACTICE_LOCATION, phone="3035551212", fax="303-555-1212", postal_code="802021234")])
    con = load_all(source, tmp_path)
    assert con.execute("SELECT phone, fax, postal_code FROM practice_location_version").fetchall() == [("3035551212", "303-555-1212", "802021234")]
    assert privacy_problems(con) == []
