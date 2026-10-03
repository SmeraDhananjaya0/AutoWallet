import { formatUsd, truncateId } from '../format';

const STEP_LABELS = {
  robinhood_buy: 'Bought on Robinhood',
  robinhood_chain_transfer: 'Treasury → agent on Robinhood Chain',
  portfolio_sell: 'Sold from portfolio',
};

export default function TopUpCard({ amount_usd, status, trigger, source, steps = [], error }) {
  const ok = status === 'completed';

  return (
    <div className={`mt-2 rounded-lg border p-3 ${ok ? 'border-robinhood/40 bg-robinhood/5' : 'border-spend/50 bg-bg/60'}`}>
      <div className="mb-2 flex items-center justify-between gap-2">
        <span className="rounded bg-robinhood/20 px-2 py-0.5 text-xs font-medium uppercase tracking-wide text-robinhood">
          {trigger === 'low_balance' ? 'Auto top-up' : 'Top-up'}
        </span>
        <span className={`font-mono text-sm font-medium ${ok ? 'text-success' : 'text-spend'}`}>
          {ok ? '+' : ''}
          {formatUsd(amount_usd)}
        </span>
      </div>
      {trigger === 'low_balance' && (
        <p className="mb-2 text-xs text-muted">
          {source === 'portfolio'
            ? 'Wallet ran low mid-task, so the agent sold part of its portfolio to keep going.'
            : 'Wallet ran low mid-task, so the agent refilled it before paying.'}
        </p>
      )}
      <ol className="space-y-1 text-xs">
        {steps.map((step) => (
          <li key={step.step} className="flex items-center justify-between gap-2">
            <span className={step.ok ? 'text-text/90' : 'text-spend'}>
              {step.ok ? '✓' : '✕'} {step.detail || STEP_LABELS[step.step] || step.step}
              {step.simulated && <span className="ml-1 text-muted">(sim)</span>}
            </span>
            <span className="font-mono text-muted" title={step.order_id || step.tx_hash}>
              {step.explorer_url ? (
                <a href={step.explorer_url} target="_blank" rel="noreferrer" className="text-accent hover:underline">
                  {truncateId(step.tx_hash, 12)} ↗
                </a>
              ) : (
                truncateId(step.order_id || step.tx_hash, 12)
              )}
            </span>
          </li>
        ))}
      </ol>
      {error && <p className="mt-2 text-xs text-spend">{error}</p>}
    </div>
  );
}
