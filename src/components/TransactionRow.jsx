function formatCents(cents) {
  const abs = Math.abs(cents);
  const sign = cents > 0 ? '-' : '+';
  return `${sign}$${(abs / 100).toFixed(2)}`;
}

function formatTime(iso) {
  return new Date(iso).toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

function truncateId(id, len = 14) {
  if (!id || id.length <= len) return id;
  return `${id.slice(0, len)}…`;
}

export default function TransactionRow({ transaction }) {
  const { timestamp, reason, amount_cents, stripe_charge_id } = transaction;
  const isCredit = amount_cents < 0;

  return (
    <div className="flex items-start justify-between gap-3 border-b border-border py-3 last:border-b-0">
      <div className="min-w-0 flex-1">
        <p className="text-xs text-muted">{formatTime(timestamp)}</p>
        <p className="mt-0.5 truncate text-sm text-text">{reason}</p>
        <p className="mt-1 font-mono text-xs text-muted" title={stripe_charge_id}>
          {truncateId(stripe_charge_id)}
        </p>
      </div>
      <span
        className={`shrink-0 font-mono text-sm font-medium ${isCredit ? 'text-success' : 'text-spend'}`}
      >
        {formatCents(amount_cents)}
      </span>
    </div>
  );
}
