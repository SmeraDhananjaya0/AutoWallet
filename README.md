# AutoWallet

An AI agent that pays for its own tools. Claude runs research tasks and every web
search is a paid call, priced by query complexity and settled on the cheapest rail
that can carry the amount. When the wallet runs low mid-task, the agent tops itself
up from a Robinhood-backed treasury and keeps going.

```
                    ┌──────────── /chat ────────────┐
 React UI ──────────▶  Claude agent (search_web tool) │
                    └──────────────┬─────────────────┘
                                   │ charge before results
                                   ▼
                    ┌────────── PaymentRouter ───────┐
   ledger (µUSD) ◀──┤  < $0.50 → Robinhood Chain USDG │──▶ merchant
                    │           (Circle fallback)     │
                    │ ≥ $0.50 → Stripe card           │
                    └──────────────┬─────────────────┘
                                   │ balance under threshold?
                                   ▼
                    AutoTopUp: Robinhood buy → treasury → agent wallet
```

## Run

```bash
# backend - run from the repo root
pip install -r backend/requirements.txt
cp backend/.env.example backend/.env      # ANTHROPIC_API_KEY is the only required key
uvicorn backend.main:app --reload --port 8000

# frontend
npm install
npm run dev                                # http://localhost:5173 (desktop, 1280px+)

# tests
python -m pytest
```

With only `ANTHROPIC_API_KEY` set, everything else runs **simulated**: Robinhood
Chain transfers return fake tx hashes and Robinhood orders are fake fills (both are
tagged `sim` in the UI). Add a Stripe test key to enable card top-ups.

### Demo: "runs out of money mid-task"

1. Click **Drain to $0.002** in the wallet panel (or `POST /demo/set-balance`).
2. Ask the agent to research something.
3. The chat shows an **Auto top-up** card (Robinhood buy → Robinhood Chain transfer),
   then the paid search, then the answer.

## How payments work

| Piece | File | Notes |
|---|---|---|
| Ledger | `backend/ledger.py` | Single source of truth. Integer micro-dollars; two-phase reserve → commit/release so the balance only moves when a rail settles. |
| Pricing | `backend/pricing.py` | Complexity score 1-10 → $0.001-$0.010. |
| Router | `backend/router.py` | `< $0.50` → micropayment rail, `≥ $0.50` → Stripe (Stripe's USD minimum). |
| Robinhood Chain rail | `backend/rails/robinhood_chain.py` | USDG ERC-20 transfers. `simulated` / `testnet` (46630) / `mainnet` (4663). |
| Stripe rail | `backend/rails/stripe_rail.py` | Test card only; refuses live keys unless `STRIPE_ALLOW_LIVE_KEYS=true`. |
| Circle rail | `backend/rails/circle_rail.py` | USDC via Programmable Wallets; fallback micropayment rail. |
| Robinhood treasury | `backend/treasury/robinhood_crypto.py` | Crypto Trading API client (Ed25519-signed requests). |
| Auto top-up | `backend/treasury/auto_topup.py` | Threshold, per-event amount, rolling 24h cap. |
| Agent | `backend/agent.py` | Claude tool loop. `search_web` charges first; a failed payment returns an error and no results. |

**Why a treasury wallet?** Robinhood's Crypto Trading API covers accounts,
holdings, market data and orders, but has **no withdrawal endpoint**. So the
top-up buys on Robinhood (replenishing the position), then sends USDG to the agent
from a treasury wallet on Robinhood Chain that you fund by withdrawing from
Robinhood in the app. The ledger is credited only after both steps succeed.

## Selling search to other agents

- **x402-style paywall** - `POST /search` without payment returns `402` with the
  accepted schemes. Retry with `X-PAYMENT: spt <token>` (a scoped allowance from
  `POST /spt/issue`, capped and tracked per token) or `X-PAYMENT: tx <hash>` (a USDG
  transfer to the merchant, verified on-chain, single use). This follows x402's
  shape, not its exact wire format.
- **Manifest** - `GET /merchant/manifest` describes pricing and payment schemes.
- **MCP server** - exposes `search_web` (runs the 402 → token → paid retry flow)
  and `wallet_status`:

  ```bash
  python -m backend.mcp_server          # stdio, for Claude Code / Claude Desktop
  python -m backend.mcp_server --http   # streamable HTTP, for remote agents
  ```

## Going live (real money)

| Setting | What it does |
|---|---|
| `ROBINHOOD_CHAIN_MODE=testnet` | Real transfers on testnet; set `ROBINHOOD_CHAIN_TOKEN_ADDRESS` to a test ERC-20. |
| `ROBINHOOD_CHAIN_MODE=mainnet` | Real USDG. Needs `AGENT_WALLET_PRIVATE_KEY` (ETH for gas + USDG), `MERCHANT_ADDRESS`, and `TREASURY_PRIVATE_KEY` for top-ups. |
| `ROBINHOOD_CRYPTO_MODE=live` | Real Robinhood market orders. Needs `ROBINHOOD_API_KEY` + `ROBINHOOD_PRIVATE_KEY`. There is no Robinhood sandbox. |
| `BRAVE_SEARCH_API_KEY` | Real search results instead of mock ones. |

Keep `AUTO_TOPUP_DAILY_CAP_USD` low while testing.

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/chat` | Agent turn → response, tool_calls, search_results, events, wallet |
| GET | `/wallet/balance` | `balance_micros`, `balance_usd` |
| GET | `/wallet/status` | Rails, auto top-up policy, Robinhood status |
| POST | `/wallet/topup` | Stripe test-card top-up |
| POST | `/wallet/topup/robinhood` | Run the Robinhood treasury top-up now |
| GET | `/transactions` | Ledger history |
| POST | `/search` | Paid search (x402-style) |
| POST | `/spt/issue`, `/spt/verify` | Scoped payment tokens |
| GET | `/merchant/manifest` | Service manifest |
| POST | `/demo/set-balance`, `/demo/external-payment` | Demo helpers (`DEMO_MODE=true`) |

## Secrets

Real keys go only in `backend/.env`, which is gitignored. Check before pushing:

```bash
git status                       # backend/.env must not appear
git check-ignore -v backend/.env
```
