-- IDXSY Signal S7 — PRE-CUTOVER VERIFICATION (READ ONLY)
-- Run after approved S3/S4 migrations are applied, before enabling live workflows.
-- This script performs SELECTs only and does not mutate canonical data.

-- ---------------------------------------------------------------------------
-- A. Reconstruction baseline. Before the first live rebuild run these should
--    equal the authoritative baseline. After live operation begins, totals may
--    increase; they must never fall below the baseline.
-- ---------------------------------------------------------------------------
WITH metrics AS (
    SELECT 'canonical signals' AS check_name, COUNT(*)::bigint AS actual, 1138::bigint AS baseline
    FROM public.signals
    UNION ALL
    SELECT 'source records', COUNT(*)::bigint, 3023::bigint FROM public.source_records
    UNION ALL
    SELECT 'source links', COUNT(*)::bigint, 2025::bigint FROM public.signal_source_links
    UNION ALL
    SELECT 'Telegram event sources', COUNT(*)::bigint, 480::bigint
      FROM public.source_records
     WHERE source_system = 'TELEGRAM' AND source_record_type = 'EVENT'
    UNION ALL
    SELECT 'matched signal events', COUNT(*)::bigint, 460::bigint FROM public.signal_events
    UNION ALL
    SELECT 'Zeta outcome assertions', COUNT(*)::bigint, 1000::bigint FROM public.signal_outcome_assertions
    UNION ALL
    SELECT 'active canonical outcomes', COUNT(*)::bigint, 1000::bigint
      FROM public.signal_outcomes WHERE superseded_at IS NULL
)
SELECT check_name, actual, baseline,
       CASE WHEN actual >= baseline THEN 'PASS' ELSE 'FAIL' END AS result
FROM metrics
ORDER BY check_name;

-- The reconstructed unresolved Telegram evidence is valid evidence, not a data
-- loss. Before first live processing this should be 20; later it may grow.
SELECT
    'unresolved Telegram events' AS check_name,
    COUNT(*)::bigint AS actual,
    20::bigint AS baseline,
    CASE WHEN COUNT(*) >= 20 THEN 'PASS' ELSE 'FAIL' END AS result
FROM public.reconciliation_items ri
JOIN public.source_records sr ON sr.id = ri.source_record_id
WHERE sr.source_system = 'TELEGRAM'
  AND ri.item_type = 'UNMATCHED_EVENT'
  AND ri.status <> 'ACCEPTED';

-- ---------------------------------------------------------------------------
-- B. Identity / duplicate invariants. Every query below must return zero rows.
-- ---------------------------------------------------------------------------

-- Native source identity is globally idempotent per user/source system.
SELECT user_id, source_system, native_source_id, COUNT(*) AS duplicate_count
FROM public.source_records
GROUP BY user_id, source_system, native_source_id
HAVING COUNT(*) > 1;

-- One source record must link to at most one canonical signal.
SELECT source_record_id, COUNT(*) AS duplicate_count
FROM public.signal_source_links
GROUP BY source_record_id
HAVING COUNT(*) > 1;

-- GAP-001: one source event -> at most one canonical signal_event.
SELECT source_record_id, COUNT(*) AS duplicate_count
FROM public.signal_events
WHERE source_record_id IS NOT NULL
GROUP BY source_record_id
HAVING COUNT(*) > 1;

-- Outcome assertion source identity is one-to-one.
SELECT source_record_id, COUNT(*) AS duplicate_count
FROM public.signal_outcome_assertions
GROUP BY source_record_id
HAVING COUNT(*) > 1;

-- One active canonical outcome maximum per signal.
SELECT signal_id, COUNT(*) AS active_count
FROM public.signal_outcomes
WHERE superseded_at IS NULL
GROUP BY signal_id
HAVING COUNT(*) > 1;

