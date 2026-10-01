-- 005_add_directive_control_type.sql
--
-- controls.control_type only allowed 'preventive' | 'detective' | 'corrective'.
-- That taxonomy has no home for controls that mandate a required action
-- (e.g. a mandatory report/escalation to a regulator or authority) rather
-- than preventing, detecting, or correcting something — so the extractor's
-- 'directive' classification was rejected by this constraint even though
-- it's a valid ControlType in src/core/schemas.py.

alter table controls
  drop constraint controls_control_type_check;

alter table controls
  add constraint controls_control_type_check
  check (control_type in ('preventive', 'detective', 'corrective', 'directive'));
