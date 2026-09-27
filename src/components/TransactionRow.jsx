import RailBadge from './RailBadge';
import { formatUsd, truncateId } from '../format';

function formatTime(iso) {
  return new Date(iso).toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

export default function TransactionRow({ transaction }) {
  const { timestamp, reason, amount_usd, rail, reference, simulated, explorer_url, status } = transaction;
  const isCredit = amount_usd < 0;

  return (
    <div className="flex items-start justify-between gap-3 border-b border-border py-3 last:border-b-0">
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <p className="text-xs text-muted">{formatTime(timestamp)}</p>
          <RailBadge rail={rail} simulated={simulated} />
          {status === 'submitted' && <span className="text-[10px] uppercase text-muted">pending</span>}
        </div>
        <p className="mt-0.5 truncate text-sm text-text" title={reason}>
          {reason}
        </p>
        {reference &&
          (explorer_url ? (
            <a
              href={explorer_url}
              target="_blank"
              rel="noreferrer"
              className="mt-1 block font-mono text-xs text-accent hover:underline"
              title={reference}
            >
              {truncateId(reference)} ↗
            </a>
          ) : (
            <p className="mt-1 font-mono text-xs text-muted" title={reference}>
              {truncateId(reference)}
            </p>
          ))}
      </div>
      <span className={`shrink-0 font-mono text-sm font-medium ${isCredit ? 'text-success' : 'text-spend'}`}>
        {isCredit ? '+' : '-'}
        {formatUsd(amount_usd)}
      </span>
    </div>
  );
}
