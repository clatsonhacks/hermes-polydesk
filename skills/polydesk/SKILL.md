---
name: polydesk
description: Research Polymarket markets and paper-trade them safely with the Polydesk tools.
---

# Polydesk: paper trading Polymarket

Use the `polydesk_*` tools whenever the user wants to bet on, trade, or track a Polymarket
prediction market without real money.

## Workflow

1. **Find the market.** `polydesk_markets` with `query` returns open markets and their
   outcome prices. Pass the chosen `slug` as `market` to see the live order book
   (best bid/ask, spread, depth).
2. **Quote.** `polydesk_trade` with `market`, `outcome`, `side` and `amount_usd` (buy) or
   `shares` (sell). This returns a quote and a `ticket_id`, or `status: rejected` with
   `problems`.
3. **Confirm.** Show the user the shares, average price (that's the implied probability),
   cost, max payout and spread. Ask "Confirm?" Never execute without a clear yes.
4. **Execute.** Call `polydesk_trade` with only `ticket_id`. The order is re-priced against
   the live book, and if the price moved against the user by more than `max_slippage` it is
   rejected. In that case, quote again.
   Tickets expire after 5 minutes. A fresh quote is a new price, so show it and get a new
   yes before executing it. Never execute a re-quoted ticket on an earlier confirmation.
5. **Track.** `polydesk_portfolio` marks positions to market and settles resolved markets.
   `polydesk_history` lists activity.

## Testnet-backed bankroll

`polydesk_wallet` links a Base Sepolia address (public address only, never a key), shows its
testnet ETH/USDC, and deposits testnet USDC 1:1 into paper cash. Testnet USDC comes from
https://faucet.circle.com (choose Base Sepolia).

## Rules

- Always say this is paper trading. No real orders, no real money.
- Prices are probabilities: 0.62 means the market says 62%. Max payout is $1 per share.
- When a trade is rejected, explain each problem and suggest a fix (smaller size, a
  different outcome, or depositing more).
- Not financial advice.
