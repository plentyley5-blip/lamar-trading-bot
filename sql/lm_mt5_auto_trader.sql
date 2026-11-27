-- LM AUTO TRADER: additive MT5 account-linking and execution ledger.
-- These tables are server-managed only. The Android app and MT5 EA must never
-- receive the Supabase service-role key.

create table if not exists public.lm_mt5_link_codes (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references auth.users(id) on delete cascade,
    code_hash text not null unique,
    expires_at timestamptz not null,
    consumed_at timestamptz,
    created_at timestamptz not null default now()
);

create index if not exists lm_mt5_link_codes_user_created_idx
    on public.lm_mt5_link_codes (user_id, created_at desc);

create table if not exists public.lm_mt5_devices (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references auth.users(id) on delete cascade,
    account_login bigint not null check (account_login > 0),
    broker_server text not null check (char_length(broker_server) between 1 and 120),
    account_mode text not null check (account_mode in ('demo', 'real', 'contest')),
    token_hash text not null unique,
    auto_trade_enabled boolean not null default false,
    risk_per_trade numeric(6,4) not null default 0.25 check (risk_per_trade between 0.1 and 1.0),
    max_daily_loss_percent numeric(6,3) not null default 2.0 check (max_daily_loss_percent between 0.5 and 5.0),
    max_open_positions integer not null default 1 check (max_open_positions between 1 and 3),
    max_trades_per_day integer not null default 3 check (max_trades_per_day between 1 and 10),
    max_lot numeric(10,4) not null default 0.10 check (max_lot between 0.01 and 5.0),
    min_confidence integer not null default 75 check (min_confidence between 50 and 100),
    max_signal_age_minutes integer not null default 10 check (max_signal_age_minutes between 1 and 120),
    max_entry_deviation_points integer not null default 50 check (max_entry_deviation_points between 1 and 2000),
    max_spread_points integer not null default 50 check (max_spread_points between 1 and 2000),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    last_seen_at timestamptz,
    revoked_at timestamptz,
    constraint lm_mt5_devices_account_unique unique (account_login, broker_server)
);

create index if not exists lm_mt5_devices_user_idx
    on public.lm_mt5_devices (user_id, created_at desc);

create table if not exists public.lm_mt5_execution_records (
    id uuid primary key default gen_random_uuid(),
    device_id uuid not null references public.lm_mt5_devices(id) on delete cascade,
    user_id uuid not null references auth.users(id) on delete cascade,
    signal_id uuid not null references public.analysis_jobs(id) on delete cascade,
    symbol text not null,
    direction text not null check (direction in ('BUY', 'SELL')),
    status text not null default 'claimed' check (status in ('claimed', 'executed', 'rejected')),
    order_ticket bigint,
    position_id bigint,
    requested_volume numeric(18,8),
    filled_volume numeric(18,8),
    fill_price numeric(24,10),
    reason text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint lm_mt5_execution_device_signal_unique unique (device_id, signal_id)
);

create index if not exists lm_mt5_execution_device_created_idx
    on public.lm_mt5_execution_records (device_id, created_at desc);

create index if not exists lm_mt5_execution_position_idx
    on public.lm_mt5_execution_records (device_id, position_id)
    where position_id is not null;
create index if not exists lm_mt5_execution_records_signal_idx
    on public.lm_mt5_execution_records (signal_id);

create index if not exists lm_mt5_execution_records_user_idx
    on public.lm_mt5_execution_records (user_id, created_at desc);

create table if not exists public.lm_mt5_trade_deals (
    id uuid primary key default gen_random_uuid(),
    device_id uuid not null references public.lm_mt5_devices(id) on delete cascade,
    user_id uuid not null references auth.users(id) on delete cascade,
    account_login bigint not null,
    broker_server text not null,
    deal_ticket bigint not null,
    position_id bigint not null,
    symbol text not null,
    deal_entry text not null,
    deal_type text not null,
    volume numeric(18,8) not null default 0,
    price numeric(24,10) not null default 0,
    profit numeric(20,8) not null default 0,
    commission numeric(20,8) not null default 0,
    swap numeric(20,8) not null default 0,
    deal_time timestamptz not null,
    created_at timestamptz not null default now(),
    constraint lm_mt5_trade_deals_device_ticket_unique unique (device_id, deal_ticket)
);

create index if not exists lm_mt5_trade_deals_user_time_idx
    on public.lm_mt5_trade_deals (user_id, deal_time desc);

create index if not exists lm_mt5_trade_deals_position_idx
    on public.lm_mt5_trade_deals (device_id, position_id, deal_time);

alter table public.lm_mt5_link_codes enable row level security;
alter table public.lm_mt5_devices enable row level security;
alter table public.lm_mt5_execution_records enable row level security;
alter table public.lm_mt5_trade_deals enable row level security;

revoke all on table public.lm_mt5_link_codes from anon, authenticated, public;
revoke all on table public.lm_mt5_devices from anon, authenticated, public;
revoke all on table public.lm_mt5_execution_records from anon, authenticated, public;
revoke all on table public.lm_mt5_trade_deals from anon, authenticated, public;

grant select, insert, update, delete on table public.lm_mt5_link_codes to service_role;
grant select, insert, update, delete on table public.lm_mt5_devices to service_role;
grant select, insert, update, delete on table public.lm_mt5_execution_records to service_role;
grant select, insert, update, delete on table public.lm_mt5_trade_deals to service_role;

comment on table public.lm_mt5_link_codes is 'One-time, hashed linking codes for user-authorized MT5 terminals.';
comment on table public.lm_mt5_devices is 'MT5 terminal links and bounded user-selected auto-trading risk settings.';
comment on table public.lm_mt5_execution_records is 'Idempotent claims and execution outcomes for LM Analyzer signal jobs.';
comment on table public.lm_mt5_trade_deals is 'Broker-reported MT5 deal ledger for verified performance accounting; not a payment or withdrawal ledger.';