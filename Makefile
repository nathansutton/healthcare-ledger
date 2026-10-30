# Convenience targets. All real logic lives in `healthcare-ledger` (src/healthcare_ledger/cli.py).
# Paths below are also the CLI defaults, so `uv run healthcare-ledger <cmd>` with no flags does the same thing.
# The source that download, check-sources, load and sources act on: nppes, ppef (Medicare PPEF quarters), pos
# (Provider of Services quarters) ahrq (the AHRQ Compendium) or medicaid (HHS's Medicaid spending), e.g. `make load SOURCE_NAME=ppef` (docs/MAINTAINING.md).
SOURCE_NAME ?= nppes
# Its directory. Empty = the CLI default: data/raw (NPPES_Data_Dissemination_*.zip files), data/ppef, data/pos, data/ahrq, data/medicaid.
SOURCE   ?=
WAREHOUSE ?= data/warehouse
WORK     ?= data/work
OUT      ?= export
RELEASES ?= releases
# DuckDB resource limits. Unset = DuckDB's defaults (all cores, 80% of RAM). Set e.g. THREADS=8 MEMORY=24GB.
THREADS  ?=
MEMORY   ?=
RES = $(if $(THREADS),--threads $(THREADS)) $(if $(MEMORY),--memory $(MEMORY))
SRC = $(if $(SOURCE),--source $(SOURCE))
DEST = $(if $(SOURCE),--dest $(SOURCE))
# Base URL holding the 'archive' zips of releases/sources.csv under their recorded names (docs/MAINTAINING.md).
MIRROR   ?=
# VERIFY_ALL=1 makes `make verify` check every loaded month, not just the latest.
VERIFY_ALL ?=
# MONTHLY_FILES=1 adds one wide provider file per month to `make export` / `make monthly` (about 100 GB).
MONTHLY_FILES ?=
MONTHLY_FLAG = $(if $(MONTHLY_FILES),--monthly-files)

.PHONY: install test smoke discover probe download check-sources load validate verify export package status monthly sources graph

install:
	uv sync

test:
	uv run pytest -q

# One NPPES month end to end (the newest in releases/sources.csv): download, load, validate, verify, export and read back,
# all under data/smoke. About 1.2 GB to download and 15 GB of disk while it runs.
smoke:
	uv run healthcare-ledger smoke $(RES)

discover:
	uv run healthcare-ledger discover $(SRC)

# Report month by month what is local, where each recorded month comes from (releases/sources.csv), and what CMS has since.
probe:
	uv run healthcare-ledger download $(DEST) --releases $(RELEASES) --probe

# Fetch every recorded file the source directory lacks (sha256-checked); for NPPES, then any newer months CMS serves.
download:
	uv run healthcare-ledger download --source-name $(SOURCE_NAME) $(DEST) --releases $(RELEASES) $(if $(MIRROR),--mirror $(MIRROR))

# sha256 every file in the source directory against its releases/ sources file (sources.csv, ppef_sources.csv, pos_sources.csv,
# ahrq_sources.csv, medicaid_sources.csv).
check-sources:
	uv run healthcare-ledger download --source-name $(SOURCE_NAME) $(DEST) --releases $(RELEASES) --check

# Load every snapshot (NPPES month, PPEF or POS quarter) not yet in the warehouse, in as-of order; AHRQ is rebuilt wholesale.
load:
	uv run healthcare-ledger load --source-name $(SOURCE_NAME) $(SRC) --warehouse $(WAREHOUSE) --work $(WORK) $(RES)

# PPEF and POS: record the size and sha256 of every loaded file in releases/<source>_sources.csv (fill in the urls
# before exporting). NPPES's releases/sources.csv is written by `download`.
sources:
	uv run healthcare-ledger sources --source-name $(SOURCE_NAME) --warehouse $(WAREHOUSE) --releases $(RELEASES)

validate:
	uv run healthcare-ledger validate --warehouse $(WAREHOUSE)

# Derive the provider graph (docs/METHODOLOGY.md section 8) from the NPPES, PPEF and POS tables into $(GRAPH).
# COMPENDIUM=1 also writes the AHRQ system tables (section 8a) from the ahrq source (make download load SOURCE_NAME=ahrq);
# MEDICAID=1 the Medicaid main-group tables (section 8c) from the medicaid source (make download load SOURCE_NAME=medicaid);
# OWNERSHIP=1 the firm and chain tables (section 8d), DAC=1 the claims-filtered membership (section 8e) and MEDICARE=1
# medicare_volume (section 8c), each from its source.
GRAPH ?= graph
COMPENDIUM ?=
MEDICAID ?=
OWNERSHIP ?=
DAC ?=
MEDICARE ?=
graph:
	uv run healthcare-ledger graph-build --warehouse $(WAREHOUSE) --out $(GRAPH) --work $(WORK) $(RES) $(if $(COMPENDIUM),--compendium) $(if $(MEDICAID),--medicaid) \
	  $(if $(OWNERSHIP),--ownership) $(if $(DAC),--doctors-clinicians) $(if $(MEDICARE),--medicare-utilization)

verify:
	uv run healthcare-ledger verify --warehouse $(WAREHOUSE) $(SRC) --work $(WORK) $(RES) $(if $(VERIFY_ALL),--all)

export:
	uv run healthcare-ledger export --warehouse $(WAREHOUSE) --out $(OUT) --csv --releases $(RELEASES) $(MONTHLY_FLAG) $(RES)

# Turn the export into a Zenodo deposit (docs/MAINTAINING.md, "Publish on Zenodo").
DEPOSIT ?= deposit
package:
	uv run healthcare-ledger package --out $(OUT) --dest $(DEPOSIT)

status:
	uv run healthcare-ledger status --warehouse $(WAREHOUSE)

# The monthly routine for maintainers (docs/MAINTAINING.md): download, load, validate, verify, continuity, export.
monthly:
	uv run healthcare-ledger monthly $(SRC) --warehouse $(WAREHOUSE) --work $(WORK) --out $(OUT) --releases $(RELEASES) $(MONTHLY_FLAG) $(RES)
