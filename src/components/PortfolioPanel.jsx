import { formatCents } from '../format';

function Pnl({ value }) {
  if (value === null || value === undefined) return <span className="text-muted">—</span>;
  const rounded = Math.round(value * 100) / 100; // ignore sub-cent rounding dust
  const cls = rounded > 0 ? 'text-success' : rounded < 0 ? 'text-spend' : 'text-muted';
  return (
    <span className={`font-mono ${cls}`}>
      {rounded > 0 ? '+' : rounded < 0 ? '-' : ''}
      {formatCents(rounded)}
    </span>
  );
}

export default function PortfolioPanel({ portfolio }) {
  if (!portfolio) return <p className="px-6 py-8 text-center text-sm text-muted">Loading portfolio…</p>;
  const { positions, total_value_usd, total_pnl_usd, cash_usd, exchange } = portfolio;

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-y-auto px-6 py-4">
      <div className="grid grid-cols-3 gap-3 text-center">
        <div className="rounded-lg border border-border p-3">
          <p className="text-[10px] uppercase tracking-wider text-muted">Holdings</p>
          <p className="mt-1 font-mono text-lg text-text">{formatCents(total_value_usd)}</p>
        </div>
        <div className="rounded-lg border border-border p-3">
          <p className="text-[10px] uppercase tracking-wider text-muted">P&amp;L</p>
          <p className="mt-1 text-lg">
            <Pnl value={total_pnl_usd} />
          </p>
        </div>
        <div className="rounded-lg border border-border p-3">
          <p className="text-[10px] uppercase tracking-wider text-muted">Cash</p>
          <p className="mt-1 font-mono text-lg text-text">{formatCents(cash_usd)}</p>
        </div>
      </div>

      <p className="mt-3 text-[11px] text-muted">
        Play-money demo stock tokens on Robinhood Chain {exchange?.mode === 'simulated' ? '(simulated)' : 'testnet'}, priced
        from real Chainlink feeds on mainnet.
      </p>

      <h3 className="mt-4 text-xs font-medium uppercase tracking-wider text-muted">Positions</h3>
      {positions.length === 0 ? (
        <p className="py-6 text-center text-sm text-muted">
          No positions yet. Ask the agent to research a stock and propose a trade.
        </p>
      ) : (
        <table className="mt-2 w-full text-sm">
          <thead>
            <tr className="text-left text-[10px] uppercase tracking-wider text-muted">
              <th className="pb-1 font-medium">Token</th>
              <th className="pb-1 text-right font-medium">Shares</th>
              <th className="pb-1 text-right font-medium">Price</th>
              <th className="pb-1 text-right font-medium">Value</th>
              <th className="pb-1 text-right font-medium">P&amp;L</th>
            </tr>
          </thead>
          <tbody>
            {positions.map((p) => (
              <tr key={p.symbol} className="border-t border-border">
                <td className="py-2">
                  {p.token_url ? (
                    <a href={p.token_url} target="_blank" rel="noreferrer" className="font-medium text-accent hover:underline">
                      {p.token} ↗
                    </a>
                  ) : (
                    <span className="font-medium">{p.token}</span>
                  )}
                </td>
                <td className="py-2 text-right font-mono text-xs">{p.shares}</td>
                <td className="py-2 text-right font-mono text-xs">${p.price_usd?.toFixed(2)}</td>
                <td className="py-2 text-right font-mono text-xs">{formatCents(p.value_usd)}</td>
                <td className="py-2 text-right text-xs">
                  <Pnl value={p.pnl_usd} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
