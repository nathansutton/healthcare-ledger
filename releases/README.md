# Release records

`export` writes one directory here per release: `manifest.json`, `SHA256SUMS`, `snapshots.csv`, `data_dictionary.csv` and, when those sources are loaded, `medicare_snapshots.csv`, `pos_snapshots.csv` and `privacy_audit.csv`. Committing a record means the release is published. Nothing has been published yet. `EDITION` is the current edition.

The `*_sources.csv` files pin every input file (name, bytes, sha256, origin, url): `sources.csv` for the NPPES zips, `ppef_sources.csv` for the Medicare PPEF extracts and `pos_sources.csv` for the Provider of Services files. See [docs/MAINTAINING.md](../docs/MAINTAINING.md).
