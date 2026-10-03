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

### Demo: both rails in one chat

Ask: *"Write me a deep research report on Robinhood Chain: its launch, adoption numbers, partners, and criticisms. Use the premium report tool."*
The report ($0.75) settles on Stripe; ordinary searches ($0.001-$0.010) settle on Robinhood Chain.

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
| Premium report | `backend/agent.py`, `backend/search.py` | `deep_research_report`: flat $0.75 (`REPORT_PRICE_USD`), so it settles on **Stripe**; several Claude web searches then a sourced report. Refunds the card if the report fails after payment. |
| Search | `backend/search.py` | `SEARCH_PROVIDER=auto`: Brave if keyed, else Claude web search (your Anthropic key), else mock. |

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

## Demo trading (testnet, play money)

The agent can research stocks and **propose** trades; only you can approve them.

- **Assets:** play-money demo stock tokens (`dNVDA`, `dAAPL`, `dTSLA`, `dGOOGL`, `dMSFT`, `dSPY`) on Robinhood Chain
  testnet. They are *not* Robinhood Stock Tokens.
- **Prices:** real, read for free from the Chainlink feed for each Robinhood Stock Token on Robinhood Chain mainnet
  (`backend/trading/prices.py`).
- **Exchange:** `contracts/DemoExchange.sol` swaps tUSDG for demo tokens at the posted Chainlink price (mint on buy,
  burn on sell) and rejects prices older than an hour. Deploy with `python scripts/deploy_demo_exchange.py`.
- **Flow:** Claude calls `propose_trade` → a proposal card appears → **Approve** executes on testnet (price post if
  stale, one-time tUSDG approval, swap), each step linked to the explorer. Nothing executes without approval.
- **Rules** (Rules tab): kill switch, per-trade cap, 24h limit, allowed tickers, enough cash. Checked when the agent
  proposes *and* again when you approve.
- **Self-funding:** with "Fund spending from portfolio" on, a low wallet sells part of the largest position before
  tapping the Robinhood treasury.

Try: *"Research how NVIDIA is doing lately with one search, then propose buying $10 of it if it looks good."*

## Running on Robinhood Chain testnet (play money)

1. Create three wallets (agent, treasury, merchant) and put their keys in `backend/.env`:
   `AGENT_WALLET_PRIVATE_KEY`, `TREASURY_PRIVATE_KEY`, `MERCHANT_ADDRESS`. Use throwaway wallets, never ones holding real funds.
2. Get free testnet ETH (for gas) for the agent and treasury from a faucet, e.g.
   [QuickNode](https://faucet.quicknode.com/robinhood/testnet). A few thousandths of an ETH is plenty.
3. Deploy the play-money token and fund the wallets. Robinhood publishes no testnet USDG, so
   `contracts/TestUSDG.sol` is a minimal 6-decimal ERC-20 ("tUSDG"):

   ```bash
   pip install py-solc-x
   python scripts/deploy_test_token.py      # saves ROBINHOOD_CHAIN_TOKEN_ADDRESS to backend/.env
   ```
4. Set `ROBINHOOD_CHAIN_MODE=testnet` and restart the API. Transfers now show explorer links to
   [explorer.testnet.chain.robinhood.com](https://explorer.testnet.chain.robinhood.com). The Robinhood *buy* step stays
   simulated because Robinhood has no trading sandbox.

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
