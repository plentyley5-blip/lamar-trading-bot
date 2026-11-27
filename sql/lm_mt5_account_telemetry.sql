-- Add live account telemetry to existing linked MT5 devices.
-- Values come only from the linked MT5 terminal using its device token.
alter table public.lm_mt5_devices
    add column if not exists account_balance numeric(24,8),
    add column if not exists account_equity numeric(24,8),
    add column if not exists account_margin numeric(24,8),
    add column if not exists account_free_margin numeric(24,8),
    add column if not exists account_floating_profit numeric(24,8),
    add column if not exists account_currency text,
    add column if not exists account_positions jsonb not null default '[]'::jsonb,
    add column if not exists account_snapshot_at timestamptz,
    add column if not exists account_ea_auto_trade_enabled boolean not null default false;

alter table public.lm_mt5_devices
    drop constraint if exists lm_mt5_devices_account_positions_array_check;

alter table public.lm_mt5_devices
    add constraint lm_mt5_devices_account_positions_array_check
    check (jsonb_typeof(account_positions) = 'array');

comment on column public.lm_mt5_devices.account_positions is
    'Latest validated open-position snapshot reported by the linked MT5 terminal.';

comment on column public.lm_mt5_devices.account_snapshot_at is
    'Timestamp of the latest account and positions telemetry snapshot.';

comment on column public.lm_mt5_devices.account_ea_auto_trade_enabled is
    'Local InpEnableAutoTrading permission reported by the linked MT5 Expert Advisor.';
