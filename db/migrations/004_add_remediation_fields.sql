-- 004_add_remediation_fields.sql
--
-- Note: Migrations 001-003 have already been applied to the live database, 
-- so these schema updates are introduced as a new forward-only migration.
--
-- The remediation pipeline needs to specify an `action_type` and a concrete 
-- `monitoring_metric` to properly track ongoing fixes, but neither field currently 
-- existed on the `remediations` table.
--
-- `action_type` is implemented as a plain TEXT column without a CHECK constraint. 
-- Unlike stricter internal taxonomies (such as gap classes), the list of action types 
-- is open-ended and expected to evolve. Leaving this unconstrained at the database 
-- level prevents unnecessary migration churn as new action types emerge over time.

alter table remediations
  add column action_type text,
  add column monitoring_metric text;