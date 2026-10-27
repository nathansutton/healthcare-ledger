import urllib.error
from datetime import date

import pytest

from conftest import main_row, write_zip
from healthcare_ledger.sources.nppes import download as dl
from healthcare_ledger.core.errors import UserError
from healthcare_ledger.sources.nppes.snapshots import discover


def test_candidate_names_cover_cms_conventions():
    assert dl.candidate_names(2015, 9) == [
        "NPPES_Data_Dissemination_September_2015_V2.zip", "NPPES_Data_Dissemination_September_2015.zip",
        "NPPES_Data_Dissemination_Sep_2015.zip", "NPPES_Data_Dissemination_Sept_2015.zip"]
    assert "NPPES_Data_Dissemination_Jan_2016.zip" in dl.candidate_names(2016, 1)


@pytest.mark.parametrize("name,ym", [
    ("NPPES_Data_Dissemination_Sept_2015.zip", (2015, 9)),
    ("NPPES_Data_Dissemination_Apr_2016.zip", (2016, 4)),
    ("NPPES_Data_Dissemination_October_2026_V2.zip", (2026, 10)),
    ("NPPES_Data_Dissemination_feb_2008.zip", (2008, 2)),
    ("NPPES_Data_Dissemination_100526_101126_Weekly_V2.zip", None),
    ("NPPES_Deactivated_NPI_Report_101226_V2.zip", None),
])
def test_parse_month(name, ym):
    assert dl.parse_month(name) == ym


def test_local_months_accepts_both_conventions(tmp_path):
    for n in ["NPPES_Data_Dissemination_20160801.zip", "NPPES_Data_Dissemination_Sept_2015.zip",
              "NPPES_Data_Dissemination_May_2026_V2.zip", "NPPES_Data_Dissemination_June_2026_V2.zip.part"]:
        (tmp_path / n).write_bytes(b"PK")
    assert sorted(dl.local_months(tmp_path)) == [(2015, 9), (2016, 8), (2026, 5)]


def test_discover_rejects_html_saved_as_zip(source):
    write_zip(source, "20200101", "20200112", [main_row("1000000001", entity_type="1")])
    (source / "NPPES_Data_Dissemination_February_2020.zip").write_text("<HTML>Access Denied</HTML>")
    with pytest.raises(ValueError, match="not a zip file"):
        discover(source)


class _Resp:
    def __init__(self, headers):
        self.headers = headers

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _urlopen(answers):
    """Fake urlopen returning (or raising) successive answers; records each URL."""
    seen = []

    def fake(req, timeout=None):
        seen.append(req.full_url)
        a = answers.pop(0) if isinstance(answers, list) else answers(req.full_url)
        if isinstance(a, Exception):
            raise a
        return _Resp(a)
    return fake, seen


def _http(code):
    return urllib.error.HTTPError("u", code, "x", {}, None)


@pytest.fixture
def no_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr(dl.time, "sleep", slept.append)
    return slept


def test_head_html_page_is_a_block_not_a_file(monkeypatch, no_sleep):
    fake, _ = _urlopen([{"Content-Type": "text/html", "Content-Length": "312"}])
    monkeypatch.setattr(dl.urllib.request, "urlopen", fake)
    with pytest.raises(UserError, match="Access Denied"):
        dl.head(dl.BASE + "x.zip")


def test_head_absent_and_present(monkeypatch, no_sleep):
    fake, _ = _urlopen([_http(404), {"Content-Type": "application/zip"}, {"Content-Length": "9"}])
    monkeypatch.setattr(dl.urllib.request, "urlopen", fake)
    assert dl.head(dl.BASE + "a") is None
    assert dl.head(dl.BASE + "b") == 0  # present, size unknown
    assert dl.head(dl.BASE + "c") == 9


def test_head_network_error_retries_then_user_error(monkeypatch, no_sleep):
    fake, seen = _urlopen([urllib.error.URLError("dns"), TimeoutError("t"), _http(503)])
    monkeypatch.setattr(dl.urllib.request, "urlopen", fake)
    with pytest.raises(UserError, match="could not reach CMS"):
        dl.head(dl.BASE + "a")
    assert len(seen) == 3


def test_probe_skips_local_months(tmp_path, monkeypatch, no_sleep):
    (tmp_path / "NPPES_Data_Dissemination_September_2026_V2.zip").write_bytes(b"PK")
    fake, seen = _urlopen(lambda url: {"Content-Length": "5"} if "October_2026_V2" in url else _http(404))
    monkeypatch.setattr(dl.urllib.request, "urlopen", fake)
    rows = dl.probe(tmp_path, date(2026, 9, 1), date(2026, 10, 1), use_cdx=False)
    assert [(r[0], r[1], r[2], r[4]) for r in rows] == [
        (2026, 9, "NPPES_Data_Dissemination_September_2026_V2.zip", None),
        (2026, 10, None, "NPPES_Data_Dissemination_October_2026_V2.zip")]
    assert all("September" not in u for u in seen)


def test_fetch_does_not_sleep_after_last_try(tmp_path, monkeypatch, no_sleep):
    monkeypatch.setattr(dl.urllib.request, "urlopen", lambda req, timeout=None: (_ for _ in ()).throw(OSError("reset")))
    assert dl.fetch(tmp_path, "x.zip", 0, tries=3) is None
    assert no_sleep == [120, 240]


def test_month_arg():
    assert dl.month_arg("2025-03") == date(2025, 3, 1)
    for bad in ["2025", "2025-3", "25-03-01"]:
        with pytest.raises(UserError, match="YYYY-MM"):
            dl.month_arg(bad)
