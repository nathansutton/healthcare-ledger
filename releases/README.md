# Release records

For each release, `export` writes one directory here. The directory holds:

- `manifest.json`, `SHA256SUMS`, `data_dictionary.csv`, `privacy_audit.csv` and `snapshots.csv`
- the snapshots file of each other source that is loaded: `medicare_snapshots.csv`, `pos_snapshots.csv`, `ownership_snapshots.csv`, `doctors_clinicians_snapshots.csv`

When you commit a record, the release is published. There is no published release yet. `EDITION` holds the current edition.

The `*_sources.csv` files pin each input file with its name, bytes, sha256, origin and url:

| File | Input files |
|---|---|
| `sources.csv` | the NPPES zips |
| `ppef_sources.csv` | the Medicare PPEF extracts |
| `pos_sources.csv` | the Provider of Services files |
| `ownership_sources.csv` | the Enrollments and All Owners files |
| `doctors_clinicians_sources.csv` | the Doctors and Clinicians archive files |

See [docs/MAINTAINING.md](../docs/MAINTAINING.md).
