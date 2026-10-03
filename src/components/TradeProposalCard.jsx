import { useState } from 'react';
import { formatUsd, truncateId } from '../format';

const STATUS = {
  pending: { label: 'Awaiting your approval', cls: 'text-robinhood' },
  executing: { label: 'Executing…', cls: 'text-accent' },
  executed: { label: 'Executed', cls: 'text-success' },
  rejected: { label: 'Rejected', cls: 'text-muted' },
  blocked: { label: 'Blocked by trading rules', cls: 'text-spend' },
  failed: { label: 'Failed', cls: 'text-spend' },
};

export default function TradeProposalCard({ proposal, onDecide }) {
  const [busy, setBusy] = useState(null);
  const p = proposal;
  const status = STATUS[p.status] || { label: p.status, cls: 'text-muted' };
  const isBuy = p.side === 'buy';

  const decide = async (action) => {
    setBusy(action);
    try {
      await onDecide(p.id, action);
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="mt-2 rounded-lg border border-robinhood/40 bg-robinhood/5 p-3">
      <div className="mb-2 flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className="rounded bg-robinhood/20 px-2 py-0.5 text-xs font-medium uppercase tracking-wide text-robinhood">
            Trade proposal
          </span>
          <span className="text-[10px] uppercase tracking-wide text-muted">testnet · play money</span>
        </div>
        <span className={`text-xs font-medium ${status.cls}`}>{status.label}</span>
      </div>

      <div className="flex items-baseline justify-between gap-2">
        <p className="text-base font-semibold text-text">
          <span className={isBuy ? 'text-success' : 'text-spend'}>{isBuy ? 'Buy' : 'Sell'}</span>{' '}
          {formatUsd(p.amount_usd)} of {p.token}
          <span className="ml-2 text-xs font-normal text-muted">{p.name}</span>
        </p>
        <p className="font-mono text-xs text-muted">
          ≈{p.est_shares} @ ${p.price_usd?.toFixed(2)}
        </p>
      </div>
      <p className="mt-1 text-xs text-muted">Price: {p.price_source}</p>

      {p.thesis && <p className="mt-2 text-sm leading-relaxed text-text/90">{p.thesis}</p>}

      {p.sources?.length > 0 && (
        <ul className="mt-2 space-y-0.5">
          {p.sources.map((s) => (
            <li key={s} className="truncate text-xs">
              <a href={s} target="_blank" rel="noreferrer" className="text-accent hover:underline">
                {s}
              </a>
            </li>
          ))}
        </ul>
      )}

      <div className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 border-t border-border pt-2 text-xs">
        {p.checks?.map((c) => (
          <div key={c.rule} className="flex items-center justify-between gap-2">
            <span className={c.ok ? 'text-text/80' : 'text-spend'}>
              {c.ok ? '✓' : '✕'} {c.rule}
            </span>
            <span className="truncate text-muted" title={c.detail}>
              {c.detail}
            </span>
          </div>
        ))}
        {p.research_cost_usd > 0 && (
          <div className="col-span-2 text-muted">Research that informed this: {formatUsd(p.research_cost_usd)}</div>
        )}
      </div>

      {p.status === 'pending' && (
        <div className="mt-3 flex gap-2">
          <button
            type="button"
            onClick={() => decide('approve')}
            disabled={!!busy}
            className="rounded-lg bg-robinhood px-4 py-1.5 text-xs font-semibold text-black transition hover:bg-robinhood/90 disabled:opacity-50"
          >
            {busy === 'approve' ? 'Executing on testnet…' : 'Approve'}
          </button>
          <button
            type="button"
            onClick={() => decide('reject')}
            disabled={!!busy}
            className="rounded-lg border border-border px-4 py-1.5 text-xs font-medium text-muted transition hover:text-text disabled:opacity-50"
          >
            Reject
          </button>
        </div>
      )}

      {p.status === 'executed' && p.fill && (
        <div className="mt-2 border-t border-border pt-2 text-xs">
          <p className="text-success">
            Filled {p.fill.shares} {p.token} @ ${p.fill.price_usd.toFixed(2)} for {formatUsd(p.fill.amount_usd)}
            {p.fill.simulated && <span className="ml-1 text-muted">(sim)</span>}
          </p>
          <ul className="mt-1 space-y-0.5">
            {p.fill.txs?.map((tx) => (
              <li key={tx.hash} className="flex justify-between gap-2 font-mono">
                <span className="text-muted">{tx.label}</span>
                {tx.explorer_url ? (
                  <a href={tx.explorer_url} target="_blank" rel="noreferrer" className="text-accent hover:underline">
                    {truncateId(tx.hash, 12)} ↗
                  </a>
                ) : (
                  <span className="text-muted">{truncateId(tx.hash, 12)}</span>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
      {p.status === 'failed' && p.error && <p className="mt-2 text-xs text-spend">{p.error}</p>}
    </div>
  );
}
