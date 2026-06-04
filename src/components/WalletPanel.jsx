import TransactionRow from './TransactionRow';

function formatBalance(cents) {
  return `$${(cents / 100).toFixed(2)}`;
}

const LOW_BALANCE_CENTS = 200;

export default function WalletPanel({
  balanceCents,
  transactions,
  onTopUp,
  isToppingUp,
}) {
  const isLow = balanceCents < LOW_BALANCE_CENTS;
  const balanceColor = isLow ? 'text-spend' : 'text-success';

  return (
    <section className="flex h-full min-h-0 w-[40%] flex-col bg-bg">
      <header className="shrink-0 border-b border-border px-6 py-4">
        <h2 className="text-sm font-medium uppercase tracking-wider text-muted">Wallet</h2>
      </header>

      <div className="shrink-0 border-b border-border px-6 py-10 text-center">
        <p className="text-xs font-medium uppercase tracking-wider text-muted">Balance</p>
        <p className={`mt-2 font-mono text-5xl font-semibold tracking-tight ${balanceColor}`}>
          {formatBalance(balanceCents)}
        </p>
        <button
          type="button"
          onClick={onTopUp}
          disabled={isToppingUp}
          className="mt-6 rounded-lg border border-accent bg-accent/10 px-6 py-2.5 text-sm font-medium text-accent transition hover:bg-accent/20 disabled:opacity-50"
        >
          {isToppingUp ? 'Adding funds…' : 'Top Up (+$10.00)'}
        </button>
      </div>

      <div className="flex min-h-0 flex-1 flex-col px-6">
        <h3 className="shrink-0 py-4 text-xs font-medium uppercase tracking-wider text-muted">
          Transactions
        </h3>
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
