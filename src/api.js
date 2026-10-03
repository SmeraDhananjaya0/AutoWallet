// All API calls go under /api: Vite proxies it locally, Vercel routes it to the Python function.
const API = '/api';

async function request(path, options = {}) {
  const res = await fetch(`${API}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    const detail = data?.detail || data?.event?.error || data?.reason;
    const err = new Error(detail ? String(detail) : `API error: ${res.status}`);
    err.data = data;
    throw err;
  }
  return data;
}

const post = (url, body) =>
  request(url, { method: 'POST', body: body ? JSON.stringify(body) : undefined });

export const getBalance = () => request('/wallet/balance');
export const getWalletStatus = () => request('/wallet/status');
export const getTransactions = () => request('/transactions');
export const topUpCard = () => post('/wallet/topup');
export const topUpRobinhood = () => post('/wallet/topup/robinhood');
export const setDemoBalance = (balanceUsd) => post('/demo/set-balance', { balance_usd: balanceUsd });

export const getPortfolio = () => request('/portfolio');
export const getTradingRules = () => request('/trading/rules');
export const saveTradingRules = (rules) =>
  request('/trading/rules', { method: 'PUT', body: JSON.stringify(rules) });
export const decideTrade = (id, action) => post(`/trades/${id}/${action}`);

export const sendChatMessage = (message, sessionId = null) =>
  post('/chat', { message, ...(sessionId ? { session_id: sessionId } : {}) });

export const WELCOME_MESSAGE = {
  id: 'msg_welcome',
  role: 'agent',
  text:
    "Hi, I'm AutoWallet. I pay for my own tools: each web search costs $0.001–$0.010 depending on how complex it is, " +
    'settled on Robinhood Chain. If my wallet runs low, I top it up from a Robinhood-backed treasury. ' +
    'I can also research stocks and propose play-money trades on Robinhood Chain testnet at real prices — you approve every one.',
};
