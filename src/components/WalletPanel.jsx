import TransactionRow from './TransactionRow';
import RailBadge from './RailBadge';
import { formatUsd } from '../format';

function RailStatus({ status }) {
  if (!status) return null;
  const autoTopUp = status.auto_topup;

  return (
    <div className="space-y-3 border-b border-border px-6 py-4 text-xs">
      <div>
        <p className="mb-2 font-medium uppercase tracking-wider text-muted">Rails</p>
        <ul className="space-y-1.5">
          {status.rails.map((r) => (
            <li key={r.rail} className="flex items-center justify-between gap-2">
              <RailBadge rail={r.rail} />
              <span className={r.available ? 'text-success' : 'text-muted'} title={r.detail}>
                {r.available ? (r.mode === 'simulated' ? 'simulated' : r.mode || 'ready') : 'off'}
                {r.rail === 'stripe' && ` · ≥ ${formatUsd(r.min_charge_usd)}`}
                {r.rail === status.micropayment_rail && ' · < $0.50'}
              </span>
            </li>
          ))}
        </ul>
      </div>
      {autoTopUp && (
        <div>
          <p className="mb-1 font-medium uppercase tracking-wider text-muted">Robinhood auto top-up</p>
          <p className="text-text/80">
            {autoTopUp.enabled
              ? `Below ${formatUsd(autoTopUp.threshold_usd)}, add ${formatUsd(autoTopUp.amount_usd)} · ` +
                `${formatUsd(autoTopUp.spent_last_24h_usd)} of ${formatUsd(autoTopUp.daily_cap_usd)} used today`
              : 'Disabled'}
            <span className="ml-1 text-muted">({autoTopUp.robinhood?.mode})</span>
          </p>
        </div>
      )}
    </div>
  );
}

export default function WalletPanel({
  balanceUsd,
  transactions,
  walletStatus,
  busyAction,
  error,
  onCardTopUp,
  onRobinhoodTopUp,
  onDrain,
}) {
  const threshold = walletStatus?.auto_topup?.threshold_usd ?? 2;
  const balanceColor = balanceUsd < threshold ? 'text-spend' : 'text-success';
  const buttonClass =
    'rounded-lg border px-4 py-2 text-xs font-medium transition disabled:cursor-not-allowed disabled:opacity-50';

  return (
    <section className="flex h-full min-h-0 w-[40%] flex-col bg-bg">
      <header className="shrink-0 border-b border-border px-6 py-4">
        <h2 className="text-sm font-medium uppercase tracking-wider text-muted">Wallet</h2>
      </header>

      <div className="shrink-0 border-b border-border px-6 py-8 text-center">
        <p className="text-xs font-medium uppercase tracking-wider text-muted">Balance</p>
        <p className={`mt-2 font-mono text-5xl font-semibold tracking-tight ${balanceColor}`}>
          ${balanceUsd.toFixed(4)}
        </p>
        <div className="mt-5 flex flex-wrap justify-center gap-2">
          <button
            type="button"
            onClick={onRobinhoodTopUp}
            disabled={!!busyAction}
            className={`${buttonClass} border-robinhood bg-robinhood/10 text-robinhood hover:bg-robinhood/20`}
          >
            {busyAction === 'robinhood' ? 'Buying on Robinhood…' : 'Robinhood top-up'}
          </button>
          <button
            type="button"
            onClick={onCardTopUp}
            disabled={!!busyAction}
            className={`${buttonClass} border-accent bg-accent/10 text-accent hover:bg-accent/20`}
          >
            {busyAction === 'card' ? 'Charging card…' : 'Card top-up (+$10)'}
          </button>
          {walletStatus?.demo_mode && (
            <button
              type="button"
              onClick={onDrain}
              disabled={!!busyAction}
              className={`${buttonClass} border-border text-muted hover:text-text`}
              title="Set the balance to $0.002 so the next paid search triggers an auto top-up"
            >
              Drain to $0.002
            </button>
          )}
        </div>
        {error && <p className="mt-3 text-xs text-spend">{error}</p>}
      </div>

      <RailStatus status={walletStatus} />

      <div className="flex min-h-0 flex-1 flex-col px-6">
        <h3 className="shrink-0 py-4 text-xs font-medium uppercase tracking-wider text-muted">Transactions</h3>
        <div className="flex-1 overflow-y-auto pb-6">
          {transactions.length === 0 ? (
            <p className="py-8 text-center text-sm text-muted">No transactions yet</p>
          ) : (
            transactions.map((txn) => <TransactionRow key={txn.id} transaction={txn} />)
          )}
        </div>
      </div>
    </section>
  );
}
