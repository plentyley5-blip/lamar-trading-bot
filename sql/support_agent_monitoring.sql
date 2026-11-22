-- LM Analyzer support-agent monitoring foundation.
-- Applied to the production Supabase project on 2026-11-22.
-- The application backend accesses these tables with the service role only.

create table if not exists public.support_agent_presence (
    agent_id uuid primary key references public.support_agents(user_id) on delete cascade,
    status text not null default 'offline'
        check (status in ('online', 'away', 'offline')),
    last_seen_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists public.support_agent_activity (
    id uuid primary key default gen_random_uuid(),
    agent_id uuid not null references public.support_agents(user_id) on delete cascade,
    action text not null check (action in ('claimed', 'replied', 'resolved')),
    conversation_id uuid references public.support_conversations(id) on delete set null,
    details jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now()
);

create index if not exists support_agent_presence_last_seen_idx
    on public.support_agent_presence(last_seen_at desc);
create index if not exists support_agent_activity_agent_created_idx
    on public.support_agent_activity(agent_id, created_at desc);
create index if not exists support_agent_activity_created_idx
    on public.support_agent_activity(created_at desc);

alter table public.support_agent_presence enable row level security;
alter table public.support_agent_activity enable row level security;

revoke all on public.support_agent_presence from anon, authenticated;
revoke all on public.support_agent_activity from anon, authenticated;
grant all on public.support_agent_presence to service_role;
grant all on public.support_agent_activity to service_role;
