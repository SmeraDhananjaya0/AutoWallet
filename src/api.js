const MOCK_BALANCE_CENTS = 1000;
const MOCK_TRANSACTIONS = [
  {
    id: 'txn_001',
    timestamp: '2026-06-04T10:15:00Z',
    reason: 'Web search: AI funding rounds',
    amount_cents: 1,
    stripe_charge_id: 'ch_3PxK9m2nQ8vL4wR7',
  },
  {
    id: 'txn_002',
    timestamp: '2026-06-03T18:42:00Z',
    reason: 'Web search: competitor pricing',
    amount_cents: 1,
    stripe_charge_id: 'ch_3PxJ7k1mN5tH2yU6',
  },
  {
    id: 'txn_003',
    timestamp: '2026-06-02T09:00:00Z',
    reason: 'Wallet top-up',
    amount_cents: -1000,
    stripe_charge_id: 'ch_topup_test_001',
  },
];

const MOCK_CHAT_RESPONSE = {
  response:
    'Here is a summary of recent AI funding activity. Anthropic raised a $2B round in early 2026, OpenAI closed a strategic partnership worth $6.5B, and several agentic-AI startups (Cognition, Sierra) announced Series B rounds above $100M.',
  tool_calls: [],
  transactions: [],
};

let balanceCents = MOCK_BALANCE_CENTS;
let transactions = [...MOCK_TRANSACTIONS];

const USE_MOCK = true;

async function fetchJson(url, options) {
  if (USE_MOCK) return null;
  const res = await fetch(url, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

export async function getBalance() {
  const data = await fetchJson('/wallet/balance');
  if (data) return data;
  return { balance_cents: balanceCents };
}

export async function topUpWallet() {
  const data = await fetchJson('/wallet/topup', { method: 'POST' });
  if (data) return data;
  balanceCents += 1000;
  const txn = {
    id: `txn_${Date.now()}`,
    timestamp: new Date().toISOString(),
    reason: 'Wallet top-up',
    amount_cents: -1000,
    stripe_charge_id: `ch_topup_${Date.now()}`,
  };
  transactions = [txn, ...transactions];
  return { balance_cents: balanceCents };
}

export async function getTransactions() {
  const data = await fetchJson('/transactions');
  if (data) return data;
  return [...transactions];
}

export async function sendChatMessage(message) {
  const data = await fetchJson('/chat', {
    method: 'POST',
    body: JSON.stringify({ message }),
  });
  if (data) return data;

  const lower = message.toLowerCase();
  if (lower.includes('funding') || lower.includes('research')) {
    const searchTxn = {
      id: `txn_${Date.now()}`,
      timestamp: new Date().toISOString(),
      reason: 'Web search: AI funding rounds',
      amount_cents: 1,
      stripe_charge_id: `ch_search_${Date.now().toString(36)}`,
    };
    balanceCents -= 1;
    transactions = [searchTxn, ...transactions];
    return {
      response: MOCK_CHAT_RESPONSE.response,
      tool_calls: [
        {
          tool: 'web_search',
          reason: 'Look up latest AI funding rounds',
          amount_cents: 1,
          stripe_charge_id: searchTxn.stripe_charge_id,
        },
      ],
      search_results: [
        {
          query: 'latest AI funding rounds 2026',
          snippet:
            'Anthropic Series E ($2B), OpenAI strategic round ($6.5B), Cognition Series B ($175M) — sources: TechCrunch, Bloomberg.',
        },
      ],
      transactions: [searchTxn],
    };
  }

  return {
    response: `I received your message: "${message}". I'm ready to help — ask me to research something and I'll use paid tools from your wallet.`,
    tool_calls: [],
    search_results: [],
    transactions: [],
  };
}

export const INITIAL_MESSAGES = [
  {
    id: 'msg_1',
    role: 'user',
    content: 'research the latest AI funding rounds',
  },
  {
    id: 'msg_2',
    role: 'agent',
    content: null,
    tool_calls: [
      {
        tool: 'web_search',
        reason: 'Look up latest AI funding rounds',
        amount_cents: 1,
        stripe_charge_id: 'ch_3PxK9m2nQ8vL4wR7',
      },
    ],
    search_results: [
      {
        query: 'latest AI funding rounds 2026',
        snippet:
          'Anthropic Series E ($2B), OpenAI strategic round ($6.5B), Cognition Series B ($175M) — sources: TechCrunch, Bloomberg.',
      },
    ],
    text: 'Here is a summary of recent AI funding activity. Anthropic raised a $2B round in early 2026, OpenAI closed a strategic partnership worth $6.5B, and several agentic-AI startups announced Series B rounds above $100M.',
  },
];
