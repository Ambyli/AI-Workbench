-- Fuzzy account search by service address (in-house CRM, `phoenix` schema).
-- Step 1 of the account check: the customer gives their name, then their address;
-- this query finds candidate projects for the address. Step 2 runs
-- project_fuzzy_search.sql on the name on the account; the chatbot code (not the
-- LLM) combines both. See backend/account_match.py.
--
-- Score (0-100): 60% best street similarity (street1 or street2), 30% city
-- similarity, 10% ZIP prefix match. Requires pg_trgm.
--
-- Runtime copy with bound parameters: backend/sql/account_address_search.sql.
-- Saved 2026-09-30. The example literals from the working session were replaced with
-- :street / :city / :zip so no real address is stored in the repo.
--
-- Calibration notes (Python pg_trgm equivalent, backend/trgm.py):
-- - A real address typed with typos in the street, suffix and city (mock account S02:
--   "318 Kilowat Drive, Clearwatr" for "318 Kilowatt Dr, Clearwater") scores only ~57-66, so a
--   "very high" threshold would reject real customers with typos.
-- - "100 Solar Way" vs "101 Solar Way" is 0.75 street similarity. The wrong house scores
--   high, so the code requires an exact house number on top of the score.
-- - "100 Main St" vs "100 Oak St" is 0.44. Same number, different street, so the code also
--   requires street-name similarity (number removed, suffixes normalized).

SELECT
  p.id                          AS project_id,
  p.project_name                AS project_name,
  p.street1,
  p.street2,
  p.city,
  p.postal_code,
  st.abbreviation               AS state,
  ROUND(100 * (
        0.6 * GREATEST(COALESCE(public.similarity(p.street1, :street), 0),
                       COALESCE(public.similarity(p.street2, :street), 0))
      + 0.3 * COALESCE(public.similarity(p.city, :city), 0)
      + 0.1 * CASE WHEN p.postal_code ILIKE :zip || '%' THEN 1 ELSE 0 END
  )::numeric, 1)                AS fuzzy_score
FROM phoenix.project p
LEFT JOIN phoenix.state st ON st.id = p.state_id AND st.archived IS FALSE
WHERE p.archived IS FALSE
  AND (
        GREATEST(COALESCE(public.similarity(p.street1, :street), 0),
                 COALESCE(public.similarity(p.street2, :street), 0)) > 0.2
     OR COALESCE(public.similarity(p.city, :city), 0) > 0.3
     OR p.postal_code ILIKE :zip || '%'
  )
ORDER BY fuzzy_score DESC
LIMIT 5;
