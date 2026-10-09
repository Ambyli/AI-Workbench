-- Fuzzy project (account) search by name + city (in-house CRM, `phoenix` schema).
-- Saved as the starting point for the chatbot's account-matching tool
-- (`search_accounts` in the proposed design). Not wired into the app yet.
--
-- Returns the top 5 candidate projects with a 0-100 score:
--   70% trigram similarity of project_name to :name
--   30% trigram similarity of city to :city (NULL/absent city contributes 0)
-- Requires the pg_trgm extension (public.similarity).
--
-- Notes for turning this into a tool:
-- - Street address is returned but not scored. The chatbot collects the full Solar
--   Account address, so the tool should also require/score the street number (exact)
--   and street1/postal_code; policy code enforces that before anything counts as a match.
-- - Name + city alone is an enumeration oracle (it returns other people's addresses).
--   Results must stay server-side: never shown to the customer, and only the minimal
--   comparison fields passed to the model, per the matching-tool design.
-- - `similarity(...) > 0.2` in WHERE cannot use a trigram index, so every call scans
--   phoenix.project. For an index-backed version use the `%` operator with
--   `SET pg_trgm.similarity_threshold = 0.2` and a GIN gin_trgm_ops index on
--   project_name (a DBA/owner change).
--
-- Parameters: :name, :city

SELECT
  p.id                          AS project_id,
  p.project_name                AS project_name,
  p.street1,
  p.city,
  p.postal_code,
  st.abbreviation               AS state,
  ROUND(100 * (
        0.7 * COALESCE(public.similarity(p.project_name, :name), 0)
      + 0.3 * COALESCE(public.similarity(p.city, :city), 0)
  )::numeric, 1)                AS fuzzy_score
FROM phoenix.project p
LEFT JOIN phoenix.state st ON st.id = p.state_id AND st.archived IS FALSE
WHERE p.archived IS FALSE
  AND public.similarity(p.project_name, :name) > 0.2
ORDER BY fuzzy_score DESC
LIMIT 5;
