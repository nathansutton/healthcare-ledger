"""The source manifest (releases/sources.csv) and the downloader that rebuilds a source directory from it."""

from __future__ import annotations

import hashlib
import io
import urllib.error

import pytest

from conftest import main_row, write_zip
from nppes_over_time import download as dl
from nppes_over_time import sources
from nppes_over_time.cli import main
from nppes_over_time.errors import UserError
from nppes_over_time.export import export_all
from nppes_over_time.sources import Source


@pytest.fixture
def no_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr(dl.time, "sleep", slept.append)
    return slept


class _Body(io.BytesIO):
    def __init__(self, data: bytes, headers: dict, status: int):
        super().__init__(data)
        self.headers, self.status = headers, status

    def __enter__(self):
        return self


class FakeServer:
    """urlopen stand-in serving ``files`` {url: bytes}; honours Range; ``cut`` truncates the next body."""

    def __init__(self, files: dict[str, bytes], etag: bool = True):
        self.files, self.etag, self.cut, self.seen, self.html = files, etag, None, [], set()

    def __call__(self, req, timeout=None):
        url = req.full_url
        self.seen.append((req.get_method(), url, req.headers.get("Range")))
        if url in self.html:
            return _Body(b"<HTML>Access Denied</HTML>", {"Content-Type": "text/html"}, 200)
        if url not in self.files:
            raise urllib.error.HTTPError(url, 404, "nf", {}, None)
        data = self.files[url]
        h = {"Content-Type": "application/zip", "Content-Length": str(len(data))}
        if self.etag:
            h["ETag"] = f'"{hashlib.md5(data).hexdigest()}:1700000000"'
        if req.get_method() == "HEAD":
            return _Body(b"", h, 200)
        rng, status = req.headers.get("Range"), 200
        if rng:
            start = int(rng.split("=")[1].rstrip("-"))
            if start >= len(data):
                raise urllib.error.HTTPError(url, 416, "range", {}, None)
            data, status = data[start:], 206
        if self.cut is not None:
            data, self.cut = data[:self.cut], None
        return _Body(data, {k: v for k, v in h.items() if k != "Content-Length"}, status)


def _zip(tmp_path, zip_date, as_of, name):
    """A real NPPES-shaped zip, its bytes and a manifest row for it."""
    (tmp_path / "build").mkdir(exist_ok=True)
    p = write_zip(tmp_path / "build", zip_date, as_of, [main_row("1000000001", entity_type="1", last_name="A")])
    data = p.read_bytes()
    return data, Source(int(as_of), name, len(data), hashlib.sha256(data).hexdigest(), "cms", dl.BASE + name)


@pytest.fixture
def releases(tmp_path):
    d = tmp_path / "releases"
    d.mkdir()
    return d


def test_manifest_round_trip_and_validation(tmp_path):
    p = tmp_path / "sources.csv"
    rows = [Source(20200209, "NPPES_Data_Dissemination_February_2020.zip", 10, "a" * 64, "cms", dl.BASE + "x"),
            Source(20071112, "NPPES_Data_Dissemination_20071101.zip", 5, "b" * 64, "archive", "")]
    sources.write_sources(p, rows)
    assert [s.snapshot_id for s in sources.read_sources(p)] == [20071112, 20200209]
    assert sources.read_sources(p)[0].month == (2007, 11)
    p.write_text(p.read_text().replace(",archive,", ",cms,"))
    with pytest.raises(UserError, match="origin 'archive'"):
        sources.read_sources(p)


def test_origin_of():
    assert sources.origin_of("") == "archive"
    assert sources.origin_of("https://web.archive.org/web/2014id_/http://x/y.zip") == "wayback"
    assert sources.origin_of(dl.BASE + "y.zip") == "cms"
    assert sources.origin_of("https://data.nber.org/nppes/zip-orig/NPPES_Data_Dissemination_Nov_2007.zip") == "nber"


def test_head_403_is_a_block(monkeypatch, no_sleep):
    def fake(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 403, "denied", {}, None)
    monkeypatch.setattr(dl.urllib.request, "urlopen", fake)
    with pytest.raises(dl.Blocked, match="Access Denied"):
        dl.head(dl.BASE + "x.zip")


def test_head_reads_md5_from_etag(monkeypatch, no_sleep):
    srv = FakeServer({dl.BASE + "a.zip": b"PK123"})
    monkeypatch.setattr(dl.urllib.request, "urlopen", srv)
    assert dl.head_info(dl.BASE + "a.zip") == (5, hashlib.md5(b"PK123").hexdigest())


def test_fetch_resumes_a_cut_download(tmp_path, monkeypatch, no_sleep):
    data, row = _zip(tmp_path, "20200201", "20200209", "NPPES_Data_Dissemination_February_2020.zip")
    srv = FakeServer({row.url: data})
    srv.cut = len(data) // 2
    monkeypatch.setattr(dl.urllib.request, "urlopen", srv)
    dest = tmp_path / "raw"
    dest.mkdir()
    assert dl.fetch(dest, row.file, row.bytes, url=row.url, sha256=row.sha256) == row.sha256
    assert (dest / row.file).read_bytes() == data
    assert srv.seen[1][2] == f"bytes={len(data) // 2}-"  # second attempt asked only for the rest


