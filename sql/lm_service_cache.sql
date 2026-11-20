-- LM Analyzer shared market-data cache.
-- Safe to run more than once. Does not alter existing analysis, user, or subscription tables.

create table if not exists public.lm_service_cache (
    cache_key text primary key,
    payload jsonb,
    updated_at timestamptz not null default now(),
    refresh_lock_until timestamptz
);

alter table public.lm_service_cache enable row level security;

revoke all on table public.lm_service_cache from public, anon, authenticated;
grant select, insert, update, delete on table public.lm_service_cache to service_role;

create or replace function public.claim_lm_service_cache_refresh(
    p_cache_key text,
    p_lock_seconds integer default 25
)
returns boolean
language plpgsql
security definer
set search_path = public, pg_temp
as $function$
declare
    v_claimed boolean := false;
    v_lock_seconds integer := greatest(1, least(coalesce(p_lock_seconds, 25), 300));
begin
    if p_cache_key is null or btrim(p_cache_key) = '' then
        raise exception 'p_cache_key must not be empty';
    end if;

    insert into public.lm_service_cache (
        cache_key,
        payload,
        updated_at,
        refresh_lock_until
    )
    values (
        p_cache_key,
        null,
        now(),
        now() + make_interval(secs => v_lock_seconds)
    )
    on conflict (cache_key) do update
        set refresh_lock_until = excluded.refresh_lock_until
        where public.lm_service_cache.refresh_lock_until is null
           or public.lm_service_cache.refresh_lock_until <= now()
    returning true into v_claimed;

    return coalesce(v_claimed, false);
end;
$function$;

revoke all on function public.claim_lm_service_cache_refresh(text, integer) from public, anon, authenticated;
grant execute on function public.claim_lm_service_cache_refresh(text, integer) to service_role;

-- Atomically reserve Twelve Data API credits across quote and candle requests.
-- The budget is shared by every Vercel instance and capped at 8 credits per 60 seconds.
-- The current Python reader expects one row per supported cache key.
-- Seed all current market categories and every supported candle symbol/timeframe.
insert into public.lm_service_cache (cache_key)
select 'market_quotes_' || lower(category)
from unnest(array['Forex', 'Metals', 'Indices', 'Energy', 'Crypto', 'Stocks']::text[]) as categories(category)
on conflict (cache_key) do nothing;

insert into public.lm_service_cache (cache_key)
select 'market_candles_' || symbol || '_' || interval
from unnest(array[
    'EURUSD', 'GBPUSD', 'USDJPY', 'USDCHF', 'USDCAD', 'AUDUSD', 'NZDUSD',
    'EURGBP', 'EURJPY', 'GBPJPY', 'AUDJPY', 'EURCHF',
    'XAUUSD', 'XAGUSD', 'XPTUSD',
    'WTI', 'BRENT', 'COPPER',
    'BTCUSD', 'ETHUSD', 'SOLUSD', 'XRPUSD', 'LTCUSD', 'ADAUSD',
    'AAPL', 'MSFT', 'NVDA', 'TSLA', 'AMZN', 'GOOGL', 'META',
    'US500', 'NAS100', 'US30', 'GER40', 'UK100'
]::text[]) as symbols(symbol)
cross join unnest(array['1min', '5min', '15min', '1h', '4h', '1day']::text[]) as intervals(interval)
on conflict (cache_key) do nothing;
