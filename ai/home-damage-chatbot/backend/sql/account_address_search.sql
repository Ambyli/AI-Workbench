-- Fuzzy account search by service address (phoenix). Runtime copy of
-- docs/crm/account_address_search.sql with bound parameters (psycopg named style).
-- Customer text only ever arrives as parameters, never formatted into this string.
-- Params: street, city, zip ('' when absent; '' never matches a postal code).
SELECT
  p.id                          AS project_id,
  p.project_name                AS project_name,
  p.street1,
  p.street2,
  p.city,
  p.postal_code,
  st.abbreviation               AS state,
  ROUND(100 * (
        0.6 * GREATEST(COALESCE(public.similarity(p.street1, %(street)s), 0),
                       COALESCE(public.similarity(p.street2, %(street)s), 0))
      + 0.3 * COALESCE(public.similarity(p.city, %(city)s), 0)
      + 0.1 * CASE WHEN %(zip)s <> '' AND p.postal_code ILIKE %(zip)s || '%%' THEN 1 ELSE 0 END
  )::numeric, 1)                AS fuzzy_score
FROM phoenix.project p
LEFT JOIN phoenix.state st ON st.id = p.state_id AND st.archived IS FALSE
WHERE p.archived IS FALSE
  AND (
        GREATEST(COALESCE(public.similarity(p.street1, %(street)s), 0),
                 COALESCE(public.similarity(p.street2, %(street)s), 0)) > 0.2
     OR COALESCE(public.similarity(p.city, %(city)s), 0) > 0.3
     OR (%(zip)s <> '' AND p.postal_code ILIKE %(zip)s || '%%')
  )
ORDER BY fuzzy_score DESC
LIMIT 5;
