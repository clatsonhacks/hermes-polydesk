"""Tool handlers and the /desk slash command. Handlers never raise: they return JSON."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable

from . import polymarket as pm
from . import wallet as wl
from .desk import Desk

logger = logging.getLogger(__name__)
PLUGIN = "polydesk"
DISCLAIMER = "Paper trading only: no real orders are placed and no real money moves."


def _ok(payload: Any) -> str:
    return json.dumps(payload, default=str)


def _err(msg: str) -> str:
    return json.dumps({"error": msg})


def _data_dir() -> Path:
    try:
        from plugins.plugin_storage import plugin_data_dir
        return plugin_data_dir(PLUGIN)
    except Exception:  # running outside Hermes (tests, dev)
        d = Path.home() / ".polydesk-dev"
        d.mkdir(parents=True, exist_ok=True)
        return d


class Handlers:
    def __init__(self, ctx=None, db_path: Path | None = None):
        self.ctx = ctx
        self._desk: Desk | None = None
        self._db_path = db_path

    # lazily open storage so registration never touches disk
    @property
    def desk(self) -> Desk:
        if self._desk is None:
            self._desk = Desk(self._db_path or (_data_dir() / "desk.db"), self.setting)
        return self._desk

    def setting(self, key: str, default: Any = None) -> Any:
        if self.ctx is None:
            return default
        try:
            val = self.ctx.get_config(key, default=default)
            return default if val is None or val == "" else val
        except Exception:
            return default

    def _guard(fn: Callable) -> Callable:  # noqa: N805
        def wrapper(self, args: dict | None = None, **kwargs) -> str:
            try:
                return fn(self, args or {})
            except (pm.PolymarketError, wl.WalletError, ValueError) as exc:
                return _err(str(exc))
            except Exception as exc:  # never let a tool crash the agent
                logger.exception("polydesk tool failed")
                return _err(f"internal error: {exc}")
        wrapper.__name__ = fn.__name__
        return wrapper

    # ---------------------------------------------------------------- tools
    @_guard
    def markets(self, a: dict) -> str:
        limit = max(1, min(int(a.get("limit") or 8), 20))
        if a.get("market"):
            m = pm.get_market(a["market"])
            if not m:
                return _err(f"no market found for '{a['market']}'")
            for o in m["outcomes"]:
                if o.get("token_id") and not m["closed"]:
                    try:
                        b = pm.order_book(o["token_id"])
                        o["best_bid"] = b["bids"][0][0] if b["bids"] else None
                        o["best_ask"] = b["asks"][0][0] if b["asks"] else None
                        o["ask_depth_usd"] = round(sum(p * s for p, s in b["asks"]), 2)
                        o["bid_depth_usd"] = round(sum(p * s for p, s in b["bids"]), 2)
                    except pm.PolymarketError as exc:
                        o["book_error"] = str(exc)
                o.pop("token_id", None)
            return _ok({"market": m})
        rows = pm.search_markets(a["query"], limit) if a.get("query") else pm.trending(limit)
        for m in rows:
            for o in m["outcomes"]:
                o.pop("token_id", None)
        return _ok({"count": len(rows), "markets": rows})

    @_guard
    def trade(self, a: dict) -> str:
        if a.get("ticket_id"):
            return _ok({**self.desk.execute(str(a["ticket_id"]).strip()), "note": DISCLAIMER})
        for k in ("market", "outcome", "side"):
            if not a.get(k):
                return _err(f"'{k}' is required to preview a trade (or pass ticket_id to execute one)")
        amt = float(a["amount_usd"]) if a.get("amount_usd") is not None else None
        sh = float(a["shares"]) if a.get("shares") is not None else None
        if bool(self.setting("auto_execute", False)):
            res = self.desk.trade_now(a["market"], a["outcome"], a["side"], amt, sh)
        else:
            res = self.desk.preview(a["market"], a["outcome"], a["side"], amt, sh)
            if res.get("status") == "awaiting_confirmation":
                res["next_step"] = ("Show this quote to the user and ask them to confirm. If they say yes, "
                                    f"call polydesk_trade with ticket_id='{res['ticket_id']}'.")
        return _ok({**res, "note": DISCLAIMER})

    @_guard
    def portfolio(self, a: dict) -> str:
        return _ok(self.desk.portfolio())

    @_guard
    def history(self, a: dict) -> str:
        return _ok({"activity": self.desk.history(int(a.get("limit") or 20))})

    @_guard
    def wallet(self, a: dict) -> str:
        action = (a.get("action") or "status").lower()
        rpc = self.setting("rpc_url", wl.DEFAULT_RPC) or wl.DEFAULT_RPC
        if action == "link":
            addr = (a.get("address") or "").strip()
            if not wl.valid_address(addr):
                return _err("give a valid 0x… address (40 hex chars). Never share a private key.")
            bal = wl.balances(addr, rpc)
            self.desk.link_wallet(addr)
            return _ok({"linked": True, **bal,
                        "next_step": "Deposit with action=deposit and amount_usd (capped at the testnet USDC balance)."})
        addr = self.desk.wallet() or self.setting("wallet_address", None)
        if not addr:
            return _err("no wallet linked yet: call polydesk_wallet with action=link and a 0x address")
        if action == "status":
            pf = self.desk.portfolio(settle=False)
            return _ok({**wl.balances(addr, rpc), "paper_bankroll": pf["bankroll"], "cash_usd": pf["cash_usd"]})
        if action == "deposit":
            if self.desk.wallet() is None:
                self.desk.link_wallet(addr)
            amount = float(a.get("amount_usd") or 0)
            bal = wl.balances(addr, rpc)
            return _ok({**self.desk.deposit_backed(amount, bal["usdc"]), "onchain_usdc": bal["usdc"],
                        "explorer": bal["explorer"]})
        return _err("action must be status, link or deposit")

    # ---------------------------------------------------------------- /desk command
    def command(self, raw: str = "", **kwargs) -> str:
        parts = (raw or "").split()
        sub = parts[0].lower() if parts else ""
        try:
            if sub == "reset":
                if len(parts) < 2 or parts[1] != "confirm":
                    return "This wipes all paper positions and history. Run `/desk reset confirm` to proceed."
                self.desk.reset()
                return "Paper desk reset. Demo bankroll restored; linked wallet kept."
            if sub == "history":
                rows = self.desk.history(10)
                if not rows:
                    return "No activity yet."
                return "\n".join(
                    f"{r['time']}  {r['kind']:<7} {r['outcome'] or '':<5} "
                    f"{(r['market'] or r['note'] or '')[:48]:<48} ${r['usd'] or 0:>9.2f}" for r in rows)
            return render_portfolio(self.desk.portfolio())
        except Exception as exc:
            return f"polydesk: {exc}"


def render_portfolio(p: dict) -> str:
    sign = lambda x: f"+{x:.2f}" if (x or 0) >= 0 else f"{x:.2f}"  # noqa: E731
    lines = [
        "📈 Polydesk (paper trading on live Polymarket books)",
        f"Equity ${p['equity_usd']:.2f}   Cash ${p['cash_usd']:.2f}   "
        f"P&L {sign(p['total_pnl_usd'])}"
        + (f" ({p['total_return_pct']:+.2f}%)" if p.get("total_return_pct") is not None else ""),
    ]
    b = p["bankroll"]
    lines.append(f"Bankroll: ${b['testnet_backed_usd']:.2f} backed by Base Sepolia USDC"
                 + (f" ({b['wallet'][:6]}…{b['wallet'][-4:]})" if b.get("wallet") else "")
                 + f", ${b['unbacked_demo_usd']:.2f} demo")
    for s in p.get("settled_now") or []:
        lines.append(f"✔ Settled {s['result']}: {s['market'][:50]} [{s['outcome']}] {sign(s['realized_pnl'])}")
    if not p["positions"]:
        lines.append("No open positions. Ask Hermes to find a market and paper-trade it.")
    for x in p["positions"]:
        mid = f"{x['mid']:.3f}" if x["mid"] is not None else "  n/a"
        lines.append(f"• {x['market'][:52]} [{x['outcome']}] {x['shares']:.2f} sh @ {x['avg_cost']:.3f} → "
                     f"{mid}  {sign(x['unrealized_pnl'])}")
    lines.append("Commands: /desk · /desk history · /desk reset")
    return "\n".join(lines)
