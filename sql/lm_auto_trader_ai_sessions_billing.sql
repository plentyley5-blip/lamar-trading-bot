-- LM AUTO TRADER commercial catalog and backend-controlled trading sessions.
-- Additive migration. Does not modify LM Analyzer subscription entitlements.
-- Google Play products must be created/activated in Play Console before plans are enabled.

alter table public.lm_auto_trader_plans
    add column if not exists google_play_product_id text,
    add column if not exists discount_percent numeric(5,2) not null default 0
        check (discount_percent between 0 and 100);

insert into public.lm_auto_trader_plans
    (plan_key, display_name, duration_months, currency, price, enabled,
     includes_managed_vps, google_play_product_id, discount_percent)
values
    ('monthly', 'LM AUTO TRADER Monthly', 1, 'USD', 49.99, false,
     true, 'lm_auto_trader_monthly', 0),
    ('quarterly', 'LM AUTO TRADER 3 Months', 3, 'USD', 125.99, false,
     true, 'lm_auto_trader_3_months', 16),
    ('six_months', 'LM AUTO TRADER 6 Months', 6, 'USD', 249.99, false,
     true, 'lm_auto_trader_6_months', 17)
on conflict (plan_key) do update set
    display_name = excluded.display_name,
    duration_months = excluded.duration_months,
    currency = excluded.currency,
    price = excluded.price,
    google_play_product_id = excluded.google_play_product_id,
    discount_percent = excluded.discount_percent,
    updated_at = now();

alter table public.lm_auto_trader_subscriptions
    add column if not exists google_play_product_id text,
    add column if not exists purchase_token_hash text,
    add column if not exists order_id text,
    add column if not exists acknowledged boolean not null default false,
    add column if not exists auto_renewing boolean not null default false;

create unique index if not exists lm_auto_trader_subscriptions_purchase_hash_uidx
    on public.lm_auto_trader_subscriptions (purchase_token_hash)
    where purchase_token_hash is not null;


create table if not exists public.lm_mt5_market_data (
    device_id uuid primary key references public.lm_mt5_devices(id) on delete cascade,
    user_id uuid not null references auth.users(id) on delete cascade,
    account_login bigint not null,
    broker_server text not null,
    market_data jsonb not null check (jsonb_typeof(market_data) = 'object'),
    updated_at timestamptz not null default now()
);

create index if not exists lm_mt5_market_data_user_updated_idx
    on public.lm_mt5_market_data (user_id, updated_at desc);

create table if not exists public.lm_mt5_trading_sessions (
    id uuid primary key default gen_random_uuid(),
    device_id uuid not null unique references public.lm_mt5_devices(id) on delete cascade,
    user_id uuid not null references auth.users(id) on delete cascade,
    status text not null default 'stopped'
        check (status in ('stopped', 'starting', 'active', 'paused', 'risk_locked', 'subscription_locked')),
    account_mode text not null check (account_mode in ('demo', 'real', 'contest')),
    started_at timestamptz,
    stopped_at timestamptz,
    last_decision_at timestamptz,
    last_heartbeat_at timestamptz,
    stop_reason text,
    risk_config jsonb not null default '{}'::jsonb check (jsonb_typeof(risk_config) = 'object'),
    updated_at timestamptz not null default now()
);

create index if not exists lm_mt5_trading_sessions_user_status_idx
    on public.lm_mt5_trading_sessions (user_id, status, updated_at desc);

create table if not exists public.lm_mt5_ai_decisions (
    id uuid primary key default gen_random_uuid(),
    device_id uuid not null references public.lm_mt5_devices(id) on delete cascade,
    user_id uuid not null references auth.users(id) on delete cascade,
    session_id uuid not null references public.lm_mt5_trading_sessions(id) on delete cascade,
    symbol text not null,
    decision text not null check (decision in ('BUY', 'SELL', 'WAIT')),
    confidence integer not null check (confidence between 0 and 100),
    entry_price numeric(24,10),
    stop_loss numeric(24,10),
    take_profit numeric(24,10),
    rationale text,
    model text,
    market_snapshot jsonb not null default '{}'::jsonb check (jsonb_typeof(market_snapshot) = 'object'),
    status text not null default 'pending'
        check (status in ('pending', 'claimed', 'executed', 'rejected', 'expired', 'cancelled')),
    expires_at timestamptz not null,
    claimed_at timestamptz,
    completed_at timestamptz,
    execution_result jsonb not null default '{}'::jsonb check (jsonb_typeof(execution_result) = 'object'),
    created_at timestamptz not null default now(),
    check (
        decision = 'WAIT'
        or (entry_price is not null and stop_loss is not null and take_profit is not null)
    )
);

