"""Spike: tax id -> NPI rosters from the head of insurers' Transparency in Coverage in-network files.

Each in-network file opens with ``provider_references``: provider groups, each a tax id (EIN, or an NPI for a
sole proprietor) with the NPIs contracted under it. The rates that follow are 1 to 20 GB more, so this reads
the file in HTTP ranges, inflates as it goes and stops at the ``in_network`` key: 25 to 40 MB per file.

    python tic_heads.py list  OUT_DIR             # manifests/<payer>.csv, one row per distinct network file
    python tic_heads.py fetch OUT_DIR PAYER [N]   # heads of the N largest unfetched files -> heads/<payer>/*.parquet

Only the current month is served by any of the three portals, so this is a snapshot, not a history.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import sys
import time
import urllib.request
import zlib
from pathlib import Path

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"}
CHUNK = 8 << 20
CAP = 512 << 20  # stop if provider_references has not ended by then (it comes after the rates in that file)
AETNA = "https://mrf.healthsparq.com/aetnacvs-egress.nophi.kyruushsq.com/prd/mrf/AETNACVS_I"
AETNA_BRANDS = ("ALICFI", "AFEHBPFI")  # fully insured commercial and FEHB; ALICSI (self-funded) reuses their networks


def get(url: str, headers: dict | None = None, timeout: int = 120):
    return urllib.request.urlopen(urllib.request.Request(url, headers={**UA, **(headers or {})}), timeout=timeout)


def resolve(url: str) -> tuple[str, int]:
    """Final URL after redirects and the file's total size, from a one-byte range."""
    with get(url, {"Range": "bytes=0-0"}) as r:
        return r.geturl(), int(r.headers.get("Content-Range", "/0").split("/")[-1] or 0)


# ---------------------------------------------------------------- manifests

def list_uhc() -> list[dict]:
    with get("https://transparency-in-coverage.uhc.com/api/v1/uhc/blobs/", timeout=300) as r:
        blobs = json.load(r)["blobs"]
    # UHC publishes one network under every legal entity and employer; byte-identical copies share a size
    seen, rows = set(), []
    for b in sorted(blobs, key=lambda b: b["name"]):
        if "in-network" in b["name"] and b["name"].endswith(".gz") and b["size"] not in seen:
            seen.add(b["size"])
            rows.append({"key": b["name"], "url": b["downloadUrl"], "size": b["size"]})
    return rows


def list_cigna() -> list[dict]:
    with get("https://www.cigna.com/static/mrf/latest.json") as r:
        toc_url = json.load(r)["mrfs"][0]["files"][0]["url"]
    with get(toc_url, timeout=300) as r:
        toc = json.load(r)
    urls = {}
    for rs in toc["reporting_structure"]:
        for f in rs.get("in_network_files", []):
            # partner networks host their own zips; only Cigna's gzip files can be range-read
            if f["location"].split("?")[0].endswith(".json.gz"):
                urls.setdefault(f["location"].split("?")[0].rsplit("/", 1)[-1], f["location"])
    rows = []
    for key, url in sorted(urls.items()):
        rows.append({"key": key, "url": url, "size": resolve(url)[1]})
        time.sleep(0.2)
    return rows


def list_aetna() -> list[dict]:
    codes = {}
    for brand in AETNA_BRANDS:
        with get(f"{AETNA}/{brand}/latest_metadata.json") as r:
            for f in json.load(r)["files"]:
                if f["fileSchema"] == "IN_NETWORK_RATES":
                    m = re.match(r"\d{4}-\d\d-\d\d_(pl-[^_]+)", f["fileName"])
                    codes.setdefault(m.group(1) if m else f["fileName"], f"{AETNA}/{brand}/{f['filePath']}")
    rows = []
    for key, url in sorted(codes.items()):
        rows.append({"key": key, "url": url, "size": resolve(url)[1]})
        time.sleep(0.2)
    return rows


# ---------------------------------------------------------------- heads

