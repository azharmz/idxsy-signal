-- IDXSY Signal S3 live Telegram ingestion safety amendments.
-- Architecture HQ approved:
--   GAP-001 -> DB-level uniqueness for signal_events.source_record_id
--   GAP-002 -> small transactional RPC for canonical publication + provenance link
--
-- Apply only after verifying the preflight duplicate query returns 0 rows.

-- ---------------------------------------------------------------------------
-- GAP-001 preflight
-- ---------------------------------------------------------------------------
SELECT source_record_id, COUNT(*) AS duplicate_count
FROM public.signal_events
WHERE source_record_id IS NOT NULL
GROUP BY source_record_id
HAVING COUNT(*) > 1;

CREATE UNIQUE INDEX IF NOT EXISTS signal_events_source_unique_idx
ON public.signal_events (source_record_id)
WHERE source_record_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- GAP-002: atomically create/reuse a Telegram publication signal + source link.
-- If the same Telegram message was edited, update the SAME canonical signal.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.ensure_telegram_publication(
    p_user_id uuid,
    p_source_record_id uuid,
    p_signal jsonb
)
RETURNS uuid
LANGUAGE plpgsql
SET search_path = public
AS $$
DECLARE
    v_source public.source_records%ROWTYPE;
    v_signal_id uuid;
BEGIN
    SELECT *
      INTO v_source
      FROM public.source_records
     WHERE id = p_source_record_id
       AND user_id = p_user_id
       AND source_system = 'TELEGRAM'
     FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Telegram source_record % not found for user %', p_source_record_id, p_user_id;
    END IF;

    IF NULLIF(p_signal->>'ticker', '') IS NULL THEN
        RAISE EXCEPTION 'ticker is required for Telegram publication source %', p_source_record_id;
    END IF;

    IF NULLIF(p_signal->>'signal_timestamp', '') IS NULL THEN
        RAISE EXCEPTION 'signal_timestamp is required for Telegram publication source %', p_source_record_id;
    END IF;

    IF NULLIF(p_signal->>'market_date', '') IS NULL THEN
        RAISE EXCEPTION 'market_date is required for Telegram publication source %', p_source_record_id;
    END IF;

    SELECT signal_id
      INTO v_signal_id
      FROM public.signal_source_links
     WHERE source_record_id = p_source_record_id;

    IF v_signal_id IS NOT NULL THEN
        UPDATE public.signals
           SET ticker = p_signal->>'ticker',
               signal_type = NULLIF(p_signal->>'signal_type', ''),
               signal_timestamp = (p_signal->>'signal_timestamp')::timestamptz,
               market_date = (p_signal->>'market_date')::date,
               entry_price = NULLIF(p_signal->>'entry_price', '')::numeric,
               tp1_price = NULLIF(p_signal->>'tp1_price', '')::numeric,
               tp1_pct = NULLIF(p_signal->>'tp1_pct', '')::numeric,
               tp2_price = NULLIF(p_signal->>'tp2_price', '')::numeric,
               tp2_pct = NULLIF(p_signal->>'tp2_pct', '')::numeric,
               sl_default_price = NULLIF(p_signal->>'sl_default_price', '')::numeric,
               sl_default_pct = NULLIF(p_signal->>'sl_default_pct', '')::numeric,
               sl_moderat_price = NULLIF(p_signal->>'sl_moderat_price', '')::numeric,
               sl_moderat_pct = NULLIF(p_signal->>'sl_moderat_pct', '')::numeric,
               sl_konservatif_price = NULLIF(p_signal->>'sl_konservatif_price', '')::numeric,
               sl_konservatif_pct = NULLIF(p_signal->>'sl_konservatif_pct', '')::numeric,
               confidence_score = NULLIF(p_signal->>'confidence_score', '')::numeric,
               confidence_label = NULLIF(p_signal->>'confidence_label', ''),
               detail = COALESCE(p_signal->'detail', '{}'::jsonb)
         WHERE id = v_signal_id
           AND user_id = p_user_id;

        IF NOT FOUND THEN
            RAISE EXCEPTION 'Linked signal % not found for Telegram source %', v_signal_id, p_source_record_id;
        END IF;

        RETURN v_signal_id;
    END IF;

    INSERT INTO public.signals (
        user_id,
        ticker,
        signal_type,
        signal_timestamp,
        market_date,
        entry_price,
        tp1_price,
        tp1_pct,
        tp2_price,
        tp2_pct,
        sl_default_price,
        sl_default_pct,
        sl_moderat_price,
        sl_moderat_pct,
        sl_konservatif_price,
        sl_konservatif_pct,
        confidence_score,
        confidence_label,
        detail
    ) VALUES (
        p_user_id,
        p_signal->>'ticker',
        NULLIF(p_signal->>'signal_type', ''),
        (p_signal->>'signal_timestamp')::timestamptz,
        (p_signal->>'market_date')::date,
        NULLIF(p_signal->>'entry_price', '')::numeric,
        NULLIF(p_signal->>'tp1_price', '')::numeric,
        NULLIF(p_signal->>'tp1_pct', '')::numeric,
        NULLIF(p_signal->>'tp2_price', '')::numeric,
        NULLIF(p_signal->>'tp2_pct', '')::numeric,
        NULLIF(p_signal->>'sl_default_price', '')::numeric,
        NULLIF(p_signal->>'sl_default_pct', '')::numeric,
        NULLIF(p_signal->>'sl_moderat_price', '')::numeric,
        NULLIF(p_signal->>'sl_moderat_pct', '')::numeric,
        NULLIF(p_signal->>'sl_konservatif_price', '')::numeric,
        NULLIF(p_signal->>'sl_konservatif_pct', '')::numeric,
        NULLIF(p_signal->>'confidence_score', '')::numeric,
        NULLIF(p_signal->>'confidence_label', ''),
        COALESCE(p_signal->'detail', '{}'::jsonb)
    )
    RETURNING id INTO v_signal_id;

    INSERT INTO public.signal_source_links (
        user_id,
        signal_id,
        source_record_id,
        match_method,
        match_confidence,
        evidence,
        review_status
    ) VALUES (
        p_user_id,
        v_signal_id,
        p_source_record_id,
        'NATIVE_TELEGRAM_PUBLICATION',
        'STRONG',
        jsonb_build_object(
            'source_system', 'TELEGRAM',
            'native_source_id', v_source.native_source_id,
            'parser_version', v_source.parser_version
        ),
        'AUTO_ACCEPTED'
    );

    RETURN v_signal_id;
END;
$$;

COMMENT ON FUNCTION public.ensure_telegram_publication(uuid, uuid, jsonb)
IS 'IDXSY Signal S3: atomically create/update canonical Telegram publication and preserve one source link.';
