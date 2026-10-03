-- AutoWallet state for multi-instance hosting (applied to Supabase project "autowallet").
-- Private schema: not exposed through Supabase's Data API.
create schema if not exists autowallet;

create table autowallet.wallet (
  id smallint primary key default 1 check (id = 1),
  balance_micros bigint not null check (balance_micros >= 0)
);

-- Two-phase spending: a hold is money reserved while a rail settles.
create table autowallet.holds (
  id text primary key,
  amount_micros bigint not null check (amount_micros > 0),
  reason text not null,
  created_at timestamptz not null default now()
);

create table autowallet.transactions (
  seq bigserial primary key,
  id text not null unique,
  kind text not null check (kind in ('charge', 'topup')),
  reason text not null,
  amount_micros bigint not null check (amount_micros > 0),
  rail text not null,
  reference text not null default '',
  status text not null default 'settled',
  simulated boolean not null default false,
  explorer_url text,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);
create index transactions_reference_idx on autowallet.transactions (lower(reference));
create index transactions_topups_idx on autowallet.transactions (kind, rail, created_at);

-- Scoped payment tokens: cumulative spend enforced across server instances.
create table autowallet.spt_grants (
  jti text primary key,
  agent_id text not null,
  scope text not null,
  max_micros bigint not null check (max_micros > 0),
  spent_micros bigint not null default 0 check (spent_micros >= 0 and spent_micros <= max_micros),
  expires_at timestamptz not null
);

-- On-chain payment proofs accepted by /search (single use).
create table autowallet.used_payment_proofs (
  tx_hash text primary key,
  used_at timestamptz not null default now()
);

create table autowallet.chat_sessions (
  id text primary key,
  messages jsonb not null default '[]'::jsonb,
  updated_at timestamptz not null default now()
);

create table autowallet.meta (
  key text primary key,
  value text not null
);

-- Least-privilege login for the app: only this schema, no DDL.
-- Set its password separately, e.g. with a SCRAM verifier so the plaintext
-- never leaves your machine:  alter role autowallet_app with login password 'SCRAM-SHA-256$...';
create role autowallet_app nologin;
grant usage on schema autowallet to autowallet_app;
grant select, insert, update, delete on all tables in schema autowallet to autowallet_app;
grant usage, select on all sequences in schema autowallet to autowallet_app;
