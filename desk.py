"""Paper-trading desk: SQLite ledger, order-book fill simulation, risk checks,
confirm tickets and settlement of resolved markets.

Every fill walks the *live* Polymarket order book level by level, so the price you
get is what a real market order of that size would have paid at that moment
(slippage included). Nothing here touches a real wallet.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from . import polymarket as pm

TICKET_TTL = 300.0  # long enough for a slow model plus a human reply; execution re-prices anyway
EPS = 1e-9

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS positions (
  token_id TEXT PRIMARY KEY, slug TEXT, question TEXT, outcome TEXT, url TEXT,
  shares REAL NOT NULL, cost REAL NOT NULL, opened REAL NOT NULL);
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, kind TEXT NOT NULL,
  token_id TEXT, slug TEXT, question TEXT, outcome TEXT,
  shares REAL, price REAL, usd REAL, realized REAL, note TEXT);
CREATE TABLE IF NOT EXISTS tickets (id TEXT PRIMARY KEY, created REAL NOT NULL, payload TEXT NOT NULL);
"""


# ============================================================================ fill engine

def simulate_buy(asks: list[tuple[float, float]], usd: float) -> dict:
    """Spend up to `usd` sweeping asks best-first."""
    left, shares, levels, worst = usd, 0.0, 0, None
    for price, size in asks:
        if left <= EPS:
            break
        take = min(size, left / price)
        shares += take
        left -= take * price
        levels += 1
        worst = price
    spent = usd - left
    return {"shares": shares, "usd": spent, "avg_price": spent / shares if shares else None,
            "best_price": asks[0][0] if asks else None, "worst_price": worst,
            "levels": levels, "unfilled_usd": max(0.0, left)}


def simulate_sell(bids: list[tuple[float, float]], shares: float) -> dict:
    """Sell up to `shares` sweeping bids best-first."""
    left, got, levels, worst = shares, 0.0, 0, None
    for price, size in bids:
        if left <= EPS:
            break
        take = min(size, left)
        got += take * price
        left -= take
        levels += 1
        worst = price
    sold = shares - left
    return {"shares": sold, "usd": got, "avg_price": got / sold if sold else None,
            "best_price": bids[0][0] if bids else None, "worst_price": worst,
            "levels": levels, "unfilled_shares": max(0.0, left)}


def _r(x: Optional[float], n: int = 4) -> Optional[float]:
    return None if x is None else round(x, n)


# ============================================================================ desk

