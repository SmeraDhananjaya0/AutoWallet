import { useEffect, useState } from 'react';

const ALL_SYMBOLS = ['NVDA', 'AAPL', 'TSLA', 'GOOGL', 'MSFT', 'SPY'];

function Toggle({ checked, onChange, label, hint, danger = false }) {
  return (
    <label className="flex cursor-pointer items-start justify-between gap-4 py-2">
      <span>
        <span className="text-sm text-text">{label}</span>
        {hint && <span className="block text-xs text-muted">{hint}</span>}
      </span>
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        onClick={() => onChange(!checked)}
        className={`relative mt-0.5 h-5 w-9 shrink-0 rounded-full transition ${
          checked ? (danger ? 'bg-success' : 'bg-accent') : 'bg-border'
        }`}
      >
        <span
          className={`absolute top-0.5 h-4 w-4 rounded-full bg-white transition ${checked ? 'left-4' : 'left-0.5'}`}
        />
      </button>
    </label>
  );
}

export default function RulesPanel({ rules, onSave }) {
  const [draft, setDraft] = useState(rules);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => setDraft(rules), [rules]);
  if (!draft) return <p className="px-6 py-8 text-center text-sm text-muted">Loading rules…</p>;

  const set = (key, value) => setDraft((d) => ({ ...d, [key]: value }));
  const dirty = JSON.stringify(draft) !== JSON.stringify(rules);

  const save = async (next = draft) => {
    setSaving(true);
    setError(null);
    try {
      await onSave(next);
    } catch (err) {
      setError(err.message);
    } finally {
      setSaving(false);
    }
  };

  const toggleSymbol = (s) =>
    set('allowed_symbols', draft.allowed_symbols.includes(s)
      ? draft.allowed_symbols.filter((x) => x !== s)
      : [...draft.allowed_symbols, s]);

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-y-auto px-6 py-4">
      <div
        className={`rounded-lg border p-3 ${draft.trading_enabled ? 'border-border' : 'border-spend/60 bg-spend/10'}`}
      >
        <Toggle
          checked={draft.trading_enabled}
          onChange={(v) => save({ ...draft, trading_enabled: v })}
          label={draft.trading_enabled ? 'Trading enabled' : 'Kill switch ON: all trading halted'}
          hint="Applies instantly. Pending proposals can't execute while this is off."
          danger
        />
      </div>

      <div className="mt-4 space-y-3">
        <label className="block">
          <span className="text-sm text-text">Max per trade</span>
          <div className="mt-1 flex items-center gap-2">
            <span className="text-muted">$</span>
            <input
              type="number"
              min="1"
              step="1"
              value={draft.max_trade_usd}
              onChange={(e) => set('max_trade_usd', Number(e.target.value))}
              className="w-28 rounded-lg border border-border bg-bg px-3 py-1.5 font-mono text-sm focus:border-accent focus:outline-none"
            />
          </div>
        </label>
        <label className="block">
          <span className="text-sm text-text">Daily limit (buys + sells, 24h)</span>
          <div className="mt-1 flex items-center gap-2">
            <span className="text-muted">$</span>
            <input
              type="number"
              min="1"
              step="1"
              value={draft.daily_limit_usd}
              onChange={(e) => set('daily_limit_usd', Number(e.target.value))}
              className="w-28 rounded-lg border border-border bg-bg px-3 py-1.5 font-mono text-sm focus:border-accent focus:outline-none"
            />
          </div>
        </label>

        <div>
          <span className="text-sm text-text">Allowed tickers</span>
          <div className="mt-1 flex flex-wrap gap-1.5">
            {ALL_SYMBOLS.map((s) => {
              const on = draft.allowed_symbols.includes(s);
              return (
                <button
                  key={s}
                  type="button"
                  onClick={() => toggleSymbol(s)}
                  className={`rounded-full border px-3 py-1 font-mono text-xs transition ${
                    on ? 'border-robinhood bg-robinhood/10 text-robinhood' : 'border-border text-muted'
                  }`}
                >
                  d{s}
                </button>
              );
            })}
          </div>
        </div>

        <Toggle
          checked={draft.fund_from_portfolio}
          onChange={(v) => set('fund_from_portfolio', v)}
          label="Fund spending from portfolio"
          hint="When the wallet runs low, sell holdings before tapping the Robinhood treasury."
        />
      </div>

      <div className="mt-4 flex items-center gap-3">
        <button
          type="button"
          onClick={() => save()}
          disabled={!dirty || saving}
          className="rounded-lg bg-accent px-4 py-1.5 text-sm font-medium text-white transition hover:bg-accent/90 disabled:opacity-40"
        >
          {saving ? 'Saving…' : 'Save rules'}
        </button>
        {error && <span className="text-xs text-spend">{error}</span>}
      </div>
      <p className="mt-4 text-xs text-muted">
        The agent can only propose trades. Every proposal is checked against these rules, and again when you approve it.
      </p>
    </div>
  );
}
