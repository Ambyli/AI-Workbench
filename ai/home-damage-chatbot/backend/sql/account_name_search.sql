-- Fuzzy account search by the name on the account (phoenix). Runtime copy of
-- docs/crm/project_fuzzy_search.sql with bound parameters (psycopg named style).
-- Params: name, city (city parsed from the customer's address; '' when absent).
SELECT
  p.id                          AS project_id,
  p.project_name                AS project_name,
  p.street1,
  p.city,
  p.postal_code,
  st.abbreviation               AS state,
  ROUND(100 * (
        0.7 * COALESCE(public.similarity(p.project_name, %(name)s), 0)
      + 0.3 * COALESCE(public.similarity(p.city, %(city)s), 0)
  )::numeric, 1)                AS fuzzy_score
FROM phoenix.project p
LEFT JOIN phoenix.state st ON st.id = p.state_id AND st.archived IS FALSE
WHERE p.archived IS FALSE
  AND public.similarity(p.project_name, %(name)s) > 0.2
ORDER BY fuzzy_score DESC
LIMIT 5;
