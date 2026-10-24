# Convenience targets. All real logic lives in `nppes-over-time` (src/nppes_over_time/cli.py).
# Paths below are also the CLI defaults, so `uv run nppes-over-time <cmd>` with no flags does the same thing.
SOURCE   ?= data/raw            # directory of NPPES_Data_Dissemination_*.zip files
DB       ?= data/nppes.duckdb
WORK     ?= data/work
OUT      ?= export
RELEASES ?= releases
PPEF_SOURCE ?= data/ppef    # one directory per quarter of Medicare PPEF extracts (docs/MAINTAINING.md)
# DuckDB resource limits. Unset = DuckDB's defaults (all cores, 80% of RAM). Set e.g. THREADS=8 MEMORY=24GB.
THREADS  ?=
MEMORY   ?=
RES = $(if $(THREADS),--threads $(THREADS)) $(if $(MEMORY),--memory $(MEMORY))
# Base URL holding the 'archive' zips of releases/sources.csv under their recorded names (docs/MAINTAINING.md).
MIRROR   ?=
# VERIFY_ALL=1 makes `make verify` check every loaded month, not just the latest.
VERIFY_ALL ?=
# MONTHLY_FILES=1 adds one wide provider file per month to `make export` / `make monthly` (about 100 GB).
MONTHLY_FILES ?=
MONTHLY_FLAG = $(if $(MONTHLY_FILES),--monthly-files)

.PHONY: install test discover probe download check-sources load validate verify export status monthly backfill-fingerprints ppef-load ppef-sources

install:
	uv sync

test:
	uv run pytest -q

discover:
	uv run nppes-over-time discover --source $(SOURCE)

# Report month by month what is local, where each recorded month comes from (releases/sources.csv), and what CMS has since.
probe:
	uv run nppes-over-time download --dest $(SOURCE) --releases $(RELEASES) --probe

# Fetch every recorded source zip that $(SOURCE) lacks (sha256-checked), then any newer months CMS serves.
download:
	uv run nppes-over-time download --dest $(SOURCE) --releases $(RELEASES) $(if $(MIRROR),--mirror $(MIRROR))

# sha256 every zip in $(SOURCE) against releases/sources.csv.
check-sources:
	uv run nppes-over-time download --dest $(SOURCE) --releases $(RELEASES) --check

load:
	uv run nppes-over-time load --source $(SOURCE) --db $(DB) --work $(WORK) $(RES)

validate:
	uv run nppes-over-time validate --db $(DB)

# Medicare PPEF quarters (optional): load every quarter directory in $(PPEF_SOURCE) not yet in the warehouse, then
# record file sizes and sha256s in releases/ppef_sources.csv (fill in the urls before exporting).
ppef-load:
	uv run nppes-over-time ppef-load --source $(PPEF_SOURCE) --db $(DB) --work $(WORK) $(RES)

ppef-sources:
	uv run nppes-over-time ppef-sources --db $(DB) --releases $(RELEASES)

verify:
	uv run nppes-over-time verify --db $(DB) --source $(SOURCE) --work $(WORK) $(RES) $(if $(VERIFY_ALL),--all)

export:
	uv run nppes-over-time export --db $(DB) --out $(OUT) --csv --releases $(RELEASES) $(MONTHLY_FLAG) $(RES)

status:
	uv run nppes-over-time status --db $(DB)

# The monthly routine for maintainers (docs/MAINTAINING.md): download, load, validate, verify, continuity, export.
monthly:
	uv run nppes-over-time monthly --source $(SOURCE) --db $(DB) --work $(WORK) --out $(OUT) --releases $(RELEASES) $(MONTHLY_FLAG) $(RES)

# One-time, for a warehouse built before content fingerprints existed.
backfill-fingerprints:
	uv run nppes-over-time backfill-fingerprints --db $(DB) $(RES)
