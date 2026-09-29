-- 003_add_control_span_fields.sql
--
-- Note: Migrations 001 and 002 have already been applied to the database, 
-- so we are rolling this out as a new, forward-only migration.
--
-- The ingestion pipeline needs to verify control citations extracted 
-- from unstructured policy documents, matching the same strict verification 
-- process we use for regulatory obligations. Previously, the `controls` table 
-- was missing the necessary tracking fields (`verbatim_quote`, `span_verified`, 
-- and `span_verify_method`).
--
-- These new columns are intentionally nullable: controls imported directly 
-- from structured systems or internal registers won't have a free-text quote 
-- to verify against.

alter table controls
  add column verbatim_quote text,
  add column span_verified boolean not null default false,
  add column span_verify_method text check (span_verify_method in ('exact', 'normalized', 'fuzzy_extraction'));