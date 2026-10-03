-- Demo trading (play-money stock tokens on Robinhood Chain testnet).
create table autowallet.trade_proposals (
  id text primary key,
  data jsonb not null,
  status text not null check (status in ('pending', 'executing', 'executed', 'rejected', 'blocked', 'failed')),
  notional_micros bigint,
  created_at timestamptz not null default now(),
  executed_at timestamptz
);
create index trade_proposals_executed_idx on autowallet.trade_proposals (status, executed_at);

create table autowallet.positions (
  symbol text primary key,
  shares_units numeric(78, 0) not null default 0 check (shares_units >= 0),  -- 1e18 units = 1 share
  cost_micros bigint not null default 0 check (cost_micros >= 0)
);

grant select, insert, update, delete on autowallet.trade_proposals, autowallet.positions to autowallet_app;
