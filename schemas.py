"""Tool schemas exposed to the model."""

MARKETS = {
    "name": "polydesk_markets",
    "description": (
        "Find live Polymarket prediction markets and read their odds. With `query`, searches open "
        "markets; with `market` (slug, id or polymarket.com URL), returns that market's outcomes, "
        "prices and live order book (best bid/ask, spread, depth). With neither, lists the top "
        "markets by 24h volume. Use this before paper-trading to get the exact market slug and "
        "outcome names."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Words to search for, e.g. 'fed rate cut december'."},
            "market": {"type": "string", "description": "Market slug, numeric id or polymarket.com URL."},
            "limit": {"type": "integer", "description": "Max markets to list (1-20, default 8)."},
        },
        "required": [],
    },
}

TRADE = {
    "name": "polydesk_trade",
    "description": (
        "Paper-trade a Polymarket outcome against the LIVE order book (no real money). Fills sweep "
        "real book levels, so slippage is realistic. Two steps: (1) call without ticket_id to get a "
        "quote + risk check + ticket_id; show the user the shares, average price, cost and max "
        "payout and ask them to confirm; (2) only after the user confirms, call again with just "
        "ticket_id to execute. Tickets expire after 120 s and are re-priced at execution. "
        "Buy with amount_usd; sell with shares (omit shares to sell the whole position)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "market": {"type": "string", "description": "Market slug, id or polymarket.com URL."},
            "outcome": {"type": "string", "description": "Outcome name exactly as listed, e.g. 'Yes' or 'No'."},
            "side": {"type": "string", "enum": ["buy", "sell"]},
            "amount_usd": {"type": "number", "description": "Paper USDC to spend (buy only)."},
            "shares": {"type": "number", "description": "Shares to sell (sell only; omit = all)."},
            "ticket_id": {"type": "string", "description": "Ticket from a previous preview; executes it."},
        },
        "required": [],
    },
}

PORTFOLIO = {
    "name": "polydesk_portfolio",
    "description": (
        "Show the paper-trading portfolio: cash, every open Polymarket position marked to the live "
        "mid price, unrealized and realized P&L, total return, and how much of the bankroll is "
        "backed by Base Sepolia testnet USDC. Automatically settles positions in markets that have "
        "resolved (winning shares pay $1, losing shares $0)."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}

HISTORY = {
    "name": "polydesk_history",
    "description": "List recent paper-desk activity: buys, sells, settlements and deposits, newest first.",
    "parameters": {
        "type": "object",
        "properties": {"limit": {"type": "integer", "description": "How many entries (default 20)."}},
        "required": [],
    },
}

WALLET = {
    "name": "polydesk_wallet",
    "description": (
        "Back the paper bankroll with a Base Sepolia (Coinbase L2 testnet) wallet. "
        "action=status shows the linked address and its live testnet ETH/USDC balances; "
        "action=link with `address` links a 0x wallet (read-only: no keys, no signing); "
        "action=deposit with `amount_usd` moves that much testnet USDC into paper cash, capped at "
        "what the wallet actually holds on-chain. Testnet USDC comes from faucet.circle.com."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["status", "link", "deposit"]},
            "address": {"type": "string", "description": "0x… address (for link)."},
            "amount_usd": {"type": "number", "description": "Testnet USDC to deposit (for deposit)."},
        },
        "required": ["action"],
    },
}

ALL = [MARKETS, TRADE, PORTFOLIO, HISTORY, WALLET]