-- ---------------------------------------------------------------------------
-- C. Orphan invariants. Every count must be zero.
-- ---------------------------------------------------------------------------
SELECT 'orphan source link -> signal' AS check_name, COUNT(*) AS actual,
       CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS result
FROM public.signal_source_links l
LEFT JOIN public.signals s ON s.id = l.signal_id
WHERE s.id IS NULL
UNION ALL
SELECT 'orphan source link -> source', COUNT(*), CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END
FROM public.signal_source_links l
LEFT JOIN public.source_records sr ON sr.id = l.source_record_id
WHERE sr.id IS NULL
UNION ALL
SELECT 'orphan event -> signal', COUNT(*), CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END
FROM public.signal_events e
LEFT JOIN public.signals s ON s.id = e.signal_id
WHERE s.id IS NULL
UNION ALL
SELECT 'orphan assertion -> signal', COUNT(*), CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END
FROM public.signal_outcome_assertions a
LEFT JOIN public.signals s ON s.id = a.signal_id
WHERE s.id IS NULL
UNION ALL
SELECT 'orphan assertion -> source', COUNT(*), CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END
FROM public.signal_outcome_assertions a
LEFT JOIN public.source_records sr ON sr.id = a.source_record_id
WHERE sr.id IS NULL
UNION ALL
SELECT 'orphan outcome -> signal', COUNT(*), CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END
FROM public.signal_outcomes o
LEFT JOIN public.signals s ON s.id = o.signal_id
WHERE s.id IS NULL;

-- ---------------------------------------------------------------------------
-- D. Semantic boundary checks.
-- ---------------------------------------------------------------------------

-- Telegram events must not masquerade as Zeta outcome assertions.
SELECT a.id, a.signal_id, sr.source_system, a.outcome_status
FROM public.signal_outcome_assertions a
JOIN public.source_records sr ON sr.id = a.source_record_id
WHERE sr.source_system <> 'ZETA';

-- Canonical active outcomes for the current resolver must remain Zeta-authority.
SELECT id, signal_id, outcome_status, resolver_version, authority
FROM public.signal_outcomes
WHERE superseded_at IS NULL
  AND resolver_version = 'zeta-authority-v1'
  AND authority <> 'ZETA';

-- Zeta final assertion vocabulary check. Zero rows expected.
SELECT id, signal_id, outcome_status
FROM public.signal_outcome_assertions
WHERE outcome_status NOT IN ('TP_HIT', 'SL_HIT', 'EXPIRED');

-- ---------------------------------------------------------------------------
-- E. S3/S4 physical cutover prerequisites.
-- ---------------------------------------------------------------------------

SELECT
    'signal_events_source_unique_idx' AS object_name,
    CASE WHEN to_regclass('public.signal_events_source_unique_idx') IS NOT NULL
         THEN 'PASS' ELSE 'FAIL' END AS result
UNION ALL
SELECT
    'ensure_telegram_publication RPC',
    CASE WHEN to_regprocedure('public.ensure_telegram_publication(uuid,uuid,jsonb)') IS NOT NULL
         THEN 'PASS' ELSE 'FAIL' END
UNION ALL
SELECT
    'ensure_zeta_publication RPC',
    CASE WHEN to_regprocedure('public.ensure_zeta_publication(uuid,uuid,jsonb)') IS NOT NULL
         THEN 'PASS' ELSE 'FAIL' END
UNION ALL
SELECT
    'resolve_zeta_outcome RPC',
    CASE WHEN to_regprocedure('public.resolve_zeta_outcome(uuid,uuid)') IS NOT NULL
         THEN 'PASS' ELSE 'FAIL' END;

-- ---------------------------------------------------------------------------
-- F. Legacy dependency observation (informational only).
-- `trades_data` is intentionally not dropped during rebuild.
-- ---------------------------------------------------------------------------
SELECT
    CASE WHEN to_regclass('public.trades_data') IS NOT NULL
         THEN 'PRESENT (expected during transition)'
         ELSE 'ABSENT'
    END AS trades_data_state;
