# AutoWallet

AI agent wallet UI — Claude spends from a wallet to complete tasks.

## Stack

- React 19 + Vite 6
- Tailwind CSS v4
- Mock API in `src/api.js` (flip `USE_MOCK` to `false` when the backend is ready)

## Run

```bash
npm install
npm run dev
```

Open http://localhost:5173 (desktop layout, 1280px+).

## API (when wired)

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/chat` | Send message → response, tool_calls, transactions |
| GET | `/wallet/balance` | `{ balance_cents }` |
| POST | `/wallet/topup` | Add $10 test funds |
| GET | `/transactions` | Transaction list |

Vite proxies these to `http://localhost:8000` when mock mode is off.
