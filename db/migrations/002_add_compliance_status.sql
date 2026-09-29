-- 002_add_compliance_status.sql
-- 001_init.sql has been applied to a live database
-- this is a new migration, not an edit to 001.

alter table obligation_control_map
  add column compliance_status text
    check (compliance_status in ('COMPLIANT', 'PARTIALLY_COMPLIANT', 'NON_COMPLIANT', 'NOT_APPLICABLE'));
