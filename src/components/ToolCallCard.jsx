function formatCents(cents) {
  return `$${(cents / 100).toFixed(2)}`;
}

function truncateId(id, len = 12) {
  if (!id || id.length <= len) return id;
  return `${id.slice(0, len)}…`;
}

export default function ToolCallCard({ tool, reason, amount_cents, stripe_charge_id }) {
  return (
    <div className="mt-2 rounded-lg border border-border bg-bg/60 p-3">
      <div className="mb-2 flex items-center gap-2">
        <span className="rounded bg-accent/20 px-2 py-0.5 text-xs font-medium uppercase tracking-wide text-accent">
          Tool call
        </span>
        <span className="font-mono text-sm font-medium text-text">{tool}</span>
      </div>
      <p className="text-sm text-muted">{reason}</p>
      <div className="mt-2 flex flex-wrap items-center justify-between gap-2 border-t border-border pt-2 text-xs">
        <span className="font-medium text-spend">Charged {formatCents(amount_cents)}</span>
        <span className="font-mono text-muted" title={stripe_charge_id}>
          {truncateId(stripe_charge_id, 16)}
        </span>
      </div>
    </div>
  );
}
