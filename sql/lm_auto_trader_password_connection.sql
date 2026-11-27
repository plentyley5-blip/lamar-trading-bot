-- LM AUTO TRADER direct MT5 credential connection.
-- The trading password is forwarded in-memory to a private Windows MT5 connector.
-- It is intentionally NOT stored in Supabase.

alter table public.lm_mt5_devices
    add column if not exists broker_name text;

comment on column public.lm_mt5_devices.broker_name is
    'User-selected MT5 broker brand/name. Never store broker passwords in this table.';

-- The mobile/backend one-time link-code flow has been removed.
drop table if exists public.lm_mt5_link_codes;
