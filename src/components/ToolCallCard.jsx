import RailBadge from './RailBadge';
import { formatUsd, truncateId } from '../format';

export default function ToolCallCard({
  tool,
  query,
  score,
  amount_usd,
  rail,
  status,
  reference,
  simulated,
  explorer_url,
  error,
}) {
  const failed = status === 'failed';

  return (
    <div className={`mt-2 rounded-lg border bg-bg/60 p-3 ${failed ? 'border-spend/50' : 'border-border'}`}>
      <div className="mb-2 flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className="rounded bg-accent/20 px-2 py-0.5 text-xs font-medium uppercase tracking-wide text-accent">
            Tool call
          </span>
          <span className="font-mono text-sm font-medium text-text">{tool}</span>
        </div>
        {rail && <RailBadge rail={rail} simulated={simulated} />}
      </div>
      <p className="text-sm text-muted">
        “{query}”{score ? <span className="ml-2 font-mono text-xs">complexity {score}/10</span> : null}
      </p>
      <div className="mt-2 flex flex-wrap items-center justify-between gap-2 border-t border-border pt-2 text-xs">
        {failed ? (
          <span className="font-medium text-spend">Payment failed: {error}</span>
        ) : (
          <span className="font-medium text-spend">Paid {formatUsd(amount_usd)}</span>
        )}
        {reference &&
          (explorer_url ? (
            <a
              href={explorer_url}
              target="_blank"
              rel="noreferrer"
              className="font-mono text-accent hover:underline"
              title={reference}
            >
              {truncateId(reference, 16)} ↗
            </a>
          ) : (
            <span className="font-mono text-muted" title={reference}>
              {truncateId(reference, 16)}
            </span>
          ))}
      </div>
    </div>
  );
}
