# Polydesk: a paper-trading desk for Polymarket, built for Hermes Agent

Polydesk lets Hermes trade Polymarket prediction markets **on paper, against the live order
book**. Your agent can research a market, quote a trade, show you the real cost and
slippage, and execute only after you confirm. It tracks P&L and settles positions when markets
resolve. The bankroll can be backed 1:1 by testnet USDC in your **Base Sepolia** wallet
(Coinbase's L2 testnet), so it pairs with Base tooling like `basekit`.

> No keys. No signing. No real orders. No real money. Not financial advice.

## Why it's more than a prompt

- **Real fills, real slippage.** Every order walks the live CLOB book level by level, so a
  $500 buy in a thin market gets the price it would actually get, not the headline odds.
- **Quote, then confirm.** A trade first returns a single-use ticket that expires in 5 minutes.
  At execution it's re-priced against a fresh book and rejected if the price moved against you.
- **Risk limits.** Max per-trade size, max % of equity in one outcome, a max-slippage sweep
  guard, cash checks, and a liquidity check. Rejections explain exactly why.
- **Persistent ledger.** SQLite in the plugin's own data folder. Mark-to-mid portfolio,
  liquidation value, realized and unrealized P&L, full activity log.
- **Auto-settlement.** When a market resolves, winning shares pay $1 and losing shares pay $0.
- **Testnet-backed bankroll.** Link a Base Sepolia address (public address only) and deposit up
  to its on-chain testnet USDC balance.

## Tools

| Tool | What it does |
|---|---|
| `polydesk_markets` | Search open markets, list trending ones, or read one market's outcomes and live order book |
| `polydesk_trade` | Quote → ticket → confirm → fill against the live book (buy by USD, sell by shares or all) |
| `polydesk_portfolio` | Cash, positions marked to market, P&L, return, bankroll backing; settles resolved markets |
| `polydesk_history` | Buys, sells, settlements and deposits |
| `polydesk_wallet` | `status` / `link` / `deposit` for a Base Sepolia wallet |

Slash command: `/desk` (portfolio), `/desk history`, `/desk reset confirm`.
A bundled `polydesk` skill teaches the agent the confirm-before-execute workflow.

## Try it

```
hermes plugins install clatsonhacks/hermes-polydesk
hermes plugins enable polydesk
hermes
> What are the odds the Fed cuts in December? Put $50 on whichever side you think is underpriced.
> /desk
```

To back the bankroll with testnet funds, get Base Sepolia USDC at https://faucet.circle.com, then:

```
> Link my testnet wallet 0xYourAddress and deposit 10 USDC
```

## Settings

Settings are under `plugins.entries.polydesk.settings`, or in the Desktop plugin form.

| Key | Default | |
|---|---|---|
| `demo_bankroll` | 1000 | Unbacked paper cash on first use (0 = testnet-backed only) |
| `max_trade_usd` | 250 | Per-trade cap |
| `max_position_pct` | 25 | Max % of equity in one outcome |
| `max_slippage` | 0.03 | Max sweep from best price, and max adverse re-price at execution |
| `auto_execute` | false | Skip the confirmation ticket (for scheduled strategies) |
| `rpc_url` | https://sepolia.base.org | Base Sepolia JSON-RPC (chain id is verified) |
| `wallet_address` | "" | Optional default testnet address |

## Disclosure

- **Network:** HTTPS GET to `gamma-api.polymarket.com` and `clob.polymarket.com` only (the client
  refuses any other host). JSON-RPC `eth_chainId`, `eth_getBalance` and `eth_call` (USDC `balanceOf`)
  to `sepolia.base.org` or your configured `rpc_url`, only when you use `polydesk_wallet`. The
  wallet address you link is sent to that RPC.
- **Storage:** a SQLite ledger at `<HERMES_HOME>/plugin-data/polydesk/desk.db`. It reads nothing
  else on disk.
- **No** credentials, private keys, signing, transactions, shell commands, background processes,
  telemetry, self-updating code or binaries. Standard library only.

## Development

```
python -m pytest tests -q        # offline tests, Polymarket and the RPC are mocked
hermes plugins validate . --install-deps
```

MIT licensed.
