-- Views over an nppes-over-time export. Nothing is copied: the Parquet files are read lazily.
-- Needs: SET VARIABLE export = '/path/to/export';
-- The views are TEMP so the study database holds only what this study derives.

CREATE OR REPLACE TEMP VIEW provider AS
SELECT * FROM read_parquet(getvariable('export') || '/parquet/provider/*/*.parquet', hive_partitioning = true);

CREATE OR REPLACE TEMP VIEW provider_taxonomy AS
SELECT * FROM read_parquet(getvariable('export') || '/parquet/provider_taxonomy/*/*.parquet', hive_partitioning = true);

-- one row per CMS file in the build
CREATE OR REPLACE TEMP VIEW snapshots AS
SELECT * FROM read_csv(getvariable('export') || '/snapshots.csv');
