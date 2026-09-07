-- IDXSY Signal S3 — backend-only RPC permissions.
-- The canonical publication path is backend-authoritative. Supabase/Postgres
-- functions are executable by PUBLIC unless privileges are explicitly revoked,
-- so do not expose this mutation RPC to browser roles.

REVOKE ALL ON FUNCTION public.ensure_telegram_publication(uuid, uuid, jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.ensure_telegram_publication(uuid, uuid, jsonb) FROM anon;
REVOKE ALL ON FUNCTION public.ensure_telegram_publication(uuid, uuid, jsonb) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.ensure_telegram_publication(uuid, uuid, jsonb) TO service_role;