def test_fetch_rejects_truncated_body_without_content_length(tmp_path, monkeypatch, no_sleep):
    data, row = _zip(tmp_path, "20200201", "20200209", "NPPES_Data_Dissemination_February_2020.zip")
    srv = FakeServer({row.url: data}, etag=False)
    srv.cut = len(data) - 10
    monkeypatch.setattr(dl.urllib.request, "urlopen", srv)
    dest = tmp_path / "raw"
    dest.mkdir()
    assert dl.fetch(dest, row.file, 0, url=row.url) == row.sha256  # retried from scratch after the zip check failed
    assert len(srv.seen) == 2 and srv.seen[1][2] is None


def test_fetch_html_on_resume_keeps_the_part(tmp_path, monkeypatch, no_sleep):
    dest = tmp_path / "raw"
    dest.mkdir()
    (dest / "a.zip.part").write_bytes(b"PK" + b"x" * 100)
    srv = FakeServer({})
    srv.html.add(dl.BASE + "a.zip")
    monkeypatch.setattr(dl.urllib.request, "urlopen", srv)
    with pytest.raises(dl.Blocked):
        dl.fetch(dest, "a.zip", 1000)
    assert (dest / "a.zip.part").stat().st_size == 102


def test_fetch_sha256_mismatch_gives_up(tmp_path, monkeypatch, no_sleep):
    data, row = _zip(tmp_path, "20200201", "20200209", "NPPES_Data_Dissemination_February_2020.zip")
    monkeypatch.setattr(dl.urllib.request, "urlopen", FakeServer({row.url: data}))
    dest = tmp_path / "raw"
    dest.mkdir()
    with pytest.raises(UserError, match="upstream file changed"):
        dl.fetch(dest, row.file, row.bytes, url=row.url, sha256="0" * 64)
    assert not list(dest.iterdir())


def test_fetch_missing_uses_recorded_urls_and_records_new_months(tmp_path, monkeypatch, no_sleep, releases):
    old, old_row = _zip(tmp_path, "20200101", "20200112", "NPPES_Data_Dissemination_January_2020_V1pinned.zip")
    old_row = Source(old_row.snapshot_id, "NPPES_Data_Dissemination_January_2020.zip", old_row.bytes, old_row.sha256,
                     "wayback", "https://web.archive.org/web/2020id_/http://x/NPPES_Data_Dissemination_January_2020.zip")
    new, _ = _zip(tmp_path, "20200201", "20200209", "x")
    sources.write_sources(releases / "sources.csv", [old_row])
    srv = FakeServer({old_row.url: old, dl.BASE + "NPPES_Data_Dissemination_February_2020.zip": new,
                      dl.BASE + "NPPES_Data_Dissemination_January_2020_V2.zip": b"PK-wrong-variant"})
    monkeypatch.setattr(dl.urllib.request, "urlopen", srv)
    dest = tmp_path / "raw"
    got = dl.fetch_missing(dest, until=dl.date(2020, 2, 1), releases=releases, pace=0)
    assert got == ["NPPES_Data_Dissemination_January_2020.zip", "NPPES_Data_Dissemination_February_2020.zip"]
    assert not any("January_2020_V2" in u for _, u, _ in srv.seen)  # recorded months are never guessed at
    rows = sources.read_sources(releases / "sources.csv")
    assert [(s.snapshot_id, s.origin) for s in rows] == [(20200112, "wayback"), (20200209, "cms")]
    assert rows[1].sha256 == hashlib.sha256(new).hexdigest()
    assert dl.check(dest, releases) == 0


def test_fetch_missing_refuses_archive_months_without_a_mirror(tmp_path, monkeypatch, no_sleep, releases):
    data, row = _zip(tmp_path, "20200101", "20200112", "NPPES_Data_Dissemination_20200101.zip")
    sources.write_sources(releases / "sources.csv", [Source(row.snapshot_id, row.file, row.bytes, row.sha256, "archive")])
    srv = FakeServer({"https://mirror.example/nppes/" + row.file: data})
    monkeypatch.setattr(dl.urllib.request, "urlopen", srv)
    dest = tmp_path / "raw"
    with pytest.raises(UserError, match="no public source"):
        dl.fetch_missing(dest, until=dl.date(2020, 1, 1), releases=releases, pace=0)
    assert dl.fetch_missing(dest, until=dl.date(2020, 1, 1), releases=releases, pace=0,
                            mirror="https://mirror.example/nppes") == [row.file]


def test_check_flags_a_different_variant(tmp_path, releases):
    data, row = _zip(tmp_path, "20200101", "20200112", "NPPES_Data_Dissemination_January_2020.zip")
    sources.write_sources(releases / "sources.csv", [row])
    dest = tmp_path / "raw"
    dest.mkdir()
    (dest / "NPPES_Data_Dissemination_January_2020_V2.zip").write_bytes(data + b"\0")
    assert dl.check(dest, releases) == 1


def test_export_refuses_a_snapshot_without_a_recorded_source(source, tmp_path, releases):
    write_zip(source, "20200101", "20200112", [main_row("1000000001", entity_type="1", last_name="A")])
    zip_path = next(source.iterdir())
    db = tmp_path / "w.duckdb"
    assert main(["load", "--source", str(source), "--db", str(db), "--work", str(tmp_path / "work")]) == 0
    sources.write_sources(releases / "sources.csv", [])
    with pytest.raises(UserError, match="no row in"):
        export_all(db, tmp_path / "export", releases=releases)
    assert main(["download", "--releases", str(releases), "--record", str(zip_path)]) == 0
    assert export_all(db, tmp_path / "export", releases=releases).startswith("e1-20200112-")
