async function request(url, options = {}) {
  const res = await fetch(url, {
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

export const sendChatMessage = (message, sessionId = null) =>
  post('/chat', { message, ...(sessionId ? { session_id: sessionId } : {}) });

export const WELCOME_MESSAGE = {
  id: 'msg_welcome',
  role: 'agent',
  text:
    "Hi, I'm AutoWallet. I pay for my own tools: each web search costs $0.001–$0.010 depending on how complex it is, " +
    'settled on Robinhood Chain. If my wallet runs low, I top it up from a Robinhood-backed treasury. ' +
    'Ask me to research something.',
};
