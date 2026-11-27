-- LM AUTO TRADER managed Windows VPS, account telemetry, and separate subscription catalog.
-- Additive only: existing LM Analyzer subscriptions and MT5 execution records are untouched.
-- No MT5 passwords or plaintext secrets are stored in these tables.

create table if not exists public.lm_mt5_account_snapshots (
    device_id uuid primary key references public.lm_mt5_devices(id) on delete cascade,
    user_id uuid not null references auth.users(id) on delete cascade,
    account_login bigint not null,
    broker_server text not null,
    account_mode text not null check (account_mode in ('demo', 'real', 'contest')),
    balance numeric(20,8) not null,
    equity numeric(20,8) not null,
    floating_profit numeric(20,8) not null default 0,
    credit numeric(20,8) not null default 0,
    margin numeric(20,8) not null default 0,
    free_margin numeric(20,8) not null default 0,
    margin_level numeric(20,8) not null default 0,
    currency text not null check (currency ~ '^[A-Z0-9]{3,8}$'),
    leverage integer not null check (leverage between 1 and 1000000),
    terminal_connected boolean not null default false,
    terminal_build integer not null default 0,
    positions jsonb not null default '[]'::jsonb check (jsonb_typeof(positions) = 'array'),
    updated_at timestamptz not null default now()
);

create index if not exists lm_mt5_account_snapshots_user_updated_idx
    on public.lm_mt5_account_snapshots (user_id, updated_at desc);

create table if not exists public.lm_mt5_vps_instances (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references auth.users(id) on delete cascade,
    device_id uuid not null unique references public.lm_mt5_devices(id) on delete cascade,
    provider text not null default 'windows_vps',
    provider_instance_id text,
    hostname text,
    region text,
    status text not null default 'pending'
        check (status in ('pending', 'provisioning', 'online', 'offline', 'failed', 'released')),
    agent_token_hash text unique,
    agent_version text,
    last_heartbeat_at timestamptz,
    metadata jsonb not null default '{}'::jsonb check (jsonb_typeof(metadata) = 'object'),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists lm_mt5_vps_instances_user_status_idx
    on public.lm_mt5_vps_instances (user_id, status, updated_at desc);

create table if not exists public.lm_auto_trader_plans (
    plan_key text primary key check (plan_key in ('monthly', 'quarterly', 'six_months')),
    display_name text not null,
    duration_months integer not null check (duration_months in (1, 3, 6)),
    currency text not null default 'USD' check (currency in ('USD', 'UGX')),
    price numeric(12,2),
    enabled boolean not null default false,
    includes_managed_vps boolean not null default true,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    check (price is null or price > 0)
);

insert into public.lm_auto_trader_plans
    (plan_key, display_name, duration_months, currency, price, enabled, includes_managed_vps)
values
    ('monthly', 'LM AUTO TRADER Monthly', 1, 'USD', null, false, true),
    ('quarterly', 'LM AUTO TRADER 3 Months', 3, 'USD', null, false, true),
    ('six_months', 'LM AUTO TRADER 6 Months', 6, 'USD', null, false, true)
on conflict (plan_key) do nothing;

create table if not exists public.lm_auto_trader_subscriptions (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references auth.users(id) on delete cascade,
    plan_key text not null references public.lm_auto_trader_plans(plan_key),
    status text not null default 'pending'
        check (status in ('pending', 'active', 'expired', 'cancelled', 'refunded')),
    provider text,
    provider_reference text unique,
    amount numeric(12,2),
    currency text not null default 'USD',
    starts_at timestamptz,
    expires_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists lm_auto_trader_subscriptions_user_expiry_idx
    on public.lm_auto_trader_subscriptions (user_id, status, expires_at desc);

alter table public.lm_mt5_account_snapshots enable row level security;
alter table public.lm_mt5_vps_instances enable row level security;
alter table public.lm_auto_trader_plans enable row level security;
alter table public.lm_auto_trader_subscriptions enable row level security;

revoke all on table public.lm_mt5_account_snapshots from anon, authenticated, public;
revoke all on table public.lm_mt5_vps_instances from anon, authenticated, public;
revoke all on table public.lm_auto_trader_plans from anon, authenticated, public;
revoke all on table public.lm_auto_trader_subscriptions from anon, authenticated, public;

grant select, insert, update, delete on table public.lm_mt5_account_snapshots to service_role;
grant select, insert, update, delete on table public.lm_mt5_vps_instances to service_role;
grant select, insert, update, delete on table public.lm_auto_trader_plans to service_role;
grant select, insert, update, delete on table public.lm_auto_trader_subscriptions to service_role;

comment on table public.lm_mt5_account_snapshots is
    'Latest verified account metrics and LM AUTO TRADER-owned open positions reported by the linked MT5 terminal.';
comment on table public.lm_mt5_vps_instances is
    'Managed Windows VPS assignment metadata. Credentials and plaintext agent secrets must never be stored here.';
comment on table public.lm_auto_trader_plans is
    'Separate LM AUTO TRADER subscription catalog. Prices and activation remain disabled until commercial pricing is approved.';
comment on table public.lm_auto_trader_subscriptions is
    'Independent LM AUTO TRADER subscription entitlement; separate from ordinary LM Analyzer subscriptions.';
