-- Post-PTO block states for one project (in-house CRM, `phoenix` schema).
-- Saved for a future chatbot CRM tool (e.g. "does this account already have an
-- open Home Damage / Service Work / ESC block?"). Not wired into the app yet.
--
-- One call returns one row per project with a 3-state verdict per block:
--   NEVER OPENED  no non-archived rows for the block
--   OPEN          any row whose base status is NULL or not COMPLETE/CANCELED
--   CLOSED        rows exist and all are COMPLETE or CANCELED
--
-- Blocks: Home Damage = PB 31, Service Work = PB 16, Electrical Service Change = PB 14.
-- To add a block (e.g. Roof Leak PB 30): another LEFT JOIN triple plus one CASE column.
--
-- Change from the original draft: the instance counts use COUNT(DISTINCT ...).
-- The three LEFT JOIN chains are not isolated. They multiply rows, so a project with
-- 2 ESC rows and 3 Home Damage rows would have counted 6 of each. The state
-- (BOOL_OR) and the MIN/MAX dates were already correct.
--
-- Known nuance: an archived object_status leaves base_status NULL, which counts as OPEN.
--
-- Verified 2026-09-30 against one real project with 0 / 0 / 2 instances. The results
-- matched the three single-block queries (that case never triggers the fan-out). Re-verify
-- on a project with multiple rows in 2+ blocks. Do not store customer names or
-- project ids from real verifications in this repo.
--
-- Parameter: :project_id

SELECT
  p.id                          AS project_id,
  p.project_name                AS project_name,
  -- Home Damage (PB 31)
  CASE
    WHEN COUNT(ppb31.id) = 0 THEN 'NEVER OPENED'
    WHEN BOOL_OR(bs31.base_status IS NULL
                 OR bs31.base_status NOT IN ('COMPLETE', 'CANCELED'))
         THEN 'OPEN'
    ELSE 'CLOSED'
  END                           AS home_damage_state,
  COUNT(DISTINCT ppb31.id)      AS home_damage_instances,
  MIN(ppb31.date_created)       AS home_damage_first_opened,
  MAX(ppb31.date_modified)      AS home_damage_last_modified,
  -- Service Work (PB 16)
  CASE
    WHEN COUNT(ppb16.id) = 0 THEN 'NEVER OPENED'
    WHEN BOOL_OR(bs16.base_status IS NULL
                 OR bs16.base_status NOT IN ('COMPLETE', 'CANCELED'))
         THEN 'OPEN'
    ELSE 'CLOSED'
  END                           AS service_work_state,
  COUNT(DISTINCT ppb16.id)      AS service_work_instances,
  MIN(ppb16.date_created)       AS service_work_first_opened,
  MAX(ppb16.date_modified)      AS service_work_last_modified,
  -- Electrical Service Change (PB 14)
  CASE
    WHEN COUNT(ppb14.id) = 0 THEN 'NEVER OPENED'
    WHEN BOOL_OR(bs14.base_status IS NULL
                 OR bs14.base_status NOT IN ('COMPLETE', 'CANCELED'))
         THEN 'OPEN'
    ELSE 'CLOSED'
  END                           AS esc_state,
  COUNT(DISTINCT ppb14.id)      AS esc_instances,
  MIN(ppb14.date_created)       AS esc_first_opened,
  MAX(ppb14.date_modified)      AS esc_last_modified,
  -- Main-row status labels (diagnostic)
  (SELECT os2.object_status
     FROM phoenix.project_process_block ppb2
     JOIN phoenix.object_status os2 ON os2.id = ppb2.object_status_id
     WHERE ppb2.project_id = p.id AND ppb2.process_block_id = 31
       AND ppb2.archived IS FALSE AND ppb2.main IS TRUE
     LIMIT 1)                   AS home_damage_status,
  (SELECT os2.object_status
     FROM phoenix.project_process_block ppb2
     JOIN phoenix.object_status os2 ON os2.id = ppb2.object_status_id
     WHERE ppb2.project_id = p.id AND ppb2.process_block_id = 16
       AND ppb2.archived IS FALSE AND ppb2.main IS TRUE
     LIMIT 1)                   AS service_work_status,
  (SELECT os2.object_status
     FROM phoenix.project_process_block ppb2
     JOIN phoenix.object_status os2 ON os2.id = ppb2.object_status_id
     WHERE ppb2.project_id = p.id AND ppb2.process_block_id = 14
       AND ppb2.archived IS FALSE AND ppb2.main IS TRUE
     LIMIT 1)                   AS esc_status
FROM phoenix.project p
LEFT JOIN phoenix.project_process_block ppb31
       ON ppb31.project_id = p.id AND ppb31.process_block_id = 31
      AND ppb31.archived IS FALSE
LEFT JOIN phoenix.object_status os31
       ON os31.id = ppb31.object_status_id AND os31.archived IS FALSE
LEFT JOIN phoenix.base_status bs31
       ON bs31.id = os31.base_status_id AND bs31.archived IS FALSE
LEFT JOIN phoenix.project_process_block ppb16
       ON ppb16.project_id = p.id AND ppb16.process_block_id = 16
      AND ppb16.archived IS FALSE
LEFT JOIN phoenix.object_status os16
       ON os16.id = ppb16.object_status_id AND os16.archived IS FALSE
LEFT JOIN phoenix.base_status bs16
       ON bs16.id = os16.base_status_id AND bs16.archived IS FALSE
LEFT JOIN phoenix.project_process_block ppb14
       ON ppb14.project_id = p.id AND ppb14.process_block_id = 14
      AND ppb14.archived IS FALSE
LEFT JOIN phoenix.object_status os14
       ON os14.id = ppb14.object_status_id AND os14.archived IS FALSE
LEFT JOIN phoenix.base_status bs14
       ON bs14.id = os14.base_status_id AND bs14.archived IS FALSE
WHERE p.id = :project_id
  AND p.archived IS FALSE
GROUP BY p.id, p.project_name;
