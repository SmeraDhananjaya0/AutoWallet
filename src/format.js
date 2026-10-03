export const RAIL_LABELS = {
  robinhood_chain: 'Robinhood Chain',
  robinhood: 'Robinhood',
  stripe: 'Stripe',
  circle: 'Circle',
};

export function railLabel(rail) {
  return RAIL_LABELS[rail] || rail || 'none';
}

/** Sub-cent aware: $0.0040, $4.9980, $10.00 */
export function formatUsd(amount) {
  const abs = Math.abs(amount);
  return `$${abs.toFixed(abs !== 0 && abs < 100 && !Number.isInteger(abs * 100) ? 4 : 2)}`;
}

/** Always two decimals, e.g. portfolio values: $10.00 (sub-cent dust rounds away). */
export function formatCents(amount) {
  const cents = Math.round(Math.abs(amount) * 100) / 100;
  return `$${cents.toFixed(2)}`;
}

export function truncateId(id, len = 14) {
  if (!id || id.length <= len) return id;
  return `${id.slice(0, len)}…`;
}