def read_head(url: str) -> tuple[bytes, dict]:
    final, total = resolve(url)
    d, buf, pos, status = zlib.decompressobj(31), bytearray(), 0, "cap"
    while pos < CAP:
        with get(final, {"Range": f"bytes={pos}-{pos + CHUNK - 1}"}) as r:
            if r.status != 206:
                raise RuntimeError(f"range ignored ({r.status})")
            data = r.read()
            # the first hop sometimes answers the one-byte probe without a range, so trust each chunk's own
            total = int(r.headers.get("Content-Range", "/0").split("/")[-1] or total)
        pos += len(data)
        while data:  # a gzip file may be several concatenated members (UHC's are)
            buf += d.decompress(data)
            data = d.unused_data if d.eof else b""
            if d.eof:
                d = zlib.decompressobj(31)
        i = buf.find(b'"in_network"')
        if i >= 0:
            return bytes(buf[:i]), {"status": "ok", "read": pos, "total": total}
        if pos >= total:
            status = "eof"
            break
    return b"", {"status": status, "read": pos, "total": total}


def parse(head: bytes) -> tuple[list[tuple], int]:
    """(provider_group_id, network_name, tin_type, tin, business_name, npi) rows and the count of remote refs."""
    doc = json.loads(head.rstrip().rstrip(b",") + b"}")
    rows, remote = [], 0
    for ref in doc.get("provider_references", []):
        if "location" in ref:
            remote += 1
            continue
        net = "|".join(ref.get("network_name") or [])
        for g in ref.get("provider_groups", []):
            tin = g.get("tin") or {}
            for npi in g.get("npi", []):
                npi = int(npi) if str(npi).isdigit() else 0
                if 1_000_000_000 <= npi <= 2_999_999_999:
                    rows.append((ref.get("provider_group_id"), net, tin.get("type"), str(tin.get("value")),
                                 tin.get("business_name"), npi))
    return rows, remote


def fetch(out: Path, payer: str, n: int) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    with open(out / "manifests" / f"{payer}.csv") as f:
        todo = sorted(csv.DictReader(f), key=lambda r: -int(r["size"]))
    dest = out / "heads" / payer
    dest.mkdir(parents=True, exist_ok=True)
    log = open(out / "fetch_log.jsonl", "a")
    done = 0
    for row in todo:
        name = hashlib.sha1(row["key"].encode()).hexdigest()[:16]
        if (dest / f"{name}.parquet").exists() or (dest / f"{name}.skip").exists():
            continue
        if done >= n:
            break
        t0 = time.time()
        try:
            head, info = read_head(row["url"])
            rows, remote = parse(head) if head else ([], 0)
        except Exception as e:  # noqa: BLE001 - a spike logs and moves on
            info, rows, remote = {"status": f"error: {e}"}, [], 0
        info |= {"payer": payer, "key": row["key"], "rows": len(rows), "remote_refs": remote,
                 "secs": round(time.time() - t0, 1)}
        if rows:
            cols = list(zip(*rows))
            pq.write_table(pa.table({
                "payer": [payer] * len(rows), "file_key": [row["key"]] * len(rows),
                "provider_group_id": pa.array([str(x) for x in cols[0]]), "network_name": cols[1],
                "tin_type": cols[2], "tin": cols[3], "business_name": cols[4], "npi": pa.array(cols[5], pa.int64()),
            }), dest / f"{name}.parquet")
        else:
            (dest / f"{name}.skip").write_text(json.dumps(info))
        log.write(json.dumps(info) + "\n")
        log.flush()
        print(json.dumps(info), flush=True)
        done += 1
        time.sleep(1)


def main(argv: list[str]) -> None:
    cmd, out = argv[0], Path(argv[1])
    if cmd == "list":
        (out / "manifests").mkdir(parents=True, exist_ok=True)
        for payer, fn in (("cigna", list_cigna), ("aetna", list_aetna), ("uhc", list_uhc)):
            rows = fn()
            with open(out / "manifests" / f"{payer}.csv", "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=["key", "url", "size"])
                w.writeheader()
                w.writerows(rows)
            print(payer, len(rows), "files", round(sum(r["size"] for r in rows) / 1e12, 2), "TB", flush=True)
    elif cmd == "fetch":
        fetch(out, argv[2], int(argv[3]) if len(argv) > 3 else 10**9)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