create index if not exists lm_mt5_ai_decisions_device_queue_idx
    on public.lm_mt5_ai_decisions (device_id, status, created_at)
    where status in ('pending', 'claimed');

create index if not exists lm_mt5_ai_decisions_user_created_idx
    on public.lm_mt5_ai_decisions (user_id, created_at desc);

create table if not exists public.lm_mt5_daily_risk_state (
    device_id uuid not null references public.lm_mt5_devices(id) on delete cascade,
    trading_day date not null,
    starting_equity numeric(20,8) not null,
    realized_pnl numeric(20,8) not null default 0,
    peak_equity numeric(20,8) not null,
    daily_loss_locked boolean not null default false,
    drawdown_locked boolean not null default false,
    trades_opened integer not null default 0,
    updated_at timestamptz not null default now(),
    primary key (device_id, trading_day)
);

-- Equity tiers are policy data, not a substitute for broker-side margin/volume checks.
create table if not exists public.lm_auto_trader_risk_tiers (
    tier_key text primary key,
    min_equity numeric(20,8) not null,
    max_equity numeric(20,8),
    max_open_positions integer not null check (max_open_positions between 1 and 5),
    max_lot numeric(18,8) not null check (max_lot > 0),
    risk_per_trade_percent numeric(5,3) not null check (risk_per_trade_percent > 0 and risk_per_trade_percent <= 1),
    created_at timestamptz not null default now(),
    check (max_equity is null or max_equity > min_equity)
);

insert into public.lm_auto_trader_risk_tiers
    (tier_key, min_equity, max_equity, max_open_positions, max_lot, risk_per_trade_percent)
values
    ('under_50', 0, 50, 1, 0.01, 0.50),
    ('50_to_199', 50, 200, 2, 0.02, 0.50),
    ('200_to_499', 200, 500, 3, 0.05, 0.50),
    ('500_to_999', 500, 1000, 4, 0.10, 0.50),
    ('1000_plus', 1000, null, 5, 0.20, 0.50)
on conflict (tier_key) do update set
    min_equity = excluded.min_equity,
    max_equity = excluded.max_equity,
    max_open_positions = excluded.max_open_positions,
    max_lot = excluded.max_lot,
    risk_per_trade_percent = excluded.risk_per_trade_percent;

alter table public.lm_mt5_market_data enable row level security;
alter table public.lm_mt5_trading_sessions enable row level security;
alter table public.lm_mt5_ai_decisions enable row level security;
alter table public.lm_mt5_daily_risk_state enable row level security;
alter table public.lm_auto_trader_risk_tiers enable row level security;

revoke all on public.lm_mt5_market_data from anon, authenticated, public;
revoke all on public.lm_mt5_trading_sessions from anon, authenticated, public;
revoke all on public.lm_mt5_ai_decisions from anon, authenticated, public;
revoke all on public.lm_mt5_daily_risk_state from anon, authenticated, public;
revoke all on public.lm_auto_trader_risk_tiers from anon, authenticated, public;

grant select, insert, update, delete on public.lm_mt5_market_data to service_role;
grant select, insert, update, delete on public.lm_mt5_trading_sessions to service_role;
grant select, insert, update, delete on public.lm_mt5_ai_decisions to service_role;
grant select, insert, update, delete on public.lm_mt5_daily_risk_state to service_role;
grant select, insert, update, delete on public.lm_auto_trader_risk_tiers to service_role;

comment on table public.lm_mt5_market_data is
    'Fresh per-account broker M1/M5 OHLC bars uploaded by the linked MT5 terminal; the AI decision engine reads only this source.';
comment on table public.lm_mt5_trading_sessions is
    'One backend-authoritative Start/Stop session per linked MT5 device. Linking never starts trading.';
comment on table public.lm_mt5_ai_decisions is
    'Backend-generated, account-isolated AI decisions queued for the linked MT5 execution bridge.';
comment on table public.lm_mt5_daily_risk_state is
    'Per-device daily loss, equity drawdown, and trade-count guard state.';
comment on table public.lm_auto_trader_risk_tiers is
    'Proposed equity-based position and lot ceilings; broker volume and margin checks remain mandatory.';