class Desk:
    def __init__(self, db_path: Path, settings: Callable[[str, Any], Any]):
        self.db_path = Path(db_path)
        self.setting = settings
        self._lock = threading.RLock()
        with self._db() as c:
            c.executescript(_SCHEMA)

    # -------------------------------------------------------------- storage
    def _db(self) -> sqlite3.Connection:
        c = sqlite3.connect(str(self.db_path), timeout=10, check_same_thread=False)
        c.row_factory = sqlite3.Row
        return c

    def _meta(self, c, key: str, default: Any = None) -> Any:
        row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def _set(self, c, key: str, value: Any) -> None:
        c.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, json.dumps(value)))

    def _log(self, c, kind: str, **kw) -> None:
        c.execute("INSERT INTO trades(ts,kind,token_id,slug,question,outcome,shares,price,usd,realized,note)"
                  " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                  (time.time(), kind, kw.get("token_id"), kw.get("slug"), kw.get("question"),
                   kw.get("outcome"), kw.get("shares"), kw.get("price"), kw.get("usd"),
                   kw.get("realized"), kw.get("note")))

    def _ensure_bankroll(self, c) -> None:
        """First use: grant the unbacked demo bankroll unless a wallet is linked."""
        if self._meta(c, "initialized"):
            return
        self._set(c, "initialized", True)
        self._set(c, "cash", 0.0)
        self._set(c, "backed_deposits", 0.0)
        self._set(c, "realized", 0.0)
        demo = float(self.setting("demo_bankroll", 1000.0) or 0)
        if demo > 0:
            self._set(c, "cash", demo)
            self._set(c, "demo_credit", demo)
            self._log(c, "DEPOSIT", usd=demo, note="unbacked demo bankroll")

    # -------------------------------------------------------------- market resolution helpers
    @staticmethod
    def pick_outcome(market: dict, outcome: str) -> dict:
        want = (outcome or "").strip().lower()
        for o in market["outcomes"]:
            if o["name"].strip().lower() == want:
                return o
        names = ", ".join(o["name"] for o in market["outcomes"])
        raise ValueError(f"outcome '{outcome}' not in this market (choose one of: {names})")

    # -------------------------------------------------------------- quotes
    def quote(self, market_ref: str, outcome: str, side: str,
              amount_usd: Optional[float] = None, shares: Optional[float] = None) -> dict:
        market = pm.get_market(market_ref)
        if not market:
            raise ValueError(f"no Polymarket market found for '{market_ref}'")
        o = self.pick_outcome(market, outcome)
        if not o.get("token_id"):
            raise ValueError("this market has no order book token for that outcome")
        book = pm.order_book(o["token_id"])
        side = side.lower()
        if side == "buy":
            if not amount_usd or amount_usd <= 0:
                raise ValueError("amount_usd must be > 0 for a buy")
            fill = simulate_buy(book["asks"], float(amount_usd))
        elif side == "sell":
            if not shares or shares <= 0:
                raise ValueError("shares must be > 0 for a sell")
            fill = simulate_sell(book["bids"], float(shares))
        else:
            raise ValueError("side must be 'buy' or 'sell'")
        bid = book["bids"][0][0] if book["bids"] else None
        ask = book["asks"][0][0] if book["asks"] else None
        return {"market": market, "outcome": o, "side": side, "fill": fill,
                "book": {"best_bid": bid, "best_ask": ask,
                         "spread": _r(ask - bid) if bid is not None and ask is not None else None,
                         "bid_depth_usd": _r(sum(p * s for p, s in book["bids"]), 2),
                         "ask_depth_usd": _r(sum(p * s for p, s in book["asks"]), 2)}}

    # -------------------------------------------------------------- risk
    def _risk_check(self, c, q: dict) -> list[str]:
        problems = []
        m, f = q["market"], q["fill"]
        if m["closed"] or not m["active"] or not m["accepting_orders"]:
            problems.append("market is closed or not accepting orders")
        if not f["shares"]:
            problems.append("order book is empty on that side, nothing would fill")
            return problems
        slip_cap = float(self.setting("max_slippage", 0.03))
        if f["best_price"] is not None and f["worst_price"] is not None:
            slip = abs(f["worst_price"] - f["best_price"])
            if slip > slip_cap + EPS:
                problems.append(f"order would sweep the book {slip:.3f} away from best price "
                                f"(limit {slip_cap:.3f}); trade smaller")
        if q["side"] == "buy":
            cash = self._meta(c, "cash", 0.0)
            if f["usd"] > cash + EPS:
                problems.append(f"insufficient paper cash: need ${f['usd']:.2f}, have ${cash:.2f}")
            cap = float(self.setting("max_trade_usd", 250.0))
            if f["usd"] > cap + EPS:
                problems.append(f"trade ${f['usd']:.2f} exceeds max_trade_usd ${cap:.2f}")
            if f["unfilled_usd"] > 0.01:
                problems.append(f"only ${f['usd']:.2f} of liquidity available; ${f['unfilled_usd']:.2f} would not fill")
            pct = float(self.setting("max_position_pct", 25.0))
            equity = self._equity_estimate(c)
            row = c.execute("SELECT cost FROM positions WHERE token_id=?", (q["outcome"]["token_id"],)).fetchone()
            exposure = (row["cost"] if row else 0.0) + f["usd"]
            if equity > 0 and exposure / equity * 100 > pct + EPS:
                problems.append(f"position would be {exposure / equity * 100:.1f}% of equity "
                                f"(max_position_pct {pct:.0f}%)")
        else:
            row = c.execute("SELECT shares FROM positions WHERE token_id=?", (q["outcome"]["token_id"],)).fetchone()
            held = row["shares"] if row else 0.0
            if q["fill"]["shares"] + q["fill"]["unfilled_shares"] > held + 1e-6:
                problems.append(f"you hold {held:.4f} shares of that outcome")
            if f["unfilled_shares"] > 1e-6:
                problems.append(f"only {f['shares']:.2f} shares of bid liquidity; {f['unfilled_shares']:.2f} would not fill")
        return problems

    def _equity_estimate(self, c) -> float:
        cash = self._meta(c, "cash", 0.0)
        cost = c.execute("SELECT COALESCE(SUM(cost),0) AS s FROM positions").fetchone()["s"]
        return cash + cost

    # -------------------------------------------------------------- trading
    def preview(self, market_ref: str, outcome: str, side: str,
                amount_usd: Optional[float] = None, shares: Optional[float] = None) -> dict:
        with self._lock, self._db() as c:
            self._ensure_bankroll(c)
            shares = self._default_shares(c, market_ref, outcome, side, shares)
            q = self.quote(market_ref, outcome, side, amount_usd, shares)
            problems = self._risk_check(c, q)
            out = self._describe(q)
            if problems:
                out.update(status="rejected", problems=problems)
                return out
            tid = "T" + secrets.token_hex(4)
            payload = {"market_ref": q["market"]["slug"] or market_ref, "outcome": q["outcome"]["name"],
                       "side": q["side"], "amount_usd": amount_usd, "shares": shares,
                       "quoted_avg": q["fill"]["avg_price"]}
            c.execute("DELETE FROM tickets WHERE created < ?", (time.time() - TICKET_TTL,))
            c.execute("INSERT INTO tickets(id,created,payload) VALUES(?,?,?)", (tid, time.time(), json.dumps(payload)))
            out.update(status="awaiting_confirmation", ticket_id=tid, expires_in_s=int(TICKET_TTL))
            return out

    def execute(self, ticket_id: str) -> dict:
        with self._lock, self._db() as c:
            self._ensure_bankroll(c)
            row = c.execute("SELECT created,payload FROM tickets WHERE id=?", (ticket_id,)).fetchone()
            if not row:
                raise ValueError("unknown or already-used ticket_id; preview the trade again")
            c.execute("DELETE FROM tickets WHERE id=?", (ticket_id,))
            if time.time() - row["created"] > TICKET_TTL:
                raise ValueError("ticket expired (prices move); preview the trade again")
            t = json.loads(row["payload"])
            return self._fill(c, t)

    def _default_shares(self, c, market_ref, outcome, side, shares):
        """A sell with no share count means 'sell the whole position'."""
        if side.lower() != "sell" or shares:
            return shares
        m = pm.get_market(market_ref)
        if not m:
            return shares
        o = self.pick_outcome(m, outcome)
        row = c.execute("SELECT shares FROM positions WHERE token_id=?", (o["token_id"],)).fetchone()
        if not row:
            raise ValueError(f"you hold no '{o['name']}' shares in this market")
        return row["shares"]

    def trade_now(self, market_ref, outcome, side, amount_usd=None, shares=None) -> dict:
        with self._lock, self._db() as c:
            self._ensure_bankroll(c)
            shares = self._default_shares(c, market_ref, outcome, side, shares)
            return self._fill(c, {"market_ref": market_ref, "outcome": outcome, "side": side,
                                  "amount_usd": amount_usd, "shares": shares, "quoted_avg": None})

    def _fill(self, c, t: dict) -> dict:
        pm.clear_cache()  # always fill against a fresh book
        q = self.quote(t["market_ref"], t["outcome"], t["side"], t.get("amount_usd"), t.get("shares"))
        problems = self._risk_check(c, q)
        f = q["fill"]
        if t.get("quoted_avg") and f["avg_price"] is not None:
            drift = f["avg_price"] - t["quoted_avg"]
            adverse = drift if t["side"] == "buy" else -drift
            if adverse > float(self.setting("max_slippage", 0.03)) + EPS:
                problems.append(f"price moved against you since the quote "
                                f"({t['quoted_avg']:.4f} → {f['avg_price']:.4f}); re-preview")
        out = self._describe(q)
        if problems:
            out.update(status="rejected", problems=problems)
            return out

        m, o = q["market"], q["outcome"]
        cash = self._meta(c, "cash", 0.0)
        row = c.execute("SELECT shares,cost FROM positions WHERE token_id=?", (o["token_id"],)).fetchone()
        realized = None
        if q["side"] == "buy":
            cash -= f["usd"]
            if row:
                c.execute("UPDATE positions SET shares=shares+?, cost=cost+? WHERE token_id=?",
                          (f["shares"], f["usd"], o["token_id"]))
            else:
                c.execute("INSERT INTO positions VALUES(?,?,?,?,?,?,?,?)",
                          (o["token_id"], m["slug"], m["question"], o["name"], m["url"],
                           f["shares"], f["usd"], time.time()))
        else:
            cash += f["usd"]
            basis = row["cost"] * (f["shares"] / row["shares"])
            realized = f["usd"] - basis
            self._set(c, "realized", self._meta(c, "realized", 0.0) + realized)
            if row["shares"] - f["shares"] <= 1e-6:
                c.execute("DELETE FROM positions WHERE token_id=?", (o["token_id"],))
            else:
                c.execute("UPDATE positions SET shares=shares-?, cost=cost-? WHERE token_id=?",
                          (f["shares"], basis, o["token_id"]))
        self._set(c, "cash", cash)
        self._log(c, q["side"].upper(), token_id=o["token_id"], slug=m["slug"], question=m["question"],
                  outcome=o["name"], shares=f["shares"], price=f["avg_price"], usd=f["usd"], realized=realized)
        out.update(status="filled", cash_after=_r(cash, 2), realized_pnl=_r(realized, 2))
        return out

    @staticmethod
    def _describe(q: dict) -> dict:
        f = q["fill"]
        return {
            "market": q["market"]["question"], "url": q["market"]["url"], "slug": q["market"]["slug"],
            "outcome": q["outcome"]["name"], "side": q["side"],
            "shares": _r(f["shares"]), "avg_price": _r(f["avg_price"]), "usd": _r(f["usd"], 2),
            "best_price": f["best_price"], "worst_price": f["worst_price"], "book_levels_swept": f["levels"],
            "max_payout_usd": _r(f["shares"], 2) if q["side"] == "buy" else None,
            "implied_probability": _r(f["avg_price"]),
            "book": q["book"],
        }

    # -------------------------------------------------------------- portfolio / settlement
    def portfolio(self, settle: bool = True) -> dict:
        with self._lock, self._db() as c:
            self._ensure_bankroll(c)
            settled = self._settle(c) if settle else []
            rows = c.execute("SELECT * FROM positions ORDER BY opened").fetchall()
            positions, mark_total, liq_total, errors = [], 0.0, 0.0, []
            for r in rows:
                bid = ask = mid = None
                try:
                    b = pm.order_book(r["token_id"])
                    bid = b["bids"][0][0] if b["bids"] else None
                    ask = b["asks"][0][0] if b["asks"] else None
                    mid = (bid + ask) / 2 if bid is not None and ask is not None else (bid or ask)
                except pm.PolymarketError as exc:
                    errors.append(f"{r['question']}: {exc}")
                mark = (mid if mid is not None else r["cost"] / r["shares"]) * r["shares"]
                liq = (bid or 0.0) * r["shares"]
                mark_total += mark
                liq_total += liq
                positions.append({
                    "market": r["question"], "outcome": r["outcome"], "url": r["url"], "slug": r["slug"],
                    "shares": _r(r["shares"]), "avg_cost": _r(r["cost"] / r["shares"]),
                    "cost_usd": _r(r["cost"], 2), "mid": _r(mid), "best_bid": bid,
                    "value_usd": _r(mark, 2), "unrealized_pnl": _r(mark - r["cost"], 2),
                    "unrealized_pct": _r((mark - r["cost"]) / r["cost"] * 100, 1) if r["cost"] else None,
                })
            cash = self._meta(c, "cash", 0.0)
            backed = self._meta(c, "backed_deposits", 0.0)
            demo = self._meta(c, "demo_credit", 0.0)
            equity = cash + mark_total
            contributed = backed + demo
            return {
                "cash_usd": _r(cash, 2), "positions_value_usd": _r(mark_total, 2),
                "liquidation_value_usd": _r(cash + liq_total, 2), "equity_usd": _r(equity, 2),
                "total_pnl_usd": _r(equity - contributed, 2),
                "total_return_pct": _r((equity - contributed) / contributed * 100, 2) if contributed else None,
                "realized_pnl_usd": _r(self._meta(c, "realized", 0.0), 2),
                "bankroll": {"testnet_backed_usd": _r(backed, 2), "unbacked_demo_usd": _r(demo, 2),
                             "wallet": self._meta(c, "wallet")},
                "positions": positions, "settled_now": settled, "warnings": errors,
            }

    def _settle(self, c) -> list[dict]:
        out = []
        for r in c.execute("SELECT * FROM positions").fetchall():
            try:
                m = pm.get_market(r["slug"], fresh=True)
            except pm.PolymarketError:
                continue
            if not m or not m["closed"]:
                continue
            price = next((o["price"] for o in m["outcomes"] if o["token_id"] == r["token_id"]), None)
            if price is None or 0.01 < price < 0.99:
                continue  # closed but not yet resolved
            payout = r["shares"] * (1.0 if price >= 0.99 else 0.0)
            realized = payout - r["cost"]
            self._set(c, "cash", self._meta(c, "cash", 0.0) + payout)
            self._set(c, "realized", self._meta(c, "realized", 0.0) + realized)
            c.execute("DELETE FROM positions WHERE token_id=?", (r["token_id"],))
            self._log(c, "SETTLE", token_id=r["token_id"], slug=r["slug"], question=r["question"],
                      outcome=r["outcome"], shares=r["shares"], price=1.0 if payout else 0.0,
                      usd=payout, realized=realized, note="market resolved")
            out.append({"market": r["question"], "outcome": r["outcome"],
                        "result": "WON" if payout else "LOST", "payout_usd": _r(payout, 2),
                        "realized_pnl": _r(realized, 2)})
        return out

    def history(self, limit: int = 20) -> list[dict]:
        with self._db() as c:
            rows = c.execute("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (max(1, min(limit, 200)),)).fetchall()
        return [{"time": time.strftime("%Y-%m-%d %H:%M", time.localtime(r["ts"])), "kind": r["kind"],
                 "market": r["question"], "outcome": r["outcome"], "shares": _r(r["shares"]),
                 "price": _r(r["price"]), "usd": _r(r["usd"], 2), "realized": _r(r["realized"], 2),
                 "note": r["note"]} for r in rows]

    # -------------------------------------------------------------- wallet-backed bankroll
    def link_wallet(self, address: str) -> None:
        with self._lock, self._db() as c:
            self._ensure_bankroll(c)
            self._set(c, "wallet", address)

    def wallet(self) -> Optional[str]:
        with self._db() as c:
            return self._meta(c, "wallet")

    def deposit_backed(self, amount: float, onchain_usdc: float) -> dict:
        with self._lock, self._db() as c:
            self._ensure_bankroll(c)
            backed = self._meta(c, "backed_deposits", 0.0)
            room = onchain_usdc - backed
            if amount <= 0:
                raise ValueError("amount must be > 0")
            if amount > room + EPS:
                raise ValueError(f"wallet holds {onchain_usdc:.2f} testnet USDC and {backed:.2f} is already "
                                 f"deposited; at most {max(room, 0):.2f} more can be backed")
            self._set(c, "backed_deposits", backed + amount)
            cash = self._meta(c, "cash", 0.0) + amount
            self._set(c, "cash", cash)
            self._log(c, "DEPOSIT", usd=amount, note=f"backed by Base Sepolia USDC in {self._meta(c, 'wallet')}")
            return {"deposited": _r(amount, 2), "cash_usd": _r(cash, 2),
                    "testnet_backed_usd": _r(backed + amount, 2)}

    def reset(self) -> None:
        with self._lock, self._db() as c:
            wallet = self._meta(c, "wallet")
            for t in ("meta", "positions", "trades", "tickets"):
                c.execute(f"DELETE FROM {t}")
            self._ensure_bankroll(c)
            if wallet:
                self._set(c, "wallet", wallet)
