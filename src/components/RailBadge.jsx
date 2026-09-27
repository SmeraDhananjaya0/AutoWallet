import { railLabel } from '../format';

const RAIL_STYLES = {
  robinhood_chain: 'bg-robinhood/15 text-robinhood',
  robinhood: 'bg-robinhood/15 text-robinhood',
  stripe: 'bg-accent/15 text-accent',
  circle: 'bg-sky-400/15 text-sky-400',
};

export default function RailBadge({ rail, simulated = false }) {
  return (
    <span className="inline-flex items-center gap-1">
      <span
        className={`rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${
          RAIL_STYLES[rail] || 'bg-border text-muted'
        }`}
      >
        {railLabel(rail)}
      </span>
      {simulated && (
        <span className="rounded bg-border px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-muted">
          sim
        </span>
      )}
    </span>
  );
}
